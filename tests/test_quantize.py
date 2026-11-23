"""Acceptance tests for the offline ledger quantizer.

The fixtures are synthetic.  Every case runs the CLI in its own process.  A
delivered dataset is checked against the v1 simulator's exact evidence with
an independent verifier (rounding domains, per-step posting, conserved
total, pins, reported error), and small jobs are additionally compared
against a brute-force enumeration of every legal rounding for the exact
minimal cost and the exact lexicographic tie-break.
"""

import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from itertools import product

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures" / "quantize"
sys.path.insert(0, str(SRC))

from tank_transfer.service import process as v1_process  # noqa: E402

ZERO = Fraction(0)
HALF = Fraction(1, 2)


# ---------------------------------------------------------------------------
# Independent exact evidence, verifier and brute-force enumerator
# ---------------------------------------------------------------------------

def exact_evidence(job):
    """Exact per-row masses of the selected species from the v1 simulator."""
    report = v1_process(job["plan"])
    sigma = job["species"]
    initial = {t: Fraction(e["mass_g"][sigma])
               for t, e in report["initial"].items()}
    steps = []
    for trace in report["steps"]:
        transfers = {}
        out_sum = {t: ZERO for t in initial}
        for record in trace["transfers"]:
            mass = Fraction(record["mass_g"][sigma])
            transfers[record["transfer_id"]] = (record["source"],
                                                record["target"], mass)
            out_sum[record["source"]] += mass
        closing = {t: Fraction(e["mass_g"][sigma])
                   for t, e in trace["closing"].items()}
        steps.append({"transfers": transfers, "out_sum": out_sum,
                      "closing": closing})
    return initial, steps


def floor_frac(x):
    floor = x.numerator // x.denominator
    return floor, x - floor


def round_half_even(x):
    floor, frac = floor_frac(x)
    if frac < HALF:
        return floor
    if frac > HALF:
        return floor + 1
    return floor if floor % 2 == 0 else floor + 1


def row_keys(job):
    """All ledger row keys in the canonical vector order."""
    tanks = sorted(t["id"] for t in job["plan"]["tanks"])
    keys = [("i", t) for t in tanks]
    for step in job["plan"]["steps"]:
        keys += [("t", step["id"], tid)
                 for tid in sorted(t["id"] for t in step["transfers"])]
        keys += [("c", step["id"], t) for t in tanks]
    return keys


def exact_units(job):
    """Map every canonical row key to its exact unit value."""
    quantum = Fraction(job["quantum_g"])
    initial, steps = exact_evidence(job)
    values = {}
    for tank, mass in initial.items():
        values[("i", tank)] = mass / quantum
    for step, trace in zip(job["plan"]["steps"], steps):
        sid = step["id"]
        for tid, (_src, _dst, mass) in trace["transfers"].items():
            values[("t", sid, tid)] = mass / quantum
        for tank, mass in trace["closing"].items():
            values[("c", sid, tank)] = mass / quantum
    return values


def check_dataset(case, job, result):
    """Independently verify a balanced dataset against v1 exact evidence."""
    case.assertEqual(result["status"], "balanced")
    case.assertEqual(result["species"], job["species"])
    case.assertEqual(result["quantum_g"], job["quantum_g"])
    case.assertIsInstance(result["total_units"], int)
    quantum = Fraction(job["quantum_g"])
    initial, steps = exact_evidence(job)
    tanks = sorted(initial)
    # Structure: every plan step appears, in plan order.
    case.assertEqual([s["step_id"] for s in result["steps"]],
                     [s["id"] for s in job["plan"]["steps"]])
    case.assertEqual(sorted(result["initial_units"]), tanks)
    # Rounding domain of every row, including zero rows.
    def check_domain(value, exact):
        floor, frac = floor_frac(exact)
        case.assertIn(value, {floor} if frac == 0 else {floor, floor + 1})
        return abs(Fraction(value) - exact)
    error = ZERO
    for tank in tanks:
        error += check_domain(result["initial_units"][tank],
                              initial[tank] / quantum)
    opening = dict(result["initial_units"])
    exact_opening = dict(initial)
    for step, trace in zip(result["steps"], steps):
        sid = step["step_id"]
        case.assertEqual(sorted(step["transfer_units"]),
                         sorted(trace["transfers"]))
        case.assertEqual(sorted(step["closing_units"]), tanks)
        for tid, (_src, _dst, mass) in trace["transfers"].items():
            error += check_domain(step["transfer_units"][tid], mass / quantum)
        for tank in tanks:
            error += check_domain(step["closing_units"][tank],
                                  trace["closing"][tank] / quantum)
        # Posting: deduct integer outflows from the integer opening; the
        # pre-arrival balance must sit inside the exact window, then integer
        # inflows are added to reach the posted closing.
        for tank in tanks:
            out_int = sum(step["transfer_units"][tid]
                          for tid, (src, _d, _m) in trace["transfers"].items()
                          if src == tank)
            in_int = sum(step["transfer_units"][tid]
                         for tid, (_s, dst, _m) in trace["transfers"].items()
                         if dst == tank)
            balance = opening[tank] - out_int
            exact_balance = ((exact_opening[tank] - trace["out_sum"][tank])
                             / quantum)
            low, frac = floor_frac(exact_balance)
            case.assertTrue(low <= balance <= low + (1 if frac else 0),
                            f"{sid} {tank}: balance {balance} outside "
                            f"[{low}, {low + (1 if frac else 0)}]")
            case.assertEqual(step["closing_units"][tank],
                             balance + in_int)
        opening = dict(step["closing_units"])
        exact_opening = dict(trace["closing"])
        # The conserved total must hold at this closing too.
        case.assertEqual(sum(step["closing_units"].values()),
                         result["total_units"])
    # The initial sum is the round-half-even of the exact total.
    exact_total = sum(initial.values()) / quantum
    case.assertEqual(result["total_units"], round_half_even(exact_total))
    case.assertEqual(sum(result["initial_units"].values()),
                     result["total_units"])
    # Pins are honoured verbatim.
    for pin in job["pins"]:
        if pin["kind"] == "initial":
            value = result["initial_units"][pin["item"]]
        elif pin["kind"] == "closing":
            value = next(s["closing_units"][pin["item"]]
                         for s in result["steps"]
                         if s["step_id"] == pin["step"])
        else:
            value = next(s["transfer_units"][pin["item"]]
                         for s in result["steps"]
                         if s["step_id"] == pin["step"])
        case.assertEqual(value, pin["units"])
    # The reported absolute error is the exact one.
    case.assertEqual(result["absolute_error_units"], str(error))
    return error


