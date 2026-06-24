"""Website health + marketing-tag detection for the Atrium 'Website Health' tab (team-only).

`check_website(url)` fetches a URL server-side and reports (a) reachability / errors -- HTTP status,
HTTPS, redirects, response time -- and (b) the marketing/analytics tags installed on the page:
Google Tag Manager containers, GA4, Universal Analytics, Google Ads, Meta Pixel, TikTok, LinkedIn,
Hotjar, Microsoft Clarity, and friends.

Detection scans the LIVE page's returned HTML/scripts -- it does NOT call the Google Tag Manager API
-- so it needs no new infra, no credentials, and no OAuth scopes (matching Atrium's "no new infra"
rule). Reading the tag list *inside* a published GTM container would require the GTM API; that is a
deliberate, separate, opt-in step (mirroring the Drive-API strategy feature) and is intentionally not
done here. What you get is everything that actually loads on the page, which is what an operator needs
to confirm tracking is live and spot a broken/missing tag.

Pure + testable: the only side effect is the HTTP GET, and `check_website` accepts a `fetcher`
injection so unit tests run with no network. Every failure is caught and returned as a result whose
`ok` is False with a human-readable `error` -- it never raises. `requests` is imported lazily.
"""

import datetime
import re
import time

# A polite, identifiable UA so the target site's logs show who is probing it.
_UA = "Mozilla/5.0 (compatible; AgoraAtriumHealthCheck/1.0; +https://agoradatadriven.com)"


