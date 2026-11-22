"""Acceptance and recovery tests for the sample publication catalog."""

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
sys.path.insert(0, str(SRC))

from tank_transfer.sources import build_plan  # noqa: E402


def base_plan():
    return {
        "species": ["salt"],
        "tanks": [
            {"id": "A", "capacity_l": "20", "volume_l": "10", "mass_g": {"salt": "100"}},
            {"id": "B", "capacity_l": "20", "volume_l": "10", "mass_g": {"salt": "100"}},
            {"id": "C", "capacity_l": "20", "volume_l": "5", "mass_g": {"salt": "50"}},
            {"id": "D", "capacity_l": "20", "volume_l": "0", "mass_g": {"salt": "0"}},
            {"id": "E", "capacity_l": "20", "volume_l": "0", "mass_g": {"salt": "0"}},
        ],
        "steps": [
            {"id": "s1", "transfers": [
                {"id": "ac", "source": "A", "target": "C", "volume_l": "5"},
                {"id": "bd", "source": "B", "target": "D", "volume_l": "5"},
            ]},
            {"id": "s2", "transfers": [
                {"id": "draw", "source": "C", "target": "E", "volume_l": "4"},
                {"id": "dc", "source": "D", "target": "C", "volume_l": "5"},
            ]},
        ],
    }