def enumerate_best(job):
    """Brute-force the exact optimum: ``(error, canonical vector)`` or None.

    Every row rounds to its floor or ceil, pins are forced, the per-step
    posting rules and the conserved total are checked directly, and ties are
    broken by the canonical row vector compared numerically.
    """
    quantum = Fraction(job["quantum_g"])
    initial, steps = exact_evidence(job)
    tanks = sorted(initial)
    keys = row_keys(job)
    exact = exact_units(job)
    pins = {}
    for pin in job["pins"]:
        if pin["kind"] == "initial":
            key = ("i", pin["item"])
        elif pin["kind"] == "closing":
            key = ("c", pin["step"], pin["item"])
        else:
            key = ("t", pin["step"], pin["item"])
        floor, frac = floor_frac(exact[key])
        domain = {floor} if frac == 0 else {floor, floor + 1}
        if pin["units"] not in domain:
            return None
        pins[key] = pin["units"] - floor
    floors = {key: floor_frac(x)[0] for key, x in exact.items()}
    fracs = {key: floor_frac(x)[1] for key, x in exact.items()}
    free = [key for key in keys if fracs[key] > 0 and key not in pins]
    total_units = round_half_even(sum(initial.values()) / quantum)
    # Exact intermediate windows per step and tank.
    windows = []
    exact_opening = dict(initial)
    for trace in steps:
        window = {}
        for tank in tanks:
            exact_balance = ((exact_opening[tank] - trace["out_sum"][tank])
                             / quantum)
            low, frac = floor_frac(exact_balance)
            window[tank] = (low, low + (1 if frac else 0))
        windows.append(window)
        exact_opening = dict(trace["closing"])
    best = None
    for assignment in product((0, 1), repeat=len(free)):
        delta = {key: pins.get(key, 0) for key in keys}
        delta.update(zip(free, assignment))
        values = {key: floors[key] + delta[key] for key in keys}
        if sum(values[("i", t)] for t in tanks) != total_units:
            continue
        opening = {t: values[("i", t)] for t in tanks}
        feasible = True
        for step, trace, window in zip(job["plan"]["steps"], steps, windows):
            sid = step["id"]
            for tank in tanks:
                out_int = sum(values[("t", sid, tid)]
                              for tid, (src, _d, _m)
                              in trace["transfers"].items() if src == tank)
                in_int = sum(values[("t", sid, tid)]
                             for tid, (_s, dst, _m)
                             in trace["transfers"].items() if dst == tank)
                balance = opening[tank] - out_int
                low, high = window[tank]
                if not low <= balance <= high:
                    feasible = False
                    break
                if values[("c", sid, tank)] != balance + in_int:
                    feasible = False
                    break
                opening[tank] = values[("c", sid, tank)]
            if not feasible:
                break
        if not feasible:
            continue
        error = sum(fracs[key] if delta[key] == 0 else 1 - fracs[key]
                    for key in keys)
        vector = [values[key] for key in keys]
        if best is None or (error, vector) < (best[0], best[1]):
            best = (error, vector)
    return best


def canonical_vector(job, result):
    """The delivered dataset flattened into the canonical row order."""
    values = dict(result["initial_units"])
    steps = {step["step_id"]: step for step in result["steps"]}
    vector = []
    for key in row_keys(job):
        if key[0] == "i":
            vector.append(values[key[1]])
        elif key[0] == "t":
            vector.append(steps[key[1]]["transfer_units"][key[2]])
        else:
            vector.append(steps[key[1]]["closing_units"][key[2]])
    return vector


# ---------------------------------------------------------------------------
# CLI scaffolding
# ---------------------------------------------------------------------------

class QuantizeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quantize-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def env(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SRC)
        return environment

    def cli(self, *args, preexec=None):
        return subprocess.run(
            [sys.executable, "-m", "tank_transfer.quantize", *map(str, args)],
            capture_output=True, text=True, env=self.env(), cwd=str(ROOT),
            preexec_fn=preexec)

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def solve(self, job, name="job.json", out="result.json"):
        path = self.write(name, job) if isinstance(job, dict) else Path(job)
        result_path = self.tmp / out
        proc = self.cli("--input", path, "--output", result_path)
        return proc, result_path

    def expect_ok(self, job, name="job.json", out="result.json"):
        proc, result_path = self.solve(job, name, out)
        self.assertEqual(proc.returncode, 0,
                         msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        return json.loads(proc.stdout), result, result_path

    def expect_error(self, job, name="job.json", out="result.json", raw=None):
        path = self.write(name, job) if raw is None else self.tmp / name
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        result_path = self.tmp / out
        proc = self.cli("--input", path, "--output", result_path)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r}")
        self.assertTrue(proc.stderr.startswith("error:"))
        self.assertFalse(result_path.exists())
        return proc

    def expect_balanced(self, job, name="job.json", out="result.json"):
        """Run the CLI and verify the dataset against v1 exact evidence."""
        summary, result, _ = self.expect_ok(job, name, out)
        self.assertEqual(summary["status"], "balanced")
        data = job if isinstance(job, dict) else json.loads(
            Path(job).read_text(encoding="utf-8"))
        check_dataset(self, data, result)
        return result

    def expect_conflict(self, job, name="job.json", out="result.json"):
        summary, result, _ = self.expect_ok(job, name, out)
        self.assertEqual(summary, {"status": "conflict"})
        self.assertEqual(result, {"status": "conflict"})
        return result

    def cross_check(self, job, name="job.json", out="result.json"):
        """Compare the CLI result with the brute-force exact optimum."""
        data = job if isinstance(job, dict) else json.loads(
            Path(job).read_text(encoding="utf-8"))
        best = enumerate_best(data)
        if best is None:
            self.expect_conflict(job, name, out)
            return None
        error, vector = best
        result = self.expect_balanced(job, name, out)
        self.assertEqual(result["absolute_error_units"], str(error))
        self.assertEqual(canonical_vector(data, result), vector)
        return result


def make_job(tanks, steps, pins=(), quantum="1", species="salt"):
    return {"plan": {"species": [species],
                     "tanks": [dict(id=tid, capacity_l=cap, volume_l=vol,
                                    mass_g={species: mass})
                               for tid, cap, vol, mass in tanks],
                     "steps": json.loads(json.dumps(list(steps)))},
            "species": species, "quantum_g": quantum, "pins": list(pins)}


def step(sid, *transfers):
    return {"id": sid, "transfers": [
        {"id": tid, "source": src, "target": dst, "volume_l": vol}
        for tid, src, dst, vol in transfers]}


CASE1_TANKS = [("A", "4", "4", "1"), ("B", "4", "0", "0"), ("C", "4", "0", "0"),
               ("D", "4", "0", "0"), ("E", "4", "0", "0")]
CASE1_STEPS = [step("s1", ("ab", "A", "B", "1"), ("ac", "A", "C", "1")),
               step("s2", ("bd", "B", "D", "1"), ("ce", "C", "E", "1"))]


class AcceptanceCaseOneTests(QuantizeCase):
    def test_no_pin_keeps_the_unit_in_a(self):
        job = make_job(CASE1_TANKS, CASE1_STEPS)
        result = self.expect_balanced(job)
        self.assertEqual(result["total_units"], 1)
        self.assertEqual(result["absolute_error_units"], "3")
        self.assertEqual(result["initial_units"],
                         {"A": 1, "B": 0, "C": 0, "D": 0, "E": 0})
        for s in result["steps"]:
            self.assertEqual(s["transfer_units"],
                             {tid: 0 for tid in s["transfer_units"]})
            self.assertEqual(s["closing_units"]["A"], 1)
            self.assertEqual(
                [s["closing_units"][t] for t in ("B", "C", "D", "E")],
                [0, 0, 0, 0])
        self.cross_check(job, "job1x.json", "result1x.json")

    def test_pinned_closing_pulls_the_unit_through(self):
        job = make_job(CASE1_TANKS, CASE1_STEPS,
                       pins=[{"kind": "closing", "item": "D", "step": "s2",
                              "units": 1}])
        result = self.expect_balanced(job)
        self.assertEqual(result["absolute_error_units"], "5")
        s1, s2 = result["steps"]
        self.assertEqual(s1["transfer_units"], {"ab": 1, "ac": 0})
        self.assertEqual(s1["closing_units"]["B"], 1)
        self.assertEqual(s2["transfer_units"]["bd"], 1)
        self.assertEqual(s2["closing_units"]["D"], 1)
        self.assertEqual(s1["closing_units"]["A"], 0)
        self.assertEqual(s2["closing_units"]["A"], 0)
        self.cross_check(job, "job2x.json", "result2x.json")

    def test_two_separately_feasible_pins_conflict_together(self):
        base = {"kind": "closing", "item": "D", "step": "s2", "units": 1}
        for tag, pins in (("close", [base]),
                          ("xfer", [{"kind": "transfer", "item": "ac",
                                     "step": "s1", "units": 1}])):
            job = make_job(CASE1_TANKS, CASE1_STEPS, pins=pins)
            self.expect_balanced(job, f"sep-{tag}.json",
                                 f"sep-{tag}-result.json")
        job = make_job(CASE1_TANKS, CASE1_STEPS,
                       pins=[base, {"kind": "transfer", "item": "ac",
                                    "step": "s1", "units": 1}])
        self.expect_conflict(job)

    def test_equal_cost_tie_prefers_ab_zero_ac_one(self):
        job = make_job(CASE1_TANKS, CASE1_STEPS,
                       pins=[{"kind": "closing", "item": "A", "step": "s1",
                              "units": 0}])
        result = self.expect_balanced(job)
        self.assertEqual(result["absolute_error_units"], "5")
        s1, _ = result["steps"]
        self.assertEqual(s1["transfer_units"], {"ab": 0, "ac": 1})
        self.cross_check(job, "job4x.json", "result4x.json")

    def test_three_litre_variant_prefers_cost_over_lex(self):
        job = make_job(CASE1_TANKS,
                       [step("s1", ("ab", "A", "B", "3")),
                        step("s2", ("bd", "B", "D", "3"))])
        result = self.expect_balanced(job)
        self.assertEqual(result["absolute_error_units"], "3/2")
        s1, s2 = result["steps"]
        self.assertEqual(s1["transfer_units"], {"ab": 1})
        self.assertEqual(s2["transfer_units"], {"bd": 1})
        self.assertEqual(s1["closing_units"]["B"], 1)
        self.assertEqual(s2["closing_units"]["D"], 1)
        self.cross_check(job, "job5x.json", "result5x.json")


