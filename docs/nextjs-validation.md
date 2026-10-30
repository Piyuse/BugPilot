# Next.js regression runtime

The Python sample image does not run Next.js tests. This runtime is built from
the registered repository's checkout and includes Node 26, its pinned pnpm,
workspace dependencies, compiled Next.js, and Chromium.

From the BugPilot project directory, prepare the image once:

```powershell
docker build --build-context bugpilot_runtime=./docs --file docs/nextjs-test-runtime.Dockerfile --tag bugpilot-nextjs-tests:prepared <path-to-nextjs-checkout>
```

The build runs the preview regression suite with network access to populate its
isolated-install cache. BugPilot runs it again offline after overlaying the
agent's current source and rebuilding affected packages. The wrapper rejects
an empty or skipped test selection.

Register the successfully built image with these validation arguments:

```json
[["bugpilot-nextjs-check", "test/e2e/prerender-preview/prerender-preview.test.ts"]]
```

For this larger monorepo, allow `BUGPILOT_TEST_TIMEOUT=900` and
`BUGPILOT_TEST_MEMORY=4g` in the worker's environment, subject to host capacity.
The writable workspace is a fresh anonymous Docker volume populated from the
image's prepared dependencies; the read-only source mount overlays the current
patch. Container root storage remains read-only and regression networking stays
disabled. The container and its anonymous volume are removed after the check.

Rebuild the image when the repository's dependency lockfile or native compiler
version changes. A bare `node` image without these dependencies is insufficient.
