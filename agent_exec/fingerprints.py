"""Input identity: two execution identities and component fingerprints.

Implements the identity half of the plan's Design C ("Two identities, not a
vague similarity score"):

- an **execution key** identifies exactly reusable work. The variant is part
  of the key, so pool/isolation changes are DIFFERENT executions;
- a **recovery goal key** groups related attempts. It deliberately excludes
  wrapper/presentation fields, so log filters, deadline bumps and reporter
  format changes neither create new work nor escape a recovery budget;
- **component fingerprints** hash the actual bytes of the relevant inputs
  (tracked and untracked), skip declared outputs/excludes and ``.git``, and
  carry a per-file mtime+size+hash snapshot so pre/post comparison can detect
  concurrent edits during a job.

Side-effect free by contract: hashing and filesystem reads only; nothing here
writes, spawns processes or queries services. Unknown or unreadable inputs
produce an explicit ``"unknown"`` result, never a guessed hash.
"""

from __future__ import annotations

import hashlib
import json
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Optional, Sequence

UNKNOWN = "unknown"
EXECUTION_KEY_VERSION = "execution-key-v1"
GOAL_KEY_VERSION = "recovery-goal-key-v1"
FAMILY_KEY_VERSION = "recovery-family-key-v1"
COMPONENT_FINGERPRINT_VERSION = "component-fingerprint-v1"


class FingerprintError(ValueError):
    """An identity request is malformed, or an input cannot be verified."""


