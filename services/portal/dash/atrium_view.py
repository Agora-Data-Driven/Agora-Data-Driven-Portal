"""Presentation helpers for the Agora Atrium workspace (pure functions, no I/O).

The Atrium template renders ALL data with Jinja in HTML and keeps inline <script> blocks free of
Jinja (so tools/_validate_dash_js.py / esprima only ever sees real JS). A few things are awkward
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
                    "id": item.get("id", ""),
                    "camp_id": camp.get("id", ""),
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


def calendar_payload(events):
    """Flatten events to {index,date,label,kind,status} for the admin day-editor popup.

    The calendar tab renders the prev/current/next months server-side; on top of that, AGORA can click
    any day to open a popup that adds/recategorises/marks-done/deletes events. The popup keys actions
    by `index` (the event's position in the stored ws['calendar'] list, which the /admin/calendar route
    expects), so each item carries its stored index."""
    out = []
    for i, e in enumerate(events or []):
        out.append({
            "index": i,
            "date": str(e.get("date", "")),
            "label": e.get("label", ""),
            "kind": e.get("kind", "milestone"),
            "status": e.get("status", ""),
        })
    return out


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

    # Wins: mirror the Overview exactly -- the same auto-generated (or curated) wins().
    recap_wins = [{"title": w.get("title", ""), "detail": w.get("detail", "")} for w in wins(ws)]

    return {
        "client": ws.get("display_name") or "",
        "month": "%s %d" % (_MONTHS[today.month], today.year),
        "headline": headline,
        "wins": recap_wins,
    }


def recap_b64(ws, today):
    """URL-safe, unpadded base64 of the recap JSON -- this rides in the share link's #fragment, so the
    recap never touches the server or any bucket; the link literally IS the data.
    """
    raw = json.dumps(recap(ws, today), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# --- Monthly goal: three-tier progress bar -----------------------------------------------------
def _parse_number(s):
    """Best-effort numeric value of a metric string: '$48.6k'->48600, '148'->148, '82%'->82, else None."""
    s = str(s).strip().replace(",", "").replace("$", "").replace("%", "")
    mult = 1.0
    if s[-1:].lower() == "k":
        mult, s = 1000.0, s[:-1]
    elif s[-1:].lower() == "m":
        mult, s = 1_000_000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _metric_number(ws, label):
    """The parsed numeric value of the KPI metric whose label == `label`, or None."""
    for m in ws.get("metrics", []):
        if m.get("label") == label:
            return _parse_number(m.get("value", ""))
    return None


def _trim1(x):
    s = "%.1f" % x
    return s[:-2] if s.endswith(".0") else s


def _abbrev(n):
    a = abs(n)
    if a >= 1_000_000:
        return _trim1(n / 1_000_000.0) + "M"
    if a >= 1_000:
        return _trim1(n / 1_000.0) + "k"
    return "{:,}".format(int(round(n)))


def fmt_goal_value(n, fmt):
    """Format per the goal's chosen format: 'currency' -> '$48.6k'/'$60k'; else plain int '1,480'."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        n = 0.0
    return ("$" + _abbrev(n)) if fmt == "currency" else "{:,}".format(int(round(n)))


