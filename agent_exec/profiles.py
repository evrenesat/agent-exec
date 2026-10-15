"""Operation profile loading and the read-only profiles CLI.

`python3 -m agent_exec.profiles --check <dir>` validates every
profile JSON file. `--render` prints the rendered argv/cwd for a binding.
Neither mode installs anything, spawns a test, creates a profile or queries
services: they are read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from .contracts import ProfileError, render, validate_profile

ADMITTED_STATES = ("fixture_verified", "project_verified")
REPORT_DIR_PATTERN = re.compile(r"/[^\s'\"]*/reports/")
REQUIRED_ADMISSION_REPEATS = 3


def _normalize_argv(argv) -> list[str]:
    """Replace concrete per-job report dirs with a sentinel for comparison."""
    return [REPORT_DIR_PATTERN.sub("/REPORTS/", str(part)) for part in argv]


def verify_admission(profile: dict, *, scored: bool,
                     profile_path: Path | None = None) -> list[str]:
    """Return admission violations (empty list = usable as requested).

    Scored operations require an admitted profile whose receipt is intact:
    it must record a pass, at least three runs, belong to this profile id,
    and bind this profile's identity — by the recorded profile_sha256 when
    present, otherwise by matching the normalized argv each recorded run
    executed against what the current profile renders. A changed command
    therefore invalidates the receipt. Draft profiles are always violations
    for scored operations; unscored calibration/diagnostic operations may
    run drafts.
    """
    errors: list[str] = []
    status = profile.get("status")
    if status not in ADMITTED_STATES:
        if scored:
            errors.append(f"profile status is '{status}'; scored operations "
                          "require fixture_verified or project_verified")
        return errors
    receipt_path = profile.get("admission_receipt")
    if not receipt_path:
        return [f"admitted profile {profile['profile_id']} lacks admission_receipt"]
    receipt_file = Path(receipt_path)
    if not receipt_file.is_file():
        errors.append(f"admission receipt missing: {receipt_path}")
        return errors
    try:
        receipt = json.loads(receipt_file.read_text())
    except json.JSONDecodeError as error:
        return [f"admission receipt invalid JSON: {error}"]
    if receipt.get("passed") is not True:
        errors.append("admission receipt does not record a pass")
    runs = receipt.get("runs") or []
    if len(runs) < REQUIRED_ADMISSION_REPEATS or \
            receipt.get("repeats", len(runs)) < REQUIRED_ADMISSION_REPEATS:
        errors.append(f"admission receipt has insufficient repeats "
                      f"({len(runs)} < {REQUIRED_ADMISSION_REPEATS})")
    if receipt.get("admission") != profile["profile_id"]:
        errors.append("admission receipt belongs to a different profile id")
    recorded_sha = receipt.get("profile_sha256")
    if recorded_sha:
        if profile_path is not None and profile_path.is_file():
            actual = hashlib.sha256(profile_path.read_bytes()).hexdigest()
            if actual != recorded_sha:
                errors.append("profile changed since admission (sha256 "
                              "mismatch); re-admission required")
        else:
            errors.append("receipt records profile_sha256 but no profile "
                          "file was provided to verify against")
    elif runs and runs[0].get("argv") and receipt.get("source_root"):
        # Legacy binding: re-render with the receipt's own source root and
        # compare normalized argv. The executor binary may resolve differently
        # (absolute vs PATH) but every subsequent argument must match.
        src = receipt["source_root"]
        try:
            rendered = render(profile, source_root=src, job_id="verify",
                              report_dir="/ADMITEVT/jobs/verify/reports")
        except ProfileError as error:
            errors.append("profile no longer renders against its admission "
                          "source root: " + str(error))
            return errors
        expected = _normalize_argv(rendered.argv)
        recorded = _normalize_argv(runs[0]["argv"])
        same = expected == recorded or (
            [Path(e).name for e in expected] == [Path(r).name for r in recorded])
        if not same:
            errors.append("recorded admission argv differs from the current "
                          "profile render; re-admission required")
    # Receipts with neither profile_sha256 nor recorded argv cannot prove
    # identity across profile edits; that is a documented verification gap
    # for pre-binding receipts only. New admissions always record the sha.
    return errors


def load_profile(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ProfileError(f"{path}: invalid JSON: {error}") from error
    return validate_profile(data)


def load_profile_dir(directory: Path) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        profile = load_profile(path)
        profile_id = profile["profile_id"]
        if profile_id in profiles:
            raise ProfileError(f"duplicate profile_id {profile_id!r} in {path}")
        profiles[profile_id] = profile
    return profiles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_exec.profiles",
        description="validate/render operation profiles (read-only)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", metavar="DIR", type=Path,
                      help="validate all profiles in a directory")
    mode.add_argument("--render", metavar="PROFILE_ID",
                      help="render one profile's command (prints JSON)")
    parser.add_argument("profiles_dir", type=Path, nargs="?", default=None,
                        help="profile directory (required for --render)")
    parser.add_argument("--source-root")
    parser.add_argument("--job-id", default="render-preview")
    parser.add_argument("--report-dir")
    parser.add_argument("--selectors", nargs="*", default=[])
    parser.add_argument("--purpose", default="verify")
    args = parser.parse_args(argv)
    try:
        if args.check is not None:
            profiles = load_profile_dir(args.check)
            print(json.dumps({"checked": sorted(profiles),
                              "statuses": {pid: p["status"]
                                           for pid, p in profiles.items()}}, indent=2))
            return 0
        missing = [name for name in ("profiles_dir", "source_root", "report_dir")
                   if getattr(args, name) is None]
        if missing:
            parser.error(f"--render requires: {', '.join(missing)}")
        profiles = load_profile_dir(args.profiles_dir)
        profile = profiles[args.render]
        rendered = render(profile, source_root=args.source_root, job_id=args.job_id,
                          report_dir=args.report_dir, selectors=args.selectors or None,
                          purpose=args.purpose)
        print(json.dumps({
            "operation_id": rendered.operation_id, "argv": list(rendered.argv),
            "cwd": rendered.cwd, "reports": [dict(r) for r in rendered.reports],
            "deadlines": rendered.deadlines, "purpose": rendered.purpose,
            "variant": rendered.variant, "notes": list(rendered.notes),
        }, indent=2))
        return 0
    except (ProfileError, KeyError) as error:
        print(f"profile error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
