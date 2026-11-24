"""Acceptance and recovery tests for offline fact-table delivery."""

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
FIXTURES = ROOT / "fixtures" / "delivery"
sys.path.insert(0, str(SRC))

from tank_transfer.facts import (build_facts, fingerprint,  # noqa: E402
                                 packet_fingerprint)


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class DeliveryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="delivery-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.producer = self.tmp / "producer"

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_module(self, module, store, *args, crash=None, check=True):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env.pop("TANK_TRANSFER_CRASH_POINT", None)
        if crash:
            env["TANK_TRANSFER_CRASH_POINT"] = crash
        proc = subprocess.run(
            [sys.executable, "-m", module, "--store", str(store),
             *map(str, args)],
            capture_output=True, text=True, env=env, cwd=str(ROOT))
        if check:
            self.assertEqual(proc.returncode, 0,
                             msg=f"stderr={proc.stderr!r} "
                                 f"stdout={proc.stdout!r}")
            return json.loads(proc.stdout)
        return proc

    def prod(self, *args, **kwargs):
        return self.run_module("tank_transfer.delivery", self.producer,
                               *args, **kwargs)

    def repl(self, name, *args, **kwargs):
        return self.run_module("tank_transfer.replica", self.tmp / name,
                               *args, **kwargs)

    def init_producer(self, k=2, plan="v0.json", drill="drill-1"):
        return self.prod("init", "--drill", drill, "--k", k,
                         "--plan", FIXTURES / plan)

    def register(self, consumer):
        return self.prod("register", "--consumer", consumer)

    def bootstrap_replica(self, consumer):
        snapshot = self.prod("snapshot", "--consumer", consumer)
        path = self.write(f"snapshot-{consumer}.json", snapshot)
        return self.repl(consumer, "init", "--consumer", consumer,
                         "--snapshot", path)

    def cursor_of(self, consumer):
        status = self.repl(consumer, "status")
        return {"drill": status["drill"], "version": status["version"],
                "snapshot": status["snapshot"]}

    def fetch_next(self, consumer):
        cursor = self.write(f"cursor-{consumer}.json",
                            self.cursor_of(consumer))
        return self.prod("next", "--cursor", cursor)

    def apply_next(self, consumer):
        packet = self.fetch_next(consumer)
        self.assertNotEqual(packet.get("status"), "none")
        path = self.write(f"packet-{consumer}-{packet['to_version']}.json",
                          packet)
        ack = self.repl(consumer, "apply", "--packet", path)
        return packet, ack

    def confirm(self, consumer, ack):
        path = self.write(f"ack-{consumer}-{ack['version']}.json", ack)
        return self.prod("confirm", "--consumer", consumer, "--ack", path)

    def publish(self, plan, expect, **kwargs):
        return self.prod("publish", "--plan", FIXTURES / plan,
                         "--expect", expect, **kwargs)

    def replica_facts(self, consumer):
        return self.repl(consumer, "facts")["facts"]

    def plan_facts(self, name):
        return build_facts(load_fixture(name))

    def producer_state_bytes(self):
        return (self.producer / "state.json").read_bytes()


