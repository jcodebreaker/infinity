#!/bin/sh
# Installs the Node libraries the pptx-generator skill needs (package.json + package-lock.json)
# and applies the pinned PptxGenJS fixes in patches/. Run by the host image build (Dockerfile
# stage pptx-runtime-builder) and by the pipeline test, so both use the same bytes.
#
#   install.sh [target-dir]      default: this directory (node_modules lands next to it)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
target=${1:-$here}
if [ "$target" != "$here" ]; then
  mkdir -p "$target"
  cp "$here/package.json" "$here/package-lock.json" "$target/"
fi
cd "$target"
# --ignore-scripts: sharp ships prebuilt binaries as optional packages; nothing needs a build step.
npm ci --ignore-scripts --no-audit --no-fund
pkg=node_modules/pptxgenjs
patch_file="$here/patches/pptxgenjs-4.0.1.patch"
# Idempotent: the patch leaves a marker comment in both bundles.
if grep -q "PptxGenJS #1540" "$pkg/dist/pptxgen.cjs.js"; then
  echo "pptxgenjs already patched"
else
  patch -p1 -f -s -d "$pkg" -i "$patch_file"
  echo "pptxgenjs patched (#1531, #1537, #1540, zero cells)"
fi
