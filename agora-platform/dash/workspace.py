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


def _write_object(name, data):
    """Write `data` (bytes) to object `name`, creating parent dirs for the local backend."""
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
    blob.upload_from_string(data, content_type="application/json")


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
    """Default notification prefs: on for master/content/replies/summary, off for status/news."""
    return {
        "master": True,
        "content": True,
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


def add_campaign(client, channel, name, eyebrow="", strategy=None, ai_summary="", campaign_id=None):
    """Add a campaign (team-facing). `channel` is 'paid' or 'organic'. Returns the campaign dict."""
    def fn(ws):
        camp = {
            "id": campaign_id or _new_id("cmp"),
            "channel": channel,
            "name": name or "(untitled campaign)",
            "eyebrow": eyebrow or "",
            "strategy": strategy or {"what": "", "why": "", "next": ""},
            "ai_summary": ai_summary or "",
            "content": [],
        }
        ws.setdefault("campaigns", []).append(camp)
        return camp
    return _mutate(client, fn)


def update_campaign(client, campaign_id, name=None, eyebrow=None, strategy=None, ai_summary=None):
    """Edit a campaign's name / eyebrow / strategy / AI summary (team-facing). Returns it."""
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
        return camp
    return _mutate(client, fn)


def add_content(client, campaign_id, content):
    """Add a content piece to a campaign (team-facing); forces status 'awaiting'. Returns it.

    `content` is a dict of the content fields (ref, type_tag, sub_tag, platform, caption, etc.).
    Missing id/ref are generated; status is always reset to 'awaiting' for a fresh review.
    """
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        item = dict(content or {})
        item.setdefault("id", _new_id("cnt"))
        item.setdefault("ref", item["id"])
        item["status"] = "awaiting"
        item.setdefault("client_note", "")
        item.setdefault("decided_at", "")
        camp.setdefault("content", []).append(item)
        return item
    return _mutate(client, fn)


def update_content(client, content_id, fields):
    """Patch fields on an existing content piece (team-facing). Returns the content dict."""
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item.update(fields or {})
        return item
    return _mutate(client, fn)


def add_calendar_event(client, date, label, kind):
    """Append a calendar event ('paid'|'organic'|'due'|'milestone'). Returns it."""
    def fn(ws):
        event = {"date": date, "label": label or "", "kind": kind or "milestone"}
        ws.setdefault("calendar", []).append(event)
        return event
    return _mutate(client, fn)
