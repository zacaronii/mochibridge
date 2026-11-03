# <img src="art/keeper.svg" alt="" width="44" height="44" align="absmiddle"> mochibridge

Logs your immersion activity from **Jellyfin**, **Kavita**, **Audiobookshelf** and **YouTube** into
[kechimochi](https://github.com/Morgawr/kechimochi) (the web build, tested against web-0.3.3) through its HTTP API.
It creates one media entry per series and one activity log per viewing, reading or listening session, with covers and metadata.

It runs as a single container with a small web UI for reviewing, mapping and settings. A browser extension and
an iOS Shortcut feed it YouTube viewing.

## What it logs

| Source | Where the time comes from | Entry |
|---|---|---|
| **Jellyfin** | `/Sessions` sampled every 15 s. Time counts only while the playback position moves *and* the audio track being played is in an allowed language. Resumed fragments are joined into one play. The Playback Reporting plugin is a backstop for plays the sampler missed | One per series (or movie), with AniList IDs from anibridge's mapping DB when available |
| **Kavita** | Reading history: one log per reading session and chapter. Sessions Kavita invents from bulk "mark as read" are ignored by default | One per series |
| **Audiobookshelf** | Your listening sessions | One per podcast or book |
| **YouTube** | See [YouTube](#youtube) below | One per channel |

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

If Nagare's kechimochi sync has been logging for you, the bridge can replace it
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
