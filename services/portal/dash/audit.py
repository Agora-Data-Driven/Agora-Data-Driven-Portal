"""Super-admin activity log + restorable trash for Agora Atrium.

State is ONE private JSON object `audit.json` in the SAME registry bucket as platform.json -- no new
bucket / service / IAM (this mirrors store.py exactly: GCS by default, a local-fs backend via
REGISTRY_LOCAL_DIR so it runs off-cloud). It holds two lists:

  * activity[] -- the audit feed: every admin/client action across all workspaces
                  ({id, ts, client, actor, role, action, detail}). Newest first, capped at
                  ACTIVITY_CAP so the object can never grow without bound.
  * trash[]    -- soft-deleted items kept so the super admin can RESTORE them
                  ({id, ts, client, kind, label, payload, extra, actor, role}). Entries older than
                  TRASH_TTL_DAYS are purged automatically whenever the trash is read or written --
                  the app is request-driven, so this lazy purge IS the no-infra 'auto-delete'.

Every function is best-effort and swallows storage errors, so logging or trashing can never raise
into (and break) the action it accompanies.
"""

import json
import os
from datetime import datetime, timezone

# Same bucket as the registry; a distinct object so it never collides with platform.json.
AUDIT_BUCKET = os.environ.get("REGISTRY_BUCKET", "agora-data-driven-platform-dash")
AUDIT_OBJECT = os.environ.get("AUDIT_OBJECT", "audit.json")

ACTIVITY_CAP = 500          # keep the newest N activity entries
TRASH_TTL_DAYS = 30         # auto-purge soft-deleted items this many days after deletion

_storage_client = None


def _local_dir():
    """The local-filesystem backend root, or "" to use GCS (mirrors store._local_dir)."""
    return os.environ.get("REGISTRY_LOCAL_DIR", "")


def _gcs_blob():
    """Lazily construct the GCS client and return the audit blob handle."""
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage  # lazy: only the GCS backend needs the package
        _storage_client = storage.Client()
    return _storage_client.bucket(AUDIT_BUCKET).blob(AUDIT_OBJECT)


def _empty():
    return {"version": 1, "activity": [], "trash": []}


def load():
    """Return the audit dict, or an empty skeleton if the object is absent / unreadable."""
    local = _local_dir()
    if local:
        path = os.path.join(local, AUDIT_OBJECT)
        if not os.path.isfile(path):
            return _empty()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.loads(fh.read())
        except Exception:
            return _empty()
    try:
        blob = _gcs_blob()
        if not blob.exists():
            return _empty()
        return json.loads(blob.download_as_bytes().decode("utf-8"))
    except Exception:
        return _empty()


def save(data):
    """Persist the audit dict back to audit.json (private; never made public)."""
    body = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    local = _local_dir()
    if local:
        path = os.path.join(local, AUDIT_OBJECT)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(body)
        return
    _gcs_blob().upload_from_string(body, content_type="application/json")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix):
    """A clock-derived id; uniqueness within this small feed is plenty."""
    return "%s_%s" % (prefix, datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))


def _days_old(ts):
    try:
        when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0
    except Exception:
        return 0.0


# --- Activity feed ------------------------------------------------------------------------------
def log_activity(client, actor, role, action, detail=""):
    """Record one activity entry (best-effort). Returns the entry, or None on failure."""
    try:
        data = load()
        entry = {
            "id": _new_id("act"), "ts": _now_iso(), "client": client or "",
            "actor": actor or "", "role": role or "",
            "action": action or "", "detail": detail or "",
        }
        acts = data.setdefault("activity", [])
        acts.insert(0, entry)
        del acts[ACTIVITY_CAP:]
        save(data)
        return entry
    except Exception:
        return None


def recent_activity(limit=200):
    """The newest `limit` activity entries (newest first)."""
    try:
        return list(load().get("activity", []))[:max(0, limit)]
    except Exception:
        return []


# --- Trash (restorable soft-deletes) ------------------------------------------------------------
def _purge(data):
    """Drop trash entries older than TRASH_TTL_DAYS (in place). Returns True if anything was dropped."""
    trash = data.get("trash", [])
    kept = [t for t in trash if _days_old(t.get("ts", "")) < TRASH_TTL_DAYS]
    dropped = len(kept) != len(trash)
    data["trash"] = kept
    return dropped


def trash_put(client, kind, label, payload, actor="", role="", extra=None):
    """Soft-delete: stash a copy of a removed item so the super admin can restore it. Best-effort.

    `kind` is one of "content" | "campaign" | "calendar" | "client". `payload` is the removed item;
    `extra` carries anything restore needs that isn't in the payload (e.g. a content piece's
    campaign_id, or a deleted client's workspace JSON).
    """
    try:
        data = load()
        entry = {
            "id": _new_id("trash"), "ts": _now_iso(), "client": client or "",
            "kind": kind, "label": label or "", "actor": actor or "", "role": role or "",
            "payload": payload, "extra": extra or {},
        }
        data.setdefault("trash", []).insert(0, entry)
        _purge(data)
        save(data)
        return entry
    except Exception:
        return None


def trash_list():
    """Current (non-expired) trash entries, newest first, each decorated with `days_left`.

    Purges anything past TRASH_TTL_DAYS first and persists the purge -- the lazy 'auto-delete'.
    """
    try:
        data = load()
        if _purge(data):
            save(data)
        out = []
        for t in data.get("trash", []):
            t2 = dict(t)
            t2["days_left"] = max(0, int(round(TRASH_TTL_DAYS - _days_old(t.get("ts", "")))))
            out.append(t2)
        return out
    except Exception:
        return []


def trash_get(entry_id):
    """The raw trash entry with `entry_id`, or None."""
    try:
        for t in load().get("trash", []):
            if t.get("id") == entry_id:
                return t
    except Exception:
        pass
    return None


def trash_remove(entry_id):
    """Remove a trash entry by id (after a successful restore). Returns the removed entry, or None."""
    try:
        data = load()
        trash = data.get("trash", [])
        for t in list(trash):
            if t.get("id") == entry_id:
                trash.remove(t)
                save(data)
                return t
    except Exception:
        pass
    return None
