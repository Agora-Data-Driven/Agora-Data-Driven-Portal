"""Registry store -- CRUD over the portal's ONE private JSON document (no database).

The portal's entire state lives in a single private GCS object:
    bucket  agora-data-driven-platform-dash
    object  platform.json
This module reads/writes that document and resolves portal logins. It is the only code that touches
the registry shape defined in config.py.

Password model (resolve ORDER for verify_portal_login):
  1. SUPER-ADMIN: env SUPER_ADMIN_PW (mounted from Secret Manager `platform-super-admin-password`
     by tools/enable_super_admin.ps1). Matching this grants god-mode ("*", all clients).
  2. REGISTRY: a per-client pbkdf2_hmac hash stored in platform.json (set via set_client_password).
  3. BOOTSTRAP: env PORTAL_BOOTSTRAP_PW -- a fallback so the very first login works before any
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


# --- Login verification (resolve ORDER: super-admin -> registry hash -> bootstrap) ---------------
def verify_portal_login(user, password, registry=None):
    """Verify a portal login and return the list of client keys the bearer may open.

    Returns:
      ["*"]            -- super-admin (all clients), on SUPER_ADMIN_PW or bootstrap match.
      [<client keys>]  -- the specific clients whose registry password matched.
      []               -- no match (caller must treat empty as "denied").

    Resolve order (first match wins):
      1. super-admin env (SUPER_ADMIN_PW)  -> "*"
      2. registry per-client pbkdf2 hash   -> [that client]  (all matching clients are OR-ed in)
      3. bootstrap env (PORTAL_BOOTSTRAP_PW) -> "*"
    All comparisons constant-time (hmac.compare_digest).
    """
    reg = registry if registry is not None else load_registry()

    # 1. Super-admin: env-mounted secret. Matching it grants every client.
    super_pw = os.environ.get("SUPER_ADMIN_PW", "")
    if super_pw and hmac.compare_digest(password, super_pw):
        return ["*"]

    # 2. Registry per-client pbkdf2 hash. A user may match more than one client; grant all matches.
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

    # 3. Bootstrap fallback so the very first login works before any registry password is set.
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
