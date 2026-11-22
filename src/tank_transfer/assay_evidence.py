"""Re-check witnesses and contradiction certificates without any search.

The verifier imports only this module (plus symbolic physics); it never loads
the exact-rational optimiser or the planner's feasibility search.
"""

import json
from fractions import Fraction

from .assay_input import proof_fraction

ZERO = Fraction(0)

_REF_FIELDS = {
    "unknown_min": ("kind", "tank"),
    "unknown_max": ("kind", "tank"),
    "observation_min": ("kind", "id"),
    "observation_max": ("kind", "id"),
    "target_low": ("kind",),
    "target_high": ("kind",),
    "bucket_min": ("kind", "probe", "bucket"),
    "bucket_max": ("kind", "probe", "bucket"),
}


class EvidenceError(ValueError):
    """An evidence object does not prove what it claims."""


def constraint_from_ref(job, ref):
    """Rebuild one original constraint purely from the JOB and a reference."""
    if not isinstance(ref, dict) or "kind" not in ref:
        raise EvidenceError("constraint reference must be an object with kind")
    kind = ref["kind"]
    if kind not in _REF_FIELDS:
        raise EvidenceError("unknown constraint reference kind")
    if set(ref) != set(_REF_FIELDS[kind]):
        raise EvidenceError("constraint reference has extra or missing fields")
    if kind == "unknown_min":
        tank = ref["tank"]
        if tank not in job.unknown_bounds:
            raise EvidenceError("constraint references a non-unknown tank")
        index = job._index[tank]
        coeffs = [ZERO] * len(job.unknown_tanks)
        coeffs[index] = Fraction(-1)
        return _constraint_like({"kind": kind, "tank": tank}, coeffs,
                                -job.unknown_bounds[tank][0], False)
    if kind == "unknown_max":
        tank = ref["tank"]
        if tank not in job.unknown_bounds:
            raise EvidenceError("constraint references a non-unknown tank")
        index = job._index[tank]
        coeffs = [ZERO] * len(job.unknown_tanks)
        coeffs[index] = Fraction(1)
        return _constraint_like({"kind": kind, "tank": tank}, coeffs,
                                job.unknown_bounds[tank][1], False)
    if kind in ("observation_min", "observation_max"):
        observation = _find_observation(job, ref["id"])
        for candidate in job.observation_constraints:
            if candidate.ref["kind"] == kind and candidate.ref["id"] == observation["id"]:
                return candidate
        raise EvidenceError("observation constraint unavailable")
    if kind in ("target_low", "target_high"):
        return job.target_constraint("low" if kind == "target_low" else "high")
    if kind in ("bucket_min", "bucket_max"):
        probe = _find_probe(job, ref["probe"])
        bucket = ref["bucket"]
        if type(bucket) is not int or not 0 <= bucket <= len(probe["cuts"]):
            raise EvidenceError("bucket reference out of range")
        for candidate in job.bucket_constraints(probe, bucket):
            if candidate.ref["kind"] == kind:
                return candidate
        raise EvidenceError("bucket constraint does not exist")
    raise EvidenceError("unknown constraint reference kind")


def _constraint_like(ref, coeffs, rhs, strict):
    from .assay_physics import Constraint
    return Constraint(ref, coeffs, rhs, strict)


def _find_observation(job, observation_id):
    for observation in job.observations:
        if observation["id"] == observation_id:
            return observation
    raise EvidenceError("constraint references an unknown observation")


def _find_probe(job, probe_id):
    for probe in job.probes:
        if probe["id"] == probe_id:
            return probe
    raise EvidenceError("constraint references an unknown probe")


def allowed_constraints(job, level=None, probe=None, bucket=None):
    """The original constraints a conclusion at this scope may cite."""
    constraints = list(job.bound_constraints()) + list(job.observation_constraints)
    if level is not None:
        constraints.append(job.target_constraint(level))
    if probe is not None:
        constraints.extend(job.bucket_constraints(probe, bucket))
    return constraints


