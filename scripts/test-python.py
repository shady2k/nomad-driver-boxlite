#!/usr/bin/env python3
"""Run offline test modules independently to isolate host/plugin import names."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIME = ROOT / "examples/prime"
PLUGIN = PRIME / "claude-plugin/scripts"
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           PYTHONPATH=os.pathsep.join(map(str, (PRIME, PLUGIN))))
failed = []
for directory in (PRIME, ROOT / "tests", ROOT / "provision/tests", ROOT / "images/prime-base"):
    for test in sorted(directory.glob("test_*.py")):
        if test.name == "test_support.py":
            continue
        print(f"Testing {test.relative_to(ROOT)}", flush=True)
        result = subprocess.run([sys.executable, "-m", "unittest", test.stem],
                                cwd=directory, env=env)
        if result.returncode:
            failed.append(str(test.relative_to(ROOT)))
if failed:
    print("Failed modules: " + ", ".join(failed), file=sys.stderr)
    sys.exit(1)
