"""Default branch detection, consistent checkout/PR targets, and retry recovery."""
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from . import execution
from .integrations import GitHubClient, IntegrationError
from .models import Investigation, Repository
from .pipeline import run_investigation
from .queue import claim_next
from .task_context import coding_task
from .tests import LIVE_SETTINGS


@override_settings(**LIVE_SETTINGS)
class DefaultBranchProviderTests(SimpleTestCase):
    def setUp(self):
        self.repository = Repository(name="checkout", url="https://github.com/acme/checkout", default_branch="main")

    @patch.object(GitHubClient, "call")
    def test_default_branch_comes_from_github_instead_of_the_registry(self, call):
        for branch in ("master", "canary", "develop", "release/next"):
            with self.subTest(branch=branch):
                call.return_value = {"default_branch": branch}
                self.assertEqual(GitHubClient().default_branch(self.repository), branch)
                self.assertEqual(call.call_args.args, ("GET", "/repos/acme/checkout"))

    @patch.object(GitHubClient, "call")
    def test_missing_or_invalid_metadata_never_falls_back_to_main(self, call):
        for value in (None, "", "--help", "../main", "trunk\n", 12):
            call.return_value = {"default_branch": value}
            with self.assertRaisesMessage(IntegrationError, "no branch was guessed"):
                GitHubClient().default_branch(self.repository)

    @patch.object(GitHubClient, "call")
    def test_repository_discovery_reads_the_actual_default_branch(self, call):
        call.side_effect = [
            {"default_branch": "release/next"},
            {"tree": [{"path": "README.md", "type": "blob"}]},
            {"encoding": "base64", "content": "SGVsbG8="},
        ]
        self.assertEqual(GitHubClient().context(self.repository)["readme"], "Hello")
        self.assertIn("/trees/release%2Fnext?", call.call_args_list[1].args[1])
        self.assertIn("ref=release%2Fnext", call.call_args_list[2].args[1])

    @patch.object(GitHubClient, "call")
    def test_pr_uses_the_branch_recorded_for_the_investigation(self, call):
        call.side_effect = [[], {"html_url": "https://github.com/acme/checkout/pull/1"}]
        GitHubClient().create_pull_request(self.repository, "codex/fix", "Fix", "Body", base_branch="develop")
        self.assertEqual(call.call_args.args[2]["base"], "develop")
        self.assertEqual(call.call_count, 2)

    @patch.object(GitHubClient, "call")
    def test_standalone_pr_creation_also_detects_the_default(self, call):
        call.side_effect = [[], {"default_branch": "canary"}, {"html_url": "https://github.com/acme/checkout/pull/1"}]
        GitHubClient().create_pull_request(self.repository, "codex/fix", "Fix", "Body")
        self.assertEqual(call.call_args.args[2]["base"], "canary")

    @patch.object(GitHubClient, "call")
    def test_pr_recovery_is_not_filtered_by_a_changed_default_branch(self, call):
        call.return_value = [{"html_url": "https://github.com/acme/checkout/pull/1"}]
        result = GitHubClient().create_pull_request(self.repository, "codex/fix", "Fix", "Body", base_branch="canary")
        query = parse_qs(urlsplit(call.call_args.args[1]).query)
        self.assertNotIn("base", query)
        self.assertEqual(query["head"], ["acme:codex/fix"])
        self.assertEqual(call.call_count, 1)
        self.assertIn("/pull/1", result["html_url"])


@override_settings(**LIVE_SETTINGS)
class DefaultBranchPipelineTests(TestCase):
    def test_registry_does_not_require_or_accept_a_branch_override(self):
        client = APIClient()
        client.force_authenticate(get_user_model().objects.create_user("owner", is_staff=True))
        response = client.post("/api/repositories/", {"name": "checkout", "url": "https://github.com/acme/checkout", "default_branch": "main"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["default_branch"], "")

    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.GitHubClient")
    def test_retry_keeps_checkout_and_pr_target_consistent_after_default_changes(self, github_class, preprocessor, executor):
        repository = Repository.objects.create(name="checkout", url="https://github.com/acme/checkout", default_branch="main", validation_commands=[["pytest"]])
        run = Investigation.objects.create(issue_key="acme/checkout#1", source_repository_url=repository.url, source_issue_number=1, requested_repository=repository)
        github = github_class.return_value
        github.default_branch.return_value = "develop"
        github.existing_pull_request.return_value = None
        github.issue.return_value = {"title": "Crash", "url": repository.url + "/issues/1"}
        preprocessor.return_value.preprocess.return_value = {"summary": "Crash"}
        executor.redact.side_effect = lambda value: value
        executor.investigate_and_fix.return_value = {"summary": "Fix"}
        executor.validate_proposal.side_effect = [IntegrationError("Test failure"), {"passed": True, "checks": []}]
        executor.publish_pull_request.return_value = {"html_url": repository.url + "/pull/1"}
        github.comment_issue_pull_request.return_value = {"id": "42", "url": repository.url + "/issues/1#issuecomment-42"}
        claim_next()
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "needs_attention")
        self.assertEqual(run.base_branch, "develop")
        self.assertEqual(coding_task(run)["repository"]["base_branch"], "develop")
        github.default_branch.return_value = "main"
        run.status = "queued"
        run.save(update_fields=["status"])
        claim_next()
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "pr_created")
        self.assertEqual(run.base_branch, "develop")
        github.default_branch.assert_called_once()


@override_settings(**LIVE_SETTINGS)
class DefaultBranchCheckoutTests(SimpleTestCase):
    def test_actual_checkout_starts_at_recorded_default_even_when_main_exists(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        self.enterContext(override_settings(BUGPILOT_WORKSPACE_ROOT=directory / "workspaces"))
        upstream = directory / "upstream"
        upstream.mkdir()
        execution.git(["init", "-b", "develop"], cwd=upstream)
        (upstream / "marker.txt").write_text("initial", encoding="utf-8")
        execution.git(["add", "."], cwd=upstream)
        execution.git(["commit", "-m", "initial commit"], cwd=upstream)
        (upstream / "marker.txt").write_text("develop", encoding="utf-8")
        execution.git(["add", "."], cwd=upstream)
        execution.git(["commit", "-m", "develop commit"], cwd=upstream)
        develop_sha = execution.git(["rev-parse", "HEAD"], cwd=upstream)
        execution.git(["checkout", "-b", "main"], cwd=upstream)
        (upstream / "marker.txt").write_text("main", encoding="utf-8")
        execution.git(["commit", "-am", "main commit"], cwd=upstream)
        run = Investigation(branch="codex/fix", base_branch="develop", attempt=1)
        run.repository = Repository(name="checkout", url="https://github.com/acme/checkout", default_branch="main")
        run.save = Mock()
        original_git = execution.git

        def local_git(args, **kwargs):
            args = [upstream.as_uri() if arg == run.repository.url + ".git" else arg for arg in args]
            kwargs.pop("authenticated", None)
            return original_git(args, **kwargs)

        with patch("investigations.execution.git", side_effect=local_git):
            workspace = execution.prepare_workspace(run)
        self.assertEqual((workspace / "marker.txt").read_text(), "develop")
        self.assertEqual(run.base_sha, develop_sha)
        self.assertEqual(execution.git(["rev-list", "--count", "HEAD"], cwd=workspace), "1")
        self.assertEqual(execution.git(["rev-parse", "--is-shallow-repository"], cwd=workspace), "true")
        self.assertEqual(execution.git(["branch", "--show-current"], cwd=workspace), "codex/fix")
