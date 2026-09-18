#!/usr/bin/env python3
#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Self-service web app for ns8-idevice-backup.
#
# Users log in with their AD/LDAP account and manage the backups of THEIR OWN
# devices only. It runs from the same image as the engine, shares the module
# state (device registry at /state/devices.json) and the idevice-lockdown /
# idevice-data volumes, and runs idevice-tool locally for backups.
#
# Every device operation is gated by owner == the logged-in user. Devices with
# no owner are managed only in cluster-admin and never shown here.

import base64
import html
import json
import os
import plistlib
import re
import secrets
import ssl
import subprocess
import threading
import time

import ldap3
from ldap3.utils.conv import escape_filter_chars
from flask import Flask, request, redirect, session, abort

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-insecure-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=8 * 3600,
)

STATE_DIR = os.environ.get("STATE_DIR", "/state")
DEVICES_JSON = os.path.join(STATE_DIR, "devices.json")
LOCKDOWN_DIR = "/var/lib/lockdown"
BACKUP_ROOT = "/data/backups"
UDID_RE = re.compile(r"^[A-Za-z0-9-]{16,45}$")

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
LDAP_DOMAIN = os.environ.get("LDAP_DOMAIN", "").strip()
APP_TITLE = os.environ.get("APP_TITLE", "iOS Device Backup")
ALLOW_RESTORE = os.environ.get("SELFSERVICE_RESTORE", "true").strip().lower() in ("1", "true", "yes", "on")

_running = {}          # udid -> True while a backup runs in this process
_reg_lock = threading.Lock()


# --------------------------------------------------------------------------- LDAP

def _tls():
    if LDAP_CA_FILE and os.path.exists(LDAP_CA_FILE):
        return ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=LDAP_CA_FILE, version=ssl.PROTOCOL_TLS_CLIENT)
    return ldap3.Tls(validate=ssl.CERT_NONE)


def _server():
    host = LDAP_HOST or (LDAP_URL.split("://", 1)[-1].split(":")[0] if LDAP_URL else "")
    return ldap3.Server(host, port=LDAP_PORT, use_ssl=LDAP_SSL, tls=_tls() if LDAP_SSL else None)


def ldap_authenticate(login, password):
    if not (LDAP_URL and LDAP_BASE_DN and LDAP_BIND_DN) or not login or not password:
        return None
    srv = _server()
    try:
        mgr = ldap3.Connection(srv, user=LDAP_BIND_DN, password=LDAP_BIND_PASSWORD, auto_bind=True, receive_timeout=15)
    except Exception:
        return None
    safe = escape_filter_chars(login)
    flt = f"(&({LDAP_USER_ATTRIBUTE}={safe})(memberOf={LDAP_GROUP}))" if LDAP_GROUP else f"({LDAP_USER_ATTRIBUTE}={safe})"
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


# ----------------------------------------------------------------------- registry

def load_devices():
    try:
        with open(DEVICES_JSON) as fp:
            return json.load(fp)
    except FileNotFoundError:
        return {}


def save_devices(devices):
    tmp = DEVICES_JSON + ".web.tmp"
    with open(tmp, "w") as fp:
        json.dump(devices, fp, indent=2)
    os.replace(tmp, DEVICES_JSON)


def owns(dev, uid):
    return bool(dev) and dev.get("owner") and dev.get("owner") == uid


def my_devices(uid):
    out = []
    for udid, d in load_devices().items():
        if d.get("owner") == uid:
            item = dict(d)
            item["udid"] = udid
            item["running"] = udid in _running
            item["snapshots"] = list_snapshots(udid)
            out.append(item)
    out.sort(key=lambda x: x.get("name", ""))
    return out


def list_snapshots(udid):
    d = os.path.join(BACKUP_ROOT, udid)
    if not os.path.isdir(d):
        return []
    snaps = []
    for name in sorted(os.listdir(d), reverse=True):
        p = os.path.join(d, name)
        if os.path.isdir(p) and not name.startswith("."):
            snaps.append(name)
    return snaps


# ------------------------------------------------------------------------- backup

def _do_backup(udid, ip, incremental, enc_pw, set_pw):
    dest = f"{BACKUP_ROOT}/{udid}/" + ("incremental" if incremental else time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime()))
    cmd = ["idevice-tool", "backup", "--udid", udid, "--ip", ip, "--dir", dest]
    if incremental:
        cmd.append("--incremental")
    if set_pw:
        cmd.append("--set-password")
    env = dict(os.environ)
    if set_pw and enc_pw:
        env["IDEVICE_SET_PASSWORD"] = enc_pw
    ok = False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        ok = proc.returncode == 0 and b'"ok": true' in proc.stdout.encode()
        err = ""
        if not ok:
            for line in reversed((proc.stdout or "").splitlines()):
                if line.strip().startswith("{"):
                    try:
                        err = json.loads(line).get("error") or json.loads(line).get("type") or ""
                    except Exception:
                        err = ""
                    break
    except Exception as e:
        err = str(e)
    with _reg_lock:
        devices = load_devices()
        d = devices.get(udid)
        if d is not None:
            if ok:
                d["last_backup"] = int(time.time())
                d["last_status"] = "ok"
                d["last_error"] = ""
                if set_pw:
                    d["encryption"] = True
            else:
                if not incremental:
                    subprocess.run(["rm", "-rf", dest], capture_output=True)
                d["last_status"] = "failed"
                d["last_error"] = (err or "backup failed")[:300]
            devices[udid] = d
            save_devices(devices)
    _running.pop(udid, None)


