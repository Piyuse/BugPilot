import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import execution
from .integrations import OpenAIPreprocessor, GitHubClient, IntegrationError, JiraClient, adf_text
from .models import Investigation, Repository, WorkerHeartbeat
from .pipeline import run_investigation, select_repository
from .queue import claim_next, recover_stale
from .repository_urls import normalize_repository_url, parse_ticket

LIVE_SETTINGS = {
    "JIRA_BASE_URL": "https://acme.atlassian.net", "JIRA_EMAIL": "test@example.com",
    "JIRA_API_TOKEN": "jira-test", "OPENAI_API_KEY": "openai-test",
    "OPENAI_PREPROCESS_MODEL": "test-preprocessor", "GITHUB_TOKEN": "github-test", "CODEX_API_KEY": "codex-test",
}



class FrontendAuthenticationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("frontend-dev", password="test-password", is_staff=True)
        self.client = APIClient(enforce_csrf_checks=True)

    def csrf(self):
        self.client.get("/api/auth/session/")
        return self.client.cookies["csrftoken"].value

    def test_session_bootstrap_sets_csrf_without_authentication(self):
        response = self.client.get("/api/auth/session/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authenticated"])
        self.assertEqual(response.json()["username"], "")
        self.assertIn("csrftoken", response.cookies)

    def test_login_requires_csrf_even_for_anonymous_users(self):
        response = self.client.post("/api/auth/login/", {"username": "frontend-dev", "password": "test-password"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_frontend_login_rotates_csrf_and_authenticates_api_requests(self):
        token = self.csrf()
        response = self.client.post("/api/auth/login/", {"username": "frontend-dev", "password": "test-password"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authenticated"])
        self.assertTrue(response.json()["can_manage"])
        self.assertNotEqual(self.client.cookies["csrftoken"].value, token)
        self.assertEqual(self.client.get("/api/overview/").status_code, 200)

    def test_invalid_login_and_malformed_json_do_not_authenticate(self):
        token = self.csrf()
        for payload in ({"username": "frontend-dev", "password": "wrong"}, [], {"username": 7, "password": "x"}):
            response = self.client.post("/api/auth/login/", payload, format="json", HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(self.client.get("/api/auth/session/").json()["authenticated"])

    def test_logout_requires_csrf_and_ends_the_session(self):
        self.client.force_login(self.user)
        token = self.csrf()
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 403)
        response = self.client.post("/api/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authenticated"])
        self.assertEqual(self.client.get("/api/overview/").status_code, 403)

    def test_proxy_preserving_frontend_host_passes_origin_check(self):
        token = self.csrf()
        response = self.client.post("/api/auth/login/", {"username": "frontend-dev", "password": "test-password"}, format="json",
                                    HTTP_X_CSRFTOKEN=token, HTTP_HOST="127.0.0.1:5173", HTTP_ORIGIN="http://127.0.0.1:5173")
        self.assertEqual(response.status_code, 200)


class URLTests(SimpleTestCase):
    def test_github_normalization_preserves_name_when_removing_git_suffix(self):
        self.assertEqual(normalize_repository_url(" https://github.com/Acme/Checkout.git/ "), "https://github.com/acme/checkout")

    def test_untrusted_repository_urls_are_rejected(self):
        for url in ["http://github.com/a/b", "https://github.com.evil.test/a/b", "https://user:pass@github.com/a/b", "https://github.com/a/b?token=x", "https://github.com/a/b/tree/main", "https://github.com/a/..", "file:///etc/passwd", "https://github.com/a/b#x"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_repository_url(url)

    def test_ticket_key_and_trusted_url(self):
        self.assertEqual(parse_ticket("pay-142"), "PAY-142")
        self.assertEqual(parse_ticket("https://acme.atlassian.net/browse/PAY-142?focusedCommentId=1", "https://acme.atlassian.net"), "PAY-142")

    def test_ticket_cannot_redirect_service_requests(self):
        for ticket in ["https://evil.test/browse/PAY-142", "http://acme.atlassian.net/browse/PAY-142", "../PAY-142", "PAY-0", "PAY-142;curl evil.test", "https://acme.atlassian.net/rest/api/3/issue/PAY-142"]:
            with self.subTest(ticket=ticket), self.assertRaises(ValueError):
                parse_ticket(ticket, "https://acme.atlassian.net")

    def test_adf_preserves_paragraphs_and_code(self):
        self.assertEqual(adf_text({"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Checkout fails"}]},
            {"type": "codeBlock", "content": [{"type": "text", "text": "AttributeError"}]},
        ]}), "Checkout fails\nAttributeError\n")


