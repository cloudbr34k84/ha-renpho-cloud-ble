"""Require 100% statement coverage in every integration module."""

import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
report = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json").read_text())
failed = []
for module in sorted((root / "custom_components/renpho").glob("*.py")):
    name = module.relative_to(root).as_posix()
    entry = report["files"].get(name)
    if entry is None:
        failed.append(f"{name}: missing coverage")
        continue
    summary = entry["summary"]
    total = summary["num_statements"]
    covered = summary["covered_lines"]
    percent = 100 * covered / total if total else 100
    print(f"{name}: {percent:.2f}%")
    if covered != total:
        failed.append(f"{name}: must have 100% statement coverage")
if failed:
    sys.exit("\n".join(failed))
