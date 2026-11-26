"""The SQLite state store, its import of the old JSON files, and the CLI around it."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from netboot_server import cli
from netboot_server.store import DB_NAME, SCHEMA_VERSION, Store

from helpers import make_image

MAC = "00:30:65:00:00:b2"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_database_has_the_current_schema(self):
        Store.open(self.data).close()
        db = sqlite3.connect(self.data / DB_NAME)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        db.close()

    def test_newer_schema_is_refused(self):
        db = sqlite3.connect(self.data / DB_NAME)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        db.close()
        with self.assertRaises(RuntimeError):
            Store.open(self.data)

    def test_update_client_changes_only_the_given_fields(self):
        store = Store()
        store.update_client(MAC.upper(), model="PowerMac2,2", arch="ppc")
        store.set_selected_image(MAC, 4)
        store.update_client(MAC, ip="192.168.50.50", last_message="DISCOVER")
        [row] = store.clients()
        self.assertEqual((row.mac, row.model, row.arch, row.ip, row.selected_image),
                         (MAC, "PowerMac2,2", "ppc", "192.168.50.50", 4))
        self.assertIsNotNone(row.first_seen)
        with self.assertRaises(TypeError):
            store.update_client(MAC, colour="blue")

    def test_leases(self):
        store = Store()
        store.save_lease(MAC, "10.0.0.5", 100.0)
        store.save_lease(MAC, "10.0.0.6", 200.0)
        self.assertEqual(store.leases(), {MAC: ("10.0.0.6", 200.0)})
        store.delete_lease(MAC)
        self.assertEqual(store.leases(), {})

    def test_imports_the_json_files_once(self):
        (self.data / "leases.json").write_text(json.dumps(
            {MAC: {"ip": "192.168.50.50", "expires": 1795652478.8}}))
        (self.data / "bsdp-bindings.json").write_text(json.dumps({MAC: 4, "00:05:02:00:00:a1": 5}))
        store = Store.open(self.data)
        lines = store.import_json(self.data)
        self.assertEqual([level for level, _ in lines], [20, 20])
        self.assertEqual(store.leases(), {MAC: ("192.168.50.50", 1795652478.8)})
        self.assertEqual(store.selected_image("00:05:02:00:00:a1"), 5)
        self.assertTrue((self.data / "leases.json.imported").exists())
        self.assertFalse((self.data / "bsdp-bindings.json").exists())
        self.assertEqual(store.import_json(self.data), [])
        store.close()

    def test_unreadable_json_is_left_in_place(self):
        (self.data / "bsdp-bindings.json").write_text("not json")
        store = Store.open(self.data)
        [(level, message)] = store.import_json(self.data)
        self.assertEqual(level, 30)
        self.assertIn("left in place", message)
        self.assertTrue((self.data / "bsdp-bindings.json").exists())
        store.close()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        make_image(self.data / "images", 1, "Tiger Install", install=True)
        make_image(self.data / "images", 4, "Jaguar Install", protocol="NFS", install=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data", str(self.data), *args])
        return code, out.getvalue(), err.getvalue()

    def test_set_image_and_list(self):
        store = Store.open(self.data)
        store.update_client(MAC, model="PowerMac2,2", arch="ppc", ip="192.168.50.50")
        store.close()
        code, out, _ = self.run_cli("set-image", "00-30-65-00-00-B2", "1")
        self.assertEqual(code, 0)
        self.assertIn("will boot 'Tiger Install' (index 1)", out)
        code, out, _ = self.run_cli("clients")
        self.assertIn("PowerMac2,2", out)
        self.assertIn("1 'Tiger Install'", out)
        self.run_cli("set-image", MAC, "default")
        self.assertIn("boots the default", self.run_cli("clients")[1])

    def test_set_image_rejects_unknown_images_and_addresses(self):
        code, _, err = self.run_cli("set-image", MAC, "9")
        self.assertEqual(code, 1)
        self.assertIn("no image with index 9", err)
        code, _, err = self.run_cli("set-image", "00:30:65", "1")
        self.assertEqual(code, 1)
        self.assertIn("not an Ethernet address", err)

    def test_set_image_for_a_mac_not_seen_yet(self):
        code, out, _ = self.run_cli("set-image", MAC, "4")
        self.assertEqual(code, 0)
        self.assertIn("hasn't been seen yet", out)


if __name__ == "__main__":
    unittest.main()
