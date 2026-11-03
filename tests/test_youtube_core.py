import json
from datetime import datetime, timezone

from fakes import FakeKechimochi, entry
from kbridge import config
from kbridge.models import SeriesMeta
from kbridge.service import PollStats
from test_resolver_sync import bridge, polled

EXISTING_YT = {"youtube": {"channel_id": "UCw5X6y7Z8a9B0c1D2e3F4gH", "channel_url": "https://www.youtube.com/@example_ponpoko"}}


def channel_meta(channel_id="UCw5X6y7Z8a9B0c1D2e3F4gH", name="ぽんぽこ"):
    return SeriesMeta(source="youtube", series_key=f"channel:{channel_id}", title=name, library_id="youtube",
                      library_name="YouTube", content_type="Youtube Video", activity_type="Watching", language="ja",
                      fields={"youtube": {"channel_id": channel_id, "channel_url": f"https://www.youtube.com/channel/{channel_id}"},
                              "Library": "YouTube"},
                      match_ids={"youtube.channel_id": channel_id})


def test_youtube_config_defaults():
    c = config.parse({"sources": {"youtube": {"enabled": True, "languages": ["ja"]}}}, {})
    yt = c.source("youtube")
    assert yt.enabled and yt.unknown_language == "review" and yt.default_track
    assert yt.content_type == "Youtube Video" and yt.activity_type == "Watching"
    assert c.web_api_token is None
    assert config.parse({"web": {"api_token": "abc"}}, {}).web_api_token == "abc"


async def test_links_existing_channel_entry_by_channel_id():
    km = FakeKechimochi([entry(27, "ぽんぽこ", extra=EXISTING_YT, content_type="Youtube Video"),
                         entry(28, "ぽんぽこ", variant="other", content_type="Youtube Video")])   # same title, no ID
    b = bridge(km)
    assert await b.resolver.ensure(channel_meta()) == "linked"
    row = b.db.one("SELECT media_id, link_method FROM series_map")
    assert (row["media_id"], row["link_method"]) == (27, "id")


async def test_new_channel_entry_keeps_youtube_object_shape():
    km = FakeKechimochi()
    b = bridge(km)
    m = channel_meta("UCnew", "新しいチャンネル")
    p = polled(m, session="yt1")
    p.event.source = "youtube"
    await b.ingest(p, PollStats("youtube", datetime.now(timezone.utc)))
    await b.sync()
    (media,) = km.media.values()
    data = json.loads(media["extra_data"])
    assert data["youtube"] == {"channel_id": "UCnew", "channel_url": "https://www.youtube.com/channel/UCnew"}
    assert media["content_type"] == "Youtube Video" and data["Sync ID"].startswith("yt:")
