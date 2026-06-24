"""Portal / CRM front-door for Agora Data Driven (Cloud Run service `platform-dash`).

What this is:
  ONE Cloud Run service at portal.agoradatadriven.com that is (a) a single login over every
  client dashboard and (b) a reverse proxy that serves each dashboard under /d/<client>/...
  behind that single login. It is designed to GROW INTO A CRM: the registry is one private JSON
  in GCS (agora-data-driven-platform-dash/platform.json) and the `# CRM:` markers below show
  where client records, notes, and tasks attach as the portal expands.

Auth model (two cooperating layers):
  1. Portal session -- a signed Flask session cookie set on successful /login.
  2. SSO -- on login the portal ALSO mints the shared .agoradatadriven.com-scoped cookie via
     platform_sso.mint_sso_cookie. That cookie is presented to every <c>.agoradatadriven.com
     dashboard so a portal login is trusted additively (each dashboard's own password still works).

Reverse proxy (/d/<client>/...):
  The portal logs into the upstream <client>-dash service ONCE, server-side, using that dash's own
  Secret-Manager password (store.get_client_dash_password), holds the upstream session in a cookie
  jar, and proxies requests/responses. It injects a small logout pill + feedback widget into proxied
  HTML so the user always has a way back to the portal. This means the end user only ever types the
  PORTAL password -- they never see the per-dashboard password.

Org policy forbids public Cloud Run: deploy with --no-invoker-iam-check (never
--allow-unauthenticated); this app does its OWN auth in-process.
"""

import datetime
import os
import re
import secrets

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import atrium_docs
import atrium_view
import brand
import notify
import platform_sso
import store
import workspace
import feedback as feedback_store

# Product name for the Atrium client workspace (kept in one place so it is easy to change).
WORKSPACE_NAME = "Agora Atrium"

# --- Configuration from the environment --------------------------------------------------
# SESSION_SECRET signs the Flask session cookie; mounted from Secret Manager at deploy time.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# SSO_SECRET is the shared HMAC key (Secret Manager `platform-sso-key`) the portal signs the
# .agoradatadriven.com SSO cookie with and every dashboard verifies against.
SSO_SECRET = os.environ.get("SSO_SECRET", "")
# COOKIE_DOMAIN scopes the SSO cookie across all subdomains (leading dot). Defaults to the
# platform_sso constant so a missing env var is still correct.
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", platform_sso.COOKIE_DOMAIN)
# REGION is needed to address the upstream Cloud Run dashboards (set by enable_super_admin.ps1).
REGION = os.environ.get("REGION", "asia-southeast1")

app = Flask(__name__)
app.secret_key = SESSION_SECRET

# Cookie hardening. SameSite=None + Secure is REQUIRED for the cross-subdomain portal flow
# (portal.agoradatadriven.com -> <c>.agoradatadriven.com): a Lax/Strict cookie would be dropped
# on the cross-site navigation. HttpOnly keeps the session out of JS.
# In production these MUST stay on; locally they are relaxed (PORTAL_SECURE_COOKIES=0) because a
# browser drops a Secure cookie over plain http://localhost, which would make login loop. The
# default is the secure production posture, so a normal deploy is unaffected.
_secure_cookies = os.environ.get("PORTAL_SECURE_COOKIES", "1") != "0"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=_secure_cookies,
    SESSION_COOKIE_SAMESITE="None" if _secure_cookies else "Lax",
)

# Local preview ("no password") mode. When PORTAL_DEV_NOAUTH=1 every request is auto-signed-in as a
# super-admin, so a developer can double-click a launcher and click straight through the whole portal
# -- all clients, all Atrium workspaces, and the in-place admin edit affordances -- with no login.
# It is DELIBERATELY tied to `not _secure_cookies` (i.e. the local http posture, PORTAL_SECURE_COOKIES=0):
# production always serves over https with secure cookies ON, so even if this env var ever leaked into
# a deploy it would stay inert. Only the local launcher (run_local.ps1) ever sets it.
DEV_NOAUTH = os.environ.get("PORTAL_DEV_NOAUTH", "") == "1" and not _secure_cookies
# Deliberate LIVE-demo "no password" mode. Unlike DEV_NOAUTH (interlocked to the local http posture
# so it stays inert in prod), DEMO_NOAUTH works over https with secure cookies ON -- an operator can
# flip it on for a presentation and every request is auto-signed-in as a super-admin. OFF by default;
# only a deploy that sets PORTAL_DEMO_NOAUTH=1 turns it on. To demo the client-facing view, log in with
# a client password (role is fixed at login -- there is no in-session "view as client" toggle).
DEMO_NOAUTH = os.environ.get("PORTAL_DEMO_NOAUTH", "") == "1"
AUTO_LOGIN = DEV_NOAUTH or DEMO_NOAUTH
# Cap request bodies. On Cloud Run the platform caps requests at ~32 MiB, so live large videos use the
# signed-URL direct-to-GCS path. In LOCAL dev (local-fs backend, no GCS to sign for) there is no such
# cap and no signing, so allow large in-app uploads (~1 GB) so the same "Upload .mp4" button works
# end-to-end off-cloud. _LOCAL_BACKEND is true exactly when the workspace runs on the local-fs backend.
_LOCAL_BACKEND = bool(os.environ.get("WORKSPACE_LOCAL_DIR"))
app.config["MAX_CONTENT_LENGTH"] = (1100 * 1024 * 1024) if _LOCAL_BACKEND else (32 * 1024 * 1024)

# Per-upstream-dashboard proxy sessions, keyed by client key. Each holds the upstream <c>-dash
# login cookie so we only log into a dashboard ONCE server-side, then reuse the session. This is
# an in-process cache; on a cold start it is rebuilt lazily on first proxy request.
_upstream_sessions = {}

# Hop-by-hop headers must NOT be forwarded when proxying (RFC 7230 sec 6.1) -- forwarding them
# corrupts the proxied response (e.g. a stale Content-Length, double Transfer-Encoding).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}


# --- Auth helpers ------------------------------------------------------------------------
def authed():
    """True iff the current request has a valid portal session."""
    return session.get("ok") is True


def current_user():
    """The logged-in portal user's identifier (email), or None."""
    return session.get("user")


def allowed_clients():
    """Client keys this session may open -- ["*"] for super-admin, else a concrete list."""
    return session.get("clients") or []


def can_open(client_key):
    """True iff the session is allowed to open `client_key` (super-admin "*" opens everything)."""
    allowed = allowed_clients()
    return "*" in allowed or client_key in allowed


def real_superadmin():
    """True iff this session actually holds the super-admin ("*") grant."""
    return "*" in allowed_clients()


def is_superadmin():
    """Super-admin: the session holds the "*" grant. Role is fixed at LOGIN, not toggleable.

    There is deliberately no "view as client" override: who you are (admin vs client) is decided by
    which password you logged in with (store.verify_portal_login -> ["*"] for admin, else the client
    keys). Removing the in-session toggle keeps the admin/client boundary a hard, login-derived line.
    """
    return real_superadmin()


# THE super admin: the one account allowed to create/manage OTHER admin accounts. Everyone with "*"
# is an "admin" (full client access); only the super admin (info@, or an account whose role is
# "superadmin") sits above them. Env-overridable so a different deploy can nominate its own owner.
SUPER_ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "info@agoradatadriven.com").strip().lower()


def current_account():
    """The registry account dict for the logged-in user, or None (env/bootstrap logins have none)."""
    user = current_user()
    return store.get_account(user) if user else None


def is_root_admin():
    """True iff this session is THE super admin -- may manage admin accounts.

    That is the configured SUPER_ADMIN_EMAIL (info@) or any account whose role is "superadmin". Must
    also hold full admin access ("*"), so a half-configured account can never escalate.
    """
    if not is_superadmin():
        return False
    if (current_user() or "").strip().lower() == SUPER_ADMIN_EMAIL:
        return True
    acct = current_account()
    return bool(acct and acct.get("role") == "superadmin")


def _gen_password():
    """A readable, strong portal password (mirrors onboard_client._generate_password)."""
    return secrets.token_urlsafe(9)


@app.before_request
def _dev_auto_login():
    """In local preview mode (DEV_NOAUTH), establish a super-admin session for every request.

    This is what turns the double-click launcher into a "no password" portal: the session looks
    exactly like a real super-admin login (`clients == ["*"]`), so all the normal auth checks
    (authed / can_open / is_superadmin) pass and the in-place admin edit affordances render. It is a
    no-op unless DEV_NOAUTH is on, which only happens locally (see the DEV_NOAUTH definition above).
    """
    if AUTO_LOGIN and not session.get("ok"):
        session["ok"] = True
        # Sign in as THE super admin so the no-password preview matches the seeded operator identity
        # (its Profile + admin-account management all resolve to info@agoradatadriven.com).
        session["user"] = SUPER_ADMIN_EMAIL
        session["clients"] = ["*"]


# --- Google Tag Manager (GTM) -- site-wide, opt-in via env, injected centrally -------------------
# Set GTM_CONTAINER_ID=GTM-XXXXXXX on the service to load the container on EVERY portal HTML page;
# unset (the default) means no tag loads at all (so local preview stays untracked). GA4 is configured
# INSIDE the container in the GTM UI -- the app only installs the container. Injecting here (one
# after_request hook) keeps the snippet out of all 7 self-contained templates, so it never touches
# the esprima JS gate or the "no Jinja in <script>" rule. The proxied client dashboards (/d/<c>/) are
# deliberately skipped -- they are the clients' OWN sites (which may carry their own GTM).
# The snippet is Google's standard install, verbatim, with the id substituted at the placeholder.
_GTM_HEAD = (
    "<!-- Google Tag Manager -->"
    "<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':"
    "new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],"
    "j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src="
    "'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);"
    "})(window,document,'script','dataLayer','__GTM_ID__');</script>"
    "<!-- End Google Tag Manager -->"
)
_GTM_BODY = (
    "<!-- Google Tag Manager (noscript) -->"
    "<noscript><iframe src=\"https://www.googletagmanager.com/ns.html?id=__GTM_ID__\""
    " height=\"0\" width=\"0\" style=\"display:none;visibility:hidden\"></iframe></noscript>"
    "<!-- End Google Tag Manager (noscript) -->"
)


