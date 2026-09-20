#!/usr/bin/env python3
#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tests of the backup orchestration in idevice-tool without an iPhone: the one
# function that talks to the device (attempt_backup) is replaced by a fake.
# Run inside the engine image:  python3 /opt/idevice-tests/test_backup_logic.py
# or from a checkout:           python3 device/tests/test_backup_logic.py

import argparse
import asyncio
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("IDEVICE_TOOL") or next(
    p for p in (os.path.join(HERE, "..", "idevice-tool"), "/usr/local/bin/idevice-tool") if os.path.exists(p))


def load_tool():
    loader = importlib.machinery.SourceFileLoader("idevice_tool_under_test", TOOL)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


UDID = "00008130-000000000000TEST"


def finish(snapdir):
    """What a device leaves behind when it completed a backup."""
    dev = os.path.join(snapdir, UDID)
    os.makedirs(dev, exist_ok=True)
    for name in ("Manifest.plist", "Manifest.db", "Status.plist"):
        with open(os.path.join(dev, name), "w") as fp:
            fp.write("x")


class Fake:
    """Scripted device: each entry is an exception to raise or None to succeed."""

    def __init__(self, script, connected=True):
        self.script = list(script)
        self.connected = connected
        self.calls = []

    async def __call__(self, args, snapdir, full, status, state):
        self.calls.append({"snapdir": snapdir, "full": full})
        step = self.script.pop(0)
        if self.connected:
            state["connected"] = True
        os.makedirs(os.path.join(snapdir, UDID), exist_ok=True)
        with open(os.path.join(snapdir, UDID, f"payload-{len(self.calls)}"), "w") as fp:
            fp.write("data")
        status.on_progress(40)
        if step is not None:
            raise step
        status.on_progress(100)
        finish(snapdir)


