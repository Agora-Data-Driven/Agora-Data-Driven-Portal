"""Live GA4 event counts for the Atrium 'Website Health' tab (team-only, OPT-IN).

`fetch_event_counts(property_id)` calls the **Google Analytics Data API** (`runReport`) for a GA4
property and returns the per-event counts over a recent window -- so the Website Health tab can show
how many times `page_view`, `session_start`, `purchase`, etc. actually fired (real numbers from
Google, NOT something the HTML tag-scan can know).

Where this differs from `atrium_health.detect_tags` (which only reads the page HTML, no infra): this
reads REAL analytics data, so it needs auth + the Data API. It is therefore a **deliberate, opt-in
deviation** from Atrium's "no new infra" rule -- it stays dormant unless `GA4_REPORTING_ENABLED=1`,
mirroring the Drive-API strategy feature and the signed-upload feature.

Keyless auth (no key file, mirroring `workspace.signed_upload_url`): the runtime SA holds
`roles/iam.serviceAccountTokenCreator` on ITSELF (granted by `enable_atrium_uploads.ps1` /
`enable_ga4_reporting.ps1`). A cloud-platform ADC token is used to call the IAM Credentials
`generateAccessToken` API, minting a short-lived `analytics.readonly`-scoped token for the Data API.
(A plain Cloud Run token is cloud-platform-scoped only, which the Data API rejects.) The SA must also
be added as a **Viewer** on each client's GA4 property (a per-client step in GA4 Admin) -- until then
the API returns 403 and this degrades to a friendly "grant access" message, never a 500.

Pure + testable: all network/auth lives in `_default_runner`; `fetch_event_counts` accepts a `runner`
injection so unit tests run with canned API JSON and no network. Every failure is caught and returned
as a result whose `ok` is False with a human-readable `error` -- it never raises. `requests` /
`google.auth` are imported lazily.
"""

import datetime
import os
import re

# The GA4 property id is the NUMERIC id (e.g. 123456789), NOT the G-XXXXXXXX measurement id the
# tag-scan finds. They are different identifiers; the Data API only accepts the numeric property id.
_PID_RE = re.compile(r"(\d{4,15})")

# The canonical funnel events shown for EVERY client (same list everywhere), in display order. The
# table always renders this full list: each event's Count comes from GA4 (0 when it never fired) and
# its Status is Active when it fired in the window, Inactive when it did not. Hand-edit this list to
# change what every client's Website Health table tracks.
TRACKED_EVENTS = [
    "page_view",
    "view_item_list",
    "view_item",
    "add_to_cart",
    "view_cart",
    "begin_checkout",
    "purchase",
    "newsletter_signup",
]

# Friendly label + one-line "what it tracks" for the common GA4 events, so the table reads in plain
# English. Unknown/custom events fall back to the raw name + a generic note (see `describe_event`).
_EVENT_INFO = {
    "page_view": ("Page view", "Someone opened or viewed a page"),
    "session_start": ("Session start", "A new visit began"),
    "first_visit": ("First visit", "A brand-new visitor's first session"),
    "user_engagement": ("Engagement", "Time spent actively on a page"),
    "scroll": ("Scroll", "A visitor scrolled to the bottom of a page"),
    "click": ("Outbound click", "An outbound link was clicked"),
    "view_item": ("Product view", "A product was viewed"),
    "view_item_list": ("Product list view", "A list of products was viewed"),
    "select_item": ("Product select", "A product was chosen from a list"),
    "view_promotion": ("Promo view", "A promotion was seen"),
    "add_to_cart": ("Add to cart", "A product was added to the cart"),
    "remove_from_cart": ("Remove from cart", "A product was removed from the cart"),
    "view_cart": ("Cart view", "The cart was viewed"),
    "begin_checkout": ("Begin checkout", "Checkout was started"),
    "add_shipping_info": ("Shipping info", "Shipping details were entered"),
    "add_payment_info": ("Payment info", "Payment details were entered"),
    "purchase": ("Purchase", "An order was completed"),
    "refund": ("Refund", "An order was refunded"),
    "search": ("Search", "A site search was run"),
    "view_search_results": ("Search results", "Search results were viewed"),
    "form_start": ("Form start", "A form was started"),
    "form_submit": ("Form submit", "A form was submitted"),
    "generate_lead": ("Lead", "A lead was generated"),
    "sign_up": ("Sign up", "Someone signed up"),
    "login": ("Login", "Someone logged in"),
    "file_download": ("File download", "A file was downloaded"),
    "video_start": ("Video start", "A video started playing"),
    "video_progress": ("Video progress", "A video reached a progress milestone"),
    "video_complete": ("Video complete", "A video finished"),
}