@app.after_request
def _inject_gtm(resp):
    """Inject the GTM container into every portal HTML page when GTM_CONTAINER_ID is set.

    Head snippet goes as high in <head> as possible; the <noscript> goes right after <body>. No-op
    unless the env var is set, the response is HTML, and it isn't a streamed/proxied response."""
    gtm_id = os.environ.get("GTM_CONTAINER_ID", "").strip()
    if not gtm_id or resp.direct_passthrough:
        return resp
    if "text/html" not in (resp.content_type or "").lower():
        return resp
    if request.path.startswith("/d/"):        # reverse-proxied client dashboards -- not our page
        return resp
    try:
        html = resp.get_data(as_text=True)
    except (RuntimeError, UnicodeDecodeError):
        return resp
    if not html or gtm_id in html:            # idempotent: never inject the same container twice
        return resp
    head = _GTM_HEAD.replace("__GTM_ID__", gtm_id)
    body = _GTM_BODY.replace("__GTM_ID__", gtm_id)
    html = re.sub(r"<head[^>]*>", lambda m: m.group(0) + head, html, count=1, flags=re.IGNORECASE)
    html = re.sub(r"<body[^>]*>", lambda m: m.group(0) + body, html, count=1, flags=re.IGNORECASE)
    resp.set_data(html)
    return resp


def _visible_clients():
    """The client dicts this session is allowed to see, for the portal landing page."""
    clients = store.list_clients()
    if real_superadmin():
        return clients
    allowed = set(allowed_clients())
    return [c for c in clients if c.get("key") in allowed]


def _brand_ctx():
    """Shared brand assets for every rendered page (the AGORA mark + favicon, from brand.py).

    The deployed container only bundles dash/, so the mark lives in brand.py rather than being read
    from assets/ at runtime; this keeps the portal/login chrome in step with the Atrium sidebar.
    """
    return {"agora_logo": brand.AGORA_LOGO_LIGHT, "favicon": brand.FAVICON_DATA_URI}


def _post_login_destination(granted, next_url):
    """Where to send a user immediately after a successful login.

    A client who can open exactly ONE workspace lands straight on that workspace's overview -- the
    company overview -- instead of the portal card list, so a single-client login feels like "their"
    app. Super-admins ("*") and users with several clients keep the card list, and an explicit deep
    link (any next_url other than the bare "/") always wins so /admin, a specific /w/<c>/ tab, or a
    proxied dashboard link is never hijacked.
    """
    if next_url and next_url not in ("/", ""):
        return next_url
    if "*" not in granted and len(granted) == 1:
        only = granted[0]
        if workspace.workspace_exists(only):
            return url_for("atrium", client=only, tab="dashboard")
    return next_url or "/"


# --- Routes: auth + landing --------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    if not authed():
        return redirect(url_for("login", next="/"))
    # The team console IS the landing page for the agency operator -- a super-admin never sees the
    # old client portal page; they go straight to /admin/atrium. Clients still land on their portal.
    if is_superadmin():
        return redirect(url_for("admin_atrium"))
    return render_template(
        "portal.html",
        user=current_user(),
        clients=_visible_clients(),
        is_admin=authed(),
        is_superadmin=is_superadmin(),
        **_brand_ctx(),
    )


@app.route("/dashboard/<client>")
def client_dashboard(client):
    """Standalone full-screen Looker Studio dashboard for ONE client, reachable from the portal
    'Open dashboard' button -- it shows their live report without entering the full Atrium workspace.
    Gated to whoever may open the client (super-admin opens any; a client opens only their own). The
    URL + height come from the workspace settings (atrium_view.dashboard)."""
    if not authed():
        return redirect(url_for("login", next="/dashboard/%s" % client))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client) or {}
    name = ws.get("display_name") or (store.get_client(client) or {}).get("name") or client
    return render_template("dashboard_view.html", client=client, name=name,
                           dash=atrium_view.dashboard(ws, client),
                           user=current_user(), **_brand_ctx())


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "/")
    if request.method == "GET":
        if authed():
            return redirect(next_url or "/")
        return render_template("login.html", next=next_url, error=None, **_brand_ctx())

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    granted = store.verify_portal_login(email, password)
    if not granted:
        return render_template("login.html", next=next_url, email=email,
                               error="Incorrect email or password.", **_brand_ctx()), 401

    # Establish the portal session.
    session["ok"] = True
    session["user"] = email
    session["clients"] = granted

    resp = redirect(_post_login_destination(granted, next_url))
    # Mint the shared SSO cookie so the dashboards trust this portal login additively. Only do this
    # when the signing key is configured; without it SSO is simply inert (the dashboards' own
    # password gate still works), so a missing key must never break portal login.
    if SSO_SECRET:
        cookie_value = platform_sso.mint_sso_cookie(SSO_SECRET, granted, subject=email)
        resp.set_cookie(
            platform_sso.COOKIE_NAME,
            cookie_value,
            domain=COOKIE_DOMAIN,
            secure=True,
            httponly=True,
            samesite="None",
            max_age=platform_sso.DEFAULT_TTL_SECONDS,
        )
    return resp


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Self-service client sign-up -> creates a PENDING account an admin then approves.

    A visitor enters their company name, email, and a password; we record a `pending` client account
    in the registry (no client/workspace is created yet). They cannot log in until an admin approves
    the request from the team console (which creates the client + a blank workspace and activates the
    account). This keeps sign-up self-service for clients while the admin stays in control of access.
    """
    if request.method == "GET":
        return render_template("signup.html", error=None, sent=False, **_brand_ctx())

    company = request.form.get("company", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    # Friendly, specific validation (the design guidance: clear messages, email not username, no
    # confirm field -- the form offers a show-password toggle instead).
    domain = email.split("@")[-1] if "@" in email else ""
    if not company:
        error = "Please enter your company name."
    elif "@" not in email or "." not in domain:
        error = "Please enter a valid email address."
    elif len(password) < 6:
        error = "Please choose a password of at least 6 characters."
    elif store.get_account(email) is not None:
        error = "An account or pending request already exists for that email."
    else:
        error = None
    if error:
        return render_template("signup.html", error=error, sent=False,
                               company=company, email=email, **_brand_ctx()), 400

    store.add_account(email, password, name=company, role="client", clients=[],
                      status="pending", requested_name=company)
    # Let the team know a request is waiting (graceful: notify falls back to a stdout log if email
    # isn't configured, and never raises in a way that would fail the sign-up).
    try:
        notify.signup_requested(company, email)
    except Exception:
        pass
    return render_template("signup.html", error=None, sent=True,
                           company=company, email=email, **_brand_ctx())


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    resp = redirect(url_for("login"))
    # Clear the shared SSO cookie too (expire it on the same domain it was set).
    resp.set_cookie(
        platform_sso.COOKIE_NAME, "", domain=COOKIE_DOMAIN,
        secure=True, httponly=True, samesite="None", expires=0,
    )
    return resp


@app.route("/r", methods=["GET"])
def shared_recap():
    """Public, capability-URL recap page for the Overview "Share recap" button.

    DELIBERATELY UNAUTHENTICATED so a client can forward the link to a colleague who has no login.
    This does NOT weaken the "never make the data JSON public" rule: the recap data rides ENTIRELY in
    the URL #fragment (ROAS / leads / revenue / wins, base64), which browsers never send to the
    server -- so this route reads no client data, touches no bucket, and stores nothing. It serves a
    static branded shell that decodes the fragment and renders it client-side. The only thing exposed
    is what the client themselves chose to put in the link.

    TODO(recap-pdf): add a PDF export of this recap -- e.g. a "Download PDF" button here that renders
    the same recap to PDF (server-side via a headless renderer, or client-side). Copy-link ships now.
    """
    return render_template("recap.html", workspace_name=WORKSPACE_NAME, **_brand_ctx())


# --- Reverse proxy: /d/<client>/<path> ---------------------------------------------------
def _upstream_base_url(client_key):
    """Resolve the upstream dashboard's base URL.

    Prefer the client's subdomain (so the .agoradatadriven.com SSO cookie reaches it); fall back to
    the dash service name's default custom domain. The registry holds the canonical subdomain.
    """
    client = store.get_client(client_key)
    subdomain = (client or {}).get("subdomain") or ("%s.agoradatadriven.com" % client_key)
    return "https://%s" % subdomain


def _ensure_upstream_login(client_key):
    """Return a requests.Session logged into the upstream <client>-dash, creating it if needed.

    Logs in ONCE server-side using the dashboard's own Secret-Manager password and holds the
    upstream session cookie. Reused across proxied requests so the end user only ever enters the
    portal password.
    """
    sess = _upstream_sessions.get(client_key)
    if sess is not None:
        return sess

    sess = requests.Session()
    base = _upstream_base_url(client_key)
    try:
        dash_password = store.get_client_dash_password(client_key)
        # The dashboard's /login is a tiny form POST; a 302 (redirect to /) means success.
        sess.post(
            "%s/login" % base,
            data={"password": dash_password},
            allow_redirects=False,
            timeout=15,
        )
    except Exception:
        # Login failure is non-fatal here: we still return the session so the proxied request can
        # surface the upstream's own 401/login page rather than a 500. The user can then use the
        # dashboard's own password if needed.
        pass

    _upstream_sessions[client_key] = sess
    return sess


def _inject_portal_chrome(html, client_key):
    """Inject a logout pill + feedback widget into proxied dashboard HTML.

    Keeps the user oriented inside the portal frame: a way back/out, and a one-line feedback box
    that posts to the portal's own /feedback. Inserted just before </body> when present.
    """
    # Brand-aligned floating chrome: clean white pills + a green feedback CTA, readable on any
    # dashboard background (mirrors assets/brand.json -- green CTA, charcoal text).
    _pill = ("padding:7px 13px;border-radius:999px;background:#fff;border:1px solid #E7E8EE;"
             "text-decoration:none;font-size:12px;font-weight:600;"
             "box-shadow:0 4px 14px rgba(16,24,40,.16);")
    pill = (
        '<div id="ag-portal-chrome" style="position:fixed;top:12px;right:12px;z-index:2147483647;'
        'display:flex;gap:8px;align-items:center;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">'
        '<a href="/" style="%scolor:#353535;">All dashboards</a>'
        '<a href="/logout" style="%scolor:#E5413E;">Log out</a>'
        '<a href="/" title="Send feedback" style="padding:7px 13px;border-radius:999px;'
        'background:#4FAB4A;color:#fff;text-decoration:none;font-size:12px;font-weight:700;'
        'box-shadow:0 4px 14px rgba(79,171,74,.34);">Feedback</a>'
        '</div>'
    ) % (_pill, _pill)
    marker = "</body>"
    if marker in html:
        return html.replace(marker, pill + marker, 1)
    return html + pill


@app.route("/d/<client>/", defaults={"subpath": ""}, methods=["GET", "POST"])
@app.route("/d/<client>/<path:subpath>", methods=["GET", "POST"])
def proxy(client, subpath):
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    if store.get_client(client) is None:
        return Response("Unknown dashboard", status=404, mimetype="text/plain")

    sess = _ensure_upstream_login(client)
    base = _upstream_base_url(client)
    target = "%s/%s" % (base, subpath)
    if request.query_string:
        target = "%s?%s" % (target, request.query_string.decode("latin-1"))

    # Forward the request to the upstream dashboard, preserving method and body. We drop the inbound
    # Host header so requests sets it from the target URL.
    fwd_headers = {k: v for k, v in request.headers if k.lower() != "host"}
    try:
        upstream = sess.request(
            method=request.method,
            url=target,
            headers=fwd_headers,
            data=request.get_data(),
            allow_redirects=False,
            timeout=30,
        )
    except Exception:
        return Response("Upstream dashboard unavailable.", status=502, mimetype="text/plain")

    # Strip hop-by-hop headers from the upstream response before relaying it.
    resp_headers = [(k, v) for k, v in upstream.headers.items()
                    if k.lower() not in _HOP_BY_HOP]

    content_type = upstream.headers.get("Content-Type", "")
    body = upstream.content
    # Inject portal chrome only into HTML responses; leave JSON/assets untouched.
    if "text/html" in content_type.lower():
        try:
            html = body.decode("utf-8", errors="replace")
            html = _inject_portal_chrome(html, client)
            body = html.encode("utf-8")
        except Exception:
            # If injection fails for any reason, relay the original body unchanged.
            pass

    return Response(body, status=upstream.status_code, headers=resp_headers)


# --- Admin + super-admin consoles --------------------------------------------------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    """Admin console -- add client dashboards to the registry (the seed of the CRM)."""
    if not authed():
        return redirect(url_for("login", next="/admin"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")

    message = None
    if request.method == "POST":
        key = request.form.get("key", "").strip()
        name = request.form.get("name", "").strip()
        if key:
            # CRM: add_client appends a client record. As the CRM grows, this is where contact
            # details, plan, and onboarding state would be captured alongside the dashboard link.
            store.add_client(key, name or None)
            message = "Added client '%s'." % key
        else:
            message = "Client key is required."

    return render_template(
        "portal.html",
        user=current_user(),
        clients=_visible_clients(),
        is_admin=True,
        is_superadmin=is_superadmin(),
        admin_message=message,
        **_brand_ctx(),
    )


@app.route("/superadmin", methods=["GET", "POST"])
def superadmin():
    """Super-admin console -- set/reveal client portal passwords (operator helpdesk).

    reveal_password returns the RECOVERABLE plaintext kept beside each pbkdf2 hash (see store.py
    for the deliberate trade-off comment) so an operator can read a password back to a client.
    """
    if not authed():
        return redirect(url_for("login", next="/superadmin"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")

    message = None
    revealed = None
    if request.method == "POST":
        action = request.form.get("action", "")
        key = request.form.get("key", "").strip()
        if action == "set_password" and key:
            new_pw = request.form.get("password", "")
            try:
                store.set_client_password(key, new_pw)
                message = "Password set for '%s'." % key
            except KeyError:
                message = "Unknown client '%s'." % key
        elif action == "reveal" and key:
            revealed = store.reveal_password(key)
            message = ("Password for '%s': %s" % (key, revealed)) if revealed \
                else "No recoverable password stored for '%s'." % key

    return render_template(
        "portal.html",
        user=current_user(),
        clients=_visible_clients(),
        is_admin=True,
        is_superadmin=True,
        superadmin_message=message,
        revealed_password=revealed,
        **_brand_ctx(),
    )


# --- Feedback (text + voice) -------------------------------------------------------------
@app.route("/feedback", methods=["POST"])
def feedback():
    """Store user feedback in the portal's own private bucket; optionally enrich it with AI.

    Accepts either a text `message` field or an uploaded `audio` file (voice note). Enrichment
    (feedback_ai) is a no-op unless configured, so this route never depends on an LLM being wired.
    """
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")

    subject = current_user() or ""
    audio_file = request.files.get("audio")

    try:
        if audio_file is not None:
            audio_bytes = audio_file.read()
            # Optional AI: transcribe the voice note. Gracefully None if unconfigured.
            import feedback_ai
            transcript = feedback_ai.transcribe_voice(audio_bytes,
                                                      content_type=audio_file.mimetype or "audio/webm")
            extra = {"transcript": transcript} if transcript else None
            feedback_store.save_voice_feedback(
                audio_bytes,
                content_type=audio_file.mimetype or "audio/webm",
                subject=subject,
                extra=extra,
            )
        else:
            message = request.form.get("message", "")
            if not message.strip():
                return Response('{"error":"empty"}', status=400, mimetype="application/json")
            # Optional AI: summarise/interpret the text. Gracefully None if unconfigured.
            import feedback_ai
            summary = feedback_ai.summarize_text(message)
            extra = {"summary": summary} if summary else None
            feedback_store.save_text_feedback(message, subject=subject, extra=extra)
    except Exception:
        # Never fail loudly on feedback -- it must not block the user. Report a soft error.
        return Response('{"error":"could_not_store"}', status=500, mimetype="application/json")

    return Response('{"ok":true}', mimetype="application/json")


# --- Agora Atrium: the co-branded client workspace (the next step on the CRM growth path) -------
# Atrium is additive: it reuses the SAME session auth (authed / can_open / is_superadmin) and the
# SAME private bucket as the registry (per-client workspace/<c>.json via workspace.py). No new
# service, bucket, SA, IAM, or domain. Client-facing routes live under /w/<c>/; team management
# extends the operator console under /admin/atrium/.
ATRIUM_TABS = {"overview", "dashboard", "leadgen", "organic", "calendar", "conversations", "settings"}
# Team-only tabs: rendered ONLY for admins/super-admins (is_superadmin), never shown to clients. The
# Website Health tab monitors the client's live site + the marketing tags installed on it.
ATRIUM_TEAM_TABS = {"website-health"}


@app.template_filter("atrium_dt")
def _atrium_dt(iso):
    """Format a stored ISO-8601 Z timestamp as 'Jun 18, 3:40 PM' (cross-platform, no %- codes)."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return iso
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return "%s %d, %d:%02d %s" % (dt.strftime("%b"), dt.day, hour12, dt.minute, ampm)


