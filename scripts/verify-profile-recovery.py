#!/usr/bin/env python3
"""Exercise startup recovery in an isolated application profile namespace.

Linux requires a display (use xvfb-run -a). macOS opens temporary dialogs.
Does not change the user's Firefox registry, profiles, locks or installation.
"""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time
import uuid


def probe(binary, mode, recovery_supported):
    with tempfile.TemporaryDirectory(prefix="frx-startup-fixture-") as temporary:
        root = Path(temporary)
        namespace = "frx-startup-fixture-" + uuid.uuid4().hex
        mac = platform.system() == "Darwin"
        registry = Path.home() / "Library/Application Support" / namespace if mac else root / "home" / ("." + namespace)
        cache = Path.home() / "Library/Caches" / namespace if mac else root / "home/.cache" / namespace
        registry.mkdir(parents=True)
        resource = binary.parent.parent / "Resources" if mac else binary.parent
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        assert config.read(resource / "application.ini"), "application.ini unavailable"
        config["App"]["Profile"] = namespace
        appdir = root / "browser"
        appdir.mkdir()
        for item in (resource / "browser").iterdir():
            if item.name != "application.ini":
                (appdir / item.name).symlink_to(item, target_is_directory=item.is_dir())
        appfile = appdir / "application.ini"
        with appfile.open("w") as stream:
            config.write(stream, space_around_delimiters=False)
        missing = registry / "Profiles/missing"
        original = "[General]\nStartWithLastProfile=1\nVersion=2\n[Profile0]\nName=fixture-missing\nIsRelative=1\nPath=Profiles/missing\nDefault=1\n"
        (registry / "profiles.ini").write_text(original)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("MOZ_FRX_", "FRX_ENV_", "XRE_PROFILE_", "MOZ_HEADLESS", "MOZ_APP_FILE"))}
        env.update(HOME=str(root / "home"), MOZ_LEGACY_PROFILES="1")
        (root / "home").mkdir(exist_ok=True)
        command = [str(binary), "-app", str(appfile), "-new-instance"]
        if mode == "not-directory":
            missing.parent.mkdir(parents=True)
            missing.write_text("do-not-replace")
        elif mode == "explicit":
            missing.mkdir(parents=True)
            (missing / "fixture-marker").write_text("preserve-existing-profile")
            command += ["-profile", str(missing)]
        elif mode == "environment":
            env.update(XRE_PROFILE_PATH=str(missing), XRE_PROFILE_LOCAL_PATH=str(missing))
        elif mode == "headless":
            env["MOZ_HEADLESS"] = "1"
        command.append("about:blank")
        try:
            with (root / "startup.log").open("w+") as log:
                process = subprocess.Popen(command, env=env, stdout=log, stderr=log)
                try:
                    time.sleep(6)
                    running = process.poll() is None
                    windows = subprocess.check_output(["xwininfo", "-root", "-tree"], text=True) if not mac and mode != "headless" else ""
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                log.seek(0)
                text = log.read()
            recovery = "opening Profile Manager for user choice." in text
            expected = recovery_supported and mode in ("default", "not-directory")
            # Existing explicit profiles must bypass recovery without a reset.
            untouched = missing.read_text() == "do-not-replace" if mode == "not-directory" else (missing / "fixture-marker").read_text() == "preserve-existing-profile" if mode == "explicit" else not missing.exists()
            assert recovery == expected, f"{mode}: recovery={recovery}, expected={expected}; {text[-2000:]}"
            assert untouched, f"{mode}: profile handling changed unexpectedly"
            assert running or mode == "headless", f"{mode}: unexpected early exit; {text[-2000:]}"
            assert "Error: App:" not in text and "Couldn't load XPCOM" not in text, "invalid fixture application"
            return {"mode": mode, "recoveryOffered": recovery, "profileHandlingMatchesExpected": untouched, "profileDirectoryExists": missing.is_dir(), "runningBeforeStop": running, "exitCode": process.returncode, "windows": windows, "logTail": text[-2000:].replace(str(root), "$FIXTURE")}
        finally:
            shutil.rmtree(registry)
            if cache.exists():
                shutil.rmtree(cache)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firefox", required=True)
    parser.add_argument("--xul-sha256", required=True)
    parser.add_argument("--baseline", action="store_true", help="Expect old behavior, before recovery patch")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    assert platform.system() in ("Linux", "Darwin"), "This fixture currently supports Linux/macOS only"
    binary = Path(args.firefox).absolute()
    if binary.suffix == ".app":
        binary /= "Contents/MacOS/firefox"
    xul = binary.parent / ("XUL" if platform.system() == "Darwin" else "libxul.so")
    digest = hashlib.sha256()
    with xul.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    digest = digest.hexdigest()
    assert digest == args.xul_sha256, "artifact hash mismatch"
    results = [probe(binary, mode, not args.baseline) for mode in ("default", "not-directory", "explicit", "environment", "headless")]
    report = {"platform": platform.system(), "systemVersion": platform.mac_ver()[0] if platform.system() == "Darwin" else platform.release(), "xulSHA256": digest, "baseline": args.baseline, "checks": results, "limitations": ["Synthetic isolated application namespace, not a reproduction of the reporter's macOS 27 installation.", "No automatic data migration or profile locking bypass."]}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