class FactTableTests(DeliveryCase):
    def test_acceptance_scenario_facts_and_deltas(self):
        self.init_producer(k=8)
        self.register("grading")
        self.bootstrap_replica("grading")

        # Version 0 facts, straight from the bootstrap snapshot.
        facts0 = self.plan_facts("v0.json")
        self.assertEqual(self.replica_facts("grading"), facts0)
        by_key = {tuple(row["key"]): row["value"] for row in facts0}
        self.assertEqual(by_key[("transfer", "s2", "draw")],
                         {"source": "B", "target": "C", "volume_l": "1",
                          "mass_g": {"salt": "2/3"}})
        self.assertEqual(by_key[("closing", "s2", "C")],
                         {"volume_l": "1", "mass_g": {"salt": "2/3"}})
        self.assertEqual(by_key[("step", "s2")], {"ordinal": 2})
        # Rows full of zeros are kept.
        self.assertEqual(by_key[("initial", "C")],
                         {"volume_l": "0", "mass_g": {"salt": "0"}})
        self.assertEqual(by_key[("closing", "s1", "D")],
                         {"volume_l": "0", "mass_g": {"salt": "0"}})

        # Version 1: feed retargeted to D; the draw instruction is unchanged
        # but now carries 0 g, so its row and the closing rows are rewritten.
        self.publish("v1.json", 0)
        packet1, _ack1 = self.apply_next("grading")
        self.assertEqual(packet1["deletes"], [])
        writes1 = {tuple(row["key"]): row["value"]
                   for row in packet1["writes"]}
        self.assertEqual(writes1[("transfer", "s2", "draw")],
                         {"source": "B", "target": "C", "volume_l": "1",
                          "mass_g": {"salt": "0"}})
        self.assertEqual(writes1[("closing", "s2", "C")],
                         {"volume_l": "1", "mass_g": {"salt": "0"}})
        facts1 = self.plan_facts("v1.json")
        self.assertEqual(self.replica_facts("grading"), facts1)
        by_key1 = {tuple(row["key"]): row["value"] for row in facts1}
        self.assertEqual(by_key1[("transfer", "s2", "draw")]["mass_g"],
                         {"salt": "0"})
        self.assertEqual(by_key1[("closing", "s2", "C")],
                         {"volume_l": "1", "mass_g": {"salt": "0"}})

        # Version 2: draw removed; the empty step s2 and C's closing row stay.
        self.publish("v2.json", 1)
        packet2, _ack2 = self.apply_next("grading")
        self.assertEqual(packet2["deletes"], [["transfer", "s2", "draw"]])
        keys2 = {tuple(row["key"]) for row in self.replica_facts("grading")}
        self.assertIn(("step", "s2"), keys2)
        self.assertIn(("closing", "s2", "C"), keys2)
        self.assertNotIn(("transfer", "s2", "draw"), keys2)
        self.assertEqual(self.replica_facts("grading"),
                         self.plan_facts("v2.json"))

        # Version 3: unchanged content still forms a new version whose delta
        # is empty; equal snapshot fingerprints never replace version numbers.
        self.publish("v3.json", 2)
        packet3, ack3 = self.apply_next("grading")
        self.assertEqual(packet3["deletes"], [])
        self.assertEqual(packet3["writes"], [])
        self.assertEqual(packet3["from_snapshot"], packet3["to_snapshot"])
        self.assertNotEqual(packet3["fingerprint"], packet2["fingerprint"])
        self.assertEqual(ack3["version"], 3)
        self.assertEqual(self.replica_facts("grading"),
                         self.plan_facts("v3.json"))
        # Version 3 confirms independently of version 2.
        self.assertEqual(self.confirm("grading", ack3),
                         {"status": "confirmed", "consumer": "grading",
                          "confirmed": 3})
        status = self.prod("status")
        self.assertEqual(status["consumers"]["grading"]["confirmed"], 3)

    def test_step_order_is_a_fact_change(self):
        init = self.init_producer(k=8)
        swapped = load_fixture("v0.json")
        swapped["steps"] = [swapped["steps"][1], swapped["steps"][0]]
        result = self.prod("publish", "--plan",
                           self.write("swapped.json", swapped), "--expect", 0)
        self.assertNotEqual(result["snapshot"], init["snapshot"])
        by_key = {tuple(row["key"]): row["value"]
                  for row in build_facts(swapped)}
        self.assertEqual(by_key[("step", "s1")], {"ordinal": 2})
        self.assertEqual(by_key[("step", "s2")], {"ordinal": 1})

    def test_equivalent_numbers_and_reordering_give_empty_delta(self):
        plan = {
            "species": ["salt", "sugar"],
            "tanks": [
                {"id": "A", "capacity_l": "10", "volume_l": "4",
                 "mass_g": {"salt": "8", "sugar": "2"}},
                {"id": "B", "capacity_l": "10", "volume_l": "2",
                 "mass_g": {"salt": "0", "sugar": "1"}},
                {"id": "C", "capacity_l": "10", "volume_l": "0",
                 "mass_g": {"salt": "0", "sugar": "0"}},
            ],
            "steps": [
                {"id": "s1", "transfers": [
                    {"id": "t1", "source": "A", "target": "B",
                     "volume_l": "1"},
                    {"id": "t2", "source": "A", "target": "C",
                     "volume_l": "2"}]},
                {"id": "s2", "transfers": [
                    {"id": "t1", "source": "B", "target": "C",
                     "volume_l": "1"}]},
            ],
        }
        init = self.prod("init", "--drill", "drill-1", "--k", 8,
                         "--plan", self.write("eq0.json", plan))
        # Same-named transfer ids in different steps are distinct rows.
        keys = {tuple(row["key"]) for row in build_facts(plan)}
        self.assertIn(("transfer", "s1", "t1"), keys)
        self.assertIn(("transfer", "s2", "t1"), keys)

        # Equivalent decimal writings and reordered transfer/tank/species
        # lists do not change the fact table.
        same = {
            "species": ["sugar", "salt"],
            "tanks": [
                {"id": "C", "capacity_l": "10.0", "volume_l": "0.00",
                 "mass_g": {"sugar": "0", "salt": "0.0"}},
                {"id": "B", "capacity_l": "10", "volume_l": "2.0",
                 "mass_g": {"salt": "0.00", "sugar": "1"}},
                {"id": "A", "capacity_l": "10", "volume_l": "4",
                 "mass_g": {"salt": "8.0", "sugar": "2"}},
            ],
            "steps": [
                {"id": "s1", "transfers": [
                    {"id": "t2", "source": "A", "target": "C",
                     "volume_l": "2.00"},
                    {"id": "t1", "source": "A", "target": "B",
                     "volume_l": "1.0"}]},
                {"id": "s2", "transfers": [
                    {"id": "t1", "source": "B", "target": "C",
                     "volume_l": "1"}]},
            ],
        }
        result = self.prod("publish", "--plan", self.write("eq1.json", same),
                           "--expect", 0)
        self.assertEqual(result["snapshot"], init["snapshot"])
        cursor = self.write("eq-cursor.json",
                            {"drill": "drill-1", "version": 0,
                             "snapshot": init["snapshot"]})
        packet = self.prod("next", "--cursor", cursor)
        self.assertEqual(packet["deletes"], [])
        self.assertEqual(packet["writes"], [])
        self.assertEqual(packet["from_snapshot"], packet["to_snapshot"])


