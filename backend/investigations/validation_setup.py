"""Detect test commands and prepare dependency images before invoking the agent.

Repository commands only execute inside Docker. Build contexts contain the Git
snapshot, never the worker's environment, Git credentials or untracked files.
"""
import ast
import configparser
import hashlib
import io
import json
import re
import shlex
import tarfile
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass
from itertools import chain, islice
from pathlib import Path, PurePosixPath

from django.conf import settings

from . import execution
from .integrations import IntegrationError


AUTO_IMAGE_PREFIX = "bugpilot-auto-"
CONTEXT_DIR = ".bugpilot-runtime"
UV_VERSION = "0.13.6"
TORCH_CPU_VERSION = "2.9.1+cpu"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYTHON_REQUIREMENTS = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "requirements-tests.txt",
                       "test-requirements.txt", "requirements/dev.txt", "requirements/test.txt", "requirements/tests.txt",
                       "test/requirements.txt", "tests/requirements.txt")


def automatic_validation(repository):
    return not repository.validation_commands or repository.runtime_image.startswith(AUTO_IMAGE_PREFIX)


@dataclass
class ValidationPlan:
    label: str
    commands: list
    dockerfile: str
    npmrc: str = ""
    python_collection: bool = False


def read_file(workspace, name):
    path = workspace / name
    if not path.is_file():
        return ""
    if not path.resolve().is_relative_to(workspace.resolve()) or path.stat().st_size > 1024 * 1024:
        raise IntegrationError(f"Cannot safely inspect validation manifest: {name}.")
    return path.read_text(encoding="utf-8")


def ci_python_packages(workspace):
    """Recognize literal pip requirements in CI, not arbitrary workflow scripts."""
    packages = []
    workflows = workspace / ".github" / "workflows"
    if not workflows.is_dir():
        return packages
    for path in sorted(workflows.glob("*.y*ml"))[:30]:
        for line in read_file(workspace, path.relative_to(workspace).as_posix()).splitlines():
            match = re.match(r"\s*(?:-\s*)?(?:run:\s*)?(?:python[\d.]*\s+-m\s+)?pip[\d.]*\s+install\s+(.+)", line)
            if not match:
                continue
            try:
                args = shlex.split(match[1])
            except ValueError:
                continue
            # Ignore flags, shell fragments, URLs, secrets and dynamic expressions.
            if args and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:[=<>!~]{1,2}[A-Za-z0-9.*+_-]+)?", arg) for arg in args):
                packages.extend(args)
    return list(dict.fromkeys(packages))


def runtime_version(value, default):
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+|x)(?:\.(\d+|x))?)?", value.strip())
    if not match:
        return default
    return ".".join(part for part in match.groups() if part and part != "x")


def pnpm_workspace_config(workspace):
    """Preserve dependency layout settings without copying npm credentials or hooks."""
    allowed = {
        "link-workspace-packages": {"true", "false", "deep"},
        "prefer-workspace-packages": {"true", "false"},
        "shared-workspace-lockfile": {"true", "false"},
        "node-linker": {"isolated", "hoisted", "pnp"},
    }
    settings = {}
    for line in read_file(workspace, ".npmrc").lstrip("\ufeff").splitlines():
        key, separator, value = line.strip().partition("=")
        key = key.strip().lower()
        if not separator or key not in allowed:
            continue
        value = re.split(r"\s+[;#]", value, maxsplit=1)[0].strip().strip("\"'")
        if value not in allowed[key]:
            raise IntegrationError(f"Unsupported value for pnpm setting '{key}'. Configure a custom test image in Repositories.")
        settings[key] = value
    return "".join(f"{key}={value}\n" for key, value in sorted(settings.items()))


def python_test_files(workspace):
    patterns = ("test_*.py", "*_test.py", "conftest.py")
    return islice(chain.from_iterable(
        (directory.rglob(pattern) if recursive else directory.glob(pattern))
        for directory, recursive in ((workspace, False), (workspace / "test", True), (workspace / "tests", True))
        for pattern in patterns), 200)


def tests_import_torch(workspace):
    """Recognize a known dependency from a required import, without running source."""
    for path in python_test_files(workspace):
        try:
            tree = ast.parse(read_file(workspace, path.relative_to(workspace).as_posix()))
        except (IntegrationError, OSError, UnicodeError, SyntaxError, RecursionError, ValueError):
            continue  # Collection gives the actual error if the file cannot be loaded.
        for node in tree.body:
            if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "torch" for alias in node.names):
                return True
            if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "torch":
                return True
    return False