def corrected_plan():
    data = base_plan()
    data["steps"][0]["transfers"][0]["target"] = "D"
    data["steps"][0]["transfers"][1]["target"] = "C"
    return data


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="catalog-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_cli(self, *args, crash=None, check=True):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env.pop("TANK_TRANSFER_CRASH_POINT", None)
        if crash:
            env["TANK_TRANSFER_CRASH_POINT"] = crash
        proc = subprocess.run(
            [sys.executable, "-m", "tank_transfer.catalog",
             "--store", str(self.store), *map(str, args)],
            capture_output=True, text=True, env=env, cwd=str(ROOT),
        )
        if check:
            self.assertEqual(proc.returncode, 0,
                             msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
            return json.loads(proc.stdout)
        return proc

    def init(self):
        return self.run_cli("init", "--revision", "r1",
                            "--source", self.write("r1.json", base_plan()))

    def issue(self, receipt, kind, step, item):
        return self.run_cli("issue", "--receipt", receipt, "--kind", kind,
                            "--step", step, "--item", item)


class ScenarioTests(CatalogCase):
    def test_scenario_one_evidence(self):
        self.init()
        draw = self.issue("rc-draw", "transfer", "s2", "draw")
        self.assertEqual(draw["evidence"]["volume_l"], "4")
        self.assertEqual(draw["evidence"]["mass_g"], {"salt": "40"})
        self.assertEqual(draw["evidence"]["source"], "C")
        self.assertEqual(draw["evidence"]["target"], "E")
        self.assertEqual(draw["evidence"]["lots"], {
            "A": {"volume_l": "2", "mass_g": {"salt": "20"}},
            "C": {"volume_l": "2", "mass_g": {"salt": "20"}},
        })
        self.assertNotIn("B", draw["evidence"]["lots"])
        self.assertEqual(draw["issued_at"], "r1")
        self.assertIsNone(draw["revocation"])
        self.assertEqual(draw["selector"],
                         {"kind": "transfer", "step": "s2", "item": "draw"})

        closing = self.issue("rc-e", "closing", "s2", "E")
        self.assertEqual(closing["evidence"]["lots"], draw["evidence"]["lots"])
        self.assertEqual(closing["evidence"]["mass_g"], {"salt": "40"})

        see_a = self.issue("rc-a", "closing", "s2", "A")
        self.assertEqual(see_a["evidence"]["lots"],
                         {"A": {"volume_l": "5", "mass_g": {"salt": "50"}}})

    def test_scenario_two_revision_and_archive(self):
        self.init()
        self.issue("rc-draw", "transfer", "s2", "draw")
        self.issue("rc-e", "closing", "s2", "E")
        old_a = self.issue("rc-a", "closing", "s2", "A")

        result = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                              "--source", self.write("r2.json", corrected_plan()))
        self.assertEqual(result, {
            "revision": "r2", "base": "r1",
            "revoked": [{"receipt": "rc-draw", "reason": "changed"},
                        {"receipt": "rc-e", "reason": "changed"}],
        })
        self.assertEqual(self.run_cli("head"), {"revision": "r2"})

        for rid in ("rc-draw", "rc-e"):
            proc = self.run_cli("read", "--receipt", rid, check=False)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")

        # The unaffected receipt is still readable with unchanged evidence.
        self.assertEqual(self.run_cli("read", "--receipt", "rc-a"), old_a)

        # Archived receipts keep the original evidence and record revocation.
        archived = self.run_cli("read", "--receipt", "rc-draw", "--archive")
        self.assertEqual(archived["evidence"], {
            "volume_l": "4", "mass_g": {"salt": "40"}, "source": "C",
            "target": "E",
            "lots": {"A": {"volume_l": "2", "mass_g": {"salt": "20"}},
                     "C": {"volume_l": "2", "mass_g": {"salt": "20"}}},
        })
        self.assertEqual(archived["revocation"],
                         {"revision": "r2", "reason": "changed"})
        self.assertEqual(archived["issued_at"], "r1")

        # New receipts on r2 carry B instead of A.
        new_draw = self.issue("rc-draw-2", "transfer", "s2", "draw")
        self.assertEqual(set(new_draw["evidence"]["lots"]), {"B", "C"})
        self.assertEqual(new_draw["issued_at"], "r2")

    def test_scenario_two_other_cases(self):
        self.init()
        self.issue("rc-draw", "transfer", "s2", "draw")
        self.issue("rc-a", "closing", "s2", "A")
        data = corrected_plan()
        # Delete the draw transfer entirely, and reorder s1 without changing
        # any evidence: reordering alone must not revoke anything.
        data["steps"][1]["transfers"] = [
            t for t in data["steps"][1]["transfers"] if t["id"] != "draw"]
        data["steps"][0]["transfers"].reverse()
        self.run_cli("revise", "--revision", "r2", "--base", "r1",
                     "--source", self.write("r2b.json", data))
        archived = self.run_cli("read", "--receipt", "rc-draw", "--archive")
        self.assertEqual(archived["revocation"], {"revision": "r2", "reason": "missing"})
        self.assertEqual(self.run_cli("read", "--receipt", "rc-a")["evidence"],
                         {"volume_l": "5", "mass_g": {"salt": "50"},
                          "lots": {"A": {"volume_l": "5", "mass_g": {"salt": "50"}}}})

        # Non-empty to empty: draw now goes to D, so E closes empty.
        self.store = self.tmp / "empty-store"
        self.run_cli("init", "--revision", "r1",
                     "--source", self.write("e1.json", base_plan()))
        receipt = self.issue("rc-e", "closing", "s2", "E")
        self.assertEqual(receipt["evidence"]["volume_l"], "4")
        empty = corrected_plan()
        empty["steps"][1]["transfers"][0]["target"] = "D"
        self.run_cli("revise", "--revision", "r2", "--base", "r1",
                     "--source", self.write("e2.json", empty))
        archived = self.run_cli("read", "--receipt", "rc-e", "--archive")
        self.assertEqual(archived["revocation"], {"revision": "r2", "reason": "empty"})
        # The archived evidence is still exactly what was issued.
        self.assertEqual(archived["evidence"], receipt["evidence"])

    def test_zero_mass_source_and_merge_after_split(self):
        data = {
            "species": ["salt"],
            "tanks": [
                {"id": "A", "capacity_l": "20", "volume_l": "10", "mass_g": {"salt": "0"}},
                {"id": "B", "capacity_l": "20", "volume_l": "10", "mass_g": {"salt": "100"}},
                {"id": "C", "capacity_l": "20", "volume_l": "0", "mass_g": {"salt": "0"}},
                {"id": "D", "capacity_l": "20", "volume_l": "0", "mass_g": {"salt": "0"}},
            ],
            "steps": [
                {"id": "split", "transfers": [
                    {"id": "a1", "source": "A", "target": "C", "volume_l": "4"},
                    {"id": "a2", "source": "A", "target": "D", "volume_l": "6"},
                ]},
                {"id": "merge", "transfers": [
                    {"id": "c1", "source": "C", "target": "B", "volume_l": "4"},
                    {"id": "d1", "source": "D", "target": "B", "volume_l": "6"},
                ]},
            ],
        }
        self.run_cli("init", "--revision", "r1",
                     "--source", self.write("z.json", data))
        merged = self.issue("rc-b", "closing", "merge", "B")
        self.assertEqual(merged["evidence"]["lots"]["A"],
                         {"volume_l": "10", "mass_g": {"salt": "0"}})
        split = self.issue("rc-c", "closing", "split", "C")
        self.assertEqual(split["evidence"]["lots"],
                         {"A": {"volume_l": "4", "mass_g": {"salt": "0"}}})
        self.assertEqual(merged["evidence"]["mass_g"]["salt"], "100")



