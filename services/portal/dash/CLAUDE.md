# CLAUDE.md — services/portal/dash (the portal/CRM Flask app + Agora Atrium)

**Rules live in the repo-root [`/CLAUDE.md`](../../../CLAUDE.md)** — read it first; this file only
adds local context for this subtree. If they disagree, root wins.

You are in the **`platform-dash`** Cloud Run service: the portal/CRM front-door **and** Agora Atrium
(the co-branded client workspace). One self-contained Flask app, no build step.

- **`main.py`** — all routes (portal, Atrium client `/w/<c>/*`, admin `/w/<c>/admin/*` + the dark
  `/admin/atrium/*` console). `WORKSPACE_NAME` is the Atrium product-name constant.
- **`store.py`** — the registry (one private `platform.json`): clients **and** `accounts` (real
  email+password logins; role admin/client, status active/pending). `verify_portal_login` resolves
  super-admin env → account → legacy per-client hash → bootstrap. **`workspace.py`** — per-client
  Atrium state (`workspace/<c>.json`). Both import `google-cloud-storage` lazily and have a local-fs
  backend (`REGISTRY_LOCAL_DIR` / `WORKSPACE_LOCAL_DIR`) so they run off-cloud.
- **Sign-up + approval:** `GET/POST /signup` (Agora-branded `signup.html`) creates a **pending**
  client account; an admin approves it from `/admin/atrium` (`POST /admin/accounts/{approve,reject}`),
  which creates the client + blank workspace and activates the login. No public self-service access.
- **Operator console (`/admin/atrium`, `admin_atrium.html`)** = a left **side panel** with panes:
  Clients (cards + add) · Access requests · Accounts · Create account · Profile. Account management
  routes (`/admin/accounts/{create-client,create-admin,set-password,reset-password,delete}` +
  `/admin/profile/password`) are gated `is_superadmin()`; **admin-account** creation/management is
  gated `is_root_admin()`. **Roles:** `client` < `admin` (clients `["*"]`) < `superadmin`. THE super
  admin is `SUPER_ADMIN_EMAIL` (default `info@agoradatadriven.com`, env-overridable) or any account
  with role `superadmin`; only they create/manage admin accounts and they can't be deleted from the
  list. The no-password preview (`DEV_NOAUTH`) auto-signs in as `SUPER_ADMIN_EMAIL`.
- **`templates/*.html`** — big self-contained pages. Inline JS must be **esprima-4.x-safe** (no `?.`
  / `??`; classic `&&`/`||`). No Jinja inside `<script>` — JS reads state from the DOM.
- **`atrium_docs.py` / `feedback_ai.py`** — the opt-in Google-Doc → AI strategy feature (gated, degrades).
- **`atrium_health.py`** — the team-only Website Health tab: fetches the client's live site + detects
  installed marketing tags (GTM/GA4/pixels) by scanning the page HTML (no GTM API, infra-free, degrades).
- **`brand.py`** — bundled palette + AGORA mark (the container can't read repo-root `assets/`).

**Deploy:** `deploy_dash_platform.ps1` (build → `gcloud run deploy platform-dash --no-invoker-iam-check`).
**Test (off-cloud, what CI runs):** `python _workspace_localtest.py`, `python _accounts_localtest.py`, and `python _atrium_smoketest.py`
from this dir. **Preview:** `run_local.ps1` (or `preview/Preview Portal (admin).cmd` at repo root).
