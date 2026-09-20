#!/usr/bin/env python3
#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#
# imageroot/pypkg/devreg.py: migration of the device passwords out of
# devices.json. Plain Python, run from a checkout:
#   python3 tests/unit/test_devreg.py

import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "imageroot", "pypkg"))
import devreg  # noqa: E402


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AGENT_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write_legacy(self):
        path = os.path.join(self.tmp.name, "devices.json")
        with open(path, "w") as fp:
            json.dump({"A": {"name": "a", "encryption_password": "pw-a"}, "B": {"name": "b"},
                       "C": {"name": "c", "encryption_password": ""}}, fp)
        os.chmod(path, 0o644)

    def mode(self, name):
        return stat.S_IMODE(os.stat(os.path.join(self.tmp.name, name)).st_mode)

    def test_moves_passwords_and_tightens_the_files(self):
        self.write_legacy()
        self.assertEqual(devreg.migrate(), 2)
        self.assertEqual(devreg.get_password("A"), "pw-a")
        self.assertFalse(devreg.has_password("B"))
        self.assertFalse(devreg.has_password("C"))
        self.assertNotIn("encryption_password", open(os.path.join(self.tmp.name, "devices.json")).read())
        self.assertEqual(devreg.load_devices()["A"]["name"], "a")
        self.assertEqual(self.mode("devices.json"), 0o600)
        self.assertEqual(self.mode("device-secrets.json"), 0o600)

    def test_is_idempotent_and_keeps_a_newer_password(self):
        self.write_legacy()
        devreg.migrate()
        with devreg.locked():
            devreg.set_password("A", "newer")
        self.write_legacy()              # e.g. an old backup restored over the state
        devreg.migrate()
        self.assertEqual(devreg.get_password("A"), "newer")
        self.assertEqual(devreg.migrate(), 0)

    def test_nothing_to_do_on_a_fresh_instance(self):
        self.assertEqual(devreg.migrate(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "devices.json")))

    def test_save_never_writes_a_password(self):
        with devreg.locked():
            devreg.save_devices({"A": {"name": "a", "encryption_password": "leak"}})
        self.assertNotIn("leak", open(os.path.join(self.tmp.name, "devices.json")).read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
