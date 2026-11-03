"""Backfill preview outputs: backfill_preview.csv, summary.md, backfill_report.html."""

from __future__ import annotations

import base64
import csv
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .db import DB, utcnow
from .kechimochi import Kechimochi
from .service import PollStats
from .sync import SyncStats

CSV_COLUMNS = ["source", "library", "series", "target", "match", "suggestion", "merge", "date", "minutes", "note",
               "quality", "included", "reason", "flags", "session_id"]
_MARKER = re.compile(r"^\[nagare:[^:\]]+:([A-Za-z0-9_\-]+)\]")


@dataclass
class Report:
    generated_at: str
    mode_note: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    series: list[dict[str, Any]] = field(default_factory=list)
    comparison: list[dict[str, Any]] = field(default_factory=list)
    planned: dict[str, int] = field(default_factory=dict)
    kechimochi_counts: dict[str, int] = field(default_factory=dict)


def nagare_history_id(notes: str | None) -> str | None:
    """Decode the `[nagare:<instance>:<base64url(history_id)>]` first line of a Nagare-owned log."""
    m = _MARKER.match(notes or "")
    if not m:
        return None
    raw = m.group(1)
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def base_history_id(hid: str) -> str:
    return re.sub(r"^rewatch:\d+:", "", hid)


async def build(db: DB, km: Kechimochi, cfg: Config, poll_stats: dict[str, PollStats],
                sync_stats: SyncStats) -> Report:
    media = {m["id"]: m for m in await km.list_media()}
    klogs = await km.list_logs()
    manual = [l for l in klogs if nagare_history_id(l.get("notes")) is None]
    manual_by: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for l in manual:
        manual_by[(l["media_id"], l["date"])].append(l)

    series = {(r["source"], r["series_key"]): dict(r) for r in db.all("SELECT * FROM series_map")}
    sugg: dict[tuple[str, str], list[dict]] = defaultdict(list)     # close titles to confirm
    merges: dict[tuple[str, str], list[dict]] = defaultdict(list)   # Nagare duplicates of the linked entry
    for r in db.all("SELECT * FROM suggestions ORDER BY score DESC"):
        (merges if r["method"] == "merge" else sugg)[(r["source"], r["series_key"])].append(dict(r))
    fmt_merge = lambda ms: ", ".join(f"#{m['media_id']} {m['title']}" for m in ms)
    long_minutes = cfg.long_session_hours * 60

    def target_of(s: dict | None) -> tuple[str, str]:
        if s is None:
            return "", ""
        if s["media_id"]:
            title = media.get(s["media_id"], {}).get("title", s["title"])
            return f"#{s['media_id']} {title}", s["link_method"] or ""
        return {"pending_create": "new", "needs_mapping": "needs mapping"}.get(s["status"], s["status"]), ""

    rows: list[dict[str, Any]] = []
    for r in db.all("SELECT * FROM logs"):
        meta = json.loads(r["meta_json"] or "{}")
        desired = json.loads(r["desired_json"])
        s = series.get((r["source"], r["series_key"]))
        target, match = target_of(s)
        suggestions = sugg.get((r["source"], r["series_key"]), [])
        flags = list(meta.get("flags") or [])
        if desired["duration_minutes"] >= long_minutes:
            flags.append(f"long_session>{cfg.long_session_hours:g}h")
        candidates = [(s["media_id"], "")] if s and s["media_id"] else []
        candidates += [(g["media_id"], " (suggested entry)") for g in suggestions]
        for mid, why in candidates:
            for ml in manual_by.get((mid, desired["date"]), []):
                flags.append(f"possible_duplicate:#{ml['id']} {ml['duration_minutes']}m{why}")
        rows.append({
            "source": r["source"], "library": meta.get("library", ""), "series": meta.get("series", ""),
            "target": target, "match": match,
            "suggestion": "; ".join(f"#{g['media_id']} {g['title']} ({g['method']})" for g in suggestions),
            "merge": fmt_merge(merges.get((r["source"], r["series_key"]), [])),
            "date": desired["date"], "minutes": desired["duration_minutes"], "note": desired["notes"],
            "quality": meta.get("quality", ""), "included": "yes", "reason": "", "flags": ", ".join(flags),
            "session_id": r["session_id"], "_series_key": r["series_key"], "_nagare_key": meta.get("nagare_key"),
        })
    for r in db.all("SELECT * FROM skips"):
        meta = json.loads(r["meta_json"] or "{}")
        rows.append({
            "source": r["source"], "library": meta.get("library", ""),
            "series": meta.get("series") or meta.get("title") or r["detail"] or "",
            "target": "", "match": "", "suggestion": "", "merge": "", "date": meta.get("date", ""),
            "minutes": round(meta.get("minutes") or 0), "note": meta.get("note", ""),
            "quality": meta.get("quality", ""), "included": "no", "reason": r["reason"],
            "flags": ", ".join(meta.get("flags") or []), "session_id": r["session_id"],
            "_series_key": None, "_nagare_key": meta.get("nagare_key"),
        })
    rows.sort(key=lambda x: (x["date"] or "", x["source"], x["series"]))

    sources = []
    for name, st in poll_stats.items():
        mine = [x for x in rows if x["source"] == name]
        inc = [x for x in mine if x["included"] == "yes"]
        dates = [x["date"] for x in mine if x["date"]]
        sources.append({
            "source": name, "sessions": st.sessions, "rows": len(mine), "included": len(inc),
            "hours": round(sum(x["minutes"] for x in inc) / 60, 1),
            "earliest": min(dates) if dates else "", "latest": max(dates) if dates else "",
            "quality": dict(Counter(x["quality"] for x in inc)),
            "skipped": dict(Counter(x["reason"] for x in mine if x["included"] == "no")),
        })

    per_series: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for x in rows:
        if x["included"] == "yes":
            per_series[(x["source"], x["_series_key"])].append(x)
    series_rows = []
    for key, s in sorted(series.items(), key=lambda kv: (kv[0][0], kv[1]["title"])):
        logs = per_series.get(key, [])
        target, match = target_of(s)
        series_rows.append({
            "source": key[0], "title": s["title"], "status": s["status"], "target": target, "match": match,
            "logs": len(logs), "hours": round(sum(x["minutes"] for x in logs) / 60, 1),
            "first": min((x["date"] for x in logs), default=""), "last": max((x["date"] for x in logs), default=""),
            "suggestions": "; ".join(f"#{g['media_id']} {g['title']} ({g['method']}, {g['score']:.2f})"
                                     for g in sugg.get(key, [])),
            "merge": fmt_merge(merges.get(key, [])),
        })

    comparison = _compare(rows, klogs, media)
    ops = Counter((r["method"], r["path"].split("/")[2] if r["path"].count("/") >= 2 else r["path"])
                  for r in db.all("SELECT method, path FROM ops WHERE mode='dry_run'"))
    planned = {
        "media to create": ops.get(("POST", "media"), 0),
        "existing entries to fill (description/extra fields)": ops.get(("PUT", "media"), 0),
        "covers to upload": ops.get(("POST", "covers"), 0),
        "logs to create": sum(1 for x in rows if x["included"] == "yes"),
        "series needing a mapping decision": sum(1 for s in series.values() if s["status"] == "needs_mapping"),
        "series with a close title match to confirm": sum(1 for k in sugg if series.get(k, {}).get("status") == "pending_create"),
        "Nagare duplicate entries to merge": len({m["media_id"] for ms in merges.values() for m in ms}),
    }
    return Report(
        generated_at=utcnow().isoformat(timespec="seconds"),
        mode_note="Preview only: nothing was written to kechimochi.",
        rows=rows, sources=sources, series=series_rows, comparison=comparison, planned=planned,
        kechimochi_counts={"media": len(media), "logs": len(klogs), "nagare_logs": len(klogs) - len(manual),
                           "manual_logs": len(manual)},
    )