def start_backup(udid, dev):
    if udid in _running:
        return
    ip = (dev.get("ip") or "").strip()
    if not ip:
        return
    incremental = (dev.get("backup_mode") or "full") == "incremental"
    enc_pw = dev.get("encryption_password") or ""
    set_pw = bool(enc_pw) and not dev.get("encryption", False)
    _running[udid] = True
    threading.Thread(target=_do_backup, args=(udid, ip, incremental, enc_pw, set_pw), daemon=True).start()


# --------------------------------------------------------------------------- CSRF

def csrf_token():
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        session["csrf"] = tok
    return tok


def check_csrf():
    if request.form.get("csrf") != session.get("csrf"):
        abort(400)


# ----------------------------------------------------------------------------- UI

def esc(s):
    return html.escape(str(s or ""))


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>{title}</title><style>
:root{{color-scheme:light dark}}
body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f4f4f4;color:#161616}}
.wrap{{max-width:88rem;margin:1.5rem auto;padding:0 1.5rem}}
.login{{max-width:26rem;margin:5vh auto}}
.card{{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:1.25rem;margin-bottom:1rem}}
h1{{font-size:1.25rem;margin:0}} h2{{font-size:1rem;margin:0 0 .75rem}}
.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}}
label{{display:block;font-size:.8rem;color:#525252;margin:.6rem 0 .2rem}}
input,select{{width:100%;box-sizing:border-box;padding:.55rem;border:1px solid #8d8d8d;border-radius:4px;font-size:1rem;background:#fff;color:#161616}}
button{{padding:.5rem 1rem;border:0;border-radius:4px;background:#0f62fe;color:#fff;font-size:.95rem;cursor:pointer}}
button.sec{{background:#393939}} button.danger{{background:#da1e28}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}} th,td{{text-align:left;padding:.6rem .75rem;border-bottom:1px solid #e0e0e0;vertical-align:top;font-size:.92rem}}
col.c-dev{{width:34%}} col.c-status{{width:26%}} col.c-mode{{width:14%}} col.c-act{{width:26%}}
.udid{{font-family:monospace;font-size:.72rem;color:#6f6f6f}}
.ok{{color:#24a148;font-weight:600}}.bad{{color:#da1e28;font-weight:600}}.muted{{color:#6f6f6f;font-size:.85rem}}
.row{{display:flex;gap:.5rem;flex-wrap:wrap;align-items:end}} .row>div{{flex:1;min-width:8rem}}
.err{{background:#fff1f1;border:1px solid #da1e28;color:#a2191f;padding:.6rem;border-radius:4px;margin-bottom:1rem}}
.actbtns{{display:flex;gap:.4rem;flex-wrap:wrap}} form{{margin:0}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#f4f4f4}}.card{{background:#262626;border-color:#393939}}input,select{{background:#161616;color:#f4f4f4;border-color:#6f6f6f}}}}
</style></head><body><div class="wrap">{body}
<p class="muted" style="text-align:center">iOS Device Backup — self service</p></div></body></html>"""


def render(body, refresh=False):
    r = '<meta http-equiv="refresh" content="6">' if refresh else ""
    return PAGE.format(title=esc(APP_TITLE), body=body, refresh=r)


def login_view(err=False):
    e = '<div class="err">Invalid username or password.</div>' if err else ""
    body = (
        '<div class="login">'
        f'<div class="card"><h1>{esc(APP_TITLE)}</h1></div>'
        f'<div class="card">{e}<form method="post" action="/login">'
        f'<input type="hidden" name="csrf" value="{esc(csrf_token())}">'
        '<label for="u">Username</label><input id="u" name="username" autocomplete="username" autofocus>'
        '<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">'
        '<div style="margin-top:1rem"><button type="submit">Log in</button></div></form>'
        '<p class="muted">Use your organization account.</p></div></div>'
    )
    return render(body)


def fmt_time(epoch):
    if not epoch:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(epoch)))
    except Exception:
        return str(epoch)


def devices_view():
    uid = session["uid"]
    disp = esc(session.get("display", uid))
    devs = my_devices(uid)
    tok = esc(csrf_token())
    any_running = any(d["running"] for d in devs)

    rows = ""
    for d in devs:
        udid = esc(d["udid"])
        status = ""
        if d["running"]:
            status = '<span class="muted">backing up…</span>'
        elif d.get("last_status") == "ok":
            status = f'<span class="ok">ok</span> <span class="muted">{fmt_time(d.get("last_backup"))}</span>'
        elif d.get("last_status") == "failed":
            status = f'<span class="bad">failed</span><div class="muted">{esc(d.get("last_error"))}</div>'
        else:
            status = '<span class="muted">no backup yet</span>'
        snaps = d["snapshots"]
        snap_html = ""
        if snaps:
            opts = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in snaps)
            restore = ""
            if ALLOW_RESTORE:
                restore = (
                    f'<form method="post" action="/devices/{udid}/restore" onsubmit="return confirm('
                    f"'This ERASES the device and restores the selected backup. Continue?');\">"
                    f'<input type="hidden" name="csrf" value="{tok}">'
                    f'<select name="snapshot">{opts}</select>'
                    f'<button class="danger" style="margin-top:.4rem">Restore</button></form>'
                )
            snap_html = f'<div class="muted">{len(snaps)} snapshot(s)</div>{restore}'
        enc = "✓" if (d.get("encryption") or d.get("encryption_password_set") or d.get("encryption_password")) else "—"
        disabled = "disabled" if d["running"] else ""
        rows += f"""<tr>
<td><b>{esc(d.get('name'))}</b><div class="udid">{udid}</div>
  <form method="post" action="/devices/{udid}/ip" class="row" style="margin-top:.4rem">
    <input type="hidden" name="csrf" value="{tok}">
    <div><input name="ip" value="{esc(d.get('ip'))}" placeholder="WiFi IP"></div>
    <div style="flex:0"><button class="sec" {disabled}>Save IP</button></div></form></td>
<td>{status}<div style="margin-top:.3rem">{snap_html}</div></td>
<td>enc {enc}
  <form method="post" action="/devices/{udid}/mode" style="margin-top:.3rem">
    <input type="hidden" name="csrf" value="{tok}">
    <select name="backup_mode" onchange="this.form.submit()">
      <option value="full" {'selected' if (d.get('backup_mode') or 'full')=='full' else ''}>Full</option>
      <option value="incremental" {'selected' if d.get('backup_mode')=='incremental' else ''}>Incremental</option>
    </select></form></td>
<td><div class="actbtns">
  <form method="post" action="/devices/{udid}/backup"><input type="hidden" name="csrf" value="{tok}">
    <button {disabled}>Back up now</button></form>
  <form method="post" action="/devices/{udid}/delete" onsubmit="return confirm('Remove this device from the list?');">
    <input type="hidden" name="csrf" value="{tok}"><button class="danger" {disabled}>Remove</button></form>
</div></td></tr>"""

    if not devs:
        rows = '<tr><td colspan="4" class="muted">No devices yet. Add one below.</td></tr>'

    body = f"""
<div class="top"><h1>Hello, {disp}</h1>
  <form method="post" action="/logout"><input type="hidden" name="csrf" value="{tok}">
  <button class="sec">Log out</button></form></div>
<div class="card"><h2>My devices</h2>
<table><colgroup><col class="c-dev"><col class="c-status"><col class="c-mode"><col class="c-act"></colgroup><thead><tr><th>Device</th><th>Last backup</th><th>Mode</th><th>Actions</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card"><h2>Add a device</h2>
<p class="muted">Create the pairing file on your computer with the iPhone on USB, enable WiFi lockdown once, then upload the file here. The device is registered to your account.</p>
<form method="post" action="/devices/add" enctype="multipart/form-data">
  <input type="hidden" name="csrf" value="{tok}">
  <div class="row">
    <div><label>Name</label><input name="name" placeholder="My iPhone"></div>
    <div><label>WiFi IP</label><input name="ip" placeholder="192.168.1.40"></div>
  </div>
  <label>Backup encryption password (optional)</label><input name="encryption_password" type="password" autocomplete="new-password">
  <label>Pairing file (&lt;UDID&gt;.plist or .mobiledevicepairing)</label>
  <input type="file" name="pairing" accept=".plist,.mobiledevicepairing,application/xml,text/xml">
  <div style="margin-top:1rem"><button type="submit">Upload and add</button></div>
</form></div>"""
    return render(body, refresh=any_running)


# ----------------------------------------------------------------------- routes

@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/")
def index():
    if not session.get("uid"):
        return redirect("/login")
    return devices_view()


@app.get("/login")
def login_form():
    if session.get("uid"):
        return redirect("/")
    return login_view(err=bool(request.args.get("e")))


@app.post("/login")
def login():
    check_csrf()
    res = ldap_authenticate((request.form.get("username") or "").strip(), request.form.get("password") or "")
    if not res:
        return redirect("/login?e=1")
    uid, display = res
    keep = session.get("csrf")
    session.clear()
    session["csrf"] = keep
    session.permanent = True
    session["uid"] = uid
    session["display"] = display
    return redirect("/")


@app.post("/logout")
def logout():
    check_csrf()
    session.clear()
    return redirect("/login")


def _require_owned(udid):
    if not session.get("uid"):
        abort(403)
    if not UDID_RE.match(udid or ""):
        abort(404)
    dev = load_devices().get(udid)
    if not owns(dev, session["uid"]):
        abort(403)
    return dev


@app.post("/devices/add")
def device_add():
    check_csrf()
    uid = session.get("uid")
    if not uid:
        abort(403)
    f = request.files.get("pairing")
    if not f:
        return redirect("/?e=nofile")
    raw = f.read()
    try:
        pl = plistlib.loads(raw)
    except Exception:
        return redirect("/?e=badfile")
    udid = str(pl.get("UDID") or "").strip()
    if not udid:
        udid = re.sub(r"\.(plist|mobiledevicepairing)$", "", os.path.basename(f.filename or ""), flags=re.I)
    if not UDID_RE.match(udid):
        return redirect("/?e=baududid")
    for key in ("HostID", "SystemBUID", "HostCertificate", "DeviceCertificate"):
        if key not in pl:
            return redirect("/?e=incomplete")
    os.makedirs(LOCKDOWN_DIR, exist_ok=True)
    with open(os.path.join(LOCKDOWN_DIR, f"{udid}.plist"), "wb") as fp:
        fp.write(raw)
    with _reg_lock:
        devices = load_devices()
        d = devices.get(udid, {})
        if d.get("owner") and d.get("owner") != uid:
            abort(403)  # someone else's device
        d.update({
            "udid": udid,
            "name": (request.form.get("name") or "").strip() or d.get("name") or udid,
            "ip": (request.form.get("ip") or "").strip() or d.get("ip", ""),
            "owner": uid,
            "paired_at": int(time.time()),
        })
        if LDAP_DOMAIN:
            d["domain"] = LDAP_DOMAIN
        d.setdefault("backup_mode", "full")
        d.setdefault("last_backup", 0)
        d.setdefault("last_status", "")
        d.setdefault("encryption", False)
        pw = request.form.get("encryption_password") or ""
        if pw:
            d["encryption_password"] = pw
        devices[udid] = d
        save_devices(devices)
    return redirect("/")


@app.post("/devices/<udid>/ip")
def device_ip(udid):
    check_csrf()
    _require_owned(udid)
    with _reg_lock:
        devices = load_devices()
        devices[udid]["ip"] = (request.form.get("ip") or "").strip()
        save_devices(devices)
    return redirect("/")


@app.post("/devices/<udid>/mode")
def device_mode(udid):
    check_csrf()
    _require_owned(udid)
    m = (request.form.get("backup_mode") or "full").strip().lower()
    with _reg_lock:
        devices = load_devices()
        devices[udid]["backup_mode"] = "incremental" if m == "incremental" else "full"
        save_devices(devices)
    return redirect("/")


@app.post("/devices/<udid>/backup")
def device_backup(udid):
    check_csrf()
    dev = _require_owned(udid)
    start_backup(udid, dev)
    return redirect("/")


@app.post("/devices/<udid>/restore")
def device_restore(udid):
    check_csrf()
    dev = _require_owned(udid)
    if not ALLOW_RESTORE:
        abort(403)
    snapshot = (request.form.get("snapshot") or "").strip()
    if snapshot not in list_snapshots(udid):
        abort(404)
    ip = (dev.get("ip") or "").strip()
    if not ip or udid in _running:
        return redirect("/")
    dest = f"{BACKUP_ROOT}/{udid}/{snapshot}"
    enc_pw = dev.get("encryption_password") or ""

    def _do():
        env = dict(os.environ)
        if enc_pw:
            env["IDEVICE_RESTORE_PASSWORD"] = enc_pw
        cmd = ["idevice-tool", "restore", "--udid", udid, "--ip", ip, "--dir", dest, "--source", udid]
        try:
            subprocess.run(cmd, capture_output=True, text=True, env=env)
        finally:
            _running.pop(udid, None)

    _running[udid] = True
    threading.Thread(target=_do, daemon=True).start()
    return redirect("/")


@app.post("/devices/<udid>/delete")
def device_delete(udid):
    check_csrf()
    _require_owned(udid)
    with _reg_lock:
        devices = load_devices()
        devices.pop(udid, None)
        save_devices(devices)
    try:
        os.remove(os.path.join(LOCKDOWN_DIR, f"{udid}.plist"))
    except OSError:
        pass
    return redirect("/")


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("WEB_INTERNAL_PORT", "8080")))