def torch_cpu_fallback(pip):
    # Probe in the prepared interpreter, after declared dependencies were
    # installed. Never replace an existing torch installation or infer unknown
    # package names from imports. Run only during the online image build.
    args = [*pip, "--index-url", TORCH_CPU_INDEX, f"torch=={TORCH_CPU_VERSION}"]
    script = ("from importlib.util import find_spec\nfrom subprocess import check_call\n"
              "if find_spec('torch') is None:\n"
              f"    check_call({args!r})\n")
    return "RUN " + json.dumps(["python", "-c", script]) + "\n"


def python_validation(workspace, metadata, requirements, common):
    """Distinguish installable distributions from dependency-only Python projects."""
    project = metadata.get("project", {})
    tool = metadata.get("tool", {})
    groups = metadata.get("dependency-groups", {})
    if not all(isinstance(value, dict) for value in (project, tool, groups)):
        raise IntegrationError("Automatic validation found invalid Python project metadata. Configure a custom test image in Repositories.")
    uv = tool.get("uv", {})
    if not isinstance(uv, dict):
        raise IntegrationError("Automatic validation found invalid tool.uv metadata.")
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read_string(read_file(workspace, "setup.cfg"))
    except configparser.Error:
        raise IntegrationError("Automatic validation could not parse setup.cfg.") from None
    packaged = "build-system" in metadata or (workspace / "setup.py").is_file() or config.has_section("metadata")
    use_uv = ((workspace / "pyproject.toml").is_file()
              and ((workspace / "uv.lock").is_file() or (workspace / "uv.toml").is_file()
                   or "uv" in tool or bool(groups) or (bool(project) and not packaged)))
    version = runtime_version(read_file(workspace, ".python-version"), "3.12")
    extras = project.get("optional-dependencies", {})
    if not isinstance(extras, dict):
        raise IntegrationError("Automatic validation found invalid optional Python dependencies.")
    test_extra = next((name for name in ("test", "tests", "dev") if name in extras), None)
    additional = ci_python_packages(workspace)
    if use_uv:
        install = "RUN " + json.dumps(["python", "-m", "pip", "install", "--no-cache-dir", f"uv=={UV_VERSION}"]) + "\n"
        # Keep the interpreter/dependencies outside /work, which is replaced by
        # the writable test volume. Pytest runs directly, so checks never sync.
        install += 'ENV UV_PROJECT_ENVIRONMENT=/opt/bugpilot-venv UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 PATH="/opt/bugpilot-venv/bin:${PATH}"\n'
        sync = ["uv", "sync", "--python", "/usr/local/bin/python"]
        if (workspace / "uv.lock").is_file():
            # Install the committed resolution as-is. Locks may include
            # one-off resolver options absent from pyproject.toml; checking
            # freshness would re-resolve those dependencies during setup.
            sync.append("--frozen")
        if "cpu" in extras:
            sync.extend(["--extra", "cpu"])
        if test_extra:
            sync.extend(["--extra", test_extra])
        test_group = next((name for name in ("test", "tests", "dev") if name in groups), None)
        if test_group:
            sync.extend(["--group", test_group])
        elif uv.get("dev-dependencies"):
            sync.append("--dev")
        install += "RUN " + json.dumps(sync) + "\n"
        pip = ["uv", "pip", "install", "--python", "/opt/bugpilot-venv/bin/python"]
        for name in requirements:
            install += "RUN " + json.dumps([*pip, "-r", name]) + "\n"
        install += "RUN " + json.dumps([*pip, "pytest", *additional]) + "\n"
        label = "Python (uv + pytest)"
    else:
        pip = ["python", "-m", "pip", "install", "--no-cache-dir"]
        install = "RUN " + json.dumps([*pip, "pytest", *additional]) + "\n"
        for name in requirements:
            install += "RUN " + json.dumps([*pip, "-r", name]) + "\n"
        if packaged:
            install += "RUN " + json.dumps([*pip, "-e", f".[{test_extra}]" if test_extra else "."]) + "\n"
        label = "Python (pytest)"
    if not (workspace / "uv.lock").is_file() and tests_import_torch(workspace):
        install += torch_cpu_fallback(pip)
    return ValidationPlan(label, [["python", "-m", "pytest"]], f"FROM python:{version}-bookworm\n" + common + install,
                          python_collection=True)


