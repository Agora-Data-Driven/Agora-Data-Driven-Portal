"""Off-cloud test for the email+password accounts model in store.py (no GCS, no Flask).

Points the registry at a throwaway local dir and exercises the full account lifecycle the way the
sign-up + admin-approval flow uses it: seed an admin, deny a pending sign-up, approve it, then verify
the login resolves to the right clients. Mirrors _workspace_localtest.py: pure data layer, runnable
in CI with no credentials.

Run:  python _accounts_localtest.py   (exit 0 = pass)
"""

import os
import sys
import tempfile

FAILS = []


def check(label, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def main():
    tmp = tempfile.mkdtemp(prefix="accounts_localtest_")
    os.environ["REGISTRY_LOCAL_DIR"] = tmp
    print("[localtest] REGISTRY_LOCAL_DIR = %s" % tmp)

    import store

    # --- Admin account (seeded, like dev@localhost) -------------------------------------------
    store.ensure_admin_account("dev@localhost", "dev-admin", name="Dev Admin")
    check("admin login resolves to '*'", store.verify_portal_login("dev@localhost", "dev-admin") == ["*"])
    check("admin login is case-insensitive on email",
          store.verify_portal_login("DEV@LOCALHOST", "dev-admin") == ["*"])
    check("admin wrong password denied", store.verify_portal_login("dev@localhost", "nope") == [])
    # ensure_admin is idempotent: a second call must NOT clobber the existing password.
    store.ensure_admin_account("dev@localhost", "different-password")
    check("ensure_admin_account is idempotent (password unchanged)",
          store.verify_portal_login("dev@localhost", "dev-admin") == ["*"])

    # --- Client sign-up: pending account cannot log in ----------------------------------------
    acct = store.add_account("owner@riverdance.test", "river-pw", name="Riverdance RV",
                             role="client", clients=[], status="pending",
                             requested_name="Riverdance RV")
    check("add_account returns the new account", acct is not None and acct.get("status") == "pending")
    check("pending account cannot log in (denied)",
          store.verify_portal_login("owner@riverdance.test", "river-pw") == [])
    check("duplicate email is rejected (returns None)",
          store.add_account("OWNER@riverdance.test", "x", status="pending") is None)

    # --- Approve: link a client + activate -> login now works ---------------------------------
    store.set_account_clients("owner@riverdance.test", ["riverdance"])
    store.set_account_status("owner@riverdance.test", "active")
    check("approved client login resolves to its client key",
          store.verify_portal_login("owner@riverdance.test", "river-pw") == ["riverdance"])
    check("active client still denied on wrong password",
          store.verify_portal_login("owner@riverdance.test", "river-pw-WRONG") == [])

    # --- A legacy per-client password (email-agnostic) still works alongside accounts ----------
    store.add_client("legacyco", "Legacy Co")
    store.set_client_password("legacyco", "legacy-pw")
    check("legacy per-client password still authenticates (any email)",
          store.verify_portal_login("anyone@example.com", "legacy-pw") == ["legacyco"])

    # --- Reject: removing the account denies the login ----------------------------------------
    check("remove_account reports a removal", store.remove_account("owner@riverdance.test") is True)
    check("removed account can no longer log in",
          store.verify_portal_login("owner@riverdance.test", "river-pw") == [])
    check("remove_account on unknown email is a no-op", store.remove_account("ghost@nope.test") is False)

    # --- Persistence: a fresh load sees the same accounts -------------------------------------
    reg = store.load_registry()
    emails = [a.get("email") for a in reg.get("accounts", [])]
    check("admin account persisted to disk", "dev@localhost" in emails)
    check("rejected account not persisted", "owner@riverdance.test" not in emails)

    if FAILS:
        print("\n[localtest] FAIL (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\n[accounts-localtest] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
