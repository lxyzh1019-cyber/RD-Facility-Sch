#!/usr/bin/env python3
"""
Red Deer Drop-In Schedule Aggregator
------------------------------------
Scrapes Red Deer City's looknbook.reddeer.ca for Drop-In Swimming, Arena (Skating),
and Climbing & Bouldering across the next N days and renders a single, self-contained
HTML dashboard.

Runs locally OR in GitHub Actions. When running in a GitHub Actions runner (UTC),
the 'today' anchor is computed in America/Edmonton timezone so the 14-day window
always reflects the user's local date.

Usage:
    python sync.py                         # 14 days → public/index.html (CI default)
    python sync.py --out schedule.html     # local dev
    python sync.py --days 7
    python sync.py --start 2026-05-01

Requirements: requests, beautifulsoup4, lxml
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import os
import re
import sys
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://looknbook.reddeer.ca/RedDeer/public/Category/ClassList"
PARTICIPANT = "00000000-0000-0000-0000-000000000000"
LOCAL_TZ = ZoneInfo("America/Edmonton")

CATEGORIES: dict[str, dict[str, str]] = {
    "swim": {
        "guid": "09d0604f-a42d-43a3-a93c-64a93ebda20c",
        "label": "Swimming",
        "label_zh": "游泳",
        "color": "#2563eb",
    },
    "skate": {
        "guid": "6f659e94-7b6a-4042-8c63-fbeba072256d",
        "label": "Skating / Arena",
        "label_zh": "冰场",
        "color": "#0891b2",
    },
    "climb": {
        "guid": "654b289b-816d-443f-a84f-8f0cae322e54",
        "label": "Climbing & Bouldering",
        "label_zh": "攀岩",
        "color": "#ca8a04",
    },
}

# Conflict detector tuning
CONFLICT_GAP_MIN = 30            # ± minutes
CONFLICT_CROSS_DOMAIN_ONLY = True  # same-domain pairs aren't 'conflicts'

HTTP_TIMEOUT = 30
HTTP_MAX_WORKERS = 6
USER_AGENT = "RD-DropIn-Aggregator/1.1 (personal use; single user)"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Session:
    date: dt.date
    activity_domain: str
    class_name: str
    start_time: dt.time
    end_time: dt.time
    duration_min: int
    location: str
    venue: str
    spaces: str
    availability: str
    source_url: str
    conflicts: list["ConflictRef"] = field(default_factory=list)

    def fmt_time(self, t: dt.time) -> str:
        fmt = "%-I:%M %p" if sys.platform != "win32" else "%#I:%M %p"
        return t.strftime(fmt)

    @property
    def time_label(self) -> str:
        return f"{self.fmt_time(self.start_time)} – {self.fmt_time(self.end_time)}"

    def start_minutes(self) -> int:
        return self.start_time.hour * 60 + self.start_time.minute

    def end_minutes(self) -> int:
        e = self.end_time.hour * 60 + self.end_time.minute
        s = self.start_minutes()
        return e if e >= s else e + 24 * 60


@dataclass
class ConflictRef:
    other_domain: str
    other_class: str
    other_location: str
    other_time: str
    gap_min: int            # negative = overlap
    kind: str               # 'overlap' | 'tight-transition'


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def build_url(category_guid: str, date: dt.date) -> str:
    return (
        f"{BASE_URL}?CategoryGUID={category_guid}"
        f"&StartDate={date.isoformat()}"
        f"&Participant={PARTICIPANT}"
    )


def fetch_page(url: str, session: requests.Session) -> str | None:
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"  [WARN] fetch failed: {url}\n         {e}", file=sys.stderr)
        return None


_TIME_RX = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)


def _parse_time(text: str) -> dt.time | None:
    m = _TIME_RX.search(text)
    if not m:
        return None
    h, mnt, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return dt.time(h, mnt)


def parse_sessions(
    html_text: str,
    date: dt.date,
    domain: str,
    source_url: str,
) -> list[Session]:
    soup = BeautifulSoup(html_text, "lxml")
    sessions: list[Session] = []
    table = soup.find("table")
    if table is None:
        return sessions

    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells or any(c.name == "th" for c in cells):
            continue
        if len(cells) < 7:
            continue

        time_raw = cells[0].get_text(" ", strip=True)
        class_name = cells[1].get_text(" ", strip=True)
        location = cells[3].get_text(" ", strip=True)
        venue = cells[4].get_text(" ", strip=True)
        spaces = cells[5].get_text(" ", strip=True)
        availability = cells[6].get_text(" ", strip=True)

        parts = re.split(r"\s*[-–]\s*", time_raw, maxsplit=1)
        if len(parts) != 2:
            continue
        start = _parse_time(parts[0])
        end = _parse_time(parts[1])
        if start is None or end is None:
            continue

        dur_m = re.search(r"(\d+)\s*mins?", parts[1], re.IGNORECASE)
        duration = int(dur_m.group(1)) if dur_m else _minutes_between(start, end)

        if domain == "swim" and "swim" not in class_name.lower():
            continue

        sessions.append(Session(
            date=date, activity_domain=domain, class_name=class_name,
            start_time=start, end_time=end, duration_min=duration,
            location=location, venue=venue, spaces=spaces,
            availability=availability, source_url=source_url,
        ))
    return sessions


def _minutes_between(start: dt.time, end: dt.time) -> int:
    s = start.hour * 60 + start.minute
    e = end.hour * 60 + end.minute
    if e < s:
        e += 24 * 60
    return e - s


def scrape_all(days: int, start_date: dt.date) -> list[Session]:
    dates = [start_date + dt.timedelta(days=i) for i in range(days)]
    jobs: list[tuple[str, dt.date, str]] = []
    for domain, meta in CATEGORIES.items():
        for d in dates:
            jobs.append((domain, d, build_url(meta["guid"], d)))

    print(f"Fetching {len(jobs)} pages "
          f"({len(CATEGORIES)} categories × {days} days)...")
    all_sessions: list[Session] = []
    with requests.Session() as http:
        http.headers.update({"User-Agent": USER_AGENT})
        with cf.ThreadPoolExecutor(max_workers=HTTP_MAX_WORKERS) as ex:
            futures = {
                ex.submit(fetch_page, url, http): (domain, date, url)
                for (domain, date, url) in jobs
            }
            for fut in cf.as_completed(futures):
                domain, date, url = futures[fut]
                html_text = fut.result()
                if html_text is None:
                    continue
                all_sessions.extend(parse_sessions(html_text, date, domain, url))

    all_sessions.sort(key=lambda s: (s.date, s.start_time, s.activity_domain, s.location))
    print(f"  parsed {len(all_sessions)} sessions.")
    return all_sessions


# ---------------------------------------------------------------------------
# Conflict detector
# ---------------------------------------------------------------------------

def detect_conflicts(sessions: list[Session]) -> None:
    """
    Flag cross-facility session pairs that are too close to comfortably attend both.

    Logic:
      - Group sessions by date
      - For every pair on the same date at DIFFERENT facilities:
          * overlap         → gap negative → hard conflict
          * 0..CONFLICT_GAP_MIN gap → travel-time warning
      - Ignore same-domain pairs if CONFLICT_CROSS_DOMAIN_ONLY is True
      - Mutates each session's `conflicts` list
    """
    by_date: dict[dt.date, list[Session]] = {}
    for s in sessions:
        by_date.setdefault(s.date, []).append(s)

    for day, items in by_date.items():
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if a.venue == b.venue:
                    continue
                if CONFLICT_CROSS_DOMAIN_ONLY and a.activity_domain == b.activity_domain:
                    continue

                a_s, a_e = a.start_minutes(), a.end_minutes()
                b_s, b_e = b.start_minutes(), b.end_minutes()

                if a_e <= b_s:
                    gap = b_s - a_e
                elif b_e <= a_s:
                    gap = a_s - b_e
                else:
                    overlap = min(a_e, b_e) - max(a_s, b_s)
                    gap = -overlap

                if gap > CONFLICT_GAP_MIN:
                    continue

                kind = "overlap" if gap < 0 else "tight-transition"
                a.conflicts.append(ConflictRef(
                    other_domain=b.activity_domain,
                    other_class=b.class_name,
                    other_location=_short_loc(b.location, b.venue),
                    other_time=b.time_label,
                    gap_min=gap, kind=kind,
                ))
                b.conflicts.append(ConflictRef(
                    other_domain=a.activity_domain,
                    other_class=a.class_name,
                    other_location=_short_loc(a.location, a.venue),
                    other_time=a.time_label,
                    gap_min=gap, kind=kind,
                ))


def _short_loc(location: str, venue: str) -> str:
    v = re.sub(r"^Leisure Centres\s*-\s*", "", venue)
    v = re.sub(r"^Arenas\s*-\s*", "", v)
    return location if location and location != venue else v


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Red Deer Drop-In Schedule · {generated}</title>
<style>
  :root {{
    --bg: #0f172a; --panel: #1e293b; --panel-2: #0b1222;
    --text: #e2e8f0; --muted: #94a3b8; --border: #334155;
    --accent: #38bdf8;
    --swim:  #2563eb; --skate: #0891b2; --climb: #ca8a04;
    --today: #22c55e; --warn: #f59e0b; --conflict: #ef4444;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.4;
  }}
  header {{
    padding: 16px 24px; background: var(--panel);
    border-bottom: 1px solid var(--border);
    display: flex; flex-wrap: wrap; gap: 16px;
    align-items: center; justify-content: space-between;
  }}
  header h1 {{ margin: 0; font-size: 18px; letter-spacing: 0.3px; }}
  header .meta {{ color: var(--muted); font-size: 13px; }}
  header .controls {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .chip {{
    padding: 6px 12px; border-radius: 999px;
    border: 1px solid var(--border); background: var(--panel-2);
    color: var(--text); font-size: 12px; cursor: pointer; user-select: none;
  }}
  .chip.active {{ background: var(--accent); color: #0b1222; border-color: var(--accent); }}
  .legend {{ display: inline-flex; gap: 10px; align-items: center; font-size: 12px; color: var(--muted); }}
  .legend .sw {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; margin-right: 4px; vertical-align: middle; }}

  main {{ padding: 16px 24px 48px 24px; }}

  .day {{
    margin-bottom: 20px; background: var(--panel);
    border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
  }}
  .day-head {{
    padding: 10px 16px; background: var(--panel-2);
    border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: baseline;
  }}
  .day-head .date {{ font-weight: 600; font-size: 15px; }}
  .day-head .count {{ color: var(--muted); font-size: 12px; }}
  .day.today .day-head {{ border-left: 3px solid var(--today); }}
  .day.today .date::after {{ content: " · Today"; color: var(--today); font-weight: 500; }}

  .domain-row {{
    display: grid; grid-template-columns: 150px 1fr;
    border-top: 1px solid var(--border);
  }}
  .domain-row:first-child {{ border-top: none; }}
  .domain-label {{
    padding: 12px 16px; background: var(--panel-2);
    font-size: 13px; font-weight: 600;
    display: flex; flex-direction: column; gap: 2px;
    justify-content: center; border-right: 1px solid var(--border);
  }}
  .domain-label .zh {{ font-size: 11px; color: var(--muted); font-weight: 400; }}
  .domain-label.swim  {{ border-left: 3px solid var(--swim); }}
  .domain-label.skate {{ border-left: 3px solid var(--skate); }}
  .domain-label.climb {{ border-left: 3px solid var(--climb); }}

  .sessions {{
    padding: 10px; display: flex; flex-wrap: wrap; gap: 8px;
    min-height: 48px; align-items: flex-start;
  }}
  .session {{
    display: flex; flex-direction: column; gap: 2px;
    padding: 8px 10px; border-radius: 6px;
    background: var(--panel-2); border: 1px solid var(--border);
    border-left-width: 3px; font-size: 12.5px;
    min-width: 220px; max-width: 320px;
    text-decoration: none; color: inherit;
    transition: transform 120ms ease, border-color 120ms ease;
    position: relative;
  }}
  .session:hover {{ transform: translateY(-1px); border-color: var(--accent); }}
  .session.swim  {{ border-left-color: var(--swim); }}
  .session.skate {{ border-left-color: var(--skate); }}
  .session.climb {{ border-left-color: var(--climb); }}
  .session .time {{ font-weight: 600; }}
  .session .name {{ color: var(--muted); font-size: 11.5px; }}
  .session .loc  {{ color: var(--text); font-size: 11.5px; opacity: 0.85; }}
  .session .dur  {{ color: var(--muted); font-size: 10.5px; }}

  .badge {{
    display: inline-block; margin-top: 4px;
    padding: 2px 6px; font-size: 10.5px;
    border-radius: 4px; font-weight: 600; letter-spacing: 0.2px;
  }}
  .badge.warn     {{ background: rgba(245,158,11,0.15); color: var(--warn);     border: 1px solid var(--warn); }}
  .badge.conflict {{ background: rgba(239,68,68,0.15);  color: var(--conflict); border: 1px solid var(--conflict); }}
  .conflicts-list {{
    margin-top: 4px; padding: 6px 8px;
    background: rgba(0,0,0,0.25); border-radius: 4px;
    font-size: 10.5px; color: var(--muted); line-height: 1.35;
  }}
  .conflicts-list .row {{ display: block; }}
  .conflicts-list strong {{ color: var(--text); font-weight: 600; }}
  .session.has-conflict {{ border-color: var(--conflict); }}

  .empty {{ color: var(--muted); font-style: italic; font-size: 12px; padding: 4px 6px; }}

  footer {{
    padding: 16px 24px; color: var(--muted);
    font-size: 11.5px; border-top: 1px solid var(--border);
  }}
  footer code {{ background: var(--panel); padding: 2px 6px; border-radius: 4px; }}

  body.hide-swim  .domain-row.swim  {{ display: none; }}
  body.hide-skate .domain-row.skate {{ display: none; }}
  body.hide-climb .domain-row.climb {{ display: none; }}
  body.only-conflicts .session:not(.has-conflict) {{ display: none; }}
</style>
</head>
<body>

<header>
  <div>
    <h1>Red Deer Drop-In Schedule · 红鹿市公共活动时间表</h1>
    <div class="meta">
      Generated {generated} ({generated_tz}) · {total_sessions} sessions · {total_conflicts} conflict flag(s) · {days} day window
    </div>
  </div>
  <div class="controls">
    <span class="legend">
      <span><span class="sw" style="background:var(--swim)"></span>Swim</span>
      <span><span class="sw" style="background:var(--skate)"></span>Skate</span>
      <span><span class="sw" style="background:var(--climb)"></span>Climb</span>
    </span>
    <button class="chip active" data-toggle="swim">Swim</button>
    <button class="chip active" data-toggle="skate">Skate</button>
    <button class="chip active" data-toggle="climb">Climb</button>
    <button class="chip" data-conflicts-only>⚠️ Conflicts only</button>
  </div>
</header>

<main>
{body}
</main>

<footer>
  Data source: <a href="https://looknbook.reddeer.ca" style="color:var(--accent)">looknbook.reddeer.ca</a>.
  Click any session card to open the original booking page.
  ⚠️ = cross-facility sessions within ±30 min of each other (travel-time warning).
  Auto-refreshed daily via GitHub Actions. Last scrape {generated} {generated_tz}.
</footer>

<script>
  document.querySelectorAll('.chip[data-toggle]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const key = btn.dataset.toggle;
      btn.classList.toggle('active');
      document.body.classList.toggle('hide-' + key, !btn.classList.contains('active'));
    }});
  }});
  const conflictBtn = document.querySelector('[data-conflicts-only]');
  if (conflictBtn) {{
    conflictBtn.addEventListener('click', () => {{
      conflictBtn.classList.toggle('active');
      document.body.classList.toggle('only-conflicts');
    }});
  }}
</script>

</body>
</html>
"""