class WindowTests(DeliveryCase):
    def prepare(self):
        self.r0 = self.init_producer(k=2)
        self.register("grading")
        self.register("archive")
        self.bootstrap_replica("grading")
        self.bootstrap_replica("archive")
        self.r1 = self.publish("v1.json", 0)
        self.r2 = self.publish("v2.json", 1)

    def confirm_grading_to_2(self):
        self.apply_next("grading")
        _packet, ack2 = self.apply_next("grading")
        self.confirm("grading", ack2)

    def confirm_archive_to_1(self):
        packet1 = self.fetch_next("archive")
        path = self.write("archive-p1.json", packet1)
        ack1 = self.repl("archive", "apply", "--packet", path)
        self.confirm("archive", ack1)
        return ack1

    def test_blocked_then_released_window(self):
        self.prepare()
        self.confirm_grading_to_2()
        self.assertEqual(self.prod("status")["consumers"], {
            "archive": {"bootstrap": 0, "confirmed": 0},
            "grading": {"bootstrap": 0, "confirmed": 2}})

        # Only archive blocks the publish; the old window stays fetchable.
        blocked = self.publish("v3.json", 2)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["blockers"],
                         [{"consumer": "archive", "confirmed": 0}])
        status = self.prod("status")
        self.assertEqual((status["floor"], status["head"]), (0, 2))
        snapshot0 = self.prod("snapshot", "--consumer", "archive")
        self.assertEqual(snapshot0["facts"], self.plan_facts("v0.json"))
        packet1 = self.fetch_next("archive")
        self.assertEqual((packet1["from_version"], packet1["to_version"]),
                         (0, 1))
        cursor1 = self.write("cursor-v1.json",
                             {"drill": "drill-1", "version": 1,
                              "snapshot": self.r1["snapshot"]})
        packet2 = self.prod("next", "--cursor", cursor1)
        self.assertEqual((packet2["from_version"], packet2["to_version"]),
                         (1, 2))

        # Fetching a packet is not a confirmation: still blocked.
        self.assertEqual(self.publish("v3.json", 2)["status"], "blocked")

        # Archive actually applies packet 1 and confirms version 1.
        ack1 = self.repl("archive", "apply", "--packet",
                         self.write("archive-p1.json", packet1))
        self.confirm("archive", ack1)

        # The same publish now succeeds and advances floor to 1.
        result = self.publish("v3.json", 2)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["floor"], 1)
        status = self.prod("status")
        self.assertEqual((status["floor"], status["head"]), (1, 3))
        # Queryable evidence: out-of-window content has been reclaimed.
        self.assertEqual(status["versions"], [1, 2, 3])
        self.assertEqual(status["deltas"], [2, 3])
        proc = self.prod("snapshot", "--consumer", "archive", check=False)
        self.assertEqual(proc.returncode, 2)

        # Reopen the producer in a new process; a late consumer bootstraps
        # from version 1 and reaches the same version 3 facts.
        self.assertEqual(self.register("late")["bootstrap"], 1)
        self.bootstrap_replica("late")
        self.assertEqual(self.replica_facts("late"), self.plan_facts("v1.json"))
        self.apply_next("late")
        self.apply_next("late")
        self.assertEqual(self.replica_facts("late"), self.plan_facts("v3.json"))

        # The version 0 cursor is expired.
        expired = self.write("expired.json",
                             {"drill": "drill-1", "version": 0,
                              "snapshot": snapshot0["fingerprint"]})
        proc = self.prod("next", "--cursor", expired, check=False)
        self.assertEqual(proc.returncode, 2)

        # Archive continues from its own version 1 with the intermediate
        # packet; it is not forced onto the newest snapshot.
        self.apply_next("archive")
        self.assertEqual(self.replica_facts("archive"),
                         self.plan_facts("v2.json"))

        # Re-sending archive's version 1 confirmation succeeds without
        # changing any state, even though delta 1 has been reclaimed.
        before = self.producer_state_bytes()
        self.assertEqual(self.confirm("archive", ack1),
                         {"status": "confirmed", "consumer": "archive",
                          "confirmed": 1})
        self.assertEqual(self.producer_state_bytes(), before)

    def test_publish_crash_before_commit_keeps_old_window(self):
        self.prepare()
        self.confirm_grading_to_2()
        self.confirm_archive_to_1()
        proc = self.prod("publish", "--plan", FIXTURES / "v3.json",
                         "--expect", 2, crash="before-commit", check=False)
        self.assertNotEqual(proc.returncode, 0)
        status = self.prod("status")
        self.assertEqual((status["floor"], status["head"]), (0, 2))
        self.assertEqual(status["versions"], [0, 1, 2])
        self.assertEqual(status["deltas"], [1, 2])
        # The complete old window is still deliverable.
        self.assertEqual(self.prod("snapshot", "--consumer", "archive")
                         ["facts"], self.plan_facts("v0.json"))
        cursor0 = self.write("cursor-v0.json",
                             {"drill": "drill-1", "version": 0,
                              "snapshot": self.r0["snapshot"]})
        self.assertEqual(self.prod("next", "--cursor", cursor0)
                         ["to_version"], 1)
        cursor1 = self.write("cursor-v1.json",
                             {"drill": "drill-1", "version": 1,
                              "snapshot": self.r1["snapshot"]})
        self.assertEqual(self.prod("next", "--cursor", cursor1)
                         ["to_version"], 2)
        # The retry commits cleanly.
        self.assertEqual(self.publish("v3.json", 2)["status"], "published")
        self.assertEqual(self.prod("status")["floor"], 1)

    def test_publish_crash_after_commit_shows_new_window(self):
        self.prepare()
        self.confirm_grading_to_2()
        self.confirm_archive_to_1()
        proc = self.prod("publish", "--plan", FIXTURES / "v3.json",
                         "--expect", 2, crash="after-commit", check=False)
        self.assertNotEqual(proc.returncode, 0)
        status = self.prod("status")
        self.assertEqual((status["floor"], status["head"]), (1, 3))
        self.assertEqual(status["versions"], [1, 2, 3])
        self.assertEqual(status["deltas"], [2, 3])
        # Only the complete new window is visible and it is deliverable.
        self.assertEqual(self.register("late")["bootstrap"], 1)
        self.bootstrap_replica("late")
        self.apply_next("late")
        self.apply_next("late")
        self.assertEqual(self.replica_facts("late"), self.plan_facts("v3.json"))
        expired = self.write("expired.json",
                             {"drill": "drill-1", "version": 0,
                              "snapshot": "0" * 64})
        proc = self.prod("next", "--cursor", expired, check=False)
        self.assertEqual(proc.returncode, 2)
        # The already-committed publish is not re-accepted as a retry.
        proc = self.publish("v3.json", 2, check=False)
        self.assertEqual(proc.returncode, 2)