@app.template_filter("atrium_date")
def _atrium_date(iso):
    """Format a stored ISO-8601 date/timestamp as a date only: 'Jun 7, 2026' (no time)."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return iso
    return "%s %d, %d" % (dt.strftime("%b"), dt.day, dt.year)


def _atrium_json_gate(client):
    """Shared gate for Atrium POST actions: returns a JSON error Response, or None when allowed."""
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not can_open(client):
        return Response('{"error":"forbidden"}', status=403, mimetype="application/json")
    return None


def _atrium_admin_json_gate(client):
    """Gate for in-workspace ADMIN edit actions: super-admin only. JSON error Response, or None."""
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not is_superadmin():
        return Response('{"error":"forbidden"}', status=403, mimetype="application/json")
    return None


def _atrium_root_json_gate(client):
    """Gate for Website-Health EDITS: THE super admin only (admins are view-only). JSON error or None.

    Website Health is visible to every admin (is_superadmin) but only THE super admin (is_root_admin)
    may change the monitored URL, run a check, or edit notes -- so an admin "just sees it".
    """
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not is_root_admin():
        return Response('{"error":"forbidden"}', status=403, mimetype="application/json")
    return None


def _bool_field(name):
    """Read a checkbox-style form field as a bool."""
    return request.form.get(name, "0") in ("1", "true", "True", "on")


def _client_sender_name(user):
    """A human-ish name for a client message sender, derived from the login email."""
    if user and "@" in user:
        return user.split("@")[0].split(".")[0].title()
    return "Client"


def _admin_sender_name(user):
    """The attribution shown on an admin's inline comments/replies (defaults to 'AGORA').

    A shared mailbox like info@ is generic, so we fall back to the team name 'AGORA' rather than a
    personal first name; a personal login (e.g. maya@...) gets their first name.
    """
    if user and "@" in user:
        local = user.split("@")[0]
        if local.lower() not in atrium_view._GENERIC_MAILBOXES:
            return local.split(".")[0].split("_")[0].title()
    return "AGORA"


@app.route("/w/<client>/", defaults={"tab": "overview"}, methods=["GET"])
@app.route("/w/<client>/<tab>", methods=["GET"])
def atrium(client, tab):
    """Render the Atrium workspace shell for `client`, with `tab` active (client-facing)."""
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response("No workspace exists for this client yet.", status=404, mimetype="text/plain")
    if tab not in ATRIUM_TABS and tab not in ATRIUM_TEAM_TABS:
        tab = "overview"
    if tab in ATRIUM_TEAM_TABS and not is_superadmin():
        # Team-only tabs (Website Health) are never shown to clients -- bounce to Dashboard.
        tab = "dashboard"
    if tab == "overview":
        # Overview is hidden in the nav for the demo; land on Dashboard instead so the bare
        # workspace URL never opens on the hidden tab. (Revert with the nav list to restore it.)
        tab = "dashboard"
    user = current_user()
    view = atrium_view.build(ws, client, user, tab)
    # Admin-only "Preview as client": a real admin can append ?preview=client to SEE the exact
    # client-facing view (admin edit affordances hidden) WITHOUT changing their session/role. This is
    # not the old in-header session toggle (which could flip roles) -- it's a per-request preview the
    # client themselves never see, so the login-derived role boundary stays intact.
    admin_preview = is_superadmin() and request.args.get("preview") == "client"
    return render_template(
        "atrium.html",
        workspace_name=WORKSPACE_NAME,
        ws=ws,
        view=view,
        user=user,
        user_notify=workspace.get_notify(ws, user or ""),
        is_superadmin=(is_superadmin() and not admin_preview),
        # Website Health is editable by THE super admin only; admins see it read-only.
        can_edit_health=(is_root_admin() and not admin_preview),
        admin_preview=admin_preview,
        admin_name=_admin_sender_name(user),
        favicon=brand.FAVICON_DATA_URI,
    )


@app.route("/w/<client>/approve", methods=["POST"])
@app.route("/w/<client>/request-changes", methods=["POST"])
def atrium_decide(client):
    """Approve or request changes on a content piece (client-facing); notifies the AGORA team."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    decision = "approved" if request.path.endswith("/approve") else "changes"
    content_id = request.form.get("content_id", "").strip()
    note = request.form.get("note", None)
    try:
        item = workspace.decide_content(client, content_id, decision, note=note)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.client_decided(client, item, decision, current_user())
    return jsonify(ok=True, status=item.get("status"), decided_at=item.get("decided_at", ""))


