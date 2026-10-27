"""Run independent business and release-integrity tests in isolated temporary workspaces."""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
from support import ROOT, prepare_inputs, safe_text

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundary", action="store_true", help="Run only exact-boundary cases")
    args = parser.parse_args()
    prepare_inputs()
    command = [sys.executable, "-m", "pytest", "tests", "-q", "--tb=short"]
    if args.boundary:
        command.extend(["-k", "exact_sla_boundary"])
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    print(safe_text(result.stdout), end="")
    print(safe_text(result.stderr), end="", file=sys.stderr)
    return result.returncode

if __name__ == "__main__":
    raise SystemExit(main())
