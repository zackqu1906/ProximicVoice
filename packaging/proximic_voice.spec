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
# FunASR's package data includes assets for every model family.  The runtime
# only reads its version file; checkpoints live in the per-user model cache.
datas += collect_data_files("funasr", includes=["version.txt"])
datas += [(str(streaming_root), "third_party/streaming-sensevoice")]
datas += [
    (str(funasr_root / "model.py"), "third_party/Fun-ASR"),
    (str(funasr_root / "ctc.py"), "third_party/Fun-ASR"),
    (str(funasr_root / "tools" / "__init__.py"), "third_party/Fun-ASR/tools"),
    (str(funasr_root / "tools" / "utils.py"), "third_party/Fun-ASR/tools"),
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
    configured_opus = os.environ.get("PROXIMIC_OPUS_DYLIB", "").strip()
    candidates = ([Path(configured_opus)] if configured_opus else []) + [
        project_root / ".runtime" / "opus" / "lib" / "libopus.0.dylib",
        project_root / ".runtime" / "opus" / "libopus.0.dylib",
        Path("/opt/homebrew/opt/opus/lib/libopus.0.dylib"),
        Path("/opt/homebrew/opt/opus/lib/libopus.dylib"),
    ]
    opus_dylib = next((item for item in candidates if item.is_file()), None)
    if opus_dylib is None:
        raise SystemExit("Missing libopus; run: ./scripts/install-opus-macos.sh")
    binaries.append((str(opus_dylib), "opus"))
    opus_license = project_root / ".runtime" / "opus" / "COPYING.libopus"
    if opus_license.is_file():
        datas.append((str(opus_license), "opus"))

notices = project_root / "THIRD_PARTY_NOTICES.md"
if notices.is_file():
    datas.append((str(notices), "."))

hiddenimports = collect_submodules("proximic_ring.asr.backends")
hiddenimports += [
    "funasr",
    "funasr.auto.auto_model",
    "funasr.frontends.wav_frontend",
    "funasr.models.llm_asr.adaptor",
    "funasr.models.sense_voice.whisper_lib.tokenizer",
    "funasr.tokenizer.hf_tokenizer",
    "funasr.tokenizer.sentencepiece_tokenizer",
    "funasr.tokenizer.whisper_tokenizer",
    "streaming_sensevoice",
    "transformers",
    "opuslib",
    "websocket",
    "asr_decoder",
    "online_fbank",
    "modelscope_hub.compat.snapshot_download",
]
# Fun-ASR-Nano embeds Qwen3.  Transformers loads architecture modules lazily,
# so make this one supported architecture explicit instead of bundling every
# Transformers model family.
hiddenimports += collect_submodules("transformers.models.qwen3", on_error="raise")

a = Analysis(
    [str(project_root / "packaging" / "launcher.py")],
    pathex=[str(source_root), str(streaming_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(project_root / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "jupyter",
        # Current inference uses torchaudio/kaldi-native-fbank.  librosa is an
        # unused top-level import in FunASR load_utils; the app supplies a
        # narrow compatibility placeholder before importing FunASR.
        "librosa",
        "numba",
        "llvmlite",
        "scipy",
        "sklearn",
        "soxr",
        # Training, clustering, plotting, and language-normalization packages
        # are unrelated to the three supported runtime ASR backends.
        "jieba",
        "jamo",
        "jaconv",
        "umap",
        "pynndescent",
        "pandas",
        "tensorboardX",
        "pyopenjtalk",
        "sudachipy",
        "sudachidict_core",
        "whisper_normalizer",
        "zhconv",
        "modelscope",
    ],
    module_collection_mode={"funasr": "pyz+py"},
    noarchive=False,
)

# Both ModelScope and Transformers expose enormous TYPE_CHECKING import maps.
# The former is replaced by the focused download shim above.  For the latter,
# retain common runtime code plus the auto factories and the one architecture
# embedded in Fun-ASR-Nano.
def _used_transformers_model(name):
    normalized = str(name).replace("\\", "/")
    marker = "transformers/models/"
    if marker not in normalized:
        return True
    relative = normalized.split(marker, 1)[1]
    return (
        relative == "__init__.py"
        or relative.startswith("auto/")
        or relative.startswith("qwen3/")
    )


a.pure = [
    item
    for item in a.pure
    if not str(item[0]).startswith("transformers.models.")
    or str(item[0]).startswith("transformers.models.auto")
    or str(item[0]).startswith("transformers.models.qwen3")
]
a.datas = [item for item in a.datas if _used_transformers_model(item[0])]

# Binary dependency discovery is conservative for QML and retains frameworks
# belonging only to excluded modules.  None of these are imported by Python or
# QML in Proximic Voice; the packaged startup probe verifies the final closure.
_UNUSED_QT_FRAMEWORKS = {
    "QtMultimediaWidgets.framework",
    "QtPdf.framework",
    "QtQuick3DUtils.framework",
    "QtQmlXmlListModel.framework",
    "QtStateMachine.framework",
    "QtStateMachineQml.framework",
    "QtVirtualKeyboard.framework",
    "QtVirtualKeyboardQml.framework",
}


def _used_qt_entry(item):
    destination = str(item[0]).replace("\\", "/")
    if any(
        f"/{framework}/" in f"/{destination}/"
        or destination in {framework, framework.removesuffix(".framework")}
        for framework in _UNUSED_QT_FRAMEWORKS
    ):
        return False
    if destination.startswith("PySide6/Qt/plugins/qmltooling/"):
        return False
    if destination.startswith("PySide6/Qt/plugins/imageformats/"):
        return destination.endswith("/libqsvg.dylib")
    if destination.startswith("PySide6/Qt/translations/"):
        filename = destination.rsplit("/", 1)[-1]
        return filename.endswith(("_zh_CN.qm", "_zh_TW.qm"))
    if destination.startswith("PySide6/Qt") and destination.endswith(".abi3.so"):
        return not destination.rsplit("/", 1)[-1].startswith(
            ("QtConcurrent.", "QtDBus.", "QtMultimediaWidgets.")
        )
    if destination.startswith("torch/bin/"):
        return destination.endswith("/torch_shm_manager")
    return True


a.binaries = [item for item in a.binaries if _used_qt_entry(item)]
a.datas = [item for item in a.datas if _used_qt_entry(item)]
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
            "LSMinimumSystemVersion": "15.0",
            "NSBluetoothAlwaysUsageDescription": "Proximic Voice 使用蓝牙连接 Ringo 并接收语音。",
            "NSBluetoothPeripheralUsageDescription": "Proximic Voice 使用蓝牙连接 Ringo 并接收语音。",
            "NSMicrophoneUsageDescription": "Proximic Voice 处理来自 Ringo 的语音以完成转写。",
        },
    )
