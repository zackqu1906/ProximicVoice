# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import platform

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).parent
source_root = project_root / "src"
streaming_root = project_root / "third_party" / "streaming-sensevoice"
funasr_root = project_root / "third_party" / "Fun-ASR"

datas = collect_data_files("proximic_ring")
datas += collect_data_files("funasr")
datas += [(str(streaming_root), "third_party/streaming-sensevoice")]
datas += [
    (str(funasr_root / "model.py"), "third_party/Fun-ASR"),
    (str(funasr_root / "ctc.py"), "third_party/Fun-ASR"),
    (str(funasr_root / "tools"), "third_party/Fun-ASR/tools"),
]

binaries = []
if os.name == "nt":
    opus_dll = project_root / ".runtime" / "opus" / "opus.dll"
    opus_license = project_root / ".runtime" / "opus" / "COPYING.libopus"
    if not opus_dll.is_file() or not opus_license.is_file():
        raise SystemExit(
            "Missing .runtime/opus runtime files; run scripts/setup.ps1"
        )
    binaries.append((str(opus_dll), "opus"))
    datas.append((str(opus_license), "opus"))
elif platform.system() == "Darwin":
    candidates = [
        Path("/opt/homebrew/opt/opus/lib/libopus.0.dylib"),
        Path("/opt/homebrew/opt/opus/lib/libopus.dylib"),
    ]
    opus_dylib = next((item for item in candidates if item.is_file()), None)
    if opus_dylib is None:
        raise SystemExit("Missing Homebrew libopus; run: brew install opus")
    binaries.append((str(opus_dylib), "opus"))

hiddenimports = collect_submodules("proximic_ring.asr.backends")
hiddenimports += collect_submodules("funasr", on_error="ignore")
hiddenimports += [
    "funasr",
    "streaming_sensevoice",
    "transformers",
    "opuslib",
    "websocket",
    "asr_decoder",
    "online_fbank",
    "zhconv",
    "pyopenjtalk",
]

a = Analysis(
    [str(project_root / "packaging" / "launcher.py")],
    pathex=[str(source_root), str(streaming_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(project_root / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter"],
    module_collection_mode={"funasr": "pyz+py"},
    noarchive=False,
)
# Qt 6 on Windows links to the OS-provided, unsuffixed ICU API. A developer's
# PATH may contain Poppler/Conda ICU binaries with the same filename but
# version-suffixed exports; bundling those makes QtCore fail with WinError 127.
if os.name == "nt":
    foreign_icu = {"icuuc.dll", "icudt78.dll"}
    a.binaries = [
        item for item in a.binaries
        if Path(item[0]).name.lower() not in foreign_icu
    ]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ProximicVoice",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ProximicVoice",
)

if platform.system() == "Darwin":
    app = BUNDLE(
        coll,
        name="Proximic Voice.app",
        bundle_identifier="com.proximic.voice",
        info_plist={
            "CFBundleDisplayName": "Proximic Voice",
            "CFBundleShortVersionString": "0.6.0",
            "CFBundleVersion": "0.6.0",
            "LSMinimumSystemVersion": "12.0",
            "NSBluetoothAlwaysUsageDescription": "Proximic Voice 使用蓝牙连接 Ringo 并接收语音。",
            "NSBluetoothPeripheralUsageDescription": "Proximic Voice 使用蓝牙连接 Ringo 并接收语音。",
            "NSMicrophoneUsageDescription": "Proximic Voice 处理来自 Ringo 的语音以完成转写。",
        },
    )
