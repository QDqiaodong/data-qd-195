"""Validated offline ledger-quantization jobs (``python -m tank_transfer.quantize``).

A JOB carries a complete v1 plan, one selected species, the ledger quantum
``quantum_g`` and a list of already-posted ``pins``.  The converter writes an
integer-unit ledger for the selected species only, but the whole plan is
validated and simulated with the exact v1 rules before any rounding happens.

A structurally valid JOB whose pins cannot be reconciled with the rounding
domains is a business result (``conflict``), not an input error.  Unknown pin
rows, duplicate pin locations, bad types, missing or extra fields, an unknown
species selector, a non-positive quantum and a physically impossible plan are
all invalid inputs (exit 2, no dataset).
"""

from .checks import array, fields, ident, integer, number
from .model import validate as validate_plan

MAX_JOB_BYTES = 2097152

MAX_SPECIES = 8
MAX_TANKS = 80
MAX_STEPS = 16
MAX_PINS = 2048
MAX_PIN_UNITS = 10 ** 18

JOB_FIELDS = ("plan", "species", "quantum_g", "pins")

KIND_INITIAL = "initial"
KIND_CLOSING = "closing"
KIND_TRANSFER = "transfer"
PIN_KINDS = (KIND_INITIAL, KIND_CLOSING, KIND_TRANSFER)


class JobError(ValueError):
    """The JOB is not a valid quantization input (exit 2, no dataset)."""


class Pin:
    __slots__ = ("kind", "step_id", "item", "units")

    def __init__(self, kind, step_id, item, units):
        self.kind = kind
        self.step_id = step_id          # step id string, or None for initial
        self.item = item                # tank id or transfer id
        self.units = units              # plain int in 0..10**18


class Job:
    __slots__ = ("species", "tanks", "steps", "sigma", "quantum",
                 "quantum_text", "pins")

    def __init__(self, species, tanks, steps, sigma, quantum, quantum_text,
                 pins):
        self.species = species          # tuple of all v1 species ids
        self.tanks = tanks              # id -> {"capacity","volume","masses"}
        self.steps = steps              # list of v1 step dicts (plan order)
        self.sigma = sigma              # selected species id
        self.quantum = quantum          # Fraction, grams per ledger unit
        self.quantum_text = quantum_text  # raw JOB string, republished verbatim
        self.pins = pins                # tuple of Pin, input order


def _parse_plan(raw):
    try:
        species, tanks, steps = validate_plan(raw)
    except ValueError as exc:
        raise JobError(str(exc))
    if len(species) > MAX_SPECIES:
        raise JobError(f"at most {MAX_SPECIES} species")
    if len(tanks) > MAX_TANKS:
        raise JobError(f"at most {MAX_TANKS} tanks")
    if len(steps) > MAX_STEPS:
        raise JobError(f"at most {MAX_STEPS} steps")
    return tuple(species), tanks, steps


def _parse_pins(raw_pins, tanks, steps):
    pins = []
    seen = set()
    step_index = {step["id"]: position for position, step in enumerate(steps)}
    transfer_ids = [set() for _ in steps]
    for position, step in enumerate(steps):
        for transfer in step["transfers"]:
            transfer_ids[position].add(transfer["id"])
    for raw in array(raw_pins, 0, MAX_PINS):
        if not isinstance(raw, dict):
            raise JobError("pin must be an object")
        kind = raw.get("kind")
        if kind == KIND_INITIAL:
            fields(raw, ("kind", "item", "units"))
            step_id = None
        elif kind in (KIND_CLOSING, KIND_TRANSFER):
            fields(raw, ("kind", "item", "step", "units"))
            step_id = raw["step"]
            if not isinstance(step_id, str):
                raise JobError("pin step must be a string")
        else:
            raise JobError("unknown pin kind")
        item = raw["item"]
        if not isinstance(item, str):
            raise JobError("pin item must be a string")
        units = integer(raw["units"], 0, MAX_PIN_UNITS)
        location = (kind, step_id, item)
        if location in seen:
            raise JobError("duplicate pin location")
        seen.add(location)
        # Locate the pinned row inside the plan; unknown rows are invalid.
        if kind == KIND_INITIAL:
            if item not in tanks:
                raise JobError(f"unknown pin row: initial {item}")
        elif kind == KIND_CLOSING:
            if step_id not in step_index or item not in tanks:
                raise JobError(f"unknown pin row: closing {step_id} {item}")
        else:
            if (step_id not in step_index
                    or item not in transfer_ids[step_index[step_id]]):
                raise JobError(f"unknown pin row: transfer {step_id} {item}")
        pins.append(Pin(kind, step_id, item, units))
    return tuple(pins)


def parse_job(data):
    """Validate a loaded JSON document and return a :class:`Job`."""
    try:
        fields(data, JOB_FIELDS)
        species, tanks, steps = _parse_plan(data["plan"])
        sigma = ident(data["species"])
        if sigma not in species:
            raise JobError("unknown species selector")
        quantum = number(data["quantum_g"])
        if quantum <= 0:
            raise JobError("quantum_g must be positive")
        pins = _parse_pins(data["pins"], tanks, steps)
    except ValueError as exc:
        if isinstance(exc, JobError):
            raise
        raise JobError(str(exc))
    return Job(species, tanks, steps, sigma, quantum, data["quantum_g"], pins)
