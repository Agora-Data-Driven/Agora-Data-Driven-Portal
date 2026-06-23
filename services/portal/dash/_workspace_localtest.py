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

    # 8a. Threaded comments on a content piece (client + team).
    item, comment = workspace.add_content_comment(CLIENT, "RVR-016", "client", "Sarah", "Can we brighten it?")
    _check("comment appended", comment["body"] == "Can we brighten it?")
    workspace.add_content_comment(CLIENT, "RVR-016", "agora", "Maya", "On it!")
    _camp, rvr016c = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("two comments persisted", len(rvr016c["comments"]) == 2 and rvr016c["comments"][-1]["sender"] == "agora")

    # 8b. Strategy doc attach + campaign update with new fields.
    workspace.set_strategy_doc(CLIENT, "c_paid_1", "https://docs.google.com/document/d/ABC123abc123abc123abc/edit")
    _check("strategy_doc saved",
           workspace._find_campaign(workspace.load_workspace(CLIENT), "c_paid_1")["strategy_doc"].endswith("/edit"))

    # 8c. Creative image bytes round-trip + pointer set/clear.
    obj = workspace.write_creative(CLIENT, "RVR-016", b"\x89PNG\r\n\x1a\nFAKE", content_type="image/png")
    _check("creative object name under prefix", obj == "workspace/creatives/riverdance/RVR-016")
    _check("creative bytes round-trip", workspace.read_creative_bytes(CLIENT, "RVR-016") == b"\x89PNG\r\n\x1a\nFAKE")
    workspace.set_content_image(CLIENT, "RVR-016", obj, "image/png")
    _camp, rvr016i = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("image pointer set", rvr016i["image_object"] == obj and rvr016i["image_mime"] == "image/png")
    workspace.clear_content_image(CLIENT, "RVR-016")
    workspace.delete_creative(CLIENT, "RVR-016")
    _camp, rvr016n = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("image pointer cleared", "image_object" not in rvr016n)
    _check("creative bytes deleted", workspace.read_creative_bytes(CLIENT, "RVR-016") is None)

    # 8d. Delete content + delete campaign.
    n_org = len(workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1")["content"])
    workspace.delete_content(CLIENT, "RVR-017")
    _check("content removed",
           len(workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1")["content"]) == n_org - 1)
    workspace.delete_campaign(CLIENT, "c_org_1")
    _check("campaign removed",
           workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1") is None)

    # 8e. Calendar add + delete.
    before_cal = len(workspace.load_workspace(CLIENT).get("calendar", []))
    workspace.add_calendar_event(CLIENT, "2026-07-04", "Independence Day promo", "milestone")
    _check("calendar event added", len(workspace.load_workspace(CLIENT)["calendar"]) == before_cal + 1)
    workspace.delete_calendar_event(CLIENT, before_cal)
    _check("calendar event deleted", len(workspace.load_workspace(CLIENT)["calendar"]) == before_cal)

    # 8f. Content with a date mirrors onto the Content Calendar as a linked event; editing the date
    #     re-syncs it (paid -> 'paid'/leadgen); clearing the date removes it; deleting the piece too.
    base_cal = len(workspace.load_workspace(CLIENT).get("calendar", []))
    dated = workspace.add_content(CLIENT, "c_paid_1",
                                  {"ref": "Dated teaser", "date": "2026-08-01"})
    linked = [e for e in workspace.load_workspace(CLIENT)["calendar"]
              if e.get("content_id") == dated["id"]]
    _check("dated content mirrored to calendar", len(linked) == 1)
    _check("linked event is paid/leadgen",
           linked and linked[0]["kind"] == "paid" and linked[0]["tab"] == "leadgen")
    _check("linked event carries the date + label",
           linked and linked[0]["date"] == "2026-08-01" and linked[0]["label"] == "Dated teaser")
    workspace.update_content(CLIENT, dated["id"], {"date": "2026-08-15", "ref": "Renamed teaser"})
    relinked = [e for e in workspace.load_workspace(CLIENT)["calendar"]
                if e.get("content_id") == dated["id"]]
    _check("edit re-synced the linked event (no duplicate)", len(relinked) == 1)
    _check("edit overwrote date + label on the event",
           relinked and relinked[0]["date"] == "2026-08-15" and relinked[0]["label"] == "Renamed teaser")
    workspace.update_content(CLIENT, dated["id"], {"date": ""})
    _check("clearing the date removes the linked event",
           not [e for e in workspace.load_workspace(CLIENT)["calendar"]
                if e.get("content_id") == dated["id"]])
    # A piece WITHOUT a date never touches the calendar.
    undated = workspace.add_content(CLIENT, "c_paid_1", {"ref": "No date"})
    _check("undated content adds no calendar event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal)
    # Re-date it, then delete the piece: the linked event goes with it.
    workspace.update_content(CLIENT, undated["id"], {"date": "2026-09-09"})
    _check("re-dating creates the linked event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal + 1)
    workspace.delete_content(CLIENT, undated["id"])
    _check("deleting the piece removes its linked event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal)

    # 9. Everything survived a reload from disk.
    reloaded = workspace.load_workspace(CLIENT)
    _camp, rvr016 = workspace._find_content(reloaded, "RVR-016")
    _check("approval persisted across reload", rvr016["status"] == "approved")
    _check("notify persisted across reload", reloaded["notify"][USER]["status"] is True)
    _check("comments persisted across reload", len(rvr016["comments"]) == 2)

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
