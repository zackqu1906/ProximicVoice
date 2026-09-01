"""Validate architecture, deployment target, and linkage of a macOS app bundle."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess


SYSTEM_PREFIXES = ("/System/Library/", "/usr/lib/")
PORTABLE_PREFIXES = ("@executable_path/", "@loader_path/", "@rpath/")


def _run(*args: str | Path) -> str:
    return subprocess.run(
        [str(item) for item in args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def verify_bundle(app: Path, minimum_macos: str) -> list[str]:
    errors: list[str] = []
    executable_root = app / "Contents" / "MacOS"
    resources_root = app / "Contents" / "Resources"
    files = [item for item in app.rglob("*") if item.is_file()]
    mach_o_files = [
        item for item in files if "Mach-O" in _run("/usr/bin/file", item)
    ]
    if not mach_o_files:
        return [f"no Mach-O files found in {app}"]

    allowed_version = _version(minimum_macos)
    for binary in mach_o_files:
        relative = binary.relative_to(app)
        description = _run("/usr/bin/file", binary)
        if binary.parent == executable_root and "arm64" not in description:
            errors.append(f"main executable is not arm64: {relative}")

        load_commands = _run("/usr/bin/otool", "-l", binary)
        for match in re.finditer(r"\bminos\s+(\d+(?:\.\d+)+)", load_commands):
            actual = match.group(1)
            if _version(actual) > allowed_version:
                errors.append(
                    f"requires macOS {actual}, above declared {minimum_macos}: {relative}"
                )

        linked = _run("/usr/bin/otool", "-L", binary).splitlines()[1:]
        for line in linked:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith(SYSTEM_PREFIXES + PORTABLE_PREFIXES):
                continue
            if dependency.startswith(str(app)):
                continue
            errors.append(f"non-portable dependency {dependency}: {relative}")

    if not resources_root.is_dir():
        errors.append("missing Contents/Resources")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--minimum-macos", required=True)
    args = parser.parse_args()
    errors = verify_bundle(args.app.resolve(), args.minimum_macos)
    if errors:
        print("macOS bundle verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"macOS bundle portability check passed: {args.app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
