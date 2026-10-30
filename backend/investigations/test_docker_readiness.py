"""Docker installation is distinct from a running, usable validation engine."""
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from . import execution
from .integrations import IntegrationError
from .models import Investigation, Repository
from .pipeline import run_investigation
from .tests import LIVE_SETTINGS


class DockerReadinessTests(SimpleTestCase):
    @patch("investigations.execution.shutil.which", return_value="installed")
    @patch("investigations.execution.command", side_effect=IntegrationError("missing Linux engine pipe"))
    def test_installed_cli_with_stopped_daemon_has_actionable_error(self, command, which):
        with self.assertRaisesMessage(IntegrationError, "Start Docker Desktop"):
            execution.ensure_coding_runtime()
        self.assertEqual(command.call_args.args[0], ["docker", "info", "--format", "{{.OSType}}"])
        self.assertEqual(command.call_args.kwargs["timeout"], 15)

    @patch("investigations.execution.command", return_value="windows")
    def test_windows_container_engine_is_rejected(self, command):
        with self.assertRaisesMessage(IntegrationError, "requires Linux containers"):
            execution.ensure_docker_engine()

    @patch("investigations.execution.command", side_effect=["linux", IntegrationError("image missing")])
    def test_missing_test_image_is_reported_without_running_or_pulling_it(self, command):
        with self.assertRaisesMessage(IntegrationError, "not available locally"):
            execution.ensure_validation_runtime(Repository(runtime_image="team/nextjs-tests:ready"))
        self.assertEqual([call.args[0][1] for call in command.call_args_list], ["info", "image"])

    @patch("investigations.execution.command", side_effect=["linux", "linux"])
    def test_linux_engine_and_prebuilt_linux_image_pass(self, command):
        execution.ensure_validation_runtime(Repository(runtime_image="team/nextjs-tests:ready"))
        self.assertEqual(command.call_args.args[0], ["docker", "image", "inspect", "--format", "{{.Os}}", "team/nextjs-tests:ready"])

    @patch("investigations.execution.command", side_effect=["linux", "windows"])
    def test_non_linux_image_is_rejected(self, command):
        with self.assertRaisesMessage(IntegrationError, "must be a Linux image"):
            execution.ensure_validation_runtime(Repository(runtime_image="team/windows-tests:ready"))


@override_settings(**LIVE_SETTINGS)
class DockerPreflightPipelineTests(TestCase):
    @patch("investigations.execution.shutil.which", return_value="installed")
    @patch("investigations.execution.command", side_effect=IntegrationError("daemon stopped"))
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.GitHubClient")
    def test_unavailable_engine_stops_before_model_calls(self, github, model, command, which):
        run = Investigation.objects.create(issue_key="acme/repo#1", source_repository_url="https://github.com/acme/repo", source_issue_number=1, status="running", attempt=1)
        run_investigation(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "needs_attention")
        self.assertIn("Start Docker Desktop", run.error)
        model.assert_not_called()
        github.assert_not_called()
