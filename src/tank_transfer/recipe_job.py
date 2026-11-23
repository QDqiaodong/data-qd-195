"""Validated offline recipe-planning jobs (``python -m tank_transfer.recipe``).

A JOB carries a step-free v1 initial plan, a list of optional quantitative
transfer actions, a list of final tank/species concentration targets and a
list of safety stock reserves.  The planner picks an ordered subset of the
actions whose execution (one action per v1 step) reaches every target and
keeps every reserve.

The initial plan, actions, targets and reserves are validated exactly like
the rest of the repository: ids use the shared ``ident`` rule, numbers are
bounded decimal strings, all comparisons are exact, and boolean values are
never accepted where an integer is required.  A structurally valid JOB that
simply has no feasible plan is a business result, not an input error.
"""

from .checks import array, fields, ident, integer, number, unique
from .model import validate as validate_plan

MAX_JOB_BYTES = 524288

MAX_SPECIES = 8
MAX_TANKS = 12
MAX_ACTIONS = 12
MAX_TARGETS = 12
MAX_RESERVES = 12

MIN_COST = 1
MAX_COST = 10000

JOB_FIELDS = ("plan", "actions", "targets", "reserves")
PLAN_FIELDS = ("species", "tanks")
PLAN_OPTIONAL = ("steps",)
ACTION_FIELDS = ("id", "source", "target", "volume_l", "cost")
TARGET_FIELDS = ("tank", "species", "min_g_per_l", "max_g_per_l")
RESERVE_FIELDS = ("tank", "min_volume_l")


class JobError(ValueError):
    """The JOB is not a valid recipe-planning input (exit 2, no result)."""


class Action:
    __slots__ = ("id", "source", "target", "volume", "volume_text", "cost")

    def __init__(self, aid, source, target, volume, volume_text, cost):
        self.id = aid
        self.source = source
        self.target = target
        self.volume = volume            # Fraction
        self.volume_text = volume_text  # raw v1 number string
        self.cost = cost                # plain int


class Target:
    __slots__ = ("tank", "species", "low", "high")

    def __init__(self, tank, species, low, high):
        self.tank = tank
        self.species = species
        self.low = low                  # Fraction, inclusive
        self.high = high                # Fraction, inclusive


class Reserve:
    __slots__ = ("tank", "min_volume")

    def __init__(self, tank, min_volume):
        self.tank = tank
        self.min_volume = min_volume    # Fraction


class Job:
    __slots__ = ("species", "tanks", "actions", "targets", "reserves",
                 "species_json", "tanks_json")

    def __init__(self, species, tanks, actions, targets, reserves,
                 species_json, tanks_json):
        self.species = species          # tuple of species ids
        self.tanks = tanks              # id -> {"capacity","volume","masses"}
        self.actions = actions          # tuple sorted by action id
        self.targets = targets          # tuple, input order (order-neutral use)
        self.reserves = reserves        # tuple, input order
        self.species_json = species_json  # raw JSON list, never mutated
        self.tanks_json = tanks_json      # raw JSON list, never mutated


def _parse_plan(raw):
    # ``species`` and ``tanks`` are required; ``steps`` may be omitted but, if
    # present, must be an empty array.  A step-bearing plan is never accepted.
    fields(raw, PLAN_FIELDS, PLAN_OPTIONAL)
    if "steps" in raw:
        array(raw["steps"], 0, 0)
    try:
        species, tanks, _ = validate_plan(
            {"species": raw["species"], "tanks": raw["tanks"], "steps": []})
    except ValueError as exc:
        raise JobError(str(exc))
    if len(species) > MAX_SPECIES:
        raise JobError(f"at most {MAX_SPECIES} species")
    if len(tanks) > MAX_TANKS:
        raise JobError(f"at most {MAX_TANKS} tanks")
    return tuple(species), tanks


def _parse_actions(raw_actions, tanks):
    actions = []
    for raw in array(raw_actions, 1, MAX_ACTIONS):
        fields(raw, ACTION_FIELDS)
        aid = ident(raw["id"])
        source = ident(raw["source"])
        target = ident(raw["target"])
        if source not in tanks or target not in tanks or source == target:
            raise JobError("unknown or identical action endpoints")
        volume = number(raw["volume_l"])
        if volume <= 0:
            raise JobError("positive action volume required")
        cost = integer(raw["cost"], MIN_COST, MAX_COST)
        actions.append(Action(aid, source, target, volume, raw["volume_l"],
                              cost))
    unique([action.id for action in actions])
    actions.sort(key=lambda action: action.id)
    return tuple(actions)


def _parse_targets(raw_targets, species, tanks):
    targets = []
    for raw in array(raw_targets, 1, MAX_TARGETS):
        fields(raw, TARGET_FIELDS)
        tank = ident(raw["tank"])
        selected = ident(raw["species"])
        if tank not in tanks:
            raise JobError("unknown target tank")
        if selected not in species:
            raise JobError("unknown target species")
        low = number(raw["min_g_per_l"])
        high = number(raw["max_g_per_l"])
        if low < 0 or high < 0:
            raise JobError("target concentrations must be non-negative")
        if low > high:
            raise JobError("target min_g_per_l exceeds max_g_per_l")
        targets.append(Target(tank, selected, low, high))
    unique([target.tank for target in targets])
    return tuple(targets)


def _parse_reserves(raw_reserves, tanks):
    reserves = []
    for raw in array(raw_reserves, 0, MAX_RESERVES):
        fields(raw, RESERVE_FIELDS)
        tank = ident(raw["tank"])
        if tank not in tanks:
            raise JobError("unknown reserve tank")
        minimum = number(raw["min_volume_l"])
        if minimum < 0:
            raise JobError("reserve volume must be non-negative")
        reserves.append(Reserve(tank, minimum))
    unique([reserve.tank for reserve in reserves])
    return tuple(reserves)


def parse_job(data):
    """Validate a loaded JSON document and return a :class:`Job`."""
    try:
        fields(data, JOB_FIELDS)
        species, tanks = _parse_plan(data["plan"])
        actions = _parse_actions(data["actions"], tanks)
        targets = _parse_targets(data["targets"], species, tanks)
        reserves = _parse_reserves(data["reserves"], tanks)
    except ValueError as exc:
        if isinstance(exc, JobError):
            raise
        raise JobError(str(exc))
    return Job(species, tanks, actions, targets, reserves,
               data["plan"]["species"], data["plan"]["tanks"])