@app.route("/w/<client>/save-note", methods=["POST"])
def atrium_save_note(client):
    """Persist a client's recommendation note on a content piece (silent)."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    try:
        workspace.set_content_note(client, content_id, request.form.get("note", ""))
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True)


@app.route("/w/<client>/send-message", methods=["POST"])
def atrium_send_message(client):
    """Append a client message to a conversation (client-facing); notifies the AGORA team."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    conv_id = request.form.get("conversation_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    try:
        conv, message = workspace.add_message(
            client, conv_id, "client", _client_sender_name(current_user()),
            body, set_status="awaiting_reply",
        )
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.client_messaged(client, conv, current_user())
    return jsonify(ok=True, message=message, status=conv.get("status"))


@app.route("/w/<client>/save-notify", methods=["POST"])
def atrium_save_notify(client):
    """Save the logged-in user's notification preferences for this workspace (silent)."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    user = current_user()
    if not user:
        return Response('{"error":"no_user"}', status=400, mimetype="application/json")
    prefs = {k: _bool_field(k) for k in ("master", "content", "replies", "summary", "status", "news")}
    freq = request.form.get("frequency", "instant")
    if freq in ("instant", "daily"):
        prefs["frequency"] = freq
    try:
        workspace.set_notify(client, user, prefs)
    except KeyError:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    return jsonify(ok=True)


@app.route("/w/<client>/logo", methods=["POST"])
def atrium_client_logo(client):
    """Let a workspace member set the client's own logo from INSIDE the workspace (no console needed).

    Client-facing twin of the team console's /admin/atrium/<c>/logo: any user who can open this
    workspace may replace its logo by clicking the side-panel crest. Stored INLINE in the workspace
    brand (brand.client_logo) as a self-contained <img> data: URI -- exactly how seeded logos are
    embedded, so it needs no new infra or separate object. Returns the new markup as JSON so the side
    panel can swap it in place (no reload)."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    upload = request.files.get("logo")
    if upload is None or not upload.filename:
        return Response('{"ok":false,"error":"no_file"}', status=400, mimetype="application/json")
    mime = (upload.mimetype or "").lower()
    if mime not in _ATRIUM_IMAGE_EXT:
        return Response('{"ok":false,"error":"bad_type"}', status=400, mimetype="application/json")
    data = upload.read(LOGO_MAX_BYTES + 1)
    if len(data) > LOGO_MAX_BYTES:
        return Response('{"ok":false,"error":"too_large"}', status=400, mimetype="application/json")
    # Embed as a data: URI inside an <img> (NOT inlined SVG markup): an <img> never executes script,
    # so an uploaded SVG cannot inject anything when the markup is later rendered with |safe.
    import base64  # lazy: only the logo path needs it
    b64 = base64.b64encode(data).decode("ascii")
    logo_markup = '<img src="data:%s;base64,%s" alt="logo">' % (mime, b64)
    try:
        workspace.set_client_logo(client, logo_markup)
    except KeyError:
        return Response('{"ok":false,"error":"no_workspace"}', status=404, mimetype="application/json")
    return jsonify(ok=True, logo=logo_markup)


@app.route("/w/<client>/comment", methods=["POST"])
def atrium_comment(client):
    """Append a client comment to a content piece (client-facing); notifies the AGORA team.

    A comment is either a plain note (kind="comment") or a "Request changes" comment (kind="changes"),
    which ALSO flips the piece's status to 'changes' — the request-changes decision now lives in the
    comment thread (rendered as a flagged light-red bubble) instead of a separate action button.
    """
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    kind = "changes" if request.form.get("kind", "").strip() == "changes" else "comment"
    try:
        item, comment = workspace.add_content_comment(
            client, content_id, "client", _client_sender_name(current_user()), body,
            kind=kind, set_status=("changes" if kind == "changes" else None),
        )
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    if kind == "changes":
        notify.client_decided(client, item, "changes", current_user())
    else:
        notify.client_commented(client, item, body, current_user())
    return jsonify(ok=True, comment=comment, status=item.get("status"))


@app.route("/w/<client>/resolve-comment", methods=["POST"])
def atrium_resolve_comment(client):
    """Mark a "Request changes" comment resolved (TEAM only); may return the piece to 'awaiting'.

    Resolving is a team action: only the Agora team decides a change request is addressed. Clients
    raise change requests (via /comment kind=changes) but cannot resolve them -- hence the admin gate."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    comment_id = request.form.get("comment_id", "").strip()
    try:
        _item, _comment, status = workspace.resolve_content_comment(client, content_id, comment_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, status=status)


@app.route("/w/<client>/state.json", methods=["GET"])
def atrium_state(client):
    """Live-state snapshot for in-page polling — per-content review status + comment threads.

    Read-only and intentionally tiny: the client/admin views poll this so each side sees the other's
    comments, change-requests, resolves, and approve/changes status WITHOUT a page reload. There is no
    websocket/pub-sub (single workspace JSON in GCS, Cloud Run multi-instance) — every viewer simply
    reads the same authoritative object, so a poll picks up whatever the other party just wrote. Gated
    exactly like the workspace itself (authed + can_open); the bucket/JSON stays private."""
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not can_open(client):
        return Response('{"error":"forbidden"}', status=403, mimetype="application/json")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    content = {}
    for camp in ws.get("campaigns", []) or []:
        for p in camp.get("content", []) or []:
            cid = p.get("id")
            if not cid:
                continue
            comments = []
            for cm in p.get("comments", []) or []:
                comments.append({
                    "id": cm.get("id", ""),
                    "sender": cm.get("sender", ""),
                    "sender_name": cm.get("sender_name", ""),
                    "body": cm.get("body", ""),
                    "kind": cm.get("kind", "comment"),
                    "resolved": bool(cm.get("resolved")),
                    "when": _atrium_dt(cm.get("created_at", "")),
                })
            content[cid] = {"status": p.get("status", ""), "comments": comments}
    return jsonify(ok=True, content=content)


@app.route("/w/<client>/creative/<content_id>", methods=["GET"])
def atrium_creative(client, content_id):
    """Stream a content piece's uploaded creative (authed proxy; the bucket stays private).

    Mirrors the /data.json posture: the object is never public -- it is served only to a session
    that may open this client. Returns 404 when the piece has no uploaded creative.
    """
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response("Not found", status=404, mimetype="text/plain")
    _camp, item = workspace._find_content(ws, content_id)
    if item is None or not item.get("image_object"):
        return Response("Not found", status=404, mimetype="text/plain")
    mime = item.get("image_mime") or "application/octet-stream"
    size = workspace.creative_size(client, content_id)
    if size is None:
        return Response("Not found", status=404, mimetype="text/plain")

    range_header = request.headers.get("Range", "")
    if not range_header:
        # No range: stream the whole object (200) WITHOUT a Content-Length, so werkzeug uses chunked
        # transfer-encoding. Cloud Run caps fixed-length (Content-Length) responses at ~32 MiB but
        # streams chunked ones unbounded -- so a >32 MiB video downloads fine here. (Video playback
        # uses the 206 range path below; its window stays well under the cap.)
        resp = Response(workspace.stream_creative(client, content_id, 0, size - 1), mimetype=mime)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, max-age=60"
        return resp

    # Range request (video seeking): return a bounded 206 window so memory stays small.
    window = 8 * 1024 * 1024
    start, end = 0, size - 1
    spec = range_header.split("=", 1)[1].split(",")[0].strip() if "=" in range_header else ""
    lo, _, hi = spec.partition("-")
    try:
        if lo:
            start = int(lo)
            end = int(hi) if hi else size - 1
        elif hi:  # suffix range: last N bytes
            start = max(0, size - int(hi))
    except ValueError:
        start, end = 0, size - 1
    if start >= size:
        return Response(status=416, headers={"Content-Range": "bytes */%d" % size, "Accept-Ranges": "bytes"})
    end = min(end, size - 1, start + window - 1)
    resp = Response(workspace.stream_creative(client, content_id, start, end), status=206, mimetype=mime)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    resp.headers["Content-Length"] = str(end - start + 1)
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


# --- Atrium in-workspace admin editing (super-admin; same /w/<c> surface, JSON actions) ----------
# These power the inline edit affordances rendered into atrium.html for super-admins, so the team
# edits the REAL client workspace in place instead of the separate dark console below. Every action
# reuses the same workspace.py mutators; all are gated super-admin via _atrium_admin_json_gate.
ATRIUM_UPLOAD_MAX_BYTES = 8 * 1024 * 1024    # images: reject larger than 8 MB
ATRIUM_VIDEO_MAX_BYTES = 30 * 1024 * 1024    # videos: kept under Cloud Run's ~32 MiB request cap
ATRIUM_VIDEO_MAX_BYTES_LOCAL = 1024 * 1024 * 1024  # local dev (no Cloud Run cap): in-app accepts up to 1 GB
ATRIUM_FILE_MAX_BYTES = 30 * 1024 * 1024     # any other attachment (pdf, doc, zip…): under Cloud Run's ~32 MiB cap
# A client LOGO is inlined into the workspace JSON (brand.client_logo), which is rewritten in full on
# every edit -- so keep it tiny. Seeded logos sit around ~70 KB; 512 KB is a generous ceiling.
LOGO_MAX_BYTES = 512 * 1024
_ATRIUM_IMAGE_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/webp": "webp", "image/svg+xml": "svg",
}
_ATRIUM_VIDEO_EXT = {
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
}
# Creatives accept images AND short videos; the stored mime tells the template which to render.
_ATRIUM_MEDIA_EXT = dict(_ATRIUM_IMAGE_EXT)
_ATRIUM_MEDIA_EXT.update(_ATRIUM_VIDEO_EXT)


def _strategy_from_form():
    """Read the two strategy columns (Insight / Action) from the request form into a dict."""
    return {
        "what": request.form.get("what", "").strip(),
        "why": request.form.get("why", "").strip(),
    }


@app.route("/w/<client>/admin/strategy", methods=["POST"])
def atrium_admin_strategy(client):
    """Edit a campaign's name / eyebrow / strategy columns in place."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    campaign_id = request.form.get("campaign_id", "").strip()
    fields = {"strategy": _strategy_from_form()}
    if request.form.get("name") is not None:
        fields["name"] = request.form.get("name", "").strip()
    if request.form.get("eyebrow") is not None:
        fields["eyebrow"] = request.form.get("eyebrow", "").strip()
    try:
        camp = workspace.update_campaign(client, campaign_id, **fields)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, strategy=camp.get("strategy"), name=camp.get("name"),
                   eyebrow=camp.get("eyebrow"))


