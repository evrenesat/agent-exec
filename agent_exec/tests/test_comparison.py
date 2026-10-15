"""Comparison ledger tests (offline fixtures only)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.comparison import build_ledger, check_ledger  # noqa: E402


def make_arm(root: Path, name: str, *, arm: str, repeat: int, accepted: bool | None,
             inputs: dict | None = None) -> None:
    d = root / "arms" / name
    d.mkdir(parents=True)
    (d / "arm-receipt.json").write_text(json.dumps({
        "arm": arm, "repeat": repeat,
        "session": {"exit": 0, "wall_seconds": 10},
        "metrics": {"model_requests": 1, "tool_attempts": 1},
        "frozen_inputs": inputs or {"public_spec_sha256": "x"},
    }))
    if accepted is not None:
        (d / "acceptance-receipt.json").write_text(json.dumps({
            "accepted": accepted,
            "cases": [{"case": "full-suite", "outcome": "passed",
                       "cleanup": "verified_empty"}],
            "full_report": {"complete": True, "total": 1, "passed": 1},
            "heldout_report": {"complete": True, "total": 1, "passed": 1},
        }))


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "evaluation"
        (self.root / "arms").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_collects_and_flags_inconsistent_inputs(self):
        make_arm(self.root, "control-r1", arm="control", repeat=1, accepted=True,
                 inputs={"public_spec_sha256": "x"})
        make_arm(self.root, "bridge-r1", arm="bridge", repeat=1, accepted=True,
                 inputs={"public_spec_sha256": "y"})
        ledger = build_ledger(self.root)
        self.assertEqual(len(ledger["arms"]), 2)
        self.assertFalse(ledger["task"]["inputs_consistent"])

    def test_reference_dirs_excluded(self):
        make_arm(self.root, "control-r1", arm="control", repeat=1, accepted=True)
        make_arm(self.root, "reference-r0", arm="reference", repeat=0, accepted=True)
        ledger = build_ledger(self.root)
        self.assertEqual([a["arm"] for a in ledger["arms"]], ["control"])

    def test_check_accepts_consistent_ledger(self):
        make_arm(self.root, "control-r1", arm="control", repeat=1, accepted=True)
        make_arm(self.root, "bridge-r1", arm="bridge", repeat=1, accepted=True)
        out = self.root / "ledger.json"
        build_ledger(self.root, out)
        self.assertEqual(check_ledger(out), [])

    def test_check_rejects_missing_acceptance_and_duplicates(self):
        make_arm(self.root, "control-r1", arm="control", repeat=1, accepted=None)
        make_arm(self.root, "bridge-r1", arm="bridge", repeat=1, accepted=True)
        make_arm(self.root, "bridge-r1b", arm="bridge", repeat=1, accepted=True)
        out = self.root / "ledger.json"
        build_ledger(self.root, out)
        problems = check_ledger(out)
        self.assertTrue(any("no acceptance" in p for p in problems))
        self.assertTrue(any("duplicate" in p for p in problems))

    def test_check_rejects_empty_ledger(self):
        out = self.root / "ledger.json"
        build_ledger(self.root, out)
        problems = check_ledger(out)
        self.assertTrue(any("no arms" in p for p in problems))

    def test_build_refuses_overwrite(self):
        out = self.root / "ledger.json"
        out.write_text("{}")
        make_arm(self.root, "control-r1", arm="control", repeat=1, accepted=True)
        with self.assertRaises(SystemExit):
            build_ledger(self.root, out)


if __name__ == "__main__":
    unittest.main()
