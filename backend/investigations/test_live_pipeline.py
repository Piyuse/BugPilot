"""Provider contract tests: real client code, mocked network/agent boundaries."""
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from .execution import investigate_and_fix
from .integrations import OpenAIPreprocessor, IntegrationError, JiraClient, adf_text
from .models import Investigation, Repository
from .pipeline import run_investigation
from .task_context import PREPROCESS_SCHEMA, coding_task
from .tests import LIVE_SETTINGS


PROCESSED = {
    "summary": "Checkout dereferences an expired discount.",
    "expected_behavior": "Use the normal total when the discount expires.",
    "actual_behavior": "HTTP 500 during payment.",
    "reproduction_steps": ["Expire the cart discount.", "Complete payment."],
    "error_details": "AttributeError: NoneType has no attribute amount",
    "keywords": ["checkout", "discount", "amount"],
}


@override_settings(**LIVE_SETTINGS)
class LiveProviderContractTests(SimpleTestCase):
    @patch("investigations.integrations.request_json")
    def test_jira_fetches_only_needed_fields(self, transport):
        transport.return_value = {"fields": {
            "summary": "Checkout fails",
            "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Reproduction and traceback"}]}]},
            "labels": ["checkout"], "components": [{"name": "billing"}],
            "reporter": {"emailAddress": "private@example.test"}, "attachment": [{"filename": "secret.txt"}],
        }}
        ticket = JiraClient().ticket("PAY-142")
        self.assertTrue(transport.call_args.args[1].endswith("?fields=summary,description,labels,components"))
        self.assertEqual(set(ticket), {"title", "description", "labels", "components", "url"})
        self.assertNotIn("private@example.test", json.dumps(ticket))
        self.assertNotIn("secret.txt", json.dumps(ticket))

    @patch("investigations.integrations.request_json")
    def test_openai_receives_whitelisted_input_and_an_enforced_output_schema(self, transport):
        transport.return_value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({**PROCESSED, "unexpected": "do not forward"})}]}]}
        result = OpenAIPreprocessor().preprocess({
            "title": "Checkout fails", "description": "prefix " + "x" * 25000 + " final traceback",
            "labels": ["checkout"], "components": ["billing"],
            "reporter": "private@example.test", "comments": ["unrelated private text"],
        })
        args = transport.call_args
        self.assertEqual(args.args[0], "POST")
        self.assertEqual(args.args[1], "https://api.openai.com/v1/responses")
        sent = args.kwargs["payload"]
        self.assertEqual(sent["model"], "test-preprocessor")
        self.assertEqual(args.kwargs["headers"], {"Authorization": "Bearer openai-test"})
        self.assertFalse(sent["store"])
        self.assertTrue(sent["text"]["format"]["strict"])
        data = json.loads(sent["input"][0]["content"])
        self.assertEqual(set(data), {"title", "description", "labels", "components"})
        self.assertLessEqual(len(data["description"]), 20000)
        self.assertTrue(data["description"].endswith("final traceback"))
        self.assertNotIn("private@example.test", json.dumps(sent))
        self.assertEqual(sent["text"]["format"]["schema"], PREPROCESS_SCHEMA)
        self.assertEqual(result, PROCESSED)

    @patch.object(OpenAIPreprocessor, "generate")
    def test_oversized_or_empty_preprocessed_context_is_not_forwarded(self, generate):
        for summary in ("", "x" * 6001):
            generate.return_value = {**PROCESSED, "summary": summary}
            with self.assertRaises(IntegrationError):
                OpenAIPreprocessor().preprocess({})

    @patch("investigations.integrations.request_json")
    def test_refused_incomplete_and_malformed_responses_are_not_forwarded(self, transport):
        responses = [
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "private reason"}]}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "not JSON"}]}]},
            {"status": "completed", "output": None},
        ]
        for response in responses:
            with self.subTest(response_status=response["status"]):
                transport.return_value = response
                with self.assertRaises(IntegrationError) as caught:
                    OpenAIPreprocessor().preprocess({"title": "Crash"})
                self.assertNotIn("private reason", str(caught.exception))

    @patch("investigations.integrations.request_json")
    def test_reasoning_items_are_ignored_when_parsing_structured_output(self, transport):
        transport.return_value = {"status": "completed", "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(PROCESSED)}]},
        ]}
        self.assertEqual(OpenAIPreprocessor().preprocess({"title": "Crash"}), PROCESSED)

    @patch("investigations.integrations.request_json")
    def test_jira_comment_contains_clickable_pr_branch_and_validation(self, transport):
        transport.side_effect = [{"comments": [], "total": 0}, {"id": "1001"}]
        result = JiraClient().comment_pull_request(
            "PAY-142", "https://github.com/acme/checkout/pull/1", "PAY-142: Fix checkout",
            investigation_id="run-123", branch="codex/pay-142-run123", summary="Add a null guard.",
            validation={"passed": True, "checks": [{"command": ["python", "-m", "pytest"]}]},
        )
        post = transport.call_args_list[-1]
        self.assertEqual(post.args[0], "POST")
        self.assertTrue(post.args[1].endswith("/PAY-142/comment"))
        payload = post.kwargs["payload"]
        self.assertEqual(payload["body"]["type"], "doc")
        self.assertEqual(payload["body"]["version"], 1)
        text = adf_text(payload["body"])
        self.assertIn("https://github.com/acme/checkout/pull/1", text)
        self.assertIn("codex/pay-142-run123", text)
        self.assertIn("1 configured check(s) passed", text)
        self.assertEqual(payload["properties"][0]["value"]["id"], "run-123")
        self.assertEqual(result["url"], "https://acme.atlassian.net/browse/PAY-142?focusedCommentId=1001")

    @patch("investigations.integrations.request_json")
    def test_comment_recovery_scans_later_pages_without_posting_twice(self, transport):
        transport.side_effect = [
            {"comments": [{"id": "11", "body": "unrelated"}], "total": 2},
            {"comments": [{"id": "12", "body": "stored", "properties": [{
                "key": "bugpilot.investigation",
                "value": {"id": "run-123", "pull_request_url": "https://github.com/acme/checkout/pull/1"},
            }]}], "total": 2},
        ]
        result = JiraClient().comment_pull_request(
            "PAY-142", "https://github.com/acme/checkout/pull/1", "Fix",
            investigation_id="run-123", branch="codex/pay-142", summary="Fix", validation={},
        )
        self.assertEqual(result["id"], "12")
        self.assertTrue(all(call.args[0] == "GET" for call in transport.call_args_list))
        self.assertIn("startAt=1", transport.call_args_list[1].args[1])

    @patch("investigations.integrations.request_json")
    def test_lost_comment_response_is_recovered_by_marker_and_pr_url(self, transport):
        transport.side_effect = [
            {"comments": [], "total": 0}, IntegrationError("Response lost"),
            {"comments": [{"id": "17", "body": "BugPilot investigation: run-123\nhttps://github.com/acme/checkout/pull/1"}], "total": 1},
        ]
        client = JiraClient()
        kwargs = dict(investigation_id="run-123", branch="codex/pay-142", summary="Fix", validation={})
        with self.assertRaises(IntegrationError):
            client.comment_pull_request("PAY-142", "https://github.com/acme/checkout/pull/1", "Fix", **kwargs)
        result = client.comment_pull_request("PAY-142", "https://github.com/acme/checkout/pull/1", "Fix", **kwargs)
        self.assertEqual(result["id"], "17")
        self.assertEqual(sum(call.args[0] == "POST" for call in transport.call_args_list), 1)

    @patch("investigations.integrations.request_json", return_value={"comments": [], "total": 10})
    def test_invalid_pagination_never_posts_a_comment(self, transport):
        with self.assertRaises(IntegrationError):
            JiraClient().comment_pull_request("PAY-142", "https://github.com/acme/checkout/pull/1", "Fix",
                investigation_id="run-123", branch="codex/pay-142", summary="", validation={})
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0], "GET")