class ReceiptRuleTests(CatalogCase):
    def test_receipt_ids_never_reused_and_rejected_inputs_leave_state(self):
        self.init()
        before = (self.store / "state.json").read_bytes()
        proc = self.run_cli("issue", "--receipt", "rc-1", "--kind", "closing",
                            "--step", "nope", "--item", "A", check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((self.store / "state.json").read_bytes(), before)

        self.issue("rc-1", "closing", "s2", "A")
        # Empty selector on initial plan: D closes empty after s2.
        proc = self.run_cli("issue", "--receipt", "rc-empty", "--kind",
                            "closing", "--step", "s2", "--item", "D", check=False)
        self.assertEqual(proc.returncode, 2)
        # Revoke rc-1? It is unaffected by corrected plan, so delete selector.
        data = corrected_plan()
        data["steps"][0]["transfers"] = [
            {"id": "ac", "source": "A", "target": "D", "volume_l": "5"}]
        data["steps"][1]["transfers"] = []
        self.run_cli("revise", "--revision", "r2", "--base", "r1",
                     "--source", self.write("r2c.json", data))
        # rc-1 selected s2/A, which still exists with unchanged evidence.
        self.run_cli("read", "--receipt", "rc-1")
        # Reusing a revoked or any used ID is refused.  Use a changed sample.
        marker = (self.store / "state.json").read_bytes()
        proc = self.run_cli("issue", "--receipt", "rc-1", "--kind", "closing",
                            "--step", "s2", "--item", "A", check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((self.store / "state.json").read_bytes(), marker)

    def test_selector_identity_is_step_id_not_position(self):
        self.init()
        receipt = self.issue("rc-1", "closing", "s2", "A")
        # Insert a new leading step; s2 keeps its identity and evidence.
        data = corrected_plan()
        data["steps"].insert(0, {"id": "s0", "transfers": []})
        self.run_cli("revise", "--revision", "r2", "--base", "r1",
                     "--source", self.write("r2d.json", data))
        self.assertEqual(self.run_cli("read", "--receipt", "rc-1"), receipt)


class RevisionRuleTests(CatalogCase):
    def test_retry_is_idempotent_even_after_newer_head(self):
        self.init()
        self.issue("rc-draw", "transfer", "s2", "draw")
        r2path = self.write("r2.json", corrected_plan())
        original = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                                "--source", r2path)
        restored = base_plan()
        r3 = self.run_cli("revise", "--revision", "r3", "--base", "r2",
                          "--source", self.write("r3.json", restored))
        self.assertEqual(r3["revoked"], [])
        self.assertEqual(self.run_cli("head"), {"revision": "r3"})
        # Retry the old r2 correction: return its original receipt.
        self.assertEqual(
            self.run_cli("revise", "--revision", "r2", "--base", "r1",
                         "--source", r2path), original)
        self.assertEqual(self.run_cli("head"), {"revision": "r3"})
        # Revoked receipts stay revoked.
        proc = self.run_cli("read", "--receipt", "rc-draw", check=False)
        self.assertEqual(proc.returncode, 2)

    def test_retry_ignores_key_order_and_whitespace_but_not_arrays(self):
        self.init()
        r2 = corrected_plan()
        path = self.tmp / "r2.json"
        path.write_text(json.dumps(r2, indent=4, sort_keys=True))
        original = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                                "--source", path)
        reordered = json.loads(json.dumps(r2, sort_keys=True))
        path.write_text(json.dumps(reordered))
        self.assertEqual(
            self.run_cli("revise", "--revision", "r2", "--base", "r1",
                         "--source", path), original)
        changed = corrected_plan()
        changed["steps"][0]["transfers"].reverse()
        path.write_text(json.dumps(changed))
        proc = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                            "--source", path, check=False)
        self.assertEqual(proc.returncode, 2)
        # Same for a different base.
        path.write_text(json.dumps(reordered))
        proc = self.run_cli("revise", "--revision", "r2", "--base", "other",
                            "--source", path, check=False)
        self.assertEqual(proc.returncode, 2)

    def test_initial_id_rejected_as_correction_and_limits(self):
        self.init()
        proc = self.run_cli("revise", "--revision", "r1", "--base", "r1",
                            "--source", self.write("x.json", corrected_plan()),
                            check=False)
        self.assertEqual(proc.returncode, 2)
        # A failed correction does not occupy its id.
        self.run_cli("revise", "--revision", "r2", "--base", "r1",
                     "--source", self.write("ok.json", corrected_plan()))
        # Version limit: 32 total including r1 and r2.
        for i in range(3, 33):
            self.run_cli("revise", "--revision", f"r{i}", "--base", f"r{i-1}",
                         "--source", self.write(f"ok{i}.json", corrected_plan()))
        marker = (self.store / "state.json").read_bytes()
        proc = self.run_cli("revise", "--revision", "r33", "--base", "r32",
                            "--source", self.write("ok33.json", corrected_plan()),
                            check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((self.store / "state.json").read_bytes(), marker)

    def test_invalid_physical_plan_does_not_adopt(self):
        self.init()
        self.issue("rc-a", "closing", "s2", "A")
        marker = (self.store / "state.json").read_bytes()
        bad = corrected_plan()
        # Middle step is fine, final step overdraws its opening volume.
        bad["steps"].append({"id": "s3", "transfers": [
            {"id": "over", "source": "A", "target": "B", "volume_l": "100"}]})
        proc = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                            "--source", self.write("bad.json", bad), check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((self.store / "state.json").read_bytes(), marker)
        self.assertEqual(self.run_cli("head"), {"revision": "r1"})
        self.run_cli("read", "--receipt", "rc-a")
        # The same failed correction id can be used for a valid plan.
        self.run_cli("revise", "--revision", "r2", "--base", "r1",
                     "--source", self.write("good.json", corrected_plan()))
        self.assertEqual(self.run_cli("head"), {"revision": "r2"})


class RecoveryTests(CatalogCase):
    def ready(self):
        self.init()
        self.issued = {
            "rc-draw": self.issue("rc-draw", "transfer", "s2", "draw"),
            "rc-e": self.issue("rc-e", "closing", "s2", "E"),
            "rc-a": self.issue("rc-a", "closing", "s2", "A"),
        }
        self.r2 = self.write("r2.json", corrected_plan())

    def state(self):
        return json.loads((self.store / "state.json").read_text())

    def assert_old_state(self):
        self.assertEqual(self.run_cli("head"), {"revision": "r1"})
        state = self.state()
        self.assertEqual(state["head"], "r1")
        self.assertNotIn("r2", state["revisions"])
        for rid in ("rc-draw", "rc-e", "rc-a"):
            self.assertIsNone(state["receipts"][rid]["revocation"])
            self.assertEqual(self.run_cli("read", "--receipt", rid),
                             self.issued[rid])

    def test_crash_before_commit_keeps_old_state_then_retry(self):
        self.ready()
        proc = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                            "--source", self.r2, crash="before-commit",
                            check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assert_old_state()
        result = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                              "--source", self.r2)
        self.assertEqual(result["revoked"], [
            {"receipt": "rc-draw", "reason": "changed"},
            {"receipt": "rc-e", "reason": "changed"},
        ])
        self.assertEqual(self.run_cli("head"), {"revision": "r2"})
        self.assertEqual(self.run_cli("read", "--receipt", "rc-a")["issued_at"], "r1")

    def test_crash_after_commit_before_output_then_retry(self):
        self.ready()
        proc = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                            "--source", self.r2, crash="after-commit",
                            check=False)
        self.assertNotEqual(proc.returncode, 0)
        # Committed state is fully visible in a new process.
        self.assertEqual(self.run_cli("head"), {"revision": "r2"})
        state = self.state()
        self.assertEqual(state["revisions"]["r2"]["receipt"]["revoked"], [
            {"receipt": "rc-draw", "reason": "changed"},
            {"receipt": "rc-e", "reason": "changed"},
        ])
        for rid in ("rc-draw", "rc-e"):
            archived = self.run_cli("read", "--receipt", rid, "--archive")
            self.assertEqual(archived["revocation"]["revision"], "r2")
            self.assertEqual(archived["evidence"],
                             self.issued[rid]["evidence"])
            self.assertEqual(archived["selector"], self.issued[rid]["selector"])
        self.assertEqual(self.run_cli("read", "--receipt", "rc-a"),
                         self.issued["rc-a"])
        # Retry returns the original revision receipt without re-adoption.
        retried = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                               "--source", self.r2)
        self.assertEqual(retried["revoked"], [
            {"receipt": "rc-draw", "reason": "changed"},
            {"receipt": "rc-e", "reason": "changed"},
        ])
        self.assertEqual(self.run_cli("head"), {"revision": "r2"})

    def test_store_independent_of_original_source_file(self):
        self.ready()
        self.r2.unlink()
        with self.assertRaises(OSError):
            self.r2.read_bytes()
        self.run_cli("revise", "--revision", "r2", "--base", "r1",
                     "--source", self.write("again.json", corrected_plan()))
        self.r2.write_text("not json but fine")
        self.assertEqual(self.run_cli("read", "--receipt", "rc-a")["issued_at"], "r1")


