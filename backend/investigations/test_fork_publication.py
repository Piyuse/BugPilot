import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from .integrations import GitHubClient, IntegrationError
from .models import Investigation, Repository
from .pipeline import run_investigation
from . import execution


UPSTREAM = {"id": 12, "permissions": {"push": False}}
FORK = {"id": 24, "full_name": "contributor/project", "owner": {"login": "contributor"}, "fork": True,
        "parent": {"id": 12}, "default_branch": "trunk", "permissions": {"push": True}}


@override_settings(GITHUB_TOKEN="test")
class ForkProviderTests(SimpleTestCase):
    def setUp(self):
        self.repository = Repository(name="project", url="https://github.com/upstream/project")

    @patch.object(GitHubClient, "call")
    def test_direct_access_does_not_create_a_fork(self, call):
        call.return_value = {"permissions": {"push": True}}
        self.assertEqual(GitHubClient().publication_target(self.repository), {"url": self.repository.url, "head_owner": "upstream"})
        call.assert_called_once()

    @patch("investigations.integrations.time.sleep")
    @patch.object(GitHubClient, "call")
    def test_fork_is_created_once_and_waited_for(self, call, sleep):
        call.side_effect = [UPSTREAM, {"login": "contributor"}, IntegrationError("missing", status_code=404), FORK,
                            IntegrationError("initializing", status_code=409), {"object": {"sha": "abc"}}]
        target = GitHubClient().publication_target(self.repository)
        self.assertEqual(target, {"url": "https://github.com/contributor/project", "head_owner": "contributor"})
        posts = [item for item in call.call_args_list if item.args[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].args, ("POST", "/repos/upstream/project/forks", {"default_branch_only": True}))
        sleep.assert_called_once_with(2)

    @patch.object(GitHubClient, "call")
    def test_existing_fork_is_reused(self, call):
        call.side_effect = [UPSTREAM, {"login": "contributor"}, FORK, {"object": {"sha": "abc"}}]
        self.assertEqual(GitHubClient().publication_target(self.repository)["head_owner"], "contributor")
        self.assertTrue(all(item.args[0] == "GET" for item in call.call_args_list))

    @patch.object(GitHubClient, "call")
    def test_same_name_unrelated_repository_is_never_used(self, call):
        call.side_effect = [UPSTREAM, {"login": "contributor"}, {**FORK, "parent": {"id": 99}}]
        with self.assertRaisesMessage(IntegrationError, "not a fork"):
            GitHubClient().publication_target(self.repository)
        self.assertEqual(call.call_count, 3)

    @patch.object(GitHubClient, "call")
    def test_failed_permissions_are_not_mistaken_for_a_missing_fork(self, call):
        call.side_effect = [UPSTREAM, {"login": "contributor"}, IntegrationError("forbidden", status_code=403)]
        with self.assertRaisesMessage(IntegrationError, "forbidden"):
            GitHubClient().publication_target(self.repository)
        self.assertTrue(all(item.args[0] == "GET" for item in call.call_args_list))

    @patch.object(GitHubClient, "call")
    def test_lost_creation_response_recovers_the_fork_without_reposting(self, call):
        call.side_effect = [UPSTREAM, {"login": "contributor"}, IntegrationError("missing", status_code=404),
                            IntegrationError("connection lost"), FORK, {"object": {"sha": "abc"}}]
        self.assertEqual(GitHubClient().publication_target(self.repository)["head_owner"], "contributor")
        self.assertEqual(sum(item.args[0] == "POST" for item in call.call_args_list), 1)

    @patch.object(GitHubClient, "call")
    def test_pr_targets_upstream_with_qualified_fork_head_and_recorded_base(self, call):
        call.side_effect = [[], {"html_url": self.repository.url + "/pull/1"}]
        GitHubClient().create_pull_request(self.repository, "codex/fix", "Fix", "Body", base_branch="develop", head_owner="contributor")
        self.assertEqual(call.call_args.args[1], "/repos/upstream/project/pulls")
        self.assertEqual(call.call_args.args[2]["head"], "contributor:codex/fix")
        self.assertEqual(call.call_args.args[2]["base"], "develop")
        query = parse_qs(urlsplit(call.call_args_list[0].args[1]).query)
        self.assertEqual(query["head"], ["contributor:codex/fix"])

    @patch.object(GitHubClient, "call")
    def test_lost_pr_response_recovers_without_duplicate_post(self, call):
        call.side_effect = [[], IntegrationError("response lost"), [{"html_url": self.repository.url + "/pull/1"}]]
        result = GitHubClient().create_pull_request(self.repository, "codex/fix", "Fix", "Body", base_branch="trunk", head_owner="contributor")
        self.assertEqual(result["html_url"], self.repository.url + "/pull/1")
        self.assertEqual(sum(item.args[0] == "POST" for item in call.call_args_list), 1)