CASE2_TANKS = [("A", "4", "1", "0.4"), ("B", "4", "1", "0.6"),
               ("C", "4", "0", "0")]


class AcceptanceCaseTwoTests(QuantizeCase):
    def test_simultaneous_step_posts_from_opening_only(self):
        job = make_job(CASE2_TANKS,
                       [step("s1", ("ba", "B", "A", "1"),
                             ("ac", "A", "C", "1"))],
                       pins=[{"kind": "initial", "item": "B", "units": 1}])
        result = self.expect_balanced(job)
        s1, = result["steps"]
        self.assertEqual(s1["transfer_units"], {"ac": 0, "ba": 1})
        self.assertEqual(result["initial_units"],
                         {"A": 0, "B": 1, "C": 0})
        self.cross_check(job, "job6x.json", "result6x.json")

    def test_borrowing_same_step_arrival_conflicts(self):
        job = make_job(CASE2_TANKS,
                       [step("s1", ("ba", "B", "A", "1"),
                             ("ac", "A", "C", "1"))],
                       pins=[{"kind": "initial", "item": "B", "units": 1},
                             {"kind": "transfer", "item": "ac", "step": "s1",
                              "units": 1}])
        self.expect_conflict(job)

    def test_full_swap_with_zero_exact_retention_conflicts(self):
        job = make_job(CASE2_TANKS[:2],
                       [step("s1", ("ba", "B", "A", "1"),
                             ("ab", "A", "B", "1"))],
                       pins=[{"kind": "initial", "item": "B", "units": 1},
                             {"kind": "transfer", "item": "ba", "step": "s1",
                              "units": 0}])
        self.expect_conflict(job)