class BackupLogic(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, UDID)

    def tearDown(self):
        self.tmp.cleanup()

    def run_backup(self, fake, mode="full", attempts=3, retention=0):
        self.tool.attempt_backup = fake
        args = argparse.Namespace(udid=UDID, ip="192.0.2.1", base=self.base, dir="", mode=mode, incremental=False,
                                  set_password=False, attempts=attempts, retry_pause=0.01, retention=retention,
                                  progress_events=True)
        out = io.StringIO()
        error = None
        with redirect_stdout(out):
            try:
                asyncio.run(self.tool.cmd_backup(args))
            except Exception as exc:  # main() turns this into the final JSON line
                error = exc
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return events, error

    def status(self):
        with open(os.path.join(self.base, ".status.json")) as fp:
            return json.load(fp)

    def test_success_first_attempt(self):
        events, error = self.run_backup(Fake([None]))
        self.assertIsNone(error)
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(events[-1]["attempts_used"], 1)
        self.assertFalse(events[-1]["resumed"])
        snap = events[-1]["snapshot"]
        self.assertTrue(self.tool.is_complete(self.base, snap, UDID))
        self.assertFalse(os.path.exists(os.path.join(self.base, snap, ".incomplete")))
        self.assertEqual(self.status()["state"], "done")
        self.assertEqual(self.status()["percent"], 100)
        self.assertIn({"event": "progress", "percent": 40}, events)

    def test_connection_lost_is_retried_in_the_same_snapshot(self):
        fake = Fake([ConnectionResetError("Connection lost"), None])
        events, error = self.run_backup(fake)
        self.assertIsNone(error)
        self.assertEqual([e["event"] for e in events if "event" in e and e["event"] != "progress"],
                         ["attempt", "retry", "attempt"])
        self.assertEqual(events[-1]["attempts_used"], 2)
        self.assertEqual(fake.calls[0]["snapdir"], fake.calls[1]["snapdir"])
        self.assertTrue(fake.calls[0]["full"])
        self.assertFalse(fake.calls[1]["full"], "the second attempt must not force a full backup over the partial data")
        self.assertEqual(len(self.tool.snapshot_names(self.base)), 1)

    def test_gives_up_after_the_last_attempt_and_keeps_the_partial_snapshot(self):
        fake = Fake([ConnectionResetError("Connection lost")] * 3)
        events, error = self.run_backup(fake, attempts=3)
        self.assertIsInstance(error, ConnectionResetError)
        self.assertEqual(len(fake.calls), 3)
        self.assertIn("3 attempts", error.idevice_message)
        names = self.tool.snapshot_names(self.base)
        self.assertEqual(len(names), 1)
        self.assertFalse(self.tool.is_complete(self.base, names[0], UDID))
        self.assertTrue(os.path.exists(os.path.join(self.base, names[0], UDID, "payload-1")), "partial data is kept")
        self.assertEqual(self.status()["state"], "failed")

    def test_next_run_continues_the_unfinished_snapshot(self):
        self.run_backup(Fake([ConnectionResetError("Connection lost")], connected=True), attempts=1)
        first = self.tool.snapshot_names(self.base)
        fake = Fake([None])
        events, error = self.run_backup(fake)
        self.assertIsNone(error)
        self.assertEqual(self.tool.snapshot_names(self.base), first, "no second time stamp directory")
        self.assertTrue(events[-1]["resumed"])
        self.assertFalse(fake.calls[0]["full"])
        self.assertTrue(self.tool.is_complete(self.base, first[0], UDID))

    def test_unreachable_device_is_not_retried(self):
        fake = Fake([TimeoutError("timed out")], connected=False)
        events, error = self.run_backup(fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("could not reach the device", error.idevice_message)
        self.assertEqual(self.tool.snapshot_names(self.base), [], "no empty snapshot is left behind")

    def test_other_errors_are_not_retried(self):
        fake = Fake([RuntimeError("MBErrorDomain/207 wrong password")])
        events, error = self.run_backup(fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("207", error.idevice_message)
        disk_full = OSError(28, "No space left on device")
        self.assertFalse(self.tool.is_connection_error(disk_full))

    def test_retention_counts_finished_snapshots_only(self):
        for name in ("2026-01-01_00-00-00", "2026-01-02_00-00-00", "2026-01-03_00-00-00"):
            finish(os.path.join(self.base, name))
        os.makedirs(os.path.join(self.base, "2026-01-04_00-00-00", UDID))   # unfinished leftover
        finish(os.path.join(self.base, "incremental"))
        # the unfinished one is the newest: it is continued and becomes the 4th finished snapshot
        events, error = self.run_backup(Fake([None]), retention=2)
        self.assertIsNone(error)
        self.assertEqual(events[-1]["snapshot"], "2026-01-04_00-00-00")
        self.assertEqual(self.tool.snapshot_names(self.base),
                         ["2026-01-03_00-00-00", "2026-01-04_00-00-00", "incremental"])

    def test_incremental_mode_uses_one_directory_without_a_marker(self):
        fake = Fake([None])
        events, error = self.run_backup(fake, mode="incremental")
        self.assertIsNone(error)
        self.assertEqual(events[-1]["snapshot"], "incremental")
        self.assertFalse(fake.calls[0]["full"])
        # a failed update leaves the last finished state in place and restorable
        self.run_backup(Fake([ConnectionResetError("Connection lost")]), mode="incremental", attempts=1)
        self.assertTrue(self.tool.is_complete(self.base, "incremental", UDID))

    def test_second_backup_of_the_same_device_is_refused(self):
        import fcntl
        os.makedirs(self.base)
        with open(os.path.join(self.base, ".lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            events, error = self.run_backup(Fake([None]))
        self.assertEqual(type(error).__name__, "AlreadyRunning")

    def test_stale_running_status_reads_as_idle(self):
        os.makedirs(self.base)
        with open(os.path.join(self.base, ".status.json"), "w") as fp:
            json.dump({"state": "running", "percent": 55, "updated": 1}, fp)
        self.assertEqual(self.tool.read_status(self.base)["state"], "idle")


if __name__ == "__main__":
    unittest.main(verbosity=2)
