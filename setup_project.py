"""Prepare this Python-only source package before recording. Requires Python 3.12 or newer."""
from __future__ import annotations
import argparse
import subprocess
import sys
import venv
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Create .venv and install pinned pandas and pytest")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if sys.version_info < (3, 12):
        raise SystemExit("Use Python 3.12 or newer.")
    if args.install:
        environment = root / ".venv"
        if not (environment / "bin/python").is_file():
            venv.EnvBuilder(with_pip=True).create(environment)
        subprocess.run([str(environment / "bin/python"), "-m", "pip", "install", "pandas==3.1.0", "pytest==9.1.1"], check=True)
    print("Before recording: select this folder's .venv interpreter in VS Code.")
    print("In the integrated terminal: source .venv/bin/activate")
    print("Set a clean zsh prompt: PROMPT='demo % '; RPROMPT=''")
    print("Then: python 03_workflow.py prepare")
    print("Use the separate Word narration guide for the recording steps and code line references.")
    print("Do all setup before capture. Never display installation logs or personal absolute paths in the take.")

if __name__ == "__main__":
    main()
