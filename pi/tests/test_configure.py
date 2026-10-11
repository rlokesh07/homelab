import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kodi"))
import configure  # noqa: E402

ESTUARY_HOME = os.environ.get("ESTUARY_HOME")


@unittest.skipUnless(ESTUARY_HOME, "set ESTUARY_HOME to Estuary's xml/Home.xml")
class TrimHomeTest(unittest.TestCase):
    def setUp(self):
        self.tree = ET.parse(ESTUARY_HOME)
        configure.trim_home(self.tree)

    def test_menu_only_has_movies_tv_and_doorbell(self):
        menu = self.tree.find(".//control[@id='9000']/content")
        ids = [item.find("property[@name='id']").text for item in menu]
        self.assertEqual(ids, ["movies", "tvshows", "pictures"])

    def test_panels_for_removed_entries_are_gone(self):
        shown_for = set()
        for visible in self.tree.iter("visible"):
            match = configure.MENU_ITEM_ID.fullmatch((visible.text or "").strip())
            if match:
                shown_for.add(match.group(1))
        self.assertEqual(shown_for, {"movies", "tvshows", "pictures"})

    def test_untrimmed_file_has_other_entries(self):
        text = Path(ESTUARY_HOME).read_text()
        self.assertTrue({"music", "pictures", "weather"} <= set(re.findall(r'"id">(\w+)<', text)))


@unittest.skipUnless(ESTUARY_HOME, "set ESTUARY_HOME to Estuary's xml/Home.xml")
class DoorbellTabTest(unittest.TestCase):
    def test_pictures_tab_renamed_and_opens_archive(self):
        tree = ET.parse(ESTUARY_HOME)
        configure.trim_home(tree)
        configure.label_doorbell_tab(tree)
        menu = tree.find(".//control[@id='9000']/content")
        pictures = next(
            item for item in menu if item.find("property[@name='id']").text == "pictures"
        )
        self.assertEqual(pictures.find("label").text, "Doorbell")
        onclick = pictures.find("onclick").text
        self.assertIn(configure.DOORBELL_PHOTOS, onclick)
        self.assertTrue(onclick.startswith("ActivateWindow(Pictures,"))


class GuiSettingsTest(unittest.TestCase):
    def test_overrides_defaults_and_adds_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guisettings.xml"
            path.write_text(
                '<settings version="2">'
                '<setting id="services.zeroconf" default="true">true</setting>'
                "</settings>"
            )
            configure.set_gui_settings(configure.GUI_SETTINGS, path)
            root = ET.parse(path).getroot()

        zeroconf = root.find("setting[@id='services.zeroconf']")
        self.assertEqual(zeroconf.text, "false")
        self.assertNotIn("default", zeroconf.attrib)
        self.assertEqual(root.find("setting[@id='lookandfeel.skin']").text, configure.SKIN_ID)


class WebPasswordTest(unittest.TestCase):
    def test_generated_once_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "homelab" / "kodi-web-password"
            first = configure.web_password(path)
            self.assertGreaterEqual(len(first), 16)
            self.assertEqual(configure.web_password(path), first)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
