"""Offline sample publication catalog for the multi-tank transfer model.

The catalog is a single-machine, single-writer store.  A whole committed state
(head revision, immutable revision receipts and all issued receipts) lives in
one atomically replaced file, so adopting a correction, recording its receipt
and revoking affected sample receipts become visible together.

All business values are exact fraction/integer strings.  There are no external
services and no floating-point tolerances.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

from .checks import ident
from .io import load_bounded
from .sources import (MAX_RECEIPTS, MAX_SOURCE_BYTES, MAX_VERSIONS,
                      build_plan, evidence_signature)

STATE_NAME = "state.json"
FORMAT_VERSION = 1


class Rejected(Exception):
    """Business rejection: exit 2, reason on stderr, no state change."""


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _now_revision_or_none(state):
    return state["head"]


def _crashpoint(name):
    if os.environ.get("TANK_TRANSFER_CRASH_POINT") == name:
        os.kill(os.getpid(), signal.SIGKILL)


def _fsync_directory(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_state(store, state):
    """Durably replace the committed state, with test crash injection points.

    ``before-commit`` is hit after the new state has been staged and synced
    but before it becomes visible.  ``after-commit`` is hit once the rename is
    durable but before a receipt can be printed by the CLI.
    """
    payload = (canonical_json(state) + "\n").encode("utf-8")
    target = store / STATE_NAME
    # Best-effort hygiene for staging files left behind when a previous writer
    # was killed before its rename; they were never visible as committed state.
    for stale in store.glob(".state-*.tmp"):
        try:
            stale.unlink()
        except OSError:
            pass
    fd, temporary = tempfile.mkstemp(dir=store, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _crashpoint("before-commit")
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(store)
        _crashpoint("after-commit")
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _create_store(store, state):
    """Publish a brand-new store directory atomically.

    The state is fully written and synced in a sibling staging directory, then
    the directory is renamed into place, so a terminated init never leaves a
    partially initialized store that would block a retry.
    """
    parent = store.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Rejected(f"cannot create store: {exc}")
    staging = Path(tempfile.mkdtemp(dir=parent, prefix=".store-"))
    try:
        payload = (canonical_json(state) + "\n").encode("utf-8")
        with open(staging / STATE_NAME, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(staging)
        os.rename(staging, store)
        staging = None
        _fsync_directory(parent)
    except OSError as exc:
        raise Rejected(f"cannot create store: {exc}")
    finally:
        if staging is not None:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)


def _load_state(store):
    path = Path(store) / STATE_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise Rejected("store is not initialized")
    except OSError as exc:
        raise Rejected(f"cannot read store: {exc}")
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Rejected(f"store state is corrupt: {exc}")
    if not isinstance(state, dict) or state.get("format") != FORMAT_VERSION:
        raise Rejected("store state is corrupt")
    return state


def _head_plan(state):
    record = state["revisions"][state["head"]]
    try:
        data = json.loads(record["source"])
    except json.JSONDecodeError as exc:  # defensive: stored state is trusted
        raise Rejected(f"stored plan is corrupt: {exc}")
    return build_plan(data, state["tanks"], state["species"])


def _plan_from_file(path, expected_tanks=None, expected_species=None):
    try:
        data = load_bounded(path, MAX_SOURCE_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected(f"invalid source: {exc}")
    try:
        return data, build_plan(data, expected_tanks, expected_species)
    except (ValueError, RecursionError) as exc:
        raise Rejected(f"invalid plan: {exc}")


def _selector(kind, step, item):
    if kind not in ("closing", "transfer"):
        raise Rejected("kind must be closing or transfer")
    try:
        ident(step)
        ident(item)
    except ValueError as exc:
        raise Rejected(str(exc))
    return {"kind": kind, "step": step, "item": item}


def _sample(plan, selector):
    try:
        status, evidence = plan.sample(selector["kind"], selector["step"],
                                       selector["item"])
    except ValueError as exc:
        raise Rejected(str(exc))
    return status, evidence


def _receipt_view(record, archive=False):
    view = {
        "receipt": record["receipt"],
        "issued_at": record["issued_at"],
        "selector": dict(record["selector"]),
        "evidence": record["evidence"],
        "revocation": record["revocation"],
    }
    if not archive and record["revocation"] is not None:
        raise Rejected("receipt has been revoked")
    return view


def command_init(store, revision, source):
    try:
        ident(revision)
    except ValueError as exc:
        raise Rejected(str(exc))
    path = Path(store)
    data, plan = _plan_from_file(source)
    try:
        if path.exists() or path.is_symlink():
            raise Rejected("store path already exists")
    except OSError as exc:
        raise Rejected(f"cannot inspect store path: {exc}")
    state = {
        "format": FORMAT_VERSION,
        "initial_revision": revision,
        "head": revision,
        "tanks": plan.tank_ids,
        "species": plan.species,
        "revisions": {
            revision: {
                "base": None,
                "source": canonical_json(data),
            }
        },
        "receipts": {},
    }
    _create_store(path, state)
    return {"revision": revision}


def command_head(store):
    state = _load_state(store)
    return {"revision": _now_revision_or_none(state)}


def command_issue(store, receipt_id, kind, step, item):
    try:
        ident(receipt_id)
    except ValueError as exc:
        raise Rejected(str(exc))
    state = _load_state(store)
    if receipt_id in state["receipts"]:
        raise Rejected("receipt id already used")
    if len(state["receipts"]) >= MAX_RECEIPTS:
        raise Rejected("receipt limit reached")
    selector = _selector(kind, step, item)
    plan = _head_plan(state)
    status, evidence = _sample(plan, selector)
    if status == "missing":
        raise Rejected("sample selector is missing")
    if status == "empty":
        raise Rejected("sample selector is empty")
    record = {
        "receipt": receipt_id,
        "issued_at": state["head"],
        "selector": selector,
        "evidence": evidence,
        "revocation": None,
    }
    state["receipts"][receipt_id] = record
    _write_state(Path(store), state)
    return _receipt_view(record)


def command_read(store, receipt_id, archive):
    try:
        ident(receipt_id)
    except ValueError as exc:
        raise Rejected(str(exc))
    state = _load_state(store)
    record = state["receipts"].get(receipt_id)
    if record is None:
        raise Rejected("unknown receipt")
    return _receipt_view(record, archive=archive)


def command_revise(store, revision, base, source):
    try:
        ident(revision)
    except ValueError as exc:
        raise Rejected(str(exc))
    state = _load_state(store)

    # The source file is always re-read and syntax/size checked first, even on
    # a retry of an already committed correction.
    try:
        data = load_bounded(source, MAX_SOURCE_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected(f"invalid source: {exc}")
    canonical_source = canonical_json(data)

    revisions = state["revisions"]
    if revision in revisions:
        if revision == state["initial_revision"]:
            raise Rejected("initial revision id cannot be a correction id")
        record = revisions[revision]
        if record.get("base") != base or record.get("source") != canonical_source:
            raise Rejected("revision id was already used with different content")
        # Idempotent retry: return the original receipt without comparing
        # against the current plan and without occupying a new version slot.
        return record["receipt"]

    if base != state["head"]:
        raise Rejected("base is not the current revision")
    if len(revisions) >= MAX_VERSIONS:
        raise Rejected("revision limit reached")

    try:
        plan = build_plan(data, state["tanks"], state["species"])
    except (ValueError, RecursionError) as exc:
        raise Rejected(f"invalid plan: {exc}")

    revoked = []
    for receipt_id in sorted(state["receipts"]):
        record = state["receipts"][receipt_id]
        if record["revocation"] is not None:
            continue
        status, evidence = _sample(plan, record["selector"])
        if status == "missing":
            reason = "missing"
        elif status == "empty":
            reason = "empty"
        elif evidence_signature(evidence) != evidence_signature(record["evidence"]):
            reason = "changed"
        else:
            continue
        revoked.append((receipt_id, reason))

    receipt = {
        "revision": revision,
        "base": base,
        "revoked": [{"receipt": receipt_id, "reason": reason}
                    for receipt_id, reason in revoked],
    }
    revisions[revision] = {
        "base": base,
        "source": canonical_source,
        "receipt": receipt,
    }
    for receipt_id, reason in revoked:
        state["receipts"][receipt_id]["revocation"] = {
            "revision": revision,
            "reason": reason,
        }
    state["head"] = revision
    _write_state(Path(store), state)
    return receipt


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected(f"usage: {message}")


def _parser():
    parser = _Parser(prog="python -m tank_transfer.catalog")
    parser.add_argument("--store", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--revision", required=True)
    init.add_argument("--source", required=True)

    sub.add_parser("head")

    issue = sub.add_parser("issue")
    issue.add_argument("--receipt", required=True)
    issue.add_argument("--kind", required=True, choices=("closing", "transfer"))
    issue.add_argument("--step", required=True)
    issue.add_argument("--item", required=True)

    read = sub.add_parser("read")
    read.add_argument("--receipt", required=True)
    read.add_argument("--archive", action="store_true")

    revise = sub.add_parser("revise")
    revise.add_argument("--revision", required=True)
    revise.add_argument("--base", required=True)
    revise.add_argument("--source", required=True)
    return parser


def _run(args):
    if args.command == "init":
        return command_init(args.store, args.revision, args.source)
    if args.command == "head":
        return command_head(args.store)
    if args.command == "issue":
        return command_issue(args.store, args.receipt, args.kind, args.step,
                             args.item)
    if args.command == "read":
        return command_read(args.store, args.receipt, args.archive)
    if args.command == "revise":
        return command_revise(args.store, args.revision, args.base,
                              args.source)
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
