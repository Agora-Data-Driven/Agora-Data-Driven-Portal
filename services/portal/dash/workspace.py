"""Agora Atrium workspace store -- per-client CRUD over `workspace/<c>.json` (no database).

Atrium is the co-branded client workspace that grows the portal into a CRM. Each client's
workspace state lives in ONE private JSON object in the portal's EXISTING bucket
(agora-data-driven-platform-dash) under the `workspace/` prefix:

    workspace/<c>.json

This mirrors store.py's load-modify-save, last-write-wins pattern, but ONE object PER CLIENT
(so two clients' workspaces never contend on the same object). No new bucket, SA, IAM, or
service: the platform-dash runtime SA already has objectAdmin on this bucket.

Storage backend (selected by env, so this is testable OFF-cloud):
  * Default -- Google Cloud Storage. `google-cloud-storage` is imported LAZILY (only when the GCS
    backend is actually used), so a local test never needs the package or ADC configured.
  * Local  -- set WORKSPACE_LOCAL_DIR=<dir> to read/write plain JSON files under that directory
    instead of GCS. This lets you develop and smoke-test on a laptop WITHOUT touching the real
    bucket (see seed_workspace.py / _workspace_localtest.py).

Env overrides (all optional; the defaults are the literal standup values):
  * WORKSPACE_BUCKET  -- bucket to use (defaults to REGISTRY_BUCKET, the portal's private bucket).
  * WORKSPACE_PREFIX  -- object-name prefix (default "workspace/").
  * WORKSPACE_LOCAL_DIR -- if set, use the local-filesystem backend rooted at this directory.

All timestamps are UTC ISO-8601 with a trailing Z, matching feedback.py / the freshness contract.
"""

import datetime
import json
import os
import re
import uuid


# --- Config (read live from the env so tests can set it before the first call) ------------------
def _local_dir():
    """The local-filesystem backend root, or "" to use GCS."""
    return os.environ.get("WORKSPACE_LOCAL_DIR", "")


def _bucket_name():
    """The bucket holding workspace/<c>.json -- defaults to the portal's private registry bucket."""
    return (
        os.environ.get("WORKSPACE_BUCKET")
        or os.environ.get("REGISTRY_BUCKET")
        or "agora-data-driven-platform-dash"
    )


def _prefix():
    """Object-name prefix for workspace objects (keeps them grouped in the shared bucket)."""
    return os.environ.get("WORKSPACE_PREFIX", "workspace/")


def _object_name(client):
    """The object name for a client's workspace, e.g. 'workspace/riverdance.json'."""
    return "%s%s.json" % (_prefix(), client)


# --- Timestamp helpers (UTC, matching the rest of the contract) ---------------------------------
def now_iso():
    """UTC, second precision, ISO-8601 with a trailing Z (e.g. '2026-06-20T09:12:00Z')."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def now_label():
    """A friendly activity label like 'Today, 9:12 AM' (UTC clock)."""
    t = datetime.datetime.now(datetime.timezone.utc)
    return "Today, " + t.strftime("%I:%M %p").lstrip("0")


# --- Storage backend (GCS by default; local filesystem when WORKSPACE_LOCAL_DIR is set) ---------
_storage_client = None


def _gcs_client():
    """Lazily construct and cache a GCS client (so importing this module never needs ADC)."""
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage  # lazy: only the GCS backend needs the package
        _storage_client = storage.Client()
    return _storage_client


def _read_object(name):
    """Return the raw bytes of object `name`, or None if it does not exist."""
    local = _local_dir()
    if local:
        path = os.path.join(local, name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    if not blob.exists():
        return None
    return blob.download_as_bytes()


def _write_object(name, data, content_type="application/json"):
    """Write `data` (bytes) to object `name`, creating parent dirs for the local backend.

    `content_type` defaults to JSON (the workspace objects); pass an image mime when storing an
    uploaded creative so the GCS blob carries the right Content-Type.
    """
    local = _local_dir()
    if local:
        path = os.path.join(local, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    blob.upload_from_string(data, content_type=content_type)


def _delete_object(name):
    """Delete object `name` if it exists (no error if it is already gone)."""
    local = _local_dir()
    if local:
        path = os.path.join(local, name)
        if os.path.isfile(path):
            os.remove(path)
        return
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    if blob.exists():
        blob.delete()


# --- Workspace I/O ------------------------------------------------------------------------------
def load_workspace(client):
    """Return the workspace dict for `client`, or None if it has not been seeded yet."""
    raw = _read_object(_object_name(client))
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


def save_workspace(client, ws):
    """Persist the workspace dict back to workspace/<c>.json (private; never made public)."""
    body = json.dumps(ws, indent=2, sort_keys=True).encode("utf-8")
    _write_object(_object_name(client), body)
    return ws


def workspace_exists(client):
    """True iff a workspace object already exists for `client` (used by the seed clobber-guard)."""
    return _read_object(_object_name(client)) is not None


def delete_workspace(client):
    """Delete a client's workspace JSON object (no error if absent). Used when removing a client.

    Only removes the workspace document itself; any uploaded creatives live under their own
    'workspace/creatives/<client>/' prefix and are left for a separate sweep (the bucket is private,
    so orphaned objects are inert -- never publicly reachable)."""
    _delete_object(_object_name(client))


def set_client_logo(client, logo_markup):
    """Replace the client's logo (brand.client_logo) with `logo_markup`, leaving everything else
    untouched. `logo_markup` is self-contained HTML/SVG (e.g. an <img> data: URI) rendered with
    |safe in the workspace + team console. Returns the new markup. Raises KeyError if no workspace."""
    def fn(ws):
        ws.setdefault("brand", {})["client_logo"] = logo_markup
        return ws["brand"]["client_logo"]
    return _mutate(client, fn)


# --- Website Health (team-only tab: site monitoring + tag detection) -----------------------------
# All state lives under one key, ws["website_health"] = {url, notes, last_check}. The last_check dict
# is the render-ready result from atrium_health.check_website (kept verbatim so the tab renders it).
def set_website_url(client, url):
    """Set the monitored website URL for the Website Health tab. Returns the stored url."""
    def fn(ws):
        wh = ws.setdefault("website_health", {})
        wh["url"] = (url or "").strip()
        return wh["url"]
    return _mutate(client, fn)


def set_website_notes(client, notes):
    """Set the team's free-text notes shown on the Website Health tab. Returns the stored notes."""
    def fn(ws):
        wh = ws.setdefault("website_health", {})
        wh["notes"] = notes or ""
        return wh["notes"]
    return _mutate(client, fn)