@app.route("/w/<client>/admin/strategy-doc", methods=["POST"])
def atrium_admin_strategy_doc(client):
    """Attach or clear the Google Doc URL backing a campaign's AI summary."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    try:
        camp = workspace.set_strategy_doc(client, request.form.get("campaign_id", "").strip(),
                                          request.form.get("doc_url", "").strip())
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, strategy_doc=camp.get("strategy_doc", ""))


@app.route("/w/<client>/admin/generate-summary", methods=["POST"])
def atrium_admin_generate_summary(client):
    """Read the campaign's attached Google Doc and (re)write its Insight/Action strategy. Always degrades.

    This regenerates the two strategy sections from the doc for an EXISTING campaign -- the same
    thing the Add-campaign modal does at creation -- so an admin can refresh them after editing the
    doc (or after enabling AI). Degrades: an unreadable doc returns ok:false with share guidance.
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    campaign_id = request.form.get("campaign_id", "").strip()
    ws = workspace.load_workspace(client)
    camp = workspace._find_campaign(ws, campaign_id) if ws else None
    if camp is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    doc_url = (request.form.get("doc_url", "").strip() or camp.get("strategy_doc", ""))
    if request.form.get("doc_url") is not None:
        workspace.set_strategy_doc(client, campaign_id, doc_url)
    strategy, source = atrium_docs.generate_strategy(doc_url)
    if not strategy:
        return jsonify(ok=False, source=source,
                       message="Couldn't read that Google Doc. Open it → Share → General access → "
                               "“Anyone with the link” (Viewer), then try again.")
    workspace.update_campaign(client, campaign_id, strategy=strategy)
    return jsonify(ok=True, strategy=strategy, source=source)


@app.route("/w/<client>/admin/summary", methods=["POST"])
def atrium_admin_summary(client):
    """Hand-edit a campaign's AI summary text."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    try:
        camp = workspace.update_campaign(client, request.form.get("campaign_id", "").strip(),
                                         ai_summary=request.form.get("ai_summary", "").strip())
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, ai_summary=camp.get("ai_summary", ""))


@app.route("/w/<client>/admin/campaign", methods=["POST"])
def atrium_admin_add_campaign(client):
    """Add a campaign in place. With just a name + a Google Doc link, AI writes the strategy.

    If a doc link is supplied (and the strategy fields weren't typed by hand) we best-effort read
    the doc and let AI write the campaign's "Insight / Action / What to do next?"
    sections, so the campaign lands fully formed from the doc alone. The doc link is ALWAYS saved
    regardless of whether AI is wired, so the client still gets the "View the full breakdown" link
    on a default (no-Docs/no-AI) deploy -- the admin then fills the sections via "Edit strategy".
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    name = request.form.get("name", "").strip()
    if not name:
        return Response('{"error":"name_required"}', status=400, mimetype="application/json")
    channel = "paid" if request.form.get("channel") == "paid" else "organic"
    doc_url = request.form.get("strategy_doc", "").strip()
    strategy = _strategy_from_form()       # empty from the quick modal; kept for any hand-typed flow
    source = "manual" if any(strategy.values()) else "none"
    if doc_url and not any(strategy.values()):
        generated, source = atrium_docs.generate_strategy(doc_url)
        if generated:
            strategy = generated
        else:
            # A doc was supplied but couldn't be read -- tell the admin how to fix it rather than
            # silently creating an empty campaign. They can share the doc and retry, or drop the link.
            return jsonify(ok=False, source=source,
                           message="Couldn't read that Google Doc. Open it → Share → General access "
                                   "→ “Anyone with the link” (Viewer), then try again. Or "
                                   "remove the link to add the campaign and write the strategy by hand.")
    camp = workspace.add_campaign(client, channel, name, request.form.get("eyebrow", "").strip(),
                                  strategy=strategy, strategy_doc=doc_url)
    return jsonify(ok=True, id=camp.get("id"), source=source)


@app.route("/w/<client>/admin/delete-campaign", methods=["POST"])
def atrium_admin_delete_campaign(client):
    """Delete a campaign (and clean up its content's uploaded creatives)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    campaign_id = request.form.get("campaign_id", "").strip()
    try:
        removed = workspace.delete_campaign(client, campaign_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    for item in removed.get("content", []):
        if item.get("image_object"):
            workspace.delete_creative(client, item.get("id"))
    return jsonify(ok=True)


@app.route("/w/<client>/admin/content", methods=["POST"])
def atrium_admin_add_content(client):
    """Add a content piece to a campaign in place (status -> awaiting) and notify the client."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    campaign_id = request.form.get("campaign_id", "").strip()
    content = {
        "ref": request.form.get("ref", "").strip(),
        "type_tag": request.form.get("type_tag", "").strip(),
        "sub_tag": request.form.get("sub_tag", "").strip(),
        "platform": request.form.get("platform", "").strip(),
        "caption": request.form.get("caption", "").strip(),
        "social_caption": request.form.get("social_caption", "").strip(),
        "video_url": request.form.get("video_url", "").strip(),
        # An optional publish date mirrors the piece onto the Content Calendar as a linked event.
        "date": request.form.get("date", "").strip(),
    }
    if content["ref"]:
        content["id"] = content["ref"]
    try:
        item = workspace.add_content(client, campaign_id, content)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.team_added_content(client, workspace.load_workspace(client), item)
    return jsonify(ok=True, id=item.get("id"))


@app.route("/w/<client>/admin/edit-content", methods=["POST"])
def atrium_admin_edit_content(client):
    """Patch a content piece's editable fields (ref/type/sub/platform/caption) in place."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    fields = {}
    for key in ("ref", "type_tag", "sub_tag", "platform", "caption", "social_caption", "video_url", "date"):
        if request.form.get(key) is not None:
            fields[key] = request.form.get(key, "").strip()
    try:
        item = workspace.update_content(client, content_id, fields)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, content=item)


@app.route("/w/<client>/admin/video-link", methods=["POST"])
def atrium_admin_video_link(client):
    """Attach (or clear) a video by URL on a content piece. An empty url removes the link.

    This is the 'provide a link' half of the Add-video control; the '.mp4 file' half reuses the
    existing creative upload (/admin/add-images), which already stores and renders video creatives.
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    url = request.form.get("url", "").strip()
    # Only accept http(s) links (or an empty string to clear); never store javascript:/data: URIs.
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return jsonify(ok=False, message="Enter a valid http(s) video link."), 400
    try:
        item = workspace.update_content(client, content_id, {"video_url": url})
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, content=item)


@app.route("/w/<client>/admin/delete-content", methods=["POST"])
def atrium_admin_delete_content(client):
    """Delete a content piece in place (and its uploaded creative object, if any)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    try:
        removed = workspace.delete_content(client, content_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    if removed.get("image_object"):
        workspace.delete_creative(client, content_id)
    return jsonify(ok=True)


@app.route("/w/<client>/admin/content-comment", methods=["POST"])
def atrium_admin_content_comment(client):
    """Post a team comment on a content piece (notifies opted-in client recipients)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    sender_name = request.form.get("sender_name", "").strip() or "AGORA"
    kind = "changes" if request.form.get("kind", "").strip() == "changes" else "comment"
    try:
        item, comment = workspace.add_content_comment(
            client, content_id, "agora", sender_name, body, kind=kind,
        )
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.team_commented(client, workspace.load_workspace(client), item, body, sender_name)
    return jsonify(ok=True, comment=comment)


@app.route("/w/<client>/admin/delete-comment", methods=["POST"])
def atrium_admin_delete_comment(client):
    """Delete a single comment from a content piece's thread (team-only); may return it to 'awaiting'."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    comment_id = request.form.get("comment_id", "").strip()
    try:
        _item, status = workspace.delete_content_comment(client, content_id, comment_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, status=status)


@app.route("/w/<client>/admin/upload-creative", methods=["POST"])
def atrium_admin_upload_creative(client):
    """Upload an image or short video creative for a content piece (private object; image ≤8MB, video ≤30MB)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return Response('{"error":"no_file"}', status=400, mimetype="application/json")
    mime = (upload.mimetype or "").lower()
    if mime not in _ATRIUM_MEDIA_EXT:
        return Response('{"error":"unsupported_type"}', status=400, mimetype="application/json")
    video_cap = ATRIUM_VIDEO_MAX_BYTES_LOCAL if _LOCAL_BACKEND else ATRIUM_VIDEO_MAX_BYTES
    max_bytes = video_cap if mime in _ATRIUM_VIDEO_EXT else ATRIUM_UPLOAD_MAX_BYTES
    data = upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        return Response('{"error":"too_large"}', status=413, mimetype="application/json")
    ws = workspace.load_workspace(client)
    _camp, item = workspace._find_content(ws, content_id) if ws else (None, None)
    if item is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    object_name = workspace.write_creative(client, content_id, data, content_type=mime)
    workspace.set_content_image(client, content_id, object_name, mime)
    return jsonify(ok=True, url=url_for("atrium_creative", client=client, content_id=content_id))


