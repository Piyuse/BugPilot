"""Real Git checkout of paths longer than the legacy Windows MAX_PATH limit."""
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import execution
from .models import Investigation, Repository


class LongPathCheckoutTests(SimpleTestCase):
    def test_deep_files_are_checked_out_and_longpaths_persist_for_agent_git(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        directory = Path(temp.name)
        self.assertTrue(directory.resolve().is_relative_to(root.resolve()))
        if os.name == "nt":
            # Python's directory cleanup needs extended paths on this worker too.
            temp.name = "\\\\?\\" + str(directory.resolve())
        self.addCleanup(temp.cleanup)
        upstream = directory / "source"
        upstream.mkdir()
        self.enterContext(override_settings(BUGPILOT_WORKSPACE_ROOT=directory / ("workspace-" + "x" * 60)))
        execution.git(["init", "-b", "canary"], cwd=upstream)
        relative = Path("tests") / ("fixture-" + "a" * 65) / ("case-" + "b" * 65) / "input.js"
        source = upstream / relative
        source.parent.mkdir(parents=True)
        source.write_text("export default 42;\n", encoding="utf-8")
        execution.git(["add", "."], cwd=upstream)
        execution.git(["commit", "-m", "deep fixture"], cwd=upstream)
        run = Investigation(branch="codex/long-path-fix", base_branch="canary", attempt=1)
        run.repository = Repository(name="deep", url="https://github.com/acme/deep")
        run.save = Mock()
        original_git = execution.git

        def local_git(args, **kwargs):
            args = [upstream.as_uri() if arg == run.repository.url + ".git" else arg for arg in args]
            kwargs.pop("authenticated", None)
            return original_git(args, **kwargs)

        with patch("investigations.execution.git", side_effect=local_git):
            workspace = execution.prepare_workspace(run)
        checked_out = workspace / relative
        self.assertGreater(len(str(checked_out)), 260)
        readable = Path("\\\\?\\" + str(checked_out.resolve())) if os.name == "nt" else checked_out
        self.assertEqual(readable.read_text(encoding="utf-8"), "export default 42;\n")
        self.assertEqual(execution.git(["config", "--local", "--get", "core.longpaths"], cwd=workspace), "true")
        self.assertEqual(execution.git(["status", "--porcelain"], cwd=workspace), "")
