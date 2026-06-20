"""Presentation helpers for the Agora Atrium workspace (pure functions, no I/O).

The Atrium template renders ALL data with Jinja in HTML and keeps inline <script> blocks free of
Jinja (so scripts/_validate_dash_js.py / esprima only ever sees real JS). A few things are awkward
to compute in a template -- the leads sparkline geometry, the month calendar grid, the cross-campaign
"awaiting" rollup -- so we compute them here in Python and pass the result in as a `view` dict.

Everything here is a pure function of the workspace dict + the current time, so it is trivially
testable and never touches GCS.
"""

import base64
import calendar as _calendar
import datetime
import json

# The agency and ALL its infrastructure live in Singapore (asia-southeast1). The calendar's notion of
# "today" must be the client's business day in SGT (UTC+8, no DST), not UTC -- a UTC "today" lags the
# local day by up to 8 hours, so during SGT mornings it highlights yesterday. Fixed offset is exact
# for Singapore (SGT never observes DST).
AGORA_TZ = datetime.timezone(datetime.timedelta(hours=8))


def business_today(now=None):
    """Today's date in the Agora business timezone (SGT, UTC+8) -- the calendar's anchor for 'today'.

    `now` may be None (use the wall clock), a tz-aware datetime (converted to SGT), or a naive
    datetime (used as-is, so tests can pin an exact local day).
    """
    if now is None:
        now = datetime.datetime.now(AGORA_TZ)
    elif now.tzinfo is not None:
        now = now.astimezone(AGORA_TZ)
    return now.date()


