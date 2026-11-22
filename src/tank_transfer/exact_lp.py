"""Small exact rational linear programs.

Only what the assay planner needs: maximize a linear objective over
nonnegative variables subject to ``<=``, ``=`` and ``>=`` rows.  Every value is
a :class:`fractions.Fraction`, so optima, unboundedness and infeasibility are
decided exactly.  Bland's rule guarantees termination on degenerate
tableaux.

The module is deliberately independent of any tank-transfer decision logic so
that the verifier can be kept free of search/optimisation code.
"""

from fractions import Fraction

ZERO = Fraction(0)


class Infeasible(Exception):
    """No point satisfies every row."""


class Unbounded(Exception):
    """The objective has no finite maximum."""


def _pivot(tableau, rhs, basis, row_index, column):
    pivot = tableau[row_index][column]
    if pivot != 1:
        inverse = Fraction(1) / pivot
        tableau[row_index] = [value * inverse for value in tableau[row_index]]
        rhs[row_index] *= inverse
    pivot_row = tableau[row_index]
    pivot_rhs = rhs[row_index]
    for index in range(len(tableau)):
        if index == row_index:
            continue
        factor = tableau[index][column]
        if factor:
            tableau[index] = [left - factor * right
                              for left, right in zip(tableau[index], pivot_row)]
            rhs[index] -= factor * pivot_rhs
    basis[row_index] = column


def _optimize(tableau, rhs, basis, cost, width):
    """Maximise ``cost`` in place; return the optimum value.

    Entering/leaving choices follow Bland's rule: lowest index with positive
    reduced cost, then lowest indexed basic variable among minimum-ratio ties.
    """
    rows = len(tableau)
    while True:
        reduced = list(cost)
        value = ZERO
        for index, basic in enumerate(basis):
            coefficient = cost[basic]
            if coefficient:
                value += coefficient * rhs[index]
                row = tableau[index]
                reduced = [r - coefficient * v for r, v in zip(reduced, row)]
        entering = None
        for column in range(width):
            if reduced[column] > 0:
                entering = column
                break
        if entering is None:
            return value
        leaving = None
        best_ratio = None
        for index in range(rows):
            coefficient = tableau[index][entering]
            if coefficient > 0:
                ratio = rhs[index] / coefficient
                if (best_ratio is None or ratio < best_ratio
                        or (ratio == best_ratio
                            and basis[index] < basis[leaving])):
                    best_ratio = ratio
                    leaving = index
        if leaving is None:
            raise Unbounded()
        _pivot(tableau, rhs, basis, leaving, entering)


def maximize(nvars, constraints, objective):
    """Return ``(optimum, solution)`` for ``max objective . z``.

    ``constraints`` is a sequence of ``(coefficients, rhs, sense)`` where
    coefficients has length ``nvars`` and sense is ``<=``, ``=`` or ``>=``.
    All variables are constrained to be nonnegative.  Raises
    :class:`Infeasible` or :class:`Unbounded`.
    """
    if len(objective) != nvars:
        raise ValueError("objective width mismatch")
    normalized = []
    slack_count = 0
    artificial_count = 0
    for coefficients, rhs_value, sense in constraints:
        coefficients = list(coefficients)
        if len(coefficients) != nvars:
            raise ValueError("row width mismatch")
        if sense not in ("<=", ">=", "="):
            raise ValueError("unknown row sense")
        rhs_value = Fraction(rhs_value)
        if rhs_value < 0:
            coefficients = [-value for value in coefficients]
            rhs_value = -rhs_value
            if sense == "<=":
                sense = ">="
            elif sense == ">=":
                sense = "<="
        if sense in ("<=", ">="):
            slack_count += 1
        if sense in (">=", "="):
            artificial_count += 1
        normalized.append((coefficients, rhs_value, sense))

    original_width = nvars + slack_count
    width = original_width + artificial_count
    tableau = []
    rhs = []
    basis = []
    slack_at = nvars
    artificial_at = original_width
    artificial_columns = []
    for coefficients, rhs_value, sense in normalized:
        row = coefficients + [ZERO] * (width - nvars)
        if sense == "<=":
            row[slack_at] = Fraction(1)
            basis.append(slack_at)
            slack_at += 1
        elif sense == ">=":
            row[slack_at] = Fraction(-1)
            slack_at += 1
            row[artificial_at] = Fraction(1)
            artificial_columns.append(artificial_at)
            basis.append(artificial_at)
            artificial_at += 1
        else:  # "="
            row[artificial_at] = Fraction(1)
            artificial_columns.append(artificial_at)
            basis.append(artificial_at)
            artificial_at += 1
        tableau.append(row)
        rhs.append(rhs_value)

    phase_one_cost = [ZERO] * width
    for column in artificial_columns:
        phase_one_cost[column] = Fraction(-1)
    phase_one_value = _optimize(tableau, rhs, basis, phase_one_cost, width)
    if phase_one_value != 0:
        raise Infeasible()

    # Push remaining artificial basic variables out; drop redundant rows.
    index = 0
    while index < len(tableau):
        if basis[index] >= original_width:
            replacement = None
            for column in range(original_width):
                if tableau[index][column] != 0:
                    replacement = column
                    break
            if replacement is None:
                del tableau[index]
                del rhs[index]
                del basis[index]
                continue
            _pivot(tableau, rhs, basis, index, replacement)
        index += 1

    tableau = [row[:original_width] for row in tableau]
    phase_two_cost = list(objective) + [ZERO] * (original_width - nvars)
    optimum = _optimize(tableau, rhs, basis, phase_two_cost, original_width)

    solution = [ZERO] * nvars
    for index, basic in enumerate(basis):
        if basic < nvars:
            solution[basic] = rhs[index]
    return optimum, solution
