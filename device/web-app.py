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
import sys
import subprocess
import threading
import time

import ldap3
from ldap3.utils.conv import escape_filter_chars
from flask import Flask, request, redirect, session, abort, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
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

# --------------------------------------------------------------------- i18n

from flask import request as _rq  # noqa: E402

T = {
    "en": {
        "invalid": "Invalid username or password.",
        "username": "Username", "password": "Password", "login": "Log in",
        "use_org": "Use your organization account.",
        "hello": "Hello, {name}", "logout": "Log out", "my_devices": "My devices",
        "h_device": "Device", "h_ip": "IP", "h_last": "Last backup",
        "h_snap": "Available snapshots", "h_mode": "Mode", "h_actions": "Actions",
        "save": "Save", "ok": "ok", "failed": "failed", "backing_up": "backing up…",
        "no_backup": "no backup yet", "enc": "enc", "full": "Full", "incremental": "Incremental",
        "restore": "Restore", "backup_now": "Back up now", "remove": "Remove",
        "confirm_restore": "This ERASES the device and restores the selected backup. Continue?",
        "confirm_remove": "Remove this device from the list?",
        "no_devices": "No devices yet. Add one below.",
        "add_device": "Add a device",
        "add_intro": "Register your iPhone or iPad, then back it up over WiFi. You need a pairing file created once on your own computer — see the steps below.",
        "f_name": "Name", "f_ip": "WiFi IP",
        "f_encpw": "Backup encryption password (optional)",
        "f_pairing": "Pairing file (<UDID>.plist or .mobiledevicepairing)",
        "upload_add": "Upload and add",
        "footer": "iOS Device Backup — self service",
        "howto_title": "How to get the pairing file",
        "howto_intro": "Do this once per device on your own computer, with the iPhone connected by USB:",
        "howto_1": "Connect the iPhone by USB and confirm \u201cTrust this computer\u201d. iTunes or the Apple Devices app must be installed.",
        "howto_2": "Your computer now holds a pairing file. On Windows it is %ProgramData%\\Apple\\Lockdown\\<UDID>.plist, on macOS /var/db/lockdown/<UDID>.plist. Upload that file above without renaming it.",
        "howto_3": "Enable WiFi access once: in iTunes / Apple Devices tick \u201cSync with this iPhone over Wi-Fi\u201d and apply, or run pymobiledevice3 lockdown wifi-connections on. The iPhone must have a passcode.",
        "howto_4": "Find the WiFi IP under Settings \u2192 Wi-Fi \u2192 the (i) next to the network, and enter it above.",
        "howto_5": "Keep the address stable: give the iPhone a fixed IP (a DHCP reservation on your router) and turn Private Wi-Fi Address OFF for this network (Settings \u2192 Wi-Fi \u2192 (i) \u2192 Private Wi-Fi Address). A changing address breaks the WiFi backup.",
    },
    "de": {
        "invalid": "Benutzername oder Passwort falsch.",
        "username": "Benutzername", "password": "Passwort", "login": "Anmelden",
        "use_org": "Melde dich mit deinem Organisationskonto an.",
        "hello": "Hallo, {name}", "logout": "Abmelden", "my_devices": "Meine Ger\u00e4te",
        "h_device": "Ger\u00e4t", "h_ip": "IP", "h_last": "Letztes Backup",
        "h_snap": "Verf\u00fcgbare Snapshots", "h_mode": "Modus", "h_actions": "Aktionen",
        "save": "Speichern", "ok": "ok", "failed": "fehlgeschlagen", "backing_up": "sichert\u2026",
        "no_backup": "noch kein Backup", "enc": "versch.", "full": "Voll", "incremental": "Inkrementell",
        "restore": "Wiederherstellen", "backup_now": "Jetzt sichern", "remove": "Entfernen",
        "confirm_restore": "Dies L\u00d6SCHT das Ger\u00e4t und spielt das gew\u00e4hlte Backup zur\u00fcck. Fortfahren?",
        "confirm_remove": "Dieses Ger\u00e4t aus der Liste entfernen?",
        "no_devices": "Noch keine Ger\u00e4te. F\u00fcge unten eins hinzu.",
        "add_device": "Ger\u00e4t hinzuf\u00fcgen",
        "add_intro": "Registriere dein iPhone oder iPad und sichere es \u00fcber WLAN. Du brauchst eine Pairing-Datei, die du einmal am eigenen Rechner erstellst \u2013 siehe die Schritte unten.",
        "f_name": "Name", "f_ip": "WLAN-IP",
        "f_encpw": "Backup-Verschl\u00fcsselungspasswort (optional)",
        "f_pairing": "Pairing-Datei (<UDID>.plist oder .mobiledevicepairing)",
        "upload_add": "Hochladen und hinzuf\u00fcgen",
        "footer": "iOS Device Backup \u2014 Self-Service",
        "howto_title": "So kommst du an die Pairing-Datei",
        "howto_intro": "Einmalig pro Ger\u00e4t am eigenen Rechner, das iPhone per USB angeschlossen:",
        "howto_1": "iPhone per USB anschlie\u00dfen und \u201eDiesem Computer vertrauen\u201c best\u00e4tigen. iTunes oder die App \u201eApple-Ger\u00e4te\u201c muss installiert sein.",
        "howto_2": "Der Rechner legt jetzt eine Pairing-Datei ab. Unter Windows %ProgramData%\\Apple\\Lockdown\\<UDID>.plist, unter macOS /var/db/lockdown/<UDID>.plist. Diese Datei oben hochladen, ohne sie umzubenennen.",
        "howto_3": "WLAN-Zugriff einmal aktivieren: in iTunes / Apple-Ger\u00e4te den Haken \u201eMit diesem iPhone \u00fcber WLAN synchronisieren\u201c setzen, oder pymobiledevice3 lockdown wifi-connections on ausf\u00fchren. Das iPhone braucht einen Code.",
        "howto_4": "Die WLAN-IP unter Einstellungen \u2192 WLAN \u2192 das (i) neben dem Netz ablesen und oben eintragen.",
        "howto_5": "Die Adresse stabil halten: dem iPhone eine feste IP geben (DHCP-Reservierung am Router) und \u201ePrivate WLAN-Adresse\u201c f\u00fcr dieses Netz AUSschalten (Einstellungen \u2192 WLAN \u2192 (i) \u2192 Private WLAN-Adresse). Eine wechselnde Adresse verhindert das WLAN-Backup.",
    },
}


