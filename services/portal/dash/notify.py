"""Optional, graceful notifications for Agora Atrium (mirrors feedback_ai.py).

There is no email capability in the portal yet, and we do not stand one up speculatively. By
DEFAULT every notification simply:
  * records an activity entry in the client's workspace (so the event shows in "Recent activity"), and
  * logs a line to stdout.

IF an email provider is later configured -- gated on an env flag AND a Secret-Manager-mounted key,
with the provider SDK imported LAZILY -- the same calls also send an email. An unconfigured deploy
can never break, because email is strictly optional (exactly the pattern feedback_ai.py uses for
the Anthropic SDK). No provider key is committed.

Enable real email by setting BOTH:
  * ATRIUM_EMAIL_ENABLED=1
  * ATRIUM_EMAIL_API_KEY=<provider key>     (mount from Secret Manager via env at deploy time)
Team inbox: ATRIUM_TEAM_EMAIL (default info@agoradatadriven.com).

Direction of travel:
  * client -> team   (approve / request-changes / send-message): notify the AGORA inbox.
  * team   -> client (add content / reply): notify the client, but ONLY recipients whose
                      Notification-settings toggles allow it (the master switch wins).
"""

import os
import sys

import workspace


def team_address():
    """The AGORA team inbox notifications are sent to (env override, sensible default)."""
    return os.environ.get("ATRIUM_TEAM_EMAIL", "info@agoradatadriven.com")


# --- Optional email transport (no-op until a provider is configured) ----------------------------
def _email_enabled():
    """True iff email is switched on AND a provider key is present. Fail-closed otherwise."""
    if os.environ.get("ATRIUM_EMAIL_ENABLED", "") not in ("1", "true", "True"):
        return False
    return bool(os.environ.get("ATRIUM_EMAIL_API_KEY", ""))


def _send_email(to, subject, body):
    """Send one email if a provider is configured; otherwise a no-op returning False.

    The provider SDK is imported LAZILY here so an unconfigured deploy has no hard dependency and
    cannot break. Until a provider is chosen this is a configured-but-unimplemented no-op.
    """
    if not _email_enabled() or not to:
        return False
    try:
        # TODO: pick an email provider (e.g. SendGrid / Mailgun / SES), lazily import its SDK here,
        # and send using ATRIUM_EMAIL_API_KEY (mounted from Secret Manager). Do NOT commit any key.
        return False
    except Exception:
        # Best-effort: a failed send must never raise into the request path.
        return False


def _log(msg):
    """Emit a notification line to stdout (the always-on default 'channel')."""
    try:
        sys.stdout.write("[atrium-notify] %s\n" % msg)
        sys.stdout.flush()
    except Exception:
        pass


def _record(client, icon, text):
    """Record an activity entry; swallow errors so a notification can't break the action."""
    try:
        workspace.add_activity(client, icon, text)
    except Exception:
        pass


# --- client -> team -----------------------------------------------------------------------------
def _changes_notify_on(client, user):
    """True if `user` still wants change-request notifications (master + changes both on).

    Best-effort: defaults to True if there is no user, no workspace, or the lookup fails, so a
    missing pref never silently drops the notification. Lets the actor switch change-request
    notifications off from Notification settings (the `changes` toggle)."""
    if not user:
        return True
    try:
        prefs = workspace.get_notify(workspace.load_workspace(client) or {}, user)
        return bool(prefs.get("master") and prefs.get("changes"))
    except Exception:
        return True


def client_decided(client, item, decision, user=None):
    """A client approved or requested changes on a content piece. Notify the AGORA team.

    'Approve' always notifies. A 'request changes' notification honours the actor's `changes`
    toggle in Notification settings, so it can be switched off without touching anything else."""
    ref = item.get("ref") or item.get("id") or "a piece"
    if decision == "approved":
        text, icon = "You approved %s." % ref, "check"
        subject = "%s approved %s" % (client, ref)
    else:
        if not _changes_notify_on(client, user):
            _log("change request on %s suppressed by %s's notify prefs" % (ref, user or "client"))
            return
        text, icon = "You requested changes on %s." % ref, "message"
        subject = "%s requested changes on %s" % (client, ref)
    _record(client, icon, text)
    _log("%s (by %s)" % (subject, user or "client"))
    _send_email(team_address(), subject, item.get("caption", ""))