def _compare(rows: list[dict], klogs: list[dict], media: dict[int, dict]) -> list[dict]:
    """Per series and day: Nagare's existing minutes vs the bridge's proposed minutes."""
    label_by_key: dict[str, tuple[str, str]] = {}        # nagare base history id -> (source, series)
    for x in rows:
        if x["_nagare_key"] and x["series"]:
            label_by_key.setdefault(x["_nagare_key"], (x["source"], x["series"]))
    nagare: dict[tuple[str, str], float] = defaultdict(float)
    nagare_entries: dict[tuple[str, str], set[str]] = defaultdict(set)
    for l in klogs:
        hid = nagare_history_id(l.get("notes"))
        if hid is None:
            continue
        src_series = label_by_key.get(base_history_id(hid))
        series = src_series[1] if src_series else f"(unmatched) {media.get(l['media_id'], {}).get('title', '?')}"
        nagare[(series, l["date"])] += l["duration_minutes"]
        nagare_entries[(series, l["date"])].add(f"#{l['media_id']}")
    covered = {s for s, _ in nagare}
    bridge: dict[tuple[str, str], float] = defaultdict(float)
    for x in rows:
        if x["included"] == "yes" and x["series"] in covered:
            bridge[(x["series"], x["date"])] += x["minutes"]
    out = []
    for key in sorted(set(nagare) | set(bridge)):
        n, b = nagare.get(key, 0), bridge.get(key, 0)
        out.append({"series": key[0], "date": key[1], "nagare_minutes": round(n), "bridge_minutes": round(b),
                    "diff": round(b - n), "nagare_entries": ", ".join(sorted(nagare_entries.get(key, ()))),
                    "note": "only Nagare" if not b else ("only bridge" if not n else "")})
    return out