def lang():
    lg = session.get("lang")
    if lg in T:
        return lg
    try:
        return _rq.accept_languages.best_match(["de", "en"]) or "en"
    except Exception:
        return "en"


def t(key, **kw):
    d = T.get(lang(), T["en"])
    v = d.get(key) or T["en"].get(key, key)
    return v.format(**kw) if kw else v


_running = {}          # udid -> True while a backup runs in this process
_reg_lock = threading.Lock()
_audit_lock = threading.Lock()
AUDIT_FILE = os.path.join(STATE_DIR, "audit.log")


def audit(action, uid="", **fields):
    """Append an audit line to the container log and a persistent file."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    kv = " ".join(f"{k}={v}" for k, v in fields.items() if v not in ("", None))
    line = f"{ts} user={uid or '-'} action={action} {kv}".rstrip()
    print("AUDIT " + line, file=sys.stderr, flush=True)
    try:
        with _audit_lock, open(AUDIT_FILE, "a") as fp:
            fp.write(line + "\n")
    except Exception:
        pass


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

def _do_backup(udid, ip, incremental, enc_pw, set_pw, uid=""):
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
    audit("backup_done", uid=uid, udid=udid, result=("ok" if ok else "failed"))
    _running.pop(udid, None)


def start_backup(udid, dev, uid=""):
    if udid in _running:
        return
    ip = (dev.get("ip") or "").strip()
    if not ip:
        return
    incremental = (dev.get("backup_mode") or "full") == "incremental"
    enc_pw = dev.get("encryption_password") or ""
    set_pw = bool(enc_pw) and not dev.get("encryption", False)
    _running[udid] = True
    threading.Thread(target=_do_backup, args=(udid, ip, incremental, enc_pw, set_pw, uid), daemon=True).start()


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


PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>{title}</title><style>
:root{{color-scheme:light dark}}
body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f4f4f4;color:#161616}}
.wrap{{max-width:104rem;margin:1.5rem auto;padding:0 1rem}}
.login{{max-width:26rem;margin:5vh auto}}
.card{{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:1.25rem;margin-bottom:1rem}}
h1{{font-size:1.25rem;margin:0}} h2{{font-size:1rem;margin:0 0 .75rem}}
.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}}
label{{display:block;font-size:.8rem;color:#525252;margin:.6rem 0 .2rem}}
input,select{{width:100%;box-sizing:border-box;padding:.55rem;border:1px solid #8d8d8d;border-radius:4px;font-size:1rem;background:#fff;color:#161616}}
.ip-in{{width:8rem}} .mode-sel{{width:auto;min-width:8rem}}
.iprow button{{white-space:nowrap}}
.iprow{{display:flex;gap:.5rem;align-items:center}}
.snaprow{{display:flex;gap:.5rem;align-items:center;flex-wrap:nowrap}}
.moderow{{display:flex;gap:.5rem;align-items:center;flex-wrap:nowrap;white-space:nowrap}} .snap-sel{{width:14.5rem;min-width:0;max-width:100%}}
.act-cell{{text-align:right}}
button{{padding:.5rem 1rem;border:0;border-radius:4px;background:#0f62fe;color:#fff;font-size:.95rem;cursor:pointer}}
button.sec{{background:#393939}} button.danger{{background:#da1e28}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}} th,td{{text-align:left;padding:.6rem .75rem;border-bottom:1px solid #e0e0e0;vertical-align:top;font-size:.92rem}}
col.c-dev{{width:14%}} col.c-ip{{width:14%}} col.c-last{{width:12%}} col.c-snap{{width:25%}} col.c-mode{{width:12%}} col.c-act{{width:23%}}
.udid{{font-family:monospace;font-size:.72rem;color:#6f6f6f}}
.ok{{color:#24a148;font-weight:600}}.bad{{color:#da1e28;font-weight:600}}.muted{{color:#6f6f6f;font-size:.85rem}}
.row{{display:flex;gap:.75rem;flex-wrap:wrap;align-items:end}} .row>div{{flex:1;min-width:8rem}} .row>div.sm{{flex:0 1 13rem}}
.err{{background:#fff1f1;border:1px solid #da1e28;color:#a2191f;padding:.6rem;border-radius:4px;margin-bottom:1rem}}
.actbtns{{display:flex;gap:.4rem;flex-wrap:wrap;justify-content:flex-end}}
.howto{{margin:.5rem 0 0 1.2rem;padding:0}} .howto li{{margin-bottom:.5rem}} form{{margin:0}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#f4f4f4}}.card{{background:#262626;border-color:#393939}}input,select{{background:#161616;color:#f4f4f4;border-color:#6f6f6f}}}}
</style></head><body><div class="wrap">{body}
<p class="muted" style="text-align:center">{footer}</p></div></body></html>"""


