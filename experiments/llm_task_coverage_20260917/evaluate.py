"""Explicit synthetic-only evaluation of current production LLM tasks.

Reads saved connection settings without changing them. No ASR, Ring, desktop
injection or history writes. Run manually; never collected as a unit test.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from tools import test_llm  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from proximic_ring.text_processing import prompts  # noqa: E402
from proximic_ring.text_processing.llm import LLMResponseProcessingError  # noqa: E402


def check(case, text):
    failures = []
    if "expected" in case and text != case["expected"]:
        failures.append("exact_text")
    if case.get("changed") and (not text.strip() or text.strip() == case["source"].strip()):
        failures.append("no_change")
    for token in case.get("must_include", []):
        if token.lower() not in text.lower():
            failures.append("missing:" + token)
    for group in case.get("any_groups", []):
        if not any(token.lower() in text.lower() for token in group):
            failures.append("missing_any:" + "/".join(group))
    for token in case.get("forbidden", []):
        if token in text:
            failures.append("forbidden:" + token)
    count = len(re.sub(r"\s", "", text))
    if "min_chars" in case and count < case["min_chars"]:
        failures.append(f"too_short:{count}<{case['min_chars']}")
    if "max_chars" in case and count > case["max_chars"]:
        failures.append(f"too_long:{count}>{case['max_chars']}")
    if "lines" in case and len([line for line in text.splitlines() if line.strip()]) != case["lines"]:
        failures.append("line_count")
    if "starts_with" in case and not text.startswith(case["starts_with"]):
        failures.append("prefix_changed")
    if "ends_with" in case and not text.endswith(case["ends_with"]):
        failures.append("suffix_changed")
    for pattern in case.get("patterns", []):
        if not re.search(pattern, text):
            failures.append("pattern:" + pattern)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases-file", type=Path, default=HERE / "cases.json")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--repeat", type=int, default=1, choices=(1, 2, 3))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output filename; existing evidence is not overwritten")
    cases = json.loads(args.cases_file.read_text())
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
        if len(cases) != len(set(args.case)):
            parser.error("Unknown case id")
    saved = QSettings("ProxiMic", "ProxiMic Voice")
    settings = test_llm.LLMSettings(
        enabled=True, provider=str(saved.value("llm/provider", "")),
        base_url=str(saved.value("llm/baseUrl", "")), model=str(saved.value("llm/model", "")),
        api_key=str(saved.value("llm/apiKey", "")), api_key_env=str(saved.value("llm/apiKeyEnv", "")),
        timeout_s=45,
    )
    settings.validate()
    report = dict(
        at=datetime.now(timezone.utc).isoformat(), model=settings.model,
        provider=settings.provider, repeat=args.repeat, cases=cases, results=[],
        prompt_sha256={name: hashlib.sha256(getattr(prompts, name).encode()).hexdigest()
                       for name in ("EDIT_FRAGMENT_PROMPT", "EDIT_FULL_TEXT_PROMPT", "DICTATION_PROMPT", "INPUT_MODE_ROUTER_PROMPT")},
    )

    def run(case, trial):
        processor = test_llm.OpenAICompatibleTextProcessor()
        started = time.perf_counter()
        row = dict(id=case["id"], phase=case["phase"], category=case["category"], trial=trial,
                   review_required=bool(case.get("review")))
        try:
            if case["phase"] == "route":
                actual, raw = processor.classify_input_mode_with_trace(case["utterance"], settings)
                row.update(raw_returns=[raw], status="returned")
            elif case["phase"] == "dictation":
                actual, raw = processor.process_with_trace(case["utterance"], test_llm.INPUT_MODE_DICTATION, settings)
                row.update(raw_returns=[raw], status="returned")
            else:
                try:
                    actual, raw, traces, winner = processor.process_with_collection_trace(
                        case["utterance"], test_llm.INPUT_MODE_EDIT, settings, case["source"], "race"
                    )
                    row.update(raw_returns=[raw], branch_traces=[asdict(t) for t in traces], winner=winner, status="candidate")
                except LLMResponseProcessingError as exc:
                    traces = getattr(exc, "branch_traces", ())
                    row["branch_traces"] = [asdict(t) for t in traces]
                    # The app intentionally declines edits if both protocols
                    # returned the original. Only this specific outcome counts
                    # as a successful no-op; a network/JSON error never does.
                    if len(traces) == 2 and all(t.error == "大模型未找到可可靠执行的修改" for t in traces):
                        actual = case["source"]
                        row.update(status="unchanged_declined", raw_returns=list(exc.model_outputs))
                    else:
                        raise
            failures = check(case, actual)
            row.update(actual=actual, failures=failures, checks_passed=not failures)
        except Exception as exc:
            row.update(error_type=type(exc).__name__, status="error", failures=["request_or_protocol_error"], checks_passed=False)
        row["elapsed_s"] = round(time.perf_counter() - started, 3)
        return row

    args.output.parent.mkdir(parents=True, exist_ok=True)
    jobs = [(case, trial) for trial in range(1, args.repeat + 1) for case in cases]
    # Two editing jobs may each make two simultaneous protocol requests.
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(run, *job) for job in jobs]
        for future in as_completed(pending):
            row = future.result()
            report["results"].append(row)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            compact = {key: row.get(key) for key in ("id", "trial", "checks_passed", "failures", "status", "elapsed_s")}
            compact["actual"] = row.get("actual", "")[:400]
            print(json.dumps(compact, ensure_ascii=False), flush=True)
    counts = Counter(c["category"] for c in cases)
    report["summary"] = {
        category: dict(passed=sum(r["checks_passed"] for r in report["results"] if r["category"] == category),
                       total=count * args.repeat)
        for category, count in counts.items()
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
