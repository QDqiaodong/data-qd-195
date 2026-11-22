"""Planner-side exact feasibility search and contradiction certificates.

This module is imported by the planner only.  The verifier deliberately does
not import it: it re-checks published evidence with
:mod:`tank_transfer.assay_evidence` instead of running a search.

Every linear system is over rationals.  Strict inequalities are handled
without tolerances by maximising a shared slack variable: if the largest
common margin is positive the system has a rational solution (returned as a
witness).  Otherwise a certificate of contradiction is manufactured with
nonnegative weights over the original rows.
"""

from fractions import Fraction

from .assay_evidence import (
    EvidenceError, check_proof, check_witness)
from .assay_input import fraction_text
from .exact_lp import Infeasible, Unbounded, maximize

ZERO = Fraction(0)


def find_solution(job, constraints):
    """Return ``(values or None, proof or None)`` for an exact system."""
    nvars = len(job.unknown_tanks)
    strict_present = any(constraint.strict for constraint in constraints)
    if not strict_present:
        rows, rhs = [], []
        for constraint in constraints:
            rows.append(list(constraint.coeffs))
            rhs.append(constraint.rhs)
        try:
            _value, solution = maximize(nvars, list(zip(rows, rhs,
                                                        ["<="] * len(rows))),
                                        [ZERO] * nvars)
        except Infeasible:
            return None, _certificate(job, constraints)
        except Unbounded:
            raise EvidenceError("internal unbounded feasibility problem")
        return tuple(solution), None

    # Add one shared slack variable epsilon so every strict row has a margin.
    # Bounding epsilon keeps the maximum finite whenever the system is
    # feasible; a strictly positive optimum then means "feasible".
    width = nvars + 1
    rows = []
    for constraint in constraints:
        if constraint.strict:
            rows.append((list(constraint.coeffs) + [Fraction(1)],
                         constraint.rhs))
        else:
            rows.append((list(constraint.coeffs) + [ZERO], constraint.rhs))
    rows.append(([ZERO] * nvars + [Fraction(1)], Fraction(1)))
    objective = [ZERO] * nvars + [Fraction(1)]
    try:
        optimum, solution = maximize(width,
                                     [(coeffs, rhs, "<=") for coeffs, rhs in rows],
                                     objective)
    except Infeasible:
        return None, _certificate(job, constraints)
    except Unbounded:
        raise EvidenceError("internal unbounded margin problem")
    if optimum <= 0:
        return None, _certificate(job, constraints)
    values = tuple(solution[:nvars])
    for constraint in constraints:
        if not constraint.holds(values):
            # The maximised common margin guarantees this; guard anyway.
            raise EvidenceError("internal witness does not satisfy constraints")
    return values, None


def _witness_json(job, values):
    return {"masses_g": {tank: str(value)
                         for tank, value in zip(job.unknown_tanks, values)}}


def _certificate(job, constraints):
    nvars = len(job.unknown_tanks)
    width = len(constraints)
    equations = []
    for dimension in range(nvars):
        equations.append(([constraint.coeffs[dimension]
                           for constraint in constraints], ZERO, "="))
    rhs_row = [constraint.rhs for constraint in constraints]
    strict_columns = [column for column, constraint in enumerate(constraints)
                      if constraint.strict]

    # First look for a combination with a strictly negative constant term.
    case_one = list(equations)
    case_one.append((list(rhs_row), Fraction(-1), "<="))
    solution = _feasible_weights(width, case_one)
    if solution is not None:
        return _proof_json(job, constraints, solution)
    # Otherwise a positive weight on strict rows must turn the bound into <0.
    if strict_columns:
        case_two = list(equations)
        marker = [ZERO] * width
        for column in strict_columns:
            marker[column] = Fraction(1)
        case_two.append((marker, Fraction(1), "="))
        case_two.append((list(rhs_row), ZERO, "<="))
        solution = _feasible_weights(width, case_two)
        if solution is not None:
            return _proof_json(job, constraints, solution)
    raise EvidenceError("infeasible system has no certificate in scope")


def _feasible_weights(width, rows):
    try:
        _value, solution = maximize(width, rows, [ZERO] * width)
    except Infeasible:
        return None
    except Unbounded:
        raise EvidenceError("internal unbounded certificate problem")
    return solution


def _proof_json(job, constraints, weights):
    items = []
    total_rhs = ZERO
    strict_positive = False
    for constraint, weight in zip(constraints, weights):
        if weight <= 0:
            continue
        items.append({"constraint": constraint.json(job.unknown_tanks),
                      "weight": fraction_text(weight)})
        total_rhs += weight * constraint.rhs
        if constraint.strict:
            strict_positive = True
    if not items:
        raise EvidenceError("internal empty contradiction certificate")
    proof = {"weights": items, "sum_rhs": str(total_rhs),
             "strict": strict_positive}
    # Self-check before the planner can ever publish it.
    check_proof(job, constraints, proof)
    return proof


def solve_system(job, constraints):
    """Return an evidence object for the system's reachability."""
    values, proof = find_solution(job, constraints)
    if values is not None:
        witness = _witness_json(job, values)
        check_witness(job, constraints, witness)
        return {"reachable": True, "witness": witness}
    return {"reachable": False, "proof": proof}