def howto_html():
    steps = "".join(f"<li>{esc(t(k))}</li>" for k in ("howto_1", "howto_2", "howto_3", "howto_4", "howto_5"))
    return (f'<div class="card"><h2>{esc(t("howto_title"))}</h2>'
            f'<p class="muted">{esc(t("howto_intro"))}</p><ol class="howto">{steps}</ol></div>')


def lang_switch():
    cur = lang()
    parts = []
    for code, label in (("de", "DE"), ("en", "EN")):
        cls = "langsw on" if code == cur else "langsw"
        parts.append(f'<a class="{cls}" href="{esc(url_for("set_lang", code=code))}">{label}</a>')
    return '<div class="langbar">' + " ".join(parts) + "</div>"


def render(body, refresh=False):
    r = '<meta http-equiv="refresh" content="6">' if refresh else ""
    return PAGE.format(title=esc(APP_TITLE), body=body, refresh=r, lang=lang(), footer=esc(t("footer")))


def login_view(err=False):
    login_url = esc(url_for("login"))
    e = f'<div class="err">{esc(t("invalid"))}</div>' if err else ""
    body = (
        '<div class="login">'
        + lang_switch()
        + f'<div class="card"><h1>{esc(APP_TITLE)}</h1></div>'
        + f'<div class="card">{e}<form method="post" action="{login_url}">'
        + f'<input type="hidden" name="csrf" value="{esc(csrf_token())}">'
        + f'<label for="u">{esc(t("username"))}</label><input id="u" name="username" autocomplete="username" autofocus>'
        + f'<label for="p">{esc(t("password"))}</label><input id="p" name="password" type="password" autocomplete="current-password">'
        + f'<div style="margin-top:1rem"><button type="submit">{esc(t("login"))}</button></div></form>'
        + f'<p class="muted">{esc(t("use_org"))}</p></div></div>'
    )
    return render(body)


