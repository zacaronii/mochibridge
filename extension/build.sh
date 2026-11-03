#!/usr/bin/env bash
# Builds extension zips: dist/mochibridge-chrome.zip and dist/mochibridge-firefox.zip
set -euo pipefail
cd "$(dirname "$0")"
rm -rf dist && mkdir -p dist/chrome dist/firefox
for d in chrome firefox; do cp -r src icons dist/$d/; done
cp manifest.json dist/chrome/manifest.json
python3 - <<'PY'
import json
m = json.load(open("manifest.json"))
m.pop("key")                                                   # Chrome-only: pins the unpacked extension ID
m["background"] = {"scripts": ["src/background.js"]}          # Firefox MV3 uses an event page
m["browser_specific_settings"] = {"gecko": {"id": "mochibridge@zacaronii", "strict_min_version": "128.0"}}
json.dump(m, open("dist/firefox/manifest.json", "w"), indent=2)
PY
for d in chrome firefox; do
  python3 -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2])" "dist/mochibridge-$d" "dist/$d"
done
echo "built: $(ls dist/*.zip | tr '\n' ' ')"