class ReplicaRecoveryTests(DeliveryCase):
    def prepare(self):
        self.init_producer(k=8)
        self.register("grading")
        self.bootstrap_replica("grading")
        self.publish("v1.json", 0)
        self.publish("v2.json", 1)
        self.publish("v3.json", 2)
        _packet, self.ack1 = self.apply_next("grading")
        self.packet2 = self.fetch_next("grading")
        self.packet2_path = self.write("packet2.json", self.packet2)

    def replica_state_bytes(self):
        return (self.tmp / "grading" / "state.json").read_bytes()

    def test_apply_crash_before_commit_then_retry(self):
        self.prepare()
        proc = self.repl("grading", "apply", "--packet", self.packet2_path,
                         crash="before-commit", check=False)
        self.assertNotEqual(proc.returncode, 0)
        # Data and cursor are still the old committed ones, together.
        self.assertEqual(self.repl("grading", "status")["version"], 1)
        self.assertEqual(self.replica_facts("grading"),
                         self.plan_facts("v1.json"))
        self.assertEqual(self.repl("grading", "ack"), self.ack1)
        # Retry lands exactly once; re-sending returns the same ack.
        ack2 = self.repl("grading", "apply", "--packet", self.packet2_path)
        self.assertEqual(ack2["version"], 2)
        self.assertEqual(self.replica_facts("grading"),
                         self.plan_facts("v2.json"))
        before = self.replica_state_bytes()
        self.assertEqual(self.repl("grading", "apply", "--packet",
                                   self.packet2_path), ack2)
        self.assertEqual(self.replica_state_bytes(), before)

    def test_apply_crash_after_commit_then_resend(self):
        self.prepare()
        proc = self.repl("grading", "apply", "--packet", self.packet2_path,
                         crash="after-commit", check=False)
        self.assertNotEqual(proc.returncode, 0)
        # A new process sees data and cursor landed together.
        self.assertEqual(self.repl("grading", "status")["version"], 2)
        self.assertEqual(self.replica_facts("grading"),
                         self.plan_facts("v2.json"))
        # The committed acknowledgment is re-fetchable; re-sending the same
        # canonical packet returns it without landing twice.
        ack = self.repl("grading", "ack")
        self.assertEqual(ack["version"], 2)
        before = self.replica_state_bytes()
        self.assertEqual(self.repl("grading", "apply", "--packet",
                                   self.packet2_path), ack)
        self.assertEqual(self.replica_state_bytes(), before)


