"""Regression test: a content piece whose id contains "/" (content ids are the free-text title,
e.g. "Engaged Lead / Considering") must still serve its attached creatives.

Before the fix the route used <content_id> (no slashes), so urlencode('a/b') -> 'a%2Fb' -> the WSGI
layer decoded %2F back to '/', the path split, and the creative URL 404'd (broken image preview in
the Lead Gen / Organic tabs). The fix switches the routes to <path:content_id>.

Run: python _slashid_creative_test.py   # prints PASS / FAIL, exits 0 / 1
"""

import os
import sys
import tempfile
import types

# Stub GCS before importing main (mirrors _atrium_smoketest.py).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

_TMP = tempfile.mkdtemp(prefix="slashid_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"

import seed_workspace  # noqa: E402
import store          # noqa: E402
import workspace      # noqa: E402
import main           # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
# A 1x1 PNG.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
       b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
       b"\x00IEND\xaeB`\x82")
fails = []


def check(label, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + label)
    if not cond:
        fails.append(label)


# Seed the full demo workspace (gives a complete brand block so the tab template renders), then add
# a content piece whose id contains slashes -- exactly like a real free-text title.
seed_workspace.main()
ws = workspace.load_workspace(CLIENT)
CAMP = ws["campaigns"][0]["id"]
# Free-text id with two slashes. (We avoid spaces *around* the slashes only because the local-fs
# test backend maps the id into a directory path and Windows forbids a trailing space in a path
# segment -- on the live GCS backend the id is just part of an object key, so " / " is fine there.)
CID = "Email Build Design/Engaged Lead/Considering"
item = workspace.add_content(CLIENT, CAMP, {"id": CID, "ref": CID, "type_tag": "Email"})
check("content keeps its slashed id verbatim", item["id"] == CID)

IMG_ID = workspace._new_id("img")
workspace.add_content_image(CLIENT, CID, IMG_ID, PNG, "image/png", "file 1.png")

main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
c = main.app.test_client()
with c.session_transaction() as s:
    s.update(SUPER)

# 1. The image route serves the PNG (the reported bug).
import urllib.parse as up
url = "/w/%s/creative/%s/%s" % (CLIENT, up.quote(CID), up.quote(IMG_ID))
r = c.get(url)
check("slashed-id image serves 200", r.status_code == 200)
check("served bytes are the PNG", r.data == PNG)
check("served as image/png", r.headers.get("Content-Type", "").startswith("image/png"))

# 2. The leadgen/organic tab renders the working <img src> (not a 404 / broken).
page = c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True)
check("leadgen page renders the creative url", url in page)

# 3. A legacy single-creative (image_object) whose id also contains a slash still serves
#    (router splits it into the 2-arg route; the handler re-joins and recovers it).
CID2 = "Video Promo/Summer"
workspace.add_content(CLIENT, CAMP, {"id": CID2, "ref": CID2, "type_tag": "Video"})
obj = workspace.creative_object_name(CLIENT, CID2)
workspace.write_creative(CLIENT, CID2, PNG, "image/png")
workspace.set_content_image(CLIENT, CID2, obj, "image/png")
r2 = c.get("/w/%s/creative/%s" % (CLIENT, up.quote(CID2)))
check("legacy slashed-id single creative serves 200", r2.status_code == 200)
check("legacy creative bytes ok", r2.data == PNG)

# 4. A genuinely missing image still 404s (fix didn't swallow real misses).
r3 = c.get("/w/%s/creative/%s/%s" % (CLIENT, up.quote(CID), "img_doesnotexist"))
check("missing image still 404s", r3.status_code == 404)

print("[slashid] " + ("PASS" if not fails else "FAIL: %s" % fails))
sys.exit(1 if fails else 0)
