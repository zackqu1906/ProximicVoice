from __future__ import annotations

import csv
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
BACKENDS = ("doubao", "sensevoice", "funasr_nano")
BACKEND_NAMES = {
    "doubao": "豆包 Seed-ASR",
    "sensevoice": "SenseVoice Small",
    "funasr_nano": "Fun-ASR Nano",
}
DEVICE_NAMES = {"dji": "DJI（+9.16 dB）", "ring": "Ring 原声"}


def load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def normalize(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", str(text)).lower()


def edit_distance(reference: str, hypothesis: str) -> tuple[int, int]:
    left, right = normalize(reference), normalize(hypothesis)
    row = list(range(len(right) + 1))
    for index, left_char in enumerate(left, 1):
        next_row = [index]
        for column, right_char in enumerate(right, 1):
            next_row.append(
                min(
                    next_row[-1] + 1,
                    row[column] + 1,
                    row[column - 1] + (left_char != right_char),
                )
            )
        row = next_row
    return row[-1], len(left)


def result_for(cases: dict, suffix: str, backend: str) -> str:
    item = next(value for key, value in cases.items() if key.endswith(suffix))
    return str(item["results"][backend]["text"])


def main() -> None:
    preparation = load("preparation.json")
    sentence_local = load("sentences_local.json")["cases"]
    sentence_doubao = load("sentences_doubao.json")["cases"]
    full_local = load("full_local.json")["cases"]
    full_doubao = load("full_doubao.json")["cases"]
    sentence_sources = {
        "doubao": sentence_doubao,
        "sensevoice": sentence_local,
        "funasr_nano": sentence_local,
    }
    full_sources = {
        "doubao": full_doubao,
        "sensevoice": full_local,
        "funasr_nano": full_local,
    }

    rows: list[dict] = []
    totals = {
        backend: {device: [0, 0, 0, 0] for device in DEVICE_NAMES}
        for backend in BACKENDS
    }
    for sentence in preparation["sentences"]:
        number = int(sentence["sentence"])
        prompt = str(sentence["prompt"])
        for backend in BACKENDS:
            for device in DEVICE_NAMES:
                text = result_for(
                    sentence_sources[backend], f"{number:02d}_{device}.wav", backend
                )
                errors, characters = edit_distance(prompt, text)
                cer = errors / characters if characters else 0.0
                aggregate = totals[backend][device]
                aggregate[0] += errors
                aggregate[1] += characters
                aggregate[2] += int(cer <= 0.25)
                aggregate[3] += int(cer == 0.0)
                rows.append(
                    {
                        "sentence": number,
                        "prompt": prompt,
                        "device": device,
                        "backend": backend,
                        "text": text,
                        "prompt_cer": cer,
                    }
                )

    summary = {}
    for backend in BACKENDS:
        summary[backend] = {}
        for device in DEVICE_NAMES:
            errors, characters, usable, exact = totals[backend][device]
            summary[backend][device] = {
                "prompt_cer": errors / characters if characters else 0.0,
                "sentences_cer_le_25pct": usable,
                "exact_prompt_matches": exact,
                "sentence_count": len(preparation["sentences"]),
            }

    full = {}
    for backend in BACKENDS:
        full[backend] = {
            "dji": result_for(
                full_sources[backend], "dji_loudness_matched.wav", backend
            ),
            "ring": result_for(full_sources[backend], "ring_raw.wav", backend),
        }

    consolidated = {
        "scope": {
            "ring_input": "unprocessed ring_raw.wav; no commercial processing",
            "dji_input": "+9.1598909101 dB linear gain; no denoise or tone processing",
            "asr_format": "16 kHz mono PCM16",
        },
        "preparation": preparation,
        "summary": summary,
        "full_file_results": full,
        "sentence_results": rows,
    }
    (ROOT / "results.json").write_text(
        json.dumps(consolidated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with (ROOT / "逐句识别.csv").open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# DJI 与 Ring 原声：三模型 ASR 对比",
        "",
        "Ring 输入是采集得到的 `ring_raw.wav`，没有经过商业软件或降爆破处理。DJI 仅做线性响度匹配（+9.1599 dB）和 ASR 必需的 16 kHz 单声道重采样；没有降噪、压缩、EQ 或文本修正。",
        "",
        "## 汇总",
        "",
        "CER 是相对采集提示词的字符错误率；说话者若没有逐字朗读，也会被计为错误。`≤25%` 表示九句中相对接近提示词的句数。",
        "",
        "| 模型 | DJI CER | DJI ≤25% | Ring 原声 CER | Ring ≤25% |",
        "|---|---:|---:|---:|---:|",
    ]
    for backend in BACKENDS:
        dji = summary[backend]["dji"]
        ring = summary[backend]["ring"]
        lines.append(
            f"| {BACKEND_NAMES[backend]} | {dji['prompt_cer']:.1%} | "
            f"{dji['sentences_cer_le_25pct']}/9 | {ring['prompt_cer']:.1%} | "
            f"{ring['sentences_cer_le_25pct']}/9 |"
        )
    lines.extend(["", "Fun-ASR 的 Ring CER 被第 4 段的长重复幻觉放大；其九句 CER 中位数仍为 100%。", ""])

    lines.extend(["## 完整文件一次识别", ""])
    for backend in BACKENDS:
        lines.extend(
            [
                f"### {BACKEND_NAMES[backend]}",
                "",
                f"- DJI：{full[backend]['dji'] or '（空）'}",
                f"- Ring 原声：{full[backend]['ring'] or '（空）'}",
                "",
            ]
        )

    lines.extend(["## 逐句原始输出", ""])
    for sentence in preparation["sentences"]:
        number = int(sentence["sentence"])
        lines.extend([f"### {number}. {sentence['prompt']}", ""])
        lines.extend(
            [
                "| 模型 | DJI | Ring 原声 |",
                "|---|---|---|",
            ]
        )
        for backend in BACKENDS:
            dji = next(
                row["text"]
                for row in rows
                if row["sentence"] == number
                and row["backend"] == backend
                and row["device"] == "dji"
            )
            ring = next(
                row["text"]
                for row in rows
                if row["sentence"] == number
                and row["backend"] == backend
                and row["device"] == "ring"
            )
            lines.append(
                f"| {BACKEND_NAMES[backend]} | {dji or '（空）'} | {ring or '（空）'} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 文件与方法",
            "",
            f"- DJI 原始全文件 RMS：{preparation['source']['dji']['rms_dbfs']:.2f} dBFS；Ring 原声：{preparation['source']['ring']['rms_dbfs']:.2f} dBFS。",
            f"- DJI 应用增益：{preparation['loudness_match']['applied_gain_db']:.4f} dB；输出峰值：{preparation['loudness_match']['output_peak_dbfs']:.2f} dBFS。",
            "- 三模型均使用主项目 `tools/compare_asr_audio.py` 和实际后端；豆包按主应用的 20 ms、实时节奏上传。",
            "- 完整文件结果容易受长静音、同步提示音和模型长音频截断影响，因此逐句结果是主要比较依据。",
            "",
        ]
    )
    (ROOT / "识别对比.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