@app.route("/w/<client>/admin/creative-upload-url", methods=["POST"])
def atrium_admin_creative_upload_url(client):
    """Mint a V4 signed PUT URL so the browser uploads a LARGE creative DIRECTLY to GCS, bypassing
    Cloud Run's ~32 MiB request cap. The client then calls /creative-confirm to record it."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    mime = (request.form.get("content_type", "") or "").lower()
    if mime not in _ATRIUM_MEDIA_EXT:
        return Response('{"error":"unsupported_type"}', status=400, mimetype="application/json")
    ws = workspace.load_workspace(client)
    _camp, item = workspace._find_content(ws, content_id) if ws else (None, None)
    if item is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    try:
        url, _obj = workspace.signed_upload_url(client, content_id, mime)
    except Exception as e:
        # Signing unavailable/misconfigured -> tell the client to fall back to the in-app upload.
        # app.logger isn't always wired to Cloud Run's log stream, so print the traceback to stderr
        # (which IS captured) and surface the error detail (this route is super-admin only).
        import sys
        import traceback as _tb
        sys.stderr.write("creative-upload-url signing failed for %s/%s\n" % (client, content_id))
        _tb.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return jsonify(ok=False, error="sign_unavailable", detail=str(e))
    if not url:
        return jsonify(ok=False, error="sign_unavailable", detail="signer_returned_no_url")
    return jsonify(ok=True, url=url, mime=mime)


@app.route("/w/<client>/admin/creative-confirm", methods=["POST"])
def atrium_admin_creative_confirm(client):
    """Record a creative that was uploaded straight to GCS via a signed URL. Verifies it landed."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    mime = (request.form.get("content_type", "") or "").lower()
    if mime not in _ATRIUM_MEDIA_EXT:
        return Response('{"error":"unsupported_type"}', status=400, mimetype="application/json")
    ws = workspace.load_workspace(client)
    _camp, item = workspace._find_content(ws, content_id) if ws else (None, None)
    if item is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    if workspace.creative_size(client, content_id) is None:
        return Response('{"error":"not_uploaded"}', status=400, mimetype="application/json")
    object_name = workspace.creative_object_name(client, content_id)
    workspace.set_content_image(client, content_id, object_name, mime)
    return jsonify(ok=True, url=url_for("atrium_creative", client=client, content_id=content_id))


@app.route("/w/<client>/admin/remove-creative", methods=["POST"])
def atrium_admin_remove_creative(client):
    """Remove a content piece's uploaded creative (object + workspace pointer)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    try:
        workspace.clear_content_image(client, content_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    workspace.delete_creative(client, content_id)
    return jsonify(ok=True)


# --- Multiple images per content piece: authed serve + admin add/remove -----------------------
@app.route("/w/<client>/creative/<content_id>/<image_id>", methods=["GET"])
def atrium_creative_image(client, content_id, image_id):
    """Serve ONE image of a content piece (authed proxy; the bucket stays private).

    Same posture as the single-creative route: never public, served only to a session that may open
    this client. Images are small (<=8MB) so we return them whole.
    """
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response("Not found", status=404, mimetype="text/plain")
    _camp, item = workspace._find_content(ws, content_id)
    if item is None:
        return Response("Not found", status=404, mimetype="text/plain")
    img = next((im for im in item.get("images", []) if im.get("id") == image_id), None)
    if img is None:
        return Response("Not found", status=404, mimetype="text/plain")
    data = workspace.read_content_image_bytes(client, content_id, image_id)
    if data is None:
        return Response("Not found", status=404, mimetype="text/plain")
    mime = img.get("mime") or "application/octet-stream"
    resp = Response(data, mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=300"
    # Default = inline so a PDF previews in an <iframe>; ?dl=1 forces a download with the original
    # name (the doc lightbox's download button uses ?dl=1). Always carry the filename so a download
    # keeps the real name regardless of disposition.
    safe = (img.get("name") or ("file-" + image_id)).replace('"', "").replace("\r", "").replace("\n", "")
    disposition = "attachment" if request.args.get("dl") == "1" else "inline"
    resp.headers["Content-Disposition"] = '%s; filename="%s"' % (disposition, safe)
    return resp


@app.route("/w/<client>/docview/<content_id>/<image_id>", methods=["GET"])
def atrium_docview(client, content_id, image_id):
    """Render an attached Office document (docx/xlsx/pptx/csv/txt) to a scrollable HTML preview,
    served in an <iframe> by the content card + doc lightbox. Same private/authed posture as the
    creative serve route; PDFs are previewed natively (inline serve) and never reach this route."""
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response("Not found", status=404, mimetype="text/plain")
    _camp, item = workspace._find_content(ws, content_id)
    if item is None:
        return Response("Not found", status=404, mimetype="text/plain")
    img = next((im for im in item.get("images", []) if im.get("id") == image_id), None)
    if img is None:
        return Response("Not found", status=404, mimetype="text/plain")
    data = workspace.read_content_image_bytes(client, content_id, image_id)
    if data is None:
        return Response("Not found", status=404, mimetype="text/plain")
    import atrium_docview
    page = atrium_docview.render_doc_html(data, img.get("mime") or "", img.get("name") or "")
    resp = Response(page, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@app.route("/w/<client>/admin/add-images", methods=["POST"])
def atrium_admin_add_images(client):
    """Append one or more images to a content piece (the approval ticket's picture row). Field: 'files'."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    ws = workspace.load_workspace(client)
    _camp, item = workspace._find_content(ws, content_id) if ws else (None, None)
    if item is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    uploads = request.files.getlist("files")
    if not uploads and "file" in request.files:
        uploads = [request.files["file"]]
    added = []
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        mime = (upload.mimetype or "").lower()
        # Any file type is accepted: images/video render inline, everything else as a download chip.
        # Size cap depends on kind (images are inlined small; videos/other files share the request cap).
        if mime in _ATRIUM_IMAGE_EXT:
            max_bytes = ATRIUM_UPLOAD_MAX_BYTES
        elif mime in _ATRIUM_VIDEO_EXT:
            max_bytes = ATRIUM_VIDEO_MAX_BYTES
        else:
            max_bytes = ATRIUM_FILE_MAX_BYTES
        data = upload.read(max_bytes + 1)
        if len(data) > max_bytes:
            continue
        name = os.path.basename(upload.filename or "")
        image_id = workspace._new_id("img")
        workspace.add_content_image(client, content_id, image_id, data, mime, name=name)
        added.append({
            "id": image_id, "mime": mime, "name": name,
            "url": url_for("atrium_creative_image", client=client, content_id=content_id, image_id=image_id),
        })
    if not added:
        return Response('{"error":"no_valid_files"}', status=400, mimetype="application/json")
    return jsonify(ok=True, added=added)


@app.route("/w/<client>/admin/remove-image", methods=["POST"])
def atrium_admin_remove_image(client):
    """Remove one image from a content piece (object + pointer)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    image_id = request.form.get("image_id", "").strip()
    workspace.remove_content_image(client, content_id, image_id)
    return jsonify(ok=True)


@app.route("/w/<client>/admin/metrics", methods=["POST"])
def atrium_admin_metrics(client):
    """Edit headline counts (today + split) and the KPI metric values/trends in place."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")

    def _int(name, fallback):
        try:
            return int(request.form.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    today = {
        "leads": _int("today_leads", ws.get("today", {}).get("leads", 0)),
        "visitors": _int("today_visitors", ws.get("today", {}).get("visitors", 0)),
        "bookings": _int("today_bookings", ws.get("today", {}).get("bookings", 0)),
    }
    split = {
        "paid": _int("split_paid", ws.get("split", {}).get("paid", 0)),
        "organic": _int("split_organic", ws.get("split", {}).get("organic", 0)),
    }
    workspace.set_overview_counts(client, today=today, split=split)

    metrics = []
    for i, m in enumerate(ws.get("metrics", [])):
        metrics.append({
            "icon": m.get("icon", "trending"),
            "label": m.get("label", ""),
            "value": request.form.get("metric_value_%d" % i, m.get("value", "")).strip(),
            "trend": request.form.get("metric_trend_%d" % i, m.get("trend", "")).strip(),
            "trend_up": _bool_field("metric_up_%d" % i),
        })
    if metrics:
        workspace.set_metrics(client, metrics)
    return jsonify(ok=True)


@app.route("/w/<client>/admin/goal", methods=["POST"])
def atrium_admin_goal(client):
    """Set the per-client Monthly goal: label / format / three tiers / current / optional source metric."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")

    def _num(name):
        try:
            return float(request.form.get(name, "") or 0)
        except (TypeError, ValueError):
            return 0.0

    fmt = request.form.get("goal_format", "number").strip()
    workspace.set_goal(client, {
        "label": request.form.get("goal_label", "").strip() or "goal",
        "format": "currency" if fmt == "currency" else "number",
        "target": _num("goal_target"),
        "exceed": _num("goal_exceed"),
        "breakthrough": _num("goal_breakthrough"),
        "current": _num("goal_current"),
        "source_metric": request.form.get("goal_source_metric", "").strip(),
    })
    return jsonify(ok=True)


@app.route("/w/<client>/admin/reach", methods=["POST"])
def atrium_admin_reach(client):
    """Set the per-client Total reach headline (this vs last month) shown on the Overview card."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    workspace.set_reach(client,
                        request.form.get("reach_current", "").strip(),
                        request.form.get("reach_previous", "").strip())
    return jsonify(ok=True)


