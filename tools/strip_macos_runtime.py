"""Remove non-runtime symbols from audited libraries, before bundle signing.

Do not strip the PyInstaller executable: it also contains the Python archive.
This allowlist intentionally leaves models, helpers and optional modules intact.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


LIBRARIES = (
    "torch/lib/libtorch_cpu.dylib",
    "torch/lib/libtorch_python.dylib",
    "numpy/__dot__dylibs/libopenblas64_.0.dylib",
    "libpython3.11.dylib",
    "cryptography/hazmat/bindings/_rust.abi3.so",
    "PySide6/QtWidgets.abi3.so",
    "tokenizers/tokenizers.abi3.so",
    "PySide6/QtGui.abi3.so",
    "PySide6/QtCore.abi3.so",
    "numpy/core/_multiarray_umath.cpython-311-darwin.so",
)


def _run(*args: str | Path) -> str:
    result = subprocess.run([str(arg) for arg in args], check=True,
                            capture_output=True, text=True)
    return result.stdout


def strip_bundle(app: Path, *, run=_run) -> dict:
    app = app.resolve(strict=True)
    with (app / "Contents/Info.plist").open("rb") as stream:
        if plistlib.load(stream).get("CFBundleIdentifier") != "com.proximic.voice":
            raise ValueError("Expected a Proximic Voice application bundle")
    binaries = [app / "Contents/Frameworks" / name for name in LIBRARIES]
    # Validate the entire set before changing a file. A dependency upgrade
    # should request an explicit audit rather than silently miss a library.
    for binary in binaries:
        if binary.is_symlink() or not binary.is_file() or not binary.resolve().is_relative_to(app):
            raise ValueError(f"Missing or redirected runtime library: {binary}")
        description = run("/usr/bin/file", "-b", binary)
        if "Mach-O" not in description or "executable" in description:
            raise ValueError(f"Expected a Mach-O library, not an executable: {binary}")

    entries = []
    for binary in binaries:
        before = binary.stat().st_size
        exports = set(run("/usr/bin/nm", "-gUj", binary).splitlines())
        linkage = run("/usr/bin/otool", "-L", binary).splitlines()[1:]
        # Only replace the original after the transformed file is verified.
        with tempfile.TemporaryDirectory(prefix=".strip-", dir=binary.parent) as directory:
            staged = Path(directory) / binary.name
            shutil.copy2(binary, staged)
            run("/usr/bin/strip", "-S", "-x", staged)
            if set(run("/usr/bin/nm", "-gUj", staged).splitlines()) != exports:
                raise RuntimeError(f"Exported symbols changed: {binary}")
            if run("/usr/bin/otool", "-L", staged).splitlines()[1:] != linkage:
                raise RuntimeError(f"Runtime dependencies changed: {binary}")
            after = staged.stat().st_size
            if after < before:
                os.replace(staged, binary)
            else:
                after = before  # Already stripped: retain its original bytes/signature.
        entries.append({"path": str(binary.relative_to(app)), "before": before,
                        "after": after, "saved": before - after,
                        "exported_symbols": len(exports)})
    return {"app": str(app), "saved_bytes": sum(item["saved"] for item in entries),
            "libraries": entries, "requires_resigning": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = strip_bundle(args.app)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Runtime symbols removed: {report['saved_bytes'] / 2**20:.2f} MiB; "
          "exports and linkage unchanged. Bundle must now be signed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
