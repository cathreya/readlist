#!/bin/sh
# Build FlashcapCapture.app from main.swift (Command Line Tools are enough).
set -eu
cd "$(dirname "$0")"

APP="FlashcapCapture.app"

rm -rf build "$APP"
mkdir -p build "$APP/Contents/MacOS" build/ModuleCache build/tmp

# Keep the compiler's caches inside the project: sandboxed runs cannot write to
# the default /var/folders module cache.
export TMPDIR="$PWD/build/tmp"

swiftc -O -swift-version 5 \
  -module-cache-path "$PWD/build/ModuleCache" \
  -o build/FlashcapCapture main.swift \
  -framework AppKit -framework Carbon -framework ApplicationServices

cp build/FlashcapCapture "$APP/Contents/MacOS/"
cp Info.plist "$APP/Contents/"

# Ad-hoc signature: enough to run locally and to hold an Accessibility grant.
codesign --force --sign - "$APP" >/dev/null 2>&1 \
  || echo "warning: ad-hoc signing failed; macOS may refuse the Accessibility grant" >&2

echo "Built $APP"
