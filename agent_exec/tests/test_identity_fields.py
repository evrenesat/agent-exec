"""Regression: profile "reports" must participate in admission identity.

Reports select which evidence files are read and by which parser, so a
changed report path/parser changes what a scored run proves and must
invalidate existing admission receipts. Administrative fields stay outside
identity so documented draft -> fixture_verified promotion keeps working.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.profiles import ADMISSION_IDENTITY_FIELDS  # noqa: E402


class AdmissionIdentityFieldsTests(unittest.TestCase):
    def test_reports_is_identity_relevant(self):
        # P2: fails until ADMISSION_IDENTITY_FIELDS includes reports
        self.assertIn("reports", ADMISSION_IDENTITY_FIELDS)

    def test_administrative_fields_stay_outside_identity(self):
        for field in ("status", "admission_receipt", "notes"):
            self.assertNotIn(field, ADMISSION_IDENTITY_FIELDS)


if __name__ == "__main__":
    unittest.main()