@override_settings(**LIVE_SETTINGS)
class APITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("dev", password="test-password")
        self.admin = get_user_model().objects.create_user("owner", password="test-password", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.repository = Repository.objects.create(name="checkout", url="https://github.com/acme/checkout", validation_commands=[["python", "-m", "pytest"]])

    def test_authentication_required_except_health(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/investigations/").status_code, 403)
        self.assertEqual(self.client.get("/api/health/").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_backend_root_points_to_the_separate_frontend(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["application"], "BugPilot API")
        self.assertIn(":5173", response.json()["frontend"])

    def test_submission_is_durable_and_idempotent(self):
        first = self.client.post("/api/investigations/", {"jira_ticket": "PAY-142"}, format="json")
        second = self.client.post("/api/investigations/", {"jira_ticket": "pay-142"}, format="json")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(Investigation.objects.count(), 1)
        self.assertEqual(Investigation.objects.get().status, "queued")

    def test_optional_repository_can_be_name_id_or_url(self):
        for number, hint in enumerate(["checkout", str(self.repository.pk), "https://github.com/acme/checkout.git"], start=1):
            response = self.client.post("/api/investigations/", {"jira_ticket": f"PAY-{number}", "repository": hint}, format="json")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.data["requested_repository"], "checkout")

    def test_inactive_and_unknown_repositories_rejected(self):
        self.repository.is_active = False
        self.repository.save()
        for hint in ["checkout", "unknown"]:
            self.assertEqual(self.client.post("/api/investigations/", {"jira_ticket": "PAY-142", "repository": hint}, format="json").status_code, 400)

    def test_removed_execution_mode_is_rejected_without_queuing(self):
        response = self.client.post("/api/investigations/", {"jira_ticket": "PAY-142", "mode": "demo"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("mode", response.data)
        self.assertFalse(Investigation.objects.exists())

    def test_historical_runs_are_not_listed_counted_or_retried(self):
        historical = Investigation.objects.create(issue_source="jira", issue_key="PAY-142", mode="demo", status="needs_attention")
        self.assertEqual(self.client.get("/api/investigations/").data["count"], 0)
        self.assertEqual(self.client.get("/api/overview/").data["counts"], {})
        self.assertEqual(self.client.get(f"/api/investigations/{historical.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/investigations/{historical.pk}/retry/", {}, format="json").status_code, 404)
        response = self.client.post("/api/investigations/", {"jira_ticket": "PAY-142"}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["mode"], "live")
        self.assertNotEqual(response.data["id"], str(historical.pk))
        historical.refresh_from_db()
        self.assertEqual(historical.status, "needs_attention")

    def test_repository_api_no_longer_exposes_fixture_flags(self):
        response = self.client.get("/api/repositories/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("is_demo", response.data[0])

    def test_registry_writes_are_staff_only(self):
        payload = {"name": "identity", "url": "https://github.com/acme/identity"}
        self.assertEqual(self.client.post("/api/repositories/", payload, format="json").status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post("/api/repositories/", payload, format="json").status_code, 201)

    def test_registry_validates_commands_and_duplicates(self):
        self.client.force_authenticate(self.admin)
        for commands in ["pytest", [["python"], "pytest"], [[1]], [[]]]:
            response = self.client.patch(f"/api/repositories/{self.repository.pk}/", {"validation_commands": commands}, format="json")
            self.assertEqual(response.status_code, 400)
        duplicate = self.client.post("/api/repositories/", {"name": "other", "url": "https://github.com/Acme/Checkout.git"}, format="json")
        self.assertEqual(duplicate.status_code, 400)

    def test_removed_legacy_report_endpoints_are_not_exposed(self):
        self.assertEqual(self.client.get("/api/bug-reports/").status_code, 404)
        self.assertEqual(self.client.post("/api/bug-reports/", {}, format="json").status_code, 404)
        self.assertEqual(self.client.get("/api/bug-reports/1/").status_code, 404)

    def test_case_ambiguous_repository_names_are_not_guessed(self):
        Repository.objects.create(name="CHECKOUT", url="https://github.com/acme/other-checkout")
        response = self.client.post("/api/investigations/", {"jira_ticket": "PAY-142", "repository": "checkout"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_csrf_is_required_for_cookie_authenticated_mutations(self):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post("/api/investigations/", {"jira_ticket": "PAY-142"}).status_code, 403)

    def test_retry_requires_failure_and_is_idempotent(self):
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142", status="needs_attention", error="Test failed")
        first = self.client.post(f"/api/investigations/{run.pk}/retry/", {}, format="json")
        second = self.client.post(f"/api/investigations/{run.pk}/retry/", {}, format="json")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        run.refresh_from_db()
        self.assertEqual(run.error, "")

    def test_repository_can_be_supplied_to_resolve_ambiguous_discovery(self):
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142", status="needs_attention", stage="selecting_repository",
                                           validation={"workspace_attempt": 1}, proposal={"summary": "Old patch"})
        response = self.client.post(f"/api/investigations/{run.pk}/retry/", {"repository": "checkout"}, format="json")
        self.assertEqual(response.status_code, 202)
        run.refresh_from_db()
        self.assertEqual(run.requested_repository, self.repository)
        self.assertEqual(run.validation, {})
        self.assertEqual(run.proposal, {})

    def test_publication_retries_cannot_switch_repository(self):
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142", status="needs_attention", stage="publishing")
        response = self.client.post(f"/api/investigations/{run.pk}/retry/", {"repository": "checkout"}, format="json")
        self.assertEqual(response.status_code, 409)

    def test_status_filter_and_search(self):
        Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142", status="running", title="Checkout crash")
        Investigation.objects.create(issue_source="jira", mode="live", issue_key="AUTH-1", status="queued", title="Login")
        response = self.client.get("/api/investigations/?status=running&q=checkout")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["issue_key"], "PAY-142")

    def test_overview_reports_worker_state_without_secrets(self):
        WorkerHeartbeat.objects.create(pk=1, updated_at=timezone.now())
        with override_settings(GITHUB_TOKEN="never-return-this-secret"):
            response = self.client.get("/api/overview/")
        self.assertTrue(response.data["worker_online"])
        self.assertNotIn("never-return-this-secret", response.content.decode())


@override_settings(**LIVE_SETTINGS)
class QueueAndPipelineTests(TestCase):
    def setUp(self):
        self.repository = Repository.objects.create(name="checkout-service", url="https://github.com/acme/checkout", validation_commands=[["python", "-m", "pytest"]])

    def test_claim_is_exclusive_and_attempts_are_counted(self):
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142")
        self.assertEqual(claim_next().pk, run.pk)
        self.assertIsNone(claim_next())
        run.refresh_from_db()
        self.assertEqual(run.attempt, 1)
        self.assertEqual(run.status, "running")

    def test_worker_leaves_historical_queued_runs_untouched(self):
        historical = Investigation.objects.create(issue_source="jira", mode="demo", issue_key="PAY-142")
        live = Investigation.objects.create(issue_source="jira", issue_key="PAY-143")
        self.assertEqual(claim_next().pk, live.pk)
        self.assertIsNone(claim_next())
        historical.refresh_from_db()
        self.assertEqual(historical.status, "queued")
        self.assertEqual(historical.attempt, 0)

    def test_expired_worker_moves_job_to_attention(self):
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142", status="running", heartbeat_at=timezone.now() - timedelta(hours=1))
        recover_stale()
        run.refresh_from_db()
        self.assertEqual(run.status, "needs_attention")
        self.assertEqual(run.events.count(), 1)

    @patch("investigations.pipeline.GitHubClient")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.JiraClient")
    def test_historical_execution_mode_cannot_call_providers(self, jira, openai, github):
        run = Investigation.objects.create(issue_source="jira", mode="demo", issue_key="PAY-142", status="running")
        result = run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(result["status"], "needs_attention")
        self.assertIn("historical investigation", run.error)
        self.assertEqual(run.pull_request_url, "")
        self.assertFalse(run.issue_linked)
        jira.assert_not_called()
        openai.assert_not_called()
        github.assert_not_called()

    @patch("investigations.management.commands.runworker.run_investigation", return_value={"status": "running"})
    def test_management_worker_processes_queued_job(self, pipeline):
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142")
        call_command("runworker", once=True, stdout=StringIO())
        run.refresh_from_db()
        pipeline.assert_called_once_with(run.pk)
        self.assertEqual(run.status, "running")
        self.assertTrue(WorkerHeartbeat.objects.filter(pk=1).exists())

    def test_repository_matching_uses_readme_context_and_allowlist(self):
        Repository.objects.create(name="identity", url="https://github.com/acme/identity")
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142", preprocessed={"summary": "Checkout"})
        github, openai = MagicMock(), MagicMock()
        github.context.side_effect = lambda repo: {"id": repo.pk, "readme": "context"}
        openai.select.return_value = {"repository_id": self.repository.pk, "reason": "checkout owns pricing"}
        selected, reason = select_repository(run, openai, github)
        self.assertEqual(selected, self.repository)
        self.assertEqual(github.context.call_count, 2)
        self.assertIn("pricing", reason)

    @override_settings(CODEX_API_KEY="test-key")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.JiraClient")
    @patch("investigations.pipeline.GitHubClient")
    def test_live_success_orders_fix_check_publish_and_link(self, github_class, jira_class, openai_class, executor):
        github_class.return_value.default_branch.return_value = "trunk"
        jira_class.return_value.comment_pull_request.return_value = {"id": "1001", "url": "https://acme.atlassian.net/browse/PAY-142?focusedCommentId=1001"}
        jira_class.return_value.ticket.return_value = {"title": "Checkout fails", "url": "https://acme.atlassian.net/browse/PAY-142"}
        openai_class.return_value.preprocess.return_value = {"summary": "null dereference"}
        executor.investigate_and_fix.return_value = {"summary": "Null check", "root_cause": "Missing guard", "tests_added": ["test"]}
        executor.validate_proposal.return_value = {"passed": True, "checks": []}
        executor.publish_pull_request.return_value = {"html_url": "https://github.com/acme/checkout/pull/1"}
        run = Investigation.objects.create(issue_source="jira", issue_key="PAY-142", mode="live", requested_repository=self.repository)
        claim_next()
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "pr_created")
        self.assertTrue(run.issue_linked)
        jira_class.return_value.comment_pull_request.assert_called_once()
        self.assertEqual(run.issue_comment_id, "1001")
        self.assertEqual(run.model_calls["preprocessing"]["provider"], "openai")
        self.assertEqual(run.model_calls["coding"]["provider"], "codex")
        self.assertEqual([event.stage for event in run.events.all()][-3:], ["publishing", "linking_issue", "completed"])

    @override_settings(CODEX_API_KEY="test-key")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.JiraClient")
    @patch("investigations.pipeline.GitHubClient")
    def test_failed_checks_never_publish(self, github, jira, openai, executor):
        github.return_value.default_branch.return_value = "trunk"
        jira.return_value.ticket.return_value = {"title": "Crash", "url": "https://acme.atlassian.net/browse/PAY-142"}
        openai.return_value.preprocess.return_value = {}
        executor.investigate_and_fix.return_value = {}
        executor.validate_proposal.side_effect = IntegrationError("Regression test failed")
        executor.redact.side_effect = lambda value: value
        run = Investigation.objects.create(issue_source="jira", issue_key="PAY-142", mode="live", requested_repository=self.repository)
        claim_next()
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "needs_attention")
        self.assertFalse(run.validation["passed"])
        executor.publish_pull_request.assert_not_called()
        jira.return_value.comment_pull_request.assert_not_called()

    @override_settings(CODEX_API_KEY="test-key")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.JiraClient")
    @patch("investigations.pipeline.GitHubClient")
    def test_retry_after_jira_failure_only_relinks_existing_pr(self, github, jira, openai, executor):
        run = Investigation.objects.create(issue_source="jira", issue_key="PAY-142", mode="live", title="Fix", repository=self.repository,
                                           pull_request_url="https://github.com/acme/checkout/pull/1")
        claim_next()
        jira.return_value.comment_pull_request.side_effect = IntegrationError("Jira unavailable")
        executor.redact.side_effect = lambda value: value
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "needs_attention")
        self.assertTrue(run.pull_request_url)
        Investigation.objects.filter(pk=run.pk).update(status="queued")
        claim_next()
        jira.return_value.comment_pull_request.side_effect = None
        jira.return_value.comment_pull_request.return_value = {"id": "1001", "url": "https://acme.atlassian.net/browse/PAY-142?focusedCommentId=1001"}
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "pr_created")
        executor.publish_pull_request.assert_not_called()
        executor.investigate_and_fix.assert_not_called()
        jira.return_value.ticket.assert_not_called()

    @override_settings(CODEX_API_KEY="test-key")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.JiraClient")
    @patch("investigations.pipeline.GitHubClient")
    def test_worker_recovers_pr_created_before_database_checkpoint(self, github, jira, executor):
        github.return_value.existing_pull_request.return_value = {"html_url": "https://github.com/acme/checkout/pull/2"}
        jira.return_value.comment_pull_request.return_value = {"id": "1002", "url": "https://acme.atlassian.net/browse/PAY-142?focusedCommentId=1002"}
        run = Investigation.objects.create(issue_source="jira", issue_key="PAY-142", mode="live", repository=self.repository, branch="codex/pay-142-existing")
        claim_next()
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "pr_created")
        self.assertEqual(run.pull_request_url, "https://github.com/acme/checkout/pull/2")
        executor.investigate_and_fix.assert_not_called()

    def test_inactive_explicit_repository_stops_investigation(self):
        self.repository.is_active = False
        self.repository.save()
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142", requested_repository=self.repository)
        claim_next()
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "needs_attention")

    @patch("investigations.pipeline.run_live")
    def test_worker_that_loses_its_lease_cannot_fail_a_new_attempt(self, live):
        run = Investigation.objects.create(issue_source="jira", mode="live", issue_key="PAY-142")
        claim_next()
        def lose_lease(claimed):
            Investigation.objects.filter(pk=claimed.pk).update(attempt=2)
            raise IntegrationError("This worker no longer owns the investigation.")
        live.side_effect = lose_lease
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "running")
        self.assertEqual(run.attempt, 2)
        self.assertEqual(run.error, "")