@override_settings(OPENAI_API_KEY="", CODEX_API_KEY="", GITHUB_TOKEN="test")
class PublicationRecoveryTests(TestCase):
    def setUp(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.enterContext(override_settings(BUGPILOT_WORKSPACE_ROOT=Path(self.temp.name)))
        self.repository = Repository.objects.create(name="project", url="https://github.com/upstream/project", validation_commands=[["pytest"]])
        self.run = Investigation.objects.create(issue_key="upstream/project#1", source_repository_url=self.repository.url,
            source_issue_number=1, repository=self.repository, status="running", attempt=3, publication_attempt=2,
            publication_head_owner="contributor", branch="codex/fix", base_branch="trunk", base_sha="a" * 40, validation={"passed": True}, proposal={"summary": "Fix"})
        self.workspace = Path(self.temp.name) / str(self.run.pk) / "attempt-2"
        self.workspace.mkdir(parents=True)

    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_recovery_revalidates_existing_workspace_and_publishes_without_models(self, github, executor, model):
        github.return_value.existing_pull_request.return_value = None
        github.return_value.comment_issue_pull_request.return_value = {"id": "1", "url": self.repository.url + "/issues/1#issuecomment-1"}
        executor.validate_proposal.return_value = {"passed": True, "validated_tree": "a" * 40}
        executor.publish_pull_request.return_value = {"html_url": self.repository.url + "/pull/1"}
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "pr_created")
        self.assertEqual(executor.validate_proposal.call_args.args[1], self.workspace)
        self.assertEqual(self.run.publication_attempt, 2)
        github.return_value.existing_pull_request.assert_called_once_with(self.repository, "codex/fix", head_owner="contributor")
        executor.investigate_and_fix.assert_not_called()
        executor.prepare_workspace.assert_not_called()
        model.assert_not_called()

    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_failed_revalidation_blocks_push_and_pr(self, github, executor):
        github.return_value.existing_pull_request.return_value = None
        executor.redact.side_effect = lambda value: value
        executor.validate_proposal.side_effect = IntegrationError("regression failed")
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "needs_attention")
        executor.publish_pull_request.assert_not_called()

    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_existing_fork_pr_is_linked_without_republishing(self, github, executor):
        github.return_value.existing_pull_request.return_value = {"html_url": self.repository.url + "/pull/1"}
        github.return_value.comment_issue_pull_request.return_value = {"id": "1", "url": "comment"}
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "pr_created")
        executor.publish_pull_request.assert_not_called()
        executor.validate_proposal.assert_not_called()

    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_validation_timeout_retry_reuses_patch_without_models(self, github, executor, model):
        self.run.publication_attempt = 0
        self.run.validation = {"passed": False, "workspace_attempt": 2}
        self.run.save()
        github.return_value.existing_pull_request.return_value = None
        github.return_value.comment_issue_pull_request.return_value = {"id": "1", "url": "comment"}
        executor.validate_proposal.return_value = {"passed": True, "validated_tree": "a" * 40}
        executor.publish_pull_request.return_value = {"html_url": self.repository.url + "/pull/1"}
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "pr_created")
        self.assertEqual(self.run.publication_attempt, 2)
        self.assertEqual(executor.validate_proposal.call_args.args[1], self.workspace)
        executor.prepare_workspace.assert_not_called()
        executor.investigate_and_fix.assert_not_called()
        model.assert_not_called()

    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_repeated_validation_failure_keeps_original_workspace_and_diagnostics(self, github, executor):
        self.run.publication_attempt = 0
        self.run.validation = {"passed": False, "workspace_attempt": 2}
        self.run.save()
        github.return_value.existing_pull_request.return_value = None
        executor.redact.side_effect = lambda value: value
        error = IntegrationError("timeout")
        error.validation = {"passed": False, "workspace_attempt": 3, "checks": [{"output": "test started", "timed_out": True}]}
        executor.validate_proposal.side_effect = error
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "needs_attention")
        self.assertEqual(self.run.validation["workspace_attempt"], 2)
        self.assertEqual(self.run.validation["checks"][0]["output"], "test started")
        executor.publish_pull_request.assert_not_called()

    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_missing_saved_patch_does_not_start_a_replacement_model_run(self, github, executor, model):
        self.run.publication_attempt = 0
        self.run.validation = {"passed": False, "workspace_attempt": 2}
        self.run.save()
        self.workspace.rmdir()
        github.return_value.existing_pull_request.return_value = None
        executor.redact.side_effect = lambda value: value
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "needs_attention")
        self.assertIn("workspace is missing", self.run.error)
        executor.prepare_workspace.assert_not_called()
        model.assert_not_called()

    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_invalid_checkpoint_is_rejected_before_accessing_a_workspace(self, github, executor, model):
        self.run.publication_attempt = 0
        self.run.validation = {"workspace_attempt": "../../other"}
        self.run.save()
        github.return_value.existing_pull_request.return_value = None
        executor.redact.side_effect = lambda value: value
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "needs_attention")
        self.assertIn("checkpoint is invalid", self.run.error)
        executor.validate_proposal.assert_not_called()
        model.assert_not_called()

    @patch("investigations.pipeline.prepare_automatic_validation")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_saved_patch_refreshes_automatic_environment_before_running_checks(self, github, executor, prepare):
        self.repository.runtime_image = "bugpilot-auto-1:old-recipe"
        self.repository.save()
        order = []
        prepare.side_effect = lambda *args, **kwargs: order.append("prepare")
        executor.validate_proposal.side_effect = lambda *args, **kwargs: order.append("validate") or {"passed": True, "validated_tree": "a" * 40}
        executor.publish_pull_request.return_value = {"html_url": self.repository.url + "/pull/1"}
        github.return_value.existing_pull_request.return_value = None
        github.return_value.comment_issue_pull_request.return_value = {"id": "1", "url": "comment"}
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "pr_created")
        self.assertEqual(order, ["prepare", "validate"])
        prepare.assert_called_once()
        executor.investigate_and_fix.assert_not_called()