# --- Small derivations --------------------------------------------------------------------------
def initials(user):
    """Up to two uppercase initials for an avatar, derived from an email/login string."""
    if not user:
        return "?"
    local = user.split("@")[0]
    parts = [p for p in local.replace("_", ".").split(".") if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return (local[:2] or "?").upper()


_GENERIC_MAILBOXES = {
    "info", "admin", "hello", "team", "contact", "support", "sales", "office", "owner",
}


def greeting_name(user, ws):
    """A friendly first name for the greeting -- the login's name, else the client's first word."""
    if user and "@" in user:
        local = user.split("@")[0]
        if local.lower() not in _GENERIC_MAILBOXES:
            return local.split(".")[0].split("_")[0].title()
    display = (ws.get("display_name") or "there").strip()
    return display.split()[0] if display else "there"


# --- Dashboard sparkline (geometry only; rendered as an inline <polyline>/<path>) ----------------
def sparkline(series, width=560, height=72, pad=8):
    """Return polyline points + an area path for a leads sparkline over `series`.

    Pure geometry -> the template drops `line`/`area` straight into an SVG, so no JS is needed.
    """
    values = [float(v) for v in (series or [])]
    n = len(values)
    if n == 0:
        return {"w": width, "h": height, "line": "", "area": "", "lastx": 0, "lasty": height}
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    step = inner_w / (n - 1) if n > 1 else 0.0

    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = pad + (1.0 - (v - lo) / span) * inner_h
        pts.append((round(x, 2), round(y, 2)))

    line = " ".join("%s,%s" % (x, y) for x, y in pts)
    baseline = height - pad
    area = "M %s,%s " % (pts[0][0], baseline)
    area += " ".join("L %s,%s" % (x, y) for x, y in pts)
    area += " L %s,%s Z" % (pts[-1][0], baseline)
    return {"w": width, "h": height, "line": line, "area": area,
            "lastx": pts[-1][0], "lasty": pts[-1][1]}


# --- "Where your leads came from" split bar -----------------------------------------------------
def split_percents(split):
    """Return paid/organic counts and integer percentages (summing to ~100)."""
    paid = int((split or {}).get("paid", 0))
    organic = int((split or {}).get("organic", 0))
    total = paid + organic
    if total <= 0:
        return {"paid": paid, "organic": organic, "total": 0, "paid_pct": 0, "organic_pct": 0}
    paid_pct = int(round(paid * 100.0 / total))
    return {"paid": paid, "organic": organic, "total": total,
            "paid_pct": paid_pct, "organic_pct": 100 - paid_pct}


# --- Cross-campaign rollups (Overview) ----------------------------------------------------------
def _channel_tab(channel):
    """The tab a piece of content lives under, by channel."""
    return "leadgen" if channel == "paid" else "organic"


def awaiting_items(ws):
    """Every content piece still 'awaiting', flattened with its campaign + target tab."""
    out = []
    for camp in ws.get("campaigns", []):
        for item in camp.get("content", []):
            if item.get("status") == "awaiting":
                out.append({
                    "ref": item.get("ref", item.get("id", "")),
                    "type_tag": item.get("type_tag", ""),
                    "platform": item.get("platform", ""),
                    "channel": camp.get("channel", ""),
                    "campaign_name": camp.get("name", ""),
                    "tab": _channel_tab(camp.get("channel", "")),
                })
    return out


# --- Month calendar grid ------------------------------------------------------------------------
_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def _display_month(events, today):
    """Pick the month to show: today's month if it has events, else the earliest event's month."""
    in_today = [e for e in events if str(e.get("date", "")).startswith(today.strftime("%Y-%m"))]
    if in_today or not events:
        return today.year, today.month
    earliest = min(str(e.get("date", "")) for e in events if e.get("date"))
    return int(earliest[0:4]), int(earliest[5:7])


def _month_offset(year, month, delta):
    """Return the (year, month) that is `delta` months away from (year, month)."""
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _grid_for_month(by_date, today, year, month):
    """Build one Sunday-start month grid for (year, month) from a pre-indexed {iso: [events]} map.

    Each cell has day/in_month/is_today/is_past and the events landing on that date.
    """
    cal = _calendar.Calendar(firstweekday=6)  # 6 = Sunday
    weeks = []
    for week in cal.monthdatescalendar(year, month):
        cells = []
        for d in week:
            iso = d.isoformat()
            cell_events = by_date.get(iso, [])
            is_past = d < today
            is_today = d == today
            has_ready = any(ev.get("status") in ("ready", "done") for ev in cell_events)
            cells.append({
                "day": d.day,
                "in_month": d.month == month,
                "is_today": is_today,
                "is_past": is_past,
                "iso": iso,
                "events": cell_events,
                # Green-forward: a day is 'done' once it's past (with events) or its work is ready;
                # 'ahead' = a FUTURE day whose work is already ready/done (done in advance).
                "done": bool(cell_events) and (is_past or has_ready),
                "ahead": (not is_past) and (not is_today) and has_ready,
            })
        weeks.append(cells)
    return {"label": "%s %d" % (_MONTHS[month], year), "year": year, "month": month, "weeks": weeks}


def _index_events_by_date(events):
    """Group events into a {iso-date: [events]} map (shared by every month grid)."""
    by_date = {}
    for e in (events or []):
        by_date.setdefault(str(e.get("date", "")), []).append(e)
    return by_date


def month_grid(events, today):
    """Single relevant-month grid (today's month if it has events, else the earliest event's).

    Kept for back-compat / direct tests; the calendar tab renders `calendar_months` instead.
    """
    year, month = _display_month(events or [], today)
    return _grid_for_month(_index_events_by_date(events), today, year, month)


def calendar_months(events, today, before=1, after=1):
    """Prev / current / next month grids around TODAY's month.

    The window is anchored on the current date (not on where the events happen), so the surrounding
    months are always visible and it rolls forward on its own every month: in September you also see
    August and October; in October you see September and November. Each grid carries `is_current` so
    the template can emphasise the live month.
    """
    by_date = _index_events_by_date(events)
    grids = []
    for delta in range(-before, after + 1):
        y, m = _month_offset(today.year, today.month, delta)
        grid = _grid_for_month(by_date, today, y, m)
        grid["is_current"] = (delta == 0)
        grids.append(grid)
    return grids


def _fmt_md(d):
    """A short 'Jun 24' label for a date."""
    return "%s %d" % (_MONTHS[d.month][:3], d.day)


def milestones(events, today):
    """Chronological timeline of calendar items, each tagged done / today / upcoming.

    Mirrors the calendar's green-forward logic: an item is 'done' if its date has passed OR the team
    marked it ready/done; a future done item is 'ahead' (finished in advance).
    """
    out = []
    for e in sorted(events or [], key=lambda x: str(x.get("date", ""))):
        try:
            d = datetime.date.fromisoformat(str(e.get("date", "")))
        except ValueError:
            continue
        status = e.get("status", "planned")
        if d < today or status in ("ready", "done"):
            state = "done"
        elif d == today:
            state = "today"
        else:
            state = "upcoming"
        out.append({
            "date_label": _fmt_md(d),
            "label": e.get("label", ""),
            "kind": e.get("kind", "milestone"),
            "state": state,
            "ahead": state == "done" and d > today,
        })
    return out


def project_progress(ws, events, today):
    """Project span + 'Day X of Y' progress for the calendar header, or None if undeterminable.

    Uses ws['project'].start/end when present, else the earliest/latest calendar dates.
    """
    proj = ws.get("project") or {}
    dates = []
    for e in (events or []):
        try:
            dates.append(datetime.date.fromisoformat(str(e.get("date", ""))))
        except ValueError:
            pass

    def _parse(v):
        try:
            return datetime.date.fromisoformat(str(v)) if v else None
        except ValueError:
            return None

    start = _parse(proj.get("start")) or (min(dates) if dates else None)
    end = _parse(proj.get("end")) or (max(dates) if dates else None)
    if not start or not end or end < start:
        return None
    total = (end - start).days + 1
    day = max(0, min((today - start).days + 1, total))
    pct = max(0, min(int(round(day * 100.0 / total)) if total else 0, 100))
    return {
        "name": proj.get("name", ""),
        "start_label": _fmt_md(start),
        "end_label": _fmt_md(end),
        "day": day, "total": total, "pct": pct,
        "done": today >= end,
    }


# --- Data freshness (Overview trust strip) ------------------------------------------------------
def _relative_time(iso, now=None):
    """Human 'X ago' for an ISO-8601 timestamp, or None if absent/unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 90:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return "%d minute%s ago" % (mins, "" if mins == 1 else "s")
    hrs = mins // 60
    if hrs < 24:
        return "%d hour%s ago" % (hrs, "" if hrs == 1 else "s")
    days = hrs // 24
    return "%d day%s ago" % (days, "" if days == 1 else "s")


def data_freshness(ws, now=None):
    """Where the Overview data synced from + how long ago -- a small trust signal, not a metric.

    TODO(integrations): wire `at` to the REAL last sync once Windsor/Meta/Google ingestion lands --
    e.g. the ingest watermark or the client's _freshness.json sidecar. Until then we show a subtle
    placeholder so the strip reads as a trust signal rather than a fabricated timestamp.
    """
    sync = ws.get("sync") or {}
    return {
        "sources": sync.get("sources") or "Meta & Google",
        "label": _relative_time(sync.get("at"), now) or "2 hours ago",  # placeholder until real sync
    }


# --- Shareable recap (Overview "Share recap" link) ----------------------------------------------
def recap(ws, today):
    """A small, forwardable recap of THIS MONTH's headline results -- the SAME ROAS / leads / revenue
    and 'Wins for the week' the Overview shows. A pure dict, safe to base64 into a capability link.
    """
    metrics = ws.get("metrics", []) or []

    def _find(label):
        for m in metrics:
            if m.get("label") == label:
                return m
        return None

    headline = []
    for label in ("ROAS", "New leads", "Revenue"):
        m = _find(label)
        if m:
            headline.append({"label": m.get("label", label), "value": m.get("value", ""),
                             "trend": m.get("trend", ""), "up": bool(m.get("trend_up"))})

    # Wins: mirror the Overview exactly -- curated ws['wins'] if present, else top-3 positive KPIs.
    wins = []
    if ws.get("wins"):
        for w in ws["wins"]:
            wins.append({"title": w.get("title", ""), "detail": w.get("detail", "")})
    else:
        for m in [x for x in metrics if x.get("trend_up")][:3]:
            wins.append({"title": "%s up %s" % (m.get("label", ""), m.get("trend", "")),
                         "detail": "%s is now %s." % (m.get("label", ""), m.get("value", ""))})

    return {
        "client": ws.get("display_name") or "",
        "month": "%s %d" % (_MONTHS[today.month], today.year),
        "headline": headline,
        "wins": wins,
    }


def recap_b64(ws, today):
    """URL-safe, unpadded base64 of the recap JSON -- this rides in the share link's #fragment, so the
    recap never touches the server or any bucket; the link literally IS the data.
    """
    raw = json.dumps(recap(ws, today), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# --- The full view context ----------------------------------------------------------------------
def build(ws, client, user, active_tab, now=None):
    """Assemble the `view` dict the Atrium template needs beyond the raw workspace `ws`."""
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    today = business_today(now_dt)
    awaiting = awaiting_items(ws)
    cal_events = ws.get("calendar", [])
    return {
        "client": client,
        "active_tab": active_tab,
        "initials": initials(user),
        "greeting_name": greeting_name(user, ws),
        "spark": sparkline(ws.get("series", [])),
        "split": split_percents(ws.get("split", {})),
        "awaiting_total": len(awaiting),
        "attention": awaiting,
        "campaigns_live": len(ws.get("campaigns", [])),
        "calendar": month_grid(cal_events, today),
        "calendars": calendar_months(cal_events, today),
        "milestones": milestones(cal_events, today),
        "progress": project_progress(ws, cal_events, today),
        "freshness": data_freshness(ws, now_dt),
        "recap_b64": recap_b64(ws, today),
    }