class SemanticsTests(QuantizeCase):
    def test_zero_step_total_rounds_half_to_even(self):
        for mass, total in (("0.5", 0), ("1.5", 2), ("2.5", 2), ("3.5", 4)):
            job = make_job([("A", "4", "1", mass)], [], species="salt")
            result = self.expect_balanced(job, f"zero{mass}.json",
                                          f"zero{mass}-result.json")
            self.assertEqual(result["total_units"], total)
            self.assertEqual(result["steps"], [])
            self.assertEqual(sum(result["initial_units"].values()), total)
            self.assertEqual(result["absolute_error_units"], "1/2")

    def test_zero_step_lex_and_sum(self):
        job = make_job([("A", "4", "1", "0.5"), ("B", "4", "1", "0.5")], [])
        result = self.expect_balanced(job)
        self.assertEqual(result["total_units"], 1)
        self.assertEqual(result["initial_units"], {"A": 0, "B": 1})
        self.cross_check(job, "zerolex.json", "zerolex-result.json")

    def test_empty_transfer_step(self):
        job = make_job(CASE1_TANKS,
                       [step("s1", ("ab", "A", "B", "1")),
                        step("s2"),
                        step("s3", ("bd", "B", "D", "1"))])
        result = self.expect_balanced(job)
        self.assertEqual([s["step_id"] for s in result["steps"]],
                         ["s1", "s2", "s3"])
        self.assertEqual(result["steps"][1]["transfer_units"], {})
        self.assertEqual(result["steps"][1]["closing_units"],
                         result["steps"][0]["closing_units"])
        self.cross_check(job, "emptystep.json", "emptystep-result.json")

    def test_non_unit_quantum(self):
        # Exact masses are quarters of a gram; the quantum is 0.2 g, so the
        # exact unit values are multiples of 1.25.
        job = make_job([("A", "4", "4", "1"), ("B", "4", "0", "0")],
                       [step("s1", ("ab", "A", "B", "1"))],
                       quantum="0.2")
        result = self.expect_balanced(job)
        # A opens with exactly 5 units; the transfer carries exactly 1.25.
        self.assertEqual(result["total_units"], 5)
        self.assertIn(result["steps"][0]["transfer_units"]["ab"], (1, 2))
        self.cross_check(job, "quantum.json", "quantum-result.json")

    def test_fractional_quantum_string_is_republished_verbatim(self):
        job = make_job([("A", "4", "4", "1"), ("B", "4", "0", "0")],
                       [step("s1", ("ab", "A", "B", "1"))],
                       quantum="0.25")
        result = self.expect_balanced(job)
        self.assertEqual(result["quantum_g"], "0.25")
        self.assertEqual(result["total_units"], 4)
        self.assertEqual(result["steps"][0]["transfer_units"]["ab"], 1)
        self.assertEqual(result["absolute_error_units"], "0")

    def test_same_transfer_id_in_different_steps(self):
        job = make_job(CASE1_TANKS,
                       [step("s1", ("mv", "A", "B", "1")),
                        step("s2", ("mv", "B", "D", "1"))],
                       pins=[{"kind": "transfer", "item": "mv", "step": "s2",
                              "units": 1}])
        result = self.expect_balanced(job)
        s1, s2 = result["steps"]
        self.assertEqual(s1["transfer_units"], {"mv": 1})
        self.assertEqual(s2["transfer_units"], {"mv": 1})
        self.cross_check(job, "samename.json", "samename-result.json")

    def test_pin_to_floor_value(self):
        job = make_job(CASE1_TANKS, CASE1_STEPS,
                       pins=[{"kind": "transfer", "item": "ab", "step": "s1",
                              "units": 0}])
        result = self.expect_balanced(job)
        self.assertEqual(result["steps"][0]["transfer_units"]["ab"], 0)
        self.assertEqual(result["absolute_error_units"], "3")

    def test_out_of_domain_pin_is_a_business_conflict(self):
        for units in (2, 5):
            job = make_job(CASE1_TANKS, CASE1_STEPS,
                           pins=[{"kind": "transfer", "item": "ab",
                                  "step": "s1", "units": units}])
            self.expect_conflict(job, f"ood{units}.json",
                                 f"ood{units}-result.json")
        # An exact-integer row only admits its own value.
        job = make_job(CASE1_TANKS, CASE1_STEPS,
                       pins=[{"kind": "initial", "item": "A", "units": 2}])
        self.expect_conflict(job, "oodinit.json", "oodinit-result.json")
        job = make_job(CASE1_TANKS, CASE1_STEPS,
                       pins=[{"kind": "initial", "item": "A", "units": 1}])
        self.expect_balanced(job, "okinit.json", "okinit-result.json")

    def test_multi_species_plan_ledgers_one_species(self):
        job = make_job([("A", "4", "2", "1"), ("B", "4", "0", "0")],
                       [step("s1", ("ab", "A", "B", "1"))])
        job["plan"]["species"] = ["salt", "dye"]
        for tank in job["plan"]["tanks"]:
            tank["mass_g"]["dye"] = "3" if tank["id"] == "A" else "0"
        result = self.expect_balanced(job)
        self.assertEqual(result["species"], "salt")
        self.assertEqual(result["total_units"], 1)
        # The dye masses still flow through the exact v1 trajectory; only
        # salt enters the ledger.
        report = v1_process(job["plan"])
        self.assertEqual(report["final"]["B"]["mass_g"]["dye"], "3/2")

    def test_input_order_does_not_change_the_result(self):
        pins = [{"kind": "closing", "item": "D", "step": "s2", "units": 1},
                {"kind": "transfer", "item": "ac", "step": "s1", "units": 0},
                {"kind": "initial", "item": "B", "units": 0}]
        job = make_job(CASE1_TANKS, CASE1_STEPS, pins=pins)
        first = self.expect_balanced(job)
        shuffled = json.loads(json.dumps(job))
        shuffled["plan"]["tanks"] = list(reversed(shuffled["plan"]["tanks"]))
        for s in shuffled["plan"]["steps"]:
            s["transfers"] = list(reversed(s["transfers"]))
        shuffled["pins"] = list(reversed(pins))
        second = self.expect_balanced(shuffled, "shuffled.json",
                                      "shuffled-result.json")
        self.assertEqual(first, second)


class PhysicsPriorityTests(QuantizeCase):
    def test_last_step_physics_failure_beats_pin_conflict(self):
        # Step s2 drains more than B holds (physics failure), and the pin is
        # also outside its rounding domain: the plan is rejected with exit 2
        # before any business conflict is reported.
        job = make_job(CASE1_TANKS,
                       [step("s1", ("ab", "A", "B", "1")),
                        step("s2", ("bd", "B", "D", "2"))],
                       pins=[{"kind": "transfer", "item": "ab", "step": "s1",
                              "units": 9}])
        self.expect_error(job)

    def test_capacity_overflow_is_invalid_input(self):
        job = make_job([("A", "4", "4", "1"), ("B", "1", "0", "0")],
                       [step("s1", ("ab", "A", "B", "2"))])
        self.expect_error(job)


