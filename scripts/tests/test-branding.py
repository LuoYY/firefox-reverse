import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import shlex
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("branding", REPO / "scripts/verify-branding.py")
branding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(branding)


class BrandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="frx-branding-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "MOZ_BRANDING_DIRECTORY": branding.BRAND_DIR,
            "MOZ_APP_DISPLAYNAME": branding.BRAND,
            "MOZ_MACBUNDLE_ID": "org.mozilla.firefox-reverse",
            "MOZ_WIDGET_TOOLKIT": "cocoa",
        }
        self.entries = {"chrome/chrome.manifest": b"content branding browser/content/branding/ contentaccessible=yes\n"}
        for name in branding.CONTENT:
            self.entries["chrome/browser/content/branding/" + name] = branding.expected("content/" + name)
        for size in (16, 32, 48, 64, 128):
            self.entries[f"chrome/browser/content/branding/icon{size}.png"] = branding.expected(f"default{size}.png")
        for locale in ("en-US", "zh-CN"):
            self.entries[f"localization/{locale}/branding/brand.ftl"] = branding.expected("locales/en-US/brand.ftl")
            self.entries[f"chrome/{locale}/locale/branding/brand.properties"] = branding.expected("locales/en-US/brand.properties")
        self.info = {"CFBundleName": branding.BRAND, "CFBundleIconFile": "firefox.icns", "CFBundleIdentifier": "org.mozilla.firefox-reverse"}

    def object_dir(self):
        obj = self.root / "obj"
        obj.mkdir(exist_ok=True)
        # Real config.status also includes nonliteral CompilerType instances.
        text = repr(self.config)[:-1] + ", 'CC_TYPE': CompilerType('clang')}"
        (obj / "config.status").write_text("substs = " + text, encoding="utf-8")
        return obj

    def stage(self, mac=False, code_name="Firefox Reverse"):
        stage = self.root / ("Firefox Reverse.app" if mac else "firefox")
        resources = stage / "Contents/Resources" if mac else stage
        (resources / "browser").mkdir(parents=True, exist_ok=True)
        (resources / "application.ini").write_text(f"[App]\nName=Firefox\nCodeName={code_name}\n", encoding="utf-8")
        with zipfile.ZipFile(resources / "browser/omni.ja", "w") as archive:
            for name, value in self.entries.items():
                archive.writestr(name, value)
        if mac:
            (stage / "Contents/Info.plist").write_bytes(plistlib.dumps(self.info))
            (resources / "firefox.icns").write_bytes(branding.expected("firefox.icns"))
        return stage

    def test_configured_brand_and_safe_literal_parsing(self):
        obj = self.object_dir()
        with (obj / "config.status").open("a") as stream:
            stream.write("\nraise RuntimeError('never execute config.status')\n")
        self.assertEqual(branding.verify_object(obj)["displayName"], branding.BRAND)

    def test_wrong_branding_is_rejected_even_with_correct_source_assets(self):
        self.config["MOZ_BRANDING_DIRECTORY"] = "browser/branding/nightly"
        with self.assertRaisesRegex(ValueError, "wrong active"):
            branding.verify_object(self.object_dir())

    def test_stale_configured_display_name(self):
        self.config["MOZ_APP_DISPLAYNAME"] = "Nightly"
        with self.assertRaisesRegex(ValueError, "stale MOZ_APP_DISPLAYNAME"):
            branding.verify_object(self.object_dir())

    def test_official_branding_and_wrong_bundle_id(self):
        self.config["MOZ_OFFICIAL_BRANDING"] = True
        with self.assertRaisesRegex(ValueError, "official branding"):
            branding.verify_object(self.object_dir())
        self.config["MOZ_OFFICIAL_BRANDING"] = False
        self.config["MOZ_MACBUNDLE_ID"] = "org.mozilla.nightly"
        with self.assertRaisesRegex(ValueError, "bundle identity"):
            branding.verify_object(self.object_dir())

    def test_missing_configuration(self):
        obj = self.object_dir()
        (obj / "config.status").write_text("substs = {}")
        with self.assertRaisesRegex(ValueError, "wrong active"):
            branding.verify_object(obj)

    def test_stage_and_final_archives(self):
        stage = self.stage()
        self.assertEqual(branding.verify_package(stage)["assetsChecked"], 13)
        for suffix in ("zip", "tar.xz"):
            archive_path = self.root / ("candidate." + suffix)
            if suffix == "zip":
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for file in stage.rglob("*"):
                        if file.is_file():
                            archive.write(file, file.relative_to(self.root))
            else:
                with tarfile.open(archive_path, "w:xz") as archive:
                    archive.add(stage, arcname="firefox")
            self.assertFalse(branding.verify_package(archive_path)["macOSAppChecked"])

    def test_mac_app_and_container(self):
        self.assertTrue(branding.verify_package(self.stage(mac=True))["macOSAppChecked"])
        self.assertTrue(branding.verify_package(self.root)["macOSAppChecked"])

    def test_every_shipped_logo_and_wordmark_must_match(self):
        for name in list(self.entries):
            if not name.startswith("chrome/browser/content/branding/"):
                continue
            with self.subTest(name=name):
                original = self.entries[name]
                self.entries[name] = b"wrong-default-branding"
                with self.assertRaisesRegex(ValueError, "packaged branding asset differs"):
                    branding.verify_package(self.stage())
                self.entries[name] = original

    def test_localized_nightly_terms_and_properties(self):
        for name in list(self.entries):
            if not name.endswith(("brand.ftl", "brand.properties")):
                continue
            with self.subTest(name=name):
                original = self.entries[name]
                self.entries[name] = original.replace(b"Firefox Reverse", b"Nightly")
                with self.assertRaisesRegex(ValueError, "is not Firefox Reverse"):
                    branding.verify_package(self.stage())
                self.entries[name] = original

    def test_crlf_brand_names(self):
        for name in self.entries:
            if name.endswith(("brand.ftl", "brand.properties")):
                self.entries[name] = self.entries[name].replace(b"\n", b"\r\n")
        branding.verify_package(self.stage())

    def test_missing_wordmark_or_locale(self):
        del self.entries["chrome/browser/content/branding/firefox-wordmark.svg"]
        with self.assertRaises(KeyError):
            branding.verify_package(self.stage())
        self.entries["chrome/browser/content/branding/firefox-wordmark.svg"] = branding.expected("content/firefox-wordmark.svg")
        del self.entries["localization/en-US/branding/brand.ftl"]
        with self.assertRaisesRegex(ValueError, "missing en-US"):
            branding.verify_package(self.stage())

    def test_resource_mapping_and_overrides(self):
        key = "chrome/chrome.manifest"
        original = self.entries[key]
        self.entries[key] = original.replace(b"browser/content/branding/", b"nightly/")
        with self.assertRaisesRegex(ValueError, "points to unexpected"):
            branding.verify_package(self.stage())
        self.entries[key] = original + b"override chrome://branding/content/about-logo.png chrome://foo/logo.png\n"
        with self.assertRaisesRegex(ValueError, "override is active"):
            branding.verify_package(self.stage())

    def test_mac_name_icon_catalog_and_icon_bytes(self):
        for key, wrong in (("CFBundleName", "Nightly"), ("CFBundleDisplayName", "Nightly"),
                           ("CFBundleIdentifier", "org.mozilla.nightly"), ("CFBundleIconName", "AppIcon")):
            with self.subTest(key=key):
                original = dict(self.info)
                self.info[key] = wrong
                with self.assertRaises(ValueError):
                    branding.verify_package(self.stage(mac=True))
                self.info = original
        stage = self.stage(mac=True)
        (stage / "Contents/Resources/firefox.icns").write_bytes(b"old-icon")
        with self.assertRaisesRegex(ValueError, "app icon differs"):
            branding.verify_package(stage)

    def test_application_name(self):
        with self.assertRaisesRegex(ValueError, "application.ini"):
            branding.verify_package(self.stage(code_name="Nightly"))

    def test_gate_runs_from_a_clean_source_archive(self):
        clone = self.root / "exported-source"
        clone.mkdir()
        # Do not depend on ignored files or a previous upstream checkout.
        archive = subprocess.check_output(["git", "-C", str(REPO), "archive", "HEAD", "additions/browser/branding"])
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for entry in tar:
                if entry.isfile():
                    destination = clone / entry.name
                    self.assertTrue(destination.resolve().is_relative_to(clone.resolve()))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(tar.extractfile(entry).read())
        (clone / "scripts").mkdir()
        shutil.copy2(REPO / "scripts/verify-branding.py", clone / "scripts/verify-branding.py")
        result = subprocess.run(["python3", str(clone / "scripts/verify-branding.py"), "--package", str(self.stage())], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["branding"], "passed")

    def test_build_and_release_entrypoints_have_branding_gates(self):
        for name in ("build.sh", "package.sh", "build-linux-arm64.sh", "repack-zh-cn.sh"):
            text = (REPO / "scripts" / name).read_text()
            self.assertIn("check-branding-assets.mjs", text, name)
            self.assertIn("verify-branding.py", text, name)
            self.assertIn("--objdir", text, name)
        for name in ("prepare-native-candidate.py", "verify-windows-package.py"):
            self.assertIn("branding.verify_package", (REPO / "scripts" / name).read_text())
        self.assertNotIn('cp -v obj-*', (REPO / "scripts/package.sh").read_text())

    def mock_build_tree(self):
        upstream = self.root / "upstream"
        shutil.copytree(REPO / "additions/browser/branding", upstream / "browser/branding")
        mac = upstream / "browser/app/macbuild/Contents"
        mac.mkdir(parents=True)
        (mac / "Info.plist.in").write_text("<key>CFBundleIconFile</key><string>firefox.icns</string>")
        (upstream / ".bootstrap-done").touch()
        log = self.root / "mach-calls.txt"
        obj = self.object_dir()
        script = "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$*\" >> " + shlex.quote(str(log)) + "\n"
        script += "if [ \"$1\" = environment ]; then printf '%s\\n' " + shlex.quote(json.dumps({"topobjdir": str(obj)})) + "; fi\n"
        (upstream / "mach").write_text(script)
        (upstream / "mach").chmod(0o755)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
        (bin_dir / "uname").chmod(0o755)
        env = {**os.environ, "UPSTREAM_DIR": str(upstream), "DIST_DIR": str(self.root / "output"),
               "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        return upstream, obj, log, env

    def test_build_checks_configuration_before_compiling(self):
        _, _, log, env = self.mock_build_tree()
        self.config["MOZ_APP_DISPLAYNAME"] = "Nightly"
        self.object_dir()
        result = subprocess.run(["bash", str(REPO / "scripts/build.sh")], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale MOZ_APP_DISPLAYNAME", result.stderr)
        calls = log.read_text().splitlines()
        self.assertIn("configure --with-branding=browser/branding/unofficial", calls)
        self.assertNotIn("build", calls)

    def test_packaging_checks_stage_and_only_copies_selected_object(self):
        upstream, obj, _, env = self.mock_build_tree()
        stage = obj / "dist/firefox"
        shutil.copytree(self.stage(), stage)
        artifact = obj / "dist/firefox-test.tar.xz"
        with tarfile.open(artifact, "w:xz") as archive:
            archive.add(stage, arcname="firefox")
        stale = upstream / "obj-old/dist/firefox-stale.tar.xz"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"old unrelated build")
        result = subprocess.run(["bash", str(REPO / "scripts/package.sh")], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in Path(env["DIST_DIR"]).iterdir()), [artifact.name])
        (stage / "application.ini").write_text("[App]\nCodeName=Nightly\n")
        env["DIST_DIR"] = str(self.root / "rejected-output")
        result = subprocess.run(["bash", str(REPO / "scripts/package.sh")], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("application.ini", result.stderr)
        self.assertFalse(Path(env["DIST_DIR"]).exists())

    def test_source_guard_rejects_missing_mac_patch(self):
        upstream, _, _, _ = self.mock_build_tree()
        (upstream / "browser/app/macbuild/Contents/Info.plist.in").write_text("<key>CFBundleIconName</key><string>AppIcon</string>")
        result = subprocess.run(["node", str(REPO / "scripts/check-branding-assets.mjs"), str(upstream)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AppIcon still overrides", result.stderr)


if __name__ == "__main__":
    unittest.main()
