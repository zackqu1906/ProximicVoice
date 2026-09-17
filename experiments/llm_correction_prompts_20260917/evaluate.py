"""Live, synthetic-only prompt evaluation; no microphone or desktop writes.

Uses the app's saved LLM connection (including its key, kept only in memory).
Run explicitly with the project's Python; not collected by pytest.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import test_llm  # noqa: E402  # lightweight production imports
from PySide6.QtCore import QSettings  # noqa: E402
from proximic_ring.text_processing import prompts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--phase", choices=("route", "fragment", "full"))
    args = parser.parse_args()
    cases = json.loads(Path(__file__).with_name("cases.json").read_text())
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
        if len(cases) != len(set(args.case)):
            parser.error("Unknown case id")
    saved = QSettings("ProxiMic", "ProxiMic Voice")
    settings = test_llm.LLMSettings(
        enabled=True,
        provider=str(saved.value("llm/provider", "")),
        base_url=str(saved.value("llm/baseUrl", "")),
        model=str(saved.value("llm/model", "")),
        api_key=str(saved.value("llm/apiKey", "")),
        api_key_env=str(saved.value("llm/apiKeyEnv", "")),
        timeout_s=30.0,
    )
    settings.validate()

    def run(case, phase):
        processor = test_llm.OpenAICompatibleTextProcessor()
        started = time.perf_counter()
        expected = case["route"] if phase == "route" else case["expected"]
        result = {"id": case["id"], "phase": phase, "expected": expected}
        try:
            if phase == "route":
                actual, raw = processor.classify_input_mode_with_trace(
                    case["utterance"], settings
                )
                returns = [raw]
            else:
                actual, returns = processor.process_with_attempts(
                    case["utterance"], test_llm.INPUT_MODE_EDIT, settings,
                    case["source"], edit_mode=phase,
                )
            result.update(actual=actual, raw_returns=list(returns), passed=actual == expected)
        except Exception as exc:
            # Avoid serializing request settings, headers, or arbitrary server errors.
            result.update(error_type=type(exc).__name__, passed=False)
        result["elapsed_s"] = round(time.perf_counter() - started, 3)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    jobs = []
    for case in cases:
        if "route" in case:
            jobs.append((case, "route"))
        if "source" in case:
            jobs.extend((case, mode) for mode in test_llm.EDIT_TEST_MODES)
    if args.phase:
        jobs = [job for job in jobs if job[1] == args.phase]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run, *job) for job in jobs]
        results = [future.result() for future in as_completed(futures)]
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "model": settings.model,
        "provider": settings.provider,
        "prompt_sha256": {
            name: hashlib.sha256(getattr(prompts, name).encode()).hexdigest()
            for name in ("INPUT_MODE_ROUTER_PROMPT", "EDIT_FRAGMENT_PROMPT", "EDIT_FULL_TEXT_PROMPT")
        },
        "passed": sum(result["passed"] for result in results),
        "total": len(results),
        "results": sorted(results, key=lambda result: (result["id"], result["phase"])),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Passed {report['passed']}/{report['total']}", flush=True)
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
