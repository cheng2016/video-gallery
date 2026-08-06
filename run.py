# -*- coding: utf-8 -*-
"""Double-click friendly launcher (avoids broken .bat encoding)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def venv_python_path(root: Path, platform: str | None = None) -> Path:
    platform = platform or sys.platform
    if platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


VENV_PY = venv_python_path(ROOT)
REQ = ROOT / "requirements.txt"
APP = ROOT / "app.py"


def main() -> int:
    os.chdir(ROOT)
    if not APP.is_file():
        print(f"[ERROR] app.py not found: {APP}")
        input("Press Enter to exit...")
        return 1

    if not VENV_PY.is_file():
        print("[1/2] Creating virtualenv ...")
        r = subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        if r.returncode != 0:
            print("[ERROR] venv failed")
            input("Press Enter to exit...")
            return 1
        print("[2/2] Installing dependencies ...")
        subprocess.run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"], check=False)
        r = subprocess.run([str(VENV_PY), "-m", "pip", "install", "-r", str(REQ)])
        if r.returncode != 0:
            print("[ERROR] pip install failed")
            input("Press Enter to exit...")
            return 1

    print()
    print("Starting local video gallery ...")
    print("URL: http://127.0.0.1:8765")
    print("Close this window to stop.")
    print()
    args = [str(VENV_PY), "-u", str(APP), *sys.argv[1:]]
    return subprocess.call(args)


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 0
        print("\nStopped.")
    except Exception as e:
        print(f"[ERROR] {e}")
        code = 1
    if code:
        try:
            input("Press Enter to exit...")
        except EOFError:
            pass
    sys.exit(code)