class MisuseTests(DeliveryCase):
    def prepare(self):
        self.r0 = self.init_producer(k=8)
        self.register("grading")
        self.register("archive")
        self.bootstrap_replica("grading")
        self.bootstrap_replica("archive")
        self.r1 = self.publish("v1.json", 0)
        self.r2 = self.publish("v2.json", 1)
        self.r3 = self.publish("v3.json", 2)
        _packet, self.ack1 = self.apply_next("grading")
        self.packet2 = self.fetch_next("grading")
        self.packet2_path = self.write("packet2.json", self.packet2)

    def forged(self, packet, **changes):
        packet = dict(packet)
        packet.update(changes)
        packet["fingerprint"] = packet_fingerprint(packet)
        return packet

    def assert_rejected_and_unchanged(self, packet):
        path = self.write("forged.json", packet)
        proc = self.repl("grading", "apply", "--packet", path, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.repl("grading", "status")["version"], 1)
        self.assertEqual(self.replica_facts("grading"),
                         self.plan_facts("v1.json"))

    def test_out_of_order_packet_rejected(self):
        self.prepare()
        cursor2 = self.write("cursor2.json",
                             {"drill": "drill-1", "version": 2,
                              "snapshot": self.r2["snapshot"]})
        packet3 = self.prod("next", "--cursor", cursor2)
        self.assertEqual(packet3["from_version"], 2)
        self.assert_rejected_and_unchanged(packet3)
        # An older packet that is not the one which brought the replica to
        # its current version is rejected as well.
        cursor0 = self.write("cursor0b.json",
                             {"drill": "drill-1", "version": 0,
                              "snapshot": self.r0["snapshot"]})
        packet1 = self.prod("next", "--cursor", cursor0)
        self.repl("grading", "apply", "--packet", self.packet2_path)
        proc = self.repl("grading", "apply", "--packet",
                         self.write("packet1.json", packet1), check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.repl("grading", "status")["version"], 2)
        self.assertEqual(self.replica_facts("grading"),
                         self.plan_facts("v2.json"))

    def test_forged_same_target_different_identity_rejected(self):
        self.prepare()
        self.repl("grading", "apply", "--packet", self.packet2_path)
        # Same target version as the landed packet, different packet identity.
        forged = self.forged(self.packet2, deletes=[])
        proc = self.repl("grading", "apply", "--packet",
                         self.write("forged.json", forged), check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.repl("grading", "status")["version"], 2)
        self.assertEqual(self.replica_facts("grading"),
                         self.plan_facts("v2.json"))

    def test_structured_packet_missing_deletes_rejected(self):
        self.prepare()
        # Well-formed packet for 1 -> 2 but the necessary deletion is gone.
        self.assert_rejected_and_unchanged(self.forged(self.packet2,
                                                       deletes=[]))

    def test_operation_legality_rejections(self):
        self.prepare()
        # Build honest forgeries on top of the real 0 -> 1 packet.
        cursor0 = self.write("cursor0.json",
                             {"drill": "drill-1", "version": 0,
                              "snapshot": self.r0["snapshot"]})
        packet1 = self.prod("next", "--cursor", cursor0)
        # Delete of a key that does not exist.
        bad = self.forged(packet1, deletes=[["transfer", "s2", "nope"]])
        self.assert_rejected_and_unchanged(bad)
        # Write that leaves the existing value unchanged.
        unchanged = {"key": ["initial", "A"],
                     "value": {"volume_l": "2", "mass_g": {"salt": "4"}}}
        writes = sorted(packet1["writes"] + [unchanged],
                        key=lambda row: row["key"])
        self.assert_rejected_and_unchanged(self.forged(packet1,
                                                       writes=writes))
        # The same key in deletes and writes.
        dup = self.forged(packet1, deletes=[["closing", "s1", "B"]])
        self.assert_rejected_and_unchanged(dup)
        # A stale fingerprint field is rejected before anything else.
        stale = dict(packet1)
        stale["writes"] = []
        self.assert_rejected_and_unchanged(stale)

    def test_acknowledgment_mixups_rejected(self):
        self.prepare()
        _packet, ack2 = self.apply_next("grading")
        ack2_path = self.write("ack2.json", ack2)
        # Another consumer may not spend grading's acknowledgment.
        proc = self.prod("confirm", "--consumer", "archive",
                         "--ack", ack2_path, check=False)
        self.assertEqual(proc.returncode, 2)
        # The rightful consumer confirms cumulatively to version 2.
        self.assertEqual(self.confirm("grading", ack2),
                         {"status": "confirmed", "consumer": "grading",
                          "confirmed": 2})
        # Re-sending the same acknowledgment is a no-op success.
        self.assertEqual(self.confirm("grading", ack2),
                         {"status": "confirmed", "consumer": "grading",
                          "confirmed": 2})
        # An older acknowledgment is a regression.
        proc = self.prod("confirm", "--consumer", "grading",
                         "--ack", self.write("ack1.json", self.ack1),
                         check=False)
        self.assertEqual(proc.returncode, 2)
        # Unknown version, wrong snapshot fingerprint, wrong packet
        # fingerprint for a consumer still at its bootstrap.
        unknown = dict(self.ack1, consumer="archive", version=99)
        proc = self.prod("confirm", "--consumer", "archive",
                         "--ack", self.write("ack99.json", unknown),
                         check=False)
        self.assertEqual(proc.returncode, 2)
        wrong_snapshot = dict(self.ack1, consumer="archive",
                              snapshot="0" * 64)
        proc = self.prod("confirm", "--consumer", "archive",
                         "--ack", self.write("ackbad1.json", wrong_snapshot),
                         check=False)
        self.assertEqual(proc.returncode, 2)
        wrong_packet = dict(self.ack1, consumer="archive", packet="0" * 64)
        proc = self.prod("confirm", "--consumer", "archive",
                         "--ack", self.write("ackbad2.json", wrong_packet),
                         check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.prod("status")["consumers"]["archive"]
                         ["confirmed"], 0)

    def test_invalid_last_step_cannot_evict_window(self):
        self.prod("init", "--drill", "drill-1", "--k", 1,
                  "--plan", FIXTURES / "v0.json")
        self.publish("v1.json", 0)
        self.publish("v2.json", 1)
        status = self.prod("status")
        self.assertEqual((status["floor"], status["head"]), (1, 2))
        self.assertEqual(status["deltas"], [2])
        before = self.producer_state_bytes()
        proc = self.prod("publish", "--plan",
                         FIXTURES / "invalid-last-step.json", "--expect", 2,
                         check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.producer_state_bytes(), before)
        status = self.prod("status")
        self.assertEqual((status["floor"], status["head"]), (1, 2))
        self.assertEqual(status["deltas"], [2])


