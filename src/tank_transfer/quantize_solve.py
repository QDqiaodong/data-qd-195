"""Exact integer-ledger quantization for one species of a v1 plan.

The exact v1 trajectory gives every ledger row (each initial tank, every
transfer, every per-step closing tank) an exact unit value ``x = mass /
quantum_g``; a row may only be posted as ``floor(x)`` or ``ceil(x)``.  Writing
``v = floor(x) + delta`` with ``delta in {0, 1}``, the posting rules become a
min-cost circulation over a layered graph:

* ``S0 -> P1[t]`` carries the initial row of tank ``t`` (``S0 -> T0`` when the
  plan has no steps);
* ``P_s[t] -> Q_s[t]`` is the unposted intermediate balance of tank ``t`` in
  step ``s`` (capacity is the width of its floor/ceil window, cost zero);
* ``P_s[u] -> Q_s[v]`` is one transfer row of step ``s``;
* ``Q_s[t] -> P_{s+1}[t]`` (or ``Q_S[t] -> T0``) is the closing row.

Node supplies come from the exact floor parts, so integer capacities and
integer supplies make every extreme optimum integral.  Each row arc costs
``1 - 2*frac`` (the delta-coefficient of the absolute error), scaled by a
common denominator and by ``2**N`` and added to a lexicographic bit
``2**(N-1-pos)``, so one minimisation yields first the smallest total
absolute error and then the lexicographically smallest row vector.  The
layered graph is acyclic, so initial potentials come from a single
topological pass; successive shortest augmenting paths finish the exact
min-cost circulation.
"""

from fractions import Fraction
import heapq

from .mixing import step as mixing_step

ZERO = Fraction(0)
HALF = Fraction(1, 2)


def _floor_frac(x):
    """Return ``(floor(x), x - floor(x))`` for a non-negative Fraction."""
    floor = x.numerator // x.denominator
    return floor, x - floor


def _round_half_even(x):
    """Nearest integer to ``x``; exact halves go to the even neighbour."""
    floor, frac = _floor_frac(x)
    if frac < HALF:
        return floor
    if frac > HALF:
        return floor + 1
    return floor if floor % 2 == 0 else floor + 1


def _trajectory(job):
    """Exact per-row masses of the selected species, simulated the v1 way.

    Returns ``(init_mass, step_data)`` where ``step_data`` holds, per step,
    the transfer masses, the opening and closing masses and the summed exact
    outflow of every tank.  Physics violations raise ``ValueError`` (exit 2).
    """
    species = job.species
    sigma = job.sigma
    tanks = job.tanks
    init_mass = {name: tank["masses"][sigma] for name, tank in tanks.items()}
    step_data = []
    current = tanks
    for command in job.steps:
        before = current
        current, evidence, _incoming, _outgoing = mixing_step(
            before, command["transfers"], species)
        transfer_mass = {}
        out_sum = {name: ZERO for name in tanks}
        for transfer, record in zip(command["transfers"], evidence):
            mass = Fraction(record["mass_g"][sigma])
            transfer_mass[transfer["id"]] = mass
            out_sum[transfer["source"]] += mass
        opening = {name: before[name]["masses"][sigma] for name in tanks}
        closing = {name: current[name]["masses"][sigma] for name in tanks}
        step_data.append((command, transfer_mass, opening, closing, out_sum))
    return init_mass, step_data


class _Row:
    __slots__ = ("key", "x", "floor", "frac", "pin", "position")

    def __init__(self, key, x):
        self.key = key
        self.x = x
        self.floor, self.frac = _floor_frac(x)
        self.pin = None               # forced delta from a pin, or None
        self.position = None          # lexicographic position if free


