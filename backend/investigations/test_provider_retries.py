"""Exercise real HTTP retry handling without provider calls or actual sleeps."""
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from io import BytesIO
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase, TestCase, override_settings

from .integrations import OpenAIPreprocessor, IntegrationError, request_json
from .task_context import PREPROCESS_SCHEMA
from .models import Investigation, Repository
from .pipeline import run_investigation
from .tests import LIVE_SETTINGS


@override_settings(OPENAI_API_KEY="test-key", OPENAI_PREPROCESS_MODEL="test-preprocessor")
class ProviderRetryTests(SimpleTestCase):
    url = "https://api.openai.com/v1/responses"

    def setUp(self):
        self.opener = self.enterContext(patch("investigations.integrations.build_opener")).return_value
        self.sleep = self.enterContext(patch("investigations.integrations.time.sleep"))
        self.enterContext(patch("investigations.integrations.random.uniform", return_value=0))

    def error(self, code, headers=None):
        return HTTPError(self.url, code, "secret-provider-message", headers or {}, BytesIO(b"secret-body"))

    def success(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"summary":"fixed"}'}]}]}).encode()
        return response

    def generate(self, on_retry=None):
        return OpenAIPreprocessor().generate("Normalize issue", {}, schema=PREPROCESS_SCHEMA, on_retry=on_retry)

    def test_openai_recovers_after_several_503s_with_progress(self):
        self.opener.open.side_effect = [self.error(503) for _ in range(3)] + [self.success()]
        progress = Mock()
        self.assertEqual(self.generate(progress), {"summary": "fixed"})
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(8)])
        self.assertEqual(progress.call_count, 3)
        self.assertIn("4/6", progress.call_args.args[0])

    def test_persistent_503_stops_after_six_attempts_with_service_message(self):
        self.opener.open.side_effect = [self.error(503) for _ in range(6)]
        with self.assertRaises(IntegrationError) as caught:
            self.generate()
        self.assertEqual(self.opener.open.call_count, 6)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(8), call(16), call(32)])
        message = str(caught.exception)
        self.assertIn("temporarily unavailable", message)
        self.assertIn("6 attempt(s)", message)
        self.assertNotIn("secret", message)

    def test_retry_after_seconds_is_honored(self):
        self.opener.open.side_effect = [self.error(503, {"Retry-After": "15"}), self.success()]
        self.generate()
        self.sleep.assert_called_once_with(15)

    @patch("investigations.integrations.time.time", return_value=1000)
    def test_retry_after_http_date_is_honored(self, now):
        date = format_datetime(datetime.fromtimestamp(1020, timezone.utc), usegmt=True)
        self.opener.open.side_effect = [self.error(503, {"Retry-After": date}), self.success()]
        self.generate()
        self.sleep.assert_called_once_with(20)

    def test_long_retry_after_stops_instead_of_retrying_too_early(self):
        self.opener.open.side_effect = self.error(503, {"Retry-After": "120"})
        with self.assertRaises(IntegrationError):
            self.generate()
        self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_invalid_retry_after_uses_backoff(self):
        self.opener.open.side_effect = [self.error(503, {"Retry-After": "invalid"}), self.success()]
        self.generate()
        self.sleep.assert_called_once_with(2)

    def test_auth_and_request_errors_are_not_retried(self):
        for code, explanation in [(400, "request format"), (401, "permissions"), (403, "permissions"), (404, "configured model")]:
            with self.subTest(code=code):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = self.error(code)
                with self.assertRaises(IntegrationError) as caught:
                    self.generate()
                self.assertIn(explanation, str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_transient_statuses_and_network_failure_recover(self):
        for failure in [self.error(code) for code in (429, 500, 502, 504)] + [URLError("secret")]:
            with self.subTest(failure=type(failure).__name__):
                self.opener.open.side_effect = [failure, self.success()]
                self.assertEqual(self.generate(), {"summary": "fixed"})

    def test_publication_post_is_not_automatically_replayed(self):
        self.opener.open.side_effect = self.error(503)
        with self.assertRaises(IntegrationError):
            request_json("POST", "https://api.github.com/repos/acme/repo/pulls", payload={})
        self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_quota_failure_has_distinct_diagnostic(self):
        self.opener.open.side_effect = self.error(429)
        with self.assertRaises(IntegrationError) as caught:
            request_json("GET", self.url)
        self.assertIn("quota", str(caught.exception))

    def test_payment_required_explains_openai_billing_without_retrying(self):
        self.opener.open.side_effect = self.error(402)
        with self.assertRaises(IntegrationError) as caught:
            self.generate()
        self.assertIn("payment is required", str(caught.exception))
        self.assertIn("credits", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_openai_billing_codes_stop_without_retries_or_raw_error_messages(self):
        for code in ("insufficient_quota", "credit_balance_exhausted", "project_spend_limit_exceeded", "organization_usage_limit_exceeded"):
            with self.subTest(code=code):
                self.opener.open.reset_mock()
                body = json.dumps({"error": {"code": code, "message": "secret", "type": "insufficient_quota"}}).encode()
                self.opener.open.side_effect = HTTPError(self.url, 429, "secret", {}, BytesIO(body))
                with self.assertRaises(IntegrationError) as caught:
                    self.generate()
                self.assertIn("OpenAI API billing", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_openai_rate_limit_still_retries(self):
        body = json.dumps({"error": {"code": "slow_down", "type": "rate_limit_error"}}).encode()
        self.opener.open.side_effect = [HTTPError(self.url, 429, "rate limit", {}, BytesIO(body)), self.success()]
        self.assertEqual(self.generate(), {"summary": "fixed"})
        self.sleep.assert_called_once_with(2)


@override_settings(**LIVE_SETTINGS)
class PreprocessingOutageTests(TestCase):
    @patch("investigations.integrations.time.sleep")
    @patch("investigations.integrations.build_opener")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.GitHubClient")
    def test_outage_records_progress_and_never_reaches_coding_or_publication(self, github, execution, opener, sleep):
        repository = Repository.objects.create(name="checkout", url="https://github.com/acme/checkout")
        run = Investigation.objects.create(
            issue_key="acme/checkout#123", source_repository_url=repository.url, source_issue_number=123,
            mode="live", status="running", attempt=1, requested_repository=repository,
        )
        github.return_value.issue.return_value = {"title": "Checkout fails", "url": repository.url + "/issues/123"}
        opener.return_value.open.side_effect = [HTTPError("https://api.openai.com", 503, "secret", {}, None) for _ in range(6)]
        execution.redact.side_effect = lambda value: value
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "needs_attention")
        self.assertEqual(run.stage, "preprocessing")
        self.assertIn("temporarily unavailable", run.error)
        messages = list(run.events.filter(stage="preprocessing").values_list("message", flat=True))
        self.assertEqual(sum("Retrying request" in message for message in messages), 5)
        self.assertTrue(any("6/6" in message for message in messages))
        self.assertNotIn("secret", run.error)
        execution.investigate_and_fix.assert_not_called()
        execution.publish_pull_request.assert_not_called()
        github.return_value.comment_issue_pull_request.assert_not_called()
