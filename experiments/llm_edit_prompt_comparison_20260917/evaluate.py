"""Explicit live comparison using the configured model, with no desktop writes.

Contains the user-authorized failing example and synthetic regression cases.
API authentication stays in memory; reports contain no settings or keys.
Open-ended rewrites require human review in addition to automatic checks.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from tools import test_llm  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from variants import all_variants  # noqa: E402


def check(case, actual):
    failures = []
    if "expected" in case and actual != case["expected"]:
        failures.append("exact_text")
    if case.get("changed") and actual.strip() == case["source"].strip():
        failures.append("unchanged")
    if case.get("changed") and not actual.strip():
        failures.append("empty")
    for word in case.get("must_include", []):
        if word.lower() not in actual.lower():
            failures.append(f"missing:{word}")
    if case.get("any_include") and not any(
        word.lower() in actual.lower() for word in case["any_include"]
    ):
        failures.append("missing_alternative")
    for word in case.get("forbidden", []):
        if word in actual:
            failures.append(f"forbidden:{word}")
    for key, comparator in (("min_length", lambda a, b: a >= b), ("max_length", lambda a, b: a <= b)):
        if key in case and not comparator(len(actual), case[key]):
            failures.append(key)
    if "starts_with" in case and not actual.startswith(case["starts_with"]):
        failures.append("prefix_changed")
    if "ends_with" in case and not actual.endswith(case["ends_with"]):
        failures.append("suffix_changed")
    if "lines" in case and len([line for line in actual.splitlines() if line.strip()]) != case["lines"]:
        failures.append("line_count")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variants", nargs="+", default=["baseline", "scope_prefix", "balanced"])
    parser.add_argument("--suite", choices=("screen", "holdout", "corrections"), default="screen")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--synthetic-only", action="store_true", help="Exclude the private reported example from every outbound request")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--branches", nargs="+", choices=("fragment", "full", "race"), default=["fragment", "full"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    variants = all_variants()
    if "production" in args.variants:
        from proximic_ring.text_processing.prompts import EDIT_FRAGMENT_PROMPT, EDIT_FULL_TEXT_PROMPT
        variants["production"] = {"fragment": EDIT_FRAGMENT_PROMPT, "full": EDIT_FULL_TEXT_PROMPT}
    if set(args.variants) - variants.keys():
        parser.error("Unknown prompt variant")
    if "race" in args.branches and args.variants != ["production"]:
        parser.error("race uses production prompts; select --variants production")
    if not 1 <= args.repeat <= 3:
        parser.error("repeat must be between 1 and 3")
    if args.suite == "corrections":
        cases = json.loads((HERE.parent / "llm_correction_prompts_20260917/cases.json").read_text())
        cases = [case for case in cases if "source" in case]
    else:
        cases = [case for case in json.loads((HERE / "cases.json").read_text()) if case["suite"] == args.suite]
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
        if len(cases) != len(set(args.case)):
            parser.error("Unknown case id in this suite")
    if args.synthetic_only:
        cases = [case for case in cases if case["id"] != "reported_polish"]
    if not cases:
        parser.error("No permitted cases selected")
    saved = QSettings("ProxiMic", "ProxiMic Voice")
    settings = test_llm.LLMSettings(
        enabled=True, provider=str(saved.value("llm/provider", "")),
        base_url=str(saved.value("llm/baseUrl", "")),
        model=str(saved.value("llm/model", "")),
        api_key=str(saved.value("llm/apiKey", "")),
        api_key_env=str(saved.value("llm/apiKeyEnv", "")), timeout_s=30,
    )
    settings.validate()
    report = {
        "at": datetime.now(timezone.utc).isoformat(), "model": settings.model,
        "suite": args.suite, "repeat": args.repeat,
        "prompt_hashes": {
            name: {branch: hashlib.sha256(value.encode()).hexdigest() for branch, value in variants[name].items()}
            for name in args.variants
        },
        "cases": cases, "results": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def run(case, name, branch, trial):
        started = time.perf_counter()
        row = {"case": case["id"], "variant": name, "branch": branch, "trial": trial}
        processor = test_llm.OpenAICompatibleTextProcessor()
        try:
            if branch == "race":
                final, output, traces, winner = processor.process_with_collection_trace(
                    case["utterance"], test_llm.INPUT_MODE_EDIT, settings,
                    case["source"], "race",
                )
                raw = (output,)
                row.update(branch_traces=[asdict(trace) for trace in traces], winner=winner)
            else:
                final, raw = processor._process_edit_with_retry(
                    settings, target=case["source"],
                    user_content=f"<待修改文本>\n{case['source']}\n</待修改文本>\n\n<修改要求>\n{case['utterance']}\n</修改要求>",
                    system_prompt=variants[name][branch], max_tokens=32768,
                    edit_mode=branch, max_attempts=1,
                )
            failures = check(case, final)
            row.update(final_text=final, raw_returns=list(raw), failures=failures,
                       checks_passed=not failures, review_required=bool(case.get("review")))
        except Exception as exc:
            row.update(error_type=type(exc).__name__, failures=["request_or_protocol_error"], checks_passed=False)
        row["elapsed_s"] = round(time.perf_counter() - started, 3)
        return row

    # Interleave variants to reduce time-of-run bias in rough latency comparisons.
    jobs = [(case, name, branch, trial) for trial in range(1, args.repeat + 1)
            for case in cases for branch in args.branches for name in args.variants]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run, *job) for job in jobs]
        for future in as_completed(futures):
            row = future.result()
            report["results"].append(row)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({key: row.get(key) for key in ("case", "variant", "branch", "trial", "checks_passed", "failures", "elapsed_s", "final_text")}, ensure_ascii=False), flush=True)
    report["summary"] = {
        name: {
            "passed": sum(row["checks_passed"] for row in report["results"] if row["variant"] == name),
            "total": sum(row["variant"] == name for row in report["results"]),
            "median_s": round(statistics.median(row["elapsed_s"] for row in report["results"] if row["variant"] == name), 3),
        } for name in args.variants
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
