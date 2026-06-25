"""Registry store -- CRUD over the portal's ONE private JSON document (no database).

The portal's entire state lives in a single private GCS object:
    bucket  agora-data-driven-platform-dash
    object  platform.json
This module reads/writes that document and resolves portal logins. It is the only code that touches
the registry shape defined in config.py.

Password model (resolve ORDER for verify_portal_login):
  1. SUPER-ADMIN: env SUPER_ADMIN_PW (mounted from Secret Manager `platform-super-admin-password`
     by tools/enable_super_admin.ps1). Matching this grants god-mode ("*", all clients).
  2. ACCOUNTS: a real per-user email+password account in platform.json (the `accounts` list). Login
     matches EMAIL + password; only an `active` account authenticates (a `pending` sign-up does not).
     An account carries a role ("admin" -> clients ["*"]; "client" -> its own client keys). This is
     the modern path -- accounts are created by self-service sign-up (status "pending", approved by an
     admin) or seeded directly (e.g. the dev@localhost admin). See the account CRUD below.
  3. REGISTRY: a per-client pbkdf2_hmac hash stored in platform.json (set via set_client_password).
     This is the legacy/demo path -- it matches a PASSWORD to a client regardless of the email typed.
  4. BOOTSTRAP: env PORTAL_BOOTSTRAP_PW -- a fallback so the very first login works before any
     registry password has been set. Unset it once real passwords exist.

Recoverable-plaintext trade-off (DELIBERATE):
  Alongside each pbkdf2 hash we ALSO keep the plaintext (`pw_plain`) in the registry. This is a
  conscious security trade-off: it lets the super-admin console REVEAL a client's portal password
  (reveal_password) so an operator can read it back to a client who lost it -- a CRM/helpdesk need.
  The registry object is PRIVATE (never public, bucket is not world-readable), so the plaintext
  never leaves the trust boundary; but anyone with read access to platform.json can see passwords.
  If that ever becomes unacceptable, drop `pw_plain` and reveal_password() degrades to "unavailable"
  while verify_portal_login keeps working off the pbkdf2 hash alone.

All password comparisons use hmac.compare_digest (constant-time); hashes use hashlib.pbkdf2_hmac.
"""

import datetime
import hashlib
import hmac
import json
import os

# --- Fixed locations ----------------------------------------------------------------------------
# Env var names match what deploy_dash_platform.ps1 sets (REGISTRY_BUCKET / REGISTRY_OBJECT); the
# defaults are the literal standup values so the module is correct even if the env is unset.
REGISTRY_BUCKET = os.environ.get("REGISTRY_BUCKET", "agora-data-driven-platform-dash")
REGISTRY_OBJECT = os.environ.get("REGISTRY_OBJECT", "platform.json")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "agora-data-driven")

# pbkdf2 parameters. 200k iterations of SHA-256 is a sane 2020s default for a small operator tool.
_PBKDF2_ITERATIONS = 200_000
_PBKDF2_ALGO = "sha256"


# --- Storage backend (GCS by default; local filesystem when REGISTRY_LOCAL_DIR is set) ----------
# Mirrors workspace.py: google-cloud-storage is imported LAZILY (only when the GCS backend is
# actually used) and the client is built on first use, so importing this module never needs the
# package or ADC. Set REGISTRY_LOCAL_DIR=<dir> to read/write platform.json as a plain file under
# that directory instead of GCS -- this is what lets the portal run on a laptop with no GCP access
# (see seed_registry / onboard_client / run_local.ps1).
_storage_client = None


def _local_dir():
    """The local-filesystem backend root, or "" to use GCS."""
    return os.environ.get("REGISTRY_LOCAL_DIR", "")


def _gcs_blob():
    """Lazily construct the GCS client and return the registry blob handle."""
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage  # lazy: only the GCS backend needs the package
        _storage_client = storage.Client()
    return _storage_client.bucket(REGISTRY_BUCKET).blob(REGISTRY_OBJECT)


# --- Registry I/O -------------------------------------------------------------------------------
def load_registry():
    """Return the registry dict from platform.json, or an empty skeleton if the object is absent.

    An absent object is normal BEFORE seed_registry.py has run; callers should treat the empty
    skeleton (no agencies, no clients) as "not seeded yet".
    """
    local = _local_dir()
    if local:
        path = os.path.join(local, REGISTRY_OBJECT)
        if not os.path.isfile(path):
            return {"version": 1, "agencies": [], "clients": []}
        with open(path, "r", encoding="utf-8") as fh:
            return json.loads(fh.read())
    blob = _gcs_blob()
    if not blob.exists():
        return {"version": 1, "agencies": [], "clients": []}
    return json.loads(blob.download_as_bytes().decode("utf-8"))


