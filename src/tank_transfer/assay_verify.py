"""Independent verification of an assay report.

The verifier reads only the JOB and the published REPORT.  It rebuilds the
original constraints from the JOB, directly re-checks every witness and every
weighted contradiction, and then derives classification, coverage and the
recommendation from those checked facts.  It never imports the planner's
decision logic, the feasibility search or the exact optimiser, and it never
regenerates the report for byte comparison.
"""

from fractions import Fraction

from .assay_evidence import (
    EvidenceError, allowed_constraints, check_proof, check_witness)
from .assay_input import (
    LEVELS, fraction_text, job_fingerprint, proof_fraction)

SCHEMA = "tank-transfer-assay/1"

_TOP_KEYS = {"schema", "job_fingerprint", "species", "unknown_tanks", "target",
             "baseline", "baseline_levels", "status", "classified_level",
             "candidates", "recommendation"}
_STATUSES = {"inconsistent", "classified", "probe", "unresolved"}
_CANDIDATE_KEYS = {"id", "cost", "at", "cuts_g_per_l", "buckets", "qualified"}
_BUCKET_KEYS = {"bucket", "reachable", "reachable_levels", "levels"}
_RECOMMENDATION_KEYS = {"probe", "bucket_levels"}
_BUCKET_LEVEL_KEYS = {"bucket", "level"}


class VerificationError(ValueError):
    """The report does not follow from verified facts (exit 2)."""


def _object(value, keys, label):
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be an object")
    if set(value) != keys:
        raise VerificationError(f"{label} has extra or missing fields")
    return value


def _list(value, label):
    if not isinstance(value, list):
        raise VerificationError(f"{label} must be an array")
    return value


def _position(value, expected, label):
    _object(value, {"step", "tank"}, label)
    if value["step"] != expected["step"] or value["tank"] != expected["tank"]:
        raise VerificationError(f"{label} does not match the JOB")


def _level_set(value, label):
    levels = _list(value, label)
    if any(level not in LEVELS for level in levels):
        raise VerificationError(f"{label} contains an unknown level")
    if len(set(levels)) != len(levels):
        raise VerificationError(f"{label} repeats a level")
    expected = [level for level in LEVELS if level in levels]
    if levels != expected:
        raise VerificationError(f"{label} must use canonical level order")
    return levels


def _check_evidence(job, constraints, evidence, label):
    if not isinstance(evidence, dict) or "reachable" not in evidence:
        raise VerificationError(f"{label} must declare reachability")
    reachable = evidence["reachable"]
    if reachable is True:
        _object(evidence, {"reachable", "witness"}, label)
        try:
            check_witness(job, constraints, evidence["witness"])
        except EvidenceError as exc:
            raise VerificationError(f"{label}: {exc}")
        return True
    if reachable is False:
        _object(evidence, {"reachable", "proof"}, label)
        try:
            check_proof(job, constraints, evidence["proof"])
        except EvidenceError as exc:
            raise VerificationError(f"{label}: {exc}")
        return False
    raise VerificationError(f"{label} has a non-boolean reachability")


def _scope(job, level, probe=None, bucket=None):
    return allowed_constraints(job, level=level, probe=probe, bucket=bucket)


def _check_baseline(job, report):
    baseline = _object(report["baseline"], set(LEVELS), "baseline")
    reachable = []
    for level in LEVELS:
        constraints = _scope(job, level)
        if _check_evidence(job, constraints, baseline[level],
                           f"baseline {level}"):
            reachable.append(level)
    return reachable


def _check_probe(candidate, probe, job):
    _object(candidate, _CANDIDATE_KEYS, f"candidate {probe['id']}")
    if candidate["id"] != probe["id"]:
        raise VerificationError("candidate id does not match its probe")
    if type(candidate["cost"]) is not int or candidate["cost"] != probe["cost"]:
        raise VerificationError("candidate cost does not match the JOB")
    _position(candidate["at"], probe["at"], "candidate position")
    cuts = _list(candidate["cuts_g_per_l"], "candidate cuts")
    expected_cuts = [fraction_text(cut) for cut in probe["cuts"]]
    if len(cuts) != len(expected_cuts):
        raise VerificationError("candidate cut count does not match the JOB")
    for value, expected in zip(cuts, expected_cuts):
        try:
            parsed = proof_fraction(value)
        except ValueError as exc:
            raise VerificationError(f"candidate cut: {exc}")
        if parsed != Fraction(expected):
            raise VerificationError("candidate cut does not match the JOB")

    buckets = _list(candidate["buckets"], "candidate buckets")
    if len(buckets) != len(probe["cuts"]) + 1:
        raise VerificationError("candidate bucket count is wrong")
    reachable_sets = []
    for index, entry in enumerate(buckets):
        _object(entry, _BUCKET_KEYS, f"candidate {probe['id']} bucket {index}")
        if type(entry["bucket"]) is not int or entry["bucket"] != index:
            raise VerificationError("bucket indexes must be 0..cuts in order")
        levels = _object(entry["levels"], set(LEVELS),
                         f"candidate {probe['id']} bucket {index} levels")
        observed = []
        for level in LEVELS:
            constraints = _scope(job, level, probe, index)
            if _check_evidence(job, constraints, levels[level],
                               f"candidate {probe['id']} bucket {index} "
                               f"{level}"):
                observed.append(level)
        reported = _level_set(entry["reachable_levels"],
                              f"candidate {probe['id']} bucket {index}")
        if observed != reported:
            raise VerificationError(
                f"candidate {probe['id']} bucket {index} reachable levels "
                "do not match the evidence")
        if entry["reachable"] is not bool(observed):
            raise VerificationError(
                f"candidate {probe['id']} bucket {index} reachability flag "
                "is not derived from the evidence")
        reachable_sets.append(observed)

    qualified = all(len(levels) != 2 for levels in reachable_sets)
    if candidate["qualified"] is not qualified:
        raise VerificationError(
            f"candidate {probe['id']} qualification is not derived from its "
            "buckets")
    return reachable_sets, qualified


