# `services/portal/` — the portal / CRM front-door

This is the **front-door** for Agora Data Driven: a single Cloud Run service, `platform-dash`,
served at **portal.agoradatadriven.com**. It is a reverse proxy plus a single login over every
per-client dashboard, and the home of the lightweight CRM. One project, one region
(`asia-southeast1`, Singapore), one shared Artifact Registry repo (`agora`).

## Plain English

A client (or a team member) logs in **once** at `portal.agoradatadriven.com`. From there they see
only the dashboards assigned to them, and they open each one **through the portal** — the portal
reverse-proxies `<c>.agoradatadriven.com` behind that single login. They never see a separate login
per dashboard. Internally each dashboard still has its own password gate; the portal login is
trusted **additively** on top of it (see SSO below), never as a replacement.

## What it is

- **Reverse proxy + single login.** `platform-dash` authenticates the user once, then proxies each
  client's dashboard under `/d/<c>/`. The per-client dashboards remain independent Cloud Run
  services (`<c>-dash`); the portal is the unified entry point in front of them.
- **The registry is ONE private JSON — no database.** All portal state (which clients exist, their
  display names, who may see what, and the CRM records described below) lives in a single private
  GCS object, `agora-data-driven-platform-dash/platform.json`. There is **no database**. The portal
  reads and writes that one object via its runtime service account
  (`platform-dash-web@agora-data-driven.iam.gserviceaccount.com`, which holds `objectAdmin` on the
  registry bucket). The bucket is private (uniform bucket-level access, no public grant) and is read
  only behind the portal login. `dash/seed_registry.py` writes the initial `platform.json`.

## How SSO works

The portal **mints** a signed cookie on a successful login, scoped to `.agoradatadriven.com` (note
the leading dot). Because of that leading dot the cookie is presented to **every**
`<c>.agoradatadriven.com` dashboard. Each dashboard **verifies** that cookie with the shared
vendored module `platform_sso.py` (HMAC-SHA256 over a small JSON payload), using the shared signing
key stored in Secret Manager as **`platform-sso-key`**.

- The signing key is created during the portal standup (`deploy.ps1`) — the portal is the
  party that *signs*, so the key is born here.
- Dashboards are wired to *trust* that cookie **additively** by `tools/enable_platform_sso.ps1`,
  which grants each dashboard's runtime SA read access to `platform-sso-key`, mounts it as the
  `SSO_SECRET` env var, and tells the dashboard its own `CLIENT_KEY`. A dashboard accepts the cookie
  only if the signature verifies, it has not expired, and its `CLIENT_KEY` is in the cookie's
  allowed-client list (or the cookie grants `*`, i.e. super-admin / all clients).
- **Additive, never a replacement.** Each dashboard's own password **always still works**. SSO is
  fail-safe by design: on a raw `*.run.app` host the `.agoradatadriven.com` cookie is never sent, so
  SSO is silently inert there and the password gate is the only path in. SSO only fires once a
  dashboard is served on its `<c>.agoradatadriven.com` custom domain.

The cookie name, domain, TTL, and the fail-closed verifier all live in the canonical
`platform_sso.py`, which is vendored byte-identically into `services/portal/dash/` (the minting side)
and into each `clients/client_<c>/dash/` (the verifying side).

## The super-admin console

`tools/enable_super_admin.ps1` grants the portal front-door god-mode so it can act as an operator
console straight from the browser instead of a laptop. It:

- creates a bootstrap **super-admin password** (`platform-super-admin-password`), mounted on the
  portal as `SUPER_ADMIN_PW`, and sets `REGION` so the console can address Cloud Run resources;
- grants the portal web SA project-level `roles/run.developer` so it can redeploy/rotate dashboards;
- per deployed dashboard, grants the web SA `secretVersionAdder` on that client's
  `<c>-dash-password` (to rotate the client password) and `serviceAccountUser` on the dashboard's
  runtime SA (to redeploy it).

Run it once during standup; it is safe to re-run (re-adding an existing IAM binding is a no-op, and
an already-present secret is left untouched).

## The CRM growth path

The same private `platform.json` registry is where the CRM grows. Alongside the dashboard
assignments it carries:

- **client records** — the businesses Agora works with (key, display name, contacts, status);
- **notes** — free-text history against a client;
- **tasks** — follow-ups / to-dos against a client.

Because it is one JSON object behind the portal login (no database to stand up), the CRM extends by
adding fields to the registry and UI to the portal — not by introducing new infrastructure.

## Agora Atrium — the client workspace

**Agora Atrium** is the co-branded client workspace built **into** `platform-dash` (the first big
step on the CRM growth path). A client logs into the portal and opens their workspace to see the
*strategy* behind their marketing, **approve / request changes** (re-decidable anytime) on paid and
organic content, **comment** on each creative, watch **results**, **message** the AGORA team, and
control their own **email notifications**. When **AGORA** (a super-admin) opens that same workspace,
the identical UI gains inline **edit-everything** controls — edit/delete strategy, add/edit/delete
content, upload creatives, edit metrics & calendar, generate AI summaries. It is **additive**: it
reuses the existing session auth, bucket, and runtime SA — **no new infra/IAM/bucket/secret/service**,
save the opt-in Google-Doc summary feature (below). The product name lives in one constant,
`WORKSPACE_NAME` in `dash/main.py`.

