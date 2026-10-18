#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Mail when a device has gone too long without a successful backup.

Called by bin/autobackup (hourly). A device is overdue when its last
successful backup is older than NOTIFY_OVERDUE_DAYS (or it never had one and
was added that long ago). The report goes to NOTIFY_MAIL_TO and, with
NOTIFY_OWNERS, to the owner's address the portal read from the directory at
login. At most one mail per device every NOTIFY_REPEAT_DAYS; a successful
backup resets it. Mail goes through the cluster smarthost.
"""

import email.message
import json
import os
import smtplib
import socket
import ssl
import sys
import time

import agent

STATE = "notify-state.json"
DEFAULTS = {"NOTIFY_OVERDUE_DAYS": 14, "NOTIFY_REPEAT_DAYS": 7}


def settings():
    env = os.environ
    days = {}
    for key, default in DEFAULTS.items():
        try:
            days[key] = max(1, int(env.get(key) or default))
        except ValueError:
            days[key] = default
    return {
        "mail_to": (env.get("NOTIFY_MAIL_TO") or "").strip(),
        "owners": (env.get("NOTIFY_OWNERS") or "false").lower() == "true",
        "overdue_days": days["NOTIFY_OVERDUE_DAYS"],
        "repeat_days": days["NOTIFY_REPEAT_DAYS"],
    }


def _load(path, default):
    try:
        with open(path) as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return default


def _send(recipients, subject, body):
    sh = agent.get_smarthost_settings(agent.redis_connect(use_replica=True))
    if not sh.get("enabled") or not sh.get("host"):
        raise RuntimeError("no cluster smarthost")
    host = os.environ.get("SELFSERVICE_HOST") or socket.getfqdn()
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"idevice-backup@{host.split('.', 1)[-1] if host.count('.') > 1 else host}"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    context = ssl.create_default_context()
    if not sh.get("tls_verify", True):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    port = int(sh.get("port") or 587)
    if sh.get("encrypt_smtp") == "tls":
        smtp = smtplib.SMTP_SSL(sh["host"], port, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(sh["host"], port, timeout=30)
        if sh.get("encrypt_smtp") == "starttls":
            smtp.starttls(context=context)
    with smtp:
        if sh.get("username"):
            smtp.login(sh["username"], sh.get("password") or "")
        smtp.send_message(msg)


def check(devices, now=None):
    """Send the due overdue mails; returns the number of mails sent."""
    cfg = settings()
    if not cfg["mail_to"] and not cfg["owners"]:
        return 0
    now = now or time.time()
    state = _load(STATE, {})
    sent = 0
    portal = os.environ.get("SELFSERVICE_HOST", "")
    for udid, dev in devices.items():
        if dev.get("auto_backup") is False:
            continue
        last = dev.get("last_backup") or 0
        since = last or dev.get("paired_at") or now
        if now - since < cfg["overdue_days"] * 86400:
            state.pop(udid, None)  # recovered (or still fresh): reset the reminder
            continue
        if now - state.get(udid, 0) < cfg["repeat_days"] * 86400:
            continue
        recipients = [cfg["mail_to"]] if cfg["mail_to"] else []
        if cfg["owners"] and dev.get("owner_mail") and dev["owner_mail"] not in recipients:
            recipients.append(dev["owner_mail"])
        if not recipients:
            continue
        name = dev.get("name") or udid
        days = int((now - since) / 86400)
        when = time.strftime("%d.%m.%Y %H:%M", time.localtime(last)) if last else "never"
        body = (
            f"The iPhone/iPad \"{name}\" (owner {dev.get('owner') or '-'}, WiFi IP {dev.get('ip') or '-'}) "
            f"has had no successful backup for {days} days.\n\n"
            f"Last successful backup: {when}\n"
            f"Last result: {dev.get('last_status') or '-'}\n"
            + (f"Last error: {dev.get('last_error')}\n" if dev.get("last_error") else "")
            + "\nUsual causes: the device was not on the WiFi when the hourly automatic backup ran, "
            "its IP address changed, or it is locked and asleep. Connect it to power and WiFi, "
            "or start a backup in the portal"
            + (f" (https://{portal}/)" if portal else "") + ".\n"
        )
        try:
            _send(recipients, f"iOS backup overdue: {name} ({days} days)", body)
            state[udid] = now
            sent += 1
            print(f"notify: overdue mail for {name} sent to {len(recipients)} recipient(s)", file=sys.stderr)
        except Exception as exc:
            # never print smtplib's text: it may echo addresses
            print(agent.SD_WARNING + f"notify: mail for {name} not sent ({type(exc).__name__})", file=sys.stderr)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fp:
        json.dump(state, fp)
    os.replace(tmp, STATE)
    return sent