def constraint_index(constraints):
    index = {}
    for constraint in constraints:
        key = constraint.key()
        if key in index:
            raise EvidenceError("duplicate constraint reference")
        index[key] = constraint
    return index


# -- mention checking -------------------------------------------------

def check_witness(job, constraints, witness):
    if not isinstance(witness, dict) or set(witness) != {"masses_g"}:
        raise EvidenceError("witness must contain exactly masses_g")
    masses = witness["masses_g"]
    if not isinstance(masses, dict) or set(masses) != set(job.unknown_tanks):
        raise EvidenceError("witness must assign every unknown initial mass")
    values = []
    for tank in job.unknown_tanks:
        minimum, maximum = job.unknown_bounds[tank]
        value = proof_fraction(masses[tank])
        if not minimum <= value <= maximum:
            raise EvidenceError("witness is outside unknown mass bounds")
        values.append(value)
    for constraint in constraints:
        if not constraint.holds(values):
            raise EvidenceError("witness violates an original constraint")
    return values


def check_proof(job, constraints, proof):
    """Re-check a nonnegative weighted contradiction of original rows."""
    if not isinstance(proof, dict) or set(proof) != {"weights", "sum_rhs",
                                                     "strict"}:
        raise EvidenceError("proof must contain weights, sum_rhs and strict")
    weights = proof["weights"]
    if not isinstance(weights, list) or not weights:
        raise EvidenceError("proof needs at least one weighted constraint")
    index = constraint_index(constraints)
    seen = set()
    total = [ZERO] * len(job.unknown_tanks)
    rhs = ZERO
    strict_positive = False
    for item in weights:
        if not isinstance(item, dict) or set(item) != {"constraint", "weight"}:
            raise EvidenceError(
                "weight entry must pair a constraint with a weight")
        reported = item["constraint"]
        if not isinstance(reported, dict) or set(reported) != {
                "ref", "terms", "rhs", "strict"}:
            raise EvidenceError("constraint must have ref, terms, rhs, strict")
        ref = reported["ref"]
        if not isinstance(ref, dict):
            raise EvidenceError("constraint reference must be an object")
        key = _canonical(ref)
        if key in seen:
            raise EvidenceError("duplicate constraint reference in proof")
        seen.add(key)
        constraint = index.get(key)
        if constraint is None:
            raise EvidenceError("proof cites a constraint outside its scope")
        rebuilt = constraint_from_ref(job, ref)
        if (rebuilt.coeffs != constraint.coeffs or rebuilt.rhs != constraint.rhs
                or rebuilt.strict != constraint.strict):
            raise EvidenceError("constraint reference does not match its scope")
        # The stated row must be exactly the row the JOB determines.
        if reported != rebuilt.json(job.unknown_tanks):
            raise EvidenceError(
                "constraint row does not match the original input constraint")
        weight = proof_fraction(item["weight"])
        if weight < 0:
            raise EvidenceError("proof weights must be nonnegative")
        if weight == 0:
            # A zero multiple of a strict row would itself read 0 < 0, so a
            # strict row may only appear with a genuinely positive weight.
            if constraint.strict:
                raise EvidenceError(
                    "a strict constraint needs a positive weight")
            continue
        total = [left + weight * value
                 for left, value in zip(total, constraint.coeffs)]
        rhs += weight * constraint.rhs
        if constraint.strict:
            strict_positive = True
    if any(value != 0 for value in total):
        raise EvidenceError("proof does not cancel every unknown mass")
    if proof["strict"] is not strict_positive:
        raise EvidenceError("proof strictness flag is wrong")
    try:
        reported_rhs = proof_fraction(proof["sum_rhs"])
    except ValueError as exc:
        raise EvidenceError(str(exc))
    if reported_rhs != rhs:
        raise EvidenceError("proof sum_rhs does not match the weights")
    if strict_positive:
        if not rhs <= 0:
            raise EvidenceError("strict combination is not a contradiction")
    elif not rhs < 0:
        raise EvidenceError("non-strict combination is not a contradiction")
    return True


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


