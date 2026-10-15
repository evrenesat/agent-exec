"""Pure operation-profile contracts: validation and command rendering.

This module is side-effect free by contract: nothing here installs packages,
spawns processes, creates files or queries services. Rendering is a pure
function of (profile, selectors, binding). The job layer (checkpoint 2) is the
only component allowed to execute rendered commands.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
ADMISSION_STATES = ("draft", "fixture_verified", "project_verified",
                    "unsupported_environment")
PURPOSES = ("verify", "diagnose", "confirm_flake")
CAPABILITY_KEYS = ("supports_coalescing", "supports_reuse",
                   "supports_incremental_report", "requires_external_state",
                   "needs_private_host")
SELECTOR_KINDS = ("file", "directory")
PROFILE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{2,63}")


class ProfileError(ValueError):
    """A profile is invalid, or a render request violates its contract."""


@dataclass(frozen=True)
class Selector:
    kind: str
    value: str


@dataclass(frozen=True)
class RenderedCommand:
    profile_id: str
    operation_id: str
    argv: tuple[str, ...]
    cwd: str
    reports: tuple[dict, ...]
    deadlines: dict
    variant: str
    purpose: str
    notes: tuple[str, ...] = field(default=())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProfileError(message)


def _require_keys(data: dict, keys: tuple[str, ...], where: str) -> None:
    for key in keys:
        _require(key in data, f"{where}: missing required key '{key}'")


def validate_profile(data: Any) -> dict:
    """Validate one raw profile dictionary; return it unchanged on success."""
    _require(isinstance(data, dict), "profile must be a JSON object")
    _require_keys(data, ("schema_version", "profile_id", "project", "component",
                         "operation", "status", "host", "command", "selectors",
                         "deadlines", "capabilities", "inputs"), "profile")
    _require(data["schema_version"] == SCHEMA_VERSION,
             f"unsupported schema_version: {data.get('schema_version')!r}")
    _require(bool(PROFILE_ID_PATTERN.fullmatch(str(data["profile_id"]))),
             f"invalid profile_id: {data.get('profile_id')!r}")
    _require(data["status"] in ADMISSION_STATES,
             f"invalid admission status: {data.get('status')!r}")
    _require(isinstance(data["host"], dict) and "hostname" in data["host"],
             "host must name an execution hostname")
    _require(isinstance(data["command"], dict), "command must be an object")
    command = data["command"]
    _require_keys(command, ("argv_template", "cwd"), "command")
    argv_template = command["argv_template"]
    _require(isinstance(argv_template, list) and argv_template
             and all(isinstance(part, str) for part in argv_template),
             "command.argv_template must be a non-empty list of strings")
    _require(all("{selectors}" not in part for part in argv_template),
             "selectors are appended as separate argv elements, never interpolated "
             "into a single argument")
    _require(isinstance(command["cwd"], str)
             and (command["cwd"].startswith("/")
                  or command["cwd"].startswith("{source_root}")),
             "command.cwd must be an absolute path template")
    selectors = data["selectors"]
    _require(isinstance(selectors, dict), "selectors must be an object")
    _require_keys(selectors, ("allowed_kinds", "base"), "selectors")
    for kind in selectors["allowed_kinds"]:
        _require(kind in SELECTOR_KINDS, f"unsupported selector kind: {kind!r}")
    deadlines = data["deadlines"]
    _require(isinstance(deadlines, dict), "deadlines must be an object")
    _require_keys(deadlines, ("diagnose_seconds", "hard_seconds"), "deadlines")
    for key in ("diagnose_seconds", "hard_seconds"):
        value = deadlines[key]
        _require(isinstance(value, int) and value > 0,
                 f"deadlines.{key} must be a positive integer")
    _require(deadlines["diagnose_seconds"] <= deadlines["hard_seconds"],
             "diagnose deadline cannot exceed the hard deadline")
    capabilities = data["capabilities"]
    _require(isinstance(capabilities, dict), "capabilities must be an object")
    for key in CAPABILITY_KEYS:
        entry = capabilities.get(key, False)
        if isinstance(entry, dict):
            _require("enabled" in entry and "reason" in entry,
                     f"capabilities.{key} needs 'enabled' and 'reason'")
            _require(isinstance(entry["enabled"], bool),
                     f"capabilities.{key}.enabled must be boolean")
        else:
            _require(entry is False,
                     f"capabilities.{key} must be false or an explicit object "
                     "(missing capability defaults to disabled)")
    inputs = data["inputs"]
    _require(isinstance(inputs, dict), "inputs must be an object")
    for key in ("fingerprints", "generated"):
        _require(isinstance(inputs.get(key, []), list), f"inputs.{key} must be a list")
    preparation = data.get("preparation", [])
    _require(isinstance(preparation, list), "preparation must be a list")
    for stage in preparation:
        _require(isinstance(stage, dict), "preparation stages must be objects")
        _require_keys(stage, ("kind", "script"), "preparation stage")
    for key in ("reports", "isolation", "notes"):
        _require(key not in data or isinstance(data[key], (list, dict)),
                 f"{key} must be structured")
    if "variant_defaults" in data:
        _require(isinstance(data["variant_defaults"], dict),
                 "variant_defaults must be an object")
    return data


def capability_enabled(profile: dict, key: str) -> bool:
    """Missing capabilities default to disabled, never inferred from language."""
    entry = profile["capabilities"].get(key, False)
    if isinstance(entry, dict):
        return bool(entry["enabled"])
    return bool(entry)


def _safe_relative(value: str, base: str, what: str) -> str:
    _require(isinstance(value, str) and value != "", f"{what} must be a non-empty string")
    _require(not value.startswith("/"), f"{what} must be relative to the source root: {value!r}")
    normalized = posixpath.normpath(value)
    _require(not normalized.startswith("..") and normalized not in (".", ""),
             f"{what} escapes the source root: {value!r}")
    _require(normalized == value,
             f"{what} is not normalized: {value!r}")
    _require(base == "" or normalized == base or normalized.startswith(base + "/"),
             f"{what} is outside the allowed selector base '{base}': {value!r}")
    return normalized


def parse_selectors(profile: dict, raw: list[str] | None) -> tuple[Selector, ...]:
    """Validate selector strings against the profile; empty means full scope."""
    allowed = set(profile["selectors"]["allowed_kinds"])
    base = profile["selectors"].get("base", "")
    if raw is None or raw == []:
        return ()
    parsed = []
    for item in raw:
        if item.startswith("dir:"):
            kind, value = "directory", item[4:]
        elif item.startswith("file:"):
            kind, value = "file", item[5:]
        else:
            # Default kind is the first allowed kind; explicit prefixes win.
            kind = profile["selectors"]["allowed_kinds"][0]
            value = item
        _require(kind in allowed, f"selector kind '{kind}' not allowed by profile")
        value = _safe_relative(value, base, f"selector '{item}'")
        _require(not value.endswith("/"), f"selector must not end with '/': {item!r}")
        parsed.append(Selector(kind, value))
    return tuple(parsed)


def render(profile: dict, *, source_root: str, job_id: str, report_dir: str,
           selectors: list[str] | None = None, purpose: str = "verify",
           variant: str = "default") -> RenderedCommand:
    """Render the exact argv/cwd/reports for one job. Pure; executes nothing."""
    validate_profile(profile)
    _require(purpose in PURPOSES, f"unknown purpose: {purpose!r}")
    _require(source_root.startswith("/") and posixpath.normpath(source_root) == source_root,
             "source_root must be a normalized absolute path")
    _require(bool(job_id) and all(c.isalnum() or c in "-_" for c in job_id),
             "job_id must be non-empty and filename-safe")
    _require(report_dir.startswith("/") and not report_dir.startswith(source_root + "/")
             and posixpath.normpath(report_dir) != source_root,
             "report_dir must be outside the source tree")
    command = profile["command"]
    cwd = command["cwd"].format(source_root=source_root)
    _require(cwd.startswith(source_root), "command.cwd must stay inside the source root")
    parsed = parse_selectors(profile, selectors)
    variant_rules = profile.get("variant_defaults", {}).get(variant)
    _require(variant == "default" or isinstance(variant_rules, dict),
             f"unknown variant: {variant!r}")
    argv: list[str] = [part.format(source_root=source_root, report_dir=report_dir,
                                   job_id=job_id) for part in command["argv_template"]]
    _require(all("{selectors}" not in part for part in argv),
             "selector placeholder must not survive template expansion")
    for selector in parsed:
        argv.append(selector.value)
    reports = []
    for report in profile.get("reports", []):
        path = report["path_template"].format(report_dir=report_dir, job_id=job_id)
        normalized = posixpath.normpath(path)
        _require(normalized.startswith(report_dir + "/"),
                 "report paths must stay inside the job report dir")
        reports.append({"kind": report["kind"], "path": normalized,
                        "admitted": bool(report.get("admitted", False))})
    notes = []
    if profile["status"] in ("draft", "unsupported_environment"):
        notes.append(f"profile admission status is '{profile['status']}'; "
                     "launching scored jobs is refused by the job layer")
    if any(not report["admitted"] for report in reports):
        notes.append("reporter combination is not yet admitted; calibration required")
    return RenderedCommand(
        profile_id=profile["profile_id"], operation_id=(
            f"{profile['project']}.{profile['component']}.{profile['operation']}"),
        argv=tuple(argv), cwd=cwd, reports=tuple(reports),
        deadlines=dict(profile["deadlines"]), variant=variant, purpose=purpose,
        notes=tuple(notes))
