"""Offline fact-table delivery: the consumer replica store.

A consumer keeps a local replica of one drill's fact table.  The replica is
created from the bootstrap snapshot document the producer hands out at
registration; afterwards it advances one delta packet at a time.  Each
packet is checked against the local cursor (drill id, version and snapshot
fingerprint), its own fingerprint, and the legality of every operation;
the post-application fingerprint must equal the packet's target snapshot
fingerprint.  Any rejection leaves the replica unchanged.  A successful
application commits the new fact table, the new cursor and the
acknowledgment together, so the acknowledgment can be fetched again and a
re-send of the same canonical packet returns it without re-landing data.

Success exits 0 and prints one JSON object.  Rejection exits 2 with the
reason on stderr and leaves the committed state unchanged.
"""

import argparse
from pathlib import Path
import sys

from .checks import ident
from .facts import (MAX_PACKET_BYTES, SNAPSHOT_FIELDS, apply_packet,
                    canonical_json, check_fingerprint_text, fingerprint,
                    packet_fingerprint, validate_facts, validate_packet)
from .io import load_bounded
from .journal import StoreError, create_store, load_state, write_state

FORMAT_VERSION = 1


class Rejected(Exception):
    """Business rejection: exit 2, reason on stderr, no state change."""


def _load(store):
    try:
        return load_state(Path(store), FORMAT_VERSION)
    except StoreError as exc:
        raise Rejected(str(exc))


def command_init(store, consumer, snapshot_path):
    try:
        ident(consumer)
    except ValueError as exc:
        raise Rejected(f"invalid consumer id: {exc}")
    path = Path(store)
    try:
        document = load_bounded(snapshot_path, MAX_PACKET_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected(f"invalid snapshot: {exc}")
    if not isinstance(document, dict) or set(document) != set(SNAPSHOT_FIELDS):
        raise Rejected("invalid snapshot: expected fields "
                         f"{sorted(SNAPSHOT_FIELDS)}")
    try:
        ident(document["drill"])
        if type(document["version"]) is not int or document["version"] < 0:
            raise ValueError("invalid snapshot version")
        check_fingerprint_text(document["fingerprint"])
        tanks, species = validate_facts(document["facts"])
    except ValueError as exc:
        raise Rejected(f"invalid snapshot: {exc}")
    if fingerprint(document["facts"]) != document["fingerprint"]:
        raise Rejected("snapshot fingerprint does not match its facts")
    try:
        if path.exists() or path.is_symlink():
            raise Rejected("store path already exists")
    except OSError as exc:
        raise Rejected(f"cannot inspect store path: {exc}")
    state = {
        "format": FORMAT_VERSION,
        "consumer": consumer,
        "drill": document["drill"],
        "version": document["version"],
        "snapshot": document["fingerprint"],
        "tanks": tanks,
        "species": species,
        "facts": document["facts"],
        "last_packet": None,
        "ack": None,
    }
    try:
        create_store(path, state)
    except StoreError as exc:
        raise Rejected(str(exc))
    return {"consumer": consumer, "drill": state["drill"],
            "version": state["version"], "snapshot": state["snapshot"]}


def command_apply(store, packet_path):
    state = _load(store)
    try:
        packet = load_bounded(packet_path, MAX_PACKET_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected(f"invalid packet: {exc}")
    try:
        validate_packet(packet, state["tanks"], state["species"])
    except ValueError as exc:
        raise Rejected(f"invalid packet: {exc}")
    if packet["drill"] != state["drill"]:
        raise Rejected("packet is for a different drill")
    if packet_fingerprint(packet) != packet["fingerprint"]:
        raise Rejected("packet fingerprint mismatch")
    if (state["last_packet"] is not None
            and packet["fingerprint"] == state["last_packet"]
            and packet["to_version"] == state["version"]):
        # The same canonical packet that brought the replica to its current
        # version: return the original acknowledgment, change nothing.
        return state["ack"]
    if packet["from_version"] != state["version"]:
        raise Rejected("packet does not continue the local cursor")
    if packet["from_snapshot"] != state["snapshot"]:
        raise Rejected("packet previous snapshot fingerprint mismatch")
    try:
        rows = apply_packet(state["facts"], packet)
    except ValueError as exc:
        raise Rejected(f"invalid packet: {exc}")
    if fingerprint(rows) != packet["to_snapshot"]:
        raise Rejected("applied facts do not match the packet target "
                       "fingerprint")
    ack = {"consumer": state["consumer"], "drill": state["drill"],
           "version": packet["to_version"], "snapshot": packet["to_snapshot"],
           "packet": packet["fingerprint"]}
    state["version"] = packet["to_version"]
    state["snapshot"] = packet["to_snapshot"]
    state["facts"] = rows
    state["last_packet"] = packet["fingerprint"]
    state["ack"] = ack
    write_state(Path(store), state)
    return ack


def command_status(store):
    state = _load(store)
    return {"consumer": state["consumer"], "drill": state["drill"],
            "version": state["version"], "snapshot": state["snapshot"]}


def command_facts(store):
    state = _load(store)
    return {"consumer": state["consumer"], "drill": state["drill"],
            "version": state["version"], "snapshot": state["snapshot"],
            "facts": state["facts"]}


def command_ack(store):
    state = _load(store)
    if state["ack"] is None:
        raise Rejected("no acknowledgment recorded")
    return state["ack"]


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected(f"usage: {message}")


def _parser():
    parser = _Parser(prog="python -m tank_transfer.replica")
    parser.add_argument("--store", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--consumer", required=True)
    init.add_argument("--snapshot", required=True)

    apply = sub.add_parser("apply")
    apply.add_argument("--packet", required=True)

    sub.add_parser("status")
    sub.add_parser("facts")
    sub.add_parser("ack")
    return parser


def _run(args):
    if args.command == "init":
        return command_init(args.store, args.consumer, args.snapshot)
    if args.command == "apply":
        return command_apply(args.store, args.packet)
    if args.command == "status":
        return command_status(args.store)
    if args.command == "facts":
        return command_facts(args.store)
    if args.command == "ack":
        return command_ack(args.store)
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