def render_html(sessions: list[Session], start_date: dt.date, days: int) -> str:
    by_day: dict[dt.date, dict[str, list[Session]]] = {}
    for s in sessions:
        by_day.setdefault(s.date, {k: [] for k in CATEGORIES})[s.activity_domain].append(s)

    today = dt.datetime.now(LOCAL_TZ).date()
    total_conflicts = sum(1 for s in sessions if s.conflicts)

    day_blocks: list[str] = []
    for i in range(days):
        d = start_date + dt.timedelta(days=i)
        day_sessions = by_day.get(d, {k: [] for k in CATEGORIES})
        day_count = sum(len(v) for v in day_sessions.values())

        rows_html: list[str] = []
        for domain_key, meta in CATEGORIES.items():
            items = day_sessions.get(domain_key, [])
            if items:
                cards = "\n".join(_render_session_card(s) for s in items)
            else:
                cards = '<span class="empty">— no sessions —</span>'
            rows_html.append(
                f'<div class="domain-row {domain_key}">'
                f'  <div class="domain-label {domain_key}">'
                f'    <span>{html.escape(meta["label"])}</span>'
                f'    <span class="zh">{html.escape(meta["label_zh"])}</span>'
                f'  </div>'
                f'  <div class="sessions">{cards}</div>'
                f'</div>'
            )

        is_today = " today" if d == today else ""
        fmt = "%a, %b %-d, %Y" if sys.platform != "win32" else "%a, %b %#d, %Y"
        day_label = d.strftime(fmt)

        day_blocks.append(
            f'<section class="day{is_today}">'
            f'  <div class="day-head">'
            f'    <span class="date">{html.escape(day_label)}</span>'
            f'    <span class="count">{day_count} session(s)</span>'
            f'  </div>'
            f'  {"".join(rows_html)}'
            f'</section>'
        )

    now_local = dt.datetime.now(LOCAL_TZ)
    return PAGE_TEMPLATE.format(
        generated=now_local.strftime("%Y-%m-%d %H:%M"),
        generated_tz=now_local.strftime("%Z"),
        total_sessions=len(sessions),
        total_conflicts=total_conflicts,
        days=days,
        body="\n".join(day_blocks),
    )


