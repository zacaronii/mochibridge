"""Runs the extension's tracker logic in headless Chrome (local binary, or the zenika/alpine-chrome image)."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "extension"
HOST_PREFIX = os.environ.get("KB_HOST_PREFIX", "")   # optional "local=host" path mapping for a remote Docker host


def run_chrome(page: Path) -> str:
    for binary in ("google-chrome", "chromium", "chromium-browser"):
        if shutil.which(binary):
            return subprocess.run([binary, "--headless=new", "--no-sandbox", "--allow-file-access-from-files",
                                   "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=60).stdout
    if shutil.which("docker") and subprocess.run(["docker", "image", "inspect", "zenika/alpine-chrome:latest"],
                                                 capture_output=True).returncode == 0:
        host_dir = str(EXT)
        if "=" in HOST_PREFIX:
            local, host = HOST_PREFIX.split("=", 1)
            host_dir = host_dir.replace(local, host, 1)
        return subprocess.run(["docker", "run", "--rm", "-v", f"{host_dir}:/ext:ro", "zenika/alpine-chrome:latest",
                               "--no-sandbox", "--allow-file-access-from-files", "--dump-dom",
                               "file:///ext/test/tracker_test.html"], capture_output=True, text=True, timeout=120).stdout
    pytest.skip("no headless Chrome available")


def test_tracker_counts_only_real_playback():
    out = run_chrome(EXT / "test" / "tracker_test.html")
    m = re.search(r"RESULT (\{.*\})", out)
    assert m, out[-500:]
    r = json.loads(m.group(1).replace("&quot;", '"'))
    assert r["after_play"] == 60                 # the first tick only starts the clock
    assert r["after_pause"] == 60                # paused: nothing
    assert r["after_ad"] == 60                   # ads: nothing
    assert r["after_buffer"] == 60               # buffering/seeking: nothing
    assert r["after_sleep"] == 62                # a 10-min gap counts at most 2 s
    assert r["flushed"] == [["AAAAAAAAAAA", 62]] and r["new_session"]
    assert r["flushed_after_leave"] == [["AAAAAAAAAAA", 62], ["BBBBBBBBBBB", 20]]
    assert r["title"] == "日本語の動画"


def test_manifest_and_build():
    manifest = json.loads((EXT / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    worlds = {cs.get("world", "ISOLATED") for cs in manifest["content_scripts"]}
    assert worlds == {"ISOLATED", "MAIN"}
    for cs in manifest["content_scripts"]:
        for js in cs["js"]:
            assert (EXT / js).exists(), js
