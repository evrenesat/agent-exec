"""Native test-report readers: truthful evidence from runner output formats.

Reports are read only AFTER process completion and only classify what the
format itself proves. A missing, empty, truncated or non-matching report is
incomplete evidence — never a pass. Zero collected tests is an incomplete
required check, not success. These readers never execute anything.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path


class ReportError(ValueError):
    pass


def read_vitest_json(path: Path, *, expect_job_id: str | None = None) -> dict:
    """Read a Vitest 1 JSON report into the bounded receipt summary."""
    path = Path(path)
    if not path.is_file():
        return {"kind": "vitest-json", "integrity": "missing", "complete": False,
                "error": "report file absent"}
    raw = path.read_bytes()
    if not raw.strip():
        return {"kind": "vitest-json", "integrity": "empty", "complete": False,
                "error": "report file is empty"}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        return {"kind": "vitest-json", "integrity": "truncated",
                "complete": False, "error": f"invalid JSON: {error}"}
    required = ("numTotalTests", "numPassedTests", "numFailedTests", "success")
    missing = [key for key in required if key not in data]
    if missing:
        return {"kind": "vitest-json", "integrity": "unrecognized",
                "complete": False, "error": f"missing fields: {missing}"}
    total = int(data["numTotalTests"])
    failures = []
    for suite in data.get("testResults", []):
        for assertion in suite.get("assertionResults", []):
            if assertion.get("status") == "failed":
                failures.append({
                    "fullName": assertion.get("fullName", "")[:200],
                    "failureMessages": [m[:500]
                                        for m in assertion.get("failureMessages", [])[:2]],
                })
    failures = failures[:20]
    # Zero collected tests is incomplete evidence in every shape — whether
    # the report carries empty suite results or structurally valid ones.
    complete = total > 0
    notes = []
    if total == 0:
        notes.append("zero collected tests: incomplete evidence and an "
                     "incomplete required check, never success")
    return {
        "kind": "vitest-json", "integrity": "valid", "complete": complete,
        "total": total, "passed": int(data["numPassedTests"]),
        "failed": int(data["numFailedTests"]),
        "pending": int(data.get("numPendingTests", 0)),
        "todo": int(data.get("numTodoTests", 0)),
        "suites_total": int(data.get("numTotalTestSuites", 0)),
        "runner_success": bool(data.get("success")),
        "failures": failures,
        "notes": notes,
        "bytes": len(raw),
    }


def read_junit_xml(path: Path) -> dict:
    """Minimal JUnit reader preserving setup/teardown vs assertion failures."""
    path = Path(path)
    if not path.is_file():
        return {"kind": "junit", "integrity": "missing", "complete": False,
                "error": "report file absent"}
    raw = path.read_bytes()
    if not raw.strip():
        return {"kind": "junit", "integrity": "empty", "complete": False,
                "error": "report file is empty"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        return {"kind": "junit", "integrity": "truncated", "complete": False,
                "error": f"invalid XML: {error}"}
    if root.tag != "testsuites":
        return {"kind": "junit", "integrity": "unrecognized", "complete": False,
                "error": f"unexpected root element: {root.tag}"}
    total = failed = errors = skipped = 0
    for suite in root.findall("testsuite"):
        total += int(suite.get("tests", 0))
        failed += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
    return {"kind": "junit", "integrity": "valid", "complete": total > 0,
            "total": total, "failed": failed, "errors": errors,
            "skipped": skipped, "bytes": len(raw),
            "notes": ["zero tests reported: incomplete evidence, never success"]
            if total == 0 else []}


def read_go_test_json(path: Path) -> dict:
    """Read a captured `go test -json` stream via the go_reports parser."""
    from .go_reports import parse_go_test_json_stream
    path = Path(path)
    if not path.is_file():
        return {"kind": "go-test-json", "integrity": "missing",
                "complete": False, "error": "report file absent"}
    raw = path.read_text()
    if not raw.strip():
        return {"kind": "go-test-json", "integrity": "empty",
                "complete": False, "error": "report file is empty"}
    parsed = parse_go_test_json_stream(raw)
    counts = parsed.get("counts", parsed.get("summary", {}))
    overall = parsed.get("overall")
    # Zero collected tests is never success: a complete stream of only
    # no-test packages (or an empty selection) reports overall
    # "no_tests"/"incomplete", so completion requires the parser's "passed"
    # verdict AND at least one executed test, not merely zero failures.
    complete = (parsed.get("integrity") == "complete"
                and overall == "passed"
                and counts.get("tests_executed", 0) > 0)
    notes = ["no-test packages counted separately, never whole-project "
             "success"] if counts.get("packages_no_tests") else []
    if overall == "no_tests":
        notes.append("zero tests executed: only packages without tests "
                     "collected — incomplete required check, never success")
    return {"kind": "go-test-json", "integrity": parsed.get("integrity"),
            "overall": overall, "complete": complete, "bytes": len(raw),
            "tests_passed": counts.get("tests_passed", 0),
            "tests_failed": counts.get("tests_failed", 0),
            "tests_skipped": counts.get("tests_skipped", 0),
            "tests_executed": counts.get("tests_executed", 0),
            "packages_no_tests": counts.get("packages_no_tests", 0),
            "packages_cached": counts.get("packages_cached", 0),
            "failures": parsed.get("failures", [])[:10],
            "notes": notes}


def summarize(reports: list[dict]) -> dict:
    """Combine report summaries; any incomplete part keeps the check honest."""
    complete = bool(reports) and all(r.get("complete") for r in reports)
    valid = bool(reports) and all(r.get("integrity") == "valid" for r in reports)
    return {"reports": reports, "all_valid": valid, "all_complete": complete}
