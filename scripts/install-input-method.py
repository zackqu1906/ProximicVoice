"""Source entry point for the same installer bundled in the desktop app."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from proximic_ring.input_method_install import main

if __name__ == "__main__":
    raise SystemExit(main())
