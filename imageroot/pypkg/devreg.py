#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Device registry and device secrets of the module.

state/devices.json        what the UIs show: name, IP, owner, mode, last status
state/device-secrets.json the backup encryption password of each device

Both files are 0600. The passwords are kept apart from the registry so that
nothing which reads or returns the registry (get-configuration, the portal,
logs) can leak them. The self-service portal uses the same two files from
inside its container (device/web-app.py carries the same few functions).
"""

import contextlib
import fcntl
import json
import os

DEVICES = "devices.json"
SECRETS = "device-secrets.json"
LOCK = ".devices.lock"


def _path(name):
    return os.path.join(os.environ.get("AGENT_STATE_DIR", "."), name)


@contextlib.contextmanager
def locked():
    """Serialise read-modify-write of both files across actions, the timer
    and the portal (flock works across the container boundary: same file)."""
    fd = os.open(_path(LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _load(name):
    try:
        with open(_path(name)) as fp:
            data = json.load(fp)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(name, data):
    path = _path(name)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fp:
        json.dump(data, fp, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_devices():
    return _load(DEVICES)


def save_devices(devices):
    # never let a password slip back into the registry
    for dev in devices.values():
        if isinstance(dev, dict):
            dev.pop("encryption_password", None)
    _save(DEVICES, devices)


def get_password(udid):
    return str((_load(SECRETS).get(udid) or {}).get("encryption_password") or "")


def has_password(udid):
    return bool(get_password(udid))


def set_password(udid, password):
    """Store the password of a device; an empty one removes it."""
    secrets = _load(SECRETS)
    if password:
        secrets[udid] = {"encryption_password": str(password)}
    else:
        secrets.pop(udid, None)
    _save(SECRETS, secrets)


def forget(udid):
    set_password(udid, "")


def migrate():
    """Move passwords that older versions kept in devices.json (mode 0644) to
    the secrets file and tighten both files. Idempotent; values do not change.
    Returns the number of passwords moved."""
    moved = 0
    with locked():
        devices = _load(DEVICES)
        secrets = _load(SECRETS)
        for udid, dev in devices.items():
            if not isinstance(dev, dict) or "encryption_password" not in dev:
                continue
            password = str(dev.get("encryption_password") or "")
            # a password already in the secrets file is the newer one: keep it
            if password and not (secrets.get(udid) or {}).get("encryption_password"):
                secrets[udid] = {"encryption_password": password}
            moved += 1
        if moved or secrets or os.path.exists(_path(SECRETS)):
            _save(SECRETS, secrets)
        if moved or os.path.exists(_path(DEVICES)):
            save_devices(devices)
    return moved