@override_settings(**LIVE_SETTINGS, CODEX_MODEL="test-coding-model")
class CodingModelHandoffTests(SimpleTestCase):
    def setUp(self):
        temp_root = settings.BASE_DIR / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        assert Path(self.temp.name).resolve().is_relative_to(temp_root.resolve())
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir()
        self.run = Investigation(issue_source="jira", id=uuid.uuid4(), issue_key="PAY-142", mode="live",
            issue_url="https://acme.atlassian.net/browse/PAY-142", branch="codex/pay-142-run123",
            ticket={"description": "RAW_JIRA_TICKET_NEVER_HANDOFF", "reporter": "private@example.test"},
            preprocessed=PROCESSED)
        self.run.repository = Repository(name="checkout", url="https://github.com/acme/checkout",
            validation_commands=[["python", "-m", "pytest"]])

    def test_higher_model_task_contains_processed_text_and_repository_not_raw_ticket(self):
        task = coding_task(self.run)
        self.assertEqual(task["repository"]["url"], "https://github.com/acme/checkout")
        self.assertEqual(task["repository"]["fix_branch"], self.run.branch)
        self.assertIn(PROCESSED["error_details"], task["processed_bug"])
        self.assertNotIn("RAW_JIRA_TICKET_NEVER_HANDOFF", json.dumps(task))
        self.assertNotIn("private@example.test", json.dumps(task))
        self.assertNotIn("ticket", task)

    @patch("investigations.execution.command")
    def test_codex_is_invoked_with_processed_prompt_key_model_and_output_schema(self, command):
        def complete(argv, **kwargs):
            if "--output-last-message" not in argv:
                return "BUGPILOT_SANDBOX_READY"
            result_file = Path(argv[argv.index("--output-last-message") + 1])
            result_file.write_text(json.dumps({"summary": "Guard discount", "root_cause": "Missing null guard", "tests_added": ["Expired discounts"]}), encoding="utf-8")
            return ""
        command.side_effect = complete
        result = investigate_and_fix(self.run, self.workspace)
        args = command.call_args
        argv, options = args.args[0], args.kwargs
        self.assertIn("exec", argv)
        self.assertIn("workspace-write", argv)
        self.assertIn("test-coding-model", argv)
        self.assertIn("--output-schema", argv)
        self.assertIn("shell_environment_policy.ignore_default_excludes=false", argv)
        self.assertEqual(options["cwd"], self.workspace)
        self.assertEqual(options["env"]["CODEX_API_KEY"], "codex-test")
        self.assertNotIn("GITHUB_TOKEN", options["env"])
        self.assertNotIn("JIRA_API_TOKEN", options["env"])
        self.assertIn(PROCESSED["summary"], options["stdin"])
        self.assertIn("https://github.com/acme/checkout", options["stdin"])
        self.assertNotIn("RAW_JIRA_TICKET_NEVER_HANDOFF", options["stdin"])
        self.assertEqual(result["root_cause"], "Missing null guard")