class DenseFixtureTests(QuantizeCase):
    def test_dense_fixture_matches_canonical_result_within_limits(self):
        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
            resource.setrlimit(resource.RLIMIT_AS,
                               (256 * 1024 * 1024, 256 * 1024 * 1024))

        job = json.loads((FIXTURES / "dense.json").read_text(encoding="utf-8"))
        result_path = self.tmp / "dense-result.json"
        proc = self.cli("--input", FIXTURES / "dense.json",
                        "--output", result_path, preexec=limits)
        self.assertEqual(proc.returncode, 0,
                         msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(proc.stdout)["status"], "balanced")
        self.assertEqual(result["total_units"], 2000000020)
        self.assertEqual(result["absolute_error_units"], "588")
        check_dataset(self, job, result)
        ceil, floor = 2500001, 2500000
        for rnd in range(1, 7):
            full, dest = ("X", "Z") if rnd % 2 == 1 else ("Z", "X")
            s1, s2 = result["steps"][2 * (rnd - 1):2 * rnd]
            self.assertEqual(s1["step_id"], f"r{rnd}s1")
            self.assertEqual(s2["step_id"], f"r{rnd}s2")
            # First step: full-row tanks 00..19 take the upper bound into
            # Y39, Y38, ..., Y22, Y01, Y00 respectively.
            expect = {i: (39 - i if i <= 17 else (1 if i == 18 else 0))
                      for i in range(20)}
            for i in range(20):
                for j in range(40):
                    self.assertEqual(
                        s1["transfer_units"][f"{full}{i:02d}_Y{j:02d}"],
                        ceil if expect[i] == j else floor)
            rich = set(expect.values())
            for j in range(40):
                self.assertEqual(s1["closing_units"][f"Y{j:02d}"],
                                 50000001 if j in rich else 50000000)
            for i in range(20):
                self.assertEqual(s1["closing_units"][f"{full}{i:02d}"], 0)
            # Second step: the two pins hold; the remaining upper-bound
            # transfers run Y22..Y39 into destination tanks 19..02.
            pinned = {(0, 0), (1, 1)}
            free_ceils = {(j, 19 - k) for k, j in enumerate(range(22, 40))}
            for j in range(40):
                for i in range(20):
                    self.assertEqual(
                        s2["transfer_units"][f"Y{j:02d}_{dest}{i:02d}"],
                        ceil if (j, i) in pinned | free_ceils else floor)
            self.assertEqual(s2["transfer_units"][f"Y00_{dest}00"], ceil)
            self.assertEqual(s2["transfer_units"][f"Y01_{dest}01"], ceil)
            for i in range(20):
                self.assertEqual(s2["closing_units"][f"{dest}{i:02d}"],
                                 100000001)
            for j in range(40):
                self.assertEqual(s2["closing_units"][f"Y{j:02d}"], 0)

    def test_dense_fixture_is_input_order_invariant(self):
        job = json.loads((FIXTURES / "dense.json").read_text(encoding="utf-8"))
        shuffled = json.loads(json.dumps(job))
        shuffled["plan"]["tanks"] = list(reversed(shuffled["plan"]["tanks"]))
        for s in shuffled["plan"]["steps"]:
            s["transfers"] = list(reversed(s["transfers"]))
        shuffled["pins"] = list(reversed(shuffled["pins"]))
        first = self.expect_balanced(job, "dense-a.json", "dense-a-result.json")
        second = self.expect_balanced(shuffled, "dense-b.json",
                                      "dense-b-result.json")
        self.assertEqual(first, second)


class CrossCheckTests(QuantizeCase):
    def test_small_jobs_against_brute_force(self):
        # Deterministic pseudo-random small jobs, each compared with the
        # brute-force exact optimum and tie-break.
        import random
        rng = random.Random(195)
        made = 0
        trial = 0
        while made < 24 and trial < 400:
            trial += 1
            tank_ids = ["A", "B", "C", "D"][:rng.randint(2, 4)]
            tanks = []
            for tid in tank_ids:
                volume = rng.randint(0, 4)
                mass = rng.randint(0, 8) if volume else 0
                tanks.append((tid, "8", str(volume),
                              f"{mass}.{rng.choice([0, 2, 5])}"))
            steps = []
            for si in range(rng.randint(0, 3)):
                transfers = []
                for ti in range(rng.randint(0, 3)):
                    src, dst = rng.sample(tank_ids, 2)
                    transfers.append((f"t{ti}", src, dst,
                                      f"0.{rng.randint(1, 5)}"))
                steps.append(step(f"s{si}", *transfers))
            job = make_job(tanks, steps)
            try:
                v1_process(job["plan"])
            except ValueError:
                continue
            keys = row_keys(job)
            exact = exact_units(job)
            pins = []
            for key in rng.sample(keys, min(len(keys), rng.randint(0, 3))):
                floor, frac = floor_frac(exact[key])
                units = floor + (1 if frac and rng.random() < 0.5 else 0)
                kind = {"i": "initial", "t": "transfer", "c": "closing"}[key[0]]
                pin = {"kind": kind, "item": key[-1], "units": units}
                if kind != "initial":
                    pin["step"] = key[1]
                pins.append(pin)
            job["pins"] = pins
            pinned_keys = set()
            for pin in pins:
                if pin["kind"] == "initial":
                    pinned_keys.add(("i", pin["item"]))
                elif pin["kind"] == "closing":
                    pinned_keys.add(("c", pin["step"], pin["item"]))
                else:
                    pinned_keys.add(("t", pin["step"], pin["item"]))
            free = sum(1 for key in keys
                       if floor_frac(exact[key])[1] > 0
                       and key not in pinned_keys)
            if free > 14:
                continue
            self.cross_check(job, f"xf{trial}.json", f"xf{trial}-result.json")
            made += 1
        self.assertGreaterEqual(made, 12)


