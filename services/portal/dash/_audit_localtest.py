"""Off-cloud test for the super-admin audit feed + restorable Trash (no GCS, no network).

Run from this directory:  python _audit_localtest.py

Mirrors _atrium_smoketest.py: stubs google.cloud.storage so the LOCAL-FS backend is used, points the
registry/workspace/audit objects at a temp dir, and exercises the audit module directly AND end-to-end
through the Flask test client (delete -> Trash -> restore; 30-day auto-purge; the console panes render).
"""

import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

# 1. Stub google.cloud.storage so the local-fs backend is used (mirrors _atrium_smoketest).
_g = types.ModuleType("google")
_gc = types.ModuleType("google.cloud")
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        raise RuntimeError("GCS disabled in audit test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

# 2. Point every object store at a temp dir.
_TMP = tempfile.mkdtemp(prefix="audit_test_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"

import audit          # noqa: E402
import seed_workspace  # noqa: E402
import store          # noqa: E402
import workspace      # noqa: E402
import main           # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    # ---- audit module: activity feed ----------------------------------------------------------
    audit.log_activity(CLIENT, "owner@x.com", "client", "approved content", "RVR-016")
    audit.log_activity(CLIENT, "info@agoradatadriven.com", "superadmin", "deleted content", "RVR-017")
    acts = audit.recent_activity()
    _check("activity recorded (>=2)", len(acts) >= 2)
    _check("activity newest-first", acts[0]["action"] == "deleted content")
    _check("activity carries actor/role/client", acts[0]["role"] == "superadmin" and acts[0]["client"] == CLIENT)

    # ---- audit module: trash put/list/get/remove ----------------------------------------------
    e = audit.trash_put(CLIENT, "calendar", "Team offsite", {"label": "Team offsite", "date": "2026-06-23"})
    _check("trash_put returns an entry id", bool(e and e.get("id")))
    listed = audit.trash_list()
    _check("trash_list shows the entry with days_left ~30",
           any(t["id"] == e["id"] and t["days_left"] >= 29 for t in listed))
    _check("trash_get fetches by id", (audit.trash_get(e["id"]) or {}).get("kind") == "calendar")

    # ---- 30-day auto-purge: age an entry past the TTL, it disappears on next read --------------
    aged = audit.trash_put(CLIENT, "calendar", "Ancient event", {"label": "Ancient"})
    data = audit.load()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=audit.TRASH_TTL_DAYS + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for t in data["trash"]:
        if t["id"] == aged["id"]:
            t["ts"] = old_ts
    audit.save(data)
    after = audit.trash_list()
    _check("expired (>30d) trash auto-purged on read", not any(t["id"] == aged["id"] for t in after))
    _check("fresh trash survives the purge", any(t["id"] == e["id"] for t in after))

    # ---- restore helpers (direct) -------------------------------------------------------------
    audit.trash_remove(e["id"])  # tidy up the calendar entry before the e2e flow

    # ---- end-to-end through the app: delete -> Trash -> restore --------------------------------
    seed_workspace.seed(register_client=True)
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(SUPER)

    # find a real content piece + its campaign
    ws = workspace.load_workspace(CLIENT)
    camp = ws["campaigns"][0]
    piece_id = camp["content"][0]["id"]

    # delete it -> should land in Trash AND log activity
    r = c.post("/w/%s/admin/delete-content" % CLIENT, data={"content_id": piece_id})
    _check("delete-content ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp_gone, gone = workspace._find_content(workspace.load_workspace(CLIENT), piece_id)
    _check("content actually removed", gone is None)
    tr = audit.trash_list()
    entry = next((t for t in tr if t["kind"] == "content" and t["payload"].get("id") == piece_id), None)
    _check("deleted content captured in Trash", entry is not None)
    _check("delete logged in activity feed",
           any(a["action"] == "deleted content" for a in audit.recent_activity()))

    # restore it -> content comes back, Trash entry consumed
    r = c.post("/admin/atrium/restore", data={"entry_id": entry["id"]})
    _check("restore redirects (303/302)", r.status_code in (302, 303))
    _camp2, back = workspace._find_content(workspace.load_workspace(CLIENT), piece_id)
    _check("content restored to its campaign", back is not None and back.get("id") == piece_id)
    _check("Trash entry consumed after restore", audit.trash_get(entry["id"]) is None)

    # delete a whole client -> Trash -> restore rebuilds login + workspace
    r = c.post("/admin/atrium/%s/delete" % CLIENT)
    _check("delete-client redirects", r.status_code in (302, 303))
    _check("client registry entry removed", store.get_client(CLIENT) is None)
    _check("client workspace removed", workspace.load_workspace(CLIENT) is None)
    centry = next((t for t in audit.trash_list() if t["kind"] == "client" and t["client"] == CLIENT), None)
    _check("deleted client captured in Trash", centry is not None)
    r = c.post("/admin/atrium/restore", data={"entry_id": centry["id"]})
    _check("client restored: registry entry back", store.get_client(CLIENT) is not None)
    _check("client restored: workspace back", workspace.load_workspace(CLIENT) is not None)

    # ---- console renders the new panes --------------------------------------------------------
    body = c.get("/admin/atrium").get_data(as_text=True)
    _check("console renders Activity tab", 'data-section="activity"' in body and 'data-pane="activity"' in body)
    _check("console renders Trash tab", 'data-section="trash"' in body and 'data-pane="trash"' in body)

    print("[audit-localtest] PASS")


if __name__ == "__main__":
    run()
