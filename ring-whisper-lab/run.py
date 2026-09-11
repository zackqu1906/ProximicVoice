"""Source-checkout launcher, also reuses local libopus when available."""
import os
from pathlib import Path
import sys

project=Path(__file__).resolve().parent
sys.path.insert(0,str(project/"src"))
for candidate in (project/".runtime/opus/lib",project.parent/".runtime/opus/lib"):
    if (candidate/"libopus.0.dylib").is_file():
        os.environ.setdefault("PROXIMIC_OPUS_DIR",str(candidate))
        break

from ring_whisper_lab.__main__ import main

if __name__=="__main__":
    # Default data lives with this project even when launched from Finder.
    args=sys.argv[1:]
    if "--data-dir" not in args:
        args=["--data-dir",str(project/"data")]+args
    raise SystemExit(main(args))
