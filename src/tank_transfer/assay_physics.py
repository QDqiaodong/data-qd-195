"""Validated assay jobs, symbolic v1 physics and exact constraints.

The selected species' initial mass in every unknown tank is a free rational
variable.  Every other initial mass is fixed by the plan.  Because all v1
transfers scale opening tank contents by a numeric volume ratio, every later
selected-species mass is an exact affine function of those variables.  A
closed-tank concentration is that mass divided by the positive closing
volume.

Constraints are always written in unknown *masses* as

    sum(coeff * x_tank) <= rhs        (inclusive)
    sum(coeff * x_tank) <  rhs        (strict)

with every fixed-mass contribution moved to ``rhs``.  A constraint carries a
reference determined by the original JOB alone, so another process can rebuild
it without trusting the report.
"""

import json
from fractions import Fraction

from . import model
from .assay_input import (
    JOB_FIELDS, LEVELS, MAX_CUTS, MAX_OBSERVATIONS, MAX_PROBES,
    MAX_PROBE_COST, MAX_SPECIES, MAX_STEPS, MAX_TANKS, MAX_TRANSFERS,
    MAX_UNKNOWN, POSITION_FIELDS, fraction_text, load_job)
from .checks import array, fields, ident, integer, number, unique

ZERO = Fraction(0)


class JobError(ValueError):
    """The JOB is not a valid assay input (exit 2, no report)."""


