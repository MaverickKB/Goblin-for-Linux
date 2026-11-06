#!/usr/bin/env python3
"""Run the engine and frontend checks with one command.

The engine suite must not inherit the app credential helper paths. A test that
mints a real provider lease is not a test. This script clears those paths, runs
the engine suite, then runs the frontend suite.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def find_python():
    requested = os.environ.get("GOBLIN_CHECK_PYTHON", "")
    if requested:
        return requested
    if sys.version_info >= (3, 11):
        return sys.executable
    raise SystemExit("No Python 3.11 or later interpreter found. Set GOBLIN_CHECK_PYTHON.")


def check_environment():
    env = os.environ.copy()
    env.pop("GOBLIN_KEYCHAIN_HELPER", None)
    env.pop("GOBLIN_RESOURCES", None)
    return env


def run(command, env):
    print("+ " + " ".join(command))
    completed = subprocess.run(command, cwd=ROOT, env=env)
    return completed.returncode == 0


def main():
    env = check_environment()
    python = find_python()
    engine_ok = run([python, "-m", "unittest", "discover", "-s", "Tests/Python"], env)

    js_tests = sorted(str(path) for path in (ROOT / "Tests" / "js").glob("*.test.mjs"))
    frontend_ok = True
    if js_tests:
        node = shutil.which("node")
        if node is None:
            print("Node 20 or later is required for the frontend suite.")
            return 1
        frontend_ok = run([node, "--test", *js_tests], env)

    if not engine_ok or not frontend_ok:
        print("checks failed")
        return 1
    print("checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
