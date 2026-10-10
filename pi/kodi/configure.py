#!/usr/bin/env python3
"""Sets Kodi up as a movies + TV shows box. Run as the Kodi user while Kodi is stopped."""
import re
import secrets
import shutil
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

KODI = Path.home() / ".kodi"
USERDATA = KODI / "userdata"
SYSTEM_SKIN = Path("/usr/share/kodi/addons/skin.estuary")
SKIN_ID = "skin.estuary.tv"
SKIN_DIR = KODI / "addons" / SKIN_ID
KEEP_MENU = {"movies", "tvshows"}

USB_DRIVE = "/srv/media"
STORAGE = "/mnt/storage"
MOVIES = ("movies", "metadata.themoviedb.org.python", 2147483647)
TV_SHOWS = ("tvshows", "metadata.tvshows.themoviedb.org.python", 0)
SOURCES = {
    "Movies": (f"{USB_DRIVE}/Movies/", *MOVIES),
    "TV Shows": (f"{USB_DRIVE}/TV Shows/", *TV_SHOWS),
    "Movies (storage)": (f"{STORAGE}/Movies/", *MOVIES),
    "TV Shows (storage)": (f"{STORAGE}/TV Shows/", *TV_SHOWS),
}

GUI_SETTINGS = {
    "lookandfeel.skin": SKIN_ID,
    "videolibrary.updateonstartup": "true",
    "gamesgeneral.enable": "false",
    "services.zeroconf": "false",
    "services.esenabled": "false",
    "services.wsdiscovery": "false",
    # JSON-RPC over HTTP, used by the 3DS remote.
    "services.webserver": "true",
    "services.webserverport": "8080",
    "services.webserverauthentication": "true",
    "services.webserverusername": "kodi",
}
# Generated on the Pi so the password stays out of this (public) repo.
WEB_PASSWORD = Path.home() / ".config" / "homelab" / "kodi-web-password"

# Downloaded copies in ~/.kodi/addons. Kodi's built-in add-ons can't be disabled (it re-enables
# everything in addon-manifest.xml), but they only load when their feature is used.
MUSIC_SCRAPERS = [
    "metadata.album.universal",
    "metadata.artists.universal",
    "metadata.generic.albums",
    "metadata.generic.artists",
]
MENU_ITEM_ID = re.compile(r"String\.IsEqual\(Container\(9000\)\.ListItem\.Property\(id\),(\w+)\)")


def build_skin():
    shutil.rmtree(SKIN_DIR, ignore_errors=True)
    shutil.copytree(SYSTEM_SKIN, SKIN_DIR, ignore_dangling_symlinks=True)

    addon_xml = SKIN_DIR / "addon.xml"
    text = addon_xml.read_text()
    text = text.replace('id="skin.estuary"', f'id="{SKIN_ID}"', 1)
    text = text.replace('name="Estuary"', 'name="Estuary TV"', 1)
    addon_xml.write_text(text)

    home_xml = SKIN_DIR / "xml" / "Home.xml"
    tree = ET.parse(home_xml)
    trim_home(tree)
    tree.write(home_xml, encoding="UTF-8", xml_declaration=True)


def trim_home(tree):
    """Deletes every home menu entry, and the panel it shows, except those in KEEP_MENU."""
    parents = {child: parent for parent in tree.iter() for child in parent}

    menu = tree.find(".//control[@id='9000']/content")
    for item in list(menu):
        prop = item.find("property[@name='id']")
        if prop is None or prop.text not in KEEP_MENU:
            menu.remove(item)

    for group in list(tree.iter("control")):
        visible = group.find("visible")
        match = MENU_ITEM_ID.fullmatch((visible.text or "").strip()) if visible is not None else None
        if match and match.group(1) not in KEEP_MENU and group in parents:
            parents[group].remove(group)


def web_password(path=WEB_PASSWORD):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.write_text(secrets.token_urlsafe(16) + "\n")
    return path.read_text().strip()


def set_gui_settings(settings, path=USERDATA / "guisettings.xml"):
    tree = ET.parse(path)
    root = tree.getroot()
    for key, value in settings.items():
        setting = root.find(f"setting[@id='{key}']")
        if setting is None:
            setting = ET.SubElement(root, "setting", id=key)
        setting.attrib.pop("default", None)
        setting.text = value
    tree.write(path, encoding="UTF-8", xml_declaration=True)


def write_sources():
    root = ET.Element("sources")
    video = ET.SubElement(root, "video")
    ET.SubElement(video, "default", pathversion="1")
    for name, (path, *_rest) in SOURCES.items():
        source = ET.SubElement(video, "source")
        ET.SubElement(source, "name").text = name
        ET.SubElement(source, "path", pathversion="1").text = path
        ET.SubElement(source, "allowsharing").text = "true"
    ET.indent(root)
    ET.ElementTree(root).write(USERDATA / "sources.xml", encoding="UTF-8", xml_declaration=True)


def latest_db(prefix):
    return max((USERDATA / "Database").glob(f"{prefix}*.db"), key=lambda p: int(p.stem[len(prefix):]))


def set_library_content():
    with sqlite3.connect(latest_db("MyVideos")) as db:
        for path, content, scraper, recursive in SOURCES.values():
            if db.execute("select 1 from path where strPath = ?", (path,)).fetchone():
                db.execute(
                    "update path set strContent = ?, strScraper = ?, scanRecursive = ? where strPath = ?",
                    (content, scraper, recursive, path),
                )
            else:
                db.execute(
                    "insert into path (strPath, strContent, strScraper, scanRecursive, useFolderNames,"
                    " strSettings, noUpdate, exclude, allAudio, dateAdded)"
                    " values (?, ?, ?, ?, 0, '', 0, 0, 0, datetime('now'))",
                    (path, content, scraper, recursive),
                )


def set_addons():
    with sqlite3.connect(latest_db("Addons")) as db:
        origin = db.execute("select origin from installed where addonID = 'skin.estuary'").fetchone()
        db.execute(
            "insert into installed (addonID, enabled, installDate, origin) values (?, 1, datetime('now'), ?)"
            " on conflict(addonID) do update set enabled = 1",
            (SKIN_ID, origin[0] if origin else ""),
        )

    for addon in MUSIC_SCRAPERS:
        shutil.rmtree(KODI / "addons" / addon, ignore_errors=True)


if __name__ == "__main__":
    build_skin()
    set_gui_settings({**GUI_SETTINGS, "services.webserverpassword": web_password()})
    write_sources()
    set_library_content()
    set_addons()
    print("kodi configured")