@app.route("/w/<client>/admin/communication", methods=["POST"])
def atrium_admin_communication(client):
    """Add, edit, or delete an email/meeting summary in the Client Communications tab. `op` is
    'add' | 'edit' | 'delete'; `kind` is 'email' | 'meeting'."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    op = request.form.get("op", "").strip()
    kind = "email" if request.form.get("kind", "").strip() == "email" else "meeting"
    if op == "delete":
        workspace.delete_communication(client, kind, request.form.get("item_id", "").strip())
        return jsonify(ok=True)
    if op == "add":
        date = (request.form.get("date", "") or "").strip() or None
        if kind == "email":
            item = workspace.add_email_summary(client, request.form.get("subject", "").strip(),
                                               request.form.get("summary", "").strip(),
                                               date=date)
        else:
            item = workspace.add_meeting_summary(client, request.form.get("title", "").strip(),
                                                 request.form.get("summary", "").strip(),
                                                 request.form.get("attendees", "").strip(),
                                                 date=date)
        return jsonify(ok=True, id=item.get("id"))
    if op == "edit":
        fields = {}
        for key in ("subject", "title", "attendees", "summary"):
            if request.form.get(key) is not None:
                fields[key] = request.form.get(key, "").strip()
        workspace.update_communication(client, kind, request.form.get("item_id", "").strip(), fields)
        return jsonify(ok=True)
    return Response('{"error":"bad_op"}', status=400, mimetype="application/json")


@app.route("/w/<client>/admin/dashboard-url", methods=["POST"])
def atrium_admin_dashboard_url(client):
    """Set the per-client Looker Studio embed URL (https only; empty hides the dashboard), the report
    native height in px (default 800, clamped 200..5000) and native width (default 1200, clamped
    320..5000). The embed scales so the native width fills the container -- no dead strip on the right."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    url = (request.form.get("url", "") or "").strip()
    if url and not url.lower().startswith("https://"):
        return Response('{"error":"must_be_https"}', status=400, mimetype="application/json")
    try:
        height = int(request.form.get("height", "") or 800)
    except (TypeError, ValueError):
        height = 800
    try:
        width = int(request.form.get("width", "") or 1200)
    except (TypeError, ValueError):
        width = 1200
    workspace.set_dashboard_url(client, url, max(200, min(height, 5000)), max(320, min(width, 5000)))
    return jsonify(ok=True)


# --- Website Health (team-only tab: site monitoring + tag detection; THE super admin edits) --------
@app.route("/w/<client>/admin/website-health/save", methods=["POST"])
def atrium_admin_website_health_save(client):
    """Save the monitored website URL and/or team notes (Website Health tab; super-admin only).

    Each field is written only when present in the form, so saving notes never clobbers the URL and
    vice-versa.
    """
    gate = _atrium_root_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    if "url" in request.form:
        workspace.set_website_url(client, request.form.get("url", "").strip())
    if "notes" in request.form:
        workspace.set_website_notes(client, request.form.get("notes", ""))
    return jsonify(ok=True)


@app.route("/w/<client>/admin/website-health/check", methods=["POST"])
def atrium_admin_website_health_check(client):
    """Run a website health check (reachability + tag detection) and store the result (super-admin only).

    The URL comes from the form (so an unsaved-but-typed URL still checks AND is remembered) or falls
    back to the stored one. Degrades gracefully: a dead site / network error still returns ok:true with
    the failure recorded INSIDE the result (so the tab shows the problem); only a MISSING url returns
    ok:false with guidance. Detection scans the live page's HTML -- no GTM API, no new infra.
    """
    gate = _atrium_root_json_gate(client)
    if gate:
        return gate
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    url = request.form.get("url", "").strip() or (ws.get("website_health") or {}).get("url", "")
    if not url:
        return jsonify(ok=False, message="Enter a website URL first.")
    import atrium_health  # lazy: only this route needs it
    result = atrium_health.check_website(url)
    workspace.save_website_check(client, result)
    return jsonify(ok=True, result=result)


@app.route("/w/<client>/admin/calendar", methods=["POST"])
def atrium_admin_calendar(client):
    """Add, delete, or mark-done a calendar event in place. `op` is 'add', 'delete', or 'status'."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    op = request.form.get("op")
    if op == "delete":
        try:
            index = int(request.form.get("index", "-1"))
        except (TypeError, ValueError):
            index = -1
        workspace.delete_calendar_event(client, index)
        return jsonify(ok=True)
    if op == "status":
        # Mark an event done (or clear it). Empty status clears; anything else normalizes to "done".
        try:
            index = int(request.form.get("index", "-1"))
        except (TypeError, ValueError):
            index = -1
        raw = request.form.get("status", "").strip()
        status = "done" if raw else ""
        event = workspace.set_calendar_status(client, index, status)
        if event is None:
            return Response('{"error":"not_found"}', status=404, mimetype="application/json")
        return jsonify(ok=True, event=event)
    if op == "edit":
        # Edit an existing event's date / label / kind (paid|organic|due|milestone) in place.
        try:
            index = int(request.form.get("index", "-1"))
        except (TypeError, ValueError):
            index = -1
        date = request.form.get("date", "").strip()
        if not date:
            return Response('{"error":"date_required"}', status=400, mimetype="application/json")
        kind = request.form.get("kind", "milestone").strip() or "milestone"
        event = workspace.edit_calendar_event(client, index, date,
                                              request.form.get("label", "").strip(), kind)
        if event is None:
            return Response('{"error":"not_found"}', status=404, mimetype="application/json")
        return jsonify(ok=True, event=event)
    date = request.form.get("date", "").strip()
    if not date:
        return Response('{"error":"date_required"}', status=400, mimetype="application/json")
    kind = request.form.get("kind", "milestone").strip() or "milestone"
    event = workspace.add_calendar_event(client, date, request.form.get("label", "").strip(), kind)
    return jsonify(ok=True, event=event)


@app.route("/w/<client>/admin/reply", methods=["POST"])
def atrium_admin_reply_inline(client):
    """Reply to a conversation as the AGORA team from inside the workspace; notifies the client."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    conv_id = request.form.get("conversation_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    sender_name = request.form.get("sender_name", "").strip() or "AGORA"
    new_status = "resolved" if _bool_field("resolve") else "awaiting_reply"
    try:
        conv, message = workspace.add_message(client, conv_id, "agora", sender_name, body,
                                              set_status=new_status)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.team_replied(client, workspace.load_workspace(client), conv, sender_name)
    return jsonify(ok=True, message=message, status=conv.get("status"))


# --- Atrium team management (super-admin operator console) --------------------------------------
# The console is the LANDING PAGE only: a card grid to open / add / delete clients. There is no
# per-client management page anymore -- the team edits each workspace IN PLACE via /w/<c>/admin/*
# (see the in-workspace admin editing section above), and a card opens that workspace directly.
def _atrium_redirect_list(msg, section=None, err=False):
    """Redirect back to the console with a flash, optionally reopening a section pane.

    `err=True` marks the flash as an ERROR so the console styles it red (a rejected action must not
    look like a green success -- that was the "it silently didn't add" confusion)."""
    return redirect(url_for("admin_atrium", msg=msg, section=section, err=(1 if err else None)))


def _account_view(account, name_by_key):
    """Shape one account for the console table: resolve its client keys to a readable label."""
    keys = account.get("clients") or []
    if "*" in keys:
        label = "All clients"
    elif keys:
        label = ", ".join(name_by_key.get(k, k) for k in keys)
    else:
        label = "—"
    return {
        "email": account.get("email"),
        "name": account.get("name") or account.get("email"),
        "role": account.get("role") or "client",
        "status": account.get("status") or "active",
        "client_label": label,
        # The client's own workspace key, so the console can link straight to that client's site.
        "client_key": next((k for k in keys if k != "*"), ""),
        "created_at": account.get("created_at", ""),
    }


