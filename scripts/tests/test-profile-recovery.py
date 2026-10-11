"""Check the startup patch on the pinned source without editing that checkout."""
from pathlib import Path
import subprocess
import tempfile
import sys

repo = Path(__file__).resolve().parents[2]
upstream = Path(sys.argv[1]) if len(sys.argv) > 1 else repo / "upstream"
baseline = "cebc55aab4d2661d1f6c2d1526362947ec4016c1"
relative = "toolkit/xre/nsAppRunner.cpp"
source = subprocess.check_output(["git", "-C", str(upstream), "show", f"{baseline}:{relative}"])
patch = repo / "patches/agent-ui/0005-offer-profile-manager-for-missing-default.patch"
with tempfile.TemporaryDirectory(prefix="frx-startup-patch-") as temporary:
    root = Path(temporary)
    target = root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=root, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=root, check=True)
    changed = target.read_text()
    start = changed.index("  if (wasDefaultSelection) {")
    end = changed.index("  // We always want to lock the profile", start)
    block = changed[start:end]
    for guard in ("!gfxPlatform::IsHeadless()", "!gDoProfileReset", "NS_SUCCEEDED(mProfD->Exists", "NS_SUCCEEDED(mProfD->IsDirectory", "ShowProfileManager(mProfileSvc, mNativeApp)"):
        assert guard in block, guard
    assert "Create" not in block and "Remove" not in block and "Unlock" not in block
    assert "Output(" not in block, "Output opens a second modal dialog on Windows"
    assert changed[:start] == source.decode()[:source.decode().index("  if (wasDefaultSelection) {")]
    assert changed[end:] == source.decode()[source.decode().index("  // We always want to lock the profile", source.decode().index("  if (wasDefaultSelection) {")):]
    subprocess.run(["git", "apply", "--reverse", str(patch)], cwd=root, check=True)
    assert target.read_bytes() == source
print("Profile recovery patch: pinned-source application, explicit/headless/reset boundaries and unchanged locking verified; native runtime test still required")
