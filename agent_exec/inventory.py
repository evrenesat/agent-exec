"""Read-only repository inventory for profile admission.

Scans only the explicit source roots named below, hashing manifests, CI files
and nearest instruction files, and probes interpreter/package-manager versions.
Never mutates a repository, installs anything or contacts services. Output goes
to stdout or an explicit --output path under the evidence root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

SOURCE_ROOTS = {
    "aflow": "/root/code/agent_flow",
    "asky": "/root/code/asky",
    "audioventura": "/root/code/audioventura",
    "doublangu": "/root/code/doublangu",
    "cdx": "/root/code/cdx",
    "nosleeptillodone": "/root/code/nosleeptillodone",
    "iassist": "/root/code/iassist",
}
PER_ROOT_FILES = [
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Makefile",
    "AGENTS.md", "README.md", "ARCHITECTURE.md",
]
NESTED_FILES = {
    "aflow": ["apps/aflow_app/web/package.json", "apps/aflow_app/web/package-lock.json",
              "apps/aflow_app/web/vite.config.ts", ".github/workflows/ci.yml"],
    "doublangu": ["web/package.json", "web/package-lock.json"],
    "cdx": ["go.sum"],
}
VERSION_PROBES = {
    "python3": ["python3", "--version"],
    "node": ["/opt/node-v24.20.0-linux-x64/bin/node", "--version"],
    "npm": ["npm", "--version"],
    "go": ["go", "version"],
    "uv": ["uv", "--version"],
    "git": ["git", "--version"],
}


def sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def inventory_root(name: str, root: str) -> dict:
    root_path = Path(root)
    record: dict = {"name": name, "root": root, "exists": root_path.is_dir()}
    if not record["exists"]:
        return record
    try:
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        record["git_head"] = head.stdout.strip() if head.returncode == 0 else None
        status = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        record["dirty_entries"] = (len(status.stdout.splitlines())
                                   if status.returncode == 0 else None)
    except (OSError, subprocess.TimeoutExpired):
        record["git_head"] = None
    files = {}
    for rel in PER_ROOT_FILES + NESTED_FILES.get(name, []):
        path = root_path / rel
        if path.is_file():
            digest = sha256(path)
            if digest:
                files[rel] = {"bytes": path.stat().st_size, "sha256": digest}
    record["files"] = files
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None,
                        help="write JSON here (must not exist); default stdout")
    args = parser.parse_args()
    versions = {}
    for name, argv in VERSION_PROBES.items():
        executable = shutil.which(argv[0]) or (argv[0] if Path(argv[0]).is_file() else None)
        if executable is None:
            versions[name] = None
            continue
        try:
            out = subprocess.run([argv[0]] + argv[1:], capture_output=True, text=True, timeout=15)
            versions[name] = (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            versions[name] = None
    report = {
        "schema_version": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hostname": subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip(),
        "tool_versions": versions,
        "roots": [inventory_root(name, root) for name, root in SOURCE_ROOTS.items()],
        "notes": [
            "read-only: git rev-parse/status and file hashes only; no mutation, install or service query",
            "hashes are of actual working-tree bytes; admission must re-verify at job time",
            "iassist is reference-only and requires macOS execution; it is inventoried, never launched",
        ],
    }
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        if args.output.exists():
            print(f"refusing to overwrite existing output: {args.output}", file=sys.stderr)
            return 2
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
        print(str(args.output))
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
