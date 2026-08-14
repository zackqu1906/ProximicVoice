"""Stack three ASR screenshots with model labels.

Install Pillow separately for this optional experiment utility:
``python -m pip install pillow``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


DEFAULT_LABELS = ("本地 SenseVoice", "豆包 Seed-ASR", "Fun-ASR-Nano")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs=3, type=Path, help="Three terminal screenshots")
    parser.add_argument("--labels", nargs=3, default=DEFAULT_LABELS)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "asr_results_comparison.png",
    )
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/msyhbd.ttc"))
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    images = [
        (label, Image.open(path).convert("RGB"))
        for label, path in zip(args.labels, args.images, strict=True)
    ]
    width = max(image.width for _, image in images)
    title_height = 104
    gap = 24
    background = (36, 37, 57)
    title_background = (22, 24, 39)
    accent = (86, 182, 255)
    font = ImageFont.truetype(str(args.font), 44)

    total_height = sum(title_height + image.height for _, image in images)
    total_height += gap * (len(images) - 1)
    canvas = Image.new("RGB", (width, total_height), background)
    draw = ImageDraw.Draw(canvas)

    y = 0
    for index, (label, image) in enumerate(images):
        draw.rectangle((0, y, width, y + title_height), fill=title_background)
        draw.rectangle((0, y + title_height - 6, width, y + title_height), fill=accent)
        box = draw.textbbox((0, 0), label, font=font)
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        draw.text(
            ((width - text_width) / 2, y + (title_height - text_height) / 2 - box[1] - 2),
            label,
            font=font,
            fill="white",
        )
        y += title_height
        canvas.paste(image, (0, y))
        y += image.height
        if index != len(images) - 1:
            y += gap

    args.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.output, optimize=True)
    print(args.output)


if __name__ == "__main__":
    main()
