"""Timeout diagnostics, Docker exit status, and safe validation recovery."""
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import execution
from .integrations import IntegrationError
from .models import Investigation, Repository


@override_settings(BUGPILOT_CONTAINER_SETUP_TIMEOUT=900, BUGPILOT_TEST_TIMEOUT=300, BUGPILOT_TEST_MEMORY="2g", GITHUB_TOKEN="secret-token")
class ValidationExecutionTests(SimpleTestCase):
    def setUp(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "attempt-1"
        self.workspace.mkdir()
        self.enterContext(override_settings(BUGPILOT_WORKSPACE_ROOT=Path(self.temp.name)))
        self.run = Investigation(id=uuid.uuid4(), attempt=1)
        self.run.repository = Repository(name="project", runtime_image="prepared-test-image")

    @patch("investigations.execution.subprocess.run")
    @patch("investigations.execution.command")
    def test_setup_has_separate_budget_and_container_exit_is_checked(self, command, cleanup):
        command.side_effect = ["container-id", "2 passed", json.dumps({"Running": False, "ExitCode": 0, "OOMKilled": False})]
        result = execution.run_validation_check(self.run, self.workspace, ["pnpm", "test"], 0)
        self.assertTrue(result["passed"])
        create, start, inspect = command.call_args_list
        self.assertEqual(create.kwargs["timeout"], 900)
        self.assertEqual(start.kwargs["timeout"], 300)
        self.assertEqual(start.args[0][:3], ["docker", "start", "--attach"])
        for flag in ("--read-only", "none", "no-new-privileges", "2g", "type=volume,target=/work"):
            self.assertIn(flag, create.args[0])
        self.assertEqual(inspect.args[0][1], "inspect")
        self.assertEqual(cleanup.call_args.args[0][:5], ["docker", "rm", "-f", "-v", f"bugpilot-{self.run.pk.hex[:12]}-1-0"])

    @patch("investigations.execution.subprocess.run")
    @patch("investigations.execution.command")
    def test_attach_success_does_not_hide_failed_test_exit(self, command, cleanup):
        command.side_effect = ["container-id", "1 failed", json.dumps({"Running": False, "ExitCode": 1}), "1 failed"]
        with self.assertRaisesMessage(IntegrationError, "exited with code 1") as caught:
            execution.run_validation_check(self.run, self.workspace, ["npm", "test"], 0)
        self.assertEqual(caught.exception.check_result["exit_code"], 1)
        self.assertFalse(caught.exception.check_result["passed"])
        cleanup.assert_called_once()

    @patch("investigations.execution.subprocess.run")
    @patch("investigations.execution.command")
    def test_timeout_retains_redacted_output_and_identifies_source_copy(self, command, cleanup):
        command.side_effect = ["container-id", execution.CommandError("Regression check timed out", timed_out=True),
                               json.dumps({"Running": True, "ExitCode": 0}), "BUGPILOT_PHASE=copying_source\nsecret-token"]
        with self.assertRaises(IntegrationError) as caught:
            execution.run_validation_check(self.run, self.workspace, ["npm", "test"], 0)
        result = caught.exception.check_result
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["phase"], "source_copy")
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertIn("[redacted]", Path(result["log_path"]).read_text())
        cleanup.assert_called_once()

    @patch("investigations.execution.subprocess.run")
    @patch("investigations.execution.command")
    def test_memory_failure_is_distinct_from_timeout(self, command, cleanup):
        command.side_effect = ["container-id", "Killed", json.dumps({"Running": False, "ExitCode": 137, "OOMKilled": True}), "Killed"]
        with self.assertRaisesMessage(IntegrationError, "memory limit (2g)") as caught:
            execution.run_validation_check(self.run, self.workspace, ["pnpm", "test"], 0)
        self.assertTrue(caught.exception.check_result["oom_killed"])
        self.assertFalse(caught.exception.check_result["timed_out"])

    @patch("investigations.execution.subprocess.run")
    @patch("investigations.execution.os.killpg", create=True)
    @patch("investigations.execution.subprocess.Popen")
    def test_command_timeout_captures_partial_output_before_cleanup(self, popen, killpg, cleanup):
        def launch(*args, **kwargs):
            kwargs["stdout"].write(b"test started secret-token\n")
            return popen.return_value
        popen.side_effect = launch
        popen.return_value.pid = 12345
        popen.return_value.communicate.side_effect = subprocess.TimeoutExpired("docker", 300)
        log = self.workspace.parent / "timeout.log"
        with self.assertRaises(execution.CommandError) as caught:
            execution.command(["docker", "start", "--attach", "test"], timeout=300, log_path=log)
        self.assertTrue(caught.exception.timed_out)
        self.assertEqual(caught.exception.output, "test started [redacted]")
        self.assertEqual(log.read_text(), "test started [redacted]")
        popen.return_value.wait.assert_called_once()