def detect_validation(workspace):
    workspace = Path(workspace)
    common = (
        "ENV CI=true HOME=/work/.home XDG_CACHE_HOME=/work/.cache "
        "npm_config_cache=/work/.cache/npm COREPACK_HOME=/work/.cache/corepack "
        "COREPACK_ENABLE_DOWNLOAD_PROMPT=0 PYTHONDONTWRITEBYTECODE=1\n"
        "WORKDIR /work\nCOPY . /work/\nRUN mkdir -p /work/.home /work/.cache\n"
    )
    manifest = read_file(workspace, "package.json")
    if manifest:
        try:
            package = json.loads(manifest)
            script = package.get("scripts", {}).get("test", "")
        except (ValueError, AttributeError):
            raise IntegrationError("Automatic validation could not read package.json. Fix the manifest or configure custom checks in Repositories.") from None
        if not isinstance(script, str) or not script.strip() or "no test specified" in script.lower() or re.fullmatch(r"\s*(?:echo\b.*|exit\s+0)\s*", script):
            raise IntegrationError("Automatic validation found no runnable test script in package.json. Add a test script or configure custom checks in Repositories. No untested PR will be created.")
        node = runtime_version(read_file(workspace, ".nvmrc") or read_file(workspace, ".node-version"), "22")
        manager = package.get("packageManager", "")
        match = re.fullmatch(r"(npm|pnpm|yarn)@(\d+\.\d+\.\d+)(?:\+sha\d+\.[a-fA-F0-9]+)?", manager) if isinstance(manager, str) else None
        if manager and not match:
            raise IntegrationError("The declared packageManager is not supported by automatic validation. Configure its prepared image and checks in Repositories.")
        tool = match[1] if match else "pnpm" if (workspace / "pnpm-lock.yaml").is_file() else "yarn" if (workspace / "yarn.lock").is_file() else "npm"
        python_packages = ci_python_packages(workspace)
        base = f"FROM node:{node}-bookworm\n"
        if python_packages:
            base = f"FROM node:{node}-bookworm AS node-runtime\nFROM python:3.12-bookworm\nCOPY --from=node-runtime /usr/local/ /usr/local/\n"
        install = ""
        if tool == "yarn":
            # Modern Yarn releases are distributed through Corepack, not yarn@4 on npm.
            pinned = f"yarn@{match[2]}" if match else "yarn@1.22.22"
            install += 'RUN ["npm", "install", "--global", "--force", "corepack"]\n'
            install += 'RUN ["corepack", "enable"]\n'
            install += "RUN " + json.dumps(["corepack", "install", "--global", pinned]) + "\n"
        elif tool == "pnpm" or match:
            pinned = f"{tool}@{match[2]}" if match else "pnpm@10"
            install += "RUN " + json.dumps(["npm", "install", "--global", pinned]) + "\n"
        if tool == "npm":
            args = ["npm", "ci" if (workspace / "package-lock.json").is_file() or (workspace / "npm-shrinkwrap.json").is_file() else "install", "--no-audit", "--no-fund"]
        elif tool == "pnpm":
            install += "RUN pnpm config set store-dir /work/.pnpm-store\n"
            args = ["pnpm", "install", "--frozen-lockfile"] if (workspace / "pnpm-lock.yaml").is_file() else ["pnpm", "install"]
        else:
            args = ["yarn", "install", "--immutable" if match and int(match[2].split(".")[0]) >= 2 else "--frozen-lockfile"]
        install += "RUN " + json.dumps(args) + "\n"
        if python_packages:
            install += "RUN " + json.dumps(["python", "-m", "pip", "install", "--no-cache-dir", *python_packages]) + "\n"
        from .validation_profiles import node_runtime_additions
        additions = node_runtime_additions(workspace)
        install += additions
        npmrc = pnpm_workspace_config(workspace) if tool == "pnpm" else ""
        label = f"Node.js ({tool} test)" + (" with freeCodeCamp curriculum/browser setup" if additions else "")
        return ValidationPlan(label, [[tool, "test"]], base + common + install, npmrc)

    pyproject = read_file(workspace, "pyproject.toml")
    requirements = [name for name in PYTHON_REQUIREMENTS if (workspace / name).is_file()]
    if pyproject or requirements or (workspace / "setup.py").is_file() or (workspace / "setup.cfg").is_file() or (workspace / "pytest.ini").is_file() or next(python_test_files(workspace), None):
        try:
            metadata = tomllib.loads(pyproject) if pyproject else {}
        except ValueError:
            raise IntegrationError("Automatic validation could not parse pyproject.toml. Fix it or configure custom checks in Repositories.") from None
        return python_validation(workspace, metadata, requirements, common)
    raise IntegrationError("BugPilot could not detect a supported test setup. Automatic validation supports root package.json test scripts and Python projects. Configure a prepared test image and custom checks in Repositories for other layouts. No untested PR will be created.")


