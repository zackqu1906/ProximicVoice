import importlib.util
from pathlib import Path
import plistlib

import pytest


spec = importlib.util.spec_from_file_location(
    "strip_macos_runtime", Path(__file__).parents[1] / "tools/strip_macos_runtime.py")
stripper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stripper)


@pytest.fixture
def bundle(tmp_path):
    app = tmp_path / "Proximic Voice.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/ProximicVoice").write_bytes(b"executable+python-archive")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.proximic.voice"}))
    for name in stripper.LIBRARIES:
        binary = app / "Contents/Frameworks" / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"original" * 20)
    return app


def runner(fault=None):
    def run(command, *args):
        path = Path(args[-1])
        staged = path.parent.name.startswith(".strip-")
        if command.endswith("/file"):
            return "Mach-O 64-bit dynamically linked shared library arm64"
        if command.endswith("/nm"):
            return "_changed" if staged and fault == "export" else "_function\n_global\n"
        if command.endswith("/otool"):
            dependency = "/wrong.dylib" if staged and fault == "linkage" else "/usr/lib/libSystem.B.dylib"
            return f"{path}:\n\t{dependency}\n"
        assert command.endswith("/strip") and args[:2] == ("-S", "-x")
        if fault == "strip":
            raise RuntimeError("strip failed")
        path.write_bytes(b"stripped")
        return ""
    return run


def test_only_audited_libraries_change_and_second_run_is_idempotent(bundle):
    extra = bundle / "Contents/Frameworks/opus/libopus.0.dylib"
    extra.parent.mkdir()
    extra.write_bytes(b"opus runtime")
    report = stripper.strip_bundle(bundle, run=runner())
    assert report["saved_bytes"] == len(stripper.LIBRARIES) * (160 - 8)
    assert (bundle / "Contents/MacOS/ProximicVoice").read_bytes() == b"executable+python-archive"
    assert extra.read_bytes() == b"opus runtime"
    assert stripper.strip_bundle(bundle, run=runner())["saved_bytes"] == 0


@pytest.mark.parametrize("fault", ["export", "linkage", "strip"])
def test_failed_transformation_keeps_original_library(bundle, fault):
    binary = bundle / "Contents/Frameworks" / stripper.LIBRARIES[0]
    with pytest.raises(RuntimeError):
        stripper.strip_bundle(bundle, run=runner(fault))
    assert binary.read_bytes() == b"original" * 20
    assert not list(binary.parent.glob(".strip-*"))


@pytest.mark.parametrize("redirect", [False, True])
def test_missing_or_redirected_library_fails_before_any_stripping(bundle, tmp_path, redirect):
    binary = bundle / "Contents/Frameworks" / stripper.LIBRARIES[-1]
    binary.unlink()
    external = tmp_path / "unrelated.dylib"
    external.write_bytes(b"external library")
    if redirect:
        binary.symlink_to(external)
    with pytest.raises(ValueError, match="Missing or redirected"):
        stripper.strip_bundle(bundle, run=runner())
    assert (bundle / "Contents/Frameworks" / stripper.LIBRARIES[0]).read_bytes() == b"original" * 20
    assert external.read_bytes() == b"external library"


def test_rejects_other_apps(bundle):
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "other.app"}))
    with pytest.raises(ValueError, match="Expected a Proximic"):
        stripper.strip_bundle(bundle, run=runner())
