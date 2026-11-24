"""Exact fact tables and delta packets for offline drill delivery.

A *fact table* is the complete exact result of one plan version: one row per
initial tank, one row per plan step (its 1-based ordinal), one row per
transfer and one row per closing tank of every step.  Rows are sorted by key;
keys compare element by element by original characters, a common prefix
sorting before a longer key.  Volumes and masses are reduced fraction or
integer strings computed with :class:`fractions.Fraction` only.

A *delta packet* carries the difference between two adjacent versions: the
keys to delete and the whole rows to add or replace, both sorted by key, plus
the drill identity, the adjacent version numbers and the snapshot
fingerprints of both ends.  The packet fingerprint is the SHA-256 of the
canonical JSON of the packet without the ``fingerprint`` field itself.

Canonical JSON is UTF-8, object keys sorted, no extra whitespace and no
trailing newline; every SHA-256 fingerprint is computed over it.
"""

from fractions import Fraction
import hashlib
import json
import re

from .checks import ident
from .mixing import step as v1_step
from .model import validate as validate_v1

MAX_PLAN_BYTES = 1048576  # 1 MiB per plan document
MAX_TANKS = 32
MAX_SPECIES = 8
MAX_STEPS = 64
MAX_TRANSFERS = 512
MAX_PACKET_BYTES = 8000000
MAX_CONSUMERS = 8

KINDS = ("initial", "step", "transfer", "closing")
PACKET_FIELDS = ("drill", "from_version", "to_version", "from_snapshot",
                 "to_snapshot", "deletes", "writes", "fingerprint")
SNAPSHOT_FIELDS = ("drill", "version", "fingerprint", "facts")
CURSOR_FIELDS = ("drill", "version", "snapshot")
ACK_FIELDS = ("consumer", "drill", "version", "snapshot", "packet")