- **State is per-client JSON in the SAME bucket — no database.** Each client's workspace lives in
  one private object, **`workspace/<c>.json`**, in `agora-data-driven-platform-dash` (the registry
  bucket). `dash/workspace.py` is the only code that reads/writes it, mirroring `store.py`'s
  last-write-wins, load-modify-save pattern but one object **per client** (so clients never contend
  on a shared object). It carries: `metrics`, `today`, `split`, `series`, `activity`, `campaigns[]`
  (each with `strategy`/`ai_summary`/`strategy_doc` and `content[]` reviewed by status
  `awaiting|approved|changes` + a `client_note`, threaded `comments[]`, and an optional uploaded
  creative `image_object`/`image_mime`), `calendar[]`, `conversations[]` (messages from
  `client`/`agora`), and per-user `notify` prefs.
- **Uploaded creatives are their own private objects.** An admin upload is stored at
  `workspace/creatives/<c>/<content_id>` in the same bucket (so binary bytes never bloat the
  rewritten-in-full JSON) and served only through the authed proxy `GET /w/<c>/creative/<content_id>`
  — the bucket stays private, exactly like `/data.json`.
- **Off-cloud testable.** `workspace.py` imports `google-cloud-storage` lazily and supports a local
  filesystem backend via `WORKSPACE_LOCAL_DIR` (plus `WORKSPACE_BUCKET` / `WORKSPACE_PREFIX`
  overrides), so the data layer and the whole Flask surface run on a laptop with no GCS/ADC. See
  `dash/_workspace_localtest.py` (data layer) and `dash/_atrium_smoketest.py` (full route + template
  test, stubs GCS; needs a Flask-capable interpreter — the dev `.venv` excludes the web pins).
- **Routes (all behind the existing session auth).** Client-facing: `GET /w/<c>/` and
  `GET /w/<c>/<tab>` (tabs: overview, dashboard, leadgen, organic, calendar, conversations,
  settings) + `GET /w/<c>/creative/<id>`, gated `authed()` + `can_open(<c>)`; plus ownership-checked
  JSON POSTs `/w/<c>/{approve,request-changes,save-note,comment,send-message,save-notify}`. Inline
  admin editing (super-admin only): JSON POSTs `/w/<c>/admin/{strategy,strategy-doc,generate-summary,
  summary,campaign,delete-campaign,content,edit-content,delete-content,content-comment,upload-creative,
  remove-creative,metrics,calendar,reply}`. The older dark operator console `/admin/atrium` +
  `/admin/atrium/<c>` (+ `/campaign`, `/content`, `/conversation`, `/reply`, `/metrics` POSTs) stays as
  a fallback, gated `is_superadmin()`. The portal landing shows an **Open dashboard** link per client;
  the workspace `/w/<c>/` stays reachable directly and from the admin console.
- **Strategy doc → AI summary (optional, opt-in).** An admin pastes a Google Doc link on a campaign
  and clicks "Generate from doc". `dash/atrium_docs.py` reads it via the **Google Drive API** (lazy
  `googleapiclient`, runtime-SA ADC, `drive.readonly`; gated `ATRIUM_DOCS_ENABLED=1`; the doc must be
  shared with the runtime SA) and `feedback_ai.summarize_strategy` writes a client-facing summary with
  Claude (`claude-opus-4-8`, gated `FEEDBACK_AI_ENABLED` + `ANTHROPIC_API_KEY`). It degrades
  gracefully (no AI → a doc excerpt; no doc → empty, type it by hand) and stays hand-editable.
  Enabling it is the **one opt-in deviation** from "no new infra": turn on the Docs/Drive API, add
  `google-api-python-client` to `dash/requirements.txt`, and share the doc with the runtime SA.
- **Notifications are optional & graceful** (`dash/notify.py`, mirroring `feedback_ai.py`). By
  default a notification just **records an activity entry** in the client's workspace and logs to
  stdout. Real email is sent only if **both** `ATRIUM_EMAIL_ENABLED=1` and an
  `ATRIUM_EMAIL_API_KEY` (Secret-Manager-mounted) are set, with the provider SDK imported lazily — an
  unconfigured deploy can never break, and **no provider key is committed**. The team inbox is
  `ATRIUM_TEAM_EMAIL` (default `info@agoradatadriven.com`). Team→client emails respect each
  recipient's Notification-settings toggles (master switch wins).
