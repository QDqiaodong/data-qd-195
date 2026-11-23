"""Exact offline scheduler for pump work orders.

Iterative deepening over the tick count, then depth-first search that tries
candidate ticks in lexicographic order of their sorted order-id lists, so the
first complete schedule found at the minimal tick count is the required
lexicographically smallest one.  A failure memo keyed by the full exact state
(completed-order mask plus every tank volume and species mass) keeps states
that cannot be completed within the remaining ticks from being revisited;
two prefixes that reach the same completed set with the same tank contents
are interchangeable, while prefixes that merely agree on volumes but differ
in concentrations are not merged.

All physics follow the v1 contract with exact ``Fraction`` arithmetic: a tick
reads opening concentrations, same-tick arrivals neither fund outflows nor
change concentration gates, and the next tick sees the real closing state.
"""

from fractions import Fraction

ZERO = Fraction(0)


class _Problem:
    def __init__(self, job):
        self.job = job
        self.species = job.species
        self.tank_ids = tuple(sorted(job.tanks))
        self.tank_index = {name: i for i, name in enumerate(self.tank_ids)}
        self.capacity = tuple(job.tanks[name]["capacity"]
                              for name in self.tank_ids)
        self.initial = tuple(
            (job.tanks[name]["volume"],
             tuple(job.tanks[name]["masses"][s] for s in self.species))
            for name in self.tank_ids)
        self.orders = job.orders  # already sorted by order id
        self.n = len(self.orders)
        self.order_index = {order.id: i for i, order in enumerate(self.orders)}
        self.source = tuple(self.tank_index[o.source] for o in self.orders)
        self.target = tuple(self.tank_index[o.target] for o in self.orders)
        self.volume = tuple(o.volume for o in self.orders)
        self.pump = tuple(o.pump for o in self.orders)
        self.preds = tuple(
            sum(1 << self.order_index[p] for p in o.after) for o in self.orders)
        self.accept = tuple(
            None if o.accept is None
            else (self.species.index(o.accept[0]), o.accept[1], o.accept[2])
            for o in self.orders)

    # -- feasibility of one tick against an opening state -----------------

    def _single_ok(self, order, state):
        """Conditions an order must meet in any tick: outflow and gate.

        The closing-capacity rule is deliberately not checked here: in a
        shared tick the target tank may also send, so capacity is judged per
        candidate tick by :meth:`_singleton_ok` and :meth:`_pair_ok`.
        """
        source = self.source[order]
        volume = self.volume[order]
        source_volume, source_masses = state[source]
        if volume > source_volume:
            return False
        gate = self.accept[order]
        if gate is not None:
            species, low, high = gate
            mass = source_masses[species]
            # low <= mass / source_volume <= high, exactly, no division.
            if mass < low * source_volume or mass > high * source_volume:
                return False
        return True

    def _singleton_ok(self, order, state):
        target = self.target[order]
        return state[target][0] + self.volume[order] <= self.capacity[target]

    def _pair_ok(self, first, second, state):
        if self.pump[first] == self.pump[second]:
            return False
        involved = {self.source[first], self.target[first],
                    self.source[second], self.target[second]}
        for tank in involved:
            outgoing = ZERO
            incoming = ZERO
            if self.source[first] == tank:
                outgoing += self.volume[first]
            if self.source[second] == tank:
                outgoing += self.volume[second]
            if self.target[first] == tank:
                incoming += self.volume[first]
            if self.target[second] == tank:
                incoming += self.volume[second]
            opening = state[tank][0]
            if outgoing > opening:
                return False
            if opening - outgoing + incoming > self.capacity[tank]:
                return False
        return True

    def _candidates(self, mask, state):
        available = []
        for order in range(self.n):
            if mask >> order & 1:
                continue
            if self.preds[order] & ~mask:
                continue
            if self._single_ok(order, state):
                available.append(order)
        candidates = [(order,) for order in available
                      if self._singleton_ok(order, state)]
        for i, first in enumerate(available):
            for second in available[i + 1:]:
                if self._pair_ok(first, second, state):
                    candidates.append((first, second))
        candidates.sort()
        return candidates

    # -- exact v1 state transition -----------------------------------------

    def apply(self, state, tick):
        moved = []
        for order in tick:
            source = self.source[order]
            source_volume, source_masses = state[source]
            ratio = self.volume[order] / source_volume
            moved.append(tuple(mass * ratio for mass in source_masses))
        outgoing = {}
        incoming = {}
        for order in tick:
            outgoing[self.source[order]] = (outgoing.get(self.source[order], ZERO)
                                            + self.volume[order])
            incoming[self.target[order]] = (incoming.get(self.target[order], ZERO)
                                            + self.volume[order])
        result = []
        for tank, (volume, masses) in enumerate(state):
            out = outgoing.get(tank)
            inn = incoming.get(tank)
            if out is None and inn is None:
                result.append((volume, masses))
                continue
            new_masses = list(masses)
            for order, transfer in zip(tick, moved):
                if self.source[order] == tank:
                    for k, mass in enumerate(transfer):
                        new_masses[k] -= mass
                if self.target[order] == tank:
                    for k, mass in enumerate(transfer):
                        new_masses[k] += mass
            result.append((volume - (out or ZERO) + (inn or ZERO),
                           tuple(new_masses)))
        return tuple(result)

    # -- search --------------------------------------------------------------

    def solve(self):
        full = (1 << self.n) - 1
        for limit in range(1, self.job.tick_limit + 1):
            schedule = self._search(limit, full)
            if schedule is not None:
                return schedule
        return None

    def _pump_bound(self, mask, remaining_ticks):
        """False when some pump's remaining orders outnumber the ticks left."""
        needed = {}
        for order in range(self.n):
            if not mask >> order & 1:
                pump = self.pump[order]
                needed[pump] = needed.get(pump, 0) + 1
                if needed[pump] > remaining_ticks:
                    return False
        return True

    def _search(self, limit, full):
        failed = {}  # (mask, state) -> max remaining ticks proven insufficient

        def visit(mask, state, depth):
            if mask == full:
                return ()
            remaining_ticks = limit - depth
            if remaining_ticks <= 0:
                return None
            remaining_orders = self.n - bin(mask).count("1")
            if remaining_orders > 2 * remaining_ticks:
                return None
            if not self._pump_bound(mask, remaining_ticks):
                return None
            key = (mask, state)
            if failed.get(key, -1) >= remaining_ticks:
                return None
            for candidate in self._candidates(mask, state):
                next_mask = mask
                for order in candidate:
                    next_mask |= 1 << order
                rest = visit(next_mask, self.apply(state, candidate), depth + 1)
                if rest is not None:
                    return (candidate,) + rest
            failed[key] = remaining_ticks
            return None

        result = visit(0, self.initial, 0)
        if result is None:
            return None
        return [list(tick) for tick in result]

    # -- reporting -----------------------------------------------------------

    def readings(self, schedule):
        """Exact opening concentration for every gated order, by order id."""
        state = self.initial
        result = {}
        for tick in schedule:
            for order in tick:
                gate = self.accept[order]
                if gate is not None:
                    species = gate[0]
                    source_volume, source_masses = state[self.source[order]]
                    result[self.orders[order].id] = (
                        source_masses[species] / source_volume)
            state = self.apply(state, tick)
        return result


def solve(job):
    """Return ``(schedule, readings)`` or ``None`` when no schedule exists.

    ``schedule`` is a list of ticks; each tick is a list of order ids sorted
    ascending.  ``readings`` maps gated order ids to exact concentrations.
    """
    problem = _Problem(job)
    schedule = problem.solve()
    if schedule is None:
        return None
    named = [[problem.orders[order].id for order in tick] for tick in schedule]
    return named, problem.readings(schedule)
