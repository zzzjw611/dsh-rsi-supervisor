"""Minimal implementation of the loopgraph.harness.v1 JSON envelope."""

from __future__ import annotations

import json
import sys


def main() -> None:
    envelope = json.load(sys.stdin)
    request = envelope["request"]
    response = {
        "output": (
            f"Completed {request['goal']} in {request['workspace']} "
            f"(iteration {request['iteration']})"
        ),
        "artifact_ref": f"example://{request['run_id']}/{request['step_id']}",
        "metadata": {"protocol_version": envelope["protocol_version"]},
    }
    json.dump(response, sys.stdout)


if __name__ == "__main__":
    main()