def fmt_time(epoch):
    if not epoch:
        return "—"
    try:
        return time.strftime("%d.%m. %H:%M", time.localtime(int(epoch)))
    except Exception:
        return str(epoch)


def devices_view():
    uid = session["uid"]
    logout_url = esc(url_for("logout"))
    add_url = esc(url_for("device_add"))
    disp = esc(session.get("display", uid))
    devs = my_devices(uid)
    tok = esc(csrf_token())
    any_running = any(d["running"] for d in devs)
    L = {k: esc(t(k)) for k in ("logout", "my_devices", "h_device", "h_ip", "h_last",
        "h_snap", "h_mode", "h_actions", "save", "restore", "backup_now", "remove",
        "no_devices", "add_device", "f_name", "f_ip", "f_encpw", "f_pairing",
        "upload_add", "enc", "full", "incremental", "ok", "failed", "backing_up", "no_backup")}
    hello = esc(t("hello", name=disp))
    add_intro = esc(t("add_intro"))
    cr_restore = t("confirm_restore").replace("\\", "\\\\").replace("'", "\\'")
    cr_remove = t("confirm_remove").replace("\\", "\\\\").replace("'", "\\'")
    howto = howto_html()

    rows = ""
    for d in devs:
        udid = esc(d["udid"])
        u_ip = esc(url_for("device_ip", udid=d["udid"]))
        u_mode = esc(url_for("device_mode", udid=d["udid"]))
        u_backup = esc(url_for("device_backup", udid=d["udid"]))
        u_restore = esc(url_for("device_restore", udid=d["udid"]))
        u_delete = esc(url_for("device_delete", udid=d["udid"]))
        status = ""
        if d["running"]:
            status = '<span class="muted">backing up…</span>'
        elif d.get("last_status") == "ok":
            status = f'<span class="muted">{fmt_time(d.get("last_backup"))}</span> <span class="ok">{L["ok"]}</span>'
        elif d.get("last_status") == "failed":
            status = f'<span class="bad">{L["failed"]}</span><div class="muted">{esc(d.get("last_error"))}</div>'
        else:
            status = '<span class="muted">no backup yet</span>'
        snaps = d["snapshots"]
        snap_html = ""
        if snaps:
            opts = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in snaps)
            restore = ""
            if ALLOW_RESTORE:
                restore = (
                    f'<form method="post" action="{u_restore}" class="snaprow" onsubmit="return confirm('
                    f"'{cr_restore}');\">"
                    f'<input type="hidden" name="csrf" value="{tok}">'
                    f'<select class="snap-sel" name="snapshot">{opts}</select>'
                    f'<button class="danger">{L["restore"]}</button></form>'
                )
            snap_html = restore
        enc = "✓" if (d.get("encryption") or d.get("encryption_password_set") or d.get("encryption_password")) else "—"
        disabled = "disabled" if d["running"] else ""
        rows += f"""<tr>
<td><b>{esc(d.get('name'))}</b><div class="udid">{udid}</div></td>
<td><form method="post" action="{u_ip}" class="iprow">
    <input type="hidden" name="csrf" value="{tok}">
    <input class="ip-in" name="ip" value="{esc(d.get('ip'))}" placeholder="{L['f_ip']}">
    <button class="sec" {disabled}>{L['save']}</button></form></td>
<td>{status}</td>
<td>{snap_html or '<span class="muted">—</span>'}</td>
<td><div class="moderow"><span class="muted">{L['enc']} {enc}</span>
  <form method="post" action="{u_mode}">
    <input type="hidden" name="csrf" value="{tok}">
    <select class="mode-sel" name="backup_mode" onchange="this.form.submit()">
      <option value="full" {'selected' if (d.get('backup_mode') or 'full')=='full' else ''}>{L['full']}</option>
      <option value="incremental" {'selected' if d.get('backup_mode')=='incremental' else ''}>{L['incremental']}</option>
    </select></form></div></td>
<td class="act-cell"><div class="actbtns">
  <form method="post" action="{u_backup}"><input type="hidden" name="csrf" value="{tok}">
    <button {disabled}>{L['backup_now']}</button></form>
  <form method="post" action="{u_delete}" onsubmit="return confirm('{cr_remove}');">
    <input type="hidden" name="csrf" value="{tok}"><button class="danger" {disabled}>{L['remove']}</button></form>
</div></td></tr>"""

    if not devs:
        rows = '<tr><td colspan="6" class="muted">No devices yet. Add one below.</td></tr>'

    body = f"""
{lang_switch()}<div class="top"><h1>{hello}</h1>
  <form method="post" action="{logout_url}"><input type="hidden" name="csrf" value="{tok}">
  <button class="sec">{L['logout']}</button></form></div>
<div class="card"><h2>{L['my_devices']}</h2>
<table><colgroup><col class="c-dev"><col class="c-ip"><col class="c-last"><col class="c-snap"><col class="c-mode"><col class="c-act"></colgroup><thead><tr><th>{L['h_device']}</th><th>{L['h_ip']}</th><th>{L['h_last']}</th><th>{L['h_snap']}</th><th>{L['h_mode']}</th><th class="act-cell">{L['h_actions']}</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card"><h2>{L['add_device']}</h2>
<p class="muted">{add_intro}</p>
<form method="post" action="{add_url}" enctype="multipart/form-data">
  <input type="hidden" name="csrf" value="{tok}">
  <div class="row">
    <div class="sm"><label>{L['f_name']}</label><input name="name" placeholder="My iPhone"></div>
    <div class="sm"><label>{L['f_ip']}</label><input name="ip" placeholder="192.168.1.40"></div>
    <div><label>{L['f_encpw']}</label><input name="encryption_password" type="password" autocomplete="new-password"></div>
    <div><label>{L['f_pairing']}</label><input type="file" name="pairing" accept=".plist,.mobiledevicepairing,application/xml,text/xml"></div>
  </div>
  <div style="margin-top:1rem"><button type="submit">{L['upload_add']}</button></div>
</form></div>{howto}"""
    return render(body, refresh=any_running)


