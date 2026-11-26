"""The single-file build (packaging/build_pyz.py) runs on its own."""

import importlib.util
import os
import shutil
import signal
import socket
import time
import urllib.error
import urllib.request
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_builder():
    spec = importlib.util.spec_from_file_location("build_pyz", ROOT / "packaging" / "build_pyz.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PyzTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pyz = load_builder().build(Path(cls.tmp.name) / "simplenetinstallppc.pyz")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_pyz(self, *args, **env):
        environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        environment.update(env)
        return subprocess.run([sys.executable, *args], capture_output=True, text=True,
                              env=environment, cwd=self.tmp.name, timeout=60)

    def test_contents(self):
        with zipfile.ZipFile(self.pyz) as archive:
            names = archive.namelist()
        self.assertIn("__main__.py", names)
        self.assertIn("simplenetinstallppc/web/static/index.html", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
        self.assertTrue(self.pyz.read_bytes().startswith(b"#!/usr/bin/env python3\n"))
        self.assertTrue(os.access(self.pyz, os.X_OK))

    def test_runs_and_keeps_the_exit_status(self):
        result = self.run_pyz(str(self.pyz), "--version")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "0.1.0"))
        bad = self.run_pyz(str(self.pyz), "inspect", "/no/such/disc.iso")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("error:", bad.stderr)

    def test_page_and_build_runner_work_from_the_zip(self):
        # Builds run `python -m simplenetinstallppc` with PYTHONPATH set to the
        # package's parent, which is the .pyz itself here.
        code = ("from simplenetinstallppc.web import app, jobs; "
                "print(jobs.PACKAGE_PARENT); print(len(app.static_file('index.html') or b'')); "
                "print(app.static_file('missing.js'))")
        result = self.run_pyz("-c", code, PYTHONPATH=str(self.pyz))
        parent, size, missing = result.stdout.split()
        self.assertEqual(Path(parent), self.pyz.resolve())
        self.assertGreater(int(size), 1000)
        self.assertEqual(missing, "None")
        module = self.run_pyz("-m", "simplenetinstallppc", "--version", PYTHONPATH=parent)
        self.assertEqual(module.stdout.strip(), "0.1.0")

    def test_serve_survives_the_file_being_replaced(self):
        # serve runs from a private copy, so a rebuild, a copy over the file or
        # an unplugged USB stick doesn't break the running server.
        # It also keeps its data next to the original file (a portable folder on
        # the stick), not next to the private copy it runs from.
        tmp = Path(self.tmp.name)
        stick, temp = tmp / "stick", tmp / "temp"
        data = stick / "simplenetinstallppc-data"
        for folder in (stick, temp, data):
            folder.mkdir()
        pyz = stick / "simplenetinstallppc.pyz"
        shutil.copy2(self.pyz, pyz)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {k: v for k, v in os.environ.items()
                       if k not in ("PYTHONPATH", "NETBOOT_DATA")}
        environment["TMPDIR"] = str(temp)
        server = subprocess.Popen([sys.executable, str(pyz), "serve",
                                   "--ui-port", str(port), "--allow-sleep"],
                                  env=environment, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            url = f"http://127.0.0.1:{port}/login.html"
            for _ in range(100):
                try:
                    urllib.request.urlopen(url, timeout=1).read()
                    break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.1)
            [copy] = list(temp.glob("simplenetinstallppc-*/simplenetinstallppc.pyz"))
            pyz.unlink()                               # the stick is gone
            page = urllib.request.urlopen(url, timeout=5).read()
            self.assertIn(b"Log in", page)
            self.assertTrue((data / "ui-password").is_file())
            self.assertTrue((data / "images").is_dir())
        finally:
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=20)
        self.assertFalse(copy.parent.exists(), "the private copy is removed on exit")


if __name__ == "__main__":
    unittest.main()