class FixtureFileTests(QuantizeCase):
    """Replay the committed synthetic fixtures and check canonical results."""

    def check(self, name, total, error):
        job = json.loads((FIXTURES / f"{name}.json").read_text(
            encoding="utf-8"))
        result = self.expect_balanced(FIXTURES / f"{name}.json",
                                      f"{name}.json", f"{name}-result.json")
        self.assertEqual(result["total_units"], total)
        self.assertEqual(result["absolute_error_units"], error)
        return job, result

    def test_split_chain(self):
        job, result = self.check("split-chain", 1, "3")
        self.assertEqual(canonical_vector(job, result),
                         [1, 0, 0, 0, 0] + [0, 0, 1, 0, 0, 0, 0]
                         + [0, 0, 1, 0, 0, 0, 0])
        self.cross_check(FIXTURES / "split-chain.json", "fx-sc.json",
                         "fx-sc-result.json")

    def test_split_chain_pin_closing(self):
        _, result = self.check("split-chain-pin-closing", 1, "5")
        s1, s2 = result["steps"]
        self.assertEqual(s1["transfer_units"], {"ab": 1, "ac": 0})
        self.assertEqual(s1["closing_units"]["B"], 1)
        self.assertEqual(s1["closing_units"]["A"], 0)
        self.assertEqual(s2["transfer_units"]["bd"], 1)
        self.assertEqual(s2["closing_units"]["D"], 1)
        self.assertEqual(s2["closing_units"]["A"], 0)

    def test_split_chain_pins_conflict(self):
        summary, result, _ = self.expect_ok(
            FIXTURES / "split-chain-pins-conflict.json",
            "fx-spc.json", "fx-spc-result.json")
        self.assertEqual(summary, {"status": "conflict"})
        self.assertEqual(result, {"status": "conflict"})

    def test_split_chain_tie(self):
        _, result = self.check("split-chain-tie", 1, "5")
        self.assertEqual(result["steps"][0]["transfer_units"],
                         {"ab": 0, "ac": 1})

    def test_split_chain_three_litre(self):
        _, result = self.check("split-chain-three-litre", 1, "3/2")
        self.assertEqual(result["steps"][0]["transfer_units"], {"ab": 1})
        self.assertEqual(result["steps"][1]["transfer_units"], {"bd": 1})

    def test_simultaneous(self):
        _, result = self.check("simultaneous", 1, "12/5")
        self.assertEqual(result["steps"][0]["transfer_units"],
                         {"ac": 0, "ba": 1})

    def test_simultaneous_borrow_conflict(self):
        _, result, _ = self.expect_ok(
            FIXTURES / "simultaneous-borrow-conflict.json",
            "fx-sbc.json", "fx-sbc-result.json")
        self.assertEqual(result, {"status": "conflict"})

    def test_swap_conflict(self):
        _, result, _ = self.expect_ok(FIXTURES / "swap-conflict.json",
                                      "fx-swap.json", "fx-swap-result.json")
        self.assertEqual(result, {"status": "conflict"})

    def test_zero_step_half_even(self):
        _, result = self.check("zero-step-half-even", 2, "1/2")
        self.assertEqual(result["initial_units"], {"A": 2})
        self.assertEqual(result["steps"], [])

    def test_quarter_gram_quantum(self):
        _, result = self.check("quarter-gram-quantum", 4, "0")
        self.assertEqual(result["quantum_g"], "0.25")
        self.assertEqual(result["steps"][0]["transfer_units"]["ab"], 1)


