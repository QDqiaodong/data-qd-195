"""Command line: ``python -m tank_transfer.dispatch --input JOB --output RESULT``.

Reads an offline pump work-order JOB, finds the shortest schedule within
``tick_limit`` (ties broken by the lexicographically smallest sequence of
per-tick sorted order-id lists) and atomically publishes a fresh RESULT.

A schedulable job yields ``{"status": "scheduled", "plan": PLAN,
"accept_readings_g_per_l": READINGS}`` where PLAN is a standalone v1 document
that the original ``tank-transfer`` entry simulates directly.  A valid job
with no schedule yields ``{"status": "unschedulable"}`` and nothing else.
Both business results exit 0.  An invalid JOB or an I/O failure exits 2 with
the reason on stderr and never publishes a result; an existing output path
(regular file or symlink) is never overwritten.
"""

import argparse
import json
import sys

from .dispatch_job import MAX_JOB_BYTES, JobError, parse_job
from .dispatch_search import solve
from .io import load_bounded, publish


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(f"usage: {message}")


def _parser():
    parser = _Parser(prog="python -m tank_transfer.dispatch")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    return parser


def build_plan(job, schedule):
    """Assemble the standalone v1 plan for a solved schedule."""
    orders = {order.id: order for order in job.orders}
    steps = []
    for index, tick in enumerate(schedule, 1):
        transfers = []
        for oid in tick:
            order = orders[oid]
            transfers.append({"id": order.id, "source": order.source,
                              "target": order.target,
                              "volume_l": order.volume_text})
        steps.append({"id": f"s{index}", "transfers": transfers})
    return {"species": job.species_json, "tanks": job.tanks_json,
            "steps": steps}


def run(input_path, output_path):
    data = load_bounded(input_path, MAX_JOB_BYTES)
    job = parse_job(data)
    outcome = solve(job)
    if outcome is None:
        result = {"status": "unschedulable"}
    else:
        schedule, readings = outcome
        result = {
            "status": "scheduled",
            "plan": build_plan(job, schedule),
            "accept_readings_g_per_l": {
                oid: str(value) for oid, value in readings.items()},
        }
    publish(output_path, result)
    return result


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        result = run(args.input, args.output)
    except (JobError, ValueError, OSError, UnicodeError, RecursionError,
            RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    summary = {"status": result["status"]}
    if result["status"] == "scheduled":
        summary["ticks"] = len(result["plan"]["steps"])
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
