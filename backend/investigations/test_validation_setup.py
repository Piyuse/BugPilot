import io
import json
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from .integrations import IntegrationError
from .models import Investigation, Repository
from .pipeline import run_investigation
from .tests import LIVE_SETTINGS
from .validation_setup import automatic_validation, build_context, build_test_image, detect_validation, prepare_automatic_validation, transient_builder_error


class DetectionTests(SimpleTestCase):
    def setUp(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def write(self, name, content):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_node_uses_locked_dependencies_and_declared_manager(self):
        self.write("package.json", json.dumps({"scripts": {"test": "vitest run"}, "packageManager": "pnpm@10.12.1"}))
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'")
        self.write(".nvmrc", "24")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["pnpm", "test"]])
        self.assertIn("node:24-bookworm", plan.dockerfile)
        self.assertIn('"pnpm@10.12.1"', plan.dockerfile)
        self.assertIn('["pnpm", "install", "--frozen-lockfile"]', plan.dockerfile)

    def test_pnpm_keeps_workspace_layout_without_credentials_or_script_overrides(self):
        self.write("package.json", json.dumps({"scripts": {"test": "lerna run test"}, "packageManager": "pnpm@9.4.0"}))
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'")
        self.write(".npmrc", '\ufeff# Workspace dependencies\nlink-workspace-packages = true\nnode-linker="hoisted" # flat layout\n'
            'prefer-workspace-packages=false\nshared-workspace-lockfile=true\n'
            '//registry.npmjs.org/:_authToken=secret-token\nregistry=https://private.example.com\n'
            'ignore-scripts=true\nnode-options=--require=/host/hook.js\n')
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.npmrc, "link-workspace-packages=true\nnode-linker=hoisted\nprefer-workspace-packages=false\nshared-workspace-lockfile=true\n")
        self.assertEqual(plan.commands, [["pnpm", "test"]])
        self.assertIn('["pnpm", "install", "--frozen-lockfile"]', plan.dockerfile)
        self.assertNotIn("ignore-scripts", plan.dockerfile)
        self.assertNotIn("secret-token", plan.npmrc)

    def test_pnpm_layout_setting_cannot_expand_secrets_or_inject_build_instructions(self):
        self.write("package.json", json.dumps({"scripts": {"test": "vitest run"}, "packageManager": "pnpm@9.4.0"}))
        for value in ("${TOKEN}", "hoisted && curl example.com", "unknown"):
            with self.subTest(value=value):
                self.write(".npmrc", "node-linker=" + value)
                with self.assertRaisesMessage(IntegrationError, "Unsupported value for pnpm setting 'node-linker'"):
                    detect_validation(self.workspace)

    def test_npm_test_and_literal_python_ci_dependencies_are_detected(self):
        self.write("package.json", json.dumps({"scripts": {"test": "node --test tests/*.test.js"}}))
        self.write("package-lock.json", "{}")
        self.write(".github/workflows/test.yml", "run: pip install pandas\nrun: pip install $SECRET\nrun: pip install ok && curl evil\n")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["npm", "test"]])
        self.assertIn('"npm", "ci"', plan.dockerfile)
        self.assertIn('"pandas"', plan.dockerfile)
        self.assertIn("FROM python:3.12-bookworm", plan.dockerfile)
        self.assertNotIn("SECRET", plan.dockerfile)
        self.assertNotIn("curl", plan.dockerfile)

    def test_modern_yarn_is_prepared_through_corepack_with_an_offline_cache(self):
        self.write("package.json", json.dumps({"scripts": {"test": "vitest run"}, "packageManager": "yarn@4.9.2"}))
        self.write("yarn.lock", "# lockfile")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["yarn", "test"]])
        self.assertIn('["corepack", "install", "--global", "yarn@4.9.2"]', plan.dockerfile)
        self.assertIn('"--immutable"', plan.dockerfile)
        self.assertIn("COREPACK_HOME=/work/", plan.dockerfile)

    def test_placeholder_or_missing_test_script_does_not_count_as_validation(self):
        for script in (None, "", 'echo "Error: no test specified" && exit 1', "exit 0", "echo success"):
            with self.subTest(script=script):
                self.write("package.json", json.dumps({"scripts": {"test": script}}))
                with self.assertRaisesMessage(IntegrationError, "no runnable test script"):
                    detect_validation(self.workspace)

    def test_python_installs_project_test_extra_and_requirements(self):
        self.write("pyproject.toml", '[project]\nname="example"\nversion="1.0"\n[project.optional-dependencies]\ntest=["pytest-asyncio"]\n[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n')
        self.write("requirements.txt", "requests==2.32.0")
        self.write(".python-version", "3.13.2")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["python", "-m", "pytest"]])
        self.assertIn("python:3.13.2-bookworm", plan.dockerfile)
        self.assertIn('"-r", "requirements.txt"', plan.dockerfile)
        self.assertIn('"-e", ".[test]"', plan.dockerfile)

    def test_flat_uv_project_syncs_cpu_and_dev_dependencies_without_packaging(self):
        self.write("pyproject.toml", '''[project]
name = "flat-application"
version = "0.1.0"
dependencies = ["requests"]
[project.optional-dependencies]
cpu = ["numpy"]
gpu = ["cuda-python"]
[dependency-groups]
dev = ["pytest<9", "pytest-asyncio"]
[tool.uv]
default-groups = []
conflicts = [[{extra = "cpu"}, {extra = "gpu"}]]
''')
        for name in ("dev/helper.py", "runs/start.py", "application/__init__.py"):
            self.write(name, "")
        self.write("uv.lock", "version = 1\n")
        self.write(".python-version", "3.10")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["python", "-m", "pytest"]])
        self.assertIn("Python (uv", plan.label)
        self.assertIn("python:3.10-bookworm", plan.dockerfile)
        self.assertIn('["uv", "sync", "--python", "/usr/local/bin/python", "--frozen", "--extra", "cpu", "--group", "dev"]', plan.dockerfile)
        self.assertIn("UV_PROJECT_ENVIRONMENT=/opt/bugpilot-venv", plan.dockerfile)
        self.assertIn('PATH="/opt/bugpilot-venv/bin:${PATH}"', plan.dockerfile)
        self.assertNotIn('"-e"', plan.dockerfile)
        self.assertNotIn('"--extra", "gpu"', plan.dockerfile)
        self.assertNotIn("--all-extras", plan.dockerfile)
        self.assertNotIn("uv run", plan.dockerfile)

    def test_project_metadata_without_a_build_system_does_not_imply_a_package(self):
        self.write("pyproject.toml", '[project]\nname="scripts"\nversion="0.1"\ndependencies=["requests"]\n')
        plan = detect_validation(self.workspace)
        self.assertIn('["uv", "sync"', plan.dockerfile)
        self.assertNotIn('"--frozen"', plan.dockerfile)
        self.assertNotIn('"-e"', plan.dockerfile)

    def test_uv_lock_with_one_off_resolution_options_is_used_without_relocking(self):
        self.write("pyproject.toml", '[project]\nname="application"\nversion="0.1"\n')
        self.write("uv.lock", 'version=1\nrevision=3\n[options]\nexclude-newer="0001-01-01T00:00:00Z"\nexclude-newer-span="P7D"\n')
        plan = detect_validation(self.workspace)
        self.assertIn('"--frozen"', plan.dockerfile)
        self.assertNotIn('"--locked"', plan.dockerfile)
        self.assertNotIn('["uv", "lock"', plan.dockerfile)

    def test_uv_packaging_override_and_test_group_are_respected(self):
        self.write("pyproject.toml", '''[project]
name = "application"
version = "0.1"
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
[dependency-groups]
test = ["pytest"]
[tool.uv]
package = false
''')
        self.assertIn('"--group", "test"', detect_validation(self.workspace).dockerfile)
        self.assertNotIn('"-e"', detect_validation(self.workspace).dockerfile)

    def test_uv_project_includes_declared_test_extra_and_supplemental_requirements(self):
        self.write("pyproject.toml", '[project]\nname="application"\nversion="0.1"\n[project.optional-dependencies]\ntests=["pytest-asyncio"]\n')
        self.write("requirements-test.txt", "httpx")
        plan = detect_validation(self.workspace)
        self.assertIn('"--extra", "tests"', plan.dockerfile)
        self.assertIn('["uv", "pip", "install", "--python", "/opt/bugpilot-venv/bin/python", "-r", "requirements-test.txt"]', plan.dockerfile)

    def test_legacy_setup_py_still_installs_the_package(self):
        self.write("setup.py", "from setuptools import setup\nsetup(name='library', py_modules=['library'])")
        plan = detect_validation(self.workspace)
        self.assertIn('"-e", "."', plan.dockerfile)
        self.assertNotIn('"uv", "sync"', plan.dockerfile)

    def test_setup_cfg_metadata_is_packaged_but_pytest_configuration_is_not(self):
        self.write("setup.cfg", "[metadata]\nname=library\n[options]\npackages=find:\n")
        self.assertIn('"-e", "."', detect_validation(self.workspace).dockerfile)
        self.write("setup.cfg", "[tool:pytest]\ntestpaths=tests\n")
        self.assertNotIn('"-e"', detect_validation(self.workspace).dockerfile)

    def test_pytest_only_pyproject_does_not_install_a_nonexistent_package(self):
        self.write("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
        plan = detect_validation(self.workspace)
        self.assertIn('"install", "--no-cache-dir", "pytest"', plan.dockerfile)
        self.assertNotIn('"-e"', plan.dockerfile)
        self.assertNotIn('"uv", "sync"', plan.dockerfile)

    def test_requirements_with_uv_pip_configuration_do_not_imply_a_uv_project(self):
        self.write("requirements.txt", "requests")
        self.write("uv.toml", "[pip]\ncompile-bytecode=true\n")
        plan = detect_validation(self.workspace)
        self.assertIn('"-r", "requirements.txt"', plan.dockerfile)
        self.assertNotIn('"uv", "sync"', plan.dockerfile)
        self.assertNotIn('"-e"', plan.dockerfile)

    def test_invalid_python_metadata_has_an_actionable_error(self):
        for text in ('project = "invalid"', 'dependency-groups = []', '[project]\noptional-dependencies = []'):
            with self.subTest(text=text):
                self.write("pyproject.toml", text)
                with self.assertRaisesMessage(IntegrationError, "invalid"):
                    detect_validation(self.workspace)

    def test_unknown_setup_is_not_silently_marked_passing(self):
        self.write("README.md", "a project")
        with self.assertRaisesMessage(IntegrationError, "could not detect"):
            detect_validation(self.workspace)

    def test_build_context_filters_secrets_and_replaces_untrusted_dockerfile(self):
        snapshot = self.workspace / "source.tar"
        target = self.workspace / "context.tar"
        with tarfile.open(snapshot, "w") as archive:
            for name in ("test.js", ".env", "nested/.env.production", ".npmrc", "key.pem", ".git/config", ".dockerignore", ".bugpilot-runtime/Dockerfile"):
                data = b"untrusted"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        build_context(snapshot, target, "FROM trusted\n")
        # Docker must identify this as an archive even when Git supplies PAX headers.
        self.assertEqual(target.read_bytes()[:2], b"\x1f\x8b")
        with tarfile.open(target) as archive:
            self.assertEqual(set(archive.getnames()), {"test.js", ".dockerignore", ".bugpilot-runtime/Dockerfile"})
            self.assertEqual(archive.extractfile(".bugpilot-runtime/Dockerfile").read(), b"FROM trusted\n")

    def test_build_context_rejects_symlink_to_host_files(self):
        snapshot = self.workspace / "source.tar"
        with tarfile.open(snapshot, "w") as archive:
            info = tarfile.TarInfo("outside")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with self.assertRaisesMessage(IntegrationError, "symlink"):
            build_context(snapshot, self.workspace / "context.tar", "FROM trusted\n")

    def test_build_context_replaces_original_npmrc_with_safe_workspace_settings(self):
        snapshot = self.workspace / "source.tar"
        target = self.workspace / "context.tar.gz"
        with tarfile.open(snapshot, "w") as archive:
            for name in (".npmrc", "packages/core/.npmrc"):
                data = b"//registry.npmjs.org/:_authToken=secret-token\nnode-linker=hoisted\n"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        safe = "link-workspace-packages=true\nnode-linker=hoisted\n"
        build_context(snapshot, target, "FROM trusted\n", npmrc=safe)
        with tarfile.open(target) as archive:
            self.assertEqual(archive.extractfile(".npmrc").read().decode(), safe)
            self.assertNotIn("packages/core/.npmrc", archive.getnames())
            for member in archive:
                self.assertNotIn(b"secret-token", archive.extractfile(member).read())


@override_settings(**LIVE_SETTINGS)
class AutomaticValidationPipelineTests(TestCase):
    def setUp(self):
        self.repository = Repository.objects.create(name="web", url="https://github.com/acme/web", validation_commands=[])
        self.run = Investigation.objects.create(issue_key="acme/web#1", source_repository_url=self.repository.url, source_issue_number=1,
            requested_repository=self.repository, status="running", attempt=1)

    def test_registry_accepts_automatic_setup_without_image_or_commands(self):
        client = APIClient()
        client.force_authenticate(get_user_model().objects.create_user("owner", is_staff=True))
        result = client.patch(f"/api/repositories/{self.repository.pk}/", {"runtime_image": "", "validation_commands": []}, format="json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["validation_commands"], [])
        result = client.patch(f"/api/repositories/{self.repository.pk}/", {"runtime_image": "", "validation_commands": [["npm", "test"]]}, format="json")
        self.assertEqual(result.status_code, 400)

    @patch("investigations.pipeline.prepare_automatic_validation")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.GitHubClient")
    def test_empty_commands_trigger_preparation_before_agent_and_still_require_tests(self, github, model, executor, prepare):
        calls = []
        github.return_value.default_branch.return_value = "trunk"
        github.return_value.issue.return_value = {"title": "Crash", "url": self.repository.url + "/issues/1"}
        model.return_value.preprocess.return_value = {"summary": "Crash"}
        prepare.side_effect = lambda *a, **kw: calls.append("prepare")
        executor.investigate_and_fix.side_effect = lambda *a: calls.append("agent") or {"summary": "fix"}
        executor.validate_proposal.side_effect = IntegrationError("A regression failed")
        executor.redact.side_effect = lambda text: text
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(calls, ["prepare", "agent"])
        self.assertIn("preparing_validation", list(self.run.events.values_list("stage", flat=True)))
        self.assertEqual(self.run.status, "needs_attention")
        self.assertEqual(self.run.error, "A regression failed")
        executor.publish_pull_request.assert_not_called()
        github.return_value.comment_issue_pull_request.assert_not_called()

    @patch("investigations.validation_setup.execution.git", return_value="a" * 40)
    @patch("investigations.validation_setup.execution.command", return_value="linux")
    @patch("investigations.validation_setup.detect_validation")
    def test_ready_image_is_cached_and_saved_but_remains_automatic(self, detect, command, git):
        from .validation_setup import ValidationPlan
        detect.return_value = ValidationPlan("Node", [["npm", "test"]], "FROM node:22-bookworm\n")
        prepare_automatic_validation(self.repository, Path("unused"))
        self.repository.refresh_from_db()
        self.assertTrue(automatic_validation(self.repository))
        self.assertEqual(self.repository.validation_commands, [["npm", "test"]])
        self.assertEqual(command.call_count, 1)

    @patch("investigations.validation_setup.execution.git", return_value="a" * 40)
    @patch("investigations.validation_setup.execution.command", return_value="linux")
    @patch("investigations.validation_setup.detect_validation")
    def test_workspace_layout_changes_invalidate_the_cached_image(self, detect, command, git):
        from .validation_setup import ValidationPlan
        recipe = "FROM node:22-bookworm\n"
        detect.side_effect = [
            ValidationPlan("Node", [["pnpm", "test"]], recipe, "node-linker=isolated\n"),
            ValidationPlan("Node", [["pnpm", "test"]], recipe, "node-linker=hoisted\n"),
        ]
        prepare_automatic_validation(self.repository, Path("unused"))
        first = self.repository.runtime_image
        prepare_automatic_validation(self.repository, Path("unused"))
        self.assertNotEqual(first, self.repository.runtime_image)
        self.assertEqual(self.repository.validation_commands, [["pnpm", "test"]])
        self.assertEqual(command.call_count, 2)

    @patch("investigations.validation_setup.build_context")
    @patch("investigations.validation_setup.execution.git", return_value="a" * 40)
    @patch("investigations.validation_setup.execution.command", side_effect=[IntegrationError("missing"), "ready", IntegrationError("install failed")])
    @patch("investigations.validation_setup.detect_validation")
    def test_failed_image_build_does_not_save_fake_ready_configuration(self, detect, command, git, context):
        from .validation_setup import ValidationPlan
        detect.return_value = ValidationPlan("Node", [["npm", "test"]], "FROM node:22-bookworm\n")
        with tempfile.TemporaryDirectory(dir=settings.BASE_DIR / ".test-tmp") as root, override_settings(BUGPILOT_WORKSPACE_ROOT=Path(root)):
            with self.assertRaisesMessage(IntegrationError, "install failed"):
                prepare_automatic_validation(self.repository, Path(root))
        self.repository.refresh_from_db()
        self.assertEqual(self.repository.validation_commands, [])
        self.assertEqual(self.repository.runtime_image, "python:3.12-slim")
        self.assertEqual(command.call_args.args[0][1], "build")
        self.assertIsInstance(command.call_args.kwargs["stdin"], Path)

    @patch("investigations.validation_setup.build_context")
    @patch("investigations.validation_setup.execution.git", return_value="a" * 40)
    @patch("investigations.validation_setup.execution.command")
    def test_completed_image_is_recovered_after_export_client_timeout(self, command, git, context):
        command.side_effect = [IntegrationError("missing"), "ready", IntegrationError("Automatic test environment preparation timed out after 1199 seconds."), "linux", "linux"]
        progress = []
        with tempfile.TemporaryDirectory(dir=settings.BASE_DIR / ".test-tmp") as root, override_settings(BUGPILOT_WORKSPACE_ROOT=Path(root)):
            workspace = Path(root)
            (workspace / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8")
            prepare_automatic_validation(self.repository, workspace, on_progress=progress.append)
        self.repository.refresh_from_db()
        self.assertTrue(self.repository.runtime_image.startswith("bugpilot-auto-"))
        self.assertEqual(self.repository.validation_commands, [["npm", "test"]])
        self.assertTrue(any("Using the completed image" in message for message in progress))

    @patch("investigations.validation_setup.build_context")
    @patch("investigations.validation_setup.execution.git", return_value="a" * 40)
    @patch("investigations.validation_setup.execution.command")
    def test_timeout_without_a_completed_linux_image_still_fails(self, command, git, context):
        for result in (IntegrationError("missing"), "windows"):
            with self.subTest(result=str(result)), tempfile.TemporaryDirectory(dir=settings.BASE_DIR / ".test-tmp") as root, override_settings(BUGPILOT_WORKSPACE_ROOT=Path(root)):
                command.side_effect = [IntegrationError("missing"), "ready", IntegrationError("Automatic test environment preparation timed out after 1199 seconds."), result]
                workspace = Path(root)
                (workspace / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8")
                with self.assertRaisesMessage(IntegrationError, "timed out"):
                    prepare_automatic_validation(self.repository, workspace)
                self.repository.refresh_from_db()
                self.assertEqual(self.repository.validation_commands, [])
                self.assertEqual(self.repository.runtime_image, "python:3.12-slim")

    def test_manual_configuration_is_preserved(self):
        self.repository.validation_commands = [["custom", "check"]]
        self.repository.runtime_image = "team/prepared:ready"
        self.assertFalse(automatic_validation(self.repository))


class BuilderRecoveryTests(SimpleTestCase):
    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    def test_resource_saver_disconnect_rechecks_builder_and_retries_same_archive(self, command, sleep):
        command.side_effect = ["ready", IntegrationError("listing workers for Build: failed to list workers: Unavailable: error reading from server: EOF"), "ready", "built"]
        progress = []
        context = Path("context.tar.gz")
        build_test_image("bugpilot-auto-1:test", context, on_progress=progress.append)
        builds = [call for call in command.call_args_list if call.args[0][1] == "build"]
        self.assertEqual(len(builds), 2)
        self.assertTrue(all(call.kwargs["stdin"] == context for call in builds))
        self.assertEqual([call.args[0][1:3] for call in command.call_args_list], [["buildx", "inspect"], ["build", "--file"], ["buildx", "inspect"], ["build", "--file"]])
        self.assertIn("2/3", progress[0])

    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    def test_starting_builder_is_waited_for(self, command, sleep):
        command.side_effect = [IntegrationError("rpc error: code = Unavailable"), "ready", "built"]
        build_test_image("bugpilot-auto-1:test", Path("context.tar.gz"))
        self.assertEqual(command.call_count, 3)
        sleep.assert_called_once()

    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    def test_repeated_disconnects_stop_after_three_builds(self, command, sleep):
        command.side_effect = ["ready", IntegrationError("error reading from server: EOF"), "ready", IntegrationError("error reading from server: EOF"), "ready", IntegrationError("error reading from server: EOF")]
        with self.assertRaisesMessage(IntegrationError, "all 3 attempts"):
            build_test_image("bugpilot-auto-1:test", Path("context.tar.gz"))
        self.assertEqual(len([call for call in command.call_args_list if call.args[0][1] == "build"]), 3)

    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    def test_package_install_failure_is_not_retried(self, command, sleep):
        command.side_effect = ["ready", IntegrationError("npm ci: lockfile does not match package.json")]
        with self.assertRaisesMessage(IntegrationError, "lockfile"):
            build_test_image("bugpilot-auto-1:test", Path("context.tar.gz"))
        self.assertEqual(command.call_count, 2)
        sleep.assert_not_called()

    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    @patch("investigations.validation_setup.time.monotonic")
    @override_settings(BUGPILOT_VALIDATION_SETUP_TIMEOUT=10)
    def test_retries_share_one_setup_deadline(self, monotonic, command, sleep):
        monotonic.side_effect = [0, 0, 0, 0, 11, 11, 11]
        command.side_effect = ["ready", IntegrationError("error reading from server: EOF")]
        with self.assertRaisesMessage(IntegrationError, "setup time limit"):
            build_test_image("bugpilot-auto-1:test", Path("context.tar.gz"))
        self.assertEqual(command.call_count, 2)

    def test_permission_and_recipe_errors_are_not_transport_failures(self):
        for message in ("permission denied connecting to Docker", "access is denied", "Dockerfile parse error: unexpected EOF", "npm ERR! 401 Unauthorized", "RUN pip install exited with code 1"):
            self.assertFalse(transient_builder_error(IntegrationError(message)))
