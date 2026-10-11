#!/usr/bin/env python3
"""Read-only checks of the selected build branding and the actual packaged assets."""
import argparse
import ast
import configparser
import io
import json
from pathlib import Path
import plistlib
import re
import subprocess
import tarfile
import zipfile

REPO = Path(__file__).resolve().parents[1]
BRAND = "Firefox Reverse"
BRAND_DIR = "browser/branding/unofficial"
CONTENT = (
    "about.png", "about-logo.png", "about-logo@2x.png", "about-logo.svg",
    "about-logo-private.png", "about-logo-private@2x.png",
    "about-wordmark.svg", "firefox-wordmark.svg",
)
FTL_KEYS = ("-brand-shorter-name", "-brand-short-name", "-brand-shortcut-name",
            "-brand-full-name", "-brand-product-name")
PROPERTY_KEYS = ("brandShorterName", "brandShortName", "brandFullName")


def require(condition, message):
    if not condition:
        raise ValueError("branding check failed: " + message)


def expected(relative, revision=None):
    name = f"additions/{BRAND_DIR}/{relative}"
    if revision:
        require(re.fullmatch(r"[0-9a-f]{7,40}", revision), "source revision must be a commit hash")
        return subprocess.check_output(["git", "-C", str(REPO), "show", f"{revision}:{name}"])
    return (REPO / name).read_bytes()


def verify_object(objdir):
    # config.status is executable Python. Parse its literal data; never execute it.
    tree = ast.parse((Path(objdir) / "config.status").read_text(encoding="utf-8"))
    values = [node.value for node in tree.body
              if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "substs" for target in node.targets)]
    require(len(values) == 1 and isinstance(values[0], ast.Dict), "missing configured substitutions")
    wanted = {"MOZ_BRANDING_DIRECTORY", "MOZ_APP_DISPLAYNAME", "MOZ_OFFICIAL_BRANDING",
              "MOZ_MACBUNDLE_ID", "MOZ_WIDGET_TOOLKIT", "OS_TARGET"}
    config = {}
    for key, value in zip(values[0].keys, values[0].values):
        name = ast.literal_eval(key)
        if name in wanted:
            require(name not in config, f"duplicate configured field: {name}")
            config[name] = ast.literal_eval(value)
    require(config.get("MOZ_BRANDING_DIRECTORY") == BRAND_DIR, "wrong active MOZ_BRANDING_DIRECTORY; reconfigure with --with-branding=" + BRAND_DIR)
    require(config.get("MOZ_APP_DISPLAYNAME") == BRAND, "stale MOZ_APP_DISPLAYNAME; reconfigure after applying branding")
    require(not config.get("MOZ_OFFICIAL_BRANDING"), "official branding must not be enabled")
    if config.get("MOZ_WIDGET_TOOLKIT") == "cocoa" or config.get("OS_TARGET") == "Darwin":
        require(config.get("MOZ_MACBUNDLE_ID") == "org.mozilla.firefox-reverse", "unexpected macOS bundle identity")
    return {"activeBranding": BRAND_DIR, "displayName": BRAND}


def verify_names(data, keys, location):
    text = data.decode("utf-8").replace("\r\n", "\n")
    for key in keys:
        matches = re.findall(rf"^{re.escape(key)}\s*=[ \t]*(.*?)[ \t]*$", text, re.MULTILINE)
        require(matches == [BRAND], f"{location}: {key} is not {BRAND}")


class Package:
    def __init__(self, filename):
        self.path = Path(filename)
        self.archive = None
        self.mac = False
        if self.path.is_dir():
            if (self.path / "Contents/Info.plist").is_file():
                self.mac = True
            elif not (self.path / "browser/omni.ja").is_file():
                apps = list(self.path.glob("*.app"))
                require(len(apps) == 1, "stage must contain exactly one app or browser/omni.ja")
                self.path = apps[0]
                self.mac = True
            self.prefix = ""
        else:
            if zipfile.is_zipfile(self.path):
                self.archive = zipfile.ZipFile(self.path)
                names = self.archive.namelist()
            else:
                self.archive = tarfile.open(self.path, "r:*")
                names = self.archive.getnames()
            require(len(names) == len(set(names)), "duplicate archive members")
            apps = [name[:-len("Contents/Info.plist")] for name in names if name.endswith(".app/Contents/Info.plist") and "/Contents/" not in name.split(".app/Contents/Info.plist")[0]]
            self.mac = bool(apps)
            prefixes = apps if apps else [name[:-len("application.ini")] for name in names if name in ("application.ini", "firefox/application.ini")]
            require(len(prefixes) == 1, "cannot identify one packaged browser")
            self.prefix = prefixes[0]

    def read(self, relative):
        name = self.prefix + relative
        if isinstance(self.archive, zipfile.ZipFile):
            return self.archive.read(name)
        if self.archive:
            entry = self.archive.getmember(name)
            require(entry.isfile(), f"not a regular package member: {name}")
            with self.archive.extractfile(entry) as stream:
                return stream.read()
        return (self.path / relative).read_bytes()

    def close(self):
        if self.archive:
            self.archive.close()


