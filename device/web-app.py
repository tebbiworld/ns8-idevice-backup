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
import contextlib
import fcntl
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
from flask import Flask, request, redirect, session, abort, url_for, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
# An empty or missing secret must not end in a fixed, guessable key or in a
# 500 on every page: fall back to a random one (sessions then end with a restart).
app.secret_key = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=8 * 3600,
)

STATE_DIR = os.environ.get("STATE_DIR", "/state")
DEVICES_JSON = os.path.join(STATE_DIR, "devices.json")
SECRETS_JSON = os.path.join(STATE_DIR, "device-secrets.json")
REGISTRY_LOCK = os.path.join(STATE_DIR, ".devices.lock")
LOCKDOWN_DIR = "/var/lib/lockdown"
BACKUP_ROOT = os.environ.get("BACKUP_ROOT", "/data/backups")
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
try:
    RETENTION = max(0, int(os.environ.get("RETENTION", "0") or 0))   # 0 = the portal does not prune
except ValueError:
    RETENTION = 0


def _load_tool():
    """The snapshot and status logic lives in idevice-tool; use it as a library."""
    import importlib.machinery
    import importlib.util
    path = os.environ.get("IDEVICE_TOOL", "/usr/local/bin/idevice-tool")
    loader = importlib.machinery.SourceFileLoader("idevice_tool", path)
    spec = importlib.util.spec_from_loader("idevice_tool", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


tool = _load_tool()

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
        "st_running": "Backing up: {pct} %", "st_restoring": "Restoring: {pct} %",
        "st_waiting": "Connection lost, next attempt shortly ({n} of {m})",
        "st_attempt": "attempt {n} of {m}", "st_resumed": "continuing an unfinished backup",
        "st_unfinished": "unfinished backup kept, the next run continues it",
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
        "st_running": "Sicherung l\u00e4uft: {pct} %", "st_restoring": "Wiederherstellung: {pct} %",
        "st_waiting": "Verbindung verloren, n\u00e4chster Versuch gleich ({n} von {m})",
        "st_attempt": "Versuch {n} von {m}", "st_resumed": "unvollst\u00e4ndiges Backup wird fortgesetzt",
        "st_unfinished": "unvollst\u00e4ndiges Backup bleibt erhalten, der n\u00e4chste Lauf setzt es fort",
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

@contextlib.contextmanager
def registry_locked():
    """Serialise read-modify-write of the registry and the secrets with the
    module actions on the host (same lock file, flock crosses the container)."""
    fd = os.open(REGISTRY_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with _reg_lock:
            yield
    finally:
        os.close(fd)


def _load_json(path):
    try:
        with open(path) as fp:
            data = json.load(fp)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path, data):
    tmp = f"{path}.web.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fp:
        json.dump(data, fp, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_devices():
    return _load_json(DEVICES_JSON)


def save_devices(devices):
    # the backup passwords live in device-secrets.json, never in the registry
    for d in devices.values():
        if isinstance(d, dict):
            d.pop("encryption_password", None)
    _save_json(DEVICES_JSON, devices)


def get_device_password(udid):
    return str((_load_json(SECRETS_JSON).get(udid) or {}).get("encryption_password") or "")


def set_device_password(udid, password):
    """Call with the registry lock held. An empty password removes the entry."""
    secrets_ = _load_json(SECRETS_JSON)
    if password:
        secrets_[udid] = {"encryption_password": str(password)}
    else:
        secrets_.pop(udid, None)
    _save_json(SECRETS_JSON, secrets_)


def owns(dev, uid):
    return bool(dev) and dev.get("owner") and dev.get("owner") == uid


def backup_base(udid):
    return os.path.join(BACKUP_ROOT, udid)


def run_state(udid):
    """What runs for this device right now, whoever started it (this portal,
    cluster-admin or the timer): the tool keeps <base>/.status.json current."""
    st = tool.read_status(backup_base(udid))
    active = st.get("state") in ("running", "waiting", "restoring")
    if not active and udid in _running:
        # started here a moment ago, the tool has not written its status yet
        return {"running": True, "state": "running", "percent": 0, "attempt": 1, "attempts": 0, "resumed": False}
    return {
        "running": active,
        "state": st.get("state", "idle") if active else "idle",
        "percent": int(st.get("percent") or 0) if active else 0,
        "attempt": int(st.get("attempt") or 0),
        "attempts": int(st.get("attempts") or 0),
        "resumed": bool(st.get("resumed")) if active else False,
    }


def status_text(rs):
    if rs["state"] == "waiting":
        return t("st_waiting", n=min(rs["attempt"] + 1, rs["attempts"] or rs["attempt"] + 1), m=rs["attempts"] or "?")
    text = t("st_restoring" if rs["state"] == "restoring" else "st_running", pct=rs["percent"])
    extra = []
    if rs["attempt"] > 1 and rs["attempts"]:
        extra.append(t("st_attempt", n=rs["attempt"], m=rs["attempts"]))
    if rs["resumed"] and rs["state"] == "running":
        extra.append(t("st_resumed"))
    return text + (" (" + ", ".join(extra) + ")" if extra else "")


def my_devices(uid):
    out = []
    secrets_ = _load_json(SECRETS_JSON)
    for udid, d in load_devices().items():
        if d.get("owner") == uid:
            item = dict(d)
            item.pop("encryption_password", None)
            item["udid"] = udid
            item["run"] = run_state(udid)
            item["running"] = item["run"]["running"]
            item["password_set"] = bool((secrets_.get(udid) or {}).get("encryption_password"))
            snaps = list_snapshots(udid)
            item["snapshots"] = [n for n, complete in snaps if complete]
            item["unfinished"] = any(not complete for _, complete in snaps)
            out.append(item)
    out.sort(key=lambda x: x.get("name", ""))
    return out


def list_snapshots(udid):
    """[(name, complete)], newest first. Only a backup the device finished can be restored."""
    base = backup_base(udid)
    return [(n, tool.is_complete(base, n, udid)) for n in reversed(tool.snapshot_names(base))]


# ------------------------------------------------------------------------- backup

def _do_backup(udid, ip, incremental, enc_pw, set_pw, uid=""):
    # The tool chooses the snapshot: a new one, or the unfinished one of the last
    # run, which it continues. A failed run is kept (marked unfinished) instead of
    # deleted, and a lost connection is tried again before the run counts as failed.
    cmd = ["idevice-tool", "backup", "--udid", udid, "--ip", ip, "--base", backup_base(udid),
           "--mode", "incremental" if incremental else "full", "--retention", str(RETENTION)]
    if set_pw:
        cmd.append("--set-password")
    env = dict(os.environ)
    if set_pw and enc_pw:
        env["IDEVICE_SET_PASSWORD"] = enc_pw
    ok = False
    result = {}
    err = ""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") == "attempt":
                audit("backup_attempt", uid=uid, udid=udid, attempt=f"{msg.get('attempt')}/{msg.get('attempts')}",
                      snapshot=msg.get("snapshot"), resumed=("yes" if msg.get("resumed") else "no"))
            elif msg.get("event") == "retry":
                audit("backup_retry", uid=uid, udid=udid, attempt=f"{msg.get('attempt')}/{msg.get('attempts')}",
                      reason=msg.get("type"), wait=f"{int(msg.get('wait') or 0)}s")
            elif "ok" in msg:
                result = msg
        proc.wait()
        ok = proc.returncode == 0 and result.get("ok") is True
        err = "" if ok else (result.get("error") or result.get("type") or "")
    except Exception as e:
        err = str(e)
    try:
        with registry_locked():
            devices = load_devices()
            d = devices.get(udid)
            if d is not None:
                if ok:
                    d["last_backup"] = int(time.time())
                    d["last_status"] = "ok"
                    d["last_error"] = ""
                    if "device_encrypted" in result:
                        d["encryption"] = bool(result["device_encrypted"])
                else:
                    d["last_status"] = "failed"
                    d["last_error"] = (err or "backup failed")[:500]
                devices[udid] = d
                save_devices(devices)
        audit("backup_done", uid=uid, udid=udid, result=("ok" if ok else "failed"),
              attempts=result.get("attempts_used", ""), snapshot=result.get("snapshot", ""))
    finally:
        # Whatever happens above (a full disk while saving the registry, for
        # example): the device must not stay marked as busy, otherwise
        # start_backup refuses every later run and the UI shows a backup that
        # is not running. The restore path does the same.
        _running.pop(udid, None)


def start_backup(udid, dev, uid=""):
    if udid in _running or run_state(udid)["running"]:
        return False
    ip = (dev.get("ip") or "").strip()
    if not ip:
        return False
    incremental = (dev.get("backup_mode") or "full") == "incremental"
    enc_pw = get_device_password(udid)
    set_pw = bool(enc_pw)  # the engine only enables it if the device is not already encrypted
    _running[udid] = True
    threading.Thread(target=_do_backup, args=(udid, ip, incremental, enc_pw, set_pw, uid), daemon=True).start()
    return True


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
.iprow{{display:flex;gap:.5rem;align-items:center;flex-wrap:nowrap}}
.snaprow{{display:flex;gap:.5rem;align-items:center;flex-wrap:nowrap}}
.moderow{{display:flex;gap:.5rem;align-items:center;flex-wrap:nowrap;white-space:nowrap}} .snap-sel{{width:14.5rem;min-width:0;max-width:100%}}
.act-cell{{text-align:right}}
button{{padding:.5rem 1rem;border:0;border-radius:4px;background:#0f62fe;color:#fff;font-size:.95rem;cursor:pointer}}
button.sec{{background:#393939}} button.danger{{background:#da1e28}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}} th,td{{text-align:left;padding:.6rem .75rem;border-bottom:1px solid #e0e0e0;vertical-align:top;font-size:.92rem}}
col.c-dev{{width:14%}} col.c-ip{{width:17%}} col.c-last{{width:13%}} col.c-snap{{width:25%}} col.c-mode{{width:12%}} col.c-act{{width:19%}}
.udid{{font-family:monospace;font-size:.72rem;color:#6f6f6f}}
.ok{{color:#24a148;font-weight:600}}.bad{{color:#da1e28;font-weight:600}}.muted{{color:#6f6f6f;font-size:.85rem}}
.row{{display:flex;gap:.75rem;flex-wrap:wrap;align-items:end}} .row>div{{flex:1;min-width:8rem}} .row>div.sm{{flex:0 1 13rem}}
.err{{background:#fff1f1;border:1px solid #da1e28;color:#a2191f;padding:.6rem;border-radius:4px;margin-bottom:1rem}}
.actbtns{{display:flex;gap:.4rem;flex-wrap:wrap;justify-content:flex-end}}
.prog{{height:.5rem;border-radius:.25rem;background:#e0e0e0;overflow:hidden;margin:.3rem 0 .15rem;max-width:16rem}}
.prog>span{{display:block;height:100%;background:#0f62fe;transition:width .6s}}
.prog.wait>span{{background:#f1c21b}}
.howto{{margin:.5rem 0 0 1.2rem;padding:0}} .howto li{{margin-bottom:.5rem}} form{{margin:0}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#f4f4f4}}.card{{background:#262626;border-color:#393939}}input,select{{background:#161616;color:#f4f4f4;border-color:#6f6f6f}}}}
/* Phones: the wide device table becomes one stacked card per device. */
@media (max-width:640px){{
  .wrap{{margin:.75rem auto;padding:0 .75rem}}
  .top{{flex-wrap:wrap;gap:.5rem}}
  table,tbody,tr,td{{display:block;width:auto}} thead{{display:none}} table{{table-layout:auto}}
  tr{{border:1px solid #e0e0e0;border-radius:8px;margin-bottom:1rem;padding:.35rem .85rem;background:#fff}}
  td{{border:0;padding:.45rem 0}} td:first-child{{padding-top:.2rem}}
  td::before{{content:attr(data-label);display:block;font-size:.72rem;color:#6f6f6f;margin-bottom:.2rem}}
  td.c-name::before{{content:none}} td.c-name{{font-size:1.05rem}}
  .iprow,.snaprow,.moderow,.actbtns{{flex-wrap:wrap;white-space:normal}}
  .act-cell{{text-align:left}} .actbtns{{justify-content:flex-start}}
  .ip-in,.snap-sel,.mode-sel{{width:100%}}
  .iprow .ip-in,.snaprow .snap-sel{{flex:1 1 60%}} .iprow button,.snaprow button{{flex:1 1 auto}}
  .actbtns form,.actbtns button{{flex:1 1 auto}}
  .row>div,.row>div.sm{{flex:1 1 100%}}
}}
@media (max-width:640px) and (prefers-color-scheme:dark){{tr{{background:#262626;border-color:#393939}}}}
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
    # Without JavaScript the page still reloads while a backup runs; with it the
    # script below updates the percentage in place and the reload is not needed.
    r = '<noscript><meta http-equiv="refresh" content="10"></noscript>' if refresh else ""
    return PAGE.format(title=esc(APP_TITLE), body=body, refresh=r, lang=lang(), footer=esc(t("footer")))


def status_html(d, L):
    """The "last backup" cell of one device."""
    rs = d["run"]
    if rs["running"]:
        cls = "prog wait" if rs["state"] == "waiting" else "prog"
        return (f'<div class="{cls}" role="progressbar" aria-valuemin="0" aria-valuemax="100" '
                f'aria-valuenow="{rs["percent"]}"><span style="width:{rs["percent"]}%"></span></div>'
                f'<div class="muted">{esc(status_text(rs))}</div>')
    hint = f'<div class="muted">{L["st_unfinished"]}</div>' if d.get("unfinished") else ""
    if d.get("last_status") == "ok":
        return f'<span class="muted">{fmt_time(d.get("last_backup"))}</span> <span class="ok">{L["ok"]}</span>' + hint
    if d.get("last_status") == "failed":
        return f'<span class="bad">{L["failed"]}</span><div class="muted">{esc(d.get("last_error"))}</div>' + hint
    return f'<span class="muted">{L["no_backup"]}</span>' + hint


POLL_JS = """<script>
(function(){
  var url=%s, busy=%s;
  function tick(){
    fetch(url,{credentials:'same-origin',headers:{'Accept':'application/json'}}).then(function(r){
      if(r.status===401||r.status===403){location.reload();return null;}
      return r.json();
    }).then(function(j){
      if(!j)return;
      var any=false;
      Object.keys(j.devices).forEach(function(u){
        var d=j.devices[u], cell=document.getElementById('st-'+u);
        if(d.running)any=true;
        if(cell&&cell.innerHTML!==d.html)cell.innerHTML=d.html;
      });
      // a run ended (or one was started elsewhere): reload once for buttons and snapshots
      if(any!==busy){location.reload();return;}
      setTimeout(tick, any?3000:20000);
    }).catch(function(){setTimeout(tick,10000);});
  }
  setTimeout(tick, busy?2000:20000);
})();
</script>"""


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
        "upload_add", "enc", "full", "incremental", "ok", "failed", "backing_up", "no_backup",
        "st_unfinished")}
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
        status = status_html(d, L)
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
        enc = "✓" if (d.get("encryption") or d.get("password_set")) else "—"
        disabled = "disabled" if d["running"] else ""
        rows += f"""<tr>
<td class="c-name" data-label="{L['h_device']}"><b>{esc(d.get('name'))}</b><div class="udid">{udid}</div></td>
<td data-label="{L['h_ip']}"><form method="post" action="{u_ip}" class="iprow">
    <input type="hidden" name="csrf" value="{tok}">
    <input class="ip-in" name="ip" value="{esc(d.get('ip'))}" placeholder="{L['f_ip']}">
    <button class="sec" {disabled}>{L['save']}</button></form></td>
<td data-label="{L['h_last']}" id="st-{udid}">{status}</td>
<td data-label="{L['h_snap']}">{snap_html or '<span class="muted">—</span>'}</td>
<td data-label="{L['h_mode']}"><div class="moderow"><span class="muted">{L['enc']} {enc}</span>
  <form method="post" action="{u_mode}">
    <input type="hidden" name="csrf" value="{tok}">
    <select class="mode-sel" name="backup_mode" onchange="this.form.submit()">
      <option value="full" {'selected' if (d.get('backup_mode') or 'full')=='full' else ''}>{L['full']}</option>
      <option value="incremental" {'selected' if d.get('backup_mode')=='incremental' else ''}>{L['incremental']}</option>
    </select></form></div></td>
<td class="act-cell" data-label="{L['h_actions']}"><div class="actbtns">
  <form method="post" action="{u_backup}"><input type="hidden" name="csrf" value="{tok}">
    <button {disabled}>{L['backup_now']}</button></form>
  <form method="post" action="{u_delete}" onsubmit="return confirm('{cr_remove}');">
    <input type="hidden" name="csrf" value="{tok}"><button class="danger" {disabled}>{L['remove']}</button></form>
</div></td></tr>"""

    if not devs:
        rows = f'<tr><td colspan="6" class="muted">{L["no_devices"]}</td></tr>'

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
    body += POLL_JS % (json.dumps(url_for("progress")), "true" if any_running else "false")
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


@app.get("/progress.json")
def progress():
    """State of the logged-in user's own devices, polled by the device page."""
    uid = session.get("uid")
    if not uid:
        return jsonify({"error": "login required"}), 401
    L = {k: esc(t(k)) for k in ("ok", "failed", "no_backup", "st_unfinished")}
    out = {}
    for d in my_devices(uid):
        rs = d["run"]
        out[d["udid"]] = {"running": rs["running"], "state": rs["state"], "percent": rs["percent"],
                          "attempt": rs["attempt"], "attempts": rs["attempts"], "resumed": rs["resumed"],
                          "last_status": d.get("last_status", ""), "html": status_html(d, L)}
    resp = jsonify({"devices": out})
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    with registry_locked():
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
            set_device_password(udid, pw)   # device-secrets.json (0600), not the registry
        devices[udid] = d
        save_devices(devices)
    audit("device_add", uid=uid, udid=udid, src=request.remote_addr)
    return redirect(url_for("index"))


@app.post("/devices/<udid>/ip")
def device_ip(udid):
    check_csrf()
    _require_owned(udid)
    with registry_locked():
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
    with registry_locked():
        devices = load_devices()
        devices[udid]["backup_mode"] = "incremental" if m == "incremental" else "full"
        save_devices(devices)
    audit("device_mode", uid=session.get("uid", ""), udid=udid, mode=m, src=request.remote_addr)
    return redirect(url_for("index"))


@app.post("/devices/<udid>/backup")
def device_backup(udid):
    check_csrf()
    dev = _require_owned(udid)
    if start_backup(udid, dev, session.get("uid", "")):
        audit("backup_start", uid=session.get("uid", ""), udid=udid, mode=dev.get("backup_mode", "full"), src=request.remote_addr)
    return redirect(url_for("index"))


@app.post("/devices/<udid>/restore")
def device_restore(udid):
    check_csrf()
    dev = _require_owned(udid)
    if not ALLOW_RESTORE:
        abort(403)
    snapshot = (request.form.get("snapshot") or "").strip()
    if snapshot not in [n for n, complete in list_snapshots(udid) if complete]:
        abort(404)
    ip = (dev.get("ip") or "").strip()
    if not ip or udid in _running or run_state(udid)["running"]:
        return redirect(url_for("index"))
    dest = f"{BACKUP_ROOT}/{udid}/{snapshot}"
    enc_pw = get_device_password(udid)
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
    with registry_locked():
        devices = load_devices()
        devices.pop(udid, None)
        save_devices(devices)
        set_device_password(udid, "")
    audit("device_delete", uid=session.get("uid", ""), udid=udid, src=request.remote_addr)
    try:
        os.remove(os.path.join(LOCKDOWN_DIR, f"{udid}.plist"))
    except OSError:
        pass
    return redirect(url_for("index"))


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("WEB_INTERNAL_PORT", "8080")))