def save_website_check(client, result):
    """Store the latest health-check result (and the url it ran against). Returns the result."""
    def fn(ws):
        wh = ws.setdefault("website_health", {})
        if (result or {}).get("url"):
            wh["url"] = result["url"]
        wh["last_check"] = result or {}
        return wh["last_check"]
    return _mutate(client, fn)


# --- Uploaded creatives (binary objects in the SAME private bucket) -----------------------------
# A creative the team uploads for a content piece is stored as its OWN object alongside the
# workspace JSON (so a multi-KB image never bloats workspace/<c>.json, which is rewritten in full on
# every edit). The object stays private; it is only ever served through the authed proxy route in
# main.py (mirroring the /data.json posture -- buckets are never made public).
def creative_object_name(client, content_id):
    """Object name for a content piece's uploaded creative, e.g. 'workspace/creatives/riverdance/RVR-016'."""
    return "%screatives/%s/%s" % (_prefix(), client, content_id)


def write_creative(client, content_id, data, content_type="application/octet-stream"):
    """Store the uploaded creative bytes for a content piece. Returns the object name."""
    name = creative_object_name(client, content_id)
    _write_object(name, data, content_type=content_type)
    return name


def read_creative_bytes(client, content_id):
    """Return the raw bytes of a content piece's uploaded creative, or None if there is none."""
    return _read_object(creative_object_name(client, content_id))


def delete_creative(client, content_id):
    """Delete a content piece's uploaded creative object (no error if absent)."""
    _delete_object(creative_object_name(client, content_id))


# --- Multiple images per content piece (the approval ticket's picture row) ----------------------
# A content piece can carry SEVERAL images alongside, or instead of, the single legacy creative
# above. Each image is its OWN private object under a distinct '<content_id>.img/' prefix (so it
# never collides with the legacy single object at '<content_id>'); the workspace JSON records a
# small `images: [{id, mime}]` list -- never the bytes. Served only through the authed proxy route.
def creative_image_object_name(client, content_id, image_id):
    """Object name for ONE image, e.g. 'workspace/creatives/riverdance/RVR-016.img/img_ab12'."""
    return "%screatives/%s/%s.img/%s" % (_prefix(), client, content_id, image_id)


def add_content_image(client, content_id, image_id, data, mime, name=""):
    """Store one attached file (private object) and append {id, mime, name} to the piece's `images`
    list. Any file type is accepted -- images/videos render inline, others as a download chip; `name`
    is the original filename, used to label/download non-media files."""
    _write_object(creative_image_object_name(client, content_id, image_id), data,
                  content_type=mime or "application/octet-stream")

    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item.setdefault("images", []).append({"id": image_id, "mime": mime or "", "name": name or ""})
        return item["images"]
    return _mutate(client, fn)


def read_content_image_bytes(client, content_id, image_id):
    """Raw bytes of one image, or None if it does not exist."""
    return _read_object(creative_image_object_name(client, content_id, image_id))


def remove_content_image(client, content_id, image_id):
    """Delete one image (object + pointer). Returns the remaining list, or None if absent."""
    _delete_object(creative_image_object_name(client, content_id, image_id))

    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            return None
        item["images"] = [im for im in item.get("images", []) if im.get("id") != image_id]
        return item["images"]
    return _mutate(client, fn)