def _check_candidates(job, report):
    candidates = _list(report["candidates"], "candidates")
    probes = {probe["id"]: probe for probe in job.probes}
    if len(candidates) != len(probes):
        raise VerificationError("candidate coverage is not exactly the JOB "
                                "probe set")
    analysis = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or "id" not in candidate:
            raise VerificationError("candidate must be an object with an id")
        probe_id = candidate["id"]
        if probe_id not in probes:
            raise VerificationError(f"candidate {probe_id!r} is not a JOB probe")
        if probe_id in analysis:
            raise VerificationError(f"candidate {probe_id!r} is repeated")
        analysis[probe_id] = _check_probe(candidate, probes[probe_id], job)
    return analysis


def _check_recommendation(job, report, analysis):
    recommendation = report["recommendation"]
    qualifying = [probe_id for probe_id, (_sets, qualified)
                  in analysis.items() if qualified]
    if report["status"] == "unresolved":
        if qualifying:
            raise VerificationError("a qualifying probe exists but status is "
                                    "unresolved")
        if recommendation is not None:
            raise VerificationError("unresolved report must not recommend")
        return
    if report["status"] != "probe":
        raise VerificationError("unexpected status for a two-level baseline")
    if not qualifying:
        raise VerificationError("no qualifying probe but status is probe")
    if recommendation is None:
        raise VerificationError("probe status requires a recommendation")
    _object(recommendation, _RECOMMENDATION_KEYS, "recommendation")
    probe_id = recommendation["probe"]
    if probe_id not in analysis:
        raise VerificationError("recommendation names an unknown candidate")
    reachable_sets, qualified = analysis[probe_id]
    if not qualified:
        raise VerificationError("recommended candidate does not qualify")
    ordered = sorted(qualifying, key=lambda item: (
        next(probe["cost"] for probe in job.probes if probe["id"] == item),
        item))
    if probe_id != ordered[0]:
        raise VerificationError("recommendation is not minimal by cost then id")

    mapping = _list(recommendation["bucket_levels"], "bucket_levels")
    if len(mapping) != len(reachable_sets):
        raise VerificationError("recommendation must cover every bucket")
    for index, entry in enumerate(mapping):
        _object(entry, _BUCKET_LEVEL_KEYS,
                f"recommendation bucket {index}")
        if type(entry["bucket"]) is not int or entry["bucket"] != index:
            raise VerificationError("recommendation buckets must be 0..cuts "
                                    "in order")
        levels = reachable_sets[index]
        if len(levels) == 2:
            raise VerificationError("recommendation covers an ambiguous bucket")
        expected = levels[0] if len(levels) == 1 else None
        if entry["level"] != expected:
            raise VerificationError(
                f"recommendation bucket {index} level is not derived from the "
                "verified reachable set")


def verify(job, report):
    _object(report, _TOP_KEYS, "report")
    if report["schema"] != SCHEMA:
        raise VerificationError("unsupported report schema")
    if report["job_fingerprint"] != job_fingerprint(job.data):
        raise VerificationError("report fingerprint does not match the JOB")
    if report["species"] != job.species:
        raise VerificationError("report species does not match the JOB")
    unknown = _list(report["unknown_tanks"], "unknown_tanks")
    if unknown != list(job.unknown_tanks):
        raise VerificationError("report unknown tanks do not match the JOB")
    _object(report["target"], {"at", "limit_g_per_l"}, "target")
    _position(report["target"]["at"], job.target_at, "target position")
    if report["target"]["limit_g_per_l"] != fraction_text(job.target_limit):
        raise VerificationError("report target limit does not match the JOB")

    reachable = _check_baseline(job, report)
    reported = _level_set(report["baseline_levels"], "baseline_levels")
    if reported != reachable:
        raise VerificationError("baseline levels do not match the evidence")

    status = report["status"]
    if status not in _STATUSES:
        raise VerificationError("unknown report status")
    if len(reachable) == 0:
        if status != "inconsistent":
            raise VerificationError("both levels impossible but status is not "
                                    "inconsistent")
        if report["classified_level"] is not None:
            raise VerificationError("inconsistent report must not classify")
    elif len(reachable) == 1:
        if status != "classified":
            raise VerificationError("one level possible but status is not "
                                    "classified")
        if report["classified_level"] != reachable[0]:
            raise VerificationError("classified level does not match the "
                                    "evidence")
    else:
        if report["classified_level"] is not None:
            raise VerificationError("two possible levels must not classify")
        if status not in ("probe", "unresolved"):
            raise VerificationError("two possible levels need a probe status")

    if status in ("inconsistent", "classified"):
        if report["candidates"] != []:
            raise VerificationError("this status must not publish candidates")
        if report["recommendation"] is not None:
            raise VerificationError("this status must not recommend a probe")
        return {"status": status, "baseline_levels": reachable,
                "candidates": 0}

    analysis = _check_candidates(job, report)
    _check_recommendation(job, report, analysis)
    return {
        "status": status,
        "baseline_levels": reachable,
        "candidates": len(analysis),
        "recommended": (report["recommendation"]["probe"]
                        if report["recommendation"] else None),
    }



