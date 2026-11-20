# <img src="art/keeper.svg" alt="" width="44" height="44" align="absmiddle"> mochibridge

Logs your immersion activity from **Jellyfin**, **Kavita**, **Audiobookshelf**, **YouTube** and **your own apps** into
[kechimochi](https://github.com/Morgawr/kechimochi) (the web build, tested against web-0.3.3) through its HTTP API.
It creates one media entry per series and one activity log per viewing, reading or listening session, with covers and metadata.

It runs as a single container with a small web UI for reviewing, mapping and settings. A browser extension and
an iOS Shortcut feed it YouTube viewing.

## What it logs

| Source | Where the time comes from | Entry |
|---|---|---|
| **Jellyfin** | `/Sessions` sampled every 15 s. Time counts only while the playback position moves *and* the audio track being played is in an allowed language. Resumed fragments are joined into one play. The Playback Reporting plugin is a backstop for plays the sampler missed | One per series (or movie), with AniList IDs from [anibridge](https://github.com/anibridge/anibridge)'s mapping DB when available |
| **Kavita** | Reading history: one log per reading session and chapter. Sessions Kavita invents from bulk "mark as read" are ignored by default | One per series |
| **Audiobookshelf** | Your listening sessions | One per podcast or book |
| **YouTube** | See [YouTube](#youtube) below | One per channel |
| **Your own apps** | Each app posts its sessions to the [Push API](#push-api) as they grow: active time, and characters if it counts them | One per stable ID the app sends (e.g. `romm.rom_id`), else per title |

Each session passes through the same steps:

```
source adapter ──▶ filters ──▶ resolver ──▶ desired state (SQLite) ──▶ sync worker ──▶ kechimochi
                   source on?   series → entry   series_map / logs      POST/PUT/DELETE diffs only
                   library on?  (override, ID,                          (dry_run: recorded, not sent)
                   language ok?  title match,
                   long enough?  or create)
```

- **Tracking rules:** the source is enabled → the library is tracked → the language is allowed → the session is long enough.
  Each rule can instead hold the item for **review**. Every skipped session is recorded with its reason,
  and you can still log it from the web UI.
- **Matching existing entries:** before creating an entry, the bridge looks for one you already have. It checks a manual
  override, then stored IDs (e.g. `youtube.channel_id` in extra_data), then entries made by Nagare, then a title match
  (AniList native, romaji and English titles help). Content type breaks ties.
- **Never clobbers your edits:** existing entries only get *empty* fields filled in, and extra_data keys the bridge didn't
  write (or that you changed since) are left alone. A log you delete in kechimochi is not recreated.
- **Dry run first:** `mode: dry_run` records every operation it *would* send. Nothing is written to kechimochi
  until you switch to `mode: live`.

## Web UI

| Page | What it's for |
|---|---|
| Status | Health of each source, minutes today and over the last 7 days, what Jellyfin is playing now, recent logs, the event feed, YouTube quick log |
| Review | Sessions held by a `review` rule: approve (log) or reject, one at a time or in bulk |
| Activity | Every log and its sync state; skipped sessions with the reason, and **Log anyway** |
| Mappings | Which kechimochi entry each series logs to: link it to another entry (its logs move), create a new one, or ignore the series |
| Settings | Library tracking, languages, unknown-language handling and the other per-source rules, saved to `config.yaml` with comments kept. Also YouTube cookies, the API token and the extension download |

The UI has **no login**. Keep it on your LAN (or behind a VPN). Every change needs a same-origin request carrying the page's token.
The `/api/youtube/*` endpoints need the bearer token `web.api_token`.

## YouTube

YouTube has no API for watch history, so three inputs feed one `youtube` source:

| Input | Covers | Accuracy |
|---|---|---|
| **History reader** (automatic, unofficial) | Every device, including the phone app | Reads the account's history page every 10 min with its cookies, and estimates time from how far the red "watched" bar moved. Straight-through 1× viewing is within about ±5–10%. Rewinding and slow playback undercount, and Incognito or history-off viewing isn't seen |
| **Browser extension** (desktop) | youtube.com in Chrome, Edge, Brave and Firefox | Measured: only actual playback counts, not pauses, ads or buffering (±15–30 s) |
| **Share sheet / Quick log** (manual) | Anything the others miss | Your minutes, or the whole video |

When the extension or the share sheet logs a viewing the history reader already estimated, the estimate is retracted, so it isn't counted twice. Extension and share-sheet logs aren't deduplicated against each other.
Videos without kana in the title or channel name go to Review by default. Setup is in [deploy/README.md](deploy/README.md#youtube).

## Push API

For your own apps (a game launcher, a reader, a script): `POST /api/sessions` with `Authorization: Bearer <web.api_token>`,
the same token as the YouTube extension. Enable it with `sources.push.enabled: true`. Post a session as often as
you like while it grows; mochibridge keeps the largest seconds and characters it has seen, and a session nothing has
posted to for 10 minutes is finished (send `"final": true` to finish it at once).

```json
{
  "client": "genkan",
  "session_id": "genkan-1-1795117258712",
  "started_at": "2026-11-19T20:00:00Z",
  "seconds": 1200,
  "characters": 4100,
  "final": false,
  "content_type": "Visual Novel",
  "activity_type": "Reading",
  "language": "ja",
  "note": "Chapter 3",
  "entry": {
    "title": "マルコと銀河竜",
    "ids": {"romm.rom_id": 117},
    "fields": {"Platform": "PC"}
  }
}
```

| Field | |
|---|---|
| `client` | Your app's name (lowercase letters, digits, `-`, `_`). It works like a library: per-app rules (tracked, languages, default types) go under `sources.push.libraries.<client>` and on the Settings page |
| `session_id` | Unique per session within the app |
| `started_at` | ISO 8601 with an offset or `Z` (without one: the bridge's time zone) |
| `seconds`, `characters` | Active time and characters so far. Only growth counts, so a late or repeated post never shrinks a log. `characters` is optional |
| `content_type`, `activity_type` | kechimochi's types (`Visual Novel`, `Videogame`…; `Reading`, `Playing`…). Optional: the app's defaults from its rules |
| `entry.title` | The kechimochi entry's title |
| `entry.ids` | Optional stable IDs (`name` or `namespace.name`). The first one (by name) decides which entry a session belongs to; they're written into the entry's extra data (`romm.rom_id` as `{"romm": {"rom_id": …}}`) and match an existing entry that already has them |
| `entry.fields` | Optional extra data for a new entry (strings or numbers) |
| `entry.cover_url` | Optional public http(s) URL of the entry's cover image (see Covers below) |
| `language`, `note`, `quality` | Optional: language code (default `ja`), the log's note, `measured` (default) or `estimate` |

Answers: `200 {"ok": true, "cover": "wanted" | "set"}`; `400` with what's wrong; `401` without the token; `503` when
the source is disabled. `"cover": "wanted"` means the bridge has no image or URL for this entry yet.

### Covers

Upload the entry's cover once, when a session post answers `"cover": "wanted"`:

```
POST /api/covers            (multipart/form-data, same bearer token)
  client = genkan
  entry  = {"title": "サバイバルキッズ2", "ids": {"romm.rom_id": 117}}   (JSON; the same entry as in session posts)
  image  = the file: JPEG, PNG or WebP, at most 5 MB
```

Answers `200 {"ok": true, "entry": "<entry key>"}` or `400` with what's wrong. Alternatively, send `entry.cover_url` in
session posts when the app knows a correct public image URL; an uploaded image wins over a URL. Either way the image
only fills an **empty** cover in kechimochi: a cover already there (yours or an earlier upload) is never replaced. To
change one, clear it in kechimochi and upload the new image again. Uploaded images are kept in
`push-covers/` next to the database (`sources.push.covers_dir` to move them).

## Running it

Docker is the intended way; see [deploy/README.md](deploy/README.md). The image is `ghcr.io/zacaronii/mochibridge`,
built by GitHub Actions on every push to `main`, and it includes the browser extension zips.

For development:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # installs the `kbridge` command too
cp config.example.yaml config.yaml          # adjust; ${VARS} come from the environment or secrets.env
cp secrets.env.example secrets.env          # API keys (gitignored)

.venv/bin/python -m kbridge check           # connectivity + mode
.venv/bin/python -m kbridge libraries       # every library on every server and whether it's tracked
.venv/bin/python -m kbridge once            # one poll + one sync pass (the first poll starts from "now")
.venv/bin/python -m kbridge once --since 30d   # re-read a window (still respects mode)
.venv/bin/python -m kbridge pending         # series status, unsent logs, skip reasons, recent events
.venv/bin/python -m kbridge sync            # outbox only
.venv/bin/python -m kbridge run             # long-running service + web UI on web.port (8080)
```

Global flags: `-c/--config`, `--db`, `--mode dry_run|live`, `--kechimochi <url>` (e.g. a throwaway test instance).

Other commands: `health` (the container healthcheck), `idmap-refresh`, `jellyfin-debug` (watch `/Sessions` sampling live)
and `jellyfin-golive` (mark the moment live Jellyfin tracking takes over from Nagare's records).

### Moving over from Nagare

If [Nagare](https://github.com/bpwhelan/Nagare)'s kechimochi sync has been logging for you, the bridge can replace it
without duplicates:

```sh
kbridge backfill --preview --nagare-db nagare.sqlite   # read-only report: data/backfill/{backfill_report.html,backfill_preview.csv,summary.md}
kbridge migrate --check                                # preflight: kechimochi version, Nagare's sync is off, counts
kbridge migrate --plan                                 # read-only: migration_plan.{json,md}
kbridge migrate --apply                                # backups, then the plan; journaled and resumable
```

The preview uses a separate DB and a kechimochi client that refuses writes. `--apply` first writes a kechimochi full backup
(`kechimochi_full_backup.zip`) and a copy of the bridge's DB (`bridge-before.sqlite`), then does the following:
- replaces Nagare's logs (only ones carrying its `[nagare:…]` marker) with the bridge's;
- deletes any manual logs you listed in `migration.delete_logs`;
- strips Nagare's extra_data from the entries it keeps;
- deletes Nagare entries left with no logs.

## Tests

```sh
.venv/bin/pytest                                       # unit tests (fixtures, fake kechimochi; the extension test needs a local Chrome or the zenika/alpine-chrome image)
URL=$(scripts/kechimochi-test.sh up)                   # throwaway kechimochi: same image as yours, tmpfs data, private network
KB_TEST_KECHIMOCHI=$URL .venv/bin/pytest -m integration
scripts/kechimochi-test.sh down
```

The fixtures in `fixtures/` have the shape of real API responses but contain fictional data.

## Acknowledgements

mochibridge is glue between other people's work. Thank you to:

- **[kechimochi](https://github.com/Morgawr/kechimochi)** by Morgawr: the immersion tracker this whole project feeds, and the HTTP API that makes it possible.
- **[Nagare](https://github.com/bpwhelan/Nagare)** by bpwhelan: its kechimochi sync came first and showed what this could be. mochibridge reads its records to carry Jellyfin history over and migrates its entries without duplicates.
- **[anibridge](https://github.com/anibridge/anibridge)** and **[anibridge-mappings](https://github.com/anibridge/anibridge-mappings)**: the AniDB/TVDB/TMDB/IMDB → AniList ID map behind series matching.
- **[AniList](https://anilist.co)**: its public GraphQL API provides the native, romaji and English titles used to match existing entries.
- **[Jellyfin](https://github.com/jellyfin/jellyfin)** and its **[Playback Reporting](https://github.com/jellyfin/jellyfin-plugin-playbackreporting)** plugin, **[Shokofin](https://github.com/ShokoAnime/Shokofin)** / [Shoko](https://shokoanime.com), **[Kavita](https://github.com/Kareadita/Kavita)** and **[Audiobookshelf](https://github.com/advplyr/audiobookshelf)**: the servers whose APIs it reads.
- The Python libraries it's built on: [HTTPX](https://www.python-httpx.org), [FastAPI](https://fastapi.tiangolo.com), [Uvicorn](https://www.uvicorn.org), [Jinja](https://jinja.palletsprojects.com), [ruamel.yaml](https://sourceforge.net/projects/ruamel-yaml/), [PyYAML](https://pyyaml.org) and [python-multipart](https://github.com/Kludex/python-multipart), with [pytest](https://pytest.org) and [RESPX](https://lundberg.github.io/respx/) for tests.

mochibridge isn't affiliated with any of these projects, or with YouTube or Google. The YouTube history reader uses an unofficial page and may stop working whenever YouTube changes it.

## License

[MIT](LICENSE)
