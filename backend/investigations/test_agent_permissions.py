"""Sandbox readiness and actionable no-change diagnostics."""
import os
import tempfile
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import execution
from .integrations import IntegrationError
from .models import Investigation, Repository
from .tests import LIVE_SETTINGS


@override_settings(**LIVE_SETTINGS, BUGPILOT_WINDOWS_SANDBOX="mxc")
class AgentPermissionTests(SimpleTestCase):
    def setUp(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name) / "repo"
        self.workspace.mkdir()
        self.run = Investigation(attempt=2, base_branch="trunk", proposal={"summary": "Workspace is read-only. openai-test"})
        self.run.repository = Repository(name="repo", url="https://github.com/acme/repo")

    @patch("investigations.execution.command", return_value="BUGPILOT_SANDBOX_READY")
    def test_permission_probe_is_sandboxed_and_has_no_service_credentials(self, command):
        env = {"CODEX_HOME": "isolated-home", "CODEX_API_KEY": "private", "OPENAI_API_KEY": "private", "GITHUB_TOKEN": "private", "JIRA_API_TOKEN": "private"}
        execution.check_windows_sandbox(self.workspace, env)
        argv = command.call_args.args[0]
        self.assertIn('windows.sandbox="mxc"', argv)
        self.assertIn("--include-managed-config", argv)
        self.assertIn(":workspace", argv)
        self.assertEqual(command.call_args.kwargs["env"], {"CODEX_HOME": "isolated-home"})
        self.assertEqual(command.call_args.kwargs["cwd"], self.workspace)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_windows_runtime_paths_are_preserved_but_provider_keys_are_not(self):
        with patch.dict(os.environ, {"SystemDrive": "C:", "LOCALAPPDATA": "C:/local", "OPENAI_API_KEY": "private", "CODEX_API_KEY": "private"}):
            env = execution.clean_environment()
        self.assertEqual(env.get("SystemDrive", env.get("SYSTEMDRIVE")), "C:")
        self.assertEqual(env["LOCALAPPDATA"], "C:/local")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)

    @skipUnless(os.name == "nt", "Native Windows sandbox only")
    @patch("investigations.execution.command")
    @patch("investigations.execution.check_windows_sandbox", side_effect=IntegrationError("workspace permission check failed"))
    def test_failed_permission_check_stops_before_calling_the_coding_model(self, probe, command):
        with self.assertRaisesMessage(IntegrationError, "permission check failed"):
            execution.investigate_and_fix(self.run, self.workspace)
        command.assert_not_called()

    @patch("investigations.execution.git", return_value="")
    def test_no_change_error_preserves_the_reason_but_redacts_credentials(self, git):
        with self.assertRaises(IntegrationError) as caught:
            execution.validate_proposal(self.run, self.workspace)
        message = str(caught.exception)
        self.assertIn("Codex reported: Workspace is read-only", message)
        self.assertIn("[redacted]", message)
        self.assertNotIn("openai-test", message)

    @patch("investigations.execution.command", return_value="")
    @patch("investigations.execution.check_windows_sandbox")
    def test_success_without_a_new_proposal_cannot_reuse_a_stale_result(self, probe, command):
        artifacts = self.workspace.parent / "agent-attempt-2"
        artifacts.mkdir()
        result = artifacts / "proposal.json"
        result.write_text('{"summary":"stale","root_cause":"stale","tests_added":[]}', encoding="utf-8")
        with self.assertRaisesMessage(IntegrationError, "valid fix summary"):
            execution.investigate_and_fix(self.run, self.workspace)
        self.assertFalse(result.exists())
        self.assertTrue((artifacts / "agent-output.log").exists())