def verify_package(filename, revision=None):
    package = Package(filename)
    try:
        resources = "Contents/Resources/" if package.mac else ""
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_string(package.read(resources + "application.ini").decode("utf-8"))
        require(ini.get("App", "CodeName", fallback=None) == BRAND, "application.ini has the wrong display name")
        checked = []
        with zipfile.ZipFile(io.BytesIO(package.read(resources + "browser/omni.ja"))) as omni:
            names = omni.namelist()
            require(len(names) == len(set(names)), "duplicate browser omni members")
            manifest = omni.read("chrome/chrome.manifest").decode("utf-8")
            mappings = [line.split() for line in manifest.splitlines() if line.startswith("content branding ")]
            require(len(mappings) == 1 and mappings[0][2] == "browser/content/branding/", "chrome://branding points to unexpected resources")
            require(not re.search(r"^override\s+chrome://branding/", manifest, re.MULTILINE), "branding URL override is active")
            for name in CONTENT:
                require(omni.read("chrome/browser/content/branding/" + name) == expected("content/" + name, revision), f"packaged branding asset differs: {name}")
                checked.append(name)
            for size in (16, 32, 48, 64, 128):
                name = f"icon{size}.png"
                require(omni.read("chrome/browser/content/branding/" + name) == expected(f"default{size}.png", revision), f"packaged branding asset differs: {name}")
                checked.append(name)
            locales = [name for name in names if re.fullmatch(r"localization/[^/]+/branding/brand.ftl", name)]
            properties = [name for name in names if re.fullmatch(r"chrome/[^/]+/locale/branding/brand.properties", name)]
            require("localization/en-US/branding/brand.ftl" in locales, "missing en-US brand terms")
            require("chrome/en-US/locale/branding/brand.properties" in properties, "missing en-US brand properties")
            for name in locales:
                verify_names(omni.read(name), FTL_KEYS, name)
            for name in properties:
                verify_names(omni.read(name), PROPERTY_KEYS, name)
        if package.mac:
            info = plistlib.loads(package.read("Contents/Info.plist"))
            require(info.get("CFBundleName") == BRAND, "macOS CFBundleName is not Firefox Reverse")
            require(info.get("CFBundleDisplayName", BRAND) == BRAND, "macOS CFBundleDisplayName overrides the brand")
            require(info.get("CFBundleIdentifier") == "org.mozilla.firefox-reverse", "macOS bundle identity changed")
            require(info.get("CFBundleIconFile") == "firefox.icns" and not info.get("CFBundleIconName"), "macOS asset-catalog icon overrides firefox.icns")
            require(package.read(resources + "firefox.icns") == expected("firefox.icns", revision), "macOS app icon differs")
        return {"displayName": BRAND, "assetsChecked": len(checked), "brandLocales": sorted(locales), "macOSAppChecked": package.mac}
    finally:
        package.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objdir", type=Path, help="Configured object directory")
    parser.add_argument("--package", type=Path, help="App, dist/firefox stage, ZIP or tar archive")
    parser.add_argument("--source-revision", help="Expected asset commit for candidate verification")
    args = parser.parse_args()
    if not args.objdir and not args.package:
        parser.error("--objdir or --package is required")
    try:
        report = {}
        if args.objdir:
            report["configuration"] = verify_object(args.objdir)
        if args.package:
            report["package"] = verify_package(args.package, args.source_revision)
        print(json.dumps({"branding": "passed", **report}))
    except (ValueError, OSError, KeyError, SyntaxError, configparser.Error, zipfile.BadZipFile, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
