"""Which image a Mac is offered when it doesn't ask for one."""

import tempfile
import unittest
from pathlib import Path

from simplenetinstallppc.catalog import Catalog
from simplenetinstallppc.events import EventBus
from simplenetinstallppc.server import _report_catalog

from helpers import make_image


class DefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # As on the Fedora host: the --default image is disabled, and the image
        # built last sorts first by folder name.
        make_image(self.root, 3, "OS X Puma", dir_name="img3")
        make_image(self.root, 4, "Jaguar Install", dir_name="jaguar-inst", IsEnabled=False)
        self.catalog = Catalog(self.root)
        self.catalog.default_index["ppc"] = 4

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, index, name, folder, **extra):
        make_image(self.root, index, name, dir_name=folder, **extra)
        self.catalog.reload()

    def test_new_build_does_not_take_over(self):
        self.catalog.reload()
        self.assertEqual(self.catalog.default_for("ppc", None).name, "OS X Puma")
        self.build(6, "Cheetah from dmg", "cheetah-dmg")
        self.assertEqual(self.catalog.default_for("ppc", None).name, "OS X Puma")

    def test_order_of_choice(self):
        self.build(6, "Cheetah from dmg", "cheetah-dmg", IsDefault=True)
        self.assertEqual(self.catalog.default_for("ppc", None).index, 6)    # IsDefault next
        self.catalog.default_index["ppc"] = 3
        self.assertEqual(self.catalog.default_for("ppc", None).index, 3)    # operator's pick first

    def test_unusable_default_is_reported(self):
        self.catalog.reload()
        self.assertEqual(self.catalog.unusable_default("ppc"), "image 4 'Jaguar Install' is disabled")
        self.catalog.default_index["ppc"] = 9
        self.assertEqual(self.catalog.unusable_default("ppc"), "there is no image 9")
        self.catalog.default_index["ppc"] = 3
        self.assertIsNone(self.catalog.unusable_default("ppc"))
        self.assertEqual(self.catalog.unusable_default("i386"), None)

        self.catalog.default_index["ppc"] = 4
        events = EventBus()
        _report_catalog(self.catalog, None, events)
        [warning] = [e for e in events.recent() if e.kind == "default"]
        self.assertEqual(warning.level, 30)
        self.assertIn("is disabled; using 'OS X Puma' instead", warning.message)


if __name__ == "__main__":
    unittest.main()