def is_enabled():
    """True when live GA4 reporting is switched on for this deploy (env `GA4_REPORTING_ENABLED=1`).

    Default OFF, so a standard deploy ships with no GA4 calls, no extra scopes, and no new infra.
    """
    return os.environ.get("GA4_REPORTING_ENABLED", "") == "1"


def _now_iso():
    """UTC, second precision, ISO-8601 with a trailing Z -- matching the rest of the contract."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalize_property_id(value):
    """Pull the numeric GA4 property id out of `value` ('properties/123', '123', ' 123 ' -> '123').

    Returns '' if no plausible numeric id is present (so a measurement id like G-ABC123 -> '').
    """
    value = (value or "").strip()
    if not value:
        return ""
    # Reject a measurement id outright (it starts with G-/UA-/AW-) so a paste of the wrong id is not
    # silently turned into digits-in-the-middle.
    if re.match(r"^(G-|UA-|AW-|GTM-|DC-)", value, re.I):
        return ""
    m = _PID_RE.search(value)
    return m.group(1) if m else ""


def describe_event(name):
    """(label, description) for a GA4 event name -- friendly for known events, generic otherwise."""
    info = _EVENT_INFO.get(name)
    if info:
        return info
    pretty = name.replace("_", " ").strip().capitalize() if name else "Event"
    return (pretty or "Event", "A custom event tracked on the site")


def _friendly_error(exc):
    """A short, human cause for a Data API failure (access / not-found / scope / network / other)."""
    blob = ("%s %s" % (type(exc).__name__, exc)).lower()
    if "403" in blob or "permission" in blob or "denied" in blob:
        return ("access"
                ": the service account isn't a viewer on this GA4 property yet")
    if "404" in blob or "not found" in blob:
        return "not-found: no GA4 property with that id (check the numeric property id)"
    if "scope" in blob or "insufficient" in blob:
        return "scope: the analytics token could not be minted (check the IAM setup)"
    if "timeout" in blob or "timed out" in blob:
        return "the request to Google Analytics timed out"
    msg = str(exc).strip()
    return msg[:160] or type(exc).__name__


def _default_runner(property_id, days):
    """Live path: mint an analytics-scoped token via IAM Credentials, then call the Data API.

    Keyless -- the runtime SA signs for itself (Token Creator on self). Imported lazily so only the
    live path needs `google.auth` / `requests`.
    """
    import google.auth  # lazy
    import google.auth.transport.requests
    import requests

    creds, _proj = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    sa_email = getattr(creds, "service_account_email", None)
    if not sa_email or sa_email == "default":
        raise RuntimeError("could not resolve the runtime service account email")

    # Mint a token scoped to analytics.readonly (a cloud-platform token alone is rejected by the
    # Data API). generateAccessToken impersonates the runtime SA itself (Token Creator on self).
    icr = requests.post(
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/%s:generateAccessToken"
        % sa_email,
        headers={"Authorization": "Bearer %s" % creds.token},
        json={"scope": ["https://www.googleapis.com/auth/analytics.readonly"]},
        timeout=10,
    )
    icr.raise_for_status()
    token = icr.json().get("accessToken")
    if not token:
        raise RuntimeError("no analytics token returned from generateAccessToken")

    rep = requests.post(
        "https://analyticsdata.googleapis.com/v1beta/properties/%s:runReport" % property_id,
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
        json={
            "dateRanges": [{"startDate": "%ddaysAgo" % int(days), "endDate": "today"}],
            "dimensions": [{"name": "eventName"}],
            "metrics": [{"name": "eventCount"}],
            "orderBys": [{"metric": {"metricName": "eventCount"}, "desc": True}],
            "limit": 50,
        },
        timeout=15,
    )
    rep.raise_for_status()
    return rep.json()


def _parse_rows(report):
    """Turn a Data API runReport response into [{event, label, description, count}], count-desc."""
    rows = []
    for row in (report or {}).get("rows", []) or []:
        dims = row.get("dimensionValues") or []
        mets = row.get("metricValues") or []
        if not dims or not mets:
            continue
        name = (dims[0].get("value") or "").strip()
        if not name:
            continue
        try:
            count = int(mets[0].get("value") or 0)
        except (TypeError, ValueError):
            count = 0
        label, desc = describe_event(name)
        rows.append({"event": name, "label": label, "description": desc, "count": count})
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def fetch_event_counts(property_id, days=28, runner=None):
    """Fetch per-event counts for a GA4 property over the last `days` days. Returns a render-ready dict.

    Shape: {ok, enabled, property_id, days, fetched_at, rows[], total_events, error}. Never raises:
    a disabled feature, a missing/invalid id, or any API error becomes ok:False with a human `error`.
    `runner(property_id, days)` -> raw runReport JSON may be injected for tests (bypasses the env gate,
    so unit tests run offline).
    """
    pid = normalize_property_id(property_id)
    result = {
        "ok": False,
        "enabled": is_enabled(),
        "property_id": pid,
        "days": int(days),
        "fetched_at": _now_iso(),
        "rows": [],
        "tracked": _tracked_rows({}),
        "total_events": 0,
        "error": "",
    }
    if not pid:
        result["error"] = "Enter the numeric GA4 property id (Admin -> Property Settings)."
        return result
    if runner is None and not is_enabled():
        result["error"] = "GA4 reporting is turned off for this deploy."
        return result

    runner = runner or _default_runner
    try:
        report = runner(pid, int(days))
    except Exception as exc:  # noqa: BLE001 -- any API/auth failure becomes a friendly result
        result["error"] = _friendly_error(exc)
        return result

    rows = _parse_rows(report)
    result["rows"] = rows
    result["tracked"] = _tracked_rows({r["event"]: r["count"] for r in rows})
    result["total_events"] = sum(r["count"] for r in rows)
    result["ok"] = True
    if not rows:
        result["error"] = "No events reported in this window yet."
    return result


def _tracked_rows(counts):
    """The per-client events table. Two parts, in this order:

    1. EVERY predefined funnel event (TRACKED_EVENTS), kept whether it fired or not -- because a key
       event showing Inactive is itself the signal (tracking is missing/broken). `active`/`status`
       reflect whether it fired in the window.
    2. Any OTHER event GA4 reported that ISN'T in the predefined list AND is firing -- appended so a
       live event we didn't anticipate is still surfaced (count-desc). These are always Active.

    `counts` is a {event_name: count} map from the GA4 response; an empty map yields just the
    predefined list, all Inactive (so a client with no/failed data still renders the full funnel).
    Each row carries `listed` (True for the predefined funnel, False for an added unlisted event).
    """
    counts = counts or {}
    out = []
    for event in TRACKED_EVENTS:
        count = int(counts.get(event, 0) or 0)
        out.append({
            "event": event,
            "count": count,
            "active": count > 0,
            "status": "Active" if count > 0 else "Inactive",
            "listed": True,
        })
    # Firing events beyond the predefined list, most-active first.
    extras = [(e, int(c or 0)) for e, c in counts.items()
              if e not in TRACKED_EVENTS and int(c or 0) > 0]
    extras.sort(key=lambda ec: ec[1], reverse=True)
    for event, count in extras:
        out.append({
            "event": event,
            "count": count,
            "active": True,
            "status": "Active",
            "listed": False,
        })
    return out