def client_messaged(client, conversation, user=None):
    """A client sent a message in a conversation. Notify the AGORA team."""
    subject = conversation.get("subject", "(no subject)")
    _record(client, "message", 'You sent a message in "%s".' % subject)
    _log("client message in '%s' (by %s)" % (subject, user or "client"))
    messages = conversation.get("messages") or []
    body = messages[-1].get("body", "") if messages else ""
    _send_email(team_address(), "New Atrium message: %s" % subject, body)


def client_commented(client, item, body, user=None):
    """A client posted a comment on a content piece. Notify the AGORA team."""
    ref = item.get("ref") or item.get("id") or "a piece"
    _record(client, "message", "You commented on %s." % ref)
    _log("client comment on %s (by %s)" % (ref, user or "client"))
    _send_email(team_address(), "New Atrium comment on %s" % ref, body or "")


# --- visitor -> team ----------------------------------------------------------------------------
def signup_requested(company, email):
    """A visitor requested an account via self-service sign-up. Notify the AGORA team.

    There is no client/workspace yet (the request is `pending` until an admin approves), so this only
    logs + best-effort emails the team inbox -- it never records client activity.
    """
    subject = "New Atrium access request: %s" % (company or email)
    _log("access request from %s <%s>" % (company or "(no company)", email))
    _send_email(team_address(), subject,
                "%s (%s) requested an Agora Atrium account. Approve it in the team console." %
                (company or "A visitor", email))


# --- team -> client (gated by each recipient's prefs) -------------------------------------------
def _eligible_recipients(ws, kind):
    """Return (email, prefs) for users whose master switch AND `kind` toggle are on."""
    out = []
    for email in (ws.get("notify") or {}):
        prefs = workspace.get_notify(ws, email)
        if prefs.get("master") and prefs.get(kind):
            out.append((email, prefs))
    return out


def team_added_content(client, ws, item):
    """The team added content for review. Record activity; email opted-in recipients."""
    ref = item.get("ref") or item.get("id") or "new content"
    _record(client, "bell", "New content %s was added for your review." % ref)
    _log("team added content %s for %s" % (ref, client))
    for email, prefs in _eligible_recipients(ws, "content"):
        if prefs.get("frequency") == "instant":
            _send_email(email, "New content to review: %s" % ref, item.get("caption", ""))


def team_commented(client, ws, item, body, sender_name="AGORA"):
    """The team commented on a content piece. Record activity; email opted-in recipients (content)."""
    ref = item.get("ref") or item.get("id") or "a piece"
    _record(client, "message", '%s commented on %s.' % (sender_name, ref))
    _log("team comment on %s for %s" % (ref, client))
    for email, prefs in _eligible_recipients(ws, "content"):
        if prefs.get("frequency") == "instant":
            _send_email(email, "AGORA commented on %s" % ref, body or "")


def team_replied(client, ws, conversation, sender_name="AGORA"):
    """The team replied in a conversation. Record activity; email opted-in recipients."""
    subject = (conversation or {}).get("subject", "(no subject)")
    _record(client, "message", '%s replied in "%s".' % (sender_name, subject))
    _log("team reply in '%s' for %s" % (subject, client))
    messages = (conversation or {}).get("messages") or []
    body = messages[-1].get("body", "") if messages else ""
    for email, prefs in _eligible_recipients(ws, "replies"):
        if prefs.get("frequency") == "instant":
            _send_email(email, "AGORA replied: %s" % subject, body)


# --- super-admin audit alerts -------------------------------------------------------------------
def activity_alert(actor, role, client, action, detail=""):
    """Best-effort alert for the super admin's audit feed: log every workspace activity and, IF email
    is configured, also email the team inbox.

    The always-on view is the in-app Activity tab (audit.log_activity is the source of truth); this
    only adds an OPTIONAL email, dormant until ATRIUM_EMAIL_* is set, so an unconfigured deploy is
    completely unaffected. Never raises."""
    try:
        who = actor or role or "someone"
        line = "%s (%s) %s%s for %s" % (
            who, role or "?", action, (" -- " + detail) if detail else "", client or "?")
        _log("activity: " + line)
        _send_email(team_address(), "Atrium activity: %s %s" % (who, action), line)
    except Exception:
        pass
