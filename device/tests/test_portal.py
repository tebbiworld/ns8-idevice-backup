#!/usr/bin/env python3
#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tests of the self-service portal that need no directory and no iPhone:
# progress endpoint, ownership, unfinished snapshots, device passwords.
# Needs flask and ldap3, so it runs inside the engine image:
#   python3 /opt/idevice-tests/test_portal.py

import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = next(p for p in (os.path.join(HERE, "..", "web-app.py"), "/opt/web/app.py") if os.path.exists(p))
TOOL = next(p for p in (os.path.join(HERE, "..", "idevice-tool"), "/usr/local/bin/idevice-tool") if os.path.exists(p))

ELA = "00008130-0000000000000ELA"
BOB = "00008130-0000000000000BOB"
SECRET = "correct horse battery"


def finish(snapdir, udid):
    dev = os.path.join(snapdir, udid)
    os.makedirs(dev, exist_ok=True)
    for name in ("Manifest.plist", "Manifest.db", "Status.plist"):
        with open(os.path.join(dev, name), "w") as fp:
            fp.write("x")


class Portal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = os.path.join(cls.tmp.name, "state")
        cls.backups = os.path.join(cls.tmp.name, "backups")
        os.makedirs(cls.state)
        os.makedirs(cls.backups)
        os.environ.update(STATE_DIR=cls.state, BACKUP_ROOT=cls.backups, IDEVICE_TOOL=TOOL, SESSION_SECRET="test")
        loader = importlib.machinery.SourceFileLoader("portal_under_test", APP)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.app = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.app)
        cls.app.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        devices = {
            ELA: {"udid": ELA, "name": "Elas iPhone", "ip": "192.0.2.10", "owner": "ela", "backup_mode": "full",
                  "last_status": "failed", "last_error": "Connection lost"},
            BOB: {"udid": BOB, "name": "Bobs iPad", "ip": "192.0.2.11", "owner": "bob", "backup_mode": "full"},
        }
        with self.app.registry_locked():
            self.app.save_devices(devices)
            self.app.set_device_password(ELA, SECRET)
        for udid in (ELA, BOB):
            base = os.path.join(self.backups, udid)
            os.makedirs(base, exist_ok=True)
            for name in os.listdir(base):
                if name.startswith(".status"):
                    os.remove(os.path.join(base, name))

    def client(self, uid=None):
        c = self.app.app.test_client()
        if uid:
            with c.session_transaction() as s:
                s["uid"] = uid
                s["display"] = uid
                s["lang"] = "en"
        return c

    def set_status(self, udid, **fields):
        fields.setdefault("updated", int(time.time()))
        with open(os.path.join(self.backups, udid, ".status.json"), "w") as fp:
            json.dump(fields, fp)

    def test_progress_needs_a_login(self):
        self.assertEqual(self.client().get("/progress.json").status_code, 401)

    def test_progress_of_own_devices_only(self):
        self.set_status(ELA, state="running", percent=42, attempt=1, attempts=3)
        self.set_status(BOB, state="running", percent=7, attempt=1, attempts=3)
        data = self.client("ela").get("/progress.json").get_json()["devices"]
        self.assertEqual(list(data), [ELA])
        self.assertTrue(data[ELA]["running"])
        self.assertEqual(data[ELA]["percent"], 42)
        self.assertIn("42 %", data[ELA]["html"])
        self.assertIn("progressbar", data[ELA]["html"])

    def test_waiting_and_attempt_are_shown(self):
        self.set_status(ELA, state="waiting", percent=42, attempt=1, attempts=3)
        html = self.client("ela").get("/progress.json").get_json()["devices"][ELA]["html"]
        self.assertIn("2 of 3", html)
        self.set_status(ELA, state="running", percent=5, attempt=2, attempts=3, resumed=True)
        html = self.client("ela").get("/progress.json").get_json()["devices"][ELA]["html"]
        self.assertIn("attempt 2 of 3", html)
        self.assertIn("continuing", html)

    def test_dead_run_is_not_shown_as_running(self):
        self.set_status(ELA, state="running", percent=42, updated=1)
        data = self.client("ela").get("/progress.json").get_json()["devices"][ELA]
        self.assertFalse(data["running"])
        self.assertIn("failed", data["html"])

    def test_device_page_shows_percentage_instead_of_a_plain_running(self):
        self.set_status(ELA, state="running", percent=63, attempt=1, attempts=3)
        page = self.client("ela").get("/").get_data(as_text=True)
        self.assertIn("Backing up: 63 %", page)
        self.assertIn("/progress.json", page)
        self.assertNotIn("Bobs iPad", page)

    def test_unfinished_snapshot_is_kept_but_not_restorable(self):
        base = os.path.join(self.backups, ELA)
        finish(os.path.join(base, "2026-01-01_00-00-00"), ELA)
        os.makedirs(os.path.join(base, "2026-01-02_00-00-00", ELA))
        with open(os.path.join(base, "2026-01-02_00-00-00", ".incomplete"), "w") as fp:
            fp.write("{}")
        dev = [d for d in self.app.my_devices("ela")][0]
        self.assertEqual(dev["snapshots"], ["2026-01-01_00-00-00"])
        self.assertTrue(dev["unfinished"])
        c = self.client("ela")
        with c.session_transaction() as s:
            s["csrf"] = "tok"
        r = c.post(f"/devices/{ELA}/restore", data={"csrf": "tok", "snapshot": "2026-01-02_00-00-00"})
        self.assertEqual(r.status_code, 404)

    def test_passwords_stay_out_of_registry_pages_and_api(self):
        registry = open(os.path.join(self.state, "devices.json")).read()
        self.assertNotIn(SECRET, registry)
        self.assertNotIn("encryption_password", registry)
        for name in ("devices.json", "device-secrets.json"):
            mode = stat.S_IMODE(os.stat(os.path.join(self.state, name)).st_mode)
            self.assertEqual(mode, 0o600, name)
        self.assertEqual(self.app.get_device_password(ELA), SECRET)
        c = self.client("ela")
        self.assertNotIn(SECRET, c.get("/").get_data(as_text=True))
        self.assertNotIn(SECRET, c.get("/progress.json").get_data(as_text=True))
        dev = self.app.my_devices("ela")[0]
        self.assertNotIn("encryption_password", dev)
        self.assertTrue(dev["password_set"])

    def test_a_legacy_password_field_is_dropped_on_save(self):
        with self.app.registry_locked():
            devices = self.app.load_devices()
            devices[BOB]["encryption_password"] = "legacy"
            self.app.save_devices(devices)
        self.assertNotIn("legacy", open(os.path.join(self.state, "devices.json")).read())

    def fake_tool(self, lines, rc):
        """Put a scripted `idevice-tool` first in PATH; it records how it was called."""
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        path = os.path.join(bindir, "idevice-tool")
        with open(path, "w") as fp:
            fp.write("#!/bin/sh\n")
            fp.write(f'echo "$@" > {self.tmp.name}/tool-args\n')
            fp.write(f'[ -n "$IDEVICE_SET_PASSWORD" ] && echo env > {self.tmp.name}/tool-pw || echo none > {self.tmp.name}/tool-pw\n')
            for line in lines:
                fp.write("echo '" + json.dumps(line) + "'\n")
            fp.write(f"exit {rc}\n")
        os.chmod(path, 0o755)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]

    def test_backup_job_records_attempts_and_result(self):
        self.fake_tool([
            {"event": "attempt", "attempt": 1, "attempts": 3, "snapshot": "S1", "resumed": False},
            {"event": "retry", "attempt": 1, "attempts": 3, "wait": 60, "type": "ConnectionResetError", "error": "Connection lost"},
            {"event": "attempt", "attempt": 2, "attempts": 3, "snapshot": "S1", "resumed": True},
            {"ok": True, "snapshot": "S1", "attempts_used": 2, "device_encrypted": True},
        ], 0)
        self.app._do_backup(ELA, "192.0.2.10", False, SECRET, True, uid="ela")
        dev = self.app.load_devices()[ELA]
        self.assertEqual(dev["last_status"], "ok")
        self.assertTrue(dev["encryption"])
        args = open(os.path.join(self.tmp.name, "tool-args")).read()
        self.assertIn("--base " + os.path.join(self.backups, ELA), args)
        self.assertIn("--mode full", args)
        self.assertNotIn(SECRET, args, "the password must not be on the command line")
        self.assertEqual(open(os.path.join(self.tmp.name, "tool-pw")).read().strip(), "env")
        audit = open(os.path.join(self.state, "audit.log")).read()
        self.assertEqual(audit.count("action=backup_attempt"), 2)
        self.assertEqual(audit.count("action=backup_retry"), 1)
        self.assertIn("action=backup_done udid=" + ELA + " result=ok attempts=2", audit.replace("user=ela ", ""))
        self.assertNotIn(SECRET, audit)

    def test_failed_backup_job_keeps_the_message(self):
        self.fake_tool([
            {"event": "attempt", "attempt": 1, "attempts": 3, "snapshot": "S1", "resumed": False},
            {"ok": False, "type": "ConnectionResetError", "error": "the connection to the device was lost"},
        ], 1)
        self.app._do_backup(ELA, "192.0.2.10", True, "", False, uid="ela")
        dev = self.app.load_devices()[ELA]
        self.assertEqual(dev["last_status"], "failed")
        self.assertIn("connection to the device was lost", dev["last_error"])
        self.assertIn("--mode incremental", open(os.path.join(self.tmp.name, "tool-args")).read())

    def test_removing_a_device_removes_its_password(self):
        c = self.client("ela")
        with c.session_transaction() as s:
            s["csrf"] = "tok"
        c.post(f"/devices/{ELA}/delete", data={"csrf": "tok"})
        self.assertEqual(self.app.get_device_password(ELA), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
