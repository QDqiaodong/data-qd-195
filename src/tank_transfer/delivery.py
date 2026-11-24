"""Offline fact-table delivery: the producer store.

One producer store serves one caller-chosen drill id.  The store is
initialized with a complete v1 plan as version 0; every later publish
submits another complete v1 plan plus the expected current version and, if
accepted, becomes the next consecutive version.  Each version has an exact
fact table; each adjacent pair of versions has a delta packet.

The store retains a sliding window: the complete bootstrap snapshot at
``floor`` and every adjacent delta packet from ``floor`` up to ``head``.
Publishing advances ``floor`` only as far as the retention capacity
``k`` requires, and never past any registered consumer's confirmed
position — such a publish is reported ``blocked`` instead.  Content used
only by versions older than ``floor`` (snapshots, source plans and delta
packets ending at or before ``floor``) is reclaimed when ``floor`` advances.

Consumers register to receive a bootstrap version, apply delta packets to
their own local replicas and hand acknowledgments back; a confirmation only
moves that consumer's confirmed position, never the window.  All exchanges
are JSON documents; neither side touches the other's store.

Success exits 0 and prints one JSON object; ``blocked`` and ``none`` are
normal business results.  Rejection exits 2 with the reason on stderr and
leaves the committed state unchanged.
"""

import argparse
from pathlib import Path
import sys

from .checks import ident
from .facts import (ACK_FIELDS, CURSOR_FIELDS, MAX_CONSUMERS, MAX_PACKET_BYTES,
                    MAX_PLAN_BYTES, build_facts, build_packet,
                    canonical_json, check_fingerprint_text, fingerprint)
from .io import load_bounded
from .journal import StoreError, create_store, load_state, write_state

FORMAT_VERSION = 1
MAX_K = 8


class Rejected(Exception):
    """Business rejection: exit 2, reason on stderr, no state change."""


def _load(store):
    try:
        return load_state(Path(store), FORMAT_VERSION)
    except StoreError as exc:
        raise Rejected(str(exc))


