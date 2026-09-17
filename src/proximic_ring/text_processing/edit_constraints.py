"""Cheap checks for unambiguous, whole-text editing instructions."""

from __future__ import annotations

import re


_EXPAND_TO_LENGTH = re.compile(
    r"(?:请)?(?:帮我)?(?:(?:把|将)?(?:这段(?:话|文字)?|原文|全文))?"
    r"扩写(?:到|至|成)(?P<count>[0-9]+|[零〇一二两三四五六七八九十百千]+)"
    r"(?:个)?字[。！!]?"
)
_DIGITS = {char: number for number, char in enumerate("零一二三四五六七八九")}
_DIGITS.update({"〇": 0, "两": 2})
_UNITS = {"十": 10, "百": 100, "千": 1000}


def _parse_count(value: str) -> int | None:
    if value.isascii():
        return int(value) if len(value) <= 5 else None
    if not any(char in _UNITS for char in value):
        return _DIGITS[value] if len(value) == 1 else None
    total = digit = 0
    last_unit = 1
    gap = False
    for char in value:
        if char in _DIGITS:
            digit = _DIGITS[char]
            gap |= digit == 0
        else:
            last_unit = _UNITS[char]
            total += (digit or 1) * last_unit
            digit = 0
            gap = False
    # Colloquial "一百二" can mean 120; don't enforce a guessed count.
    if digit and last_unit > 10 and not gap:
        return None
    return total + digit


def validate_expansion_result(instruction: str, source: str, result: str) -> None:
    """Reject unchanged-length/incorrect-length expansions before race selection.

    Only a complete, explicit instruction such as '扩写到100个字' is matched.
    Scoped, approximate and compound requests remain under model control.
    Count visible characters (including punctuation), excluding whitespace;
    allow 10% tolerance, while requiring an actual increase over the source.
    """
    match = _EXPAND_TO_LENGTH.fullmatch(re.sub(r"\s+", "", instruction))
    if match is None:
        return
    count = _parse_count(match["count"])
    if count is None or not 1 <= count <= 30000:
        return
    actual = len(re.sub(r"\s+", "", result))
    original = len(re.sub(r"\s+", "", source))
    lower, upper = (count * 9 + 9) // 10, count * 11 // 10
    if actual <= original or not lower <= actual <= upper:
        raise ValueError(
            f"扩写未达到字数要求：目标{count}字，原文{original}字，结果{actual}字；"
            f"应增加篇幅且在{lower}～{upper}字之间"
        )
