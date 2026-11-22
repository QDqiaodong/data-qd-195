"""Planner: decide reachability and choose one qualifying assay probe.

This module contains the decision logic and the feasibility search.  The
verifier (``assay_verify``) never imports it.
"""

from .assay_evidence import allowed_constraints
from .assay_input import LEVELS, fraction_text, job_fingerprint
from .assay_search import solve_system

SCHEMA = "tank-transfer-assay/1"


def _position_json(position):
    return {"step": position["step"], "tank": position["tank"]}


def _evidence(entry):
    if entry["reachable"]:
        return {"reachable": True, "witness": entry["witness"]}
    return {"reachable": False, "proof": entry["proof"]}


def _level_scope(job, level, probe=None, bucket=None):
    return allowed_constraints(job, level=level, probe=probe, bucket=bucket)


def analyse_candidate(job, probe):
    buckets = []
    for bucket in range(len(probe["cuts"]) + 1):
        levels = {}
        for level in LEVELS:
            constraints = _level_scope(job, level, probe, bucket)
            levels[level] = solve_system(job, constraints)
        reachable_levels = [level for level in LEVELS
                            if levels[level]["reachable"]]
        buckets.append({
            "bucket": bucket,
            "reachable": bool(reachable_levels),
            "reachable_levels": reachable_levels,
            "levels": {level: _evidence(levels[level]) for level in LEVELS},
        })
    qualified = all(len(entry["reachable_levels"]) != 2 for entry in buckets)
    return {
        "id": probe["id"],
        "cost": probe["cost"],
        "at": _position_json(probe["at"]),
        "cuts_g_per_l": [fraction_text(cut) for cut in probe["cuts"]],
        "buckets": buckets,
        "qualified": qualified,
    }


def _recommendation(candidate):
    bucket_levels = []
    for entry in candidate["buckets"]:
        level = entry["reachable_levels"][0] if entry["reachable_levels"] else None
        bucket_levels.append({"bucket": entry["bucket"], "level": level})
    return {"probe": candidate["id"], "bucket_levels": bucket_levels}


def build_report(job):
    baseline = {}
    for level in LEVELS:
        baseline[level] = solve_system(job, _level_scope(job, level))
    reachable = [level for level in LEVELS if baseline[level]["reachable"]]
    report = {
        "schema": SCHEMA,
        "job_fingerprint": job_fingerprint(job.data),
        "species": job.species,
        "unknown_tanks": list(job.unknown_tanks),
        "target": {"at": _position_json(job.target_at),
                   "limit_g_per_l": fraction_text(job.target_limit)},
        "baseline": {level: _evidence(baseline[level]) for level in LEVELS},
        "baseline_levels": reachable,
        "classified_level": None,
        "candidates": [],
        "recommendation": None,
    }
    if not reachable:
        report["status"] = "inconsistent"
        return report
    if len(reachable) == 1:
        report["status"] = "classified"
        report["classified_level"] = reachable[0]
        return report

    # Both levels are reachable, so a single extra reading is required.
    candidates = [analyse_candidate(job, probe) for probe in job.probes]
    report["candidates"] = candidates
    qualifying = [candidate for candidate in candidates if candidate["qualified"]]
    if qualifying:
        chosen = min(qualifying, key=lambda candidate: (candidate["cost"],
                                                        candidate["id"]))
        report["status"] = "probe"
        report["recommendation"] = _recommendation(chosen)
    else:
        report["status"] = "unresolved"
    return report
