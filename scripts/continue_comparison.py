"""Continue the summary comparison using a proven baseline from identical synthetic inputs.

Original failed runs remain immutable; only a new comparison checkpoint is written.
Input config carries runtime key via UTF-8 stdin. No memory question is called again.
"""

import asyncio
import json
import sys
from pathlib import Path

from evaluate_models import Checkpoint, evaluate, profile_for, run_id


def prepare(config):
    output = Path(config["output_path"])
    if output.exists():
        return

    def latest(path):
        result = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            result[(row["case_id"], row["strategy"], row["repeat"])] = row
        return result

    previous = latest(config.pop("memory_records"))
    baseline = latest(config.pop("successful_baseline_records"))[("__baseline__", "summary", 0)]
    if baseline["status"] != "ok" or not baseline["answer"]:
        raise ValueError("No proven baseline")
    checkpoint = Checkpoint(output, run_id(profile_for(config)))
    checkpoint.append(baseline)
    for row in previous.values():
        if row["strategy"] == "memory":
            checkpoint.append(row)


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    try:
        config = json.load(sys.stdin)
        prepare(config)
        print(json.dumps(asyncio.run(evaluate(config)), ensure_ascii=True))
    except Exception:  # noqa: BLE001 -- never emit credentials or full upstream exceptions
        print(json.dumps({"failed": "comparison_error"}))
        raise SystemExit(1)
