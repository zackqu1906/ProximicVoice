from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv=None):
    parser=argparse.ArgumentParser(description="Ring Whisper Lab: dual-ring capture and alignment")
    parser.add_argument("--data-dir",type=Path,default=Path.cwd()/"data")
    sub=parser.add_subparsers(dest="command")
    sub.add_parser("gui",help="Open desktop capture and listening app (default)")
    sub.add_parser("scan",help="Scan devices without connecting or recording")
    sub.add_parser("demo",help="Create and align synthetic audio without hardware")
    align=sub.add_parser("align",help="Align a saved take, preserving the original")
    align.add_argument("take",type=Path)
    align.add_argument("--offset-ms",type=float)
    align.add_argument("--drift-ppm",type=float)
    export=sub.add_parser("export",help="Export accepted, QA-passing takes")
    export.add_argument("--segment-seconds",type=float,default=4.)
    args=parser.parse_args(argv)
    if args.command=="scan":
        import asyncio
        from .capture import DualCapture
        devices=asyncio.run(DualCapture(lambda *args:None).scan())
        print(json.dumps(devices,ensure_ascii=False,indent=2))
    elif args.command=="demo":
        from .demo import make_demo
        print(make_demo(args.data_dir.resolve()))
    elif args.command=="align":
        from .alignment import align_take
        print(json.dumps(align_take(args.take.resolve(),manual_offset_ms=args.offset_ms,
                                   manual_drift_ppm=args.drift_ppm),ensure_ascii=False,indent=2))
    elif args.command=="export":
        from .dataset import export_dataset
        print(export_dataset(args.data_dir.resolve(),segment_s=args.segment_seconds))
    else:
        from PySide6.QtWidgets import QApplication
        from .ui import MainWindow
        app=QApplication([])
        app.setApplicationName("Ring Whisper Lab")
        app.setOrganizationName("RingWhisperLab")
        app.setStyle("Fusion")
        window=MainWindow(args.data_dir)
        window.show()
        return app.exec()
    return 0


if __name__=="__main__":
    raise SystemExit(main())