class ProviderTests(SimpleTestCase):
    @override_settings(OPENAI_API_KEY="test", OPENAI_PREPROCESS_MODEL="openai-test")
    @patch("investigations.integrations.request_json")
    def test_openai_uses_json_mode_and_normalizes_ticket(self, request):
        expected = {"summary": "Crash", "expected_behavior": "Works", "actual_behavior": "Fails", "error_details": "", "reproduction_steps": [], "keywords": ["checkout"]}
        request.return_value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(expected)}]}]}
        self.assertEqual(OpenAIPreprocessor().preprocess({"title": "Crash"}), expected)
        self.assertEqual(request.call_args.kwargs["payload"]["text"]["format"]["type"], "json_schema")
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bearer test"})

    @patch.object(OpenAIPreprocessor, "generate", return_value={"summary": "missing required fields"})
    def test_malformed_model_result_is_rejected(self, generate):
        with self.assertRaises(IntegrationError):
            OpenAIPreprocessor().preprocess({})

    @patch.object(OpenAIPreprocessor, "generate")
    def test_low_confidence_or_non_allowlisted_repository_is_rejected(self, generate):
        for result in [
            {"repository_id": 1, "confidence": 0.5, "reason": "unsure"},
            {"repository_id": 99, "confidence": 0.99, "reason": "hallucinated"},
            {"repository_id": True, "confidence": 0.99, "reason": "invalid type"},
        ]:
            generate.return_value = result
            with self.assertRaises(IntegrationError):
                OpenAIPreprocessor().select({}, [{"id": 1}])

    @override_settings(JIRA_BASE_URL="https://acme.atlassian.net", JIRA_EMAIL="test@example.com", JIRA_API_TOKEN="test")
    @patch("investigations.integrations.request_json")
    def test_jira_remote_link_has_stable_global_id(self, request):
        url = "https://github.com/acme/checkout/pull/1"
        JiraClient().link_pull_request("PAY-142", url, "Fix")
        self.assertEqual(request.call_args.kwargs["payload"]["globalId"], "bugpilot:" + url)
        self.assertTrue(request.call_args.args[1].endswith("/PAY-142/remotelink"))

    @override_settings(GITHUB_TOKEN="test")
    @patch.object(GitHubClient, "call", return_value=[{"html_url": "https://github.com/acme/checkout/pull/1"}])
    def test_github_pr_creation_is_idempotent(self, call):
        repo = Repository(name="checkout", url="https://github.com/acme/checkout", default_branch="main")
        result = GitHubClient().create_pull_request(repo, "codex/pay-142", "Fix", "body")
        self.assertIn("/pull/1", result["html_url"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], "GET")

    @override_settings(GITHUB_TOKEN="github-secret", JIRA_API_TOKEN="jira-secret", OPENAI_API_KEY="openai-secret", CODEX_API_KEY="codex-secret")
    def test_execution_environment_and_logs_strip_service_secrets(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "github-secret", "DATABASE_URL": "db-secret", "JIRA_API_TOKEN": "jira-secret"}):
            env = execution.clean_environment()
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn("JIRA_API_TOKEN", env)
        self.assertEqual(execution.redact("github-secret jira-secret openai-secret codex-secret"), "[redacted] [redacted] [redacted] [redacted]")

    @patch("investigations.integrations.build_opener")
    def test_http_errors_do_not_include_sensitive_provider_bodies(self, opener):
        from urllib.error import HTTPError
        opener.return_value.open.side_effect = HTTPError("https://api.github.com/test", 403, "token=secret", {}, None)
        from .integrations import request_json
        with self.assertRaises(IntegrationError) as error:
            request_json("GET", "https://api.github.com/test")
        self.assertNotIn("secret", str(error.exception))


