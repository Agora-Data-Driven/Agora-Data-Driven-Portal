"""Off-cloud test for atrium_ga4: parsing, labels, ordering, and graceful degradation.

Injects a `runner` (canned runReport JSON), so it runs with NO network and NO auth -- mirroring the
other `_..._localtest.py` files. Run: python _atrium_ga4_localtest.py
"""

import atrium_ga4


def _canned(_pid, _days):
    """A runReport-shaped response, deliberately out of count order to test sorting."""
    return {
        "rows": [
            {"dimensionValues": [{"value": "session_start"}], "metricValues": [{"value": "1902"}]},
            {"dimensionValues": [{"value": "page_view"}], "metricValues": [{"value": "4213"}]},
            {"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "87"}]},
            {"dimensionValues": [{"value": "my_custom_event"}], "metricValues": [{"value": "12"}]},
        ]
    }


def test_normalize_property_id():
    assert atrium_ga4.normalize_property_id("123456789") == "123456789"
    assert atrium_ga4.normalize_property_id("properties/123456789") == "123456789"
    assert atrium_ga4.normalize_property_id("  987654 ") == "987654"
    # A measurement id is NOT a property id -- must be rejected, not digit-scraped.
    assert atrium_ga4.normalize_property_id("G-QP45TYMEB6") == ""
    assert atrium_ga4.normalize_property_id("UA-12345-6") == ""
    assert atrium_ga4.normalize_property_id("") == ""
    print("ok  normalize_property_id")


def test_parse_and_sort():
    r = atrium_ga4.fetch_event_counts("123456789", days=28, runner=_canned)
    assert r["ok"] is True, r
    assert r["property_id"] == "123456789"
    assert r["days"] == 28
    counts = [row["count"] for row in r["rows"]]
    assert counts == sorted(counts, reverse=True), counts          # count-desc
    assert r["rows"][0]["event"] == "page_view"
    assert r["rows"][0]["label"] == "Page view"
    assert r["total_events"] == 4213 + 1902 + 87 + 12
    # A custom event gets a generic, humanized label/description (never crashes).
    custom = [row for row in r["rows"] if row["event"] == "my_custom_event"][0]
    assert custom["label"] == "My custom event"
    assert custom["description"]
    print("ok  parse + sort + labels")


def test_tracked_table():
    # Hybrid table: ALL predefined funnel events (kept even when Inactive) THEN firing unlisted events.
    r = atrium_ga4.fetch_event_counts("123456789", runner=_canned)
    tracked = r["tracked"]
    events = [t["event"] for t in tracked]
    # The predefined list comes first, in order, in full.
    assert events[:len(atrium_ga4.TRACKED_EVENTS)] == atrium_ga4.TRACKED_EVENTS, events
    by_event = {t["event"]: t for t in tracked}
    # page_view fired -> Active with its count and listed=True.
    assert by_event["page_view"]["active"] is True and by_event["page_view"]["status"] == "Active"
    assert by_event["page_view"]["count"] == 4213 and by_event["page_view"]["listed"] is True
    assert by_event["purchase"]["active"] is True
    # A predefined event NOT in the GA4 response -> kept, Inactive, count 0 (THIS is the issue signal).
    assert by_event["newsletter_signup"]["count"] == 0
    assert by_event["newsletter_signup"]["active"] is False and by_event["newsletter_signup"]["status"] == "Inactive"
    assert by_event["view_item"]["status"] == "Inactive"
    # Firing events NOT in the predefined list are APPENDED (Active, listed=False), most-active first.
    extras = [t for t in tracked if not t["listed"]]
    assert [t["event"] for t in extras] == ["session_start", "my_custom_event"], extras
    assert all(t["active"] and t["status"] == "Active" for t in extras)
    print("ok  hybrid table (predefined kept Inactive + firing unlisted appended)")


def test_tracked_all_inactive_on_empty():
    # No data / failed fetch still renders the full list, all Inactive at 0.
    r = atrium_ga4.fetch_event_counts("123456789", runner=lambda p, d: {"rows": []})
    assert [t["event"] for t in r["tracked"]] == atrium_ga4.TRACKED_EVENTS
    assert all(t["count"] == 0 and t["status"] == "Inactive" for t in r["tracked"])
    print("ok  empty window -> full table, all Inactive")


def test_invalid_id():
    r = atrium_ga4.fetch_event_counts("G-QP45TYMEB6", runner=_canned)
    assert r["ok"] is False and "property id" in r["error"].lower(), r
    print("ok  invalid id -> friendly error")


def test_api_error_degrades():
    def boom(_pid, _days):
        raise RuntimeError("403 PERMISSION_DENIED: caller lacks access")
    r = atrium_ga4.fetch_event_counts("123456789", runner=boom)
    assert r["ok"] is False and "access" in r["error"], r
    print("ok  API 403 -> graceful 'access' message")


def test_empty_window():
    r = atrium_ga4.fetch_event_counts("123456789", runner=lambda p, d: {"rows": []})
    assert r["ok"] is True and r["rows"] == [] and r["total_events"] == 0, r
    assert "no events" in r["error"].lower(), r
    print("ok  empty window -> ok with note")


def test_disabled_without_runner():
    # No runner injected AND feature off -> short-circuits, never touches the network.
    assert atrium_ga4.is_enabled() is False
    r = atrium_ga4.fetch_event_counts("123456789")
    assert r["ok"] is False and "turned off" in r["error"], r
    print("ok  disabled (no env) -> off, no network")


if __name__ == "__main__":
    test_normalize_property_id()
    test_parse_and_sort()
    test_tracked_table()
    test_tracked_all_inactive_on_empty()
    test_invalid_id()
    test_api_error_degrades()
    test_empty_window()
    test_disabled_without_runner()
    print("\nALL GA4 TESTS PASSED")
