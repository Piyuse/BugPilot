# syntax=docker/dockerfile:1
# Build context: the registered Next.js checkout, not the BugPilot backend.
# Additional named context: --build-context bugpilot_runtime=./docs
FROM node:26-bookworm
ENV CI=true NEXT_TELEMETRY_DISABLED=1 TURBO_TELEMETRY_DISABLED=1 \
    HOME=/work/.home XDG_CACHE_HOME=/work/.cache \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    NEXT_TEST_PREFER_OFFLINE=1 TMPDIR=/work/.tmp
WORKDIR /work
RUN mkdir -p /work/.home /work/.cache /work/.tmp \
    && npm install --global pnpm@12.9.1 \
    && pnpm config set --global store-dir /work/.pnpm-store
COPY . /work/
RUN pnpm install --frozen-lockfile --ignore-scripts
RUN NEXT_SKIP_NATIVE_POSTINSTALL=0 node scripts/install-native.mjs
RUN pnpm exec playwright install --with-deps chromium
RUN pnpm build --filter=next
COPY --from=bugpilot_runtime nextjs-check.sh /usr/local/bin/bugpilot-nextjs-check
RUN chmod 755 /usr/local/bin/bugpilot-nextjs-check
# Warm the fixture's isolated-install cache during preparation, with networking.
# The worker repeats the checks later with --network none.
RUN bugpilot-nextjs-check test/e2e/prerender-preview/prerender-preview.test.ts
WORKDIR /work
