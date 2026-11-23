"""Command line: ``python -m tank_transfer.quantize --input JOB --output DATASET``.

Reads an offline quantization JOB, converts the selected species of a
complete v1 plan into a complete integer-unit ledger and atomically publishes
a fresh DATASET.  Every ledger row (each initial tank, every transfer, every
per-step closing tank) is posted as the floor or the ceil of its exact unit
value; already-posted ``pins`` are honoured exactly.  A valid JOB with no
ledger satisfying the posting rules and every pin yields exactly
``{"status": "conflict"}`` and never a computable prefix.  Both business
results exit 0; an invalid JOB (including a physically impossible plan) or an
I/O failure exits 2 with the reason on stderr and publishes nothing.  An
existing output path (regular file or symlink) is never overwritten.
"""

import argparse
import json
import sys

from .io import load_bounded, publish
from .quantize_job import MAX_JOB_BYTES, JobError, parse_job
from .quantize_solve import solve


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(f"usage: {message}")


def _parser():
    parser = _Parser(prog="python -m tank_transfer.quantize")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    return parser


def run(input_path, output_path):
    data = load_bounded(input_path, MAX_JOB_BYTES)
    job = parse_job(data)
    result = solve(job)
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
    if result["status"] == "balanced":
        summary["total_units"] = result["total_units"]
        summary["absolute_error_units"] = result["absolute_error_units"]
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
