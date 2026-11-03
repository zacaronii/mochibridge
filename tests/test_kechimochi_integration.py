"""Runs against the throwaway instance: KB_TEST_KECHIMOCHI=$(scripts/kechimochi-test.sh up) pytest -m integration"""
import json
import os
import uuid

import pytest

from kbridge.kechimochi import Conflict, Kechimochi, KechimochiError
from images import WEBP, png

pytestmark = pytest.mark.integration
BASE = os.environ.get("KB_TEST_KECHIMOCHI", "")
if "kechimochi:3000" in BASE.replace("kechimochi-test", ""):
    raise RuntimeError("refusing to run integration tests against the real kechimochi")


@pytest.fixture
async def km():
    if not BASE:
        pytest.skip("KB_TEST_KECHIMOCHI not set")
    k = Kechimochi(BASE)
    yield k
    await k.aclose()


def media(title):
    return {"title": title, "variant": "", "default_activity_type": "Listening", "status": "Active",
            "language": "Japanese", "description": "", "cover_image": "",
            "extra_data": json.dumps({"Library": "ABS · テスト"}), "content_type": "Audio",
            "tracking_status": "Ongoing"}


async def test_version(km):
    assert (await km.version()).startswith("web-")


async def test_media_create_conflict_update(km):
    title = f"テスト {uuid.uuid4().hex[:6]}"
    mid = await km.create_media(media(title))
    with pytest.raises(Conflict):
        await km.create_media(media(title))
    assert (await km.find_media(title))["id"] == mid
    before = await km.get_media(mid)
    changed = await km.update_media(mid, lambda m: {**m, "description": "desc"})
    after = await km.get_media(mid)
    assert changed and after["description"] == "desc" and after["uid"] == before["uid"]
    assert await km.update_media(mid, lambda m: m) is None          # no-op -> no PUT


async def test_logs(km):
    mid = await km.create_media(media(f"ログ {uuid.uuid4().hex[:6]}"))
    log = {"media_id": mid, "duration_minutes": 12, "characters": 0, "date": "2026-11-01",
           "date_precision": "day", "activity_type": "Listening", "notes": "#65-4 · タイトル"}
    with pytest.raises(KechimochiError) as e:
        await km.create_log({**log, "duration_minutes": 0})
    assert e.value.status == 400 and not e.value.transient
    lid = await km.create_log(log)
    await km.put_log(lid, {**log, "duration_minutes": 20})
    logs = await km.logs_for_media(mid)
    assert [(l["id"], l["duration_minutes"], l["notes"]) for l in logs] == [(lid, 20, "#65-4 · タイトル")]
    await km.delete_log(lid)
    with pytest.raises(KechimochiError) as e:
        await km.put_log(lid, log)
    assert e.value.not_found


async def test_cover_png_and_webp(km):
    mid = await km.create_media(media(f"カバー {uuid.uuid4().hex[:6]}"))
    path = await km.upload_cover(mid, png(), "c.png", "image/png")
    assert path.endswith(".png") and (await km.get_media(mid))["cover_image"] == path
    path = await km.upload_cover(mid, WEBP, "c.webp", "image/webp")
    assert path.endswith(".webp"), path


async def test_unreachable_is_transient():
    k = Kechimochi("http://127.0.0.1:9")
    with pytest.raises(KechimochiError) as e:
        await k.version()
    assert e.value.transient
    await k.aclose()
