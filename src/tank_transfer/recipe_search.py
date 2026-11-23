"""Exact offline recipe planner over optional quantitative transfer actions.

The planner chooses an ordered subset of the JOB's candidate actions.  Every
chosen action becomes exactly one v1 step, so the next step reads the real
closing state of the previous one.  Among all feasible ordered subsets it
selects, in order:

1. the smallest total ``cost``;
2. for an equal cost, the fewest actions;
3. for an equal cost and count, the lexicographically smallest list of action
   ids compared character by character.

The whole search is exact: every volume, mass and concentration is a
:class:`fractions.Fraction`, inequalities are never widened by a tolerance,
and a group of candidates is only abandoned once every subset in it has been
proved infeasible.  Input array order is never used as a priority; actions are
handled in ascending id order and the winning sequence is the one the
tie-break rule names.

Volumes are independent of species masses: the closing volume of a tank after
a set of transfers is its initial volume plus the net inflow of that set, no
matter the order.  That lets the planner reject whole subsets whose final
volumes already violate a reserve or leave a target tank empty, and a lighter
reachability bound rejects subsets that cannot deliver enough of a targeted
species.  Masses are only tracked for species that a target mentions, because
species never interact.
"""

from fractions import Fraction

ZERO = Fraction(0)


class _Problem:
    def __init__(self, job):
        self.job = job
        self.tank_ids = tuple(sorted(job.tanks))
        self.tank_index = {name: i for i, name in enumerate(self.tank_ids)}
        self.capacity = tuple(job.tanks[name]["capacity"]
                              for name in self.tank_ids)
        self.initial_volume = tuple(job.tanks[name]["volume"]
                                    for name in self.tank_ids)

        # Only targeted species can constrain the final state.
        needed = []
        for target in job.targets:
            if target.species not in needed:
                needed.append(target.species)
        self.needed_species = tuple(needed)
        self.species_pos = {name: i for i, name in enumerate(needed)}
        self.initial_masses = tuple(
            tuple(job.tanks[name]["masses"][s] for s in needed)
            for name in self.tank_ids)

        # Actions arrive sorted by id from recipe_job; keep that order because
        # it is exactly the tie-break order of the emitted sequence.
        self.actions = job.actions
        self.n = len(self.actions)
        self.source = tuple(self.tank_index[a.source] for a in self.actions)
        self.target = tuple(self.tank_index[a.target] for a in self.actions)
        self.volume = tuple(a.volume for a in self.actions)
        self.cost = tuple(a.cost for a in self.actions)

        self.targets = tuple(
            (self.tank_index[t.tank], self.species_pos[t.species],
             t.low, t.high) for t in job.targets)
        self.reserves = tuple(
            (self.tank_index[r.tank], r.min_volume) for r in job.reserves)
        self._reach_cache = {}

        # Physically identical actions (same endpoints and volume) commute
        # exactly with each other: either order yields the same closing state.
        # Their costs are equal, so ordering them by ascending id never changes
        # the total and never makes the id list larger.  Every feasible
        # ordered subset has an equal-cost twin with the identical actions in
        # ascending order, so restricting to that twin keeps a representative
        # of the minimum without losing the lexicographic winner.
        shape = tuple((self.source[i], self.target[i], self.volume[i])
                      for i in range(self.n))
        self.identical_before = [0] * self.n
        for later in range(self.n):
            mask = 0
            for earlier in range(later):
                if shape[earlier] == shape[later]:
                    mask |= 1 << earlier
            self.identical_before[later] = mask

        # Bitmask bookkeeping for the bounded action count.
        size = 1 << self.n
        self.popcount = [0] * size
        self.costsum = [0] * size
        for mask in range(1, size):
            low = mask & -mask
            index = low.bit_length() - 1
            self.popcount[mask] = self.popcount[mask ^ low] + 1
            self.costsum[mask] = self.costsum[mask ^ low] + self.cost[index]

    # -- exact v1 single-transfer transition --------------------------------

    def apply(self, state, action):
        volumes, masses = state
        source = self.source[action]
        target = self.target[action]
        volume = self.volume[action]
        if volume > volumes[source]:
            return None
        if volumes[target] + volume > self.capacity[target]:
            return None
        ratio = volume / volumes[source]
        opening = masses[source]
        moved = tuple(mass * ratio for mass in opening)
        new_volumes = list(volumes)
        new_volumes[source] -= volume
        new_volumes[target] += volume
        new_masses = list(masses)
        new_masses[source] = tuple(mass - out
                                   for mass, out in zip(opening, moved))
        new_masses[target] = tuple(mass + add
                                   for mass, add in zip(masses[target], moved))
        return (tuple(new_volumes), tuple(new_masses))

    # -- final business conditions -----------------------------------------

    def final_ok(self, state):
        volumes, masses = state
        for tank, position, low, high in self.targets:
            size = volumes[tank]
            if size <= 0:
                return False
            mass = masses[tank][position]
            if mass < low * size or mass > high * size:
                return False
        for tank, minimum in self.reserves:
            if volumes[tank] < minimum:
                return False
        return True

    def initial_state(self):
        return (self.initial_volume, self.initial_masses)

    # -- subset pre-pass ----------------------------------------------------

    def _final_volumes(self, mask):
        net = [ZERO] * len(self.tank_ids)
        for action in range(self.n):
            if mask >> action & 1:
                net[self.source[action]] -= self.volume[action]
                net[self.target[action]] += self.volume[action]
        return tuple(self.initial_volume[tank] + net[tank]
                     for tank in range(len(self.tank_ids)))

    def _reachable_into(self, mask, sink):
        """Tanks whose liquid can reach ``sink`` using the actions in mask."""
        cache = self._reach_cache
        key = (mask, sink)
        found = cache.get(key)
        if found is not None:
            return found
        reverse = [[] for _ in self.tank_ids]
        for action in range(self.n):
            if mask >> action & 1:
                reverse[self.target[action]].append(self.source[action])
        seen = {sink}
        stack = [sink]
        while stack:
            tank = stack.pop()
            for upstream in reverse[tank]:
                if upstream not in seen:
                    seen.add(upstream)
                    stack.append(upstream)
        found = frozenset(seen)
        cache[key] = found
        return found

    def _inbound(self, mask, tank):
        for action in range(self.n):
            if mask >> action & 1 and self.target[action] == tank:
                return True
        return False

    def _bounds_ok(self, state, mask, final):
        """Sound interval bounds on every target's final mass for ``mask``.

        ``state`` is the current exact state after some prefix, ``mask`` holds
        the actions still to run and ``final`` is the order-independent closing
        volume of each tank once all chosen actions have run.  Liquid only
        moves along remaining action edges, and dwell in a target tank cannot
        fall below its current volume minus its remaining outflows while that
        stays positive, so both bounds below are necessary conditions.
        """
        volumes, masses = state
        for tank, position, low, high in self.targets:
            size = final[tank]
            current = masses[tank][position]
            if not self._inbound(mask, tank):
                # No remaining action feeds this tank, so every remaining
                # outflow draws the same concentration: the closing mass is
                # exactly the current mass scaled by the volume ratio.
                if volumes[tank] <= 0:
                    return False
                final_mass = current * size / volumes[tank]
                if final_mass < low * size or final_mass > high * size:
                    return False
                continue
            reach = self._reachable_into(mask, tank)
            reachable_mass = sum((masses[upstream][position]
                                  for upstream in reach if upstream != tank),
                                 ZERO)
            highest = current + reachable_mass
            if low * size > highest:
                return False
            outbound_volume = sum((self.volume[action]
                                   for action in range(self.n)
                                   if mask >> action & 1
                                   and self.source[action] == tank), ZERO)
            if outbound_volume == 0:
                # Incoming liquid can only raise the mass up to ``highest``.
                if current > high * size:
                    return False
                continue
            # The tank sheds at most every remaining outflow litre, each
            # carrying at most the highest concentration that can arise: no
            # more mass than ``highest`` can ever sit in it, and its volume
            # never drops below the current volume minus all remaining
            # outflows.  Incoming liquid may be re-exported, so this is the
            # only sound lower bound.
            lowest_volume = volumes[tank] - outbound_volume
            if lowest_volume > 0:
                removable = outbound_volume * highest / lowest_volume
            else:
                outflows = sum(1 for action in range(self.n)
                               if mask >> action & 1
                               and self.source[action] == tank)
                removable = outflows * highest
            lowest = current - removable
            if high * size < lowest:
                return False
        return True

    def _subset_viable(self, mask):
        """Sound necessary conditions for mask, without choosing an order."""
        final = self._final_volumes(mask)
        if any(size < 0 for size in final):
            return False
        target_tanks = {tank for tank, _, _, _ in self.targets}
        for tank in target_tanks:
            if final[tank] <= 0:
                return False
        for tank, minimum in self.reserves:
            if final[tank] < minimum:
                return False
        inbound = {tank: False for tank in range(len(self.tank_ids))}
        outbound = {tank: False for tank in range(len(self.tank_ids))}
        for action in range(self.n):
            if mask >> action & 1:
                inbound[self.target[action]] = True
                outbound[self.source[action]] = True
        # Per target, bound the final mass of that species by the mass that
        # can actually flow into the tank.  Liquid only flows along directed
        # action edges, so mass outside the reverse-reachable set can never
        # arrive; that total is an exact upper bound whatever the order.
        for tank, position, low, high in self.targets:
            size = final[tank]
            if low > 0:
                reachable = self._reachable_into(mask, tank)
                available = sum((self.initial_masses[upstream][position]
                                 for upstream in reachable), ZERO)
                if available < low * size:
                    return False
            if not outbound[tank] and not inbound[tank]:
                mass = self.initial_masses[tank][position]
                if mass < low * size or mass > high * size:
                    return False
            elif not outbound[tank]:
                # Nothing can leave, so the initial mass is a lower bound.
                if self.initial_masses[tank][position] > high * size:
                    return False
        return True

    def _viable_masks(self):
        """Masks that pass the sound order-independent subset checks."""
        cache = self.__dict__.get("_viable")
        if cache is not None:
            return cache
        cache = frozenset(mask for mask in range(1 << self.n)
                          if self._subset_viable(mask))
        self._viable = cache
        return cache

    def candidate_groups(self):
        """Sorted ``(cost, count)`` pairs that still own a viable subset.

        Only final volumes, reserves and a reachability bound can be judged
        from a subset alone; the actual ordering feasibility is decided later.
        """
        pairs = {(self.costsum[mask], self.popcount[mask])
                 for mask in self._viable_masks()}
        return sorted(pairs)

    # -- ordered-subset search ---------------------------------------------

    def search_group(self, total_cost, count):
        """Lexicographically smallest feasible sequence in one group.

        Returns a tuple of action indexes or ``None``.  Each viable subset in
        the group is searched separately with its own exact final volumes.
        Subsets are visited in ascending order of their sorted-id list, which
        is the smallest permutation of the subset; once that list is not below
        the best sequence already found, neither this subset nor any later one
        can beat it, so the walk stops.
        """
        masks = [(tuple(action for action in range(self.n)
                        if mask >> action & 1), mask)
                 for mask in self._viable_masks()
                 if self.popcount[mask] == count
                 and self.costsum[mask] == total_cost]
        masks.sort()
        best = None
        for sorted_ids, mask in masks:
            if best is not None and sorted_ids >= best:
                break
            sequence = self._search_subset(mask)
            if sequence is not None and (best is None or sequence < best):
                best = sequence
        return best

    def _search_subset(self, mask):
        """Lexicographically smallest feasible ordering of exactly ``mask``.

        ``mask`` fixes the closing volume of every tank, so ``_bounds_ok`` can
        use exact final volumes.  A boolean completion oracle memoised on
        ``(used_mask, state)`` makes prefixes that used the same actions and
        reached the same exact state interchangeable; the winning sequence is
        rebuilt greedily, taking the smallest action whose completion is still
        feasible.
        """
        memo = {}
        final_volumes = self._final_volumes(mask)

        def complete(state, used):
            remaining = mask & ~used
            if remaining == 0:
                return self.final_ok(state)
            if not self._bounds_ok(state, remaining, final_volumes):
                return False
            key = (used, state)
            cached = memo.get(key)
            if cached is not None:
                return cached
            for action in range(self.n):
                if not remaining >> action & 1:
                    continue
                if self.identical_before[action] & remaining:
                    continue
                following = self.apply(state, action)
                if following is not None and complete(following,
                                                      used | (1 << action)):
                    memo[key] = True
                    return True
            memo[key] = False
            return False

        state = self.initial_state()
        if not complete(state, 0):
            return None
        used = 0
        chosen = []
        while used != mask:
            pick = None
            for action in range(self.n):
                if not mask >> action & 1 or used >> action & 1:
                    continue
                if self.identical_before[action] & (mask & ~used):
                    continue
                following = self.apply(state, action)
                if following is not None and complete(following,
                                                      used | (1 << action)):
                    pick = action
                    state = following
                    break
            if pick is None:  # pragma: no cover - complete() already proved it
                return None
            chosen.append(pick)
            used |= 1 << pick
        return tuple(chosen)

    def solve(self):
        """Return ``(action_indexes, final_state)`` or ``None``."""
        for total_cost, count in self.candidate_groups():
            sequence = self.search_group(total_cost, count)
            if sequence is not None:
                state = self.initial_state()
                for action in sequence:
                    state = self.apply(state, action)
                return sequence, state
        return None


def solve(job):
    """Return ``(action_ids, final_state)`` or ``None``.

    ``action_ids`` is the winning execution order of action ids.  The final
    state is the exact ``(volumes, masses)`` pair keyed like the problem's
    sorted tank and needed-species tuples.
    """
    problem = _Problem(job)
    outcome = problem.solve()
    if outcome is None:
        return None
    sequence, state = outcome
    return [problem.actions[action].id for action in sequence], state