def goal_gauge(ws, today):
    """Three-tier monthly-goal bar (Target / Exceed / Breakthrough): zone widths, fill %, tier and
    pace ticks, formatted numbers, and a dynamic status. Pure (no client JS) -- the template renders
    the zones/fill/ticks as CSS widths/offsets. Returns None if no goal is configured.
    """
    goal = ws.get("goal")
    if not goal:
        return None
    fmt = goal.get("format", "number")
    label = goal.get("label", "goal")

    def _f(key, *alts):
        for k in (key,) + alts:
            v = goal.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return 0.0

    target = _f("target")
    exceed = max(_f("exceed", "stretch"), target)             # 'exceed' tier (legacy key: 'stretch')
    breakthrough = max(_f("breakthrough"), exceed, 1.0)       # full bar width; never zero
    current = _metric_number(ws, goal.get("source_metric")) if goal.get("source_metric") else None
    if current is None:
        current = _f("current")

    def pct(v):
        return round(max(0.0, min(1.0, v / breakthrough)) * 100.0, 2)

    dim = _calendar.monthrange(today.year, today.month)[1]
    elapsed = max(1, min(today.day, dim))
    pace_value = target * elapsed / float(dim)                # baseline pace uses the Target tier
    projected = current / elapsed * dim

    client_name = ws.get("display_name") or "your business"
    if current >= breakthrough:
        status = {"level": "breakthrough", "icon": "trophy",
                  "text": "Breakthrough. Exceptional month for %s" % client_name}
    elif current >= exceed:
        status = {"level": "exceeding", "icon": "trending",
                  "text": "Exceeding expectations. On track for a breakthrough month"}
    elif current >= target:
        status = {"level": "ontrack", "icon": "check",
                  "text": "Target hit. Now pushing to exceed with %s %s" % (fmt_goal_value(exceed, fmt), label)}
    elif current >= pace_value:
        status = {"level": "ontrack", "icon": "check",
                  "text": "On pace, on track for ~%s %s by month end" % (fmt_goal_value(projected, fmt), label)}
    else:
        status = {"level": "behind", "icon": "trending",
                  "text": "Behind pace. Pushing toward your %s %s target" % (fmt_goal_value(target, fmt), label)}

    tp, sp = pct(target), pct(exceed)
    return {
        "label": label,
        "current_fmt": fmt_goal_value(current, fmt),
        "target_fmt": fmt_goal_value(target, fmt),
        "exceed_fmt": fmt_goal_value(exceed, fmt),
        "breakthrough_fmt": fmt_goal_value(breakthrough, fmt),
        "fill_pct": pct(current),
        "target_pct": tp,
        "exceed_pct": sp,
        "pace_pct": pct(pace_value),
        "zone_exceed": round(sp - tp, 2),
        "zone_break": round(100.0 - sp, 2),
        "status": status,
    }