def _min_cost_flow(node_count, arcs, supplies):
    """Exact min-cost circulation; return ``{rowref: flow}`` or ``None``.

    ``arcs`` are ``(u, v, capacity, weight, rowref)`` with integer capacities
    and (big) integer weights.  Node indices must be a topological order of
    the forward graph, which lets the initial potentials come from one pass.
    Infeasibility (supplies cannot all be met) yields ``None``.
    """
    source = 0
    sink = node_count - 1
    graph = [[] for _ in range(node_count)]
    row_edge = {}

    def add(u, v, cap, weight, rowref):
        graph[u].append([v, cap, weight, len(graph[v])])
        graph[v].append([u, 0, -weight, len(graph[u]) - 1])
        if rowref is not None:
            row_edge[rowref] = (u, len(graph[u]) - 1)

    for u, v, cap, weight, rowref in arcs:
        add(u, v, cap, weight, rowref)
    total = 0
    for node, supply in enumerate(supplies):
        if supply > 0:
            add(source, node, supply, 0, None)
            total += supply
        elif supply < 0:
            add(node, sink, -supply, 0, None)

    # Initial potentials: shortest distances from a virtual zero-cost source;
    # node order is topological, so a single pass suffices and every reduced
    # cost is non-negative from the start.
    potential = [0] * node_count
    for u in range(node_count):
        base = potential[u]
        for edge in graph[u]:
            if edge[1] > 0:
                candidate = base + edge[2]
                if candidate < potential[edge[0]]:
                    potential[edge[0]] = candidate

    flow = 0
    while flow < total:
        dist = [None] * node_count
        prev = [None] * node_count
        dist[source] = 0
        heap = [(0, source)]
        while heap:
            d, u = heapq.heappop(heap)
            if d != dist[u]:
                continue
            if u == sink:
                break
            hu = potential[u]
            for index, edge in enumerate(graph[u]):
                if edge[1] <= 0:
                    continue
                v = edge[0]
                nd = d + edge[2] + hu - potential[v]
                if dist[v] is None or nd < dist[v]:
                    dist[v] = nd
                    prev[v] = (u, index)
                    heapq.heappush(heap, (nd, v))
        if dist[sink] is None:
            return None
        # The bottleneck is always 1 here (every source-to-sink path crosses
        # a capacity-1 row arc), but compute it exactly anyway.
        bottleneck = None
        v = sink
        while v != source:
            u, index = prev[v]
            cap = graph[u][index][1]
            if bottleneck is None or cap < bottleneck:
                bottleneck = cap
            v = u
        v = sink
        while v != source:
            u, index = prev[v]
            edge = graph[u][index]
            edge[1] -= bottleneck
            graph[v][edge[3]][1] += bottleneck
            v = u
        flow += bottleneck
        for v in range(node_count):
            if dist[v] is not None:
                potential[v] += dist[v]
    return {rowref: 1 - graph[u][index][1]
            for rowref, (u, index) in row_edge.items()}


