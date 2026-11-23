"""Command line: ``python -m tank_transfer.recipe --input JOB --output RESULT``.

Reads an offline recipe-planning JOB, chooses the cheapest ordered subset of
its optional transfer actions whose execution reaches every final
concentration target while keeping every reserve, and atomically publishes a
fresh RESULT.

The winning sequence minimises total ``cost``, then the number of actions,
then the list of action ids compared character by character.  A planned
result carries a standalone v1 ``plan`` that the original ``tank-transfer``
entry simulates directly, plus the exact ``final`` volumes, masses and
concentrations.  A valid JOB with no feasible plan yields exactly
``{"status": "no_plan"}``.  Both business results exit 0; an invalid JOB, a
physics failure or an I/O failure exits 2 with the reason on stderr and
publishes nothing.  An existing output path (regular file or symlink) is
never overwritten.
"""

import argparse
import json
import sys

from .io import load_bounded, publish
from .recipe_job import MAX_JOB_BYTES, JobError, parse_job
from .recipe_search import solve
from .service import process


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(f"usage: {message}")


def _parser():
    parser = _Parser(prog="python -m tank_transfer.recipe")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    return parser


def build_plan(job, action_ids):
    """Assemble the standalone v1 plan for the winning action order."""
    actions = {action.id: action for action in job.actions}
    steps = []
    for index, aid in enumerate(action_ids, 1):
        action = actions[aid]
        steps.append({
            "id": f"s{index}",
            "transfers": [{
                "id": action.id,
                "source": action.source,
                "target": action.target,
                "volume_l": action.volume_text,
            }],
        })
    return {"species": job.species_json, "tanks": job.tanks_json,
            "steps": steps}


def build_final(job, report):
    """Exact closing readings for every target tank and reserve tank."""
    target_species = {}
    for target in job.targets:
        target_species.setdefault(target.tank, set()).add(target.species)
    tanks = sorted(set(target_species) | {reserve.tank
                                          for reserve in job.reserves})
    final = {}
    for tank in tanks:
        closing = report["final"][tank]
        entry = {"volume_l": closing["volume_l"]}
        species = sorted(target_species.get(tank, ()))
        if species:
            entry["species"] = {
                name: {
                    "mass_g": closing["mass_g"][name],
                    "concentration_g_per_l":
                        closing["concentration_g_per_l"][name],
                }
                for name in species
            }
        final[tank] = entry
    return final


def plan(job):
    """Return the business result for a validated job."""
    outcome = solve(job)
    if outcome is None:
        return {"status": "no_plan"}
    action_ids, _state = outcome
    plan_document = build_plan(job, action_ids)
    # Re-run the winner through the original v1 simulator: it must execute,
    # and its exact closing readings are the published final values.
    report = process(plan_document)
    return {
        "status": "planned",
        "total_cost": sum(action.cost for action in job.actions
                          if action.id in set(action_ids)),
        "action_ids": list(action_ids),
        "plan": plan_document,
        "final": build_final(job, report),
    }


def run(input_path, output_path):
    data = load_bounded(input_path, MAX_JOB_BYTES)
    job = parse_job(data)
    result = plan(job)
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
    if result["status"] == "planned":
        summary["total_cost"] = result["total_cost"]
        summary["actions"] = len(result["action_ids"])
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