# ------------------------------------------------------------------ writers
def write(rep: Report, out: Path) -> None:
    with open(out / "backfill_preview.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rep.rows)
    (out / "summary.md").write_text(summary_markdown(rep), encoding="utf-8")
    (out / "backfill_report.html").write_text(render_html(rep), encoding="utf-8")


def summary_markdown(rep: Report) -> str:
    lines = [f"# Backfill preview — {rep.generated_at}", "", rep.mode_note, "",
             f"kechimochi now: {rep.kechimochi_counts}", "", "## Planned", ""]
    lines += [f"- {k}: **{v}**" for k, v in rep.planned.items()]
    lines += ["", "## Sources", "", "| source | sessions | included | hours | earliest | latest | quality | skipped |",
              "|---|---|---|---|---|---|---|---|"]
    for s in rep.sources:
        lines.append(f"| {s['source']} | {s['sessions']} | {s['included']} | {s['hours']} | {s['earliest']} | "
                     f"{s['latest']} | {s['quality']} | {s['skipped']} |")
    lines += ["", "## Entries", "", "| source | series | kechimochi | match | logs | hours | first | last | suggestions | merge |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for s in rep.series:
        lines.append(f"| {s['source']} | {s['title']} | {s['target']} | {s['match']} | {s['logs']} | {s['hours']} | "
                     f"{s['first']} | {s['last']} | {s['suggestions']} | {s['merge']} |")
    lines += ["", "## Nagare comparison", "", "| series | date | Nagare min | bridge min | diff | Nagare entries | note |",
              "|---|---|---|---|---|---|---|"]
    for c in rep.comparison:
        lines.append(f"| {c['series']} | {c['date']} | {c['nagare_minutes']} | {c['bridge_minutes']} | {c['diff']} | "
                     f"{c['nagare_entries']} | {c['note']} |")
    flagged = [r for r in rep.rows if r["flags"] and r["included"] == "yes"]
    lines += ["", f"## Flagged logs ({len(flagged)})", ""]
    lines += [f"- {r['date']} {r['source']} {r['series']} — {r['note']} ({r['minutes']}m): {r['flags']}" for r in flagged]
    return "\n".join(lines) + "\n"


def _table(headers: list[tuple[str, str]], rows: list[dict], row_attrs=None, cls: str = "") -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for _, h in headers)
    body = []
    for r in rows:
        attrs = row_attrs(r) if row_attrs else ""
        cells = "".join(f"<td class=\"c-{k}\">{html.escape(str(r.get(k, '')))}</td>" for k, _ in headers)
        body.append(f"<tr{attrs}>{cells}</tr>")
    return f"<div class=\"scroll\"><table class=\"{cls}\"><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def render_html(rep: Report) -> str:
    e = html.escape
    cards = "".join(
        f"<div class=\"card\"><div class=\"k\">{e(s['source'])}</div><div class=\"v\">{s['included']}"
        f"<span> logs</span></div><div class=\"sub\">{s['hours']} h · {e(s['earliest'])} → {e(s['latest'])}<br>"
        f"{s['sessions']} sessions read · quality {e(json.dumps(s['quality']))}<br>skipped {e(json.dumps(s['skipped'], ensure_ascii=False))}</div></div>"
        for s in rep.sources)
    planned = "".join(f"<li><b>{v}</b> {e(k)}</li>" for k, v in rep.planned.items())
    series = _table([("source", "Source"), ("title", "Series"), ("target", "kechimochi"), ("match", "Match"),
                     ("logs", "Logs"), ("hours", "Hours"), ("first", "First"), ("last", "Last"),
                     ("suggestions", "Close title (confirm?)"), ("merge", "Nagare duplicates to merge")], rep.series,
                    lambda r: ' class="warn"' if r["status"] == "needs_mapping" or r["suggestions"] else "")
    comparison = _table([("series", "Series"), ("date", "Date"), ("nagare_minutes", "Nagare min"),
                         ("bridge_minutes", "Bridge min"), ("diff", "Diff"), ("nagare_entries", "Nagare entries"),
                         ("note", "")], rep.comparison,
                        lambda r: ' class="warn"' if r["note"] or abs(r["diff"]) >= 5 else "")
    log_rows = [r for r in rep.rows]
    logs = _table([("date", "Date"), ("source", "Source"), ("library", "Library"), ("series", "Series"),
                   ("note", "Note"), ("minutes", "Min"), ("quality", "Quality"), ("target", "kechimochi"),
                   ("included", "Incl."), ("reason", "Skip reason"), ("flags", "Flags")], log_rows,
                  lambda r: (f' data-source="{e(r["source"])}" data-included="{r["included"]}"'
                             f' class="{"flagged" if r["flags"] and r["included"] == "yes" else ""}'
                             f'{" skipped" if r["included"] == "no" else ""}"'), cls="logs")
    sources_opts = "".join(f'<option value="{e(s["source"])}">{e(s["source"])}</option>' for s in rep.sources)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backfill Preview</title>
<style>
:root {{ --bg:#f7f7f5; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --line:#e3e2dd; --accent:#3b6ea8;
        --warn:#fff4d6; --flag:#fde8e4; --skip:#9a9a94; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --fg:#ecebe6; --muted:#a3a29b; --card:#1f1f1d;
        --line:#33322f; --accent:#7fa8d8; --warn:#3a3220; --flag:#3d2522; --skip:#77766f; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,-apple-system,"Segoe UI",
        "Hiragino Sans","Noto Sans JP",sans-serif; }}
main {{ max-width:1400px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:32px 0 8px; }}
.muted {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; margin-top:16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }}
.card .k {{ color:var(--muted); text-transform:uppercase; font-size:12px; letter-spacing:.04em; }}
.card .v {{ font-size:26px; font-weight:600; }} .card .v span {{ font-size:14px; font-weight:400; color:var(--muted); }}
.card .sub {{ color:var(--muted); font-size:12px; margin-top:4px; }}
ul.planned {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 12px 12px 32px; }}
.scroll {{ overflow-x:auto; border:1px solid var(--line); border-radius:8px; background:var(--card); }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ position:sticky; top:0; background:var(--card); font-weight:600; }}
td.c-minutes, td.c-logs, td.c-hours, td.c-nagare_minutes, td.c-bridge_minutes, td.c-diff {{ text-align:right;
        font-variant-numeric:tabular-nums; }}