def _render_session_card(s: Session) -> str:
    display_loc = _short_loc(s.location, s.venue)
    conflict_cls = " has-conflict" if s.conflicts else ""

    badge = ""
    conflict_list_html = ""
    if s.conflicts:
        has_overlap = any(c.kind == "overlap" for c in s.conflicts)
        badge_cls = "conflict" if has_overlap else "warn"
        badge_text = "⚠️ Travel/overlap conflict" if has_overlap else "⏱ Tight transition"
        badge = f'<span class="badge {badge_cls}">{badge_text}</span>'

        rows = []
        for c in s.conflicts[:3]:
            if c.kind == "overlap":
                gap_label = f"overlaps by {abs(c.gap_min)} min"
            else:
                gap_label = f"{c.gap_min} min gap"
            rows.append(
                f'<span class="row">↔ <strong>{html.escape(c.other_domain.title())}</strong> '
                f'{html.escape(c.other_time)} @ {html.escape(c.other_location)} '
                f'({gap_label})</span>'
            )
        if len(s.conflicts) > 3:
            rows.append(f'<span class="row">+ {len(s.conflicts) - 3} more…</span>')
        conflict_list_html = f'<div class="conflicts-list">{"".join(rows)}</div>'

    return (
        f'<a class="session {html.escape(s.activity_domain)}{conflict_cls}" '
        f'href="{html.escape(s.source_url)}" target="_blank" rel="noopener">'
        f'  <span class="time">{html.escape(s.time_label)}</span>'
        f'  <span class="name">{html.escape(s.class_name)}</span>'
        f'  <span class="loc">📍 {html.escape(display_loc)}</span>'
        f'  <span class="dur">{s.duration_min} min · {html.escape(s.spaces)} spaces</span>'
        f'  {badge}'
        f'  {conflict_list_html}'
        f'</a>'
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_output() -> str:
    return "public/index.html" if os.getenv("GITHUB_ACTIONS") else "schedule.html"


def main() -> int:
    p = argparse.ArgumentParser(description="Red Deer drop-in schedule aggregator")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--start", type=str, default=None)
    p.add_argument("--out", type=str, default=None)
    args = p.parse_args()

    start = (dt.date.fromisoformat(args.start)
             if args.start else dt.datetime.now(LOCAL_TZ).date())
    out_path = args.out or _default_output()

    sessions = scrape_all(days=args.days, start_date=start)
    detect_conflicts(sessions)
    conflict_count = sum(1 for s in sessions if s.conflicts)
    print(f"  flagged {conflict_count} sessions with conflicts.")

    html_doc = render_html(sessions, start, args.days)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"Wrote {out_path} ({len(html_doc):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
