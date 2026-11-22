"""Exact per-source lot tracking on top of the frozen v1 transfer model.

The v1 model only tracks aggregate tank contents.  This module adds a second,
independent bookkeeping layer: every initial tank with positive volume is a
material source, and each source's volume and per-species mass are followed
through all transfers.  The aggregate totals must agree exactly with the v1
model at every closing state.

All arithmetic uses :class:`fractions.Fraction`; values in evidence objects are
reduced fraction strings (or integer strings).
"""

from fractions import Fraction
import json

from .checks import number
from .mixing import step as v1_step
from .model import validate

MAX_TANKS = 16
MAX_SPECIES = 8
MAX_STEPS = 64
MAX_TRANSFERS = 256
MAX_SOURCE_BYTES = 524288
MAX_VERSIONS = 32
MAX_RECEIPTS = 1024


def _new_lot(volume, masses, species):
    return {"volume": volume, "masses": {s: masses[s] for s in species}}


def _add_lot(lots, source_id, volume, masses, species):
    """Merge a contribution of one source into a lot map.

    Only positive-volume lots are retained.  A positive volume with all-zero
    component masses is still a real source lot and must be kept.
    """
    if volume < 0:
        raise ValueError("internal negative source lot")
    if volume == 0:
        if any(masses[s] != 0 for s in species):
            raise ValueError("internal mass without volume")
        return
    lot = lots.get(source_id)
    if lot is None:
        lot = _new_lot(Fraction(0), {s: Fraction(0) for s in species}, species)
        lots[source_id] = lot
    lot["volume"] += volume
    for s in species:
        lot["masses"][s] += masses[s]


def _lot_totals(lots, species):
    volume = sum((lot["volume"] for lot in lots.values()), Fraction(0))
    masses = {s: sum((lot["masses"][s] for lot in lots.values()), Fraction(0))
              for s in species}
    return volume, masses


def _evidence(volume, masses, lots, species, source=None, target=None):
    obj = {
        "volume_l": str(volume),
        "mass_g": {s: str(masses[s]) for s in species},
        "lots": {},
    }
    for source_id in sorted(lots):
        lot = lots[source_id]
        if lot["volume"] <= 0:
            continue
        obj["lots"][source_id] = {
            "volume_l": str(lot["volume"]),
            "mass_g": {s: str(lot["masses"][s]) for s in species},
        }
    if source is not None:
        obj["source"] = source
        obj["target"] = target
    return obj


def evidence_signature(evidence):
    """Canonical comparison key: field set, exact values, no key-order effect."""
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _initial_state(tanks):
    state = {}
    for name, tank in tanks.items():
        lots = {}
        if tank["volume"] > 0:
            # The source ID is the initial tank ID.  Keep it even when every
            # component mass is zero.
            lots[name] = _new_lot(tank["volume"], tank["masses"], list(tank["masses"]))
        state[name] = {
            "capacity": tank["capacity"],
            "volume": tank["volume"],
            "masses": dict(tank["masses"]),
            "lots": lots,
        }
    return state


def _source_step(tanks, transfers, species):
    """Perform one simultaneous step on the source-lot layer.

    All transfers read the opening snapshot.  Incoming material is merged into
    the closing state only after every outgoing share has been removed.
    """
    outgoing = {}
    incoming = {}
    transfer_evidence = {}

    for transfer in transfers:
        source = transfer["source"]
        target = transfer["target"]
        volume = number(transfer["volume_l"])
        opening = tanks[source]
        opening_volume = opening["volume"]
        # v1 validation has already rejected outflows from zero-volume tanks.
        if opening_volume <= 0:
            raise ValueError("opening volume insufficient: " + source)

        carried = {}
        factor = volume / opening_volume
        for source_id, lot in opening["lots"].items():
            lot_volume = lot["volume"] * factor
            lot_masses = {s: lot["masses"][s] * factor for s in species}
            _add_lot(carried, source_id, lot_volume, lot_masses, species)
            _add_lot(outgoing.setdefault(source, {}), source_id, lot_volume,
                     lot_masses, species)
            _add_lot(incoming.setdefault(target, {}), source_id, lot_volume,
                     lot_masses, species)

        carried_volume, carried_masses = _lot_totals(carried, species)
        if carried_volume != volume:
            raise ValueError("internal transfer lot volume mismatch")
        transfer_evidence[transfer["id"]] = _evidence(
            carried_volume, carried_masses, carried, species, source, target)

    result = {}
    closing_evidence = {}
    for name, tank in tanks.items():
        lots = {}
        removed_map = outgoing.get(name, {})
        for source_id, lot in tank["lots"].items():
            removed = removed_map.get(source_id)
            if removed is None:
                volume = lot["volume"]
                masses = dict(lot["masses"])
            else:
                volume = lot["volume"] - removed["volume"]
                masses = {s: lot["masses"][s] - removed["masses"][s]
                          for s in species}
            if volume < 0 or any(masses[s] < 0 for s in species):
                raise ValueError("internal negative retained lot")
            _add_lot(lots, source_id, volume, masses, species)

        for source_id, lot in incoming.get(name, {}).items():
            _add_lot(lots, source_id, lot["volume"], lot["masses"], species)

        volume, masses = _lot_totals(lots, species)
        result[name] = {
            "capacity": tank["capacity"],
            "volume": volume,
            "masses": masses,
            "lots": lots,
        }
        closing_evidence[name] = _evidence(volume, masses, lots, species)

    return result, closing_evidence, transfer_evidence


