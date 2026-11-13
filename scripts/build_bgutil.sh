#!/usr/bin/env bash
# Rebuilds chcompanion/vendor/bgutil/ from a pinned upstream tag of
# https://github.com/Brainicism/bgutil-ytdlp-pot-provider (GPL-3.0).
#   scripts/build_bgutil.sh [tag]      needs git, node >= 22 and npm
# Output: bgutil-server.js (one ES module, runs with `deno run`, no node_modules), LICENSE, VERSION.
set -euo pipefail
TAG="${1:-2.0.2}"
ESBUILD="0.25.10"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../chcompanion/vendor/bgutil"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --quiet --depth 1 --branch "$TAG" https://github.com/Brainicism/bgutil-ytdlp-pot-provider "$WORK/src"
COMMIT="$(git -C "$WORK/src" rev-parse HEAD)"
cd "$WORK/src/server"
# Lockfile install, scripts off: canvas (native, optional for jsdom) is never built.
npm ci --ignore-scripts --omit=dev --no-audit --no-fund
npm install --no-save --no-audit --no-fund "esbuild@$ESBUILD"
cp "$HERE/build_bgutil.mjs" .
mkdir -p "$OUT"
node build_bgutil.mjs . "$OUT/bgutil-server.js"
cp ../LICENSE "$OUT/LICENSE"
echo "$TAG" > "$OUT/VERSION"
echo "built $TAG ($COMMIT): $(wc -c < "$OUT/bgutil-server.js") bytes"
