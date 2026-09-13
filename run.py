"""Launcher: double-click this (or `python run.py`) to open the macro."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bnsmacro.ui import run  # noqa: E402

if __name__ == "__main__":
    run(Path(__file__).resolve().parent / "profiles")
