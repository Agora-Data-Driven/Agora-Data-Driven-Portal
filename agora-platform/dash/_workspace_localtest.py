"""Local smoke test for the Atrium data layer -- runs entirely off-cloud, never touches prod.

It points the workspace store at a throwaway temp directory (WORKSPACE_LOCAL_DIR), seeds the
Riverdance demo there, then exercises every client-facing mutation and re-reads from disk to prove
persistence. No GCS, no ADC, and no `google-cloud-storage` package required.

    python _workspace_localtest.py        # prints PASS / FAIL and exits 0 / 1
"""

import os
import shutil
import sys
import tempfile

# Point the store at a temp dir BEFORE importing the module under test.
_TMP = tempfile.mkdtemp(prefix="atrium_localtest_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP

import workspace            # noqa: E402  (must follow the env setup above)
import seed_workspace       # noqa: E402

CLIENT = "riverdance"
USER = "owner@riverdanceresort.com"


def _check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    print("[localtest] WORKSPACE_LOCAL_DIR = %s" % _TMP)

    # 1. Seed writes the object; re-seeding refuses to clobber.
    rc = seed_workspace.seed(register_client=False)
    _check("first seed returns 0", rc == 0)
    _check("workspace object exists after seed", workspace.workspace_exists(CLIENT))
    _check("re-seed refuses to clobber (returns 1)", seed_workspace.seed(register_client=False) == 1)

    # 2. The seeded shape matches the spec.
    ws = workspace.load_workspace(CLIENT)
    _check("display_name seeded", ws["display_name"] == "Riverdance RV Resort")
    _check("six KPI metrics", len(ws["metrics"]) == 6)
    _check("two campaigns (paid + organic)",
           [c["channel"] for c in ws["campaigns"]] == ["paid", "organic"])
    _check("14-point leads series", len(ws["series"]) == 14)
    _check("three conversations", len(ws["conversations"]) == 3)

    # 3. Approve an awaiting piece with a note.
    item = workspace.decide_content(CLIENT, "RVR-016", "approved", note="Looks great, ship it.")
    _check("decide_content set status approved", item["status"] == "approved")
    _check("decide_content stamped decided_at", item["decided_at"].endswith("Z"))
    _check("decide_content saved the note", item["client_note"] == "Looks great, ship it.")

    # 4. Standalone note on another piece.
    workspace.set_content_note(CLIENT, "RVR-017", "Can we add a guest quote?")
    _camp, rvr017 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-017")
    _check("set_content_note persisted", rvr017["client_note"] == "Can we add a guest quote?")

    # 5. Client sends a message -> thread goes awaiting_reply.
    conv, msg = workspace.add_message(CLIENT, "cv_1", "client", "Sarah",
                                      "Thursday works great, thank you!", set_status="awaiting_reply")
    _check("message appended", conv["messages"][-1]["body"].startswith("Thursday works"))
    _check("message sender recorded", msg["sender"] == "client")
    _check("conversation status updated", conv["status"] == "awaiting_reply")

    # 6. Notification prefs: defaults for an unknown user, merge on save.
    defaults = workspace.get_notify(ws, "nobody@example.com")
    _check("default notify: master on", defaults["master"] is True)
    _check("default notify: status off", defaults["status"] is False)
    saved = workspace.set_notify(CLIENT, USER, {"status": True, "frequency": "daily"})
    _check("set_notify merged status", saved["status"] is True)
    _check("set_notify merged frequency", saved["frequency"] == "daily")
    _check("set_notify kept defaults", saved["replies"] is True)

    # 7. Activity prepends, most-recent first.
    workspace.add_activity(CLIENT, "check", "You approved RVR-016.", "Today, 10:00 AM")
    _check("activity prepended",
           workspace.load_workspace(CLIENT)["activity"][0]["text"] == "You approved RVR-016.")

    # 8. Everything survived a reload from disk.
    reloaded = workspace.load_workspace(CLIENT)
    _camp, rvr016 = workspace._find_content(reloaded, "RVR-016")
    _check("approval persisted across reload", rvr016["status"] == "approved")
    _check("notify persisted across reload", reloaded["notify"][USER]["status"] is True)

    print("[localtest] PASS")
    return 0


def main():
    try:
        return run()
    except AssertionError as exc:
        print("[localtest] FAIL: %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
