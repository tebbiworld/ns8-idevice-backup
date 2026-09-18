#!/usr/bin/env python3
#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Self-service web app for ns8-idevice-backup.
#
# End users log in with their AD/LDAP credentials and manage the backups of
# THEIR OWN devices only. It runs from the same image as the engine, shares the
# module state (device registry) and volumes, and calls idevice-tool locally.
#
# M2 (this file): LDAP login + session + a placeholder "my devices" page. The
# per-user device operations (list own, pair, backup, restore) arrive in M3.
#
# LDAP is bound directly with admin-provided credentials (the ARSnova pattern):
# a manager bind searches the user, then a bind as the user verifies the
# password. Config comes from the environment (written by configure-module).

import html
import os
import ssl

import ldap3
from ldap3.utils.conv import escape_filter_chars
from flask import Flask, request, redirect, session, make_response

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-insecure-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,  # always served through Traefik (HTTPS)
    PERMANENT_SESSION_LIFETIME=8 * 3600,
)

LDAP_URL = os.environ.get("LDAP_URL_CONTAINER", os.environ.get("LDAP_URL", "")).strip()
LDAP_HOST = os.environ.get("LDAP_HOST_CONTAINER", os.environ.get("LDAP_HOST", "")).strip()
LDAP_PORT = int(os.environ.get("LDAP_PORT", "636") or "636")
LDAP_SSL = not LDAP_URL.startswith("ldap://")
LDAP_BASE_DN = os.environ.get("LDAP_BASE_DN", "").strip()
LDAP_BIND_DN = os.environ.get("LDAP_BIND_DN", "").strip()
LDAP_BIND_PASSWORD = os.environ.get("LDAP_BIND_PASSWORD", "")
LDAP_USER_ATTRIBUTE = os.environ.get("LDAP_USER_ATTRIBUTE", "sAMAccountName").strip() or "sAMAccountName"
LDAP_GROUP = os.environ.get("LDAP_GROUP", "").strip()
LDAP_CA_FILE = os.environ.get("LDAP_CA_FILE", "").strip()
APP_TITLE = os.environ.get("APP_TITLE", "iOS Device Backup")


def _tls():
    # On-prem AD usually presents a self-signed certificate. If configure-module
    # fetched the server certificate we validate against it (hostname check off,
    # as ARSnova does); otherwise we fall back to no validation.
    if LDAP_CA_FILE and os.path.exists(LDAP_CA_FILE):
        return ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=LDAP_CA_FILE, version=ssl.PROTOCOL_TLS_CLIENT)
    return ldap3.Tls(validate=ssl.CERT_NONE)


def _server():
    host = LDAP_HOST or (LDAP_URL.split("://", 1)[-1].split(":")[0] if LDAP_URL else "")
    return ldap3.Server(host, port=LDAP_PORT, use_ssl=LDAP_SSL, tls=_tls() if LDAP_SSL else None)


def ldap_authenticate(login, password):
    """Return (uid, display_name) on success, or None. Manager-bind, search the
    user (optionally restricted to a group), then bind as the user."""
    if not (LDAP_URL and LDAP_BASE_DN and LDAP_BIND_DN):
        return None
    if not login or not password:
        return None
    srv = _server()
    try:
        mgr = ldap3.Connection(srv, user=LDAP_BIND_DN, password=LDAP_BIND_PASSWORD, auto_bind=True, receive_timeout=15)
    except Exception:
        return None
    safe = escape_filter_chars(login)
    if LDAP_GROUP:
        flt = f"(&({LDAP_USER_ATTRIBUTE}={safe})(memberOf={LDAP_GROUP}))"
    else:
        flt = f"({LDAP_USER_ATTRIBUTE}={safe})"
    try:
        mgr.search(LDAP_BASE_DN, flt, attributes=[LDAP_USER_ATTRIBUTE, "displayName"])
        if not mgr.entries:
            return None
        entry = mgr.entries[0]
        user_dn = entry.entry_dn
        uid = str(entry[LDAP_USER_ATTRIBUTE].value) if LDAP_USER_ATTRIBUTE in entry else login
        try:
            display = str(entry["displayName"].value)
        except Exception:
            display = uid
    finally:
        mgr.unbind()
    try:
        uc = ldap3.Connection(srv, user=user_dn, password=password, receive_timeout=15)
        if not uc.bind():
            return None
        uc.unbind()
    except Exception:
        return None
    return uid, display


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{color-scheme:light dark}}
body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f4f4f4;color:#161616}}
.wrap{{max-width:26rem;margin:8vh auto;padding:0 1rem}}
.card{{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:1.5rem}}
h1{{font-size:1.25rem;margin:0 0 1rem}}
label{{display:block;font-size:.8rem;color:#525252;margin:.75rem 0 .25rem}}
input{{width:100%;box-sizing:border-box;padding:.6rem;border:1px solid #8d8d8d;border-radius:4px;font-size:1rem}}
button{{margin-top:1.25rem;width:100%;padding:.7rem;border:0;border-radius:4px;background:#0f62fe;color:#fff;font-size:1rem;cursor:pointer}}
.err{{background:#fff1f1;border:1px solid #da1e28;color:#a2191f;padding:.6rem;border-radius:4px;margin-bottom:1rem;font-size:.9rem}}
.muted{{color:#6f6f6f;font-size:.85rem}}
.top{{display:flex;justify-content:space-between;align-items:center}}
form.logout{{margin:0}} form.logout button{{width:auto;margin:0;padding:.4rem .8rem;background:#525252}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#f4f4f4}}.card{{background:#262626;border-color:#393939}}}}
</style></head><body><div class="wrap"><div class="card">{body}</div>
<p class="muted" style="text-align:center">iOS Device Backup — self service</p></div></body></html>"""


def render(body):
    return PAGE.format(title=html.escape(APP_TITLE), body=body)


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/")
def index():
    if not session.get("uid"):
        return redirect("/login")
    uid = html.escape(session.get("uid", ""))
    disp = html.escape(session.get("display", uid))
    body = (
        f'<div class="top"><h1>Hello, {disp}</h1>'
        '<form class="logout" method="post" action="/logout"><button>Log out</button></form></div>'
        f'<p class="muted">Signed in as <code>{uid}</code>.</p>'
        '<p>Your device backups will appear here. Device management is coming in the next update.</p>'
    )
    return render(body)


@app.get("/login")
def login_form():
    if session.get("uid"):
        return redirect("/")
    err = request.args.get("e")
    err_html = '<div class="err">Invalid username or password.</div>' if err else ""
    body = (
        f'<h1>{html.escape(APP_TITLE)}</h1>{err_html}'
        '<form method="post" action="/login">'
        '<label for="u">Username</label><input id="u" name="username" autocomplete="username" autofocus>'
        '<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">'
        '<button type="submit">Log in</button></form>'
        '<p class="muted">Use your organization account.</p>'
    )
    return render(body)


@app.post("/login")
def login():
    login_id = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    res = ldap_authenticate(login_id, password)
    if not res:
        return redirect("/login?e=1")
    uid, display = res
    session.clear()
    session.permanent = True
    session["uid"] = uid
    session["display"] = display
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    resp = make_response(redirect("/login"))
    return resp


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("WEB_INTERNAL_PORT", "8080")))
