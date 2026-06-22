"""Flask route + template integration smoke test for Agora Atrium (off-cloud, no real GCS).

Stubs google.cloud.storage so main.py (which imports store/feedback) loads without ADC, points the
workspace store at a temp dir, seeds the Riverdance demo there, then drives the real Flask app with
its test client: every client tab renders, and every POST action persists. Proves the route wiring,
the Jinja template, the atrium_dt filter, and atrium_view all work together before any deploy.

Run with a Flask-capable interpreter:
    python _atrium_smoketest.py        # prints PASS / FAIL, exits 0 / 1
"""

import io
import os
import shutil
import sys
import tempfile
import types

# 1. Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in smoke test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

# 2. Point the workspace store at a temp dir and sign the session.
_TMP = tempfile.mkdtemp(prefix="atrium_smoke_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP   # admin_atrium console reads the registry (reveal_password)
os.environ["SESSION_SECRET"] = "test-secret"

import seed_workspace   # noqa: E402
import store            # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    seed_workspace.seed(register_client=False)
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # Unauthenticated -> redirect to login.
    _check("unauthed /w redirects to login", c.get("/w/%s/" % CLIENT).status_code == 302)

    with c.session_transaction() as s:
        s.update(SUPER)

    # Every client tab renders.
    body = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("overview renders", "Riverdance RV Resort" in body and "Agora Atrium" in body)
    _check("greeting present", "Good <span" in body)
    _check("leadgen content present in DOM", "Summer Lead-Gen Push" in body)
    _check("organic content present in DOM", "June Nurture &amp; SEO" in body or "June Nurture" in body)
    _check("AI summary present", "AI summary" in body)
    for tab in ("dashboard", "leadgen", "organic", "calendar", "conversations", "settings"):
        _check("tab '%s' returns 200" % tab, c.get("/w/%s/%s" % (CLIENT, tab)).status_code == 200)

    # Approve an awaiting piece -> persists + confirmation shows on reload.
    r = c.post("/w/%s/approve" % CLIENT, data={"content_id": "RVR-016", "note": "Ship it."})
    _check("approve returns ok json", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, item = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("approval persisted", item["status"] == "approved" and item["client_note"] == "Ship it.")
    _check("confirmation bar on reload",
           "Approved" in c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True))

    # Re-decide: an already-approved piece can be flipped to changes (status anytime).
    r = c.post("/w/%s/request-changes" % CLIENT, data={"content_id": "RVR-016"})
    _check("re-decide flips approved -> changes",
           r.status_code == 200 and r.get_json().get("status") == "changes")
    r = c.post("/w/%s/approve" % CLIENT, data={"content_id": "RVR-016", "note": "Ship it."})
    _check("re-decide flips back to approved", r.get_json().get("status") == "approved")

    # Request changes on the organic awaiting piece.
    r = c.post("/w/%s/request-changes" % CLIENT, data={"content_id": "RVR-017"})
    _check("request-changes ok", r.status_code == 200 and r.get_json().get("status") == "changes")

    # Client posts a threaded comment on a content piece.
    r = c.post("/w/%s/comment" % CLIENT, data={"content_id": "RVR-017", "body": "Add a guest quote?"})
    _check("client comment ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, c017 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-017")
    _check("client comment persisted (sender client)",
           c017["comments"][-1]["body"] == "Add a guest quote?" and c017["comments"][-1]["sender"] == "client")

    # Save a note silently.
    _check("save-note ok",
           c.post("/w/%s/save-note" % CLIENT, data={"content_id": "RVR-014", "note": "Nice"}).status_code == 200)

    # Send a client message -> thread goes awaiting_reply.
    r = c.post("/w/%s/send-message" % CLIENT, data={"conversation_id": "cv_1", "body": "Thanks!"})
    _check("send-message ok", r.status_code == 200 and r.get_json().get("status") == "awaiting_reply")
    _check("message persisted",
           workspace.load_workspace(CLIENT)["conversations"][0]["messages"][-1]["body"] == "Thanks!")

    # Save notification prefs.
    r = c.post("/w/%s/save-notify" % CLIENT,
               data={"master": "1", "content": "0", "replies": "1", "summary": "1",
                     "status": "0", "news": "0", "frequency": "daily"})
    _check("save-notify ok", r.status_code == 200)
    prefs = workspace.get_notify(workspace.load_workspace(CLIENT), SUPER["user"])
    _check("notify persisted", prefs["content"] is False and prefs["frequency"] == "daily")

    # Team console is now the LANDING ONLY. The per-client manage page and its POST routes are GONE:
    # the team edits each workspace IN PLACE via /w/<c>/admin/* (exercised below), and a console card
    # opens /w/<c>/ directly.
    _check("old per-client manage page removed (404)",
           c.get("/admin/atrium/%s" % CLIENT).status_code == 404)
    for path in ("password", "campaign", "content", "conversation", "reply", "metrics"):
        _check("old console POST /%s removed" % path,
               c.post("/admin/atrium/%s/%s" % (CLIENT, path), data={}).status_code in (404, 405))

    # The console landing renders the welcome, links each card straight to the workspace, and hides
    # the worked-example `template` client.
    store.add_client(CLIENT, "Riverdance RV Resort")
    store.add_client("template", "Template")
    landing = c.get("/admin/atrium").get_data(as_text=True)
    _check("console landing renders welcome", "Welcome to the Admin Portal" in landing)
    _check("console card opens the workspace directly", ('href="/w/%s/"' % CLIENT) in landing)
    _check("template client hidden from console", '<div class="name">Template</div>' not in landing)
    store.remove_client("template")

    # ---- In-workspace admin editing (/w/<c>/admin/*), all JSON, super-admin only ----
    # Admin notice bar renders for a super-admin in the real workspace.
    body = c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True)
    _check("admin edit bar renders for super-admin",
           'class="ax-adminbadge"' in body and 'data-admin="1"' in body)

    # Edit strategy in place.
    r = c.post("/w/%s/admin/strategy" % CLIENT,
               data={"campaign_id": "c_paid_1", "name": "Summer Lead-Gen Push v2",
                     "eyebrow": "PAID · LEAD GEN", "what": "W2", "why": "Y2", "next": "N2"})
    _check("inline strategy ok", r.status_code == 200 and r.get_json().get("ok") is True)
    camp = workspace._find_campaign(workspace.load_workspace(CLIENT), "c_paid_1")
    _check("strategy persisted", camp["strategy"]["what"] == "W2" and camp["name"] == "Summer Lead-Gen Push v2")

    # Save a strategy doc link, then generate a summary (AI OFF -> graceful, never 500).
    r = c.post("/w/%s/admin/strategy-doc" % CLIENT,
               data={"campaign_id": "c_paid_1",
                     "doc_url": "https://docs.google.com/document/d/ABC123abc123abc123abc/edit"})
    _check("strategy-doc saved", r.status_code == 200 and r.get_json().get("strategy_doc", "").endswith("/edit"))
    r = c.post("/w/%s/admin/generate-summary" % CLIENT, data={"campaign_id": "c_paid_1"})
    _check("generate-summary degrades gracefully (no 500)", r.status_code == 200)
    _check("generate-summary reports unreadable doc when docs disabled",
           r.get_json().get("ok") is False and r.get_json().get("source") == "none")

    # Hand-edit the AI summary.
    r = c.post("/w/%s/admin/summary" % CLIENT,
               data={"campaign_id": "c_paid_1", "ai_summary": "Hand-written summary."})
    _check("manual summary saved",
           r.status_code == 200 and r.get_json().get("ai_summary") == "Hand-written summary.")

    # Add content in place, then edit + comment as the team + delete it.
    r = c.post("/w/%s/admin/content" % CLIENT,
               data={"campaign_id": "c_paid_1", "ref": "RVR-099", "type_tag": "Reel",
                     "platform": "Instagram", "caption": "A reel for review."})
    _check("inline add-content ok", r.status_code == 200 and r.get_json().get("id") == "RVR-099")
    r = c.post("/w/%s/admin/edit-content" % CLIENT,
               data={"content_id": "RVR-099", "caption": "An edited reel caption."})
    _check("inline edit-content ok", r.status_code == 200)
    _camp, v099 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("content edit persisted", v099["caption"] == "An edited reel caption.")
    r = c.post("/w/%s/admin/content-comment" % CLIENT,
               data={"content_id": "RVR-099", "body": "Team note.", "sender_name": "Maya"})
    _check("team comment ok", r.status_code == 200)
    _camp, v099b = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("team comment persisted (sender agora)", v099b["comments"][-1]["sender"] == "agora")

    # Upload a creative, fetch it back through the authed proxy, then remove it.
    png = b"\x89PNG\r\n\x1a\n" + b"riverdance-creative-bytes"
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(png), "ad.png", "image/png")},
               content_type="multipart/form-data")
    _check("upload-creative ok", r.status_code == 200 and r.get_json().get("ok") is True)
    served = c.get("/w/%s/creative/RVR-099" % CLIENT)
    _check("creative served via authed proxy",
           served.status_code == 200 and served.get_data() == png and served.mimetype == "image/png")
    r = c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})
    _check("remove-creative ok", r.status_code == 200)
    _check("creative 404 after removal", c.get("/w/%s/creative/RVR-099" % CLIENT).status_code == 404)

    # A short VIDEO creative is accepted, served with its mime, and rendered as a <video>.
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"riverdance-clip-bytes"
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(mp4), "reel.mp4", "video/mp4")},
               content_type="multipart/form-data")
    _check("video upload ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, vitem = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("video mime stored", vitem.get("image_mime") == "video/mp4")
    served = c.get("/w/%s/creative/RVR-099" % CLIENT)
    _check("video served with mime", served.status_code == 200 and served.mimetype == "video/mp4")
    _check("workspace renders a <video> for the clip",
           ('<video src="/w/%s/creative/RVR-099"' % CLIENT) in c.get("/w/%s/" % CLIENT).get_data(as_text=True))
    c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})

    # Reject a non-media upload (neither image nor video).
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(b"x"), "a.txt", "text/plain")},
               content_type="multipart/form-data")
    _check("non-media upload rejected", r.status_code == 400)

    # Signed-URL "bypass the cap" flow (the GCS signing itself needs cloud; here we test the app side).
    # 1) upload-url degrades gracefully on the local backend (no signing) -> ok:false, never crashes.
    r = c.post("/w/%s/admin/creative-upload-url" % CLIENT,
               data={"content_id": "RVR-099", "content_type": "video/mp4"})
    _check("creative-upload-url responds gracefully", r.status_code == 200 and r.get_json().get("ok") is False)
    # 2) confirm records a creative uploaded out-of-band (simulating the direct-to-GCS PUT).
    workspace.write_creative(CLIENT, "RVR-099", b"\x00\x00\x00\x18ftypmp42" + b"0123456789" * 5000, content_type="video/mp4")
    r = c.post("/w/%s/admin/creative-confirm" % CLIENT, data={"content_id": "RVR-099", "content_type": "video/mp4"})
    _check("creative-confirm records the upload", r.status_code == 200 and r.get_json().get("ok") is True)
    # 3) a Range request streams a 206 partial (video seeking + bounded memory).
    served = c.get("/w/%s/creative/RVR-099" % CLIENT, headers={"Range": "bytes=0-1023"})
    body = served.get_data()  # drain the streaming generator so its file handle closes (Windows)
    _check("range request -> 206 partial",
           served.status_code == 206 and len(body) == 1024
           and served.headers.get("Content-Range", "").startswith("bytes 0-1023/"))
    c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})

    # Delete the content piece in place.
    r = c.post("/w/%s/admin/delete-content" % CLIENT, data={"content_id": "RVR-099"})
    _check("inline delete-content ok", r.status_code == 200)
    _camp, gone = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("content deleted", gone is None)

    # Add a campaign in place, then delete it.
    n_before = len(workspace.load_workspace(CLIENT)["campaigns"])
    r = c.post("/w/%s/admin/campaign" % CLIENT,
               data={"channel": "organic", "name": "Inline Organic", "eyebrow": "ORG"})
    _check("inline add-campaign ok", r.status_code == 200)
    new_cid = r.get_json().get("id")
    _check("campaign added", len(workspace.load_workspace(CLIENT)["campaigns"]) == n_before + 1)
    r = c.post("/w/%s/admin/delete-campaign" % CLIENT, data={"campaign_id": new_cid})
    _check("inline delete-campaign ok",
           r.status_code == 200 and len(workspace.load_workspace(CLIENT)["campaigns"]) == n_before)

    # Inline metrics + calendar edits.
    r = c.post("/w/%s/admin/metrics" % CLIENT,
               data={"today_leads": "33", "split_paid": "44", "metric_value_0": "999"})
    _check("inline metrics ok", r.status_code == 200 and workspace.load_workspace(CLIENT)["today"]["leads"] == 33)
    r = c.post("/w/%s/admin/calendar" % CLIENT,
               data={"op": "add", "date": "2026-07-04", "label": "July promo", "kind": "milestone"})
    _check("inline calendar add ok", r.status_code == 200)
    cal_n = len(workspace.load_workspace(CLIENT)["calendar"])
    # Mark the just-added event done, then clear it (the "Mark as done" toggle).
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "status", "index": str(cal_n - 1), "status": "done"})
    _check("inline calendar mark-done ok",
           r.status_code == 200 and workspace.load_workspace(CLIENT)["calendar"][cal_n - 1].get("status") == "done")
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "status", "index": str(cal_n - 1), "status": ""})
    _check("inline calendar clear-done ok",
           r.status_code == 200 and "status" not in workspace.load_workspace(CLIENT)["calendar"][cal_n - 1])
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "delete", "index": str(cal_n - 1)})
    _check("inline calendar delete ok",
           r.status_code == 200 and len(workspace.load_workspace(CLIENT)["calendar"]) == cal_n - 1)

    # Inline reply to a conversation as AGORA.
    r = c.post("/w/%s/admin/reply" % CLIENT,
               data={"conversation_id": "cv_1", "body": "Inline team reply.", "resolve": "1"})
    _check("inline reply ok + resolved",
           r.status_code == 200 and r.get_json().get("status") == "resolved")

    # A non-super-admin grantee can open the workspace but is FORBIDDEN on every admin route.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    _check("grantee can open workspace", c.get("/w/%s/" % CLIENT).status_code == 200)
    _check("grantee cannot see admin bar", 'data-admin="1"' not in c.get("/w/%s/" % CLIENT).get_data(as_text=True))
    for path in ("strategy", "campaign", "content", "delete-content", "metrics", "calendar",
                 "generate-summary", "upload-creative", "reply"):
        _check("admin route /%s forbidden for grantee" % path,
               c.post("/w/%s/admin/%s" % (CLIENT, path), data={}).status_code == 403)
    # But a grantee CAN comment + re-decide (client powers).
    _check("grantee can comment",
           c.post("/w/%s/comment" % CLIENT, data={"content_id": "RVR-014", "body": "hi"}).status_code == 200)

    # A user who cannot open the client is forbidden.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "x@y.com", "clients": ["someoneelse"]})
    _check("non-grantee forbidden", c.get("/w/%s/" % CLIENT).status_code == 403)
    _check("non-grantee creative forbidden", c.get("/w/%s/creative/RVR-014" % CLIENT).status_code == 403)

    print("[smoketest] PASS")
    return 0


def main_():
    try:
        return run()
    except AssertionError as exc:
        print("[smoketest] FAIL: %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_())