class RejectionTests(QuantizeCase):
    def job(self):
        return make_job(CASE1_TANKS, CASE1_STEPS)

    def test_duplicate_json_key(self):
        raw = json.dumps(self.job()).replace('"quantum_g": "1"',
                                             '"quantum_g": "1", "quantum_g": "1"', 1)
        self.expect_error(None, raw=raw)

    def test_nonfinite_number(self):
        raw = json.dumps(self.job()).replace('"quantum_g": "1"',
                                             '"quantum_g": NaN', 1)
        self.expect_error(None, raw=raw)

    def test_top_level_fields(self):
        job = self.job()
        job["extra"] = 1
        self.expect_error(job)
        job = self.job()
        del job["pins"]
        self.expect_error(job, "missing.json")
        self.expect_error([1], "list.json")

    def test_selector_must_be_a_known_species(self):
        job = self.job()
        job["species"] = "dye"
        self.expect_error(job)
        job = self.job()
        job["species"] = 1
        self.expect_error(job, "intsel.json")

    def test_quantum_must_be_positive_v1_number(self):
        for quantum in ("0", "-1", "1e0", "abc", 1, True):
            job = self.job()
            job["quantum_g"] = quantum
            self.expect_error(job, f"q{len(str(quantum))}.json")

    def test_plan_limits(self):
        job = self.job()
        job["plan"]["species"] = [f"s{i}" for i in range(9)]
        for tank in job["plan"]["tanks"]:
            tank["mass_g"] = {s: "0" for s in job["plan"]["species"]}
        self.expect_error(job, "species9.json")
        job = self.job()
        while len(job["plan"]["tanks"]) <= 80:
            job["plan"]["tanks"].append(
                {"id": f"T{len(job['plan']['tanks'])}", "capacity_l": "4",
                 "volume_l": "0", "mass_g": {"salt": "0"}})
        self.expect_error(job, "tanks81.json")
        job = self.job()
        job["plan"]["steps"] = [step(f"s{i}") for i in range(17)]
        self.expect_error(job, "steps17.json")

    def test_plan_must_be_a_full_v1_plan(self):
        job = self.job()
        del job["plan"]["steps"]
        self.expect_error(job)
        job = self.job()
        job["plan"]["extra"] = []
        self.expect_error(job, "extraplan.json")
        job = self.job()
        job["plan"]["steps"][0]["transfers"][0]["volume_l"] = "0"
        self.expect_error(job, "zerovol.json")
        job = self.job()
        job["plan"]["steps"][0]["transfers"][1] = dict(
            job["plan"]["steps"][0]["transfers"][0])
        self.expect_error(job, "duptransfer.json")

    def test_pin_rejections(self):
        base = self.job()
        # unknown tank / step / transfer rows
        job = self.job()
        job["pins"] = [{"kind": "initial", "item": "ghost", "units": 0}]
        self.expect_error(job)
        job = self.job()
        job["pins"] = [{"kind": "closing", "item": "A", "step": "s9",
                        "units": 0}]
        self.expect_error(job, "ghoststep.json")
        job = self.job()
        job["pins"] = [{"kind": "transfer", "item": "zz", "step": "s1",
                        "units": 0}]
        self.expect_error(job, "ghosttransfer.json")
        # duplicate locations are invalid even with equal values
        job = self.job()
        job["pins"] = [{"kind": "initial", "item": "A", "units": 1},
                       {"kind": "initial", "item": "A", "units": 1}]
        self.expect_error(job, "duppin.json")
        # unknown kind, missing/extra fields, bad types
        job = self.job()
        job["pins"] = [{"kind": "opening", "item": "A", "units": 1}]
        self.expect_error(job, "badkind.json")
        job = self.job()
        job["pins"] = [{"kind": "initial", "item": "A"}]
        self.expect_error(job, "missingfield.json")
        job = self.job()
        job["pins"] = [{"kind": "initial", "item": "A", "units": 1,
                        "note": "x"}]
        self.expect_error(job, "extrafield.json")
        job = self.job()
        job["pins"] = [{"kind": "initial", "item": "A", "units": True}]
        self.expect_error(job, "boolunits.json")
        job = self.job()
        job["pins"] = [{"kind": "initial", "item": "A", "units": -1}]
        self.expect_error(job, "negunits.json")
        job = self.job()
        job["pins"] = [{"kind": "initial", "item": "A", "units": 10 ** 18 + 1}]
        self.expect_error(job, "bigunits.json")
        job = self.job()
        job["pins"] = [{"kind": "initial", "item": "A", "units": 1.0}]
        self.expect_error(job, "floatunits.json")
        job = self.job()
        job["pins"] = [{"kind": "closing", "item": "A", "units": 1}]
        self.expect_error(job, "nostep.json")
        job = self.job()
        job["pins"] = [dict(kind="initial", item="A", units=0)] * 0 + \
            [{"kind": "initial", "item": "A", "units": 0}] * 2049
        self.expect_error(job, "toomany.json")
        self.assertEqual(base["species"], "salt")

    def test_input_size_limit(self):
        path = self.write("big.json", self.job())
        with path.open("a", encoding="utf-8") as handle:
            handle.truncate(2097153)
        result_path = self.tmp / "big-result.json"
        proc = self.cli("--input", path, "--output", result_path)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(result_path.exists())

    def test_missing_input_file(self):
        result_path = self.tmp / "result.json"
        proc = self.cli("--input", self.tmp / "absent.json",
                        "--output", result_path)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(result_path.exists())


class OutputExistsTests(QuantizeCase):
    def job(self):
        return make_job(CASE1_TANKS, CASE1_STEPS)

    def test_existing_file_is_never_overwritten(self):
        target = self.tmp / "result.json"
        target.write_text("keep me", encoding="utf-8")
        proc = self.cli("--input", self.write("job.json", self.job()),
                        "--output", target)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")

    def test_existing_symlink_is_never_overwritten(self):
        real = self.tmp / "real.json"
        real.write_text("keep me", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(real)
        proc = self.cli("--input", self.write("job.json", self.job()),
                        "--output", link)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(real.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(link.is_symlink())

    def test_conflict_also_refuses_to_overwrite(self):
        target = self.tmp / "result.json"
        target.write_text("keep me", encoding="utf-8")
        job = make_job(CASE1_TANKS, CASE1_STEPS,
                       pins=[{"kind": "transfer", "item": "ab", "step": "s1",
                              "units": 7}])
        proc = self.cli("--input", self.write("job.json", job),
                        "--output", target)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