def save_registry(registry):
    """Persist the registry dict back to platform.json (private; never made public)."""
    body = json.dumps(registry, indent=2, sort_keys=True).encode("utf-8")
    local = _local_dir()
    if local:
        path = os.path.join(local, REGISTRY_OBJECT)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(body)
        return
    _gcs_blob().upload_from_string(body, content_type="application/json")


# --- Client registry CRUD -----------------------------------------------------------------------
def list_clients(registry=None):
    """Return the list of client dicts in the registry (loads it if not supplied)."""
    reg = registry if registry is not None else load_registry()
    return list(reg.get("clients", []))


def get_client(key, registry=None):
    """Return the client dict for `key`, or None."""
    for c in list_clients(registry):
        if c.get("key") == key:
            return c
    return None


def add_client(key, name=None, registry=None):
    """Add a new client (dashboard) to the registry and persist it. Idempotent on `key`.

    Derives the standard resource names from the client key `<c>` so callers never re-type them:
      subdomain    -> <c>.agoradatadriven.com
      dash_service -> <c>-dash
    """
    reg = registry if registry is not None else load_registry()
    reg.setdefault("clients", [])
    if any(c.get("key") == key for c in reg["clients"]):
        return reg  # already present -- do not clobber
    reg["clients"].append({
        "key": key,
        "name": name or key.title(),
        "subdomain": "%s.agoradatadriven.com" % key,
        "dash_service": "%s-dash" % key,
    })
    save_registry(reg)
    return reg


def remove_client(key, registry=None):
    """Remove a client from the registry (a delete). Returns True if a client was removed, else False.

    Only drops the registry entry (login + listing). The client's Atrium workspace object is a
    separate concern -- the caller deletes it via workspace.delete_workspace if desired. Persists
    only when something actually changed, so calling it on an unknown key is a cheap no-op.
    """
    reg = registry if registry is not None else load_registry()
    before = reg.get("clients", [])
    after = [c for c in before if c.get("key") != key]
    if len(after) == len(before):
        return False  # nothing matched -- do not rewrite the registry
    reg["clients"] = after
    save_registry(reg)
    return True


def restore_client(client, registry=None):
    """Re-insert a previously-removed client dict verbatim (Trash restore). Returns the registry.

    Restores the full entry (its derived names AND its password hash, if any) so the client's login
    keeps working. Idempotent on the client key (won't duplicate on a double-restore)."""
    reg = registry if registry is not None else load_registry()
    reg.setdefault("clients", [])
    key = (client or {}).get("key")
    if not key or any(c.get("key") == key for c in reg["clients"]):
        return reg  # missing key, or already present -- do not clobber
    reg["clients"].append(dict(client))
    save_registry(reg)
    return reg


def set_client_name(key, name, registry=None):
    """Update a client's display name in the registry (a rename). No-op if the client is unknown,
    the name is blank, or it is already current. Persists and returns the registry."""
    reg = registry if registry is not None else load_registry()
    client = get_client(key, reg)
    if client is None or not name or client.get("name") == name:
        return reg
    client["name"] = name
    save_registry(reg)
    return reg


# --- Password hashing helpers -------------------------------------------------------------------
def _hash_password(password, salt_hex):
    """Return the pbkdf2_hmac hex digest of `password` with the given hex salt."""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return dk.hex()


def set_client_password(key, password, registry=None):
    """Set a client's PORTAL login password: store its pbkdf2 hash + salt AND the recoverable
    plaintext (see module docstring for that trade-off). Persists and returns the registry."""
    reg = registry if registry is not None else load_registry()
    client = get_client(key, reg)
    if client is None:
        raise KeyError("unknown client '%s'" % key)
    salt_hex = os.urandom(16).hex()
    client["pw_salt"] = salt_hex
    client["pw_hash"] = _hash_password(password, salt_hex)
    # Recoverable plaintext kept beside the hash so the super-admin console can reveal it.
    client["pw_plain"] = password
    save_registry(reg)
    return reg


def reveal_password(key, registry=None):
    """Return a client's recoverable plaintext portal password, or None if not stored.

    Only the super-admin console should call this. Returns None (not an error) when pw_plain was
    never kept -- e.g. if the recoverable-plaintext trade-off is later disabled.
    """
    client = get_client(key, registry)
    if client is None:
        return None
    return client.get("pw_plain")