def _now_iso():
    """UTC, second precision, ISO-8601 with a trailing Z -- matching the rest of the contract."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalize_url(url):
    """Trim and ensure a scheme (default https://). Empty stays empty."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def detect_tags(html):
    """Return the marketing/analytics tags found in `html` as an ordered, de-duplicated list of
    {type, label, id} dicts (id may be "" when the tag is present but carries no readable id).

    Detection is by signature (container ids, init calls, vendor script hosts) over the returned
    markup -- so it reflects exactly what the live page loads.
    """
    html = html or ""
    found = []
    seen = set()

    def add(type_, label, id_):
        id_ = id_ or ""
        key = (type_, id_)
        if key in seen:
            return
        seen.add(key)
        found.append({"type": type_, "label": label, "id": id_})

    # Google Tag Manager containers (GTM-XXXXXX).
    for m in re.findall(r"GTM-[A-Z0-9]{4,8}", html):
        add("gtm", "Google Tag Manager", m)
    # Google Analytics 4 measurement ids (G-XXXXXXXXXX).
    for m in re.findall(r"\bG-[A-Z0-9]{8,12}\b", html):
        add("ga4", "Google Analytics 4", m)
    # Universal Analytics (legacy) UA-XXXXXX-Y.
    for m in re.findall(r"\bUA-\d{4,12}-\d{1,4}\b", html):
        add("ua", "Universal Analytics", m)
    # Google Ads conversion (AW-XXXXXX).
    for m in re.findall(r"\bAW-\d{6,15}\b", html):
        add("gads", "Google Ads", m)
    # Floodlight / Campaign Manager (DC-XXXXXX).
    for m in re.findall(r"\bDC-\d{4,12}\b", html):
        add("floodlight", "Floodlight / Campaign Manager", m)

    # Meta / Facebook Pixel -- prefer the init id, else flag its presence by host/init call.
    fb_ids = re.findall(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,20})['\"]", html)
    if fb_ids:
        for fid in fb_ids:
            add("meta", "Meta Pixel", fid)
    elif "connect.facebook.net" in html or "fbq(" in html:
        add("meta", "Meta Pixel", "")

    # TikTok Pixel.
    if "analytics.tiktok.com" in html or re.search(r"\bttq\.(load|page|track)\b", html):
        add("tiktok", "TikTok Pixel", "")

    # LinkedIn Insight Tag.
    li = re.search(r"_linkedin_partner_id\s*=\s*['\"]?(\d+)", html)
    if li:
        add("linkedin", "LinkedIn Insight Tag", li.group(1))
    elif "snap.licdn.com" in html:
        add("linkedin", "LinkedIn Insight Tag", "")

    # Hotjar.
    hj = re.search(r"hjid\s*:\s*(\d+)", html)
    if hj:
        add("hotjar", "Hotjar", hj.group(1))
    elif "static.hotjar.com" in html:
        add("hotjar", "Hotjar", "")

    # Microsoft Clarity.
    if "clarity.ms" in html:
        add("clarity", "Microsoft Clarity", "")
    # Pinterest Tag.
    if re.search(r"\bpintrk\b", html) or "s.pinimg.com" in html:
        add("pinterest", "Pinterest Tag", "")
    # Snapchat Pixel.
    if re.search(r"\bsnaptr\b", html) or "sc-static.net" in html:
        add("snapchat", "Snapchat Pixel", "")

    return found


def _requests_fetch(url, timeout):
    """Default fetcher: a single GET following redirects, with a polite UA. Imported lazily."""
    import requests  # lazy: only the live path needs it (tests inject their own fetcher)
    return requests.get(
        url,
        timeout=timeout,
        allow_redirects=True,
        headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,*/*"},
    )


def _friendly_error(exc):
    """A short, human cause for a fetch failure (timeout / DNS / SSL / connection / other)."""
    blob = ("%s %s" % (type(exc).__name__, exc)).lower()
    if "timeout" in blob or "timed out" in blob:
        return "the request timed out"
    if "ssl" in blob or "certificate" in blob:
        return "an SSL / certificate error"
    if "getaddrinfo" in blob or "name or service" in blob or "nodename" in blob or "resolve" in blob:
        return "the domain could not be found"
    if "connection" in blob or "refused" in blob:
        return "the connection failed"
    msg = str(exc).strip()
    return msg[:140] or type(exc).__name__


def check_website(url, timeout=10, fetcher=None):
    """Fetch `url` and return a render-ready health result dict (see the module docstring).

    Never raises: a network failure is recorded in the result (ok stays True at the ROUTE level --
    the check ran -- while the result's own `ok` is False so the tab shows the site is down). Only a
    missing url yields an empty result. `fetcher(url, timeout)` may be injected for tests.
    """
    norm = normalize_url(url)
    result = {
        "url": norm,
        "input_url": (url or "").strip(),
        "checked_at": _now_iso(),
        "ok": False,
        "status_code": None,
        "final_url": norm,
        "redirected": False,
        "https": norm.lower().startswith("https://"),
        "response_ms": None,
        "page_title": "",
        "error": "",
        "tags": [],
        "tag_count": 0,
        "gtm": [],
        "issues": [],
    }
    if not norm:
        result["error"] = "No website URL provided."
        result["issues"].append({"level": "error", "text": "No website URL has been set."})
        return result

    fetcher = fetcher or _requests_fetch
    t0 = time.monotonic()
    try:
        resp = fetcher(norm, timeout)
    except Exception as exc:  # noqa: BLE001 -- any fetch failure becomes a friendly result, never a 500
        result["response_ms"] = int((time.monotonic() - t0) * 1000)
        result["error"] = _friendly_error(exc)
        result["issues"].append(
            {"level": "error", "text": "Could not reach the website: %s." % result["error"]}
        )
        return result
    result["response_ms"] = int((time.monotonic() - t0) * 1000)

    status = getattr(resp, "status_code", None)
    final = getattr(resp, "url", norm) or norm
    html = getattr(resp, "text", "") or ""
    result["status_code"] = status
    result["final_url"] = final
    result["redirected"] = bool(getattr(resp, "history", None)) or (
        final.rstrip("/") != norm.rstrip("/")
    )
    result["https"] = final.lower().startswith("https://")

    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if title:
        result["page_title"] = re.sub(r"\s+", " ", title.group(1)).strip()[:140]

    tags = detect_tags(html)
    result["tags"] = tags
    result["tag_count"] = len(tags)
    result["gtm"] = [t["id"] for t in tags if t["type"] == "gtm" and t["id"]]

    # Reachability verdict + the human checklist shown under the status card.
    if status is None:
        result["issues"].append({"level": "error", "text": "No HTTP status was returned."})
    elif status >= 500:
        result["issues"].append(
            {"level": "error", "text": "Server error — the site returned HTTP %s." % status}
        )
    elif status >= 400:
        result["issues"].append(
            {"level": "error", "text": "The site returned HTTP %s (page error)." % status}
        )
    result["ok"] = status is not None and status < 400

    if result["ok"]:
        if not result["https"]:
            result["issues"].append(
                {"level": "warn", "text": "The site is not served over HTTPS."}
            )
        if result["redirected"]:
            result["issues"].append(
                {"level": "info", "text": "The URL redirects to %s." % final}
            )
        if result["response_ms"] and result["response_ms"] > 4000:
            result["issues"].append(
                {"level": "warn", "text": "Slow response (%d ms)." % result["response_ms"]}
            )
        if not result["gtm"]:
            result["issues"].append(
                {"level": "info", "text": "No Google Tag Manager container was detected on this page."}
            )
        if not tags:
            result["issues"].append(
                {"level": "warn", "text": "No marketing or analytics tags were detected."}
            )
        if not result["issues"]:
            result["issues"].append(
                {"level": "ok", "text": "Site is online and tags were detected — no problems found."}
            )
    return result
