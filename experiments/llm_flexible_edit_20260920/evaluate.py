"""Explicit live prompt check with synthetic inputs; never writes to an editor.

Uses the app's configured model. Credentials stay in memory. Length bands are
evaluation observations, not production rejection rules. Review prose manually.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from PySide6.QtCore import QSettings
from proximic_ring.text_processing import LLMSettings, OpenAICompatibleTextProcessor
from proximic_ring.text_processing.prompts import EDIT_FRAGMENT_PROMPT, EDIT_FULL_TEXT_PROMPT

HERE = Path(__file__).resolve().parent
CASES = [
    dict(id="insert", source="周五开会。", instruction="在开会前面加上下午三点", expected="周五下午三点开会。"),
    dict(id="delete_last", source="保留第一句。删掉这一句。", instruction="删掉最后一句话", expected="保留第一句。"),
    dict(id="delete_repeated", source="好的。我们明天见。好的。", instruction="删掉最后一句", expected="好的。我们明天见。"),
    dict(id="replace_all", source="上午三点开会，下午三点培训。", instruction="把所有三点改成四点", expected="上午四点开会，下午四点培训。"),
    dict(id="homophone", source="事情效果还不错。", instruction="识别", expected="识别效果还不错。"),
    dict(id="correction_scope", source="我们用心模型处理文本。", instruction="新模型", expected="我们用新模型处理文本。"),
    dict(id="character_explanation", source="这个界面还不错。", instruction="街道的街面包的面", expected="这个街面还不错。"),
    dict(id="name", source="我叫黎静。", instruction="木子李，安静的静", expected="我叫李静。"),
    dict(id="spelling", source="请安装 numby，然后运行程序。", instruction="英文词拼作 n u m p y，全小写", expected="请安装 numpy，然后运行程序。"),
    dict(id="spelling_symbols", source="文件叫 data-v1.csv。", instruction="文件名改成 d a t a 下划线 v 2 点 c s v，全小写", expected="文件叫 data_v2.csv。"),
    dict(id="missing_target", source="明天去买菜。", instruction="英文词拼作 n u m p y", expected="明天去买菜。"),
    dict(id="ambiguous", source="上午三点开会，下午三点培训。", instruction="四点", expected="上午三点开会，下午三点培训。"),
    dict(id="polish", source="公司里面现在就是可能还没有这个计划。", instruction="润色一下", contains=["可能", "没有"], changed=True),
    dict(id="format", source="苹果、香蕉、橙子", instruction="改成三行，每行一个词，不加编号", expected="苹果\n香蕉\n橙子"),
    dict(id="expand_digits", source="67890。", instruction="扩写到20个字", length=[16,24], compare=True),
    dict(id="expand_review", source="这套输入法用起来很方便。", instruction="扩写到80个字", length=[64,96], compare=True),
    dict(id="creative", source="小猫回家了。", instruction="扩写成100字左右的小故事", length=[80,120], compare=True),
    dict(id="notice", source="图书馆周五闭馆一天。", instruction="扩写成60字左右的通知，不增加其他安排", length=[48,72], contains=["周五", "一天"], compare=True),
    dict(id="translate", source="我们可能不会在周五发布更新。", instruction="翻译成英文", contains=["Friday"], changed=True),
    dict(id="clear", source="这段内容不需要了。", instruction="清空全文", expected=""),
    dict(id="holdout_spelling", source="我们使用 matplotlip 画图。", instruction="库名拼作 m a t p l o t l i b，全小写", expected="我们使用 matplotlib 画图。"),
    dict(id="holdout_name", source="请联系陈清。", instruction="名字是晴天的晴", expected="请联系陈晴。"),
    dict(id="holdout_delete", source="谢谢。材料已收到。谢谢。", instruction="把最后一句删掉", expected="谢谢。材料已收到。"),
    dict(id="holdout_replace_first", source="先说你好，再说你好。", instruction="第一个你好改成早上好", expected="先说早上好，再说你好。"),
    dict(id="holdout_missing", source="这束花很好看。", instruction="英文词改成 p a n d a s", expected="这束花很好看。"),
    dict(id="holdout_ambiguous", source="小王周一来，小李周一来。", instruction="周二", expected="小王周一来，小李周一来。"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "results.json")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    cases = [case for case in CASES if not args.case or case["id"] in args.case]
    if not cases or (args.case and len(cases) != len(set(args.case))): parser.error("Unknown case")
    if args.output.exists(): parser.error("Choose a new output path to preserve previous results")
    saved = QSettings("ProxiMic", "ProxiMic Voice")
    settings = LLMSettings(enabled=True, provider=str(saved.value("llm/provider", "")),
        base_url=str(saved.value("llm/baseUrl", "")), model=str(saved.value("llm/model", "")),
        api_key=str(saved.value("llm/apiKey", "")), api_key_env=str(saved.value("llm/apiKeyEnv", "")), timeout_s=35)
    settings.validate()
    variants = {"before": json.loads((HERE / "before_prompts.json").read_text()),
                "after": {"fragment": EDIT_FRAGMENT_PROMPT, "full": EDIT_FULL_TEXT_PROMPT}}
    report = {"at": datetime.now(timezone.utc).isoformat(), "model": settings.model,
        "cases": cases, "prompt_hashes": {name: {k: hashlib.sha256(v.encode()).hexdigest()
            for k,v in prompts.items()} for name,prompts in variants.items()}, "results": []}
    args.output.with_suffix(".prompts.json").write_text(json.dumps(variants["after"], ensure_ascii=False, indent=2) + "\n")

    def run(case, variant, branch):
        started = time.perf_counter()
        row = {"case": case["id"], "variant": variant, "branch": branch}
        try:
            result, outputs = OpenAICompatibleTextProcessor()._process_edit_with_retry(settings,
                target=case["source"], user_content=f"<待修改文本>\n{case['source']}\n</待修改文本>\n\n<修改要求>\n{case['instruction']}\n</修改要求>",
                system_prompt=variants[variant][branch], max_tokens=32768, edit_mode=branch,
                max_attempts=1, instruction=case["instruction"])
            count = len(re.sub(r"\s", "", result))
            failures = []
            if "expected" in case and result != case["expected"]: failures.append("exact_text")
            if case.get("changed") and result == case["source"]: failures.append("unchanged")
            for word in case.get("contains", []):
                if word.lower() not in result.lower(): failures.append("missing:" + word)
            if "length" in case and not case["length"][0] <= count <= case["length"][1]: failures.append("length_band")
            row.update(result=result, characters=count, raw_outputs=outputs, failures=failures)
        except Exception as exc:
            row.update(error_type=type(exc).__name__, failures=["request_or_protocol_error"])
        row["elapsed_s"] = round(time.perf_counter() - started, 3)
        return row

    jobs = [(case,variant,branch) for case in cases
            for variant in (["before", "after"] if args.baseline and case.get("compare") else ["after"])
            for branch in ("fragment", "full")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(run,*job) for job in jobs]):
            row = future.result(); report["results"].append(row)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({k:v for k,v in row.items() if k != "raw_outputs"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