def signed_upload_url(client, content_id, mime, ttl_minutes=15):
    """A V4 signed PUT URL so the browser uploads a creative DIRECTLY to GCS, bypassing the app's
    request-size cap (Cloud Run caps requests at ~32 MiB; GCS has no such limit).

    Returns (url, object_name). On the local-fs backend (no GCS), returns (None, object_name) -- the
    caller falls back to the in-app upload route. Signing uses the runtime SA via the IAM signBlob
    API (the SA holds roles/iam.serviceAccountTokenCreator on itself), so NO key file is needed.
    """
    name = creative_object_name(client, content_id)
    if _local_dir():
        return None, name
    import google.auth  # lazy; only the GCS signing path needs these
    import google.auth.transport.requests
    # The signBlob IAM call needs a CLOUD-PLATFORM-scoped token; the storage client's default token is
    # storage-scoped only (otherwise: ACCESS_TOKEN_SCOPE_INSUFFICIENT). Mint a cloud-platform token
    # from the runtime SA via ADC and sign with it -- keyless (the SA holds Token Creator on itself).
    creds, _proj = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=ttl_minutes),
        method="PUT",
        content_type=mime,
        service_account_email=getattr(creds, "service_account_email", None),
        access_token=creds.token,
    )
    return url, name


def creative_size(client, content_id):
    """Byte size of a content piece's uploaded creative, or None if it does not exist."""
    name = creative_object_name(client, content_id)
    local = _local_dir()
    if local:
        path = os.path.join(local, name)
        return os.path.getsize(path) if os.path.isfile(path) else None
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    if not blob.exists():
        return None
    blob.reload()
    return blob.size


def read_creative_range(client, content_id, start, end):
    """Return bytes [start, end] INCLUSIVE of a creative -- so the serve route can stream/seek video
    without loading the whole object into memory."""
    name = creative_object_name(client, content_id)
    local = _local_dir()
    if local:
        with open(os.path.join(local, name), "rb") as fh:
            fh.seek(start)
            return fh.read(end - start + 1)
    return _gcs_client().bucket(_bucket_name()).blob(name).download_as_bytes(start=start, end=end)


def stream_creative(client, content_id, start, end, chunk_size=262144):
    """Yield bytes [start, end] INCLUSIVE in chunks, so a large creative streams to the client
    without ever loading the whole object into memory (used by the serve route for video)."""
    name = creative_object_name(client, content_id)
    local = _local_dir()
    if local:
        # Local-fs is the dev/test backend; read the slice and CLOSE the file before yielding so no
        # OS handle lingers across the stream (Windows won't delete a file with an open handle). Prod
        # is GCS (below): chunked network reads, bounded memory, no local file handle.
        with open(os.path.join(local, name), "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start + 1)
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]
        return
    # GCS: one seekable download stream (blob.open internally range-fetches), NOT one HTTP GET per
    # chunk -- so a large video streams over a single connection with bounded memory.
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    with blob.open("rb") as reader:
        if start:
            reader.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            buf = reader.read(min(chunk_size, remaining))
            if not buf:
                break
            remaining -= len(buf)
            yield buf


def _mutate(client, fn):
    """Load -> apply `fn(ws)` -> save (last-write-wins). Returns whatever `fn` returns.

    Raises KeyError if the client has no workspace yet. Each client's workspace is its own object,
    so this read-modify-write only races with concurrent writes to the SAME client (acceptable for
    the low write volume here); cross-client edits never contend.
    """
    ws = load_workspace(client)
    if ws is None:
        raise KeyError("no workspace for client '%s'" % client)
    result = fn(ws)
    save_workspace(client, ws)
    return result


# --- Lookups ------------------------------------------------------------------------------------
def _find_content(ws, content_id):
    """Return (campaign, content) for `content_id` across all campaigns, or (None, None)."""
    for camp in ws.get("campaigns", []):
        for item in camp.get("content", []):
            if item.get("id") == content_id:
                return camp, item
    return None, None


def _find_campaign(ws, campaign_id):
    for camp in ws.get("campaigns", []):
        if camp.get("id") == campaign_id:
            return camp
    return None


def _find_conversation(ws, conversation_id):
    for conv in ws.get("conversations", []):
        if conv.get("id") == conversation_id:
            return conv
    return None


