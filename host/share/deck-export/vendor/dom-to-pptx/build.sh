#!/bin/sh
# Rebuilds dom-to-pptx.bundle.js.gz from upstream source plus Superchat's patches (see README.md).
#
#   build.sh [work-dir]      default: a temporary directory
#
# Needs git, node, pnpm and patch. The result replaces dom-to-pptx.bundle.js.gz next to this file.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=https://github.com/atharva9167j/dom-to-pptx.git
commit=c0447adc47d3bc734d017050834edf0941adc189 # tag v2.0.1
work=${1:-$(mktemp -d)}
mkdir -p "$work"
git clone --quiet "$repo" "$work/src"
cd "$work/src"
git checkout --quiet "$commit"
# 1. The native chart hook, the table row heights, the 'Chart' shape name.
git apply "$here/patches/dom-to-pptx-2.0.1-native-charts.patch"
pnpm install --frozen-lockfile --ignore-scripts
# 2. The bundled PptxGenJS 4.0.1 carries the same fixes as the host's (#1531, #1537, #1540,
#    zero values): the very patch install.sh applies there.
patch -p1 -f -s -d node_modules/pptxgenjs -i "$here/../../../pptx/runtime/patches/pptxgenjs-4.0.1.patch"
./node_modules/.bin/rollup -c >/dev/null
# fonteditor-core records the build machine's path in two variables; make the bundle reproducible.
sed -i.bak "s#'/[^']*/node_modules/[^']*/fonteditor-core/woff2'#'/Contributions/dom-to-pptx'#" dist/dom-to-pptx.bundle.js
grep -q "PptxGenJS #1540" dist/dom-to-pptx.bundle.js
grep -q "addNativeChart" dist/dom-to-pptx.bundle.js
gzip -n -c dist/dom-to-pptx.bundle.js >"$here/dom-to-pptx.bundle.js.gz"
echo "wrote $here/dom-to-pptx.bundle.js.gz"