_FRACTION_TEXT = re.compile(r"-?[0-9]+(/[0-9]+)?")
_FINGERPRINT_TEXT = re.compile(r"[0-9a-f]{64}")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def fingerprint(value):
    """SHA-256 hex of the canonical JSON of *value*."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_plan(data, expected_tanks=None, expected_species=None):
    """v1 input rules plus the tighter delivery limits and fixed id sets."""
    species, tanks, steps = validate_v1(data)
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


def build_facts(data, expected_tanks=None, expected_species=None):
    """Validate a complete v1 plan and return its sorted fact rows.

    The whole plan is simulated with the exact v1 semantics, so a physically
    invalid step — including the last one — rejects the whole plan.  Only the
    opening state of a step funds its transfers; nothing is rounded.
    """
    species, tanks, steps = validate_plan(data, expected_tanks,
                                          expected_species)
    rows = []
    for name in sorted(tanks):
        tank = tanks[name]
        rows.append({"key": ["initial", name],
                     "value": {"volume_l": str(tank["volume"]),
                               "mass_g": {s: str(tank["masses"][s])
                                          for s in species}}})
    state = {name: {"capacity": tank["capacity"], "volume": tank["volume"],
                    "masses": dict(tank["masses"])}
             for name, tank in tanks.items()}
    for ordinal, command in enumerate(steps, 1):
        step_id = command["id"]
        rows.append({"key": ["step", step_id], "value": {"ordinal": ordinal}})
        state, evidence, _incoming, _outgoing = v1_step(
            state, command["transfers"], species)
        for entry in evidence:
            rows.append({"key": ["transfer", step_id, entry["transfer_id"]],
                         "value": {"source": entry["source"],
                                   "target": entry["target"],
                                   "volume_l": entry["volume_l"],
                                   "mass_g": entry["mass_g"]}})
        for name in sorted(state):
            tank = state[name]
            rows.append({"key": ["closing", step_id, name],
                         "value": {"volume_l": str(tank["volume"]),
                                   "mass_g": {s: str(tank["masses"][s])
                                              for s in species}}})
    rows.sort(key=lambda row: row["key"])
    return rows


def build_packet(drill, from_version, from_rows, to_version, to_rows):
    """Delta packet between the fact tables of two adjacent versions.

    Only keys absent from the target version are deleted; only new or
    actually changed rows are written.  An unchanged plan therefore yields a
    legal packet with empty deletes and writes.
    """
    before = {tuple(row["key"]): row["value"] for row in from_rows}
    after = {tuple(row["key"]): row["value"] for row in to_rows}
    packet = {
        "drill": drill,
        "from_version": from_version,
        "to_version": to_version,
        "from_snapshot": fingerprint(from_rows),
        "to_snapshot": fingerprint(to_rows),
        "deletes": [list(key) for key in sorted(before) if key not in after],
        "writes": [{"key": list(key), "value": after[key]}
                   for key in sorted(after)
                   if key not in before or before[key] != after[key]],
    }
    packet["fingerprint"] = packet_fingerprint(packet)
    return packet


def packet_fingerprint(packet):
    """Fingerprint of a packet; the fingerprint field itself is excluded."""
    body = {field: value for field, value in packet.items()
            if field != "fingerprint"}
    return fingerprint(body)


def check_fingerprint_text(value):
    if not isinstance(value, str) or not _FINGERPRINT_TEXT.fullmatch(value):
        raise ValueError("expected sha-256 hex fingerprint")
    return value


def _check_fraction_text(value):
    if not isinstance(value, str) or not _FRACTION_TEXT.fullmatch(value):
        raise ValueError("expected reduced fraction or integer string")
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise ValueError("expected reduced fraction or integer string")
    if str(parsed) != value:
        raise ValueError("expected reduced fraction or integer string")


def validate_key(key):
    if not isinstance(key, list) or not 2 <= len(key) <= 3:
        raise ValueError("invalid fact key")
    for part in key:
        ident(part)
    kind = key[0]
    if kind not in KINDS:
        raise ValueError("invalid fact key kind")
    if len(key) != (3 if kind in ("transfer", "closing") else 2):
        raise ValueError("invalid fact key arity")
    return key


def validate_value(key, value, tanks, species):
    kind = key[0]
    if kind == "step":
        if not isinstance(value, dict) or set(value) != {"ordinal"}:
            raise ValueError("invalid step row value")
        ordinal = value["ordinal"]
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError("invalid step ordinal")
        return
    if not isinstance(value, dict):
        raise ValueError("invalid row value")
    if kind == "transfer":
        if set(value) != {"source", "target", "volume_l", "mass_g"}:
            raise ValueError("invalid transfer row value")
        source, target = value["source"], value["target"]
        ident(source)
        ident(target)
        if source == target or source not in tanks or target not in tanks:
            raise ValueError("invalid transfer endpoints")
    elif set(value) != {"volume_l", "mass_g"}:
        raise ValueError("invalid row value")
    _check_fraction_text(value["volume_l"])
    masses = value["mass_g"]
    if not isinstance(masses, dict) or set(masses) != set(species):
        raise ValueError("invalid row masses")
    for mass in masses.values():
        _check_fraction_text(mass)


def _check_sorted_unique(keys):
    if keys != sorted(keys) or len({tuple(key) for key in keys}) != len(keys):
        raise ValueError("fact keys must be sorted and unique")


def validate_facts(rows, tanks=None, species=None):
    """Shape-check a sorted fact table.

    Tank and species id sets are derived from the initial rows when not
    given; when given, the initial rows must reproduce them exactly.
    Returns the (tanks, species) sets as sorted lists.
    """
    if not isinstance(rows, list):
        raise ValueError("fact table must be an array")
    keys = []
    derived_tanks = set()
    derived_species = None
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"key", "value"}:
            raise ValueError("invalid fact row")
        validate_key(row["key"])
        keys.append(row["key"])
        if row["key"][0] == "initial":
            derived_tanks.add(row["key"][1])
            value = row["value"]
            masses = value.get("mass_g") if isinstance(value, dict) else None
            if not isinstance(masses, dict):
                raise ValueError("invalid initial row")
            if derived_species is None:
                derived_species = set(masses)
            elif set(masses) != derived_species:
                raise ValueError("inconsistent species set")
    _check_sorted_unique(keys)
    if derived_species is None:
        raise ValueError("fact table has no initial rows")
    if tanks is not None and set(tanks) != derived_tanks:
        raise ValueError("initial rows do not match known tanks")
    if species is not None and set(species) != derived_species:
        raise ValueError("initial rows do not match known species")
    tanks = derived_tanks if tanks is None else set(tanks)
    species = derived_species if species is None else set(species)
    for row in rows:
        validate_value(row["key"], row["value"], tanks, species)
    return sorted(tanks), sorted(species)


def validate_packet(packet, tanks, species):
    """Shape-check a delta packet against the known tank/species id sets."""
    if not isinstance(packet, dict) or set(packet) != set(PACKET_FIELDS):
        raise ValueError("invalid packet fields")
    ident(packet["drill"])
    for field in ("from_version", "to_version"):
        if type(packet[field]) is not int or packet[field] < 0:
            raise ValueError("invalid packet version")
    if packet["to_version"] != packet["from_version"] + 1:
        raise ValueError("packet versions are not adjacent")
    for field in ("from_snapshot", "to_snapshot", "fingerprint"):
        check_fingerprint_text(packet[field])
    deletes = packet["deletes"]
    writes = packet["writes"]
    if not isinstance(deletes, list) or not isinstance(writes, list):
        raise ValueError("invalid packet operations")
    for key in deletes:
        validate_key(key)
    _check_sorted_unique(deletes)
    write_keys = []
    for row in writes:
        if not isinstance(row, dict) or set(row) != {"key", "value"}:
            raise ValueError("invalid packet write row")
        validate_key(row["key"])
        validate_value(row["key"], row["value"], tanks, species)
        write_keys.append(row["key"])
    _check_sorted_unique(write_keys)
    return packet


def apply_packet(rows, packet):
    """Apply a shape-validated packet to a fact table; pure, no I/O.

    Rejects duplicate key operations, deletes of unknown keys and writes
    that would leave the existing value unchanged.
    """
    before = {tuple(row["key"]): row["value"] for row in rows}
    deleted = set()
    for key in packet["deletes"]:
        key = tuple(key)
        if key in deleted:
            raise ValueError("duplicate key operation")
        if key not in before:
            raise ValueError("delete of unknown key")
        deleted.add(key)
    written = set()
    for row in packet["writes"]:
        key = tuple(row["key"])
        if key in deleted or key in written:
            raise ValueError("duplicate key operation")
        if key in before and before[key] == row["value"]:
            raise ValueError("write leaves value unchanged")
        written.add(key)
    after = {key: value for key, value in before.items()
             if key not in deleted}
    for row in packet["writes"]:
        after[tuple(row["key"])] = row["value"]
    return [{"key": list(key), "value": after[key]} for key in sorted(after)]
