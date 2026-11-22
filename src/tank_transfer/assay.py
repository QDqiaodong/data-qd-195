"""Command line: ``python -m tank_transfer.assay plan|verify``.

``plan`` validates a JOB, decides the target level (or the one-probe decision)
and atomically publishes a fresh report.  ``verify`` re-checks a report
against the JOB without touching either file and without importing the
planner's decision logic, feasibility search or optimiser.

Exit code 2 means invalid business input, an I/O failure or a failed
verification; a report is only published on success.  A valid but
contradictory observation set is a business result: ``plan`` still writes the
``inconsistent`` report and exits 0.
"""

import argparse
import sys

from .assay_input import MAX_REPORT_BYTES, canonical_json
from .assay_physics import JobError, parse_job
from .assay_verify import VerificationError, verify
from .io import load_bounded, publish


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(f"usage: {message}")


def _parser():
    parser = _Parser(prog="python -m tank_transfer.assay")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--input", required=True)
    plan.add_argument("--output", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--input", required=True)
    verify_parser.add_argument("--report", required=True)
    return parser


def _plan(args):
    # Importing the decision module lazily keeps ``verify`` free of planner
    # code paths entirely.
    from .assay_decision import build_report
    job = parse_job(args.input)
    report = build_report(job)
    publish(args.output, report)
    return report


def _verify(args):
    job = parse_job(args.input)
    try:
        report = load_bounded(args.report, MAX_REPORT_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise VerificationError(f"cannot read report: {exc}")
    return verify(job, report)


def _summary(command, result):
    summary = {"status": result["status"]}
    if result.get("classified_level") is not None:
        summary["classified_level"] = result["classified_level"]
    if result.get("recommendation"):
        summary["probe"] = result["recommendation"]["probe"]
    if command == "verify":
        summary["verified"] = True
        summary["baseline_levels"] = result["baseline_levels"]
        summary["candidates"] = result["candidates"]
        if result.get("recommended") is not None:
            summary["recommended"] = result["recommended"]
    return summary


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        if args.command == "plan":
            result = _plan(args)
        elif args.command == "verify":
            result = _verify(args)
        else:  # pragma: no cover - argparse enforces the choices
            raise ValueError("unknown command")
        summary = _summary(args.command, result)
    except (JobError, VerificationError, ValueError, OSError, UnicodeError,
            RecursionError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(canonical_json(summary) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