def excluded_path(name):
    parts = PurePosixPath(name).parts
    return (not parts or PurePosixPath(name).is_absolute() or ".." in parts or "\\" in name
            or any(part in {".git", ".ssh", ".aws", ".npmrc", ".pypirc", ".dockerignore", CONTEXT_DIR} or part == ".env" or part.startswith(".env.") for part in parts)
            or name.endswith((".pem", ".key")))


def build_context(snapshot, destination, dockerfile, *, npmrc=""):
    # Filter tracked secret/config files too; Git tracking alone is not sufficient.
    # Gzip also avoids Docker mistaking a leading Git PAX header for a text Dockerfile.
    with tarfile.open(snapshot, "r") as source, tarfile.open(destination, "w:gz") as target:
        for item in source:
            if excluded_path(item.name):
                continue
            if item.issym() or item.islnk():
                link = PurePosixPath(item.name).parent / item.linkname if item.issym() else PurePosixPath(item.linkname)
                if link.is_absolute() or ".." in link.parts or excluded_path(str(link)):
                    raise IntegrationError("Automatic validation cannot package an external or protected symlink. Configure a custom test image in Repositories.")
            if item.isfile() or item.isdir() or item.issym() or item.islnk():
                target.addfile(item, source.extractfile(item) if item.isfile() else None)
        generated = [(f"{CONTEXT_DIR}/Dockerfile", dockerfile), (".dockerignore", "")]
        if npmrc:
            generated.append((".npmrc", npmrc))
        for name, content in generated:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            target.addfile(info, io.BytesIO(data))


def transient_builder_error(error):
    """Retry transport interruptions, never dependency, permission or recipe errors."""
    message = str(error).lower()
    if "permission denied" in message or "access is denied" in message:
        return False
    return any(marker in message for marker in (
        "error reading from server: eof", "rpc error: code = unavailable",
        "failed to list workers: unavailable", "http2: client connection lost",
        "transport is closing", "connection reset by peer",
        "cannot connect to the docker daemon", "failed to connect to the docker api",
        "error during connect:",
    ))


def wait_for_builder(deadline):
    # Docker Desktop can answer `docker info` from a cache while Resource Saver
    # is stopping/waking its VM. Check the actual builder immediately before use.
    ready_deadline = min(deadline, time.monotonic() + 60)
    while True:
        remaining = ready_deadline - time.monotonic()
        if remaining < 1:
            raise IntegrationError("Docker's build engine did not become ready within the setup time limit. Check Docker Desktop's engine and Resource Saver status.")
        try:
            execution.command(["docker", "buildx", "inspect", "--bootstrap"], timeout=max(1, min(15, int(remaining))), operation="Docker builder readiness check")
            return
        except IntegrationError as error:
            if not transient_builder_error(error) and "timed out" not in str(error):
                raise
            time.sleep(min(2, max(0, ready_deadline - time.monotonic())))


def build_test_image(image, context, *, on_progress=None):
    deadline = time.monotonic() + settings.BUGPILOT_VALIDATION_SETUP_TIMEOUT
    wait_for_builder(deadline)
    for attempt in range(1, 4):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise IntegrationError("Automatic test environment preparation exceeded its setup time limit while waiting for Docker.")
        try:
            # command() reopens the archive from byte zero on every attempt.
            execution.command(["docker", "build", "--file", f"{CONTEXT_DIR}/Dockerfile", "--tag", image, "-"],
                              stdin=context, timeout=max(1, int(remaining)), operation="Automatic test environment preparation")
            return
        except IntegrationError as error:
            if not transient_builder_error(error):
                raise
            if attempt == 3:
                raise IntegrationError(f"Docker's build engine disconnected on all 3 attempts. Check Docker Desktop's engine and Resource Saver status. {error}") from None
            if on_progress:
                on_progress(f"Docker's build connection was interrupted, which can happen while Resource Saver stops or wakes the engine. Waiting for the builder and retrying test setup ({attempt + 1}/3); cached layers will be reused.")
            time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
            wait_for_builder(deadline)