tr.warn td {{ background:var(--warn); }} tr.flagged td {{ background:var(--flag); }}
tr.skipped td {{ color:var(--skip); }}
.filters {{ display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; }}
.filters input, .filters select {{ font:inherit; padding:5px 8px; border:1px solid var(--line); border-radius:6px;
        background:var(--card); color:var(--fg); }}
.filters input {{ flex:1 1 220px; }}
</style></head>
<body><main>
<h1>Backfill preview</h1>
<div class="muted">{e(rep.generated_at)} · {e(rep.mode_note)} · kechimochi currently has {rep.kechimochi_counts['media']} media,
{rep.kechimochi_counts['logs']} logs ({rep.kechimochi_counts['nagare_logs']} from Nagare, {rep.kechimochi_counts['manual_logs']} manual)</div>
<div class="cards">{cards}</div>
<h2>What applying this would do</h2><ul class="planned">{planned}</ul>
<h2>Entries</h2><p class="muted">Highlighted rows need a decision: several possible entries, or a close title match to confirm.</p>
{series}
<h2>Nagare comparison</h2><p class="muted">Per series and day: minutes Nagare logged vs minutes the bridge would log.
Highlighted: one side only, or a difference of 5+ minutes.</p>
{comparison}
<h2>Logs</h2>
<div class="filters">
  <input id="q" type="search" placeholder="Filter by series, note, flag…">
  <select id="src"><option value="">All sources</option>{sources_opts}</select>
  <select id="inc"><option value="yes">Included</option><option value="no">Skipped</option><option value="">All</option></select>
  <label><input id="flag" type="checkbox"> Flagged only</label>
</div>
{logs}
</main>
<script>
(function () {{
  var rows = Array.prototype.slice.call(document.querySelectorAll("table.logs tbody tr"));
  var q = document.getElementById("q"), src = document.getElementById("src"),
      inc = document.getElementById("inc"), flag = document.getElementById("flag");
  function apply() {{
    var t = q.value.toLowerCase();
    rows.forEach(function (r) {{
      var ok = (!src.value || r.dataset.source === src.value) && (!inc.value || r.dataset.included === inc.value)
        && (!flag.checked || r.classList.contains("flagged")) && (!t || r.textContent.toLowerCase().indexOf(t) >= 0);
      r.style.display = ok ? "" : "none";
    }});
  }}
  [q, src, inc, flag].forEach(function (el) {{ el.addEventListener("input", apply); el.addEventListener("change", apply); }});
  apply();
}})();
</script>
</body></html>
"""