def _load_plan(path, expected_tanks=None, expected_species=None):
    try:
        data = load_bounded(path, MAX_PLAN_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected(f"invalid source: {exc}")
    try:
        rows = build_facts(data, expected_tanks, expected_species)
    except (ValueError, RecursionError) as exc:
        raise Rejected(f"invalid plan: {exc}")
    return data, rows


def _load_document(path, fields, what):
    try:
        document = load_bounded(path, MAX_PACKET_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected(f"invalid {what}: {exc}")
    if not isinstance(document, dict) or set(document) != set(fields):
        raise Rejected(f"invalid {what}: expected fields {sorted(fields)}")
    return document


def _load_cursor(path):
    cursor = _load_document(path, CURSOR_FIELDS, "cursor")
    try:
        ident(cursor["drill"])
        if type(cursor["version"]) is not int or cursor["version"] < 0:
            raise ValueError("invalid cursor version")
        check_fingerprint_text(cursor["snapshot"])
    except ValueError as exc:
        raise Rejected(f"invalid cursor: {exc}")
    return cursor


def _load_ack(path):
    ack = _load_document(path, ACK_FIELDS, "acknowledgment")
    try:
        ident(ack["consumer"])
        ident(ack["drill"])
        if type(ack["version"]) is not int or ack["version"] < 1:
            raise ValueError("invalid acknowledgment version")
        check_fingerprint_text(ack["snapshot"])
        check_fingerprint_text(ack["packet"])
    except ValueError as exc:
        raise Rejected(f"invalid acknowledgment: {exc}")
    return ack


def _check_ident(value, what):
    try:
        return ident(value)
    except ValueError as exc:
        raise Rejected(f"invalid {what}: {exc}")


def command_init(store, drill, k, plan):
    _check_ident(drill, "drill id")
    if type(k) is not int or not 1 <= k <= MAX_K:
        raise Rejected(f"retention capacity k must be an integer in 1..{MAX_K}")
    path = Path(store)
    data, rows = _load_plan(plan)
    try:
        if path.exists() or path.is_symlink():
            raise Rejected("store path already exists")
    except OSError as exc:
        raise Rejected(f"cannot inspect store path: {exc}")
    state = {
        "format": FORMAT_VERSION,
        "drill": drill,
        "k": k,
        "floor": 0,
        "head": 0,
        "tanks": _plan_tanks(data),
        "species": sorted(data["species"]),
        "versions": {"0": {"fingerprint": fingerprint(rows),
                           "plan": data, "facts": rows}},
        "deltas": {},
        "consumers": {},
    }
    try:
        create_store(path, state)
    except StoreError as exc:
        raise Rejected(str(exc))
    return {"drill": drill, "k": k, "version": 0, "floor": 0, "head": 0,
            "snapshot": state["versions"]["0"]["fingerprint"]}


def _plan_tanks(data):
    return sorted(tank["id"] for tank in data["tanks"])


def command_status(store):
    state = _load(store)
    return {
        "drill": state["drill"],
        "k": state["k"],
        "floor": state["floor"],
        "head": state["head"],
        "versions": sorted(int(version) for version in state["versions"]),
        "deltas": sorted(int(version) for version in state["deltas"]),
        "consumers": {consumer: {"bootstrap": record["bootstrap"],
                                 "confirmed": record["confirmed"]}
                      for consumer, record in sorted(state["consumers"].items())},
    }


def command_register(store, consumer):
    _check_ident(consumer, "consumer id")
    state = _load(store)
    record = state["consumers"].get(consumer)
    if record is not None:
        # Re-registration never resets or advances any position.
        return {"consumer": consumer, "bootstrap": record["bootstrap"],
                "fingerprint": record["bootstrap_fingerprint"]}
    if len(state["consumers"]) >= MAX_CONSUMERS:
        raise Rejected("consumer limit reached")
    floor = state["floor"]
    record = {"bootstrap": floor,
              "bootstrap_fingerprint":
                  state["versions"][str(floor)]["fingerprint"],
              "confirmed": floor,
              "ack": None}
    state["consumers"][consumer] = record
    write_state(Path(store), state)
    return {"consumer": consumer, "bootstrap": floor,
            "fingerprint": record["bootstrap_fingerprint"]}


def command_snapshot(store, consumer):
    _check_ident(consumer, "consumer id")
    state = _load(store)
    record = state["consumers"].get(consumer)
    if record is None:
        raise Rejected("unknown consumer")
    version = record["bootstrap"]
    entry = state["versions"].get(str(version))
    if entry is None:
        raise Rejected("bootstrap snapshot has been reclaimed")
    return {"drill": state["drill"], "version": version,
            "fingerprint": entry["fingerprint"], "facts": entry["facts"]}


def command_next(store, cursor_path):
    state = _load(store)
    cursor = _load_cursor(cursor_path)
    if cursor["drill"] != state["drill"]:
        raise Rejected("cursor is for a different drill")
    version = cursor["version"]
    if version < state["floor"] or version > state["head"]:
        raise Rejected("cursor is outside the retained window")
    if cursor["snapshot"] != state["versions"][str(version)]["fingerprint"]:
        raise Rejected("cursor snapshot fingerprint mismatch")
    if version == state["head"]:
        return {"status": "none", "drill": state["drill"], "version": version}
    return state["deltas"][str(version + 1)]


def command_publish(store, plan, expect):
    state = _load(store)
    if type(expect) is not int or expect != state["head"]:
        raise Rejected("expected version is not the current head")
    head = state["head"]
    data, rows = _load_plan(plan, state["tanks"], state["species"])
    new_head = head + 1
    packet = build_packet(state["drill"], head,
                          state["versions"][str(head)]["facts"],
                          new_head, rows)
    needed_floor = max(state["floor"], new_head - state["k"])
    blockers = [{"consumer": consumer, "confirmed": record["confirmed"]}
                for consumer, record in sorted(state["consumers"].items())
                if record["confirmed"] < needed_floor]
    if blockers:
        return {"status": "blocked", "drill": state["drill"],
                "floor": state["floor"], "head": head, "blockers": blockers}
    state["versions"][str(new_head)] = {
        "fingerprint": fingerprint(rows), "plan": data, "facts": rows}
    state["deltas"][str(new_head)] = packet
    state["head"] = new_head
    state["floor"] = needed_floor
    for version in [key for key in state["versions"]
                    if int(key) < needed_floor]:
        del state["versions"][version]
    for version in [key for key in state["deltas"]
                    if int(key) <= needed_floor]:
        del state["deltas"][version]
    write_state(Path(store), state)
    return {"status": "published", "drill": state["drill"],
            "version": new_head, "floor": needed_floor, "head": new_head,
            "snapshot": fingerprint(rows), "packet": packet["fingerprint"]}


def command_confirm(store, consumer, ack_path):
    _check_ident(consumer, "consumer id")
    state = _load(store)
    record = state["consumers"].get(consumer)
    if record is None:
        raise Rejected("unknown consumer")
    ack = _load_ack(ack_path)
    if ack["consumer"] != consumer:
        raise Rejected("acknowledgment belongs to a different consumer")
    if ack["drill"] != state["drill"]:
        raise Rejected("acknowledgment is for a different drill")
    if record["ack"] is not None and ack == record["ack"]:
        # The same acknowledgment this consumer currently stands on:
        # success without touching any state, even if the delta content it
        # refers to has already been reclaimed.
        return {"status": "confirmed", "consumer": consumer,
                "confirmed": record["confirmed"]}
    version = ack["version"]
    if version <= record["confirmed"]:
        raise Rejected("acknowledgment does not advance the confirmed position")
    if version > state["head"]:
        raise Rejected("acknowledgment names an unknown version")
    if ack["snapshot"] != state["versions"][str(version)]["fingerprint"]:
        raise Rejected("acknowledgment snapshot fingerprint mismatch")
    if ack["packet"] != state["deltas"][str(version)]["fingerprint"]:
        raise Rejected("acknowledgment packet fingerprint mismatch")
    record["confirmed"] = version
    record["ack"] = ack
    write_state(Path(store), state)
    return {"status": "confirmed", "consumer": consumer, "confirmed": version}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected(f"usage: {message}")


def _parser():
    parser = _Parser(prog="python -m tank_transfer.delivery")
    parser.add_argument("--store", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--drill", required=True)
    init.add_argument("--k", required=True, type=int)
    init.add_argument("--plan", required=True)

    sub.add_parser("status")

    register = sub.add_parser("register")
    register.add_argument("--consumer", required=True)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--consumer", required=True)

    next_packet = sub.add_parser("next")
    next_packet.add_argument("--cursor", required=True)

    publish = sub.add_parser("publish")
    publish.add_argument("--plan", required=True)
    publish.add_argument("--expect", required=True, type=int)

    confirm = sub.add_parser("confirm")
    confirm.add_argument("--consumer", required=True)
    confirm.add_argument("--ack", required=True)
    return parser


def _run(args):
    if args.command == "init":
        return command_init(args.store, args.drill, args.k, args.plan)
    if args.command == "status":
        return command_status(args.store)
    if args.command == "register":
        return command_register(args.store, args.consumer)
    if args.command == "snapshot":
        return command_snapshot(args.store, args.consumer)
    if args.command == "next":
        return command_next(args.store, args.cursor)
    if args.command == "publish":
        return command_publish(args.store, args.plan, args.expect)
    if args.command == "confirm":
        return command_confirm(args.store, args.consumer, args.ack)
    raise Rejected("unknown command")


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        result = _run(args)
    except Rejected as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError, UnicodeError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(canonical_json(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