class Constraint:
    """One original linear constraint over the unknown initial masses."""

    __slots__ = ("ref", "coeffs", "rhs", "strict")

    def __init__(self, ref, coeffs, rhs, strict):
        self.ref = dict(ref)
        self.coeffs = tuple(Fraction(value) for value in coeffs)
        self.rhs = Fraction(rhs)
        self.strict = bool(strict)

    def key(self):
        return json.dumps(self.ref, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def json(self, unknown_tanks):
        return {
            "ref": dict(self.ref),
            "terms": [{"tank": tank, "coefficient": fraction_text(value)}
                      for tank, value in zip(unknown_tanks, self.coeffs)
                      if value],
            "rhs": fraction_text(self.rhs),
            "strict": self.strict,
        }

    def holds(self, values):
        total = sum((coefficient * value
                     for coefficient, value in zip(self.coeffs, values)), ZERO)
        return total < self.rhs if self.strict else total <= self.rhs


def _affine_add(left, right):
    return (left[0] + right[0],
            tuple(a + b for a, b in zip(left[1], right[1])))


def _affine_scale(affine, factor):
    return (affine[0] * factor,
            tuple(value * factor for value in affine[1]))


def _affine_neg(affine):
    return (-affine[0], tuple(-value for value in affine[1]))


def _zero_affine(size):
    return (ZERO, (ZERO,) * size)


class Job:
    """A validated JOB with exact symbolic states and constraint builders."""

    def __init__(self, data):
        try:
            fields(data, JOB_FIELDS)
        except ValueError as exc:
            raise JobError(str(exc))
        self.data = data
        self._validate_plan(data["plan"])
        self.species = self._validate_species(data["species"])
        self._validate_unknown(data["unknown"])
        self._simulate()
        self._validate_observations(data["observations"])
        self._validate_target(data["target"])
        self._validate_probes(data["probes"])
        self._build_observation_constraints()

    # -- plan ---------------------------------------------------------

    def _validate_plan(self, plan):
        try:
            species, tanks, steps = model.validate(plan)
        except (ValueError, RecursionError) as exc:
            raise JobError(f"invalid plan: {exc}")
        if len(tanks) > MAX_TANKS:
            raise JobError("plan exceeds 16 tanks")
        if len(species) > MAX_SPECIES:
            raise JobError("plan exceeds 8 species")
        if len(steps) > MAX_STEPS:
            raise JobError("plan exceeds 48 steps")
        if sum(len(step["transfers"]) for step in steps) > MAX_TRANSFERS:
            raise JobError("plan exceeds 96 transfers")
        self.plan_species = list(species)
        self.tanks = tanks
        self.steps = steps

    def _validate_species(self, value):
        try:
            ident(value)
        except ValueError as exc:
            raise JobError(f"invalid species: {exc}")
        if value not in self.plan_species:
            raise JobError("species is not a plan species")
        return value

    def _validate_unknown(self, value):
        try:
            entries = array(value, 1, MAX_UNKNOWN)
        except ValueError as exc:
            raise JobError(str(exc))
        names = []
        self.unknown_bounds = {}
        for entry in entries:
            try:
                fields(entry, ("tank", "min_g", "max_g"))
                tank = ident(entry["tank"])
                minimum = number(entry["min_g"])
                maximum = number(entry["max_g"])
            except ValueError as exc:
                raise JobError(f"invalid unknown entry: {exc}")
            if tank not in self.tanks:
                raise JobError(f"unknown tank is not in the plan: {tank}")
            if tank in self.unknown_bounds:
                raise JobError("unknown tank listed twice")
            if self.tanks[tank]["volume"] <= 0:
                raise JobError("unknown tank must have positive volume")
            if self.tanks[tank]["masses"][self.species] != 0:
                raise JobError("unknown species placeholder must be zero")
            if minimum < 0 or minimum > maximum:
                raise JobError("invalid unknown mass bounds")
            names.append(tank)
            self.unknown_bounds[tank] = (minimum, maximum)
        self.unknown_tanks = sorted(names)
        self._index = {tank: index
                       for index, tank in enumerate(self.unknown_tanks)}

    # -- positions ----------------------------------------------------

    def _position(self, value, label):
        try:
            fields(value, POSITION_FIELDS)
            step_id = ident(value["step"])
            tank = ident(value["tank"])
        except ValueError as exc:
            raise JobError(f"invalid {label} position: {exc}")
        if step_id not in self.step_closing_volume:
            raise JobError(f"{label} position step does not exist: {step_id}")
        if tank not in self.tanks:
            raise JobError(f"{label} position tank does not exist: {tank}")
        if self.step_closing_volume[step_id][tank] == 0:
            raise JobError(f"{label} position has zero volume")
        return {"step": step_id, "tank": tank}

    # -- observations / target / probes -------------------------------

    def _validate_observations(self, value):
        try:
            entries = array(value, 0, MAX_OBSERVATIONS)
        except ValueError as exc:
            raise JobError(str(exc))
        observations = []
        ids = []
        for entry in entries:
            try:
                fields(entry, ("id", "at", "min_g_per_l", "max_g_per_l"))
                obs_id = ident(entry["id"])
                minimum = number(entry["min_g_per_l"])
                maximum = number(entry["max_g_per_l"])
            except ValueError as exc:
                raise JobError(f"invalid observation: {exc}")
            if minimum < 0 or minimum > maximum:
                raise JobError("invalid observation concentration range")
            ids.append(obs_id)
            observations.append({"id": obs_id,
                                 "at": self._position(entry["at"],
                                                      "observation"),
                                 "min": minimum, "max": maximum})
        try:
            unique(ids)
        except ValueError:
            raise JobError("duplicate observation id")
        self.observations = sorted(observations, key=lambda item: item["id"])

    def _validate_target(self, value):
        try:
            fields(value, ("at", "limit_g_per_l"))
            limit = number(value["limit_g_per_l"])
        except ValueError as exc:
            raise JobError(f"invalid target: {exc}")
        if limit < 0:
            raise JobError("target limit must be nonnegative")
        self.target_at = self._position(value["at"], "target")
        self.target_limit = limit

    def _validate_probes(self, value):
        try:
            entries = array(value, 0, MAX_PROBES)
        except ValueError as exc:
            raise JobError(str(exc))
        probes = []
        ids = []
        for entry in entries:
            try:
                fields(entry, ("id", "at", "cut_g_per_l", "cost"))
                probe_id = ident(entry["id"])
                cost = integer(entry["cost"], 1, MAX_PROBE_COST)
                cuts = [number(cut) for cut in
                        array(entry["cut_g_per_l"], 1, MAX_CUTS)]
            except ValueError as exc:
                raise JobError(f"invalid probe: {exc}")
            if any(cut < 0 for cut in cuts):
                raise JobError("probe cuts must be nonnegative")
            if any(left >= right for left, right in zip(cuts, cuts[1:])):
                raise JobError("probe cuts must strictly increase")
            position = self._position(entry["at"], "probe")
            if (position["step"] == self.target_at["step"]
                    and position["tank"] == self.target_at["tank"]):
                raise JobError("probe position must differ from target")
            ids.append(probe_id)
            probes.append({"id": probe_id, "at": position,
                           "cuts": cuts, "cost": cost})
        try:
            unique(ids)
        except ValueError:
            raise JobError("duplicate probe id")
        self.probes = sorted(probes, key=lambda probe: probe["id"])

    # -- symbolic physics ---------------------------------------------

    def _simulate(self):
        size = len(self.unknown_tanks)
        state = {}
        for name, tank in self.tanks.items():
            if name in self._index:
                coeffs = [ZERO] * size
                coeffs[self._index[name]] = Fraction(1)
                mass = (ZERO, tuple(coeffs))
            else:
                mass = (tank["masses"][self.species], (ZERO,) * size)
            state[name] = {"volume": tank["volume"],
                           "capacity": tank["capacity"], "mass": mass}

        self.step_closing_volume = {}
        self.mass_affine = {}
        for command in self.steps:
            opening = state
            outgoing = {name: ZERO for name in opening}
            incoming = {name: ZERO for name in opening}
            for transfer in command["transfers"]:
                volume = number(transfer["volume_l"])
                outgoing[transfer["source"]] += volume
                incoming[transfer["target"]] += volume
            closing = {}
            for name, tank in opening.items():
                if outgoing[name] > tank["volume"]:
                    raise JobError(f"opening volume insufficient: {name}")
                volume = tank["volume"] - outgoing[name] + incoming[name]
                if volume > tank["capacity"]:
                    raise JobError(f"closing capacity exceeded: {name}")
                closing[name] = volume
            delta = {name: _zero_affine(size) for name in opening}
            for transfer in command["transfers"]:
                source = transfer["source"]
                target = transfer["target"]
                volume = number(transfer["volume_l"])
                factor = volume / opening[source]["volume"]
                carried = _affine_scale(opening[source]["mass"], factor)
                delta[source] = _affine_add(delta[source],
                                            _affine_neg(carried))
                delta[target] = _affine_add(delta[target], carried)
            new_state = {}
            for name, tank in opening.items():
                mass = _affine_add(tank["mass"], delta[name])
                new_state[name] = {"volume": closing[name],
                                   "capacity": tank["capacity"],
                                   "mass": mass}
                self.mass_affine[(command["id"], name)] = mass
            self.step_closing_volume[command["id"]] = closing
            state = new_state

    # -- constraint construction --------------------------------------

    def _at_affine(self, position):
        return self.mass_affine[(position["step"], position["tank"])]

    def _at_volume(self, position):
        return self.step_closing_volume[position["step"]][position["tank"]]

    def bound_constraints(self):
        result = []
        size = len(self.unknown_tanks)
        for tank in self.unknown_tanks:
            index = self._index[tank]
            minimum, maximum = self.unknown_bounds[tank]
            coeffs = [ZERO] * size
            coeffs[index] = Fraction(-1)
            result.append(Constraint({"kind": "unknown_min", "tank": tank},
                                     coeffs, -minimum, False))
            coeffs = [ZERO] * size
            coeffs[index] = Fraction(1)
            result.append(Constraint({"kind": "unknown_max", "tank": tank},
                                     coeffs, maximum, False))
        return result

    def _observation_constraints(self):
        result = []
        for observation in self.observations:
            constant, coeffs = self._at_affine(observation["at"])
            volume = self._at_volume(observation["at"])
            result.append(Constraint(
                {"kind": "observation_min", "id": observation["id"]},
                tuple(-value for value in coeffs),
                constant - observation["min"] * volume, False))
            result.append(Constraint(
                {"kind": "observation_max", "id": observation["id"]},
                coeffs,
                observation["max"] * volume - constant, False))
        return result

    def _build_observation_constraints(self):
        self.observation_constraints = self._observation_constraints()

    def target_constraint(self, level):
        if level not in LEVELS:
            raise ValueError("unknown target level")
        constant, coeffs = self._at_affine(self.target_at)
        volume = self._at_volume(self.target_at)
        if level == "low":
            return Constraint({"kind": "target_low"}, coeffs,
                              self.target_limit * volume - constant, False)
        return Constraint({"kind": "target_high"},
                          tuple(-value for value in coeffs),
                          constant - self.target_limit * volume, True)

    def bucket_constraints(self, probe, bucket):
        cuts = probe["cuts"]
        if type(bucket) is not int or not 0 <= bucket <= len(cuts):
            raise ValueError("bucket out of range")
        constant, coeffs = self._at_affine(probe["at"])
        volume = self._at_volume(probe["at"])
        result = []
        if bucket > 0:
            result.append(Constraint(
                {"kind": "bucket_min", "probe": probe["id"], "bucket": bucket},
                tuple(-value for value in coeffs),
                constant - cuts[bucket - 1] * volume, False))
        if bucket < len(cuts):
            result.append(Constraint(
                {"kind": "bucket_max", "probe": probe["id"], "bucket": bucket},
                coeffs, cuts[bucket] * volume - constant, True))
        return result


def parse_job(path):
    try:
        data = load_job(path)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise JobError(f"cannot read JOB: {exc}")
    return Job(data)
