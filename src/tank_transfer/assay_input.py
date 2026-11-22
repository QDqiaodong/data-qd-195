"""Parsing helpers shared by the planner and the independent verifier."""

import hashlib
import json
import re
from fractions import Fraction

from .io import load_bounded

MAX_JOB_BYTES = 524288
MAX_REPORT_BYTES = 8388608

MAX_TANKS = 16
MAX_SPECIES = 8
MAX_STEPS = 48
MAX_TRANSFERS = 96
MAX_UNKNOWN = 4
MAX_OBSERVATIONS = 8
MAX_PROBES = 6
MAX_CUTS = 3
MAX_PROBE_COST = 10000

JOB_FIELDS = ("plan", "species", "unknown", "observations", "target", "probes")
POSITION_FIELDS = ("step", "tank")

LEVELS = ("low", "high")

_FRACTION_RE = re.compile(r"^-?(0|[1-9][0-9]*)(/[1-9][0-9]*)?$")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def job_fingerprint(data):
    """Stable identity of the parsed JOB, independent of JSON layout."""
    return "sha256:" + hashlib.sha256(
        canonical_json(data).encode("utf-8")).hexdigest()


def fraction_text(value):
    """Canonical reduced fraction/integer string used by every proof value."""
    return str(Fraction(value))


def proof_fraction(value):
    """Parse a reduced fraction/integer string; reject decimals and aliases."""
    if not isinstance(value, str) or not _FRACTION_RE.fullmatch(value):
        raise ValueError("reduced fraction or integer string required")
    result = Fraction(value)
    if str(result) != value:
        raise ValueError("fraction string must be reduced and canonical")
    return result


def load_job(path):
    return load_bounded(path, MAX_JOB_BYTES)