def _canonical_hash(domain: str, payload: dict) -> str:
    canonical = json.dumps(
        {"domain": domain, "fields": payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_text(value: object, what: str) -> str:
    if not isinstance(value, str) or value == "":
        raise FingerprintError(f"{what} must be a non-empty string")
    if value == UNKNOWN:
        raise FingerprintError(
            f"{what} is unknown; an unknown input must never become a "
            "guessed identity (handle it as an explicit abstention)")
    return value


def _require_text_known_or_unknown(value: object, what: str) -> str:
    """Accept UNKNOWN explicitly (grouping keys), never empty or guessed."""
    if not isinstance(value, str) or value == "":
        raise FingerprintError(f"{what} must be a non-empty string")
    return value


def _require_tokens(values: Iterable[str], what: str) -> tuple:
    tokens = tuple(values)
    for token in tokens:
        if not isinstance(token, str) or token == "":
            raise FingerprintError(f"{what} must contain non-empty strings")
    return tokens


def known_fingerprint(value: Optional[str]) -> bool:
    """True only for a verified, non-placeholder fingerprint value."""
    return isinstance(value, str) and value != "" and value != UNKNOWN


def execution_key(
    project: str,
    component: str,
    operation: str,
    variant: str,
    source_fingerprint: str,
    environment_fingerprint: str,
    selectors: Sequence[str] = (),
    semantic_flags: Sequence[str] = (),
    test_seed: Optional[str] = None,
) -> str:
    """Exact-work identity: everything that changes what a run executes.

    Pool/isolation differences belong in ``variant``; a different variant is a
    different execution even when it pursues the same recovery goal. Selector
    order is preserved (exact selected scope/order); semantic flags are a set.
    """
    return _canonical_hash(EXECUTION_KEY_VERSION, {
        "project": _require_text(project, "project"),
        "component": _require_text(component, "component"),
        "operation": _require_text(operation, "operation"),
        "variant": _require_text(variant, "variant"),
        "source_fingerprint": _require_text(source_fingerprint, "source_fingerprint"),
        "environment_fingerprint": _require_text(
            environment_fingerprint, "environment_fingerprint"),
        "selectors": _require_tokens(selectors, "selectors"),
        "semantic_flags": sorted(_require_tokens(semantic_flags, "semantic_flags")),
        "test_seed": test_seed,
    })


def recovery_goal_key(
    project: str,
    component: str,
    operation_family: str,
    input_generation: str,
    environment_class: str,
    intended_scope: str,
) -> str:
    """Related-attempt identity for recovery budgeting.

    ``input_generation`` is the *verified relevant-input* generation, not the
    whole-tree hash: wrapper/presentation changes (log filters, deadline
    bumps, reporter format) touch neither field and therefore never change
    the goal. A different intended scope (focused vs full, Chromium vs
    WebKit) is a different requirement with its own lineage.
    """
    return _canonical_hash(GOAL_KEY_VERSION, {
        "project": _require_text(project, "project"),
        "component": _require_text(component, "component"),
        "operation_family": _require_text(operation_family, "operation_family"),
        "input_generation": _require_text_known_or_unknown(
            input_generation, "input_generation"),
        "environment_class": _require_text(environment_class, "environment_class"),
        "intended_scope": _require_text(intended_scope, "intended_scope"),
    })


def recovery_family_key(
    project: str,
    component: str,
    operation_family: str,
    input_generation: str,
    environment_class: str,
) -> str:
    """Goal key minus scope: the budget neighborhood for validated lineages.

    Timeouts of focused diagnostics propagate to the family so that a second
    timeout in the same validated lineage exhausts the unchanged-goal budget
    regardless of selected-file changes, while a genuinely different
    environment class (e.g. another browser) keeps its own lineage.
    """
    return _canonical_hash(FAMILY_KEY_VERSION, {
        "project": _require_text(project, "project"),
        "component": _require_text(component, "component"),
        "operation_family": _require_text(operation_family, "operation_family"),
        "input_generation": _require_text_known_or_unknown(
            input_generation, "input_generation"),
        "environment_class": _require_text(environment_class, "environment_class"),
    })


def _matches(rel: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch(rel, pattern) for pattern in patterns)


def unknown_fingerprint_result(reason: str) -> dict:
    """The explicit unknown result. Never a guessed hash."""
    return {"status": "unknown", "reason": reason, "sha256": UNKNOWN,
            "files": 0, "bytes": 0, "entries": {}}


def component_fingerprint(
    root: Path,
    include_globs: Sequence[str],
    exclude_globs: Sequence[str] = (),
) -> dict:
    """Hash the actual bytes of relevant inputs under ``root``.

    Walks the working tree (tracked and untracked alike), matches relative
    POSIX paths against ``include_globs``, prunes ``.git`` and
    ``exclude_globs``, and returns ``{status, reason, sha256, files, bytes,
    entries}`` where ``entries`` maps each file to its
    ``{sha256, size, mtime_ns}`` snapshot. Missing root or any unreadable
    matched file yields ``status="unknown"`` with an explicit reason; the
    caller must then treat input identity as unknown (advisory only).
    """
    if not include_globs:
        raise FingerprintError("include_globs must be non-empty")
    root = Path(root)
    if not root.is_dir():
        return unknown_fingerprint_result("root_missing")
    excludes = tuple(exclude_globs)
    entries: dict[str, dict] = {}
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d != ".git"
            and not _matches(_rel(root, dirpath, d), excludes)
            and not _matches(d, excludes))
        for name in sorted(filenames):
            rel = _rel(root, dirpath, name)
            if _matches(rel, excludes) or _matches(name, excludes):
                continue
            if not _matches(rel, include_globs):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
                mtime_ns = os.stat(path).st_mtime_ns
            except OSError as exc:
                return unknown_fingerprint_result(f"unreadable:{rel}:{exc.errno}")
            digest = hashlib.sha256(data).hexdigest()
            entries[rel] = {"sha256": digest, "size": len(data),
                            "mtime_ns": mtime_ns}
            total_bytes += len(data)
    ordered = {rel: entries[rel] for rel in sorted(entries)}
    # The aggregate identity covers actual bytes (and sizes) only; per-file
    # mtimes stay in the snapshot for concurrent-edit detection and must not
    # change input identity by themselves.
    aggregate = _canonical_hash(
        COMPONENT_FINGERPRINT_VERSION,
        {rel: {"sha256": e["sha256"], "size": e["size"]}
         for rel, e in ordered.items()})
    return {"status": "ok", "reason": None, "sha256": aggregate,
            "files": len(ordered), "bytes": total_bytes, "entries": ordered}


def _rel(root: Path, dirpath: str, name: str) -> str:
    base = os.path.relpath(os.path.join(dirpath, name), root)
    return base.replace(os.sep, "/")


def snapshot_known(snapshot: dict) -> bool:
    return isinstance(snapshot, dict) and snapshot.get("status") == "ok"


def snapshot_changed(before: dict, after: dict) -> bool:
    """Conservative pre/post compare (mtime + size + hash per file).

    Unknown or missing snapshots count as changed: unverifiable state must
    look stale, never silently fresh.
    """
    if not (snapshot_known(before) and snapshot_known(after)):
        return True
    return before["entries"] != after["entries"]


def concurrent_edit_detected(before: dict, after: dict) -> bool:
    """True when inputs changed between a job's pre and post snapshots."""
    return snapshot_changed(before, after)
