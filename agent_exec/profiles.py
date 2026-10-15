"""Operation profile loading and the read-only profiles CLI.

`python3 -m agent_exec.profiles --check <dir>` validates every
profile JSON file. `--render` prints the rendered argv/cwd for a binding.
Neither mode installs anything, spawns a test, creates a profile or queries
services: they are read-only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import ProfileError, render, validate_profile


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
