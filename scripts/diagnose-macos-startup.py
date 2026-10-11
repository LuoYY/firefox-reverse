#!/usr/bin/env python3
"""Read-only macOS startup checks. Never edits a profile or deletes a lock."""
import argparse
import configparser
import json
import os
from pathlib import Path
import plistlib
import platform
import subprocess


def path_state(target):
    return {"directoryExists": target.is_dir(), "readable": os.access(target, os.R_OK), "writable": os.access(target, os.W_OK), "parentDirectoryExists": target.parent.is_dir()}


def startup_environment(environment):
    # Only record presence; inherited paths/tokens may contain private data.
    return {key: bool(environment.get(key)) for key in (
        "XRE_PROFILE_PATH", "XRE_PROFILE_LOCAL_PATH", "XRE_PROFILE_NAME",
        "MOZ_NEW_INSTANCE", "MOZ_FRX_ENV_ID", "MOZ_FRX_FINGERPRINT_CONFIG",
    )}


def profile_registry(root):
    root = Path(root)
    report = {"registryExists": (root / "profiles.ini").is_file(), "profiles": [], "installs": []}
    for filename in ("profiles.ini", "installs.ini"):
        parser = configparser.RawConfigParser(strict=False)
        try:
            with (root / filename).open(encoding="utf-8-sig") as stream:
                parser.read_file(stream)
        except FileNotFoundError:
            continue
        except (OSError, configparser.Error, UnicodeError) as error:
            report[filename + "Error"] = type(error).__name__
            continue
        for section in parser.sections():
            item = parser[section]
            if filename == "profiles.ini" and section.startswith("Profile"):
                value = item.get("Path", "")
                target = root / value if item.get("IsRelative", "1") == "1" else Path(value)
                state = path_state(target) if value else {key: False for key in ("directoryExists", "readable", "writable", "parentDirectoryExists")}
                report["profiles"].append({"entry": section, "default": item.get("Default") == "1", "pathPresent": bool(value), **state})
            elif item.get("Default"):
                target = root / item["Default"]
                # Install hashes and personal paths are deliberately omitted.
                report["installs"].append({"source": filename, **path_state(target)})
    return report


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        return {"exitCode": result.returncode, "output": (result.stdout + result.stderr)[-3000:].replace(str(Path.home()), "$HOME")}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": type(error).__name__}


def diagnose(app, registry):
    app = Path(app)
    report = {"system": platform.system(), "systemVersion": platform.mac_ver()[0], "architecture": platform.machine(), "appExists": app.is_dir(), "checks": [], "profileRegistry": profile_registry(registry), "shellEnvironmentPresence": startup_environment(os.environ)}
    try:
        with (app / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        report["application"] = {key: info.get(key) for key in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion", "LSMinimumSystemVersion")}
    except (OSError, plistlib.InvalidFileException):
        report["checks"].append("application-plist-missing-or-invalid")
    for relative in ("Contents/MacOS/firefox", "Contents/MacOS/XUL", "Contents/Resources/application.ini", "Contents/Resources/browser/omni.ja"):
        if not (app / relative).is_file():
            report["checks"].append("missing-application-file:" + relative)
    registry_report = report["profileRegistry"]
    if any(not entry["directoryExists"] for entry in registry_report["installs"]):
        report["checks"].append("installation-default-points-to-missing-profile-directory")
    if any(entry["directoryExists"] and not entry["writable"] for entry in registry_report["installs"]):
        report["checks"].append("installation-default-profile-not-writable")
    if any(not entry["directoryExists"] or not entry["writable"] for entry in registry_report["profiles"] if entry["default"]):
        report["checks"].append("default-profile-directory-missing-or-not-writable")
    if platform.system() == "Darwin" and app.is_dir():
        report["signature"] = command(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
        report["quarantine"] = command(["/usr/bin/xattr", "-p", "com.apple.quarantine", str(app)])
    report["note"] = "Missing Firefox profile means a user-data directory, not fingerprint.json. This report is read-only and cannot establish macOS 27 compatibility. Shell environment is not necessarily Finder's environment. Review before sharing."
    report["startupFlags"] = "The pinned Firefox 153 source ignores -no-remote. Use -new-instance for an intentional separate instance; it does not bypass profile locking or repair missing data. Do not delete locks or profile registries as a workaround."
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default="/Applications/Firefox Reverse.app")
    parser.add_argument("--registry", default=str(Path.home() / "Library/Application Support/Firefox"))
    parser.add_argument("--output", help="Optional report destination; only this file will be written")
    args = parser.parse_args()
    text = json.dumps(diagnose(args.app, args.registry), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