class RegistrationAndCursorTests(DeliveryCase):
    def test_registration_rules(self):
        self.init_producer(k=2)
        first = self.register("a")
        self.assertEqual(first["bootstrap"], 0)
        # Re-registration returns the original information, changes nothing.
        self.assertEqual(self.register("a"), first)
        before = self.producer_state_bytes()
        self.register("a")
        self.assertEqual(self.producer_state_bytes(), before)
        for i in range(1, 8):
            self.register(f"c{i}")
        proc = self.prod("register", "--consumer", "ninth", check=False)
        self.assertEqual(proc.returncode, 2)
        self.register("a")  # existing id still fine at the limit
        snapshot = self.prod("snapshot", "--consumer", "a")
        self.assertEqual(snapshot["version"], 0)
        self.assertEqual(snapshot["fingerprint"], first["fingerprint"])
        proc = self.prod("snapshot", "--consumer", "unknown", check=False)
        self.assertEqual(proc.returncode, 2)

    def test_cursor_validation(self):
        init = self.init_producer(k=2)
        r1 = self.publish("v1.json", 0)
        # A correct cursor at head explicitly gets "no new packet".
        cursor = self.write("at-head.json",
                            {"drill": "drill-1", "version": 1,
                             "snapshot": r1["snapshot"]})
        self.assertEqual(self.prod("next", "--cursor", cursor)["status"],
                         "none")
        # A cursor for another drill is rejected.
        foreign = self.write("foreign.json",
                             {"drill": "drill-2", "version": 0,
                              "snapshot": init["snapshot"]})
        proc = self.prod("next", "--cursor", foreign, check=False)
        self.assertEqual(proc.returncode, 2)
        # A fingerprint mismatch is rejected.
        mismatched = self.write("mismatched.json",
                                {"drill": "drill-1", "version": 0,
                                 "snapshot": r1["snapshot"]})
        proc = self.prod("next", "--cursor", mismatched, check=False)
        self.assertEqual(proc.returncode, 2)

    def test_packets_survive_plan_file_removal(self):
        plan0 = self.write("plan0.json", load_fixture("v0.json"))
        plan1 = self.write("plan1.json", load_fixture("v1.json"))
        init = self.prod("init", "--drill", "drill-1", "--k", 2,
                         "--plan", plan0)
        self.prod("publish", "--plan", plan1, "--expect", 0)
        plan0.unlink()
        plan1.write_text("not json anymore", encoding="utf-8")
        cursor = self.write("cursor.json",
                            {"drill": "drill-1", "version": 0,
                             "snapshot": init["snapshot"]})
        packet = self.prod("next", "--cursor", cursor)
        self.assertEqual(packet["to_version"], 1)
        self.assertEqual(self.prod("next", "--cursor",
                                   self.write("cursor1.json",
                                              {"drill": "drill-1",
                                               "version": 1,
                                               "snapshot":
                                               packet["to_snapshot"]}))
                         ["status"], "none")


