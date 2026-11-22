"""Acceptance tests for the offline salt-assay planning and verification tools.

The fixtures are synthetic.  Every run uses a real subprocess so the planner
and the verifier are exercised as separate process entry points.
"""

import copy
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
FIXTURES = ROOT / "fixtures" / "assay"
sys.path.insert(0, str(SRC))

from tank_transfer.assay_decision import build_report  # noqa: E402
from tank_transfer.assay_physics import Job  # noqa: E402
from tank_transfer.assay_verify import verify  # noqa: E402


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class AssayCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="assay-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def env(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SRC)
        return environment

    def cli(self, *args, check=True):
        proc = subprocess.run(
            [sys.executable, "-m", "tank_transfer.assay", *map(str, args)],
            capture_output=True, text=True, env=self.env(), cwd=str(ROOT))
        if check:
            self.assertEqual(
                proc.returncode, 0,
                msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
            return json.loads(proc.stdout)
        return proc

    def plan(self, job, name="job.json", out="report.json", check=True):
        path = self.write(name, job) if isinstance(job, dict) else Path(job)
        report = self.tmp / out
        proc = self.cli("plan", "--input", path, "--output", report,
                        check=check)
        return proc, report

    def verify(self, job, report, check=True):
        path = self.write("verify-job.json", job) if isinstance(job, dict) \
            else Path(job)
        return self.cli("verify", "--input", path, "--report", report,
                        check=check)


class ExampleOneTests(AssayCase):
    def test_recommends_b_with_strict_boundary_proof(self):
        job = load("example-1.json")
        summary, report_path = self.plan(job)
        self.assertEqual(summary["status"], "probe")
        self.assertEqual(summary["probe"], "pB")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["job_fingerprint"][:7], "sha256:")
        self.assertEqual(report["baseline_levels"], ["low", "high"])

        candidates = {entry["id"]: entry for entry in report["candidates"]}
        # The cheaper probe cannot separate the levels.
        self.assertFalse(candidates["pA"]["qualified"])
        buckets = {entry["bucket"]: entry for entry in candidates["pA"]["buckets"]}
        self.assertEqual(buckets[0]["reachable_levels"], [])
        self.assertEqual(buckets[1]["reachable_levels"], ["low", "high"])
        # B separates both buckets and is recommended.
        self.assertTrue(candidates["pB"]["qualified"])
        buckets = {entry["bucket"]: entry for entry in candidates["pB"]["buckets"]}
        self.assertEqual(buckets[0]["reachable_levels"], ["high"])
        self.assertEqual(buckets[1]["reachable_levels"], ["low"])
        self.assertEqual(report["recommendation"], {
            "probe": "pB",
            "bucket_levels": [{"bucket": 0, "level": "high"},
                              {"bucket": 1, "level": "low"}],
        })

        # The strict "B bucket 0 is not low" proof cites the observation lower
        # side, the target low side and the bucket's strict upper side.
        proof = buckets[0]["levels"]["low"]["proof"]
        self.assertTrue(proof["strict"])
        kinds = {entry["constraint"]["ref"]["kind"] for entry in proof["weights"]}
        self.assertEqual(kinds, {"observation_min", "target_low", "bucket_max"})
        self.assertEqual(proof["sum_rhs"], "0")

        # x = y = 5 is exactly on the cut (bucket 1) and is low.
        witness = buckets[1]["levels"]["low"]["witness"]["masses_g"]
        self.assertEqual(witness, {"A": "5", "B": "5"})

        result = self.verify(job, report_path)
        self.assertTrue(result["verified"])
        self.assertEqual(result["recommended"], "pB")

    def test_verify_is_independent_and_rebuilds_rows(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        # Every published constraint row equals the row rebuilt from the JOB.
        parsed = Job(job)
        evidence = report["candidates"][1]["buckets"][0]["levels"]["low"]
        for item in evidence["proof"]["weights"]:
            ref = item["constraint"]["ref"]
            kind = ref["kind"]
            rebuilt = None
            for constraint in (list(parsed.bound_constraints())
                               + list(parsed.observation_constraints)
                               + [parsed.target_constraint("low")]
                               + parsed.bucket_constraints(
                                   next(probe for probe in parsed.probes
                                        if probe["id"] == "pB"), 0)):
                if constraint.ref == ref:
                    rebuilt = constraint.json(parsed.unknown_tanks)
            self.assertEqual(item["constraint"], rebuilt)

    def test_verifier_does_not_import_planner_search(self):
        marker = self.tmp / "imports.txt"
        script = (
            "import sys\n"
            "import tank_transfer.assay_verify\n"
            f"open({str(marker)!r}, 'w').write('\\n'.join(sorted(sys.modules)))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], env=self.env(),
                              cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        loaded = set(marker.read_text().split())
        self.assertNotIn("tank_transfer.assay_decision", loaded)
        self.assertNotIn("tank_transfer.assay_search", loaded)
        self.assertNotIn("tank_transfer.exact_lp", loaded)

    def test_valid_report_verifies_in_a_new_process(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        result = self.verify(job, report_path)
        self.assertTrue(result["verified"])


class ExampleTwoTests(AssayCase):
    def test_unresolved(self):
        job = load("example-2.json")
        summary, report_path = self.plan(job)
        self.assertEqual(summary["status"], "unresolved")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertIsNone(report["recommendation"])
        candidates = {entry["id"]: entry for entry in report["candidates"]}
        self.assertFalse(candidates["pB"]["qualified"])
        self.assertEqual(
            candidates["pB"]["buckets"][0]["reachable_levels"],
            ["low", "high"])
        # (x, y) = (5, 4) and (6, 3) both fit bucket 0 with different levels.
        low = candidates["pB"]["buckets"][0]["levels"]["low"]["witness"]
        high = candidates["pB"]["buckets"][0]["levels"]["high"]["witness"]
        # Every witness satisfies the 4.5 g/L observation (x + y = 9) while
        # B's concentration stays below the cut and the target level differs.
        for witness, expected in ((low, "low"), (high, "high")):
            x = __import__("fractions").Fraction(witness["masses_g"]["A"])
            y = __import__("fractions").Fraction(witness["masses_g"]["B"])
            self.assertEqual(x + y, 9)
            self.assertLess(y, 5)
            self.assertEqual(expected == "low", x <= 5)
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_cross_job_verification_fails(self):
        _, first_report = self.plan(load("example-1.json"), out="first.json")
        proc = self.verify(load("example-2.json"), first_report, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("fingerprint", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_report_mutations_are_rejected(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        original = json.loads(report_path.read_text(encoding="utf-8"))

        def mutation(name, mutate):
            report = copy.deepcopy(original)
            mutate(report)
            path = self.tmp / f"mut-{name}.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            proc = self.verify(job, path, check=False)
            self.assertEqual(proc.returncode, 2,
                             msg=f"{name}: {proc.stderr!r}")
            self.assertEqual(proc.stdout, "")

        def candidate(report, probe_id):
            return next(entry for entry in report["candidates"]
                        if entry["id"] == probe_id)

        # Delete a bucket of an unselected candidate.
        mutation("drop-bucket",
                 lambda report: candidate(report, "pA")["buckets"].pop(0))
        # Move the threshold-equality witness to the wrong level.
        def move_witness(report):
            entry = candidate(report, "pB")
            witness = copy.deepcopy(entry["buckets"][1]["levels"]["low"]["witness"])
            entry["buckets"][0]["levels"]["high"] = {
                "reachable": True, "witness": witness}
        mutation("move-witness", move_witness)
        # Weaken the strict contradiction so it only proves 0 <= 0.
        def weaken(report):
            proof = candidate(report, "pB")["buckets"][0]["levels"]["low"]["proof"]
            for item in proof["weights"]:
                if item["constraint"]["ref"]["kind"] == "bucket_max":
                    item["weight"] = "0"
        mutation("weaken", weaken)
        # Swap the reported reachable levels of one bucket.
        def swap(report):
            entry = candidate(report, "pB")["buckets"][0]
            entry["reachable_levels"] = ["low"]
        mutation("swap", swap)
        # Recommend the cheaper but unqualified candidate.
        mutation("wrong-probe",
                 lambda report: report["recommendation"].update({"probe": "pA"}))
        # Claim both levels unreachable.
        mutation("bad-status", lambda report: report.update({"status": "inconsistent"}))


class ScenarioTests(AssayCase):
    def test_jointly_inconsistent_observations(self):
        job = load("inconsistent-observations.json")
        summary, report_path = self.plan(job)
        self.assertEqual(summary["status"], "inconsistent")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["baseline_levels"], [])
        self.assertEqual(report["candidates"], [])
        # Each level's impossibility cites both observation sides.
        for level in ("low", "high"):
            proof = report["baseline"][level]["proof"]
            ids = {entry["constraint"]["ref"].get("id")
                   for entry in proof["weights"]
                   if entry["constraint"]["ref"]["kind"].startswith("observation")}
            self.assertEqual(ids, {"oa", "ob"})
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_existing_observations_classify_without_probe_analysis(self):
        job = load("classified-before-probe.json")
        summary, report_path = self.plan(job)
        self.assertEqual(summary["status"], "classified")
        self.assertEqual(summary["classified_level"], "high")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["candidates"], [])
        self.assertIsNone(report["recommendation"])
        # It still carries a witness for the one reachable level.
        self.assertEqual(report["baseline"]["high"]["witness"]["masses_g"],
                         {"A": "9", "B": "0"})
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_equal_cost_probes_choose_lexicographic_id(self):
        job = load("equal-cost-probes.json")
        summary, report_path = self.plan(job)
        self.assertEqual(summary["probe"], "aB")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["recommendation"]["probe"], "aB")
        self.assertEqual([entry["id"] for entry in report["candidates"]],
                         ["aB", "zB"])
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_fixed_contribution_and_fractional_concentration(self):
        job = load("fixed-contribution.json")
        summary, report_path = self.plan(job)
        self.assertEqual(summary["status"], "probe")
        self.assertEqual(summary["probe"], "pB")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        low = report["baseline"]["low"]["witness"]["masses_g"]
        self.assertEqual(low, {"A": "2", "B": "2"})
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_fractional_cuts_and_closing_volume(self):
        job = load("fractional-closing.json")
        summary, report_path = self.plan(job)
        self.assertEqual(summary["status"], "probe")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        # The cut is given as a decimal but published as a reduced fraction.
        pB = next(entry for entry in report["candidates"]
                  if entry["id"] == "pB")
        self.assertEqual(pB["cuts_g_per_l"], ["3/2"])
        self.assertTrue(self.verify(job, report_path)["verified"])


class CandidateAnalysisTests(AssayCase):
    def test_no_probes_and_two_levels_is_unresolved(self):
        job = load("example-1.json")
        job["probes"] = []
        summary, report_path = self.plan(job, out="no-probe.json")
        self.assertEqual(summary["status"], "unresolved")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["candidates"], [])
        self.assertIsNone(report["recommendation"])
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_four_unknown_tanks_accepted(self):
        job = load("example-1.json")
        job["plan"]["tanks"][2]["volume_l"] = "1"
        job["plan"]["tanks"][3]["volume_l"] = "1"
        job["unknown"] = [{"tank": tank, "min_g": "0", "max_g": "10"}
                          for tank in ("A", "B", "C", "D")]
        summary, report_path = self.plan(job, out="four.json")
        self.assertIn(summary["status"], ("probe", "unresolved"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["unknown_tanks"], ["A", "B", "C", "D"])
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_verify_rejects_candidates_under_classified(self):
        job = load("classified-before-probe.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["candidates"] = [{
            "id": "pX", "cost": 1,
            "at": {"step": "s1", "tank": "B"},
            "cuts_g_per_l": ["5"], "qualified": True,
            "buckets": []}]
        path = self.tmp / "classified-extra.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        proc = self.verify(job, path, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("candidates", proc.stderr)

    def test_verify_rejects_unknown_level_name(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["baseline_levels"] = ["low", "medium"]
        path = self.tmp / "bad-level.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        proc = self.verify(job, path, check=False)
        self.assertEqual(proc.returncode, 2)


class OrderIndependenceTests(AssayCase):
    def test_array_order_does_not_change_the_report(self):
        import random
        base = load("equal-cost-probes.json")
        reference = build_report(Job(base))
        for seed in range(4):
            shuffled = copy.deepcopy(base)
            random.Random(seed).shuffle(shuffled["observations"])
            random.Random(seed).shuffle(shuffled["probes"])
            shuffled["unknown"] = list(reversed(shuffled["unknown"]))
            result = build_report(Job(shuffled))
            verify(Job(shuffled), result)
            # The fingerprint is of this exact parsed JOB (arrays included),
            # so only the derived decision content is compared here.
            reference = dict(reference)
            result = dict(result)
            self.assertEqual(result.pop("job_fingerprint")[:7], "sha256:")
            if "job_fingerprint" in reference:
                reference.pop("job_fingerprint")
            self.assertEqual(result, reference)

    def test_bad_cli_invocation_returns_2(self):
        proc = self.cli("plan", "--input", str(FIXTURES / "example-1.json"),
                        check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")


class InputValidationTests(AssayCase):
    def assert_rejected(self, job, fragment=None):
        proc, report = self.plan(job, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(report.exists())
        if fragment:
            self.assertIn(fragment, proc.stderr)

    def test_extra_and_missing_job_fields(self):
        job = load("example-1.json")
        extra = copy.deepcopy(job)
        extra["surprise"] = 1
        self.assert_rejected(extra, "expected fields")
        missing = copy.deepcopy(job)
        del missing["probes"]
        self.assert_rejected(missing, "expected fields")

    def test_duplicate_json_key_rejected(self):
        path = self.tmp / "duplicate.json"
        raw = (FIXTURES / "example-1.json").read_text(encoding="utf-8")
        path.write_text(raw.replace('"species": "salt",',
                                    '"species": "salt", "species": "salt",'),
                        encoding="utf-8")
        report = self.tmp / "dup-report.json"
        proc = self.cli("plan", "--input", path, "--output", report, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("duplicate JSON key", proc.stderr)
        self.assertFalse(report.exists())

    def test_nonfinite_and_wrong_numeric_types(self):
        job = load("example-1.json")
        path = self.tmp / "nan.json"
        raw = json.dumps(job).replace('"limit_g_per_l": "5"',
                                      '"limit_g_per_l": NaN')
        path.write_text(raw, encoding="utf-8")
        report = self.tmp / "nan-report.json"
        proc = self.cli("plan", "--input", path, "--output", report, check=False)
        self.assertEqual(proc.returncode, 2)

        wrong = copy.deepcopy(job)
        wrong["target"]["limit_g_per_l"] = 5
        self.assert_rejected(wrong, "decimal string")

        bad_cost = copy.deepcopy(job)
        bad_cost["probes"][0]["cost"] = "5"
        self.assert_rejected(bad_cost, "integer")

    def test_unknown_rules(self):
        job = load("example-1.json")
        not_zero = copy.deepcopy(job)
        not_zero["plan"]["tanks"][0]["mass_g"]["salt"] = "1"
        self.assert_rejected(not_zero, "placeholder must be zero")

        zero_volume = copy.deepcopy(job)
        zero_volume["unknown"].append(
            {"tank": "C", "min_g": "0", "max_g": "1"})
        self.assert_rejected(zero_volume, "positive volume")

        bad_bounds = copy.deepcopy(job)
        bad_bounds["unknown"][0]["min_g"] = "5"
        bad_bounds["unknown"][0]["max_g"] = "4"
        self.assert_rejected(bad_bounds, "mass bounds")

        five = copy.deepcopy(job)
        five["unknown"] = [
            {"tank": "A", "min_g": "0", "max_g": "10"},
            {"tank": "B", "min_g": "0", "max_g": "10"},
            {"tank": "C", "min_g": "0", "max_g": "10"},
            {"tank": "D", "min_g": "0", "max_g": "10"},
        ]
        # Four is the limit; a fifth entry must be rejected by array size.
        five["unknown"].append(
            {"tank": "A", "min_g": "0", "max_g": "10"})
        self.assert_rejected(five, "array size")

    def test_observation_and_probe_rules(self):
        job = load("example-1.json")
        inverted = copy.deepcopy(job)
        inverted["observations"][0]["min_g_per_l"] = "6"
        self.assert_rejected(inverted, "observation concentration")

        duplicates = copy.deepcopy(job)
        duplicates["probes"][1]["id"] = duplicates["probes"][0]["id"]
        self.assert_rejected(duplicates, "duplicate probe id")

        nonincreasing = copy.deepcopy(job)
        nonincreasing["probes"][0]["cut_g_per_l"] = ["5", "5"]
        self.assert_rejected(nonincreasing, "strictly increase")

        at_target = copy.deepcopy(job)
        at_target["probes"][0]["at"] = {"step": "s2", "tank": "D"}
        self.assert_rejected(at_target, "differ from target")

        cost = copy.deepcopy(job)
        cost["probes"][0]["cost"] = 0
        self.assert_rejected(cost, "integer")

    def test_invalid_positions(self):
        job = load("example-1.json")
        for label, position, fragment in (
            ("missing-step", {"step": "s9", "tank": "A"}, "does not exist"),
            ("missing-tank", {"step": "s1", "tank": "Z"}, "does not exist"),
            ("empty-tank", {"step": "s1", "tank": "D"}, "zero volume"),
        ):
            broken = copy.deepcopy(job)
            broken["observations"] = [{
                "id": "bad", "at": position,
                "min_g_per_l": "0", "max_g_per_l": "1"}]
            self.assert_rejected(broken, fragment)

    def test_plan_limits(self):
        job = load("example-1.json")
        too_many_steps = copy.deepcopy(job)
        empty = {"id": "later", "transfers": []}
        while len(too_many_steps["plan"]["steps"]) <= 48:
            too_many_steps["plan"]["steps"].append(
                dict(empty, id=f"later{len(too_many_steps['plan']['steps'])}"))
        self.assert_rejected(too_many_steps, "48 steps")

    def test_invalid_last_step_physics_writes_nothing(self):
        job = load("example-1.json")
        job["plan"]["steps"][1]["transfers"].append(
            {"id": "bad", "source": "C", "target": "D", "volume_l": "5"})
        self.assert_rejected(job, "opening volume insufficient")

    def test_existing_output_bytes_are_preserved(self):
        output = self.tmp / "existing.json"
        output.write_bytes(b"PRESERVE\n")
        proc, path = self.plan(load("example-1.json"), out="existing.json",
                               check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(path.read_bytes(), b"PRESERVE\n")

    def test_symbolic_link_is_not_overwritten(self):
        target = self.tmp / "link-target.json"
        link = self.tmp / "link.json"
        os.symlink(target, link)
        proc, _ = self.plan(load("example-1.json"), out="link.json",
                            check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(link.is_symlink())
        self.assertFalse(target.exists())


class MultiCutTests(AssayCase):
    def test_three_bucket_qualifying_probe(self):
        job = load("example-1.json")
        job["probes"] = [{"id": "pt", "at": {"step": "s1", "tank": "B"},
                          "cut_g_per_l": ["5", "8"], "cost": 2}]
        summary, report_path = self.plan(job, out="three-buckets.json")
        self.assertEqual(summary["status"], "probe")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        buckets = report["candidates"][0]["buckets"]
        self.assertEqual([b["reachable_levels"] for b in buckets],
                         [["high"], ["low"], ["low"]])
        self.assertEqual(report["recommendation"]["bucket_levels"], [
            {"bucket": 0, "level": "high"},
            {"bucket": 1, "level": "low"},
            {"bucket": 2, "level": "low"}])
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_zero_cut_first_bucket_is_impossible_but_harmless(self):
        job = load("example-1.json")
        job["probes"] = [{"id": "p0", "at": {"step": "s2", "tank": "B"},
                          "cut_g_per_l": ["0"], "cost": 1}]
        summary, report_path = self.plan(job, out="zero-cut.json")
        self.assertEqual(summary["status"], "unresolved")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        buckets = report["candidates"][0]["buckets"]
        self.assertEqual(buckets[0]["reachable_levels"], [])
        self.assertEqual(buckets[1]["reachable_levels"], ["low", "high"])
        self.assertFalse(report["candidates"][0]["qualified"])
        self.assertTrue(self.verify(job, report_path)["verified"])

    def test_ambiguous_middle_bucket_blocks_recommendation(self):
        job = load("example-1.json")
        job["probes"] = [{"id": "pm", "at": {"step": "s1", "tank": "B"},
                          "cut_g_per_l": ["2", "8"], "cost": 1}]
        summary, report_path = self.plan(job, out="ambiguous-middle.json")
        self.assertEqual(summary["status"], "unresolved")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [b["reachable_levels"] for b in report["candidates"][0]["buckets"]],
            [["high"], ["low", "high"], ["low"]])
        self.assertTrue(self.verify(job, report_path)["verified"])


class SymbolicConsistencyTests(unittest.TestCase):
    def test_multiple_unknown_tanks_with_split_and_merge(self):
        from fractions import Fraction
        from tank_transfer.service import process
        plan = {
            "species": ["salt"],
            "tanks": [
                {"id": tank, "capacity_l": "50",
                 "volume_l": "2" if tank in "ABC" else "0",
                 "mass_g": {"salt": "0"}} for tank in "ABCDE"],
            "steps": [
                {"id": "s1", "transfers": [
                    {"id": "a1", "source": "A", "target": "C", "volume_l": "0.5"},
                    {"id": "a2", "source": "A", "target": "D", "volume_l": "0.75"},
                    {"id": "b1", "source": "B", "target": "D", "volume_l": "1"},
                    {"id": "c1", "source": "C", "target": "E", "volume_l": "0.25"}]},
                {"id": "s2", "transfers": [
                    {"id": "d1", "source": "D", "target": "A", "volume_l": "0.5"},
                    {"id": "e1", "source": "E", "target": "B", "volume_l": "0.25"},
                    {"id": "a3", "source": "A", "target": "C", "volume_l": "0.5"}]},
            ],
        }
        job = {"plan": plan, "species": "salt",
               "unknown": [{"tank": t, "min_g": "0", "max_g": "10"}
                           for t in "ABC"],
               "observations": [],
               "target": {"at": {"step": "s2", "tank": "C"},
                          "limit_g_per_l": "1"},
               "probes": []}
        parsed = Job(job)
        for masses in ({"A": "3.75", "B": "9.25", "C": "8.5"},
                       {"A": "0", "B": "10", "C": "0.125"},
                       {"A": "5", "B": "5", "C": "5"}):
            data = copy.deepcopy(plan)
            for tank in data["tanks"]:
                if tank["id"] in masses:
                    tank["mass_g"]["salt"] = masses[tank["id"]]
            numeric = process(data)
            values = tuple(Fraction(masses[tank])
                           for tank in parsed.unknown_tanks)
            for step in numeric["steps"]:
                for tank, state in step["closing"].items():
                    constant, coeffs = parsed.mass_affine[
                        (step["step_id"], tank)]
                    expected = constant + sum(
                        coefficient * value
                        for coefficient, value in zip(coeffs, values))
                    self.assertEqual(
                        Fraction(state["mass_g"]["salt"]), expected)


class BoundaryEvidenceTests(AssayCase):
    def test_equality_witness_cannot_be_moved_to_high(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        # x = y = 5 makes the target exactly 5 g/L, which is low, never high.
        report["baseline"]["high"] = {
            "reachable": True,
            "witness": {"masses_g": {"A": "5", "B": "5"}}}
        path = self.tmp / "equality-high.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        proc = self.verify(job, path, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("witness violates", proc.stderr)

    def test_zero_le_zero_proof_is_rejected(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        # Sum observation_min and both unknown_max rows: coefficients cancel
        # but the right side is exactly 0, so this only proves 0 <= 0.
        observation = report["candidates"][1]["buckets"][0]["levels"]["low"][
            "proof"]["weights"][0]["constraint"]
        report["candidates"][1]["buckets"][0]["levels"]["low"]["proof"] = {
            "weights": [
                {"constraint": observation, "weight": "1"},
                {"constraint": {"ref": {"kind": "unknown_max", "tank": "A"},
                                "terms": [{"tank": "A", "coefficient": "1"}],
                                "rhs": "10", "strict": False},
                 "weight": "1/2"},
                {"constraint": {"ref": {"kind": "unknown_max", "tank": "B"},
                                "terms": [{"tank": "B", "coefficient": "1"}],
                                "rhs": "10", "strict": False},
                 "weight": "1/2"},
            ],
            "sum_rhs": "5", "strict": False,
        }
        path = self.tmp / "zero-le-zero.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        proc = self.verify(job, path, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a contradiction", proc.stderr)

    def test_cross_scope_citation_is_rejected(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        # pB bucket 0 may not cite pA's bucket boundary.
        proof = report["candidates"][1]["buckets"][0]["levels"]["low"]["proof"]
        for item in proof["weights"]:
            if item["constraint"]["ref"]["kind"] == "bucket_max":
                item["constraint"]["ref"]["probe"] = "pA"
        path = self.tmp / "cross-probe.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        proc = self.verify(job, path, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("outside its scope", proc.stderr)


class ReportTamperTests(AssayCase):
    def test_verify_rejects_coverage_and_classification_tampering(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        original = json.loads(report_path.read_text(encoding="utf-8"))

        def reject(name, mutate):
            report = copy.deepcopy(original)
            mutate(report)
            path = self.tmp / f"tamper-{name}.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            proc = self.verify(job, path, check=False)
            self.assertEqual(proc.returncode, 2,
                             msg=f"{name}: {proc.stderr!r}")

        # Missing candidate entry.
        reject("missing-candidate",
               lambda report: report["candidates"].pop())
        # Extra candidate entry.
        reject("extra-candidate",
               lambda report: report["candidates"].append(
                   dict(report["candidates"][0], id="pZ")))
        # Missing bucket level evidence.
        def missing_level(report):
            del report["candidates"][0]["buckets"][0]["levels"]["low"]
        reject("missing-level", missing_level)
        # Extra bucket.
        def extra_bucket(report):
            report["candidates"][0]["buckets"].append(
                copy.deepcopy(report["candidates"][0]["buckets"][0]))
        reject("extra-bucket", extra_bucket)
        # Remove a recommendation row.
        reject("missing-row",
               lambda report: report["recommendation"]["bucket_levels"].pop())
        # Wrong cost.
        reject("wrong-cost",
               lambda report: report["candidates"][0].update({"cost": 999}))
        # Duplicate levels in a reachable set.
        reject("duplicate-level",
               lambda report: report["candidates"][1]["buckets"][0].update(
                   {"reachable_levels": ["high", "high"]}))
        # Tamper an inline constraint row.
        def tamper_row(report):
            report["candidates"][1]["buckets"][0]["levels"]["low"][
                "proof"]["weights"][0]["constraint"]["rhs"] = "-9"
        reject("tamper-row", tamper_row)

    def test_verify_rejects_missing_or_extra_report_fields(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        missing = copy.deepcopy(report)
        del missing["baseline_levels"]
        extra = copy.deepcopy(report)
        extra["note"] = "hello"
        for name, value in (("missing", missing), ("extra", extra)):
            path = self.tmp / f"fields-{name}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            proc = self.verify(job, path, check=False)
            self.assertEqual(proc.returncode, 2)

    def test_verify_accepts_zero_weight_non_strict_row(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        proof = report["candidates"][1]["buckets"][0]["levels"]["low"]["proof"]
        proof["weights"].append({
            "constraint": {
                "ref": {"kind": "unknown_min", "tank": "A"},
                "terms": [{"tank": "A", "coefficient": "-1"}],
                "rhs": "0", "strict": False},
            "weight": "0"})
        path = self.tmp / "zero-weight.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        self.assertTrue(self.verify(job, path)["verified"])

    def test_verify_rejects_zero_weight_strict_row(self):
        job = load("example-1.json")
        _, report_path = self.plan(job)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        proof = report["candidates"][1]["buckets"][0]["levels"]["low"]["proof"]
        for item in proof["weights"]:
            if item["constraint"]["ref"]["kind"] == "bucket_max":
                item["weight"] = "0"
        path = self.tmp / "zero-strict.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        proc = self.verify(job, path, check=False)
        self.assertEqual(proc.returncode, 2)


class DirectProofTests(unittest.TestCase):
    """In-process checks of the evidence format and reachable-level mapping."""

    def test_every_evidence_round_trips(self):
        for name in ("example-1.json", "example-2.json",
                     "fixed-contribution.json", "fractional-closing.json",
                     "inconsistent-observations.json"):
            job = Job(load(name))
            report = build_report(job)
            verify(job, report)

    def test_symbolic_physics_matches_numeric_model(self):
        from tank_transfer.assay_physics import Job as AssayJob
        from tank_transfer.service import process
        job = load("example-1.json")
        parsed = AssayJob(job)
        # Sample several rational unknown masses and compare with the frozen
        # numeric simulation.
        for masses in (({"A": "0", "B": "10"}),
                       ({"A": "5", "B": "5"}),
                       ({"A": "2.5", "B": "0.125"})):
            data = copy.deepcopy(job["plan"])
            for tank in data["tanks"]:
                if tank["id"] in masses:
                    tank["mass_g"]["salt"] = masses[tank["id"]]
            numeric = process(data)
            values = tuple(__import__("fractions").Fraction(masses[tank])
                           for tank in parsed.unknown_tanks)
            for (step_id, tank), affine in parsed.mass_affine.items():
                expected = affine[0] + sum(
                    coefficient * value for coefficient, value
                    in zip(affine[1], values))
                got = numeric["steps"][[step["step_id"] for step in
                                        numeric["steps"]].index(step_id)]
                self.assertEqual(
                    __import__("fractions").Fraction(
                        got["closing"][tank]["mass_g"]["salt"]),
                    expected)


if __name__ == "__main__":
    unittest.main()
