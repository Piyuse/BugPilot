"""Git network budgets and process cleanup use real execution helpers."""
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import execution
from .integrations import IntegrationError
from .models import Investigation, Repository


@override_settings(BUGPILOT_GIT_NETWORK_TIMEOUT=600)
class GitTimeoutTests(SimpleTestCase):
    def setUp(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        self.enterContext(override_settings(BUGPILOT_WORKSPACE_ROOT=Path(temp.name)))

    @patch("investigations.execution.command", return_value="")
    def test_network_commands_get_configured_budget_and_local_commands_stay_bounded(self, command):
        for operation in ("clone", "fetch", "push", "ls-remote", "checkout", "diff", "rev-parse"):
            with self.subTest(operation=operation):
                execution.git([operation, "placeholder"])
                self.assertEqual(command.call_args.kwargs["timeout"], 600 if operation in {"clone", "fetch", "push", "ls-remote"} else 120)
                self.assertEqual(command.call_args.kwargs["operation"], "git " + operation)
                self.assertIn("core.longpaths=true", command.call_args.args[0])
        with override_settings(BUGPILOT_GIT_NETWORK_TIMEOUT=900):
            execution.git(["clone", "placeholder"])
            self.assertEqual(command.call_args.kwargs["timeout"], 900)

    @patch("investigations.execution.subprocess.run")
    @patch("investigations.execution.os.killpg", create=True)
    @patch("investigations.execution.subprocess.Popen")
    def test_timeout_reports_git_operation_and_terminates_its_process_tree(self, popen, killpg, run):
        process = popen.return_value
        process.pid = 12345
        process.communicate.side_effect = subprocess.TimeoutExpired("git", 600)
        with self.assertRaisesMessage(IntegrationError, "git clone timed out after 600 seconds"):
            execution.command(["git", "clone", "placeholder"], timeout=600, operation="git clone")
        process.wait.assert_called_once()
        if execution.os.name == "nt":
            self.assertEqual(run.call_args.args[0], ["taskkill", "/F", "/T", "/PID", "12345"])
        else:
            killpg.assert_called_once()

    @patch("investigations.execution.git")
    def test_remote_fix_recovery_is_shallow_and_progress_identifies_each_operation(self, git):
        git.side_effect = ["", "base-sha", "remote-branch-sha", "", ""]
        investigation = Investigation(branch="codex/recover", base_branch="canary", attempt=2)
        investigation.repository = Repository(name="repo", url="https://github.com/acme/repo")
        investigation.save = Mock()
        progress = Mock()
        execution.prepare_workspace(investigation, on_progress=progress)
        clone = git.call_args_list[0].args[0]
        self.assertIn("--depth", clone)
        self.assertIn("--no-tags", clone)
        self.assertEqual(clone[clone.index("--config") + 1], "core.longpaths=true")
        self.assertEqual(clone[clone.index("--branch") + 1], "canary")
        fetch = git.call_args_list[3].args[0]
        self.assertEqual(fetch[:5], ["fetch", "--depth", "1", "--no-tags", "https://github.com/acme/repo.git"])
        messages = " ".join(call.args[0] for call in progress.call_args_list)
        self.assertIn("Downloading", messages)
        self.assertIn("600s", messages)
        self.assertIn("Recovering", messages)
        self.assertEqual(investigation.base_sha, "base-sha")
