"""Seed data for the portal registry (platform.json).

This module holds the INITIAL contents of the registry that `seed_registry.py` writes into the
private GCS object `agora-data-driven-platform-dash/platform.json` -- the ONE source of truth for
the portal. There is no database; the registry is a single JSON document and these dicts are its
first snapshot.

What is here at standup:
  * The sole agency, `agora` ("Agora Data Driven"). Its client list starts EMPTY -- clients are
    added later through the portal admin/super-admin UI, not hand-edited here.
  * NO seeded clients. The registry starts with an empty client list; real clients are created
    through the Workspaces console (/admin/atrium/new -> onboard_client), which appends entries of
    the shape documented below. (We used to seed a placeholder `template` client; it was a stand-in
    with no real workspace, so it is no longer seeded -- a fresh portal starts clean.)

Naming derivation (kept consistent with the rest of the monorepo, derived from a client key `<c>`):
  * subdomain:    <c>.agoradatadriven.com
  * dash service: <c>-dash            (Cloud Run service)
  * password secret (upstream dash):  <c>-dash-password   (resolved by store.get_client_dash_password)

These are plain dicts on purpose: `seed_registry.py` consumes them as-is and `store.py` reads/writes
the same shape over GCS. Keep them serialisable (no objects, no callables).
"""

ROOT_DOMAIN = "agoradatadriven.com"

# The sole agency. Display name is separate from the key so the UI can rename without re-keying.
# `clients` starts EMPTY: client memberships are managed in the UI after standup, not seeded here.
AGENCIES = [
    {
        "key": "agora",
        "name": "Agora Data Driven",
        "clients": [],  # filled in later via the admin UI
    },
]

# Clients (dashboards) the portal knows about. This starts EMPTY: clients are created through the
# Workspaces console (store.add_client via onboard_client), which appends entries of exactly the
# shape below:
#     {
#         "key": "honeytribe",
#         "name": "Honey Tribe",
#         "subdomain": "honeytribe.agoradatadriven.com",
#         # The upstream Cloud Run dashboard service this client maps to. The portal reverse-proxies
#         # it under /d/<key>/ and logs into it server-side using <key>-dash-password.
#         "dash_service": "honeytribe-dash",
#         # Portal-login material is set in the UI (set_client_password):
#         #   "pw_hash":  pbkdf2_hmac hex digest (what verify_portal_login checks)
#         #   "pw_salt":  hex salt for that hash
#         #   "pw_plain": RECOVERABLE plaintext kept beside the hash so the super-admin console can
#         #               reveal it (see store.py for the deliberate trade-off comment).
#     }
CLIENTS = []

# Accounts (real per-user email+password logins) start EMPTY too. They are created at runtime by
# self-service sign-up (status "pending" until an admin approves) or seeded directly (e.g. the
# dev@localhost admin in seed_local.py). store.py owns the shape; one entry looks like:
#     {
#         "email": "owner@company.com",      # the login identity (unique, normalised lowercase)
#         "name": "Owner Name",              # display name / company at sign-up
#         "role": "client",                  # "client" -> own keys; "admin" -> clients ["*"]
#         "status": "pending",               # only "active" may log in; sign-ups start "pending"
#         "clients": [],                     # client keys this account may open (["*"] for admin)
#         "pw_hash"/"pw_salt"/"pw_plain":    # same pbkdf2 + recoverable-plaintext scheme as clients
#         "requested_name": "Company Co",    # (pending client sign-ups) the company requested
#         "created_at": "2026-06-23T09:00:00Z",
#     }
ACCOUNTS = []


def initial_registry():
    """Return the first full registry snapshot as a plain dict (what seed_registry.py writes).

    `version` lets future migrations detect/upgrade the on-disk shape; bump it when the schema of
    platform.json changes.
    """
    return {
        "version": 1,
        "root_domain": ROOT_DOMAIN,
        "agencies": [dict(a) for a in AGENCIES],
        "clients": [dict(c) for c in CLIENTS],
        "accounts": [dict(a) for a in ACCOUNTS],
        # CRM: future top-level collections (contacts, notes, tasks, deals) grow alongside these.
    }