- **The theme follows the brand kit.** The whole front-door — login, the portal landing, and the team
  console — uses the Agora **light** brand: a white canvas with bold black type, a green CTA, and a
  subtle purple accent, fronted by the AGORA mark from `dash/brand.py` (`assets/brand.json` is the
  brand board). The Atrium surface uses the same official palette (Data Green `#4FAB4A`, Accent Purple
  `#9484FB`), every selector scoped under `.atrium` so it stays self-contained. The per-client
  dashboards under `/d/<c>/` keep their own dark chrome (a small brand-coloured nav pill is injected
  over them). Inline JS is esprima-4.x-safe and reads state from the DOM (no Jinja in any script
  block), so the pre-deploy JS gate stays green.

**Seed the Riverdance demo (once).** `dash/seed_workspace.py` writes `workspace/riverdance.json`
(idempotent — refuses to clobber an existing object) and registers `riverdance` in the registry so
its **Open workspace** card appears:

```powershell
.\.venv\Scripts\python.exe services\portal\dash\seed_workspace.py
```

*(TODO: later derive the Dashboard metrics/series from the client's live `<c>.json` produced by the
dashboard pipeline, once the metric-taxonomy mapping is agreed — see the `# CRM:`/TODO marker.)*

## Local preview (no password) — for developers

To work on the portal/Atrium **without touching the live site**, double-click **`preview/Preview Portal (admin).cmd`**
at the repo root. It runs the whole front-door on your laptop and opens a browser tab at
`http://localhost:8080` with **no login** — you are auto-signed-in as a super-admin, so you can click
every client and edit every Atrium workspace in place. Edit the files under `services/portal/dash/`
and refresh; nothing is pushed anywhere.

How it stays safe and self-contained (see `dash/run_local.ps1`, which the `.cmd` just launches):

- It builds an **isolated** venv (`.venv-portal`, Flask + requests only) and points the data layer at
  a throwaway folder (`.local_portal_data`) via `REGISTRY_LOCAL_DIR` / `WORKSPACE_LOCAL_DIR`. It
  **never touches the real GCS bucket**, ADC, or any deployed service. Delete `.local_portal_data` to
  reset; the prod `.venv` is left untouched.
- It seeds demo clients + workspaces (`dash/seed_local.py`, idempotent) so there is real content to
  click into.
- The "no password" comes from `PORTAL_DEV_NOAUTH=1`, which a `@app.before_request` hook in `main.py`
  honors **only when `PORTAL_SECURE_COOKIES=0`** (the relaxed local-http posture). Production always
  serves https with secure cookies ON, so the no-auth mode **cannot activate in a deploy** even if the
  env var leaked. The launcher sets both together; deploys set neither.

No-shell alternative: run `services\portal\dash\run_local.ps1` from a terminal — same thing.

## Deploying

Two scripts, both **run as yourself** from the repo root (never Cloud Build from a laptop — the
Cloud Build SA cannot `iam.serviceAccounts.actAs` our runtime SAs, so a cloudbuild-driven deploy
fails; build the image as yourself, then `gcloud run deploy`).

| Script | What it does | When to run |
|--------|--------------|-------------|
| `deploy.ps1` | **One-shot, idempotent standup.** Enables APIs; ensures the `agora` AR repo + the private `agora-data-driven-platform-dash` bucket; creates the `platform-dash-web@` SA and its IAM (`objectAdmin` on the registry bucket, `secretAccessor`); creates the two secrets `platform-dash-session-key` and the shared `platform-sso-key`; builds + deploys `platform-dash` with `COOKIE_DOMAIN=.agoradatadriven.com` and the secrets, using `--no-invoker-iam-check`; then seeds `platform.json` via the repo `.venv` python on `dash/seed_registry.py`. | Once at standup; re-run to converge. |
| `dash/deploy_dash_platform.ps1` | **Redeploy the `platform-dash` service only** — build a fresh image and create-or-update the service (env + secrets, `--no-invoker-iam-check`). Does not touch IAM/bucket/secrets. | Every code/template change after standup. |

```powershell
# from the repo root
.\services\portal\deploy.ps1                 # full standup / converge
.\services\portal\dash\deploy_dash_platform.ps1       # fast redeploy of the portal only
```

After standup: map **portal.agoradatadriven.com** onto the `platform-dash` service, then run
`tools\enable_super_admin.ps1` and `tools\enable_platform_sso.ps1 -Keys "template"`.

### Org policy: no public Cloud Run

Domain Restricted Sharing rejects `--allow-unauthenticated`. The portal is therefore deployed with
`--no-invoker-iam-check`; the Flask app does its **own** password / SSO auth in-process, and the
private registry JSON is only ever read behind the portal login.

## A note on volatile values

The **live URL** of the deployed service (the `*.run.app` URL, and the custom domain mapping status
for `portal.agoradatadriven.com`) is volatile operational state — it lives **here**, not in
`CLAUDE.md`. `CLAUDE.md` is reserved for fixed facts (project, region, the data contract, the deploy
procedure, guardrails); anything that changes when you redeploy or remap a domain belongs in this
README or in your own operator notes.

## See also

- `tools/README.md` — the operator convenience scripts, including `enable_platform_sso.ps1` and
  `enable_super_admin.ps1`.
- `dash/platform_sso.py` — the canonical SSO signer/verifier (vendored byte-identically into each
  dashboard).
