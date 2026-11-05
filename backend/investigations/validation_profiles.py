"""Repository-specific validation adapters; explicit custom checks take precedence."""
import json
import re

from .integrations import IntegrationError


def is_freecodecamp(workspace):
    from .validation_setup import read_file
    try:
        root = json.loads(read_file(workspace, "package.json") or "{}")
        curriculum = json.loads(read_file(workspace, "curriculum/package.json") or "{}")
    except ValueError:
        return False
    return (isinstance(root, dict) and root.get("name") == "@freecodecamp/freecodecamp"
            and isinstance(curriculum, dict) and curriculum.get("name") == "@freecodecamp/curriculum")


def node_runtime_additions(workspace):
    if not is_freecodecamp(workspace):
        return ""
    # Literal, public test defaults only. Never import sample.env or CI secrets.
    # Puppeteer's pinned browser is downloaded during setup, not offline tests.
    return (
        'ENV CURRICULUM_LOCALE=english CLIENT_LOCALE=english SHOW_UPCOMING_CHANGES=true '
        'PUPPETEER_CACHE_DIR=/opt/bugpilot-browsers TURBO_TELEMETRY_DISABLED=1\n'
        'RUN apt-get update && apt-get install -y --no-install-recommends '
        'fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 '
        'libdrm2 libgbm1 libgtk-3-0 libnss3 libx11-xcb1 libxcomposite1 libxdamage1 '
        'libxfixes3 libxrandr2 libxss1 libxtst6 && rm -rf /var/lib/apt/lists/*\n'
        'RUN ["pnpm", "--dir", "curriculum", "run", "install-puppeteer"]\n'
        'RUN ["pnpm", "exec", "turbo", "run", "setup", "--filter=@freecodecamp/curriculum", "--concurrency=1"]\n'
        # The installation store is not needed when executing installed modules.
        # Keep it out of the fresh /work volume, reducing per-check copy cost.
        'RUN rm -rf /work/.pnpm-store\n'
    )


def scoped_checks(repository, workspace, changed, added):
    """Scope a content-only patch; source/config/test edits retain full checks."""
    checks = repository.validation_commands
    if (not repository.runtime_image.startswith("bugpilot-auto-")
            or checks != [["pnpm", "test"]] or not is_freecodecamp(workspace)):
        return checks, ""
    blocks = set()
    for name in changed:
        match = re.fullmatch(r"curriculum/challenges/english/blocks/([a-z0-9-]+)/[a-f0-9]{24}\.md", name)
        if match:
            blocks.add(match[1])
        elif name in added and re.fullmatch(r"curriculum/src/[A-Za-z0-9_/-]+\.test\.[jt]sx?", name):
            continue
        else:
            return checks, ""
    if not blocks:
        return checks, ""
    if len(blocks) != 1:
        raise IntegrationError("This curriculum patch spans multiple blocks. Configure custom checks covering those blocks before publishing.")
    block = next(iter(blocks))
    return [["env", f"FCC_BLOCK={block}", "pnpm", "exec", "turbo", "run", "test",
             "--filter=@freecodecamp/curriculum...", "--concurrency=1", "--env-mode=loose", "--", "--maxWorkers=2"]], (
        f"freeCodeCamp English curriculum block '{block}', all curriculum tooling tests (including new regression tests), "
        "and curriculum dependency tests. Package tasks run sequentially; API/client integration suites are outside this content-only scope.")