class RealGitValidationTests(SimpleTestCase):
    """Exercise real git diffs; Docker execution is mocked at the process boundary."""
    def setUp(self):
        temp_root = settings.BASE_DIR / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        assert Path(self.temp.name).resolve().is_relative_to(temp_root.resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.override = override_settings(BUGPILOT_WORKSPACE_ROOT=self.root / "workspaces")
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        execution.git(["init", "-b", "main"], cwd=self.workspace)
        (self.workspace / "pricing.py").write_text("total = discount.amount\n", encoding="utf-8")
        execution.git(["add", "."], cwd=self.workspace)
        execution.git(["commit", "-m", "initial"], cwd=self.workspace)
        self.base = execution.git(["rev-parse", "HEAD"], cwd=self.workspace)
        execution.git(["checkout", "-b", "codex/pay-142"], cwd=self.workspace)
        import uuid
        self.run = Investigation(issue_source="jira", id=uuid.uuid4(), branch="codex/pay-142", base_sha=self.base, attempt=1)
        self.run.repository = Repository(name="checkout", url="https://github.com/acme/checkout", validation_commands=[["python", "-m", "pytest"]])

    def test_unchanged_code_cannot_be_published(self):
        with self.assertRaisesMessage(IntegrationError, "no code changes"):
            execution.validate_proposal(self.run, self.workspace)

    def test_workflow_and_secret_changes_are_blocked_before_docker(self):
        (self.workspace / ".env").write_text("SECRET=example\n")
        with self.assertRaisesMessage(IntegrationError, "protected"):
            execution.validate_proposal(self.run, self.workspace)

    def test_real_patch_checks_run_in_networkless_container(self):
        (self.workspace / "pricing.py").write_text("total = discount.amount if discount else 0\n")
        (self.workspace / "test_pricing.py").write_text("assert True\n")
        original_command = execution.command
        seen = []
        def intercept(argv, **kwargs):
            if argv[0] == "docker":
                if argv[1] in {"info", "image"}:
                    return "linux"
                if argv[1] == "inspect":
                    return '{"Running": false, "ExitCode": 0, "OOMKilled": false}'
                seen.append(argv)
                return "2 passed"
            return original_command(argv, **kwargs)
        with patch("investigations.execution.command", side_effect=intercept), patch("investigations.execution.subprocess.run"):
            result = execution.validate_proposal(self.run, self.workspace)
        self.assertTrue(result["passed"])
        self.assertEqual(result["changed_files"], ["pricing.py", "test_pricing.py"])
        import tarfile
        with tarfile.open(self.workspace.parent / "validation-attempt-1" / "source.tar") as snapshot:
            self.assertEqual(set(snapshot.getnames()), {"pricing.py", "test_pricing.py"})
            self.assertIn(b"if discount else 0", snapshot.extractfile("pricing.py").read())
        self.assertIn("none", seen[0])
        self.assertIn("--read-only", seen[0])
        self.assertIn("no-new-privileges", seen[0])
        self.assertIn("type=volume,target=/work", seen[0])
        self.assertNotIn("/work:rw,exec,size=1g", seen[0])
        self.assertEqual(seen[0][-3:], ["python", "-m", "pytest"])
        self.assertIn("tar -xf /source.tar -C /work", seen[0][-5])

    def test_test_failure_stops_validation(self):
        (self.workspace / "pricing.py").write_text("total = 0\n")
        original_command = execution.command
        def intercept(argv, **kwargs):
            if argv[0] == "docker":
                if argv[1] in {"info", "image"}:
                    return "linux"
                raise IntegrationError("pytest failed")
            return original_command(argv, **kwargs)
        with patch("investigations.execution.command", side_effect=intercept), patch("investigations.execution.subprocess.run"):
            with self.assertRaisesMessage(IntegrationError, "pytest failed"):
                execution.validate_proposal(self.run, self.workspace)

    def test_already_committed_fix_can_recover_after_push_before_pr_creation(self):
        self.run.issue_key = "PAY-142"
        self.run.title = "Fix checkout"
        self.run.issue_url = "https://acme.atlassian.net/browse/PAY-142"
        self.run.proposal = {"summary": "Guard discount", "root_cause": "Missing null guard"}
        self.run.validation = {"passed": True, "checks": [{"command": ["python", "-m", "pytest"]}]}
        (self.workspace / "pricing.py").write_text("total = 0\n", encoding="utf-8")
        execution.git(["add", "."], cwd=self.workspace)
        execution.git(["commit", "-m", "existing fix"], cwd=self.workspace)
        self.run.validation["validated_tree"] = execution.git(["write-tree"], cwd=self.workspace)
        self.run.save = MagicMock()
        original_git = execution.git
        operations = []
        def intercept(args, **kwargs):
            operations.append(args[0])
            return "" if args[0] == "push" else original_git(args, **kwargs)
        github = MagicMock()
        github.publication_target.return_value = {"url": self.run.repository.url, "head_owner": "acme"}
        with patch("investigations.execution.git", side_effect=intercept):
            execution.publish_pull_request(self.run, self.workspace, github)
        self.assertNotIn("commit", operations)
        self.assertIn("push", operations)
        github.create_pull_request.assert_called_once()