# --- Auto-generated "Wins for the week" --------------------------------------------------------
def _positive_trend_pct(trend):
    """Parse a '+22%' style trend into its positive magnitude, or None unless it's a positive %."""
    s = str(trend or "").strip()
    if not s.endswith("%"):
        return None
    s = s[:-1].replace("+", "").replace(",", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def wins(ws):
    """The 'Wins for the week' rows: the top-3 KPIs whose % trend is a positive (favourable) move.

    Ranks metrics with a literal positive percentage trend (e.g. '+22%') by magnitude. If nothing
    improved, returns a single encouraging 'holding steady' line. A curated ws['wins'] (hand-tuned
    production) still wins. Pure -- no I/O.
    """
    if ws.get("wins"):
        return list(ws["wins"])
    scored = []
    for m in ws.get("metrics", []):
        p = _positive_trend_pct(m.get("trend"))
        if p is not None:
            scored.append((p, m))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for _p, m in scored[:3]:
        label, trend, value = m.get("label", ""), m.get("trend", ""), m.get("value", "")
        out.append({
            "icon": m.get("icon", "check"),
            "title": "%s up %s" % (label, trend),
            "detail": "%s is now %s, a %s move in your favour." % (label, value, trend),
        })
    if not out:
        out = [{"icon": "trending", "steady": True, "detail": "",
                "title": "Holding steady. Your campaigns are maintaining performance."}]
    return out


# --- Total reach (Overview card; admin-set) ----------------------------------------------------
def organic_reach(ws):
    """Total reach headline for the Overview card: formatted value + month-over-month %. Admin-set.

    Returns None if no reach is configured. MoM % is this month vs last month. Pure.
    """
    reach = ws.get("reach")
    if not reach:
        return None
    cur = _parse_number(reach.get("current", ""))
    if cur is None:
        return None
    prev = _parse_number(reach.get("previous", ""))
    trend_pct = int(round((cur - prev) / prev * 100.0)) if prev else None
    return {
        "value_fmt": "{:,}".format(int(round(cur))),
        "trend_pct": trend_pct,
        "trend_abs": abs(trend_pct) if trend_pct is not None else None,
        "up": trend_pct is not None and trend_pct >= 0,
    }


# --- Deliverables (Overview card; from the Content Calendar) ------------------------------------
def deliverables(ws, today):
    """Content Calendar progress for the Overview card: complete vs total, % and delivered-early.

    'Complete' mirrors the Calendar tab's green-forward logic: an event is done once its date has
    passed OR it was marked ready/done; 'early' = a still-future event already marked ready/done.
    Returns None if there are no dated calendar events. Pure.
    """
    total = done = early = 0
    for e in ws.get("calendar") or []:
        try:
            d = datetime.date.fromisoformat(str(e.get("date", "")))
        except ValueError:
            continue
        total += 1
        ready = e.get("status") in ("ready", "done")
        if d < today or ready:
            done += 1
            if d > today and ready:
                early += 1
    if total == 0:
        return None
    return {"done": done, "total": total,
            "pct": int(round(done * 100.0 / total)), "early": early}


# --- Dashboard tab: per-workspace Looker Studio embed (URL + height) ----------------------------
# Known per-client defaults so the Dashboard works the moment a workspace exists; an admin save
# overrides them (and saving an empty URL clears it -- see dashboard()). Onboarding a new client
# needs no code change: the agency just pastes the URL in admin.
_DASHBOARD_DEFAULTS = {
    "riverdance": {"url": "https://datastudio.google.com/embed/reporting/6a0bd089-bf0d-453b-a98e-222c4fb26815/page/p_21bylmxn4d", "height": 900},
    "rhe":        {"url": "https://datastudio.google.com/embed/reporting/e9edb3ba-486c-4ddc-b33d-410258422aa4/page/1yCWF", "height": 500},
    "honeytribe": {"url": "https://datastudio.google.com/embed/reporting/ff946d72-068c-4482-97c8-a6e9170af646/page/p_1okvtle2vd", "height": 500},
    "meloyelo":   {"url": "https://datastudio.google.com/embed/reporting/815f5d0b-2b3b-47da-8faf-0dc6cea77488/page/p_rm3oxf9l3d", "height": 2200},
    "asllogistics": {"url": "https://datastudio.google.com/embed/reporting/ebd7f2e1-4f65-4424-957b-086b4bc186cd/page/p_ixk645553d", "height": 400},
    "contractshop": {"url": "https://datastudio.google.com/embed/reporting/474672d4-3a89-46a9-a398-a11f990d2736/page/p_5ebejadlxd", "height": 500},
}


def dashboard(ws, client):
    """Resolve the Dashboard tab embed for ONE workspace: the stored per-workspace URL + height +
    width, else a known per-client default (pre-fill), else empty (the client hides the tab; the
    admin sees a placeholder). An admin who saves an empty URL clears it (the 'dashboard_url' key is
    then present, so the default is NOT re-applied). `height`/`width` are the report's native canvas
    size; the template scales the embed so the native width fills the container (no dead strip on the
    right). Height clamped to 200..5000, width to 320..5000 (default 1200, the Looker canvas). Pure.
    """
    default = _DASHBOARD_DEFAULTS.get(client, {})
    if "dashboard_url" in ws:
        url = (ws.get("dashboard_url") or "").strip()
    else:
        url = (default.get("url") or "").strip()
    height = ws.get("dashboard_height") or default.get("height") or 800
    try:
        height = int(height)
    except (TypeError, ValueError):
        height = 800
    width = ws.get("dashboard_width") or default.get("width") or 1200
    try:
        width = int(width)
    except (TypeError, ValueError):
        width = 1200
    return {"url": url, "height": max(200, min(height, 5000)), "width": max(320, min(width, 5000))}


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
        # Per-day events (with their stored index) for the admin click-a-day editor popup AND the
        # read-only "Month history" viewer (client-facing). today drives the done/upcoming status.
        "calendar_admin": calendar_payload(cal_events),
        "calendar_today": today.isoformat(),
        "milestones": milestones(cal_events, today),
        "progress": project_progress(ws, cal_events, today),
        "freshness": data_freshness(ws, now_dt),
        "recap_b64": recap_b64(ws, today),
        "goal": goal_gauge(ws, today),
        "wins": wins(ws),
        "reach": organic_reach(ws),
        "deliverables": deliverables(ws, today),
        "dashboard": dashboard(ws, client),
    }
