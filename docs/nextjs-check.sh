#!/bin/sh
set -eu
cd /work
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$TMPDIR"
# Compile the current overlaid source, not just the image's baseline source.
pnpm build --filter=next
report="/work/.bugpilot-jest-results.json"
rm -f "$report"
pnpm test-start "$@" --json --outputFile "$report"
# Jest must actually execute tests; an empty/skipped selection is not validation.
node - "$report" <<'JS'
const fs = require('node:fs')
const result = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
if (result.success !== true || !Number.isInteger(result.numPassedTests) || result.numPassedTests < 1 || result.numFailedTests !== 0) {
  throw new Error('Next.js regression validation did not execute passing tests')
}
console.log(`BugPilot verified ${result.numPassedTests} passing Next.js tests`)
JS
