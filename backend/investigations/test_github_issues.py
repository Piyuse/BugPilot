"""GitHub issue intake, provider contracts and source migration coverage."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from .configuration import missing_live_settings
from .integrations import GitHubClient, IntegrationError
from .models import Investigation, Repository
from .pipeline import run_investigation, select_repository
from .repository_urls import parse_github_issue
from .test_live_pipeline import PROCESSED
from .tests import LIVE_SETTINGS


class GitHubIssueParsingTests(SimpleTestCase):
    def test_browser_api_and_qualified_paths_have_one_identity(self):
        expected = {
            "issue_key": "acme/checkout#123",
            "issue_url": "https://github.com/acme/checkout/issues/123",
            "source_repository_url": "https://github.com/acme/checkout",
            "source_issue_number": 123,
        }
        for value in [
            "https://github.com/Acme/Checkout/issues/123",
            "https://github.com/acme/checkout/issues/123#issuecomment-456",
            "https://api.github.com/repos/acme/checkout/issues/123",
            "acme/checkout/issues/123", "/repos/acme/checkout/issues/123",
        ]:
            with self.subTest(value=value):
                self.assertEqual(parse_github_issue(value), expected)

    def test_short_endpoints_need_repository_context(self):
        for value in ["issues/123", "/issues/123", "123", "#123"]:
            with self.subTest(value=value):
                result = parse_github_issue(value, "https://github.com/Acme/Checkout.git")
                self.assertEqual(result["issue_key"], "acme/checkout#123")
        with self.assertRaisesMessage(ValueError, "paste a repository"):
            parse_github_issue("issues/123")

    def test_invalid_hosts_paths_and_numbers_are_rejected(self):
        for value in [
            "https://evil.test/acme/checkout/issues/1", "http://github.com/acme/checkout/issues/1",
            "https://github.com.evil.test/acme/checkout/issues/1",
            "https://secret@github.com/acme/checkout/issues/1",
            "https://github.com/acme/checkout/pull/1", "issues/0", "issues/-1",
            "issues/1/../2", "issues/999999999999999999999", "https://github.com/acme/checkout/issues/1?token=x",
            "https://api.github.com/repos/acme/checkout/issues/1/comments",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_github_issue(value, "https://github.com/acme/checkout")


@override_settings(BUGPILOT_MODE="demo")
class GitHubIssueIntakeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user("dev"))
        self.repository = Repository.objects.create(name="checkout", url="https://github.com/acme/checkout")

    def test_full_url_queues_without_a_repository_hint(self):
        response = self.client.post("/api/investigations/", {"github_issue": "https://github.com/acme/checkout/issues/123"}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["issue_source"], "github")
        self.assertEqual(response.data["issue_key"], "acme/checkout#123")
        self.assertEqual(response.data["source_issue_number"], 123)
        self.assertIsNone(response.data["requested_repository"])

    def test_short_endpoint_and_pasted_repository_queue(self):
        response = self.client.post("/api/investigations/", {
            "github_issue": "issues/123", "repository": "https://github.com/acme/checkout.git",
        }, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["requested_repository"], "checkout")
        self.assertEqual(response.data["source_repository_url"], self.repository.url)

    def test_relative_endpoint_without_repository_is_actionable(self):
        response = self.client.post("/api/investigations/", {"github_issue": "issues/123"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("full GitHub issue URL", str(response.data))

    def test_browser_and_api_urls_deduplicate_but_other_repository_does_not(self):
        for value in ["https://github.com/acme/checkout/issues/123", "https://api.github.com/repos/acme/checkout/issues/123"]:
            self.client.post("/api/investigations/", {"github_issue": value}, format="json")
        self.assertEqual(Investigation.objects.count(), 1)
        self.client.post("/api/investigations/", {"github_issue": "https://github.com/acme/identity/issues/123"}, format="json")
        self.assertEqual(Investigation.objects.count(), 2)

    @override_settings(**{**LIVE_SETTINGS, "JIRA_BASE_URL": "", "JIRA_EMAIL": "", "JIRA_API_TOKEN": ""})
    def test_live_github_issue_needs_no_jira_credentials(self):
        response = self.client.post("/api/investigations/", {
            "github_issue": "https://github.com/acme/checkout/issues/123", "mode": "live",
        }, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(missing_live_settings(), [])
        self.assertIn("JIRA_API_TOKEN", missing_live_settings("jira"))

    def test_two_issue_sources_in_one_request_are_rejected(self):
        response = self.client.post("/api/investigations/", {
            "github_issue": "https://github.com/acme/checkout/issues/123", "jira_ticket": "PAY-123",
        }, format="json")
        self.assertEqual(response.status_code, 400)


@override_settings(GITHUB_TOKEN="github-test")
class GitHubIssueProviderTests(SimpleTestCase):
    @patch.object(GitHubClient, "call")
    def test_fetch_keeps_only_title_body_and_labels_for_preprocessing(self, call):
        call.return_value = {"title": "Checkout fails", "body": "Error traceback",
            "labels": [{"name": "bug"}], "user": {"email": "private@example.test"},
            "assignees": [{"login": "private-user"}], "reactions": {"total_count": 99}}
        result = GitHubClient().issue("https://github.com/acme/checkout", 123)
        self.assertEqual(call.call_args.args, ("GET", "/repos/acme/checkout/issues/123"))
        self.assertEqual(set(result), {"title", "description", "labels", "components", "url"})
        self.assertNotIn("private@example.test", json.dumps(result))
        self.assertNotIn("private-user", json.dumps(result))

    @patch.object(GitHubClient, "call", return_value={"title": "A PR", "pull_request": {}})
    def test_pull_request_disguised_as_issue_is_rejected(self, call):
        with self.assertRaisesMessage(IntegrationError, "pull request"):
            GitHubClient().issue("https://github.com/acme/checkout", 123)

    @patch.object(GitHubClient, "call", return_value={"title": "Empty body", "body": None, "labels": []})
    def test_empty_issue_body_is_supported(self, call):
        self.assertEqual(GitHubClient().issue("https://github.com/acme/checkout", 123)["description"], "")

    @patch.object(GitHubClient, "call")
    def test_pr_comment_is_markdown_with_a_stable_retry_marker(self, call):
        call.side_effect = [[], {"id": 9001}]
        result = GitHubClient().comment_issue_pull_request(
            "https://github.com/acme/checkout", 123, "https://github.com/acme/checkout/pull/7", "Fix checkout",
            investigation_id="run-123", branch="codex/gh-123-abc", summary="Guard a missing discount.",
            validation={"passed": True, "checks": [{"command": ["pytest"]}]},
        )
        args = call.call_args.args
        self.assertEqual(args[:2], ("POST", "/repos/acme/checkout/issues/123/comments"))
        self.assertIn("https://github.com/acme/checkout/pull/7", args[2]["body"])
        self.assertIn("<!-- bugpilot:investigation:run-123 -->", args[2]["body"])
        self.assertIn("`codex/gh-123-abc`", args[2]["body"])
        self.assertEqual(result["url"], "https://github.com/acme/checkout/issues/123#issuecomment-9001")

    @patch.object(GitHubClient, "call")
    def test_retries_recover_an_existing_comment_without_another_post(self, call):
        call.return_value = [{"id": 9001, "body": "<!-- bugpilot:investigation:run-123 --> https://github.com/acme/checkout/pull/7"}]
        result = GitHubClient().comment_issue_pull_request(
            "https://github.com/acme/checkout", 123, "https://github.com/acme/checkout/pull/7", "Fix",
            investigation_id="run-123", branch="branch", summary="", validation={})
        self.assertEqual(result["id"], "9001")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], "GET")


@override_settings(**{**LIVE_SETTINGS, "JIRA_BASE_URL": "", "JIRA_EMAIL": "", "JIRA_API_TOKEN": ""})
class GitHubIssuePipelineTests(TestCase):
    @patch("investigations.pipeline.JiraClient")
    @patch("investigations.pipeline.execution")
    @patch("investigations.integrations.request_json")
    def test_issue_to_openai_to_fix_to_pr_to_github_comment(self, transport, execution, jira):
        repo = Repository.objects.create(name="checkout", url="https://github.com/acme/checkout", validation_commands=[["pytest"]])
        run = Investigation.objects.create(
            issue_key="acme/checkout#123", issue_source="github", source_repository_url=repo.url,
            source_issue_number=123, mode="live", status="running", attempt=1, requested_repository=repo)
        def reply(method, url, **kwargs):
            if url == "https://api.github.com/repos/acme/checkout":
                return {"default_branch": "canary"}
            if url.endswith("/issues/123"):
                return {"title": "Checkout fails", "body": "Expected and actual behavior", "labels": [{"name": "bug"}]}
            if url == "https://api.openai.com/v1/responses":
                return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(PROCESSED)}]}]}
            if "/comments?" in url:
                return []
            if url.endswith("/comments") and method == "POST":
                return {"id": 9001}
            raise AssertionError(f"Unexpected transport request: {method} {url}")
        transport.side_effect = reply
        execution.investigate_and_fix.return_value = {"summary": "Guard discount", "root_cause": "Null guard", "tests_added": ["regression"]}
        execution.validate_proposal.return_value = {"passed": True, "checks": [{"command": ["pytest"]}]}
        execution.publish_pull_request.return_value = {"html_url": "https://github.com/acme/checkout/pull/7"}
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "pr_created")
        self.assertTrue(run.branch.startswith("codex/gh-123-"))
        self.assertEqual(run.preprocessed, PROCESSED)
        self.assertEqual(run.base_branch, "canary")
        self.assertEqual(run.issue_comment_id, "9001")
        self.assertTrue(run.issue_linked)
        self.assertEqual(run.model_calls["coding"]["provider"], "codex")
        jira.assert_not_called()
        self.assertEqual(sum(call.args[0] == "POST" and "/comments" in call.args[1] for call in transport.call_args_list), 1)

    def test_automatic_repository_is_inferred_from_the_issue_url(self):
        from unittest.mock import MagicMock
        hosting = Repository.objects.create(name="checkout", url="https://github.com/acme/checkout")
        Repository.objects.create(name="identity", url="https://github.com/acme/identity")
        run = Investigation.objects.create(issue_key="acme/checkout#123", source_repository_url=hosting.url, source_issue_number=123)
        openai, github = MagicMock(), MagicMock()
        selected, reason = select_repository(run, openai, github)
        self.assertEqual(selected, hosting)
        self.assertIn("issue URL", reason)
        openai.select.assert_not_called()

    @override_settings(OPENAI_API_KEY="", CODEX_API_KEY="")
    @patch("investigations.pipeline.GitHubClient")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    def test_comment_only_retry_does_not_repeat_models(self, openai, github):
        github.return_value.comment_issue_pull_request.return_value = {"id": "9001", "url": "https://github.com/acme/checkout/issues/123#issuecomment-9001"}
        run = Investigation.objects.create(issue_key="acme/checkout#123", source_repository_url="https://github.com/acme/checkout",
            source_issue_number=123, mode="live", status="running", attempt=1, pull_request_url="https://github.com/acme/checkout/pull/7")
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "pr_created")
        self.assertEqual(run.issue_comment_id, "9001")
        openai.assert_not_called()


class GitHubSourceMigrationTests(TransactionTestCase):
    def test_existing_jira_comment_checkpoints_survive_source_migration(self):
        old = [("investigations", "0005_alter_investigation_mode")]
        new = [("investigations", "0006_github_issue_intake")]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(old)
            old_model = executor.loader.project_state(old).apps.get_model("investigations", "Investigation")
            record = old_model.objects.create(issue_key="PAY-123", mode="demo", jira_linked=True,
                jira_comment_id="42", jira_comment_url="https://acme.atlassian.net/browse/PAY-123?focusedCommentId=42")
            executor = MigrationExecutor(connection)
            executor.migrate(new)
            new_model = executor.loader.project_state(new).apps.get_model("investigations", "Investigation")
            restored = new_model.objects.get(pk=record.pk)
            self.assertEqual(restored.issue_source, "jira")
            self.assertEqual(restored.issue_comment_id, "42")
            self.assertTrue(restored.issue_linked)
            self.assertEqual(new_model(issue_key="acme/checkout#1").issue_source, "github")
        finally:
            MigrationExecutor(connection).migrate([("investigations", "0007_automatic_default_branch")])