def verify_python_collection(image, *, workspace=None, on_progress=None):
    """Check the clean image snapshot in the same sandbox used for validation."""
    try:
        execution.run_container_check(image, ["python", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            name=f"bugpilot-python-collection-{uuid.uuid4().hex}", label="Python test collection", on_progress=on_progress)
    except IntegrationError as error:
        check = getattr(error, "check_result", {})
        if check.get("exit_code") == 5 and check.get("phase") == "tests" and not (check.get("timed_out") or check.get("oom_killed")):
            if on_progress:
                on_progress("The base checkout has no tests to collect. The proposed fix must add a passing test before publication.")
            return
        missing = sorted(set(re.findall(r"ModuleNotFoundError: No module named ['\"]([A-Za-z_][\w.]*)['\"]", check.get("output", ""))))
        if workspace is not None:
            missing = [name for name in missing if not any(
                (directory / name.split(".")[0]).is_dir() or (directory / (name.split(".")[0] + ".py")).is_file()
                for directory in (workspace, workspace / "src"))]
        if (not missing and check.get("phase") == "tests" and check.get("exit_code") == 2
                and not (check.get("timed_out") or check.get("oom_killed"))
                and re.search(r"\berrors? during collection\b", check.get("output", ""))):
            if on_progress:
                on_progress("The base checkout has test collection errors without an identified missing dependency. "
                            "The proposed fix must resolve them before publication.\n" + check["output"][-1200:])
            return
        hint = (" Missing imports: " + ", ".join(missing) + ". Declare their packages in the repository's test dependencies,"
                " or configure a prepared test image in Repositories." if missing else " Check the repository's test setup and configuration.")
        raise IntegrationError(f"Python test collection failed during environment preparation.{hint}\n{error}") from None
    if on_progress:
        on_progress("Python tests collect successfully in the offline environment.")


def prepare_automatic_validation(repository, workspace, *, on_progress=None):
    """Publish configuration only after an image is actually ready locally."""
    plan = detect_validation(workspace)
    head = execution.git(["rev-parse", "HEAD"], cwd=workspace)
    fingerprint = hashlib.sha256((repository.url + head + plan.dockerfile + plan.npmrc).encode()).hexdigest()[:24]
    image = f"{AUTO_IMAGE_PREFIX}{repository.pk}:{fingerprint}"
    if on_progress:
        on_progress(f"Detected {plan.label}. Preparing its test dependencies in Docker before the coding step; the first build may download packages.")
    try:
        available = execution.command(["docker", "image", "inspect", "--format", "{{.Os}}", image], timeout=15)
    except IntegrationError:
        available = ""
    if available != "linux":
        root = Path(settings.BUGPILOT_WORKSPACE_ROOT) / ".validation-builds"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            snapshot = Path(temporary) / "snapshot.tar"
            context = Path(temporary) / "context.tar.gz"
            execution.git(["archive", "--format=tar", "--output", str(snapshot.resolve()), "HEAD"], cwd=workspace)
            build_context(snapshot, context, plan.dockerfile, npmrc=plan.npmrc)
            try:
                build_test_image(image, context, on_progress=on_progress)
            except IntegrationError as error:
                # A large image can finish exporting as the client reaches its
                # deadline. Reuse it only if Docker confirms it is local/Linux,
                # just as for the normal cached-image path above.
                completed = False
                if "timed out" in str(error):
                    try:
                        completed = execution.command(["docker", "image", "inspect", "--format", "{{.Os}}", image], timeout=15) == "linux"
                    except IntegrationError:
                        pass
                if not completed:
                    raise IntegrationError(f"Automatic test environment preparation failed for {plan.label}. {error}") from None
                if on_progress:
                    on_progress("The build client reached its time limit, but Docker finished exporting the local Linux test image. Using the completed image.")
        # A successful CLI exit alone is insufficient (e.g. a remote-only builder).
        if execution.command(["docker", "image", "inspect", "--format", "{{.Os}}", image], timeout=15) != "linux":
            raise IntegrationError("The automatic test image was not loaded into the local Linux engine. Check the Docker builder configuration.")
    if plan.python_collection:
        verify_python_collection(image, workspace=workspace, on_progress=on_progress)
    repository.runtime_image = image
    repository.validation_commands = plan.commands
    repository.save(update_fields=["runtime_image", "validation_commands"])
    if on_progress:
        on_progress(f"Test environment ready. Regression command: {'; '.join(' '.join(check) for check in plan.commands)}. Checks will run offline after the fix.")
