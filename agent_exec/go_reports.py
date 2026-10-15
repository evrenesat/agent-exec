"""Go test2json event-stream parser (pure; moved here at cdx admission).

Contract (locked by tests/test_go.py, verified against pinned go1.24.13):
- packages with no test files are package-level skips, counted separately,
  never whole-project success;
- failures before stream end are preserved; truncated/non-JSON lines mark the
  stream incomplete (never a pass);
- cached runs replay run events on go1.24.13, so the only cached marker is the
  package summary line containing "(cached)";
- `-count=1` always executes.
"""

from __future__ import annotations

import json

PACKAGE_TERMINAL = {"pass", "fail", "skip"}
TEST_TERMINAL = {"pass", "fail", "skip"}


def parse_go_test_json_stream(text: str) -> dict:
    """Parse a `go test -json` (test2json) stream into a bounded summary.

    Tolerant of interleaved non-JSON lines (tool diagnostics) and truncation:
    both are recorded and mark the stream incomplete. Never raises on stream
    content; the caller supplies the process exit code separately.
    """
    packages: dict[str, dict] = {}

    def pkg(name: str) -> dict:
        if name not in packages:
            packages[name] = {
                "status": None,
                "elapsed": None,
                "cached": False,
                "tests": {},
                "outputs": [],
            }
        return packages[name]

    bad_lines: list[dict] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            bad_lines.append({"line": lineno, "error": str(error)[:120],
                              "text": line[:160]})
            continue
        if not isinstance(event, dict) or "Action" not in event:
            bad_lines.append({"line": lineno, "error": "event without Action",
                              "text": line[:160]})
            continue
        action = event.get("Action")
        if event.get("Test"):
            entry = pkg(event.get("Package", "")).setdefault(
                "tests", {}).setdefault(event["Test"], {})
            if action == "run":
                entry["status"] = "run"
            elif action in TEST_TERMINAL:
                entry["status"] = {"pass": "passed", "fail": "failed",
                                   "skip": "skipped"}[action]
                if "Elapsed" in event:
                    entry["elapsed"] = event["Elapsed"]
            if action == "output":
                entry.setdefault("outputs", []).append(
                    str(event.get("Output", ""))[:500])
        else:
            name = event.get("Package", "")
            entry = pkg(name)
            if action == "output":
                out = str(event.get("Output", ""))
                entry["outputs"].append(out[:500])
                if "(cached)" in out:
                    entry["cached"] = True
            elif action in PACKAGE_TERMINAL:
                entry["status"] = {"pass": "passed", "fail": "failed",
                                   "skip": "skipped"}[action]
                if "Elapsed" in event:
                    entry["elapsed"] = event["Elapsed"]

    counts = {
        "packages_total": len(packages),
        "packages_passed": 0,
        "packages_failed": 0,
        "packages_no_tests": 0,
        "packages_cached": 0,
        "tests_passed": 0,
        "tests_failed": 0,
        "tests_skipped": 0,
        "tests_executed": 0,
    }
    failures: list[dict] = []
    for name, entry in sorted(packages.items()):
        if entry["status"] == "passed":
            counts["packages_passed"] += 1
        elif entry["status"] == "failed":
            counts["packages_failed"] += 1
        elif entry["status"] == "skipped":
            counts["packages_no_tests"] += 1
        if entry["cached"]:
            counts["packages_cached"] += 1
        for test_name, test in sorted(entry["tests"].items()):
            status = test.get("status")
            if status == "passed":
                counts["tests_passed"] += 1
            elif status == "failed":
                counts["tests_failed"] += 1
                failures.append({
                    "package": name, "test": test_name,
                    "output": "".join(test.get("outputs", []))[-500:],
                })
            elif status == "skipped":
                counts["tests_skipped"] += 1
            if status == "run" or status in ("passed", "failed", "skipped"):
                # Any terminal status implies the test binary reported the
                # test; "executed" here means the stream shows a run/pause/
                # terminal cycle for it.
                counts["tests_executed"] += 1
        entry["failure_class"] = None
        if entry["status"] == "failed":
            test_failures = [t for t in entry["tests"].values()
                             if t.get("status") == "failed"]
            entry["failure_class"] = ("test_failure" if test_failures
                                      else "package_failure")

    if bad_lines:
        integrity = "truncated"
        overall = "incomplete"
    elif not packages:
        integrity = "complete"
        overall = "incomplete"  # empty selection is not success
    elif counts["packages_failed"]:
        integrity = "complete"
        overall = "failed"
    elif counts["tests_executed"] == 0 and counts["tests_passed"] == 0:
        integrity = "complete"
        overall = "no_tests"
    else:
        integrity = "complete"
        overall = "passed"

    return {
        "kind": "go-test-json",
        "integrity": integrity,
        "overall": overall,
        "packages": packages,
        "counts": counts,
        "failures": failures,
        "bad_lines": bad_lines,
    }