class ForkPushTests(SimpleTestCase):
    def setUp(self):
        self.run = Investigation(issue_key="upstream/project#1", title="Preserve mode", attempt=1, branch="codex/fix", base_branch="trunk",
            proposal={"summary": "Preserve mode", "root_cause": "Wrong initialization"},
            validation={"passed": True, "validated_tree": "a" * 40, "checks": [{"command": ["npm", "test"]}]})
        self.run.repository = Repository(name="project", url="https://github.com/upstream/project")
        self.run.save = MagicMock()

    @patch("investigations.execution.git")
    def test_push_goes_to_verified_fork_and_pr_targets_upstream(self, git):
        git.side_effect = lambda args, **kw: "a" * 40 if args[0] == "write-tree" else ""
        github = MagicMock()
        github.publication_target.return_value = {"url": "https://github.com/contributor/project", "head_owner": "contributor"}
        execution.publish_pull_request(self.run, Path("repo"), github)
        pushes = [item for item in git.call_args_list if item.args[0][0] == "push"]
        self.assertEqual(pushes[0].args[0], ["push", "--", "https://github.com/contributor/project.git", "HEAD:refs/heads/codex/fix"])
        self.assertEqual(github.create_pull_request.call_args.args[0], self.run.repository)
        self.assertEqual(github.create_pull_request.call_args.kwargs, {"base_branch": "trunk", "head_owner": "contributor"})

    @patch("investigations.execution.git", return_value="b" * 40)
    def test_changed_source_tree_cannot_be_published(self, git):
        github = MagicMock()
        with self.assertRaisesMessage(IntegrationError, "changed after validation"):
            execution.publish_pull_request(self.run, Path("repo"), github)
        github.publication_target.assert_not_called()
        self.assertTrue(all(item.args[0][0] != "push" for item in git.call_args_list))
