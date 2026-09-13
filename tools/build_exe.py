"""Build a single-file Windows executable with PyInstaller.

    pip install pyinstaller
    python tools/build_exe.py

The result lands in ``dist/剑灵取色宏.exe``.

Two notes worth knowing before you ship the exe to anyone:

* Antivirus engines flag *any* freshly-built PyInstaller binary that calls
  ``SendInput`` and installs keyboard hooks.  That is a heuristic, not a
  verdict.  Building from source on the machine that runs it avoids the whole
  question -- ``python run.py`` needs no build step at all.
* ``--windowed`` hides the console.  Keep it off while you are still
  debugging, or you will not see tracebacks.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "剑灵取色宏"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401  (presence check only)
    except ImportError:
        print("缺少 pyinstaller：pip install pyinstaller", file=sys.stderr)
        return 1

    for stale in ("build", "dist"):
        shutil.rmtree(ROOT / stale, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", NAME,
        "--paths", str(ROOT),
        # profiles are user data; they live next to the exe, not inside it
        "--add-data", f"{ROOT / 'profiles'};profiles",
        str(ROOT / "run.py"),
    ]
    icon = ROOT / "docs" / "icon.ico"
    if icon.is_file():
        cmd += ["--icon", str(icon)]

    print(" ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        print(f"\n完成 -> {ROOT / 'dist' / (NAME + '.exe')}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
