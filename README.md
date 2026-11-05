# BugPilot

Paste a **GitHub issue URL or endpoint**. BugPilot extracts the relevant issue context, preprocesses it with OpenAI, asks Codex to fix the affected repository, validates the patch, opens a PR on a new branch, and comments the PR link on the original GitHub issue.

The developer submits the issue and reviews/merges the PR. Repository access, provider credentials and test commands are configured once.

## Run locally

Requires Python 3.12+, Node.js 22.12+ (Node 24 recommended), and Git.

From the project root in PowerShell:

```powershell
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.venv/Scripts/python.exe manage.py migrate
.venv/Scripts/python.exe manage.py createsuperuser
.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8000
```

Reuse the existing virtual environment if present. On macOS/Linux use `.venv/bin/python`.

In a second terminal, from `backend`:

```powershell
.venv/Scripts/python.exe manage.py runworker
```

In a third terminal, from the project root:

```powershell
cd frontend
npm ci
npm run dev
```

Open [BugPilot](http://127.0.0.1:5173). The frontend lives in `frontend/` and uses strict TypeScript; Django serves APIs and the worker. Vite proxies `/api/` to port 8000. Set `BACKEND_URL` in `frontend/.env` if using another backend port.

Sign in with the account created by `createsuperuser`, then register your repositories and configure the backend credentials.

## Accepted GitHub issue inputs

| Input | Repository field |
| --- | --- |
| `https://github.com/owner/repo/issues/123` | Optional |
| `https://api.github.com/repos/owner/repo/issues/123` | Optional |
| `owner/repo/issues/123` | Optional |
| `issues/123` or `/issues/123` | Required |
| `123` or `#123` | Required |

Paste a registered repository URL or name in **Affected repository**, or click **Find repository automatically** to clear the hint. For automatic discovery, provide a fully qualified issue URL: issue numbers are scoped to a repository and cannot identify an issue on their own.

Without a manual hint, a registered repository hosting the issue is selected directly. If the issue is in a separate tracker repository, BugPilot matches the processed context against active registered repositories using their descriptions, README files and file paths. Ambiguous matches stop with a clear reason.

Browser and API URL variants share a canonical identity such as `owner/repo#123`. Issue 123 in a different repository is a separate investigation. Pull request URLs and PRs returned by the Issues endpoint are rejected.

## Configure live model calls

Copy `backend/.env.example` to `backend/.env`, or use actual environment variables (which take precedence):

```env
GITHUB_TOKEN=your-scoped-github-token
OPENAI_API_KEY=your-openai-api-key
OPENAI_PREPROCESS_MODEL=gpt-6-luna
# Optional separate coding key; otherwise OPENAI_API_KEY is used.
CODEX_API_KEY=
CODEX_MODEL=
```

The GitHub token needs **Issues, Contents and Pull Requests read/write** for the issue and code repositories. **Jira credentials are not needed for GitHub issue runs.** Keys belong only in the backend.

Install the [Codex CLI](https://developers.openai.com/codex/cli/) and Docker on the worker host. Confirm `git --version`, `codex --version`, and `docker info` work for its OS user. Set `CODEX_BIN` if the executable is not on PATH; on Windows use a directly executable `codex.exe`, rather than an npm `.cmd` shim.

Docker Desktop must be running with its Linux engine, not merely installed. BugPilot checks the engine before model calls. With automatic validation it prepares a dependency image after checkout and before coding; with custom checks it verifies the configured image locally before checkout. Regression checks always use `--pull never`. A missing `dockerDesktopLinuxEngine` pipe means the Linux engine is unavailable, not that the repository's tests failed. Start Docker Desktop (`docker desktop start`) and wait for `docker info` to report a running Linux server.

`OPENAI_PREPROCESS_MODEL` defaults to `gpt-6-luna`, a lower-cost model for structured preprocessing and repository selection. Choose a Responses API model with Structured Outputs available to your account. `OPENAI_API_KEY` and `CODEX_API_KEY` fall back to each other, so an existing coding API key also works for preprocessing. `CODEX_MODEL` is optional; leaving it blank uses the CLI default. Use API model IDs (for example, `gpt-6.1-sol`), not display labels. The integration uses `codex exec`, a disposable `CODEX_HOME`, structured output, and the `workspace-write` sandbox.

This application uses OpenAI API-key authentication. API usage is billed through your OpenAI Platform account; a paid ChatGPT subscription does not supply credits to this API-key path. Configure API access and billing on [OpenAI Platform](https://platform.openai.com/). Gemini configuration is no longer used. Historical investigations retain the provider recorded when they ran.

On native Windows, BugPilot explicitly selects `BUGPILOT_WINDOWS_SANDBOX=mxc` and checks sandboxed workspace read/write access before calling the coding model. It preserves `SystemDrive` and `LOCALAPPDATA`, required by the native sandbox runtime, while excluding service keys from the check. Use a compatible Codex CLI and Windows host. Administrators can explicitly choose a provisioned `elevated` sandbox or a permitted `unelevated` fallback; BugPilot never disables the sandbox or silently switches implementations. See [Windows sandbox configuration](https://learn.chatgpt.com/docs/windows/windows-sandbox).

Agent output and sandbox state are isolated per investigation attempt in `workspaces/<investigation>/agent-attempt-<attempt>/`. The bounded `agent-output.log` redacts configured service keys. Empty patches remain blocked; errors include the agent's explanation so permission failures are visible instead of looking like successful fixes.

Restart the API and worker after changing configuration. The form shows missing settings before an investigation can start. Configuration presence does not verify provider permissions or model access. Provider requests remain untested against your accounts until credentials are supplied.

Optional settings: `BUGPILOT_WORKSPACE_ROOT` (default project-root `workspaces/`), `BUGPILOT_AGENT_TIMEOUT` (1200 seconds), `BUGPILOT_CONTAINER_SETUP_TIMEOUT` (900 seconds/check for Docker container and volume initialization), `BUGPILOT_TEST_TIMEOUT` (300 seconds/check after container creation), `BUGPILOT_GIT_NETWORK_TIMEOUT` (600 seconds per clone/fetch/push/remote lookup), `BUGPILOT_LEASE_SECONDS` (1800 seconds).

Workspace preparation downloads only the default branch's latest commit (`--depth 1 --no-tags --single-branch`) rather than its full history. Recovery fetches are shallow too. The timeline distinguishes downloading, remote branch lookup, recovery and local branch creation. Local Git operations retain a 120-second timeout. If a slow connection still times out, increase `BUGPILOT_GIT_NETWORK_TIMEOUT` in `backend/.env` and restart the worker. Retries before a saved validation/publication checkpoint create a new attempt directory and leave the failed checkout untouched; later retries revalidate the saved checkout.

BugPilot enables `core.longpaths=true` on its Git commands and saves it in each clone's local configuration. This lets Git for Windows check out deeply nested fixtures in repositories such as Next.js without changing system or global Git settings.

## Register repositories once

Admins add/edit repositories through the dashboard; other signed-in developers can submit issues and view runs. Registry entries specify:

- A unique name and HTTPS GitHub URL. BugPilot detects the default branch from GitHub automatically.
- An ownership description for repository discovery.
- Optional custom Docker image and validation commands as JSON argument arrays. Leave commands empty (`[]`) for automatic setup.

Automatic validation detects root Node.js `package.json` test scripts (npm, pnpm or Yarn) and Python projects (pytest). BugPilot builds a Linux dependency image before calling the coding agent, then runs the detected tests offline against the proposed fix. It respects Node lockfiles and pinned package-manager versions, installs Python requirement files and project test extras, and recognizes literal pip dependencies in CI for mixed Node/Python tests. Image preparation can access package registries; no BugPilot API credentials or untracked files enter the build context. Secret files are excluded from tracked source too. Images are cached by repository, Git revision and build recipe, and refreshed when that revision changes. `BUGPILOT_VALIDATION_SETUP_TIMEOUT` controls preparation (default 1200 seconds).

If no supported test setup exists, detection stops with an actionable error; tests are never silently skipped. Other languages, nested project layouts, private dependency registries, browser/native toolchains and complex monorepos may need a custom prepared image and checks. Existing custom configurations remain in use.

For pnpm repositories, the image preserves allowlisted `.npmrc` settings for workspace linking and dependency layout. Registry credentials and other `.npmrc` settings are excluded. Changes to these layout settings invalidate the cached image; package preparation scripts and regression tests remain enabled.

Python projects with `uv.lock`, uv settings, dependency groups, or project metadata without a package build system use uv to prepare their dependencies. Existing uv locks are installed as committed with `--frozen`, without re-resolving or rewriting them; declared test/development groups and test extras are included, and a declared `cpu` extra is selected for the Linux CPU test environment. Dependency-only projects are not forced through `pip install -e .`. Projects declaring a build system or legacy setuptools packaging retain package installation. Prepared uv environments live outside the copied source tree, and regression checks invoke Python/pytest directly without syncing or downloading dependencies.

Automatic Python setup also reads conventional test requirements files (for example `tests/requirements.txt` and `requirements/test.txt`). For projects without a uv lock, a direct PyTorch import in a test can trigger installation of a pinned CPU wheel from the official PyTorch index if the prepared interpreter still lacks it. Declared and already installed dependencies retain precedence; BugPilot does not install arbitrary packages based on import names. Before starting Codex, automatic setup runs pytest collection against the clean checkout in an isolated container with no network access. Missing external imports stop preparation, including for cached images. Existing source collection errors are reported as baseline evidence so the agent can fix them. An empty base suite may proceed so Codex can add the first test; the final regression run still has to pass.

The freeCodeCamp adapter prepares public English locale defaults, Puppeteer's pinned headless browser and Linux libraries, and curriculum build dependencies. It does not import `sample.env` or service credentials. For a patch limited to one English curriculum block plus newly added curriculum regression tests, validation runs that block, all curriculum tooling tests, and curriculum dependency tests. Package tasks run sequentially with two Vitest workers to avoid competing builds causing short test deadlines to expire. The timeline and PR description disclose the scope. Source/configuration edits, existing test edits, and explicit custom checks retain their configured coverage; multiple-block content patches require custom checks. This focused content validation does not claim to run the MongoDB-backed API or client integration suites. Saved-patch retries refresh automatic environments so cached recipes can pick up environment fixes without another model run.

The timeline reports dependency setup as **Prepare test environment**, separately from creating the fix branch. Docker Desktop Resource Saver can stop/wake the engine between a successful `docker info` check and a build. BugPilot checks builder readiness immediately before building and retries recognized transport disconnections up to three times, reusing cached layers within one setup timeout. Dependency-install, Dockerfile, permission and test failures are not automatically replayed. It does not restart Docker Desktop or change Resource Saver settings.

Example custom configuration:

```json
{
  "name": "checkout-service",
  "url": "https://github.com/your-team/checkout-service",
  "description": "Owns checkout totals, promotion expiration and payments.",
  "runtime_image": "your-registry/checkout-tests:2026-10",
  "validation_commands": [["python", "-m", "pytest", "-q"]]
}
```

Build/pull custom images beforehand. Checks have no network, a read-only root filesystem, dropped capabilities and resource limits. Each command executes in a fresh writable copy of the checkout. Commands cannot share filesystem changes; combine setup steps in a repository test script. Preinstall dependencies in the image. See `docs/test-runtime.Dockerfile` for Python or `docs/nextjs-validation.md` for the prepared Next.js runtime. Each check uses a fresh Docker-managed workspace volume so image-provided Node dependencies remain available; the read-only source mount overlays the proposed patch. Set `BUGPILOT_TEST_MEMORY` for larger repositories (default `2g`).

Docker container creation and volume initialization have their own time budget, so large dependency images do not consume the regression execution budget before tests start. Source transfers use a single Git archive of the staged patch rather than copying thousands of files individually through a Windows bind mount; custom images need `sh` and `tar`. The timeline records setup time and the test command. Bounded, redacted logs are retained under `workspaces/<investigation>/validation-attempt-<attempt>/`; failed checks retain output, exit status, phase, and timeout/memory failure details in the investigation. After a validation failure, Retry rechecks the saved patch without repeating preprocessing or coding model calls. The saved workspace must remain available. Selecting another repository clears this checkpoint and starts a new checkout. Publication still requires every configured check to pass.

The registry's `default_branch` is a read-only cache, not a branch override. Each live investigation resolves GitHub's current default branch and records it as `base_branch`. Checkout, the coding brief and the pull request target use that same recorded branch across retries. If a retry selects another repository, BugPilot resolves its default branch afresh. Repository discovery also reads the default branch from GitHub; it never guesses `main`.

Other examples: `["npm", "test", "--", "--runInBand"]`, `["go", "test", "./..."]`, `["cargo", "test", "--offline"]`, with their offline dependencies preinstalled.

## Live pipeline

1. Read the GitHub issue and retain only its title, body, labels and canonical URL. Profiles, assignees, reactions and timelines are not retained or sent to OpenAI.
2. Call OpenAI with bounded input and an enforced JSON schema to extract symptoms, expected behavior, reproduction steps, exact errors and search keywords.
3. Select the supplied registered repository, infer it from the issue URL, or discover it from the registry.
4. Clone into a per-attempt checkout and create `codex/gh-<issue-number>-<run-id>`. When no custom checks exist, detect the test setup and prepare a dependency image before coding.
5. Give Codex the validated OpenAI brief and repository metadata. Raw issue text is not sent again. Ask it to investigate, implement a minimal fix and add regression coverage.
6. Reject empty, protected-file, binary or oversized patches, then execute every configured check in Docker.
7. Commit/push the branch and create a PR with `Fixes owner/repo#123`, the root cause and validation commands. If the token cannot push upstream, create or reuse a verified fork owned by its GitHub account, push there, and open the PR against the original repository using `owner:branch` as the head. The base remains the investigation's recorded default branch.
8. Post the clickable PR URL, branch, summary and validation result as a Markdown comment on the source issue. Save its ID and URL.

No code path merges a PR. The review drawer shows completed model stages, the processed text passed to Codex, validation output and the issue-comment link.

## Reliability and operating boundaries

- Submission persists a queue record and returns HTTP 202. A separate `runworker` process does the work.
- Source/issue/mode uniqueness deduplicates submissions; browser and API URLs resolve to the same issue.
- Conditional claims and retries prevent duplicate work. Heartbeats and events persist; expired jobs become Needs attention.
- Retries recover an existing PR by head owner and branch, including fork PRs. Publication checkpoints retain the original checkout attempt; retries revalidate that existing fix and resume publishing without repeating either model. A source-tree fingerprint prevents publishing edits made after validation. A comment-only retry does not repeat the models. A stable HTML marker recovers comments accepted before a response was lost; histories are paginated.
- Fork publishing requires the token to create a repository in its account and write Contents in the resulting fork, plus permission to open the upstream PR and comment on its issue. Fine-grained tokens may require adding a newly created fork to their repository selection. A same-name repository outside the upstream fork network is never used. Fork creation waits for GitHub to initialize the default ref; an ambiguous creation response is checked before another mutation.
- For contributing to a public repository outside your membership, GitHub currently documents a fine-grained token limitation. A classic PAT with `public_repo` supports this public contribution workflow; see [GitHub's token limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations). Keep it in `backend/.env` and restart the worker after replacing it. The application never asks for tokens in the frontend.
- OpenAI preprocessing and repository selection retry temporary HTTP 429/500/502/503/504 and connection failures up to five times (six total attempts), with exponential backoff and jitter. Retry progress appears in the investigation timeline. `Retry-After` is honored for waits up to 60 seconds; longer waits stop the run for a later retry. Persistent 503 errors report provider unavailability separately from credential and quota errors. Use the investigation's Retry action after an outage; changing API keys does not resolve a provider outage.
- Pushes never force-update a branch. Subprocess timeouts terminate process trees and clean up test containers. Output is bounded and configured secret values are redacted.
- Issue hosts are restricted to `github.com` and `api.github.com`; API calls always use the fixed GitHub API host.
- Session authentication and CSRF protect the application. One trusted engineering team shares issue context and logs; this is not a multi-tenant service.
- Passing checks demonstrates that the configured checks passed, not that a generated fix is guaranteed correct. PR review remains required.

Use a dedicated disposable worker host without production credentials or unrelated private files. Codex has command network access disabled, and spawned-command environment rules exclude secret/key/token variables. Validation containers receive no application credentials and must not receive the host Docker socket.

SQLite targets a small team on one host; start with one worker. Multi-host scaling, SSO, tenant isolation, distributed throttling and automatic checkout-retention cleanup are not included. Monitor `workspaces/` disk usage and back up the database.

For deployment, set `DEBUG=false`, a strong `DJANGO_SECRET_KEY`, explicit allowed hosts and HTTPS CSRF origins. Build and serve `frontend/dist/`, proxy `/api/` to a WSGI server preserving Host, and serve Django admin static files from `collectstatic`. See `docs/nginx.conf.example`. Trust forwarded headers only from your own proxy.

## API

All application endpoints require a Django session. Health/session bootstrap are public. Mutations, including login/logout, require a CSRF token obtained through `GET /api/auth/session/`.

- `POST /api/investigations/`: submit `{"github_issue":"https://github.com/owner/repo/issues/123","repository":"","mode":"live"}`.
- `GET /api/investigations/`: history, with `?status=running&q=checkout&page=1` filters.
- `GET /api/investigations/<uuid>/`: context, model stages, validation, events and comment URL.
- `POST /api/investigations/<uuid>/retry/`: retry a failed run; optional repository hint.
- `GET/POST /api/repositories/`, `GET/PATCH /api/repositories/<id>/`: registry (writes require staff).
- `GET /api/overview/`: counts, worker heartbeat and model configuration.
- `GET /api/auth/session/`, `POST /api/auth/login/`, `POST /api/auth/logout/`: session authentication.
- `GET /api/health/`: liveness.
- The original `/api/bug-reports/` storage endpoints remain available.

The new UI uses GitHub Issues. Historical Jira records and comment checkpoints are preserved by a data-preserving migration; the legacy `jira_ticket` API adapter remains for those clients. Only legacy Jira runs require `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN`.

## Verification

```powershell
cd backend
.venv/Scripts/python.exe manage.py test
.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
cd ../frontend
npm ci
npm run build
```

Tests exercise URL normalization, short endpoints, repository context, duplicate prevention, source migration/data preservation, minimal issue extraction, model handoff, validation gates, comment recovery, legacy history and authentication. Git patch tests use real temporary repositories. Provider and agent boundaries are mocked; paid live execution needs your credentials.

## Provider references

- [GitHub Issues REST API](https://docs.github.com/en/rest/issues/issues)
- [GitHub issue comments API](https://docs.github.com/en/rest/issues/comments)
- [Linking a PR to an issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)
- [Codex non-interactive execution](https://developers.openai.com/codex/noninteractive)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