def solve(job):
    """Return the business result: a balanced dataset or a conflict."""
    init_mass, step_data = _trajectory(job)
    quantum = job.quantum
    tanks_sorted = sorted(job.tanks)
    tank_index = {name: i for i, name in enumerate(tanks_sorted)}
    step_count = len(job.steps)
    tank_count = len(job.tanks)
    step_ids = {step["id"]: index for index, step in enumerate(job.steps)}

    # --- exact unit values, floors and domains for every row ---------------
    rows = {}
    total_exact = ZERO
    for name in tanks_sorted:
        x = init_mass[name] / quantum
        total_exact += x
        rows[("i", name)] = _Row(("i", name), x)
    transfer_floor_out = []   # per step, per tank: sum of transfer floors out
    transfer_floor_in = []    # per step, per tank: sum of transfer floors in
    low = []                  # per step, per tank: floor of balance window
    wide = []                 # per step, per tank: balance window width 0/1
    for step_index, (command, transfer_mass, opening, closing, out_sum) \
            in enumerate(step_data):
        f_out = {name: 0 for name in tanks_sorted}
        f_in = {name: 0 for name in tanks_sorted}
        for transfer in command["transfers"]:
            x = transfer_mass[transfer["id"]] / quantum
            row = _Row(("t", step_index, transfer["id"]), x)
            rows[row.key] = row
            f_out[transfer["source"]] += row.floor
            f_in[transfer["target"]] += row.floor
        transfer_floor_out.append(f_out)
        transfer_floor_in.append(f_in)
        step_low = {}
        step_wide = {}
        for name in tanks_sorted:
            exact_balance = (opening[name] - out_sum[name]) / quantum
            balance_floor, balance_frac = _floor_frac(exact_balance)
            step_low[name] = balance_floor
            step_wide[name] = 1 if balance_frac > 0 else 0
            x = closing[name] / quantum
            row = _Row(("c", step_index, name), x)
            rows[row.key] = row
        low.append(step_low)
        wide.append(step_wide)
    total_units = _round_half_even(total_exact)

    # --- pins: locate rows, check domains, force deltas --------------------
    for pin in job.pins:
        if pin.kind == "initial":
            key = ("i", pin.item)
        elif pin.kind == "closing":
            key = ("c", step_ids[pin.step_id], pin.item)
        else:
            key = ("t", step_ids[pin.step_id], pin.item)
        row = rows[key]
        if pin.units == row.floor:
            row.pin = 0
        elif row.frac > 0 and pin.units == row.floor + 1:
            row.pin = 1
        else:
            return {"status": "conflict"}

    # --- lexicographic positions over the free rows -------------------------
    vector_order = [("i", name) for name in tanks_sorted]
    for step_index, command in enumerate(job.steps):
        vector_order.extend(
            ("t", step_index, tid)
            for tid in sorted(t["id"] for t in command["transfers"]))
        vector_order.extend(("c", step_index, name) for name in tanks_sorted)
    free_rows = []
    for key in vector_order:
        row = rows[key]
        if row.pin is None and row.frac > 0:
            row.position = len(free_rows)
            free_rows.append(row)
    free_count = len(free_rows)
    denominator = 1
    for row in free_rows:
        denominator = (denominator * row.frac.denominator
                       // _gcd(denominator, row.frac.denominator))

    # --- node supplies and the arc list -------------------------------------
    node_count = 4 + 2 * step_count * tank_count
    initial_node = 1                    # S0
    terminal = node_count - 2           # T0

    def p_node(step_index, name):
        return 2 + step_index * 2 * tank_count + tank_index[name]

    def q_node(step_index, name):
        return 2 + step_index * 2 * tank_count + tank_count + tank_index[name]

    supplies = [0] * node_count
    supplies[initial_node] = total_units - sum(
        rows[("i", name)].floor for name in tanks_sorted)
    for step_index in range(step_count):
        for name in tanks_sorted:
            opening_floor = (rows[("i", name)].floor if step_index == 0
                             else rows[("c", step_index - 1, name)].floor)
            supplies[p_node(step_index, name)] = (
                opening_floor - transfer_floor_out[step_index][name]
                - low[step_index][name])
            supplies[q_node(step_index, name)] = (
                low[step_index][name] + transfer_floor_in[step_index][name]
                - rows[("c", step_index, name)].floor)
    if step_count:
        supplies[terminal] = -(total_units - sum(
            rows[("c", step_count - 1, name)].floor for name in tanks_sorted))
    else:
        supplies[terminal] = -supplies[initial_node]

    arcs = []
    big = 1 << free_count

    def row_weight(row):
        scaled = denominator * row.frac.numerator // row.frac.denominator
        cost = denominator - 2 * scaled      # integer D*(1-2*frac)
        return cost * big + (1 << (free_count - 1 - row.position))

    def place(row, u, v):
        if row.pin == 1:
            supplies[u] -= 1
            supplies[v] += 1
        elif row.pin is None and row.frac > 0:
            arcs.append((u, v, 1, row_weight(row), row.key))
        # forced delta 0 (pin or integer exact value): no arc at all

    for name in tanks_sorted:
        row = rows[("i", name)]
        head = p_node(0, name) if step_count else terminal
        place(row, initial_node, head)
    for step_index, command in enumerate(job.steps):
        for transfer in command["transfers"]:
            row = rows[("t", step_index, transfer["id"])]
            place(row, p_node(step_index, transfer["source"]),
                  q_node(step_index, transfer["target"]))
        for name in tanks_sorted:
            row = rows[("c", step_index, name)]
            head = (p_node(step_index + 1, name)
                    if step_index + 1 < step_count else terminal)
            place(row, q_node(step_index, name), head)
        for name in tanks_sorted:
            if wide[step_index][name]:
                arcs.append((p_node(step_index, name),
                             q_node(step_index, name), 1, 0, None))

    flows = _min_cost_flow(node_count, arcs, supplies)
    if flows is None:
        return {"status": "conflict"}

    # --- assemble the balanced dataset --------------------------------------
    delta = {}
    for key, row in rows.items():
        if row.pin is not None:
            delta[key] = row.pin
        elif row.frac > 0:
            delta[key] = flows[key]
        else:
            delta[key] = 0
    error = ZERO
    for key, row in rows.items():
        error += row.frac if delta[key] == 0 else (1 - row.frac)
    initial_units = {name: rows[("i", name)].floor + delta[("i", name)]
                     for name in tanks_sorted}
    steps_out = []
    for step_index, command in enumerate(job.steps):
        transfer_units = {
            transfer["id"]: rows[("t", step_index, transfer["id"])].floor
            + delta[("t", step_index, transfer["id"])]
            for transfer in command["transfers"]}
        closing_units = {name: rows[("c", step_index, name)].floor
                         + delta[("c", step_index, name)]
                         for name in tanks_sorted}
        steps_out.append({"step_id": command["id"],
                          "transfer_units": transfer_units,
                          "closing_units": closing_units})
    return {"status": "balanced",
            "species": job.sigma,
            "quantum_g": job.quantum_text,
            "total_units": total_units,
            "initial_units": initial_units,
            "steps": steps_out,
            "absolute_error_units": str(error)}


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a