@override_settings(JIRA_API_TOKEN="", OPENAI_API_KEY="", GITHUB_TOKEN="", CODEX_API_KEY="")
class LiveModeIntakeTests(TestCase):
    def test_new_internal_records_default_to_live_execution(self):
        self.assertEqual(Investigation(issue_source="jira", issue_key="PAY-143").mode, "live")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user("developer"))
        self.repository = Repository.objects.create(name="checkout", url="https://github.com/acme/checkout")

    def test_default_submission_reports_missing_credentials(self):
        response = self.client.post("/api/investigations/", {"jira_ticket": "PAY-142"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("OPENAI_API_KEY", str(response.data))
        self.assertFalse(Investigation.objects.exists())

    def test_removed_execution_mode_cannot_bypass_credentials(self):
        response = self.client.post("/api/investigations/", {"jira_ticket": "PAY-142", "mode": "demo"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("mode", response.data)
        self.assertFalse(Investigation.objects.exists())

    @override_settings(**LIVE_SETTINGS)
    def test_configured_live_submission_queues_real_pipeline(self):
        response = self.client.post("/api/investigations/", {"jira_ticket": "PAY-142", "mode": "live"}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["mode"], "live")

    @override_settings(**LIVE_SETTINGS)
    def test_registered_repository_queues_without_fixture_properties(self):
        response = self.client.post("/api/investigations/", {"jira_ticket": "PAY-142", "repository": str(self.repository.pk)}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(Investigation.objects.get().requested_repository, self.repository)

    def test_overview_explains_model_configuration_without_secrets(self):
        response = self.client.get("/api/overview/")
        self.assertFalse(response.data["pipeline"]["ready"])
        self.assertEqual(response.data["pipeline"]["preprocessing"]["provider"], "openai")
        self.assertEqual(response.data["pipeline"]["coding"]["provider"], "codex")
        self.assertIn("CODEX_API_KEY", response.data["pipeline"]["missing"])


@override_settings(**LIVE_SETTINGS)
class JiraCompletionRetryTests(TestCase):
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.JiraClient")
    @patch("investigations.pipeline.GitHubClient")
    def test_explicit_failed_validation_result_blocks_publication(self, github, jira, openai, execution):
        github.return_value.default_branch.return_value = "trunk"
        repository = Repository.objects.create(name="checkout", url="https://github.com/acme/checkout", validation_commands=[["python", "-m", "pytest"]])
        run = Investigation.objects.create(issue_source="jira", issue_key="PAY-142", mode="live", status="running", attempt=1, requested_repository=repository)
        jira.return_value.ticket.return_value = {"title": "Crash", "url": "https://acme.atlassian.net/browse/PAY-142"}
        openai.return_value.preprocess.return_value = PROCESSED
        execution.investigate_and_fix.return_value = {"summary": "Fix", "root_cause": "Cause", "tests_added": []}
        execution.validate_proposal.return_value = {"passed": False, "reason": "A regression check failed"}
        execution.redact.side_effect = lambda text: text
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "needs_attention")
        execution.publish_pull_request.assert_not_called()
        jira.return_value.comment_pull_request.assert_not_called()

    @override_settings(CODEX_API_KEY="", OPENAI_API_KEY="")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.execution.investigate_and_fix")
    @patch("investigations.pipeline.JiraClient")
    def test_posting_a_recovered_pr_comment_does_not_need_model_keys_or_repeat_the_agent(self, jira, fix, openai):
        jira.return_value.comment_pull_request.return_value = {"id": "1001", "url": "https://acme.atlassian.net/browse/PAY-142?focusedCommentId=1001"}
        run = Investigation.objects.create(issue_source="jira", issue_key="PAY-142", mode="live", status="running", attempt=1,
            pull_request_url="https://github.com/acme/checkout/pull/1", branch="codex/pay-142")
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "pr_created")
        self.assertEqual(run.issue_comment_id, "1001")
        self.assertTrue(run.issue_linked)
        fix.assert_not_called()
        openai.assert_not_called()

    @patch("investigations.pipeline.JiraClient")
    def test_saved_comment_checkpoint_prevents_another_comment_call(self, jira):
        run = Investigation.objects.create(issue_source="jira", issue_key="PAY-142", mode="live", status="running", attempt=1,
            pull_request_url="https://github.com/acme/checkout/pull/1", issue_comment_id="1001",
            issue_comment_url="https://acme.atlassian.net/browse/PAY-142?focusedCommentId=1001")
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "pr_created")
        jira.return_value.comment_pull_request.assert_not_called()