class Plan:
    """A validated v1 plan together with its exact source evidence."""

    def __init__(self, species, tank_ids, closing, transfers):
        self.species = species
        self.tank_ids = tank_ids
        self._closing = closing
        self._transfers = transfers

    def sample(self, kind, step_id, item_id):
        """Return (status, evidence) where status is ok/missing/empty.

        Missing means the identity does not exist in this plan.  Empty means
        the identity exists but denotes a sample with zero volume.  Transfer
        samples have a strictly positive volume in a valid plan.
        """
        key = (step_id, item_id)
        if kind == "closing":
            evidence = self._closing.get(key)
        elif kind == "transfer":
            evidence = self._transfers.get(key)
        else:
            raise ValueError("unknown sample kind")
        if evidence is None:
            return "missing", None
        if Fraction(evidence["volume_l"]) == 0:
            return "empty", evidence
        return "ok", evidence


def validate_limits(data, expected_tanks=None, expected_species=None):
    species, tanks, steps = validate(data)
    if len(tanks) > MAX_TANKS:
        raise ValueError("too many tanks")
    if len(species) > MAX_SPECIES:
        raise ValueError("too many species")
    if len(steps) > MAX_STEPS:
        raise ValueError("too many steps")
    if sum(len(step["transfers"]) for step in steps) > MAX_TRANSFERS:
        raise ValueError("too many transfers")
    if expected_tanks is not None and set(tanks) != set(expected_tanks):
        raise ValueError("tank id set changed")
    if expected_species is not None and set(species) != set(expected_species):
        raise ValueError("species id set changed")
    return species, tanks, steps


def build_plan(data, expected_tanks=None, expected_species=None):
    """Validate a complete v1 plan and build its source-tracking evidence.

    The plan is accepted only if the normal v1 physics is valid and the
    source layer reproduces the v1 totals at every step, with per-source
    volume and mass conserved exactly.
    """
    species, tanks, steps = validate_limits(data, expected_tanks, expected_species)

    v1_tanks = {
        name: {
            "capacity": tank["capacity"],
            "volume": tank["volume"],
            "masses": dict(tank["masses"]),
        }
        for name, tank in tanks.items()
    }
    source_tanks = _initial_state(tanks)
    initial_sources = {
        source_id: {
            "volume": lot["volume"],
            "masses": dict(lot["masses"]),
        }
        for tank in source_tanks.values()
        for source_id, lot in tank["lots"].items()
    }

    closing_evidence = {}
    transfer_evidence = {}

    for command in steps:
        step_id = command["id"]
        v1_next, v1_transfer_evidence, _incoming, _outgoing = v1_step(
            v1_tanks, command["transfers"], species)
        next_tanks, closing, transfers = _source_step(
            source_tanks, command["transfers"], species)

        v1_by_id = {entry["transfer_id"]: entry for entry in v1_transfer_evidence}
        for transfer_id, evidence in transfers.items():
            reference = v1_by_id[transfer_id]
            if (evidence["volume_l"] != reference["volume_l"]
                    or evidence["source"] != reference["source"]
                    or evidence["target"] != reference["target"]
                    or evidence["mass_g"] != reference["mass_g"]):
                raise ValueError("internal transfer evidence mismatch")
            transfer_evidence[(step_id, transfer_id)] = evidence

        for name, tank in next_tanks.items():
            reference = v1_next[name]
            if tank["volume"] != reference["volume"]:
                raise ValueError("internal source volume mismatch")
            if tank["masses"] != reference["masses"]:
                raise ValueError("internal source mass mismatch")
            closing_evidence[(step_id, name)] = closing[name]

        for source_id, initial in initial_sources.items():
            volume = Fraction(0)
            masses = {s: Fraction(0) for s in species}
            for tank in next_tanks.values():
                lot = tank["lots"].get(source_id)
                if lot is not None:
                    volume += lot["volume"]
                    for s in species:
                        masses[s] += lot["masses"][s]
            if volume != initial["volume"] or masses != initial["masses"]:
                raise ValueError("internal per-source conservation failure")

        v1_tanks = v1_next
        source_tanks = next_tanks

    return Plan(species, sorted(tanks), closing_evidence, transfer_evidence)
