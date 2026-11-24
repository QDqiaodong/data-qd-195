"""Atomic single-file state journals with test crash-injection points.

Each store keeps its whole committed state in one ``state.json`` file.  A
commit stages the new payload in a sibling temporary file, fsyncs it, then
atomically renames it over the target and fsyncs the directory, so a process
killed before the rename leaves the complete old state and a process killed
after it leaves the complete new state.  Only local filesystems and serial
callers per store are supported.
"""

import json
import os
from pathlib import Path
import shutil
import signal
import tempfile

from .facts import canonical_json

STATE_NAME = "state.json"


class StoreError(Exception):
    """The store is missing, unreadable or corrupt."""


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


def write_state(store, state):
    """Durably replace the committed state file.

    ``before-commit`` is hit after the new state has been staged and synced
    but before it becomes visible.  ``after-commit`` is hit once the rename
    is durable but before the caller can receive a result.
    """
    payload = (canonical_json(state) + "\n").encode("utf-8")
    target = store / STATE_NAME
    # Best-effort hygiene for staging files left behind when a previous
    # writer was killed before its rename; they were never visible.
    for stale in store.glob(".state-*.tmp"):
        try:
            stale.unlink()
        except OSError:
            pass
    fd, temporary = tempfile.mkstemp(dir=store, prefix=".state-",
                                     suffix=".tmp")
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


def create_store(store, state):
    """Publish a brand-new store directory atomically.

    The state is fully written and synced in a sibling staging directory,
    then the directory is renamed into place, so a terminated init never
    leaves a partially initialized store that would block a retry.
    """
    parent = store.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StoreError(f"cannot create store: {exc}")
    staging = Path(tempfile.mkdtemp(dir=parent, prefix=".store-"))
    try:
        payload = (canonical_json(state) + "\n").encode("utf-8")
        with open(staging / STATE_NAME, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(staging)
        try:
            os.rename(staging, store)
        except OSError as exc:
            raise StoreError(f"cannot create store: {exc}")
        staging = None
        _fsync_directory(parent)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def load_state(store, format_version):
    path = Path(store) / STATE_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise StoreError("store is not initialized")
    except OSError as exc:
        raise StoreError(f"cannot read store: {exc}")
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"store state is corrupt: {exc}")
    if not isinstance(state, dict) or state.get("format") != format_version:
        raise StoreError("store state is corrupt")
    return state
