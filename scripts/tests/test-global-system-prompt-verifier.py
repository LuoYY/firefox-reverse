"""Guard final-package acceptance against stale or overlaid sidebar resources."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "verify-global-system-prompt.py"
spec = importlib.util.spec_from_file_location("global_prompt_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
COMMIT = "a" * 40
BUILD = "20261012000000"


class FinalPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = (Path(self.temp.name) / "Test.app").resolve()
        binary = self.app / "Contents/MacOS"
        binary.mkdir(parents=True)
        (binary / "firefox").write_bytes(b"test executable")
        (binary / "XUL").write_bytes(b"test library")
        self.resources = self.app / "Contents/Resources"
        (self.resources / "browser").mkdir(parents=True)
        self.ini = self.resources / "application.ini"
        self.ini.write_text(f"[App]\nSourceStamp={COMMIT}\nBuildID={BUILD}\n", encoding="utf-8")
        self.entries = {entry: relative.encode() for entry, relative in verifier.SOURCE_RESOURCES.items()}
        self.bundle = verifier.UI_PREFIX + "agent-sidebar.bundle.js"
        self.entries[self.bundle] = b"test compiled sidebar"
        self.bundle_sha = hashlib.sha256(self.entries[self.bundle]).hexdigest()
        self.omni = self.resources / "browser/omni.ja"
        self.write_omni()
        self.git = patch.object(verifier.subprocess, "check_output", side_effect=self.source)
        self.git.start()
        self.addCleanup(self.git.stop)

    def source(self, command, cwd):
        self.assertEqual(command[:2], ["git", "show"])
        self.assertEqual(cwd, verifier.REPO)
        prefix = COMMIT + ":additions/browser/components/agent-sidebar/"
        self.assertTrue(command[2].startswith(prefix))
        return command[2][len(prefix):].encode()

    def write_omni(self):
        with zipfile.ZipFile(self.omni, "w") as archive:
            for name, content in self.entries.items():
                archive.writestr(name, content)

    def verify(self, **kwargs):
        args = {"source_commit": COMMIT, "build_id": BUILD, "bundle_sha256": self.bundle_sha}
        args.update(kwargs)
        return verifier.verify_final_resources(self.app, **args)

    def test_matching_resources_are_read_only(self):
        before = self.omni.read_bytes()
        report = self.verify()
        self.assertEqual(self.omni.read_bytes(), before)
        self.assertEqual(report["resource_sha256"][self.bundle], self.bundle_sha)
        self.assertEqual(report["source_commit"], COMMIT)

    def test_stale_resources_fail_individually_without_rewriting(self):
        for entry in verifier.SOURCE_RESOURCES:
            with self.subTest(entry=entry):
                old = self.entries[entry]
                self.entries[entry] = b"stale resource"
                self.write_omni()
                before = self.omni.read_bytes()
                with self.assertRaisesRegex(AssertionError, "Stale packaged resource"):
                    self.verify()
                self.assertEqual(self.omni.read_bytes(), before)
                self.entries[entry] = old

    def test_stale_bundle_fails(self):
        self.entries[self.bundle] = b"old compiled sidebar"
        self.write_omni()
        with self.assertRaisesRegex(AssertionError, "Sidebar bundle mismatch"):
            self.verify()

    def test_source_mismatch_fails_before_resource_checks(self):
        with self.assertRaisesRegex(AssertionError, "SourceStamp mismatch"):
            self.verify(source_commit="b" * 40)

    def test_build_mismatch_fails(self):
        with self.assertRaisesRegex(AssertionError, "BuildID mismatch"):
            self.verify(build_id="20261011000000")

    def test_missing_module_fails(self):
        self.entries.pop(next(iter(verifier.SOURCE_RESOURCES)))
        self.write_omni()
        with self.assertRaises(KeyError):
            self.verify()

    def test_identity_inputs_are_not_git_options(self):
        for kwargs in ({"source_commit": "--help"}, {"build_id": "latest"}, {"bundle_sha256": "short"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(AssertionError):
                self.verify(**kwargs)

    def test_application_layouts(self):
        executable, resources, xul = verifier.application_paths(self.app)
        self.assertEqual(executable, self.app / "Contents/MacOS/firefox")
        self.assertEqual(resources, self.resources)
        self.assertEqual(xul.name, "XUL")
        for filename, library in (("firefox.exe", "xul.dll"), ("firefox", "libxul.so")):
            executable = (Path(self.temp.name) / filename).resolve()
            executable.write_bytes(b"fixture")
            self.assertEqual(verifier.application_paths(executable), (executable, executable.parent, executable.parent / library))


if __name__ == "__main__":
    unittest.main()