# --- Accounts (real per-user email+password logins) ---------------------------------------------
# An account is one dict in the registry's top-level `accounts` list:
#   {
#     "email":   "owner@company.com",     # normalised lowercase; the login identity (unique)
#     "name":    "Owner Name",            # display name (or the company at sign-up time)
#     "role":    "client" | "admin",      # admin -> clients ["*"]; client -> its own client keys
#     "status":  "active" | "pending",    # only `active` may log in; sign-ups start `pending`
#     "clients": ["riverdance"] | ["*"],  # what this account may open
#     "pw_salt"/"pw_hash"/"pw_plain":     # same scheme + recoverable-plaintext trade-off as clients
#     "requested_name": "Company Co",     # (pending client sign-ups) the company they asked for
#     "created_at": "2026-06-23T09:00:00Z"
#   }
def _norm_email(email):
    """Normalise an email to its canonical comparison form (trimmed, lowercase)."""
    return (email or "").strip().lower()


def _now_iso():
    """Current UTC time as an ISO-8601 Z string (used for account created_at)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_accounts(registry=None):
    """Return the list of account dicts in the registry (loads it if not supplied)."""
    reg = registry if registry is not None else load_registry()
    return list(reg.get("accounts", []))


def get_account(email, registry=None):
    """Return the account dict for `email` (case-insensitive), or None."""
    norm = _norm_email(email)
    if not norm:
        return None
    for a in list_accounts(registry):
        if _norm_email(a.get("email")) == norm:
            return a
    return None


def add_account(email, password, name=None, role="client", clients=None,
                status="pending", requested_name=None, registry=None):
    """Create a new account and persist it. Returns the account dict, or None if the email is taken.

    Stores the pbkdf2 hash + salt AND the recoverable plaintext (same trade-off as client passwords).
    `role` is coerced to client/admin and `status` to active/pending so callers can't store garbage.
    """
    reg = registry if registry is not None else load_registry()
    reg.setdefault("accounts", [])
    norm = _norm_email(email)
    if not norm:
        raise ValueError("email is required")
    if any(_norm_email(a.get("email")) == norm for a in reg["accounts"]):
        return None  # already exists -- caller treats None as "taken"
    salt_hex = os.urandom(16).hex()
    account = {
        "email": norm,
        "name": name or norm.split("@")[0],
        "role": role if role in ("superadmin", "admin", "client") else "client",
        "status": status if status in ("active", "pending") else "pending",
        "clients": list(clients) if clients else [],
        "pw_salt": salt_hex,
        "pw_hash": _hash_password(password, salt_hex),
        "pw_plain": password,
        "created_at": _now_iso(),
    }
    if requested_name:
        account["requested_name"] = requested_name
    reg["accounts"].append(account)
    save_registry(reg)
    return account


def set_account_status(email, status, registry=None):
    """Set an account's status to 'active' or 'pending'. Raises KeyError if unknown."""
    reg = registry if registry is not None else load_registry()
    account = get_account(email, reg)
    if account is None:
        raise KeyError("unknown account '%s'" % email)
    if status not in ("active", "pending"):
        raise ValueError("status must be 'active' or 'pending'")
    account["status"] = status
    save_registry(reg)
    return account


def set_account_clients(email, clients, registry=None):
    """Set which client keys an account may open (e.g. after approving a sign-up). Raises KeyError."""
    reg = registry if registry is not None else load_registry()
    account = get_account(email, reg)
    if account is None:
        raise KeyError("unknown account '%s'" % email)
    account["clients"] = list(clients) if clients else []
    save_registry(reg)
    return account


def set_account_password(email, password, registry=None):
    """Reset an account's password (new salt + hash + recoverable plaintext). Raises KeyError."""
    reg = registry if registry is not None else load_registry()
    account = get_account(email, reg)
    if account is None:
        raise KeyError("unknown account '%s'" % email)
    salt_hex = os.urandom(16).hex()
    account["pw_salt"] = salt_hex
    account["pw_hash"] = _hash_password(password, salt_hex)
    account["pw_plain"] = password
    save_registry(reg)
    return account


def remove_account(email, registry=None):
    """Delete an account (e.g. rejecting a sign-up request). Returns True if one was removed."""
    reg = registry if registry is not None else load_registry()
    before = reg.get("accounts", [])
    norm = _norm_email(email)
    after = [a for a in before if _norm_email(a.get("email")) != norm]
    if len(after) == len(before):
        return False
    reg["accounts"] = after
    save_registry(reg)
    return True


