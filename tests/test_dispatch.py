"""Acceptance tests for the offline pump work-order scheduler.

The fixtures are synthetic.  Every run uses a real subprocess so the
scheduler is exercised through its own process entry point, and the emitted
plans are replayed through the original v1 simulator entry point.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures" / "dispatch"
sys.path.insert(0, str(SRC))


class DispatchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def env(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SRC)
        return environment

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tank_transfer.dispatch", *map(str, args)],
            capture_output=True, text=True, env=self.env(), cwd=str(ROOT))

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def schedule(self, job, name="job.json", out="result.json"):
        path = self.write(name, job) if isinstance(job, dict) else Path(job)
        result_path = self.tmp / out
        proc = self.cli("--input", path, "--output", result_path)
        return proc, result_path

    def expect_ok(self, job, name="job.json", out="result.json"):
        proc, result_path = self.schedule(job, name, out)
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
        self.assertEqual(proc.returncode, 2,
                         msg=f"stdout={proc.stdout!r}")
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


def ticks(result):
    return [[transfer["id"] for transfer in step["transfers"]]
            for step in result["plan"]["steps"]]


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ScenarioOneTests(DispatchCase):
    def test_exact_2_requires_three_ticks(self):
        summary, result, _ = self.expect_ok(FIXTURES / "scenario1-exact-2.json")
        self.assertEqual(summary, {"status": "scheduled", "ticks": 3})
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(ticks(result), [["p"], ["q"], ["r"]])
        self.assertEqual(result["accept_readings_g_per_l"], {"r": "2"})

        # The plan is a standalone v1 document for the original simulator.
        job = load_fixture("scenario1-exact-2.json")
        self.assertEqual(result["plan"]["species"], job["species"])
        self.assertEqual(result["plan"]["tanks"], job["tanks"])
        self.assertEqual([step["id"] for step in result["plan"]["steps"]],
                         ["s1", "s2", "s3"])
        report = self.simulate(result["plan"])
        sent = {transfer["transfer_id"]: transfer
                for step in report["steps"] for transfer in step["transfers"]}
        self.assertEqual(sent["r"]["mass_g"]["salt"], "2")
        # Sent mass divided by sent volume matches the published reading.
        for oid, reading in result["accept_readings_g_per_l"].items():
            from fractions import Fraction
            self.assertEqual(
                Fraction(sent[oid]["mass_g"]["salt"])
                / Fraction(sent[oid]["volume_l"]), Fraction(reading))

    def test_exact_3_shares_first_tick(self):
        summary, result, _ = self.expect_ok(FIXTURES / "scenario1-exact-3.json")
        self.assertEqual(summary, {"status": "scheduled", "ticks": 2})
        self.assertEqual(ticks(result), [["p", "q"], ["r"]])
        self.assertEqual(result["accept_readings_g_per_l"], {"r": "3"})
        report = self.simulate(result["plan"])
        first, second = report["steps"]
        sent_q, = [t for t in first["transfers"] if t["transfer_id"] == "q"]
        sent_r, = [t for t in second["transfers"] if t["transfer_id"] == "r"]
        self.assertEqual(sent_q["mass_g"]["salt"], "0")
        self.assertEqual(sent_r["mass_g"]["salt"], "3")

    def test_gate_endpoints_are_inclusive(self):
        job = load_fixture("scenario1-exact-2.json")
        job["orders"][2]["accept"] = {
            "species": "salt", "min_g_per_l": "0", "max_g_per_l": "2"}
        _, result, _ = self.expect_ok(job)
        self.assertEqual(ticks(result), [["p"], ["q"], ["r"]])
        self.assertEqual(result["accept_readings_g_per_l"], {"r": "2"})
        job["orders"][2]["accept"] = {
            "species": "salt", "min_g_per_l": "3", "max_g_per_l": "4"}
        _, result, _ = self.expect_ok(job, "job2.json", "result2.json")
        self.assertEqual(ticks(result), [["p", "q"], ["r"]])
        self.assertEqual(result["accept_readings_g_per_l"], {"r": "3"})

    def test_fractional_reading_is_reduced(self):
        job = load_fixture("scenario1-exact-2.json")
        job["tanks"][0]["mass_g"]["salt"] = "1"
        job["orders"][2]["accept"] = {
            "species": "salt", "min_g_per_l": "2", "max_g_per_l": "3"}
        _, result, _ = self.expect_ok(job)
        # A opens the last tick with 14/3 g in 2 L.
        self.assertEqual(ticks(result), [["p"], ["q"], ["r"]])
        self.assertEqual(result["accept_readings_g_per_l"], {"r": "7/3"})


class ScenarioTwoTests(DispatchCase):
    def test_precedence_forces_global_interleave(self):
        _, result, _ = self.expect_ok(FIXTURES / "scenario2-after.json")
        self.assertEqual(ticks(result), [["b", "d"], ["a", "c"]])
        self.assertEqual(result["accept_readings_g_per_l"], {})

    def test_open_job_takes_lexicographic_minimum(self):
        _, result, _ = self.expect_ok(FIXTURES / "scenario2-open.json")
        self.assertEqual(ticks(result), [["a", "c"], ["b", "d"]])

    def test_input_order_is_not_a_priority(self):
        job = load_fixture("scenario2-open.json")
        job["orders"].reverse()
        _, result, _ = self.expect_ok(job)
        self.assertEqual(ticks(result), [["a", "c"], ["b", "d"]])
        job["orders"] = [job["orders"][2], job["orders"][0],
                         job["orders"][3], job["orders"][1]]
        _, result, _ = self.expect_ok(job, "job2.json", "result2.json")
        self.assertEqual(ticks(result), [["a", "c"], ["b", "d"]])

    def test_shared_pump_with_precedence_is_unschedulable(self):
        summary, result, result_path = self.expect_ok(
            FIXTURES / "scenario2-pump-conflict.json")
        self.assertEqual(summary, {"status": "unschedulable"})
        # No partial plan is ever attached.
        self.assertEqual(result, {"status": "unschedulable"})

    def test_shorter_prefix_list_wins_tie_break(self):
        job = load_fixture("scenario2-open.json")
        # Three independent orders, three pumps, two ticks: [x] < [x,y] as
        # lists, so [[x],[y,z]] must beat [[x,y],[z]].
        job["orders"] = [dict(order, id=oid, pump=f"pump-{oid}")
                         for order, oid in zip(job["orders"][:3], "xyz")]
        _, result, _ = self.expect_ok(job)
        self.assertEqual(ticks(result), [["x"], ["y", "z"]])


class ScenarioThreeTests(DispatchCase):
    def test_max_job_is_unschedulable_within_resource_limits(self):
        def limits():
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
            resource.setrlimit(resource.RLIMIT_AS,
                               (256 * 1024 * 1024, 256 * 1024 * 1024))

        result_path = self.tmp / "result.json"
        proc = subprocess.run(
            [sys.executable, "-m", "tank_transfer.dispatch",
             "--input", str(FIXTURES / "scenario3-max.json"),
             "--output", str(result_path)],
            capture_output=True, text=True, env=self.env(), cwd=str(ROOT),
            preexec_fn=limits)
        self.assertEqual(proc.returncode, 0,
                         msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
        self.assertEqual(json.loads(proc.stdout),
                         {"status": "unschedulable"})
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")),
                         {"status": "unschedulable"})

    def test_eleven_uv_orders_alone_fit_eleven_ticks(self):
        summary, result, _ = self.expect_ok(FIXTURES / "scenario3-uv-only.json")
        self.assertEqual(summary, {"status": "scheduled", "ticks": 11})
        plan_ticks = ticks(result)
        self.assertEqual(len(plan_ticks), 11)
        self.assertEqual(sorted(sum(plan_ticks, [])),
                         [f"u{i:02d}" for i in range(1, 12)])
        report = self.simulate(result["plan"])
        self.assertEqual(report["final"]["V"]["volume_l"], "33/2")


class SemanticsTests(DispatchCase):
    def base(self, tanks, orders, tick_limit):
        return {"species": ["salt"], "tanks": tanks, "orders": orders,
                "tick_limit": tick_limit}

    def tank(self, tid, capacity, volume, mass="0"):
        return {"id": tid, "capacity_l": capacity, "volume_l": volume,
                "mass_g": {"salt": mass}}

    def test_shared_tick_capacity_relief(self):
        # Neither target has room on its own; only sharing one tick, where
        # each tank also drains 3 L, makes both transfers fit.
        job = self.base(
            [self.tank("T0", "6", "5", "9"), self.tank("T1", "8", "6", "7")],
            [{"id": "e", "source": "T1", "target": "T0", "volume_l": "3",
              "pump": "R", "after": []},
             {"id": "g", "source": "T0", "target": "T1", "volume_l": "3",
              "pump": "P", "after": []}],
            4)
        _, result, _ = self.expect_ok(job)
        self.assertEqual(ticks(result), [["e", "g"]])

    def test_same_source_pair_uses_combined_outflow(self):
        tanks = [self.tank("S", "9", "3"), self.tank("T0", "9", "0"),
                 self.tank("T1", "9", "0")]
        orders = [
            {"id": "a", "source": "S", "target": "T0", "volume_l": "2",
             "pump": "P", "after": []},
            {"id": "b", "source": "S", "target": "T1", "volume_l": "2",
             "pump": "Q", "after": []}]
        # 2+2 > 3 opening litres in one tick, and no refill ever arrives.
        _, result, _ = self.expect_ok(self.base(tanks, orders, 2))
        self.assertEqual(result, {"status": "unschedulable"})
        # 2+2 <= 5: the pair shares a single tick.
        tanks[0]["volume_l"] = "5"
        _, result, _ = self.expect_ok(self.base(tanks, orders, 2),
                                      "job2.json", "result2.json")
        self.assertEqual(ticks(result), [["a", "b"]])

    def test_arrival_cannot_fund_same_tick_outflow(self):
        job = self.base(
            [self.tank("S", "9", "1"), self.tank("T", "9", "0"),
             self.tank("U", "9", "5")],
            [{"id": "x", "source": "S", "target": "T", "volume_l": "2",
              "pump": "P", "after": []},
             {"id": "y", "source": "U", "target": "S", "volume_l": "2",
              "pump": "Q", "after": []}],
            2)
        # x reads the opening litre only, so y must land a tick earlier.
        _, result, _ = self.expect_ok(job)
        self.assertEqual(ticks(result), [["y"], ["x"]])


class RejectionTests(DispatchCase):
    def job(self):
        return load_fixture("scenario1-exact-2.json")

    def test_duplicate_json_key(self):
        raw = (FIXTURES / "scenario1-exact-2.json").read_text(
            encoding="utf-8").replace('"tick_limit": 3',
                                      '"tick_limit": 3, "tick_limit": 3')
        self.expect_error(None, raw=raw)

    def test_nonfinite_number(self):
        raw = (FIXTURES / "scenario1-exact-2.json").read_text(
            encoding="utf-8").replace('"tick_limit": 3', '"tick_limit": NaN')
        self.expect_error(None, raw=raw)

    def test_tick_limit_type_and_range(self):
        for bad in (True, 0, 15, "3", 3.5, None):
            job = self.job()
            job["tick_limit"] = bad
            self.expect_error(job, f"tick-{type(bad).__name__}-{bad}.json")

    def test_top_level_fields(self):
        job = self.job()
        job["extra"] = 1
        self.expect_error(job)
        job = self.job()
        del job["orders"]
        self.expect_error(job, "missing.json")
        self.expect_error([1, 2], "list.json")

    def test_dependency_rejections(self):
        job = self.job()
        job["orders"][2]["after"] = ["p", "ghost"]
        self.expect_error(job)
        job = self.job()
        job["orders"][2]["after"] = ["r"]
        self.expect_error(job, "self.json")
        job = self.job()
        job["orders"][0]["after"] = ["r"]
        self.expect_error(job, "cycle.json")
        job = self.job()
        job["orders"][2]["after"] = ["p", "p"]
        self.expect_error(job, "dup.json")

    def test_order_field_rejections(self):
        job = self.job()
        job["orders"][0]["id"] = "q"
        self.expect_error(job)  # duplicate order id
        job = self.job()
        job["orders"][0]["volume_l"] = "0"
        self.expect_error(job, "zero.json")
        job = self.job()
        job["orders"][0]["volume_l"] = 1
        self.expect_error(job, "typed.json")
        job = self.job()
        job["orders"][0]["target"] = "B"
        self.expect_error(job, "same.json")
        job = self.job()
        job["orders"][0]["source"] = "ghost"
        self.expect_error(job, "ghost.json")
        job = self.job()
        job["orders"][0]["unexpected"] = True
        self.expect_error(job, "field.json")
        job = self.job()
        del job["orders"][0]["pump"]
        self.expect_error(job, "pump.json")

    def test_accept_rejections(self):
        accept = {"species": "salt", "min_g_per_l": "2", "max_g_per_l": "2"}
        job = self.job()
        job["orders"][0]["accept"] = dict(accept, min_g_per_l="3")
        self.expect_error(job)  # min > max
        job = self.job()
        job["orders"][0]["accept"] = dict(accept, species="dye")
        self.expect_error(job, "species.json")
        job = self.job()
        job["orders"][0]["accept"] = dict(accept, min_g_per_l="-1")
        self.expect_error(job, "negative.json")
        job = self.job()
        job["orders"][0]["accept"] = dict(accept, unit="g/L")
        self.expect_error(job, "extra.json")

    def test_orders_size_limits(self):
        job = self.job()
        job["orders"] = []
        self.expect_error(job)
        job = self.job()
        job["orders"] = [dict(job["orders"][0], id=f"o{i:02d}",
                              pump=f"pump-{i:02d}") for i in range(15)]
        self.expect_error(job, "fifteen.json")

    def test_input_size_limit(self):
        job = self.job()
        path = self.write("big.json", job)
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


class OutputExistsTests(DispatchCase):
    def test_existing_file_is_never_overwritten(self):
        target = self.tmp / "result.json"
        target.write_text("keep me", encoding="utf-8")
        proc = self.cli("--input", FIXTURES / "scenario1-exact-2.json",
                        "--output", target)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")

    def test_existing_symlink_is_never_overwritten(self):
        real = self.tmp / "real.json"
        real.write_text("keep me", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(real)
        proc = self.cli("--input", FIXTURES / "scenario2-pump-conflict.json",
                        "--output", link)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(real.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