@app.route("/admin/atrium", methods=["GET"])
def admin_atrium():
    """The operator console: clients, access requests, accounts, account creation, and the profile."""
    if not authed():
        return redirect(url_for("login", next="/admin/atrium"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    clients = []
    name_by_key = {}
    for c in store.list_clients():
        key, name = c.get("key"), c.get("name")
        name_by_key[key] = name or key
        if key == "template":
            continue  # the worked-example pattern, not a real client -- never list it in the console
        ws = workspace.load_workspace(key)
        # Logo shown on the card: the client's own logo from its workspace, else an initials monogram
        # (so a brand-new / unseeded client still renders something on-brand rather than an empty box).
        logo = (ws.get("brand", {}).get("client_logo") if ws else None) or brand.monogram(name or key)
        clients.append({"key": key, "name": name,
                        "has_workspace": ws is not None, "logo": logo})

    all_accounts = store.list_accounts()
    pending = [a for a in all_accounts if a.get("status") == "pending"]
    active = [_account_view(a, name_by_key) for a in all_accounts if a.get("status") == "active"]
    client_accounts = [a for a in active if a["role"] == "client"]
    admin_accounts = [a for a in active if a["role"] in ("admin", "superadmin")]

    me = current_account()
    profile = {
        "email": current_user(),
        "name": (me or {}).get("name") or current_user(),
        "role": (me or {}).get("role") or ("superadmin" if is_root_admin() else "admin"),
        "has_account": me is not None,
    }
    return render_template(
        "admin_atrium.html", clients=clients, pending=pending,
        client_accounts=client_accounts, admin_accounts=admin_accounts,
        profile=profile, is_root_admin=is_root_admin(), super_admin_email=SUPER_ADMIN_EMAIL,
        initial_section=(request.args.get("section") or "clients"),
        user=current_user(), workspace_name=WORKSPACE_NAME,
        msg=request.args.get("msg"), flash_err=(request.args.get("err") == "1"), **_brand_ctx())


def _valid_client_key(k):
    """Strict slug for a client key: ascii lowercase a-z0-9 + hyphens, not starting with a hyphen."""
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return bool(k) and k[0] != "-" and all(ch in allowed for ch in k)


def _slugify_key(name):
    """Derive a client key from a display name: ascii lowercase, runs of other chars -> one hyphen."""
    out = []
    for ch in (name or "").lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


@app.route("/admin/atrium/new", methods=["POST"])
def admin_atrium_new():
    """Onboard a brand-new client from JUST a display name, then land on their blank workspace.

    The key auto-derives from the name and a strong portal password is auto-generated, so the only
    thing the team types is the name. Reuses onboard_client (the same starter_workspace the CLI uses)
    so the new client opens to a tidy, empty workspace -- a blank version of /w/<key>/ with no
    campaigns -- which the team then builds out in place. On success we redirect STRAIGHT to that
    workspace (super-admin can_open is "*"), matching "click create -> the empty workspace loads".
    """
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    name = request.form.get("name", "").strip()
    # Key + password are no longer asked for: derive the key from the name, auto-generate the password.
    key = (request.form.get("key", "").strip().lower() or _slugify_key(name))
    if not _valid_client_key(key):
        return redirect(url_for("admin_atrium",
                                msg="Please enter a display name we can build a client key from "
                                    "(letters and numbers)."))
    if workspace.workspace_exists(key) or store.get_client(key) is not None:
        return redirect(url_for("atrium", client=key))  # already exists -> just open it
    import onboard_client  # lazy: reuses brand_for() + starter_workspace()
    onboard_client.onboard(key, name or None)
    return redirect(url_for("atrium", client=key))


def _unique_client_key(base):
    """Derive a client key from `base` (a company name) that no existing client/workspace uses.

    Slugifies, then appends -2, -3, ... until free, so approving two sign-ups with the same company
    name never collides on a key (or silently re-points at an existing client)."""
    root = _slugify_key(base) or "client"
    key = root
    n = 2
    while store.get_client(key) is not None or workspace.workspace_exists(key):
        key = "%s-%d" % (root, n)
        n += 1
    return key


@app.route("/admin/accounts/approve", methods=["POST"])
def admin_account_approve():
    """Approve a pending sign-up: create the client + a blank workspace, then activate the account.

    After this the client can log in with the email + password they chose at sign-up (verify_portal_login
    matches the now-`active` account and returns its single client key)."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    account = store.get_account(email)
    if account is None or account.get("status") != "pending":
        return _atrium_redirect_list("No pending request found for that email.", section="requests", err=True)
    company = account.get("requested_name") or account.get("name") or email.split("@")[0]
    key = _unique_client_key(company)
    import onboard_client  # lazy: reuses brand_for() + starter_workspace()
    onboard_client.onboard(key, company)            # creates the client + a blank Atrium workspace
    store.set_account_clients(email, [key])
    store.set_account_status(email, "active")
    return _atrium_redirect_list("Approved %s -- created client '%s' and activated their login." %
                                 (email, key), section="requests")


@app.route("/admin/accounts/reject", methods=["POST"])
def admin_account_reject():
    """Reject (delete) a pending sign-up request. No client is created."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    removed = store.remove_account(email)
    if not removed:
        return _atrium_redirect_list("No request to reject for that email.", section="requests", err=True)
    return _atrium_redirect_list("Rejected and removed the access request for %s." % email,
                                 section="requests")


@app.route("/admin/accounts/create-client", methods=["POST"])
def admin_account_create_client():
    """Admin creates an ACTIVE client account directly (no request/approval step).

    Creates the client + a blank workspace and an active client account, so the client can log in
    immediately with the email + password set here. If no password is given, a strong one is
    generated and surfaced so the operator can read it back to the client."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    company = request.form.get("company", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "") or _gen_password()
    domain = email.split("@")[-1] if "@" in email else ""
    if not company:
        return _atrium_redirect_list("Please enter a company name.", section="create", err=True)
    if "@" not in email or "." not in domain:
        return _atrium_redirect_list("Please enter a valid email like name@company.com.",
                                     section="create", err=True)
    if store.get_account(email) is not None:
        return _atrium_redirect_list("An account already exists for %s." % email, section="create", err=True)
    key = _unique_client_key(company)
    import onboard_client  # lazy
    onboard_client.onboard(key, company)            # client + blank workspace
    store.add_account(email, password, name=company, role="client", clients=[key],
                      status="active", requested_name=company)
    return _atrium_redirect_list(
        "Created client '%s'. The client signs in at /login with  %s / %s  to see their own site. "
        "Use 'Open site' below to view their workspace." % (key, email, password), section="accounts")


@app.route("/admin/accounts/create-admin", methods=["POST"])
def admin_account_create_admin():
    """THE super admin creates another ADMIN account (full client access, role 'admin')."""
    if not is_root_admin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "") or _gen_password()
    domain = email.split("@")[-1] if "@" in email else ""
    if "@" not in email or "." not in domain:
        return _atrium_redirect_list("Please enter a valid email like name@company.com.",
                                     section="create", err=True)
    if store.get_account(email) is not None:
        return _atrium_redirect_list("An account already exists for %s." % email, section="create", err=True)
    store.add_account(email, password, name=name or email.split("@")[0], role="admin",
                      clients=["*"], status="active")
    return _atrium_redirect_list(
        "Created admin '%s'. Login -> %s / %s" % (name or email, email, password), section="accounts")


@app.route("/admin/accounts/set-password", methods=["POST"])
def admin_account_set_password():
    """Change an account's password to one the operator types (helpdesk: 'change password')."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if len(password) < 6:
        return _atrium_redirect_list("New password must be at least 6 characters.", section="accounts", err=True)
    if not _can_manage_account(email) or not _can_manage_admin_target(email):
        return _atrium_redirect_list("You can't change that account's password.", section="accounts", err=True)
    try:
        store.set_account_password(email, password)
    except KeyError:
        return _atrium_redirect_list("No account found for %s." % email, section="accounts", err=True)
    return _atrium_redirect_list("Password changed for %s." % email, section="accounts")


@app.route("/admin/accounts/reset-password", methods=["POST"])
def admin_account_reset_password():
    """Reset an account's password to a freshly generated one and reveal it (helpdesk: 'reset')."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    if not _can_manage_account(email) or not _can_manage_admin_target(email):
        return _atrium_redirect_list("You can't reset that account's password.", section="accounts", err=True)
    new_pw = _gen_password()
    try:
        store.set_account_password(email, new_pw)
    except KeyError:
        return _atrium_redirect_list("No account found for %s." % email, section="accounts", err=True)
    return _atrium_redirect_list("Reset password for %s -> %s" % (email, new_pw), section="accounts")


@app.route("/admin/accounts/delete", methods=["POST"])
def admin_account_delete():
    """Delete an account. The super admin can't be deleted, and you can't delete yourself."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    if not _can_manage_account(email, allow_self=False):
        return _atrium_redirect_list("That account can't be deleted here.", section="accounts", err=True)
    # Deleting an admin account is a super-admin-only power.
    target = store.get_account(email)
    if target and target.get("role") in ("admin", "superadmin") and not is_root_admin():
        return _atrium_redirect_list("Only the super admin can remove admin accounts.", section="accounts", err=True)
    removed = store.remove_account(email)
    if not removed:
        return _atrium_redirect_list("No account found for %s." % email, section="accounts", err=True)
    return _atrium_redirect_list("Deleted the account for %s." % email, section="accounts")


@app.route("/admin/profile/password", methods=["POST"])
def admin_profile_password():
    """The logged-in operator changes their OWN password (Profile pane)."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    password = request.form.get("password", "")
    if len(password) < 6:
        return _atrium_redirect_list("New password must be at least 6 characters.", section="profile", err=True)
    me = current_user()
    if store.get_account(me) is None:
        return _atrium_redirect_list(
            "Your session isn't backed by a stored account (env login), so there's no password to "
            "change here.", section="profile", err=True)
    store.set_account_password(me, password)
    return _atrium_redirect_list("Your password was updated.", section="profile")


def _can_manage_admin_target(email):
    """True unless the target is an ADMIN account and the caller isn't the super admin.

    Clients are manageable by any admin; admin accounts are managed only by THE super admin."""
    target = store.get_account(email)
    if target and target.get("role") in ("admin", "superadmin"):
        return is_root_admin()
    return True


def _can_manage_account(email, allow_self=True):
    """Guard for account mutations: the target must exist, must not be THE super admin, and (unless
    allow_self) must not be the logged-in user. Protects info@ from being locked out or removed."""
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return False
    if email_norm == SUPER_ADMIN_EMAIL:
        return False  # the super admin is managed only from their own Profile pane
    target = store.get_account(email_norm)
    if target is None:
        return False
    if target.get("role") == "superadmin":
        return False
    if not allow_self and email_norm == (current_user() or "").strip().lower():
        return False
    return True


@app.route("/admin/atrium/<client>/logo", methods=["POST"])
def admin_atrium_logo(client):
    """Set a client's logo from an uploaded image (PNG/JPG/GIF/WEBP/SVG), shown on the workspace card
    and in their workspace header. Stored INLINE in the workspace brand (brand.client_logo) as a
    self-contained <img> data: URI -- exactly how seeded logos are embedded, so it needs no new infra
    and no separate object. We cap the upload small (logos are tiny) since the workspace JSON is
    rewritten in full on every edit."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    if store.get_client(client) is None:
        return _atrium_redirect_list("Unknown client '%s'." % client)
    upload = request.files.get("logo")
    if upload is None or not upload.filename:
        return _atrium_redirect_list("No logo file chosen.")
    mime = (upload.mimetype or "").lower()
    if mime not in _ATRIUM_IMAGE_EXT:
        return _atrium_redirect_list("Logo must be a PNG, JPG, GIF, WEBP or SVG image.")
    data = upload.read(LOGO_MAX_BYTES + 1)
    if len(data) > LOGO_MAX_BYTES:
        return _atrium_redirect_list("Logo is too large -- please use an image under %d KB."
                                     % (LOGO_MAX_BYTES // 1024))
    # Embed as a data: URI inside an <img> (NOT inlined SVG markup): an <img> never executes script,
    # so an uploaded SVG cannot inject anything when the markup is later rendered with |safe.
    import base64  # lazy: only the logo path needs it
    b64 = base64.b64encode(data).decode("ascii")
    logo_markup = '<img src="data:%s;base64,%s" alt="logo">' % (mime, b64)
    try:
        workspace.set_client_logo(client, logo_markup)
    except KeyError:
        return _atrium_redirect_list("'%s' has no workspace yet -- create one before adding a logo."
                                     % client)
    return _atrium_redirect_list("Logo updated for '%s'." % client)


@app.route("/admin/atrium/<client>/delete", methods=["POST"])
def admin_atrium_delete(client):
    """Delete a client: remove its registry entry (login + listing) AND its Atrium workspace object.

    Destructive and irreversible; gated super-admin and confirmed in the UI before the POST fires."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    removed = store.remove_client(client)
    workspace.delete_workspace(client)
    if not removed:
        return _atrium_redirect_list("No client '%s' to delete." % client)
    return _atrium_redirect_list("Deleted client '%s' (login and workspace removed)." % client)


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok", mimetype="text/plain")


# CRM: future routes attach here as the portal grows beyond dashboards --
#   /clients/<key>            -- a client record page (contact, plan, onboarding state)
#   /clients/<key>/notes      -- CRM notes timeline
#   /clients/<key>/tasks      -- CRM tasks / follow-ups
# All would read/write the same private platform.json via store.py (no database).


if __name__ == "__main__":
    # Local dev only; in Cloud Run gunicorn (see Dockerfile) serves main:app.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
