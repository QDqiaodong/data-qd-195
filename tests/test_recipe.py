"""Acceptance tests for the offline recipe planner.

The fixtures are synthetic.  Every case runs the CLI in its own process, and
every planned result is replayed through the original v1 simulator entry
point so the reported order, exact final readings and costs are checked
against real execution rather than the planner's own bookkeeping.
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

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures" / "recipe"
sys.path.insert(0, str(SRC))


class RecipeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="recipe-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def env(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SRC)
        return environment

    def cli(self, *args, preexec=None):
        return subprocess.run(
            [sys.executable, "-m", "tank_transfer.recipe", *map(str, args)],
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
        self.assertEqual(
            proc.returncode, 0,
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

    def simulate(self, plan, name="plan.json", out="report.json"):
        """Replay a plan through the original v1 simulator entry point."""
        plan_path = self.write(name, plan)
        report_path = self.tmp / out
        proc = subprocess.run(
            [sys.executable, "-m", "tank_transfer.cli",
             "--source", plan_path, "--output", report_path],
            capture_output=True, text=True, env=self.env(), cwd=str(ROOT))
        self.assertEqual(
            proc.returncode, 0,
            msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
        return json.loads(report_path.read_text(encoding="utf-8"))

    def check_delivery(self, job, result):
        """Cross-check a planned result against the JOB and the v1 simulator."""
        self.assertEqual(result["status"], "planned")
        by_id = {a["id"]: a for a in job["actions"]}
        self.assertEqual(len(set(result["action_ids"])),
                         len(result["action_ids"]))
        for aid in result["action_ids"]:
            self.assertIn(aid, by_id)
        self.assertEqual(result["total_cost"],
                         sum(by_id[aid]["cost"] for aid in result["action_ids"]))
        # The emitted plan repeats the initial tanks/species and one transfer
        # per step in exactly the winning order.
        self.assertEqual(result["plan"]["species"], job["plan"]["species"])
        self.assertEqual(result["plan"]["tanks"], job["plan"]["tanks"])
        self.assertEqual(
            [step["id"] for step in result["plan"]["steps"]],
            [f"s{i}" for i in range(1, len(result["action_ids"]) + 1)])
        self.assertEqual(
            [step["transfers"][0]["id"] for step in result["plan"]["steps"]],
            result["action_ids"])
        for step, aid in zip(result["plan"]["steps"], result["action_ids"]):
            transfer = step["transfers"][0]
            action = by_id[aid]
            self.assertEqual(set(transfer), {"id", "source", "target",
                                             "volume_l"})
            self.assertEqual(len(step["transfers"]), 1)
            self.assertEqual(transfer["source"], action["source"])
            self.assertEqual(transfer["target"], action["target"])
            self.assertEqual(Fraction(transfer["volume_l"]),
                             Fraction(action["volume_l"]))
        # Replay and compare the exact final readings with ``final``.
        report = self.simulate(result["plan"])
        for tank, entry in result["final"].items():
            closing = report["final"][tank]
            self.assertEqual(entry["volume_l"], closing["volume_l"])
            for species, reading in entry.get("species", {}).items():
                self.assertEqual(reading["mass_g"],
                                 closing["mass_g"][species])
                self.assertEqual(reading["concentration_g_per_l"],
                                 closing["concentration_g_per_l"][species])
        # Every target and reserve really holds in the replayed final state.
        for target in job["targets"]:
            volume = Fraction(report["final"][target["tank"]]["volume_l"])
            self.assertGreater(volume, 0)
            mass = Fraction(
                report["final"][target["tank"]]["mass_g"][target["species"]])
            concentration = mass / volume
            self.assertGreaterEqual(concentration,
                                    Fraction(target["min_g_per_l"]))
            self.assertLessEqual(concentration,
                                 Fraction(target["max_g_per_l"]))
        for reserve in job["reserves"]:
            self.assertGreaterEqual(
                Fraction(report["final"][reserve["tank"]]["volume_l"]),
                Fraction(reserve["min_volume_l"]))
        return report


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ScenarioOneTests(RecipeCase):
    def test_order_matters_for_the_exact_target(self):
        job = load_fixture("scenario1-order.json")
        summary, result, _ = self.expect_ok(FIXTURES / "scenario1-order.json")
        self.assertEqual(summary, {"status": "planned", "total_cost": 2,
                                   "actions": 2})
        self.assertEqual(result["action_ids"], ["feed", "draw"])
        self.assertEqual(result["total_cost"], 2)
        self.check_delivery(job, result)
        self.assertEqual(result["final"]["C"]["species"]["salt"],
                         {"mass_g": "2", "concentration_g_per_l": "2"})
        self.assertEqual(result["final"]["A"], {"volume_l": "2"})
        # Drawing first would have read 0 g/L, so this order is required.
        swapped = json.loads(json.dumps(result["plan"]))
        swapped["steps"] = [swapped["steps"][1], swapped["steps"][0]]
        swapped["steps"][0]["id"] = "s1"
        swapped["steps"][1]["id"] = "s2"
        report = self.simulate(swapped, "swapped.json", "swapped-report.json")
        self.assertEqual(report["final"]["C"]["concentration_g_per_l"]["salt"],
                         "0")

    def test_single_action_cannot_reach_target(self):
        job = load_fixture("scenario1-order.json")
        for subset in (["feed"], ["draw"]):
            trimmed = json.loads(json.dumps(job))
            trimmed["actions"] = [a for a in job["actions"]
                                  if a["id"] in subset]
            _, result, _ = self.expect_ok(
                trimmed, f"{subset[0]}.json", f"{subset[0]}-result.json")
            self.assertEqual(result, {"status": "no_plan"})

    def test_tight_reserve_is_no_plan(self):
        summary, result, _ = self.expect_ok(
            FIXTURES / "scenario1-reserve-tight.json")
        self.assertEqual(summary, {"status": "no_plan"})
        self.assertEqual(result, {"status": "no_plan"})

    def test_reserve_is_independent_of_initial_validation(self):
        job = load_fixture("scenario1-order.json")
        job["reserves"][0]["min_volume_l"] = "5"
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result, {"status": "no_plan"})
        # A reserve above capacity is still a plan question, not a JOB error.
        job["reserves"][0]["min_volume_l"] = "6"
        _, result, _ = self.expect_ok(job, "job6.json", "result6.json")
        self.assertEqual(result, {"status": "no_plan"})


class ScenarioTwoTests(RecipeCase):
    def test_all_four_actions_are_needed(self):
        job = load_fixture("scenario2-all-four.json")
        summary, result, _ = self.expect_ok(FIXTURES / "scenario2-all-four.json")
        self.assertEqual(summary, {"status": "planned", "total_cost": 6,
                                   "actions": 4})
        self.assertEqual(result["action_ids"], ["a", "b", "c", "d"])
        self.check_delivery(job, result)
        for tank in ("C", "D"):
            self.assertEqual(
                result["final"][tank]["species"]["salt"],
                {"mass_g": "10", "concentration_g_per_l": "5"})

    def test_zero_target_drops_the_unneeded_action(self):
        job = load_fixture("scenario2-d-zero.json")
        summary, result, _ = self.expect_ok(FIXTURES / "scenario2-d-zero.json")
        self.assertEqual(summary, {"status": "planned", "total_cost": 5,
                                   "actions": 3})
        self.assertEqual(result["action_ids"], ["a", "b", "c"])
        self.check_delivery(job, result)
        self.assertEqual(result["final"]["D"]["species"]["salt"],
                         {"mass_g": "0", "concentration_g_per_l": "0"})

    def test_input_order_does_not_change_the_answer(self):
        job = load_fixture("scenario2-all-four.json")
        for index, actions in enumerate((
                list(reversed(job["actions"])),
                [job["actions"][2], job["actions"][0],
                 job["actions"][3], job["actions"][1]])):
            shuffled = json.loads(json.dumps(job))
            shuffled["actions"] = actions
            shuffled["targets"] = list(reversed(job["targets"]))
            _, result, _ = self.expect_ok(
                shuffled, f"shuffle{index}.json", f"shuffle{index}-result.json")
            self.assertEqual(result["action_ids"], ["a", "b", "c", "d"])
            self.assertEqual(result["total_cost"], 6)

    def test_order_changes_the_same_target_reading(self):
        job = load_fixture("scenario2-all-four.json")
        # Only c then d on D: the low-concentration draw must come first for
        # D to close at 5 g/L.  The winning order is already [c, d].
        only_d = json.loads(json.dumps(job))
        only_d["actions"] = [a for a in job["actions"] if a["id"] in "cd"]
        only_d["targets"] = [t for t in job["targets"] if t["tank"] == "D"]
        _, result, _ = self.expect_ok(only_d, "cd.json", "cd-result.json")
        self.assertEqual(result["action_ids"], ["c", "d"])
        self.assertEqual(result["final"]["D"]["species"]["salt"]["mass_g"], "10")
        # Forcing the reverse order (input reversed) still yields [c, d].
        only_d["actions"] = list(reversed(only_d["actions"]))
        _, result, _ = self.expect_ok(only_d, "dc.json", "dc-result.json")
        self.assertEqual(result["action_ids"], ["c", "d"])


class SemanticsTests(RecipeCase):
    def job(self, species, tanks, actions, targets, reserves=()):
        return {"plan": {"species": list(species), "tanks": tanks,
                         "steps": []},
                "actions": actions, "targets": targets,
                "reserves": list(reserves)}

    def tank(self, tid, capacity, volume, **masses):
        return {"id": tid, "capacity_l": capacity, "volume_l": volume,
                "mass_g": masses}

    def test_zero_action_plan_when_initial_state_already_matches(self):
        job = self.job(
            ["salt"],
            [self.tank("A", "4", "2", salt="4"),
             self.tank("B", "4", "0", salt="0")],
            [{"id": "move", "source": "A", "target": "B",
              "volume_l": "1", "cost": 1}],
            [{"tank": "A", "species": "salt", "min_g_per_l": "2",
              "max_g_per_l": "2"}],
            [{"tank": "A", "min_volume_l": "1"}])
        summary, result, _ = self.expect_ok(job)
        self.assertEqual(summary, {"status": "planned", "total_cost": 0,
                                   "actions": 0})
        self.assertEqual(result["action_ids"], [])
        self.assertEqual(result["plan"]["steps"], [])
        self.assertEqual(result["final"]["A"],
                         {"volume_l": "2",
                          "species": {"salt": {"mass_g": "4",
                                               "concentration_g_per_l": "2"}}})

    def test_tank_can_be_target_and_reserve_at_once(self):
        # A is both a concentration target and a reserved tank; ``final`` has a
        # single merged entry carrying volume and species, and the zero-action
        # plan already satisfies it.
        job = self.job(
            ["salt"],
            [self.tank("A", "6", "3", salt="6"),
             self.tank("B", "6", "0", salt="0")],
            [{"id": "draw", "source": "A", "target": "B",
              "volume_l": "2", "cost": 1}],
            [{"tank": "A", "species": "salt", "min_g_per_l": "2",
              "max_g_per_l": "2"}],
            [{"tank": "A", "min_volume_l": "1"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result["action_ids"], [])
        self.assertEqual(sorted(result["final"]), ["A"])
        self.assertEqual(result["final"]["A"],
                         {"volume_l": "3",
                          "species": {"salt": {"mass_g": "6",
                                               "concentration_g_per_l": "2"}}})
        # Raising the reserve past the opening volume forces a drain, which
        # then breaks the exact 2 g/L target: no plan can keep both.
        job["reserves"][0]["min_volume_l"] = "4"
        _, result, _ = self.expect_ok(job, "tight.json", "tight-result.json")
        self.assertEqual(result, {"status": "no_plan"})

    def test_empty_target_needs_positive_final_volume(self):
        job = self.job(
            ["salt"],
            [self.tank("A", "4", "2", salt="0"),
             self.tank("C", "4", "0", salt="0")],
            [{"id": "feed", "source": "A", "target": "C",
              "volume_l": "1", "cost": 1}],
            [{"tank": "C", "species": "salt", "min_g_per_l": "0",
              "max_g_per_l": "0"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result["action_ids"], ["feed"])
        self.assertEqual(result["final"]["C"]["volume_l"], "1")
        # With no liquid anywhere to move, C stays empty: volume must be > 0.
        unavailable = self.job(
            ["salt"],
            [self.tank("A", "4", "0", salt="0"),
             self.tank("C", "4", "0", salt="0")],
            [{"id": "feed", "source": "A", "target": "C",
              "volume_l": "1", "cost": 1}],
            [{"tank": "C", "species": "salt", "min_g_per_l": "0",
              "max_g_per_l": "0"}])
        _, result, _ = self.expect_ok(unavailable, "dry.json",
                                      "dry-result.json")
        self.assertEqual(result, {"status": "no_plan"})

    def test_target_drained_to_empty_is_no_plan(self):
        # A starts at 2 g/L, so only draining it could reach the exact 0 g/L
        # target, but the drain leaves A at zero volume: no plan.
        job = self.job(
            ["salt"],
            [self.tank("A", "4", "2", salt="4"),
             self.tank("B", "4", "0", salt="0")],
            [{"id": "drain", "source": "A", "target": "B",
              "volume_l": "2", "cost": 1}],
            [{"tank": "A", "species": "salt", "min_g_per_l": "0",
              "max_g_per_l": "0"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result, {"status": "no_plan"})
        # An empty target tank can never be a target at all.
        dry = self.job(
            ["salt"],
            [self.tank("A", "4", "2", salt="0"),
             self.tank("B", "4", "0", salt="0")],
            [{"id": "x", "source": "A", "target": "B",
              "volume_l": "1", "cost": 1}],
            [{"tank": "C", "species": "salt", "min_g_per_l": "0",
              "max_g_per_l": "0"}])
        self.assertEqual(dry["targets"][0]["tank"], "C")
        self.expect_error(dry, "unknown-tank.json", "unknown.json")

    def test_fractional_exact_concentration_and_inclusive_bounds(self):
        # 2 L drawn from 3 L carrying 1 g moves 2/3 g into 2 L => 1/3 g/L.
        base = self.job(
            ["salt"],
            [self.tank("A", "6", "3", salt="1"),
             self.tank("B", "6", "0", salt="0")],
            [{"id": "draw", "source": "A", "target": "B",
              "volume_l": "2", "cost": 1}],
            [{"tank": "B", "species": "salt", "min_g_per_l": "0.3",
              "max_g_per_l": "0.4"}])
        _, result, _ = self.expect_ok(base)
        self.assertEqual(result["action_ids"], ["draw"])
        self.assertEqual(result["final"]["B"]["species"]["salt"],
                         {"mass_g": "2/3", "concentration_g_per_l": "1/3"})
        # A 6-decimal band that does not contain exactly 1/3 has no plan.
        base["targets"][0].update(min_g_per_l="0.333334",
                                  max_g_per_l="0.34")
        _, result, _ = self.expect_ok(base, "close.json",
                                      "close-result.json")
        self.assertEqual(result, {"status": "no_plan"})
        # An inclusive upper endpoint at the exact value is accepted.
        base["targets"][0].update(min_g_per_l="0.333333",
                                  max_g_per_l="0.333334")
        _, result, _ = self.expect_ok(base, "upper.json",
                                      "upper-result.json")
        self.assertEqual(result["action_ids"], ["draw"])
        base["targets"][0].update(max_g_per_l="0.333333")
        _, result, _ = self.expect_ok(base, "tight.json",
                                      "tight-result.json")
        self.assertEqual(result, {"status": "no_plan"})
        # An exact 1/2 is reachable: 1 L from 2 L carrying 1 g.
        half = self.job(
            ["salt"],
            [self.tank("A", "6", "2", salt="1"),
             self.tank("B", "6", "0", salt="0")],
            [{"id": "draw", "source": "A", "target": "B",
              "volume_l": "1", "cost": 1}],
            [{"tank": "B", "species": "salt", "min_g_per_l": "0.5",
              "max_g_per_l": "0.5"}])
        _, result, _ = self.expect_ok(half, "half.json", "half-result.json")
        self.assertEqual(result["final"]["B"]["species"]["salt"],
                         {"mass_g": "1/2", "concentration_g_per_l": "1/2"})

    def test_multiple_species_are_tracked_independently(self):
        # One target per tank.  Salt and dye travel through separate tanks,
        # so the two species never interfere.
        job = self.job(
            ["salt", "dye"],
            [self.tank("A", "8", "4", salt="8", dye="0"),
             self.tank("B", "8", "4", salt="0", dye="4"),
             self.tank("C", "8", "0", salt="0", dye="0"),
             self.tank("D", "8", "0", salt="0", dye="0")],
            [{"id": "dye-in", "source": "B", "target": "D",
              "volume_l": "1", "cost": 1},
             {"id": "salt-in", "source": "A", "target": "C",
              "volume_l": "1", "cost": 1}],
            [{"tank": "C", "species": "salt", "min_g_per_l": "2",
              "max_g_per_l": "2"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result["action_ids"], ["salt-in"])
        self.assertEqual(result["final"]["C"]["species"],
                         {"salt": {"mass_g": "2",
                                   "concentration_g_per_l": "2"}})
        # The dye target forces the second action; both must be delivered.
        job["targets"].append({"tank": "D", "species": "dye",
                               "min_g_per_l": "1",
                               "max_g_per_l": "1"})
        _, result, _ = self.expect_ok(job, "both.json", "both-result.json")
        self.assertEqual(result["action_ids"], ["dye-in", "salt-in"])
        self.check_delivery(job, result)
        self.assertEqual(result["final"]["C"]["species"],
                         {"salt": {"mass_g": "2",
                                   "concentration_g_per_l": "2"}})
        self.assertEqual(result["final"]["D"]["species"],
                         {"dye": {"mass_g": "1",
                                  "concentration_g_per_l": "1"}})

    def test_undersupply_is_no_plan(self):
        job = self.job(
            ["salt"],
            [self.tank("A", "4", "1", salt="8"),
             self.tank("B", "4", "0", salt="0")],
            [{"id": "big", "source": "A", "target": "B",
              "volume_l": "2", "cost": 1}],
            [{"tank": "B", "species": "salt", "min_g_per_l": "0",
              "max_g_per_l": "10"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result, {"status": "no_plan"})

    def test_capacity_overflow_is_no_plan(self):
        # A holds salty liquid at 4 g/L; B is full to its 2 L capacity with
        # 0 g/L.  The only route into B overflows it, so the 0 g/L target in
        # B is reachable without action but a nonzero target is not.
        job = self.job(
            ["salt"],
            [self.tank("A", "4", "2", salt="8"),
             self.tank("B", "2", "2", salt="0")],
            [{"id": "overflow", "source": "A", "target": "B",
              "volume_l": "1", "cost": 1}],
            [{"tank": "B", "species": "salt", "min_g_per_l": "4",
              "max_g_per_l": "4"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result, {"status": "no_plan"})

    def test_partial_satisfaction_never_ships(self):
        job = self.job(
            ["salt"],
            [self.tank("A", "6", "2", salt="4"),
             self.tank("B", "6", "0", salt="0"),
             self.tank("C", "6", "0", salt="0")],
            [{"id": "ab", "source": "A", "target": "B",
              "volume_l": "1", "cost": 1},
             {"id": "ac", "source": "A", "target": "C",
              "volume_l": "1", "cost": 1}],
            [{"tank": "B", "species": "salt", "min_g_per_l": "2",
              "max_g_per_l": "2"},
             {"tank": "C", "species": "salt", "min_g_per_l": "5",
              "max_g_per_l": "5"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result, {"status": "no_plan"})
        self.assertEqual(set(result), {"status"})

    def test_cost_then_count_decides_the_winner(self):
        # C targets exactly 4 g/L.  One 2 L draw from E (4 g/L) reaches it in
        # one action; drawing 1 L from A (2 g/L) and 1 L from D (6 g/L) also
        # reaches it, in two actions.  With equal total cost the one-action
        # plan wins on the count tiebreak.
        job = self.job(
            ["salt"],
            [self.tank("A", "8", "2", salt="4"),
             self.tank("D", "8", "2", salt="12"),
             self.tank("E", "8", "4", salt="16"),
             self.tank("C", "8", "0", salt="0")],
            [{"id": "whole", "source": "E", "target": "C",
              "volume_l": "2", "cost": 4},
             {"id": "low", "source": "A", "target": "C",
              "volume_l": "1", "cost": 2},
             {"id": "high", "source": "D", "target": "C",
              "volume_l": "1", "cost": 2}],
            [{"tank": "C", "species": "salt", "min_g_per_l": "4",
              "max_g_per_l": "4"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result["total_cost"], 4)
        self.assertEqual(result["action_ids"], ["whole"])
        # Making the two-action plan cost 1 + 1 = 2 wins on cost outright.
        job["actions"][1]["cost"] = 1
        job["actions"][2]["cost"] = 1
        _, result, _ = self.expect_ok(job, "pair.json", "pair-result.json")
        self.assertEqual(result["total_cost"], 2)
        self.assertEqual(result["action_ids"], ["high", "low"])

    def test_lexicographic_tie_uses_character_order(self):
        job = self.job(
            ["salt"],
            [self.tank("A", "10", "6", salt="12"),
             self.tank("B", "10", "0", salt="0"),
             self.tank("C", "10", "0", salt="0"),
             self.tank("D", "10", "0", salt="0")],
            [{"id": "a2", "source": "A", "target": "B",
              "volume_l": "1", "cost": 1},
             {"id": "a10", "source": "A", "target": "C",
              "volume_l": "1", "cost": 1},
             {"id": "a1", "source": "A", "target": "D",
              "volume_l": "1", "cost": 1}],
            [{"tank": tank, "species": "salt", "min_g_per_l": "2",
              "max_g_per_l": "2"} for tank in ("B", "C", "D")])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result["action_ids"], ["a1", "a10", "a2"])

    def test_empty_reserve_list_and_zero_reserve(self):
        job = self.job(
            ["salt"],
            [self.tank("A", "4", "2", salt="4"),
             self.tank("B", "4", "0", salt="0")],
            [{"id": "x", "source": "A", "target": "B",
              "volume_l": "1", "cost": 1}],
            [{"tank": "B", "species": "salt", "min_g_per_l": "2",
              "max_g_per_l": "2"}])
        _, result, _ = self.expect_ok(job)
        self.assertEqual(result["action_ids"], ["x"])
        job["reserves"] = []
        _, result, _ = self.expect_ok(job, "none.json", "none-result.json")
        self.assertEqual(result["action_ids"], ["x"])
        job["reserves"] = [{"tank": "A", "min_volume_l": "0"}]
        _, result, _ = self.expect_ok(job, "zero.json", "zero-result.json")
        self.assertEqual(result["action_ids"], ["x"])


class MaxScenarioTests(RecipeCase):
    def test_max_job_is_exact_within_limits(self):
        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
            resource.setrlimit(resource.RLIMIT_AS,
                               (256 * 1024 * 1024, 256 * 1024 * 1024))

        job = load_fixture("scenario3-max.json")
        self.assertEqual(len(job["actions"]), 12)
        self.assertEqual(len(job["plan"]["tanks"]), 12)
        self.assertEqual(len(job["plan"]["species"]), 8)
        result_path = self.tmp / "max-result.json"
        proc = self.cli("--input", FIXTURES / "scenario3-max.json",
                        "--output", result_path, preexec=limits)
        self.assertEqual(proc.returncode, 0,
                         msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(proc.stdout),
                         {"status": "planned", "total_cost": 4, "actions": 4})
        self.assertEqual(result["action_ids"], ["b", "d", "e", "f"])
        self.assertEqual(result["total_cost"], 4)
        self.check_delivery(job, result)

    def test_input_order_does_not_change_the_max_answer(self):
        job = load_fixture("scenario3-max.json")
        shuffled = json.loads(json.dumps(job))
        shuffled["actions"] = list(reversed(job["actions"]))
        shuffled["targets"] = list(reversed(job["targets"]))
        shuffled["reserves"] = list(reversed(job["reserves"]))
        _, result, _ = self.expect_ok(shuffled, "max-shuffle.json",
                                      "max-shuffle-result.json")
        self.assertEqual(result["action_ids"], ["b", "d", "e", "f"])
        self.assertEqual(result["total_cost"], 4)


class RejectionTests(RecipeCase):
    def job(self):
        return load_fixture("scenario1-order.json")

    def test_duplicate_json_key(self):
        raw = (FIXTURES / "scenario1-order.json").read_text(
            encoding="utf-8").replace('"cost": 1', '"cost": 1, "cost": 1', 1)
        self.expect_error(None, raw=raw)

    def test_nonfinite_number(self):
        raw = (FIXTURES / "scenario1-order.json").read_text(
            encoding="utf-8").replace('"cost": 1', '"cost": NaN', 1)
        self.expect_error(None, raw=raw)

    def test_top_level_fields(self):
        job = self.job()
        job["extra"] = 1
        self.expect_error(job)
        job = self.job()
        del job["reserves"]
        self.expect_error(job, "missing.json")
        self.expect_error([1], "list.json")

    def test_plan_must_have_empty_steps(self):
        job = self.job()
        job["plan"]["steps"] = [{"id": "s1", "transfers": []}]
        self.expect_error(job)
        job = self.job()
        del job["plan"]["steps"]
        # ``steps`` may be omitted; a missing required tank/species is not.
        _, result, _ = self.expect_ok(job, "nos.json", "nos-result.json")
        self.assertEqual(result["action_ids"], ["feed", "draw"])
        job = self.job()
        del job["plan"]["tanks"]
        self.expect_error(job, "notanks.json")
        job = self.job()
        job["plan"]["extra"] = []
        self.expect_error(job, "extraplan.json")

    def test_plan_limits(self):
        job = self.job()
        job["plan"]["species"] = [f"s{i}" for i in range(9)]
        for tank in job["plan"]["tanks"]:
            tank["mass_g"] = {s: "0" for s in job["plan"]["species"]}
        job["actions"] = [{"id": "a1", "source": "A", "target": "B",
                           "volume_l": "1", "cost": 1}]
        job["targets"] = [{"tank": "C", "species": "s0",
                           "min_g_per_l": "0", "max_g_per_l": "0"}]
        self.expect_error(job, "species9.json")
        job = self.job()
        index = len(job["plan"]["tanks"])
        while len(job["plan"]["tanks"]) <= 12:
            job["plan"]["tanks"].append(
                {"id": f"T{index}", "capacity_l": "4", "volume_l": "0",
                 "mass_g": {"salt": "0"}})
            index += 1
        self.expect_error(job, "tanks13.json")

    def test_action_rejections(self):
        job = self.job()
        job["actions"][0]["cost"] = True
        self.expect_error(job)
        job = self.job()
        job["actions"][0]["cost"] = 0
        self.expect_error(job, "zero.json")
        job = self.job()
        job["actions"][0]["cost"] = 10001
        self.expect_error(job, "big.json")
        job = self.job()
        job["actions"][0]["cost"] = "1"
        self.expect_error(job, "str.json")
        job = self.job()
        job["actions"][0]["cost"] = 1.0
        self.expect_error(job, "float.json")
        job = self.job()
        job["actions"][0]["id"] = job["actions"][1]["id"]
        self.expect_error(job, "dup.json")
        job = self.job()
        job["actions"][0]["target"] = job["actions"][0]["source"]
        self.expect_error(job, "same.json")
        job = self.job()
        job["actions"][0]["source"] = "ghost"
        self.expect_error(job, "ghost.json")
        job = self.job()
        job["actions"][0]["volume_l"] = "0"
        self.expect_error(job, "zero-volume.json")
        job = self.job()
        job["actions"][0]["unexpected"] = 1
        self.expect_error(job, "field.json")
        job = self.job()
        job["actions"] = []
        self.expect_error(job, "empty.json")

    def test_action_count_limit(self):
        job = self.job()
        job["actions"] = [{"id": f"x{i:02d}", "source": "A", "target": "C",
                           "volume_l": "1", "cost": 1} for i in range(13)]
        self.expect_error(job, "thirteen.json")

    def test_target_rejections(self):
        job = self.job()
        job["targets"][0]["min_g_per_l"] = "3"
        job["targets"][0]["max_g_per_l"] = "2"
        self.expect_error(job)
        job = self.job()
        job["targets"][0]["species"] = "dye"
        self.expect_error(job, "species.json")
        job = self.job()
        job["targets"][0]["tank"] = "ghost"
        self.expect_error(job, "tank.json")
        job = self.job()
        job["targets"][0]["min_g_per_l"] = "-1"
        self.expect_error(job, "neg.json")
        job = self.job()
        job["targets"].append(dict(job["targets"][0]))
        self.expect_error(job, "dup.json")
        job = self.job()
        job["targets"] = []
        self.expect_error(job, "empty.json")
        job = self.job()
        job["targets"][0]["unit"] = "g/L"
        self.expect_error(job, "unit.json")

    def test_reserve_rejections(self):
        job = self.job()
        job["reserves"][0]["tank"] = "ghost"
        self.expect_error(job)
        job = self.job()
        job["reserves"][0]["min_volume_l"] = "-1"
        self.expect_error(job, "neg.json")
        job = self.job()
        job["reserves"].append(dict(job["reserves"][0]))
        self.expect_error(job, "dup.json")
        job = self.job()
        job["reserves"] = [{"tank": "A", "min_volume_l": "1"},
                           {"tank": "B", "min_volume_l": "1"}]
        # 13 reserves over the same 12 tanks is impossible; force the count.
        job["reserves"] = [{"tank": "A", "min_volume_l": "1"} for _ in range(13)]
        self.expect_error(job, "many.json")

    def test_input_size_limit(self):
        path = self.write("big.json", self.job())
        with path.open("a", encoding="utf-8") as handle:
            handle.truncate(524289)
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


class OutputExistsTests(RecipeCase):
    def test_existing_file_is_never_overwritten(self):
        target = self.tmp / "result.json"
        target.write_text("keep me", encoding="utf-8")
        proc = self.cli("--input", FIXTURES / "scenario1-order.json",
                        "--output", target)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")

    def test_existing_symlink_is_never_overwritten(self):
        real = self.tmp / "real.json"
        real.write_text("keep me", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(real)
        proc = self.cli("--input", FIXTURES / "scenario1-reserve-tight.json",
                        "--output", link)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(real.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(link.is_symlink())

    def test_no_plan_also_refuses_to_overwrite(self):
        target = self.tmp / "result.json"
        target.write_text("keep me", encoding="utf-8")
        proc = self.cli("--input", FIXTURES / "scenario1-reserve-tight.json",
                        "--output", target)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