def ensure_admin_account(email, password, name=None, role="admin", registry=None):
    """Create an ACTIVE admin (or superadmin) account (clients ["*"]) if none exists for `email`.

    Idempotent: returns the existing account untouched if present (so re-seeding never clobbers a
    password that was later changed). Used to seed the operator accounts for local preview.
    """
    reg = registry if registry is not None else load_registry()
    existing = get_account(email, reg)
    if existing is not None:
        return existing
    role = "superadmin" if role == "superadmin" else "admin"
    return add_account(email, password, name=name, role=role, clients=["*"],
                       status="active", registry=reg)


def ensure_super_admin_account(email, password, name=None, registry=None):
    """Seed THE super admin (role 'superadmin', clients ["*"]). Idempotent (see ensure_admin_account)."""
    return ensure_admin_account(email, password, name=name, role="superadmin", registry=registry)


# --- Login verification (resolve ORDER: super-admin -> accounts -> registry hash -> bootstrap) ---
def verify_portal_login(user, password, registry=None):
    """Verify a portal login and return the list of client keys the bearer may open.

    Returns:
      ["*"]            -- super-admin (all clients), on SUPER_ADMIN_PW or bootstrap match.
      [<client keys>]  -- the specific clients whose registry password matched.
      []               -- no match (caller must treat empty as "denied").

    Resolve order (first match wins):
      1. super-admin env (SUPER_ADMIN_PW)  -> "*"
      2. account email+password (active only) -> that account's clients ("*" for an admin account)
      3. registry per-client pbkdf2 hash   -> [that client]  (all matching clients are OR-ed in)
      4. bootstrap env (PORTAL_BOOTSTRAP_PW) -> "*"
    All comparisons constant-time (hmac.compare_digest).
    """
    reg = registry if registry is not None else load_registry()

    # 1. Super-admin: env-mounted secret. Matching it grants every client.
    super_pw = os.environ.get("SUPER_ADMIN_PW", "")
    if super_pw and hmac.compare_digest(password, super_pw):
        return ["*"]

    # 2. Accounts: a real per-user EMAIL + password login. Only an `active` account authenticates
    #    (a `pending` sign-up is intentionally denied until an admin approves it). An admin account's
    #    clients are ["*"]; a client account returns its own keys.
    norm = _norm_email(user)
    for a in reg.get("accounts", []):
        if a.get("status") != "active" or _norm_email(a.get("email")) != norm:
            continue
        salt_hex = a.get("pw_salt")
        pw_hash = a.get("pw_hash")
        if not salt_hex or not pw_hash:
            continue
        if hmac.compare_digest(_hash_password(password, salt_hex), pw_hash):
            clients = a.get("clients") or []
            return ["*"] if "*" in clients else list(clients)

    # 3. Registry per-client pbkdf2 hash. A user may match more than one client; grant all matches.
    allowed = []
    for c in reg.get("clients", []):
        salt_hex = c.get("pw_salt")
        pw_hash = c.get("pw_hash")
        if not salt_hex or not pw_hash:
            continue
        candidate = _hash_password(password, salt_hex)
        if hmac.compare_digest(candidate, pw_hash):
            allowed.append(c.get("key"))
    if allowed:
        return allowed

    # 4. Bootstrap fallback so the very first login works before any registry password is set.
    boot_pw = os.environ.get("PORTAL_BOOTSTRAP_PW", "")
    if boot_pw and hmac.compare_digest(password, boot_pw):
        return ["*"]

    return []


# --- Upstream dashboard password (Secret Manager) -----------------------------------------------
def get_client_dash_password(key):
    """Resolve a client's UPSTREAM dashboard password from Secret Manager (secret `<c>-dash-password`).

    This is the password the dashboard's OWN Flask gate checks. The portal uses it to log into the
    upstream <c>-dash ONCE, server-side, when reverse-proxying that dashboard -- so the end user only
    ever enters the portal password. Distinct from the portal-login password kept in the registry.

    Imported lazily so a missing google-cloud-secret-manager dependency cannot break module import
    (the registry CRUD above must work even where Secret Manager access is not configured).
    """
    from google.cloud import secretmanager  # lazy import; see docstring

    client = secretmanager.SecretManagerServiceClient()
    secret_name = "%s-dash-password" % key
    resource = "projects/%s/secrets/%s/versions/latest" % (PROJECT, secret_name)
    response = client.access_secret_version(request={"name": resource})
    # Secret Manager stores bytes verbatim; the upstream secret was written WITHOUT a BOM or trailing
    # newline (see Write-SecretFile in the .ps1 helpers), so decode straight through.
    return response.payload.data.decode("utf-8")
