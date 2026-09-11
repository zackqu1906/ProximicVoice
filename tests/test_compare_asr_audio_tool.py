from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_SPEC = importlib.util.spec_from_file_location(
    "compare_asr_audio", Path(__file__).parents[1] / "tools/compare_asr_audio.py"
)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


def test_agreement_ignores_punctuation_and_width():
    assert tool._agreement("今天，天气好。", "今天天气好") == 1.0
    assert tool._agreement("", "") == 1.0
    assert tool._agreement("有文字", "") == 0.0


def test_parser_defaults_to_all_three_backends():
    args = tool.build_parser().parse_args([])
    assert args.backends is None
    assert tool.BACKENDS == ("doubao", "sensevoice", "funasr_nano")
    assert args.dataset_limit == -1