class LimitTests(CatalogCase):
    def test_init_limits_reject_without_creating_store(self):
        cases = []
        many_tanks = base_plan()
        many_tanks["tanks"] = [
            {"id": f"T{i}", "capacity_l": "1", "volume_l": "0",
             "mass_g": {"salt": "0"}} for i in range(17)]
        many_tanks["steps"] = []
        cases.append(many_tanks)
        many_species = base_plan()
        many_species["species"] = [f"s{i}" for i in range(9)]
        for tank in many_species["tanks"]:
            tank["mass_g"] = {f"s{i}": "0" for i in range(9)}
        many_species["steps"] = []
        cases.append(many_species)
        many_steps = base_plan()
        many_steps["steps"] = [{"id": f"x{i}", "transfers": []}
                               for i in range(65)]
        cases.append(many_steps)
        many_transfers = base_plan()
        roomy = [{"id": "S", "capacity_l": "1000", "volume_l": "1000",
                  "mass_g": {"salt": "0"}},
                 {"id": "T", "capacity_l": "1000", "volume_l": "0",
                  "mass_g": {"salt": "0"}}]
        many_transfers["tanks"] = roomy
        many_transfers["steps"] = [{"id": "s", "transfers": [
            {"id": f"t{i}", "source": "S", "target": "T",
             "volume_l": "1"} for i in range(257)]}]
        cases.append(many_transfers)
        for i, data in enumerate(cases):
            with self.subTest(case=i):
                store = self.tmp / f"limit-{i}"
                path = self.write(f"limit-{i}.json", data)
                proc = self.run_cli("init", "--revision", "r1",
                                    "--source", path, check=False)
                self.assertEqual(proc.returncode, 2)
                self.assertFalse(store.exists())

    def test_source_byte_limit(self):
        self.init()
        big = self.tmp / "big.json"
        big.write_bytes(b" " * (524288 + 1))
        proc = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                            "--source", big, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.run_cli("head"), {"revision": "r1"})
        # Exactly at the limit is allowed if it is valid JSON.
        payload = json.dumps(base_plan())
        self.assertLessEqual(len(payload.encode()), 524288)
        at_limit = self.tmp / "at.json"
        at_limit.write_bytes(payload.encode() + b" " * (524288 - len(payload)))
        self.run_cli("revise", "--revision", "r2", "--base", "r1",
                     "--source", at_limit)

    def test_receipt_limit(self):
        self.init()
        # One empty-ish but non-empty sample selector available per receipt is
        # limited, so issue the same selector under distinct ids.
        for i in range(1024):
            self.issue(f"rc{i}", "closing", "s2", "A")
        proc = self.run_cli("issue", "--receipt", "overflow", "--kind",
                            "closing", "--step", "s2", "--item", "A",
                            check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.run_cli("head"), {"revision": "r1"})


    def test_numeric_text_and_transfer_order_do_not_revoke(self):
        self.init()
        self.issue("rc-draw", "transfer", "s2", "draw")
        self.issue("rc-e", "closing", "s2", "E")
        self.issue("rc-a", "closing", "s2", "A")
        # Same exact values written differently and the s1 transfer list
        # reversed: no evidence changes, so no receipt is revoked.
        same = base_plan()
        same["steps"][0]["transfers"][0]["volume_l"] = "5.0"
        same["steps"][0]["transfers"][1]["volume_l"] = "05".lstrip("0") + ".000"
        same["steps"][0]["transfers"].reverse()
        same["tanks"][0]["volume_l"] = "10.00"
        same["tanks"][0]["capacity_l"] = "20.0"
        result = self.run_cli("revise", "--revision", "r2", "--base", "r1",
                              "--source", self.write("same.json", same))
        self.assertEqual(result["revoked"], [])
        for rid in ("rc-draw", "rc-e", "rc-a"):
            self.assertEqual(self.run_cli("read", "--receipt", rid)["issued_at"], "r1")

    def test_unknown_receipt_and_bad_base(self):
        self.init()
        proc = self.run_cli("read", "--receipt", "nope", check=False)
        self.assertEqual(proc.returncode, 2)
        proc = self.run_cli("revise", "--revision", "r2", "--base", "nope",
                            "--source", self.write("r2.json", corrected_plan()),
                            check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.run_cli("head"), {"revision": "r1"})


if __name__ == "__main__":
    unittest.main()