# ----------------------------------------------------------------------- routes

@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/")
def index():
    if not session.get("uid"):
        return redirect(url_for("login_form"))
    return devices_view()


@app.get("/login")
def login_form():
    if session.get("uid"):
        return redirect(url_for("index"))
    return login_view(err=bool(request.args.get("e")))


@app.post("/login")
def login():
    check_csrf()
    attempted = (request.form.get("username") or "").strip()
    res = ldap_authenticate(attempted, request.form.get("password") or "")
    if not res:
        audit("login", uid=attempted, result="fail", src=request.remote_addr)
        return redirect(url_for("login_form", e=1))
    uid, display = res
    audit("login", uid=uid, result="ok", src=request.remote_addr)
    keep = session.get("csrf")
    session.clear()
    session["csrf"] = keep
    session.permanent = True
    session["uid"] = uid
    session["display"] = display
    return redirect(url_for("index"))


@app.get("/lang/<code>")
def set_lang(code):
    if code in T:
        session["lang"] = code
    return redirect(_rq.referrer or url_for("index"))


@app.post("/logout")
def logout():
    check_csrf()
    audit("logout", uid=session.get("uid", ""), src=request.remote_addr)
    session.clear()
    return redirect(url_for("login_form"))


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
        return redirect(url_for("index", e="nofile"))
    raw = f.read()
    try:
        pl = plistlib.loads(raw)
    except Exception:
        return redirect(url_for("index", e="badfile"))
    udid = str(pl.get("UDID") or "").strip()
    if not udid:
        udid = re.sub(r"\.(plist|mobiledevicepairing)$", "", os.path.basename(f.filename or ""), flags=re.I)
    if not UDID_RE.match(udid):
        return redirect(url_for("index", e="baududid"))
    for key in ("HostID", "SystemBUID", "HostCertificate", "DeviceCertificate"):
        if key not in pl:
            return redirect(url_for("index", e="incomplete"))
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
    audit("device_add", uid=uid, udid=udid, src=request.remote_addr)
    return redirect(url_for("index"))