def _new_id(prefix):
    """A short, collision-resistant id like 'cv_1a2b3c4d'."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


# --- Content review (client-facing approve / request-changes / note) ----------------------------
def decide_content(client, content_id, status, note=None):
    """Set a content piece's review status and stamp the decision time. Returns the content dict.

    `status` is "approved" or "changes". An optional `note` (the client's recommendation) is saved
    alongside the decision when provided.
    """
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item["status"] = status
        item["decided_at"] = now_iso()
        if note is not None:
            item["client_note"] = note
        return item
    return _mutate(client, fn)


def set_content_note(client, content_id, note):
    """Persist the client's recommendation note on a content piece. Returns the content dict."""
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item["client_note"] = note or ""
        return item
    return _mutate(client, fn)


# --- Conversations ------------------------------------------------------------------------------
def add_message(client, conversation_id, sender, sender_name, body, set_status=None, created_at=None):
    """Append a message to a conversation. Returns (conversation, message).

    `sender` is "client" or "agora". When `set_status` is given the thread's status is updated
    (e.g. a client message moves a thread to 'awaiting_reply').
    """
    def fn(ws):
        conv = _find_conversation(ws, conversation_id)
        if conv is None:
            raise KeyError("no conversation '%s'" % conversation_id)
        message = {
            "sender": sender,
            "sender_name": sender_name or "",
            "body": body or "",
            "created_at": created_at or now_iso(),
        }
        conv.setdefault("messages", []).append(message)
        if set_status:
            conv["status"] = set_status
        return conv, message
    return _mutate(client, fn)


def set_conversation_status(client, conversation_id, status):
    """Set a conversation's status ('awaiting_reply' or 'resolved'). Returns the conversation."""
    def fn(ws):
        conv = _find_conversation(ws, conversation_id)
        if conv is None:
            raise KeyError("no conversation '%s'" % conversation_id)
        conv["status"] = status
        return conv
    return _mutate(client, fn)


def add_conversation(client, subject, status="awaiting_reply", conversation_id=None):
    """Start a new conversation thread (team-facing). Returns the conversation dict."""
    def fn(ws):
        conv = {
            "id": conversation_id or _new_id("cv"),
            "subject": subject or "(no subject)",
            "status": status,
            "messages": [],
        }
        ws.setdefault("conversations", []).append(conv)
        return conv
    return _mutate(client, fn)


# --- Notification preferences (per logged-in user, keyed by email) ------------------------------
def default_notify():
    """Default notification prefs: on for master/content/changes/replies/summary, off for status/news."""
    return {
        "master": True,
        "content": True,
        "changes": True,
        "replies": True,
        "summary": True,
        "status": False,
        "news": False,
        "frequency": "instant",
    }


def get_notify(ws, user_email):
    """Return `user_email`'s notification prefs with defaults applied (never None)."""
    merged = default_notify()
    stored = (ws.get("notify") or {}).get(user_email)
    if stored:
        merged.update(stored)
    return merged


def set_notify(client, user_email, prefs):
    """Merge `prefs` into `user_email`'s notification settings and persist. Returns the merged dict."""
    def fn(ws):
        notify = ws.setdefault("notify", {})
        current = default_notify()
        if notify.get(user_email):
            current.update(notify[user_email])
        if prefs:
            current.update(prefs)
        notify[user_email] = current
        return current
    return _mutate(client, fn)


# --- Activity feed (Recent activity panel) ------------------------------------------------------
def add_activity(client, icon, text, time_label=None, limit=40):
    """Prepend an entry to the client's 'Recent activity' feed (most-recent first). Returns it.

    Capped at `limit` entries so the workspace object cannot grow without bound.
    """
    def fn(ws):
        entry = {"icon": icon or "bell", "text": text or "", "time_label": time_label or now_label()}
        activity = ws.setdefault("activity", [])
        activity.insert(0, entry)
        del activity[limit:]
        return entry
    return _mutate(client, fn)


# --- Team management: metrics / campaigns / content / calendar ----------------------------------
def set_metrics(client, metrics):
    """Replace the KPI metrics list (team-facing). Returns the metrics list."""
    def fn(ws):
        ws["metrics"] = list(metrics or [])
        return ws["metrics"]
    return _mutate(client, fn)


def set_goal(client, goal):
    """Store the per-client Monthly goal (label/format/target/exceed/breakthrough/current/
    source_metric; legacy 'stretch' is read as 'exceed'). Period is DERIVED at render time, never
    stored. Returns the goal dict."""
    def fn(ws):
        ws["goal"] = dict(goal or {})
        return ws["goal"]
    return _mutate(client, fn)


def set_reach(client, current, previous):
    """Store the per-client Total reach headline (this month + last month) shown on the Overview card."""
    def fn(ws):
        ws["reach"] = {"current": current, "previous": previous}
        return ws["reach"]
    return _mutate(client, fn)


def set_display_name(client, name):
    """Update the workspace's display name in place (a client rename), leaving all other content
    untouched. Returns the new name."""
    def fn(ws):
        ws["display_name"] = name
        return ws["display_name"]
    return _mutate(client, fn)


def set_dashboard_url(client, url, height=None, width=None):
    """Set the per-client Looker Studio embed URL (empty string hides the dashboard from the client)
    and, optionally, the report's native height + width in px. All read by atrium_view.dashboard().
    Width is the report's native canvas width; the embed scales to fill the container preserving
    aspect (see the Dashboard tab in atrium.html), so it no longer leaves a dead strip on the right."""
    def fn(ws):
        ws["dashboard_url"] = (url or "").strip()
        if height is not None:
            try:
                ws["dashboard_height"] = int(height)
            except (TypeError, ValueError):
                pass
        if width is not None:
            try:
                ws["dashboard_width"] = int(width)
            except (TypeError, ValueError):
                pass
        return ws["dashboard_url"]
    return _mutate(client, fn)


def set_overview_counts(client, today=None, split=None, series=None):
    """Update the headline counts used by Overview/Dashboard. Returns the workspace dict."""
    def fn(ws):
        if today is not None:
            ws["today"] = today
        if split is not None:
            ws["split"] = split
        if series is not None:
            ws["series"] = list(series)
        return ws
    return _mutate(client, fn)


def add_campaign(client, channel, name, eyebrow="", strategy=None, ai_summary="", campaign_id=None,
                 strategy_doc=""):
    """Add a campaign (team-facing). `channel` is 'paid' or 'organic'. Returns the campaign dict."""
    def fn(ws):
        camp = {
            "id": campaign_id or _new_id("cmp"),
            "channel": channel,
            "name": name or "(untitled campaign)",
            "eyebrow": eyebrow or "",
            "strategy": strategy or {"what": "", "why": ""},
            "ai_summary": ai_summary or "",
            "strategy_doc": strategy_doc or "",
            "content": [],
        }
        ws.setdefault("campaigns", []).append(camp)
        return camp
    return _mutate(client, fn)


def update_campaign(client, campaign_id, name=None, eyebrow=None, strategy=None, ai_summary=None,
                    channel=None, strategy_doc=None):
    """Edit a campaign's name / eyebrow / strategy / AI summary / channel / strategy doc. Returns it."""
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        if name is not None:
            camp["name"] = name
        if eyebrow is not None:
            camp["eyebrow"] = eyebrow
        if strategy is not None:
            camp["strategy"] = strategy
        if ai_summary is not None:
            camp["ai_summary"] = ai_summary
        if channel is not None:
            camp["channel"] = channel
        if strategy_doc is not None:
            camp["strategy_doc"] = strategy_doc
        return camp
    return _mutate(client, fn)


def set_strategy_doc(client, campaign_id, doc_url):
    """Attach (or clear) the Google Doc URL backing a campaign's AI summary. Returns the campaign."""
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        camp["strategy_doc"] = doc_url or ""
        return camp
    return _mutate(client, fn)


def delete_campaign(client, campaign_id):
    """Remove a campaign (and its content) from the workspace. Returns the removed campaign or None."""
    def fn(ws):
        camps = ws.get("campaigns", [])
        for i, camp in enumerate(camps):
            if camp.get("id") == campaign_id:
                return camps.pop(i)
        raise KeyError("no campaign '%s'" % campaign_id)
    return _mutate(client, fn)


def insert_campaign(client, campaign):
    """Re-insert a previously-removed campaign verbatim (Trash restore). Returns it.

    Appends the raw campaign dict back (its content, strategy, etc. intact) and re-mirrors any dated
    content onto the Content Calendar. Idempotent on the campaign id (won't duplicate)."""
    def fn(ws):
        c = dict(campaign or {})
        camps = ws.setdefault("campaigns", [])
        if any(x.get("id") == c.get("id") for x in camps):
            return c  # already present -- don't duplicate on a double-restore
        camps.append(c)
        for item in c.get("content", []):
            _sync_content_calendar(ws, c, item)
        return c
    return _mutate(client, fn)


def _content_event_kind(camp):
    """The content calendar 'kind' for a piece, derived from its campaign channel: a paid/lead-gen
    campaign mirrors as a 'paid' event, anything else as 'organic'."""
    return "paid" if (camp or {}).get("channel") == "paid" else "organic"


def _sync_content_calendar(ws, camp, item):
    """Keep a content piece's mirrored calendar event in step with the piece (called on add/edit).

    A content piece with a `date` shows up on the Content Calendar as a linked event (the event
    carries `content_id` back to the piece, plus the `tab` it lives under so the day-popup arrow can
    jump straight to it). The content piece is the source of truth for the event's date/label/kind/
    tab -- editing the piece OVERWRITES those on the linked event -- but the calendar keeps its own
    `status` (mark-as-done) untouched. A piece with no date has no event (any prior one is removed).
    """
    cid = item.get("id")
    if cid is None:
        return
    cal = ws.setdefault("calendar", [])
    existing = next((e for e in cal if e.get("content_id") == cid), None)
    date = str(item.get("date", "") or "").strip()
    if not date:
        if existing is not None:
            cal.remove(existing)
        return
    kind = _content_event_kind(camp)
    tab = "leadgen" if kind == "paid" else "organic"
    label = item.get("ref") or item.get("type_tag") or "Content"
    if existing is not None:
        existing["date"] = date
        existing["label"] = label
        existing["kind"] = kind
        existing["tab"] = tab
        existing["campaign_id"] = (camp or {}).get("id")
    else:
        cal.append({
            "date": date, "label": label, "kind": kind,
            "content_id": cid, "campaign_id": (camp or {}).get("id"), "tab": tab,
        })


def add_content(client, campaign_id, content):
    """Add a content piece to a campaign (team-facing); forces status 'awaiting'. Returns it.

    `content` is a dict of the content fields (ref, type_tag, sub_tag, platform, caption, date, etc.).
    Missing id/ref are generated; status is always reset to 'awaiting' for a fresh review. If the
    piece carries a `date`, it is also mirrored onto the Content Calendar as a linked event.
    """
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        item = dict(content or {})
        item.setdefault("id", _new_id("cnt"))
        # The id drives EVERY per-piece DOM hook, route, and JS selector. The add route derives it
        # from the human title (ref), so two pieces sharing a title would collide -- and a duplicate
        # id makes the second piece impossible to open/edit (the modal/card selector always resolves
        # to the FIRST match). Guarantee uniqueness across ALL campaigns, suffixing on a clash.
        existing = {it.get("id") for c in ws.get("campaigns", []) for it in c.get("content", [])}
        if item["id"] in existing:
            base, n = item["id"], 2
            while ("%s-%d" % (base, n)) in existing:
                n += 1
            item["id"] = "%s-%d" % (base, n)
        item.setdefault("ref", item["id"])
        item["status"] = "awaiting"
        item.setdefault("client_note", "")
        item.setdefault("decided_at", "")
        item.setdefault("comments", [])
        camp.setdefault("content", []).append(item)
        _sync_content_calendar(ws, camp, item)
        return item
    return _mutate(client, fn)


def update_content(client, content_id, fields):
    """Patch fields on an existing content piece (team-facing). Returns the content dict.

    If the patch touches the piece's date/title (or it already carries a date), the mirrored Content
    Calendar event is re-synced so the calendar always reflects the piece.
    """
    def fn(ws):
        camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item.update(fields or {})
        _sync_content_calendar(ws, camp, item)
        return item
    return _mutate(client, fn)


def delete_content(client, content_id):
    """Remove a content piece from whatever campaign holds it (and its mirrored calendar event, if
    any). Returns the removed piece.

    Note: the caller is responsible for deleting any uploaded creative object via delete_creative().
    """
    def fn(ws):
        for camp in ws.get("campaigns", []):
            items = camp.get("content", [])
            for i, item in enumerate(items):
                if item.get("id") == content_id:
                    removed = items.pop(i)
                    ws["calendar"] = [e for e in ws.get("calendar", [])
                                      if e.get("content_id") != content_id]
                    return removed
        raise KeyError("no content '%s'" % content_id)
    return _mutate(client, fn)


def insert_content(client, campaign_id, content):
    """Re-insert a previously-removed content piece verbatim into its campaign (Trash restore).

    Restores the piece as it was (status/comments/date preserved) and re-mirrors its calendar event
    if it had a date. Raises KeyError if the campaign no longer exists (e.g. it was deleted too --
    restore the campaign instead). Idempotent on the content id."""
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        items = camp.setdefault("content", [])
        c = dict(content or {})
        if any(x.get("id") == c.get("id") for x in items):
            return c
        items.append(c)
        _sync_content_calendar(ws, camp, c)
        return c
    return _mutate(client, fn)


def add_content_comment(client, content_id, sender, sender_name, body, created_at=None,
                        kind="comment", set_status=None):
    """Append a threaded comment to a content piece. Returns (content, comment).

    `sender` is "client" or "agora". `kind` is "comment" (default) or "changes" — a "Request changes"
    comment, rendered as a flagged light-red bubble. When `set_status` is given (e.g. "changes"), the
    piece's review status + decided_at are stamped in the SAME write, so requesting changes through
    the comment thread also records the decision atomically. Comments are an ongoing discussion on a
    creative, separate from the one-shot `client_note`.
    """
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        comment = {
            "id": _new_id("cm"),
            "sender": sender,
            "sender_name": sender_name or "",
            "body": body or "",
            "created_at": created_at or now_iso(),
            "kind": kind or "comment",
        }
        if (kind or "comment") == "changes":
            comment["resolved"] = False
        item.setdefault("comments", []).append(comment)
        if set_status:
            item["status"] = set_status
            item["decided_at"] = now_iso()
        return item, comment
    return _mutate(client, fn)


def resolve_content_comment(client, content_id, comment_id):
    """Mark a "Request changes" comment resolved. Returns (content, comment, status).

    When the piece has no remaining UNRESOLVED changes-comments and its status is still 'changes', it
    returns to 'awaiting' (back in the review queue). Raises KeyError if the piece or comment is gone.
    """
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        target = next((c for c in item.get("comments", []) if c.get("id") == comment_id), None)
        if target is None:
            raise KeyError("no comment '%s'" % comment_id)
        target["resolved"] = True
        unresolved = [c for c in item.get("comments", [])
                      if c.get("kind") == "changes" and not c.get("resolved")]
        if not unresolved and item.get("status") == "changes":
            item["status"] = "awaiting"
            item["decided_at"] = now_iso()
        return item, target, item.get("status")
    return _mutate(client, fn)


def delete_content_comment(client, content_id, comment_id):
    """Remove a single comment from a content piece's thread. Returns (content, status).

    Mirrors `resolve_content_comment`: if deleting the comment leaves no remaining UNRESOLVED
    changes-comments and the piece is still 'changes', it returns to 'awaiting' (back in the review
    queue). Raises KeyError if the piece or comment is gone.
    """
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        comments = item.get("comments", [])
        target = next((c for c in comments if c.get("id") == comment_id), None)
        if target is None:
            raise KeyError("no comment '%s'" % comment_id)
        item["comments"] = [c for c in comments if c.get("id") != comment_id]
        unresolved = [c for c in item["comments"]
                      if c.get("kind") == "changes" and not c.get("resolved")]
        if not unresolved and item.get("status") == "changes":
            item["status"] = "awaiting"
            item["decided_at"] = now_iso()
        return item, item.get("status")
    return _mutate(client, fn)


def set_content_image(client, content_id, object_name, mime):
    """Record that a content piece now has an uploaded creative (object name + mime). Returns it."""
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item["image_object"] = object_name
        item["image_mime"] = mime or "application/octet-stream"
        return item
    return _mutate(client, fn)


def clear_content_image(client, content_id):
    """Forget a content piece's uploaded creative (does NOT delete the object). Returns the piece."""
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item.pop("image_object", None)
        item.pop("image_mime", None)
        return item
    return _mutate(client, fn)


def add_calendar_event(client, date, label, kind):
    """Append a calendar event ('paid'|'organic'|'due'|'milestone'). Returns it."""
    def fn(ws):
        event = {"date": date, "label": label or "", "kind": kind or "milestone"}
        ws.setdefault("calendar", []).append(event)
        return event
    return _mutate(client, fn)


def edit_calendar_event(client, index, date, label, kind):
    """Edit the calendar event at `index` (date/label/kind) in place. Returns it, or None if out of range.
    A blank date or kind is ignored (the existing value is kept); the label is set as given (may be empty)."""
    def fn(ws):
        events = ws.get("calendar", [])
        if 0 <= index < len(events):
            event = events[index]
            if date:
                event["date"] = date
            event["label"] = label or ""
            if kind:
                event["kind"] = kind
            return event
        return None
    return _mutate(client, fn)


def delete_calendar_event(client, index):
    """Remove the calendar event at `index` (as ordered in the stored list). Returns it, or None."""
    def fn(ws):
        events = ws.get("calendar", [])
        if 0 <= index < len(events):
            return events.pop(index)
        return None
    return _mutate(client, fn)


def insert_calendar_event(client, event):
    """Re-insert a previously-removed calendar event verbatim (Trash restore). Returns it.

    Only used to restore PERSONAL (non-content) events -- a content-linked event is owned by its
    piece and is recreated by restoring the content, so this never re-adds a content-linked one."""
    def fn(ws):
        ev = dict(event or {})
        ws.setdefault("calendar", []).append(ev)
        return ev
    return _mutate(client, fn)


def set_calendar_status(client, index, status):
    """Set or clear a calendar event's status (e.g. 'done'/'ready') at `index`. An empty status clears
    it. The calendar view treats a 'done'/'ready' event as accomplished (green ✓, 'ahead' if future).
    Returns the updated event, or None if the index is out of range."""
    def fn(ws):
        events = ws.get("calendar", [])
        if 0 <= index < len(events):
            if status:
                events[index]["status"] = status
            else:
                events[index].pop("status", None)
            return events[index]
        return None
    return _mutate(client, fn)


# --- Client Communications: email + meeting summaries (team-written, client-read) ---------------
def add_email_summary(client, subject, summary, date=None, email_id=None):
    """Add an email summary (newest first) to the Client Communications tab. Returns it."""
    def fn(ws):
        item = {
            "id": email_id or _new_id("em"),
            "subject": subject or "(no subject)",
            "date": date or now_iso(),
            "summary": summary or "",
        }
        ws.setdefault("email_summaries", []).insert(0, item)
        return item
    return _mutate(client, fn)


def add_meeting_summary(client, title, summary, attendees="", date=None, meeting_id=None):
    """Add a meeting summary / notes (newest first) to the Client Communications tab. Returns it."""
    def fn(ws):
        item = {
            "id": meeting_id or _new_id("mt"),
            "title": title or "(untitled meeting)",
            "date": date or now_iso(),
            "attendees": attendees or "",
            "summary": summary or "",
        }
        ws.setdefault("meeting_summaries", []).insert(0, item)
        return item
    return _mutate(client, fn)


def delete_communication(client, kind, item_id):
    """Delete an email ('email') or meeting ('meeting') summary by id. Returns the remaining list."""
    key = "email_summaries" if kind == "email" else "meeting_summaries"
    def fn(ws):
        ws[key] = [it for it in ws.get(key, []) if it.get("id") != item_id]
        return ws[key]
    return _mutate(client, fn)


def update_communication(client, kind, item_id, fields):
    """Edit an email/meeting summary's fields in place by id. Email accepts subject/summary; meeting
    accepts title/attendees/summary. Returns the updated item, or None if not found."""
    key = "email_summaries" if kind == "email" else "meeting_summaries"
    allowed = ("subject", "summary") if kind == "email" else ("title", "attendees", "summary")
    def fn(ws):
        for it in ws.get(key, []):
            if it.get("id") == item_id:
                for k in allowed:
                    if k in (fields or {}):
                        it[k] = fields[k]
                return it
        return None
    return _mutate(client, fn)


# --- Market Intelligence: the weekly briefing (team-written, client-read) -----------------------
# A team-curated briefing the client reads, split into two sections that each hold a list of
# entries (newest first). One key, ws["intel"] = {"business_research": [...], "media_buying": [...]}.
# An entry is {id, heading, title, body, source, link, date} -- mirroring the "Weekly Intelligence
# Report" shape (a sub-heading + headline + paragraph + a source tag/link). Same load-modify-save
# posture as the Client Communications summaries above; no new infra.
INTEL_SECTIONS = ("business_research", "media_buying")
_INTEL_FIELDS = ("heading", "title", "body", "source", "link", "date")


def _intel_key(section):
    """Canonical intel-section key, or None if `section` is not one of the two valid sections."""
    return section if section in INTEL_SECTIONS else None


def add_intel_entry(client, section, entry, entry_id=None):
    """Add a Market Intelligence entry (newest first) to `section`. Returns the entry.

    `section` is 'business_research' or 'media_buying'; an unknown section raises KeyError. `entry`
    is a dict of any of the intel fields (heading/title/body/source/link/date); missing ones default
    to empty strings."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def fn(ws):
        item = {"id": entry_id or _new_id("intel")}
        for f in _INTEL_FIELDS:
            item[f] = (entry or {}).get(f, "") or ""
        ws.setdefault("intel", {}).setdefault(key, []).insert(0, item)
        return item
    return _mutate(client, fn)


def update_intel_entry(client, section, entry_id, fields):
    """Edit a Market Intelligence entry's fields in place by id. Returns the entry, or None if not
    found. Only the recognised intel fields are written; unknown keys are ignored.

    Editing an AUTO-pulled entry (one the daily refresh wrote) PINS it: the `auto` flag is dropped so
    a hand-correction survives the next refresh (which only ever replaces still-auto entries)."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def fn(ws):
        for it in ws.get("intel", {}).get(key, []):
            if it.get("id") == entry_id:
                for f in _INTEL_FIELDS:
                    if f in (fields or {}):
                        it[f] = fields[f]
                it.pop("auto", None)  # an admin edit pins the entry (no longer auto-managed)
                return it
        return None
    return _mutate(client, fn)


def delete_intel_entry(client, section, entry_id):
    """Delete a Market Intelligence entry by id from `section`. Returns the remaining list."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def fn(ws):
        lst = ws.setdefault("intel", {}).setdefault(key, [])
        ws["intel"][key] = [it for it in lst if it.get("id") != entry_id]
        return ws["intel"][key]
    return _mutate(client, fn)


# --- Market Intelligence: per-client research topics + the daily auto-refresh -------------------
# The intel tab can auto-fill from real news every day (services/intel-refresh, fed by intel_feed).
# Two extra pieces of state, both additive (no new infra -- still one workspace JSON per client):
#   * ws["intel_topics"]  -- a list of keyword strings the daily refresh searches for the client's
#     Business Research section (e.g. ["RV industry", "motorhome sales"]). Empty -> the refresh job
#     falls back to a generic marketing set. Team-edited from inside the workspace.
#   * each auto-pulled entry carries `"auto": True`. replace_auto_intel swaps out exactly those on
#     each run, so hand-added (and admin-edited, see update_intel_entry) entries are NEVER clobbered.
_MAX_INTEL_TOPICS = 12


def get_intel_topics(ws):
    """The client's research-keyword list from an already-loaded workspace dict (never None)."""
    topics = (ws or {}).get("intel_topics") or []
    return [t for t in topics if isinstance(t, str) and t.strip()]


def set_intel_topics(client, topics):
    """Replace the client's Business-Research keyword list (trimmed, de-duped, capped). Returns it.

    Accepts a list of strings OR a single comma/newline-separated string (what the admin textarea
    posts); blanks are dropped and order is preserved (first occurrence wins)."""
    if isinstance(topics, str):
        topics = re.split(r"[,\n]", topics)
    cleaned, seen = [], set()
    for t in topics or []:
        t = (t or "").strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            cleaned.append(t)
        if len(cleaned) >= _MAX_INTEL_TOPICS:
            break

    def fn(ws):
        ws["intel_topics"] = cleaned
        return cleaned
    return _mutate(client, fn)


def replace_auto_intel(client, section, entries):
    """Swap the AUTO entries of a section for `entries`, preserving hand-added/pinned ones.

    `entries` is a list of intel-field dicts (heading/title/body/source/link/date) the daily refresh
    built from real news; each is stored with a fresh id and `auto:True`. Manual entries (no `auto`
    flag) are kept untouched. Returns the section's resulting entry list."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def fn(ws):
        existing = ws.setdefault("intel", {}).setdefault(key, [])
        kept = [it for it in existing if not it.get("auto")]
        fresh = []
        for e in entries or []:
            item = {"id": _new_id("intel"), "auto": True}
            for f in _INTEL_FIELDS:
                item[f] = (e or {}).get(f, "") or ""
            fresh.append(item)
        ws["intel"][key] = fresh + kept  # the view re-sorts by date, so order here is immaterial
        return ws["intel"][key]
    return _mutate(client, fn)
