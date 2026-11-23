"""Validated offline pump work-order jobs (``python -m tank_transfer.dispatch``).

A JOB names the v1 species and initial tanks, then lists 1..14 transfer
orders.  Every order runs exactly once inside one tick; a tick is a non-empty
v1 step holding at most two orders that never share a pump.  ``after`` lists
orders that must finish in a strictly earlier tick.  An optional ``accept``
gate constrains the exact concentration of the liquid the order sends, judged
on the opening state of its source tank in that tick.

Unknown predecessors, self-dependencies and dependency cycles are invalid
jobs (exit 2), while a valid job that simply admits no schedule is the
business result ``unschedulable`` (exit 0).
"""

from .checks import array, fields, ident, integer, number, unique

MAX_JOB_BYTES = 524288
MAX_SPECIES = 8
MAX_TANKS = 16
MAX_ORDERS = 14
MIN_TICK_LIMIT = 1
MAX_TICK_LIMIT = 14

JOB_FIELDS = ("species", "tanks", "orders", "tick_limit")
TANK_FIELDS = ("id", "capacity_l", "volume_l", "mass_g")
ORDER_FIELDS = ("id", "source", "target", "volume_l", "pump", "after")
ORDER_OPTIONAL = ("accept",)
ACCEPT_FIELDS = ("species", "min_g_per_l", "max_g_per_l")


class JobError(ValueError):
    """The JOB is not a valid dispatch input (exit 2, no result)."""


class Order:
    __slots__ = ("id", "source", "target", "volume", "volume_text", "pump",
                 "after", "accept")

    def __init__(self, oid, source, target, volume, volume_text, pump,
                 after, accept):
        self.id = oid
        self.source = source          # tank id
        self.target = target          # tank id
        self.volume = volume          # Fraction
        self.volume_text = volume_text
        self.pump = pump
        self.after = after            # tuple of order ids
        self.accept = accept          # (species, lo, hi) Fractions or None


class Job:
    __slots__ = ("species", "tanks", "orders", "tick_limit",
                 "species_json", "tanks_json")

    def __init__(self, species, tanks, orders, tick_limit,
                 species_json, tanks_json):
        self.species = species        # tuple of species ids
        self.tanks = tanks            # id -> {"capacity","volume","masses"}
        self.orders = orders          # tuple of Order, sorted by order id
        self.tick_limit = tick_limit
        self.species_json = species_json    # raw JSON list, never mutated
        self.tanks_json = tanks_json        # raw JSON list, never mutated


def _parse_tanks(data, species):
    tanks = {}
    for tank in array(data["tanks"], 1, MAX_TANKS):
        fields(tank, TANK_FIELDS)
        name = ident(tank["id"])
        if name in tanks:
            raise JobError("duplicate tank")
        capacity = number(tank["capacity_l"])
        volume = number(tank["volume_l"])
        if capacity <= 0 or not 0 <= volume <= capacity:
            raise JobError("invalid tank capacity or volume")
        masses = tank["mass_g"]
        if not isinstance(masses, dict) or set(masses) != set(species):
            raise JobError("every species mass required")
        masses = {s: number(masses[s]) for s in species}
        if any(v < 0 for v in masses.values()):
            raise JobError("invalid initial species mass")
        if volume == 0 and any(masses.values()):
            raise JobError("invalid initial species mass")
        tanks[name] = {"capacity": capacity, "volume": volume,
                       "masses": masses}
    return tanks


def _parse_accept(raw, species):
    fields(raw, ACCEPT_FIELDS)
    selected = raw["species"]
    if not isinstance(selected, str) or selected not in species:
        raise JobError("accept species must be a defined species")
    low = number(raw["min_g_per_l"])
    high = number(raw["max_g_per_l"])
    if low < 0 or high < 0:
        raise JobError("accept concentrations must be non-negative")
    if low > high:
        raise JobError("accept min_g_per_l exceeds max_g_per_l")
    return (selected, low, high)


def _check_dependencies(orders):
    ids = {order.id for order in orders}
    for order in orders:
        for predecessor in order.after:
            if predecessor not in ids:
                raise JobError(f"unknown predecessor: {predecessor}")
    # Kahn's algorithm: every order must become reachable, else a cycle.
    indegree = {order.id: len(order.after) for order in orders}
    dependents = {order.id: [] for order in orders}
    for order in orders:
        for predecessor in order.after:
            dependents[predecessor].append(order.id)
    queue = [oid for oid, degree in indegree.items() if degree == 0]
    settled = 0
    while queue:
        oid = queue.pop()
        settled += 1
        for dependent in dependents[oid]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if settled != len(orders):
        raise JobError("dependency cycle")


def parse_job(data):
    """Validate a loaded JSON document and return a :class:`Job`."""
    try:
        fields(data, JOB_FIELDS)
        species = tuple(array(data["species"], 1, MAX_SPECIES))
        for item in species:
            ident(item)
        unique(species)
        tanks = _parse_tanks(data, species)
        tick_limit = integer(data["tick_limit"], MIN_TICK_LIMIT,
                             MAX_TICK_LIMIT)
        raw_orders = array(data["orders"], 1, MAX_ORDERS)
        orders = []
        for raw in raw_orders:
            fields(raw, ORDER_FIELDS, ORDER_OPTIONAL)
            oid = ident(raw["id"])
            source = ident(raw["source"])
            target = ident(raw["target"])
            if source not in tanks or target not in tanks or source == target:
                raise JobError("unknown or identical transfer endpoints")
            volume = number(raw["volume_l"])
            if volume <= 0:
                raise JobError("positive transfer volume required")
            pump = ident(raw["pump"])
            after = tuple(array(raw["after"], 0, MAX_ORDERS - 1))
            for predecessor in after:
                ident(predecessor)
            unique(after)
            if oid in after:
                raise JobError("self dependency")
            accept = None
            if "accept" in raw:
                accept = _parse_accept(raw["accept"], species)
            orders.append(Order(oid, source, target, volume, raw["volume_l"],
                                pump, after, accept))
        unique([order.id for order in orders])
        _check_dependencies(orders)
    except ValueError as exc:
        if isinstance(exc, JobError):
            raise
        raise JobError(str(exc))
    orders.sort(key=lambda order: order.id)
    return Job(species, tanks, tuple(orders), tick_limit,
               data["species"], data["tanks"])