@app.post("/devices/<udid>/ip")
def device_ip(udid):
    check_csrf()
    _require_owned(udid)
    with _reg_lock:
        devices = load_devices()
        devices[udid]["ip"] = (request.form.get("ip") or "").strip()
        save_devices(devices)
    audit("device_ip", uid=session.get("uid", ""), udid=udid, src=request.remote_addr)
    return redirect(url_for("index"))


@app.post("/devices/<udid>/mode")
def device_mode(udid):
    check_csrf()
    _require_owned(udid)
    m = (request.form.get("backup_mode") or "full").strip().lower()
    with _reg_lock:
        devices = load_devices()
        devices[udid]["backup_mode"] = "incremental" if m == "incremental" else "full"
        save_devices(devices)
    audit("device_mode", uid=session.get("uid", ""), udid=udid, mode=m, src=request.remote_addr)
    return redirect(url_for("index"))


@app.post("/devices/<udid>/backup")
def device_backup(udid):
    check_csrf()
    dev = _require_owned(udid)
    start_backup(udid, dev, session.get("uid", ""))
    audit("backup_start", uid=session.get("uid", ""), udid=udid, mode=dev.get("backup_mode", "full"), src=request.remote_addr)
    return redirect(url_for("index"))


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
        return redirect(url_for("index"))
    dest = f"{BACKUP_ROOT}/{udid}/{snapshot}"
    enc_pw = dev.get("encryption_password") or ""
    ruid = session.get("uid", "")
    audit("restore_start", uid=ruid, udid=udid, snapshot=snapshot, src=request.remote_addr)

    def _do():
        env = dict(os.environ)
        if enc_pw:
            env["IDEVICE_RESTORE_PASSWORD"] = enc_pw
        cmd = ["idevice-tool", "restore", "--udid", udid, "--ip", ip, "--dir", dest, "--source", udid]
        ok = False
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
            ok = proc.returncode == 0 and '"ok": true' in (proc.stdout or "")
        finally:
            audit("restore_done", uid=ruid, udid=udid, snapshot=snapshot, result=("ok" if ok else "failed"))
            _running.pop(udid, None)

    _running[udid] = True
    threading.Thread(target=_do, daemon=True).start()
    return redirect(url_for("index"))


@app.post("/devices/<udid>/delete")
def device_delete(udid):
    check_csrf()
    _require_owned(udid)
    with _reg_lock:
        devices = load_devices()
        devices.pop(udid, None)
        save_devices(devices)
    audit("device_delete", uid=session.get("uid", ""), udid=udid, src=request.remote_addr)
    try:
        os.remove(os.path.join(LOCKDOWN_DIR, f"{udid}.plist"))
    except OSError:
        pass
    return redirect(url_for("index"))


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("WEB_INTERNAL_PORT", "8080")))