class LimitTests(DeliveryCase):
    def test_plan_shape_limits(self):
        base = load_fixture("v0.json")
        cases = []
        many_tanks = load_fixture("v0.json")
        many_tanks["tanks"] = [
            {"id": f"T{i}", "capacity_l": "10", "volume_l": "0",
             "mass_g": {"salt": "0"}} for i in range(33)]
        many_tanks["steps"] = []
        cases.append(many_tanks)
        many_species = load_fixture("v0.json")
        many_species["species"] = [f"s{i}" for i in range(9)]
        for tank in many_species["tanks"]:
            tank["mass_g"] = {f"s{i}": "0" for i in range(9)}
        many_species["steps"] = []
        cases.append(many_species)
        many_steps = load_fixture("v0.json")
        many_steps["steps"] = [{"id": f"x{i}", "transfers": []}
                               for i in range(65)]
        cases.append(many_steps)
        many_transfers = load_fixture("v0.json")
        many_transfers["tanks"] = [
            {"id": "S", "capacity_l": "10000", "volume_l": "10000",
             "mass_g": {"salt": "0"}},
            {"id": "T", "capacity_l": "10000", "volume_l": "0",
             "mass_g": {"salt": "0"}}]
        many_transfers["steps"] = [{"id": "s", "transfers": [
            {"id": f"t{i}", "source": "S", "target": "T", "volume_l": "1"}
            for i in range(513)]}]
        cases.append(many_transfers)
        self.assertEqual(base["species"], ["salt"])
        for i, data in enumerate(cases):
            with self.subTest(case=i):
                store = self.tmp / f"limit-{i}"
                proc = self.run_module(
                    "tank_transfer.delivery", store, "init", "--drill", "d",
                    "--k", 1, "--plan", self.write(f"limit-{i}.json", data),
                    check=False)
                self.assertEqual(proc.returncode, 2)
                self.assertFalse(store.exists())

    def test_plan_byte_limit(self):
        payload = json.dumps(load_fixture("v0.json")).encode("utf-8")
        self.assertLessEqual(len(payload), 1048576)
        at_limit = self.tmp / "at-limit.json"
        at_limit.write_bytes(payload + b" " * (1048576 - len(payload)))
        self.prod("init", "--drill", "d", "--k", 1, "--plan", at_limit)
        over = self.tmp / "over.json"
        over.write_bytes(payload + b" " * (1048577 - len(payload)))
        store = self.tmp / "over-store"
        proc = self.run_module("tank_transfer.delivery", store, "init",
                               "--drill", "d", "--k", 1, "--plan", over,
                               check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(store.exists())

    def test_k_and_id_rules(self):
        for bad_k in (0, 9):
            proc = self.run_module("tank_transfer.delivery",
                                   self.tmp / f"k{bad_k}", "init",
                                   "--drill", "d", "--k", bad_k,
                                   "--plan", FIXTURES / "v0.json",
                                   check=False)
            self.assertEqual(proc.returncode, 2)
        proc = self.run_module("tank_transfer.delivery", self.tmp / "bad-id",
                               "init", "--drill", "bad id!", "--k", 1,
                               "--plan", FIXTURES / "v0.json", check=False)
        self.assertEqual(proc.returncode, 2)

    def test_id_sets_and_expected_version_enforced(self):
        self.init_producer(k=2)
        before = self.producer_state_bytes()
        extra_tank = load_fixture("v1.json")
        extra_tank["tanks"].append(
            {"id": "E", "capacity_l": "10", "volume_l": "0",
             "mass_g": {"salt": "0"}})
        proc = self.prod("publish", "--plan",
                         self.write("extra-tank.json", extra_tank),
                         "--expect", 0, check=False)
        self.assertEqual(proc.returncode, 2)
        renamed = load_fixture("v1.json")
        renamed["species"] = ["sugar"]
        for tank in renamed["tanks"]:
            tank["mass_g"] = {"sugar": tank["mass_g"]["salt"]}
        proc = self.prod("publish", "--plan",
                         self.write("renamed.json", renamed),
                         "--expect", 0, check=False)
        self.assertEqual(proc.returncode, 2)
        proc = self.prod("publish", "--plan", FIXTURES / "v1.json",
                         "--expect", 5, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.producer_state_bytes(), before)
        self.publish("v1.json", 0)
        proc = self.prod("publish", "--plan", FIXTURES / "v2.json",
                         "--expect", 0, check=False)
        self.assertEqual(proc.returncode, 2)

    def test_replica_init_checks_snapshot(self):
        self.init_producer(k=2)
        self.register("grading")
        snapshot = self.prod("snapshot", "--consumer", "grading")
        tampered = json.loads(json.dumps(snapshot))
        tampered["facts"][0]["value"]["volume_l"] = "9"
        proc = self.run_module("tank_transfer.replica", self.tmp / "replica",
                               "init", "--consumer", "grading", "--snapshot",
                               self.write("tampered.json", tampered),
                               check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((self.tmp / "replica").exists())


if __name__ == "__main__":
    unittest.main()
