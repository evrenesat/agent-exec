"""Unit tests for operation-profile contracts (pure, no execution)."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Running via `python3 -m unittest` from the project root puts the root on
# sys.path; resolve the package through its full path to avoid a top-level
# module name collision with benchmarks.opencode or stdlib names.
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.contracts import (  # noqa: E402
    ProfileError, capability_enabled, parse_selectors, render, validate_profile)
from agent_exec.profiles import load_profile_dir  # noqa: E402

PROFILES_DIR = HERE.parent / "profiles"


def base_profile() -> dict:
    return {
        "schema_version": 1,
        "profile_id": "demo-web-unit",
        "project": "demo",
        "component": "web",
        "operation": "unit",
        "status": "draft",
        "host": {"hostname": "codex"},
        "command": {
            "argv_template": ["npm", "test", "--", "--run",
                              "--outputFile={report_dir}/out.json"],
            "cwd": "{source_root}",
        },
        "selectors": {"allowed_kinds": ["file", "directory"], "base": "src"},
        "deadlines": {"diagnose_seconds": 60, "hard_seconds": 180},
        "capabilities": {},
        "inputs": {"fingerprints": ["src/**"], "generated": []},
    }


class ValidationTests(unittest.TestCase):
    def test_valid_profile_round_trips(self):
        profile = base_profile()
        self.assertEqual(validate_profile(copy.deepcopy(profile)), profile)

    def test_bad_schema_version_rejected(self):
        profile = base_profile()
        profile["schema_version"] = 2
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_bad_status_rejected(self):
        profile = base_profile()
        profile["status"] = "launched_once"
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_nonpositive_deadline_rejected(self):
        profile = base_profile()
        profile["deadlines"]["hard_seconds"] = 0
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_diagnose_above_hard_rejected(self):
        profile = base_profile()
        profile["deadlines"]["diagnose_seconds"] = 999
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_selector_interpolation_rejected(self):
        profile = base_profile()
        profile["command"]["argv_template"] = ["npm", "test {selectors}"]
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_capability_true_rejected(self):
        profile = base_profile()
        profile["capabilities"] = {"supports_reuse": True}
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_capability_object_requires_reason(self):
        profile = base_profile()
        profile["capabilities"] = {"supports_reuse": {"enabled": True}}
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_missing_capability_defaults_disabled(self):
        profile = validate_profile(base_profile())
        for key in ("supports_coalescing", "supports_reuse",
                    "supports_incremental_report", "requires_external_state",
                    "needs_private_host"):
            self.assertFalse(capability_enabled(profile, key))

    def test_capability_object_enabled_flag_respected(self):
        profile = base_profile()
        profile["capabilities"] = {"supports_incremental_report": {
            "enabled": True, "reason": "native stream"}}
        profile = validate_profile(profile)
        self.assertTrue(capability_enabled(profile, "supports_incremental_report"))
        self.assertFalse(capability_enabled(profile, "supports_reuse"))


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.profile = validate_profile(base_profile())

    def test_empty_selectors_means_full_scope(self):
        self.assertEqual(parse_selectors(self.profile, None), ())
        self.assertEqual(parse_selectors(self.profile, []), ())

    def test_explicit_prefixes(self):
        parsed = parse_selectors(self.profile, ["file:src/a.test.ts", "dir:src/components"])
        self.assertEqual([s.kind for s in parsed], ["file", "directory"])
        self.assertEqual(parsed[0].value, "src/a.test.ts")

    def test_default_kind_is_first_allowed(self):
        parsed = parse_selectors(self.profile, ["src/a.test.ts"])
        self.assertEqual(parsed[0].kind, "file")

    def test_traversal_rejected(self):
        with self.assertRaises(ProfileError):
            parse_selectors(self.profile, ["../../etc/passwd"])

    def test_absolute_rejected(self):
        with self.assertRaises(ProfileError):
            parse_selectors(self.profile, ["/etc/passwd"])

    def test_outside_base_rejected(self):
        with self.assertRaises(ProfileError):
            parse_selectors(self.profile, ["package.json"])

    def test_unnormalized_rejected(self):
        with self.assertRaises(ProfileError):
            parse_selectors(self.profile, ["src/./a.test.ts"])

    def test_disallowed_kind_rejected(self):
        profile = base_profile()
        profile["selectors"]["allowed_kinds"] = ["file"]
        with self.assertRaises(ProfileError):
            parse_selectors(validate_profile(profile), ["dir:src/components"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.profile = validate_profile(base_profile())

    def render(self, profile=None, **kwargs):
        return render(profile or self.profile,
                      source_root=kwargs.pop("source_root", "/own/copy"),
                      job_id=kwargs.pop("job_id", "job-1"),
                      report_dir=kwargs.pop("report_dir", "/jobs/job-1/reports"),
                      **kwargs)

    def test_template_expansion_and_selector_append(self):
        rendered = self.render(selectors=["src/a.test.ts"])
        self.assertEqual(
            list(rendered.argv),
            ["npm", "test", "--", "--run", "--outputFile=/jobs/job-1/reports/out.json",
             "src/a.test.ts"])
        self.assertEqual(rendered.cwd, "/own/copy")

    def test_selectors_are_separate_argv_elements(self):
        rendered = self.render(selectors=["src/a b.test.ts"])
        self.assertIn("src/a b.test.ts", rendered.argv)

    def test_report_dir_inside_source_rejected(self):
        with self.assertRaises(ProfileError):
            self.render(report_dir="/own/copy/reports")

    def test_job_id_safety(self):
        with self.assertRaises(ProfileError):
            self.render(job_id="../evil")

    def test_draft_profile_renders_with_launch_warning(self):
        rendered = self.render()
        self.assertTrue(any("draft" in note for note in rendered.notes))

    def test_unadmitted_reporter_noted(self):
        profile = base_profile()
        profile["reports"] = [{"kind": "vitest-json",
                               "path_template": "{report_dir}/out.json",
                               "admitted": False}]
        rendered = self.render(profile=validate_profile(profile))
        self.assertTrue(any("not yet admitted" in note for note in rendered.notes))
        self.assertEqual(rendered.reports[0]["path"], "/jobs/job-1/reports/out.json")

    def test_report_path_escape_rejected(self):
        profile = base_profile()
        profile["command"]["argv_template"] = ["npm", "test"]
        profile["reports"] = [{"kind": "vitest-json",
                               "path_template": "{report_dir}/../escape.json"}]
        with self.assertRaises(ProfileError):
            self.render(profile=validate_profile(profile))

    def test_unknown_purpose_rejected(self):
        with self.assertRaises(ProfileError):
            self.render(purpose="ship")

    def test_unknown_variant_rejected(self):
        with self.assertRaises(ProfileError):
            self.render(variant="singleFork")

    def test_declared_variant_allowed(self):
        profile = base_profile()
        profile["variant_defaults"] = {"parallel": {"note": "CI default-parallel"}}
        rendered = self.render(profile=validate_profile(profile), variant="parallel")
        self.assertEqual(rendered.variant, "parallel")


class ShippedProfilesTests(unittest.TestCase):
    """The shipped profile directory must validate and render coherently."""

    def test_shipped_profiles_validate(self):
        profiles = load_profile_dir(PROFILES_DIR)
        self.assertIn("aflow-web-p100", profiles)
        # fixture_verified only with a bound admission receipt that exists.
        first = profiles["aflow-web-p100"]
        self.assertEqual(first["status"], "fixture_verified")
        self.assertEqual(profiles["cdx-unit"]["status"], "fixture_verified")
        self.assertTrue(Path(profiles["cdx-unit"]["admission_receipt"]).is_file())
        receipt = Path(first["admission_receipt"])
        self.assertTrue(receipt.is_file(),
                        "fixture_verified profile must bind an existing receipt")
        for profile_id, profile in profiles.items():
            with self.subTest(profile=profile_id):
                validate_profile(profile)

    def test_aflow_web_renders_canonical_plus_reporter(self):
        profiles = load_profile_dir(PROFILES_DIR)
        rendered = render(profiles["aflow-web-p100"], source_root="/own/aflow",
                          job_id="job-9", report_dir="/jobs/job-9/reports")
        self.assertEqual(
            list(rendered.argv),
            ["npm", "--prefix", "apps/aflow_app/web", "test", "--", "--run",
             "--maxWorkers=1", "--minWorkers=1", "--pool=forks",
             "--reporter=json", "--outputFile=/jobs/job-9/reports/vitest-json.json"])
        self.assertEqual(rendered.deadlines["hard_seconds"], 180)
        self.assertFalse(any(r["admitted"] for r in rendered.reports))

    def test_aflow_web_rejects_out_of_base_selector(self):
        profiles = load_profile_dir(PROFILES_DIR)
        with self.assertRaises(ProfileError):
            render(profiles["aflow-web-p100"], source_root="/own/aflow",
                   job_id="job-9", report_dir="/jobs/job-9/reports",
                   selectors=["server/src/x.test.py"])

    def test_drafts_render_with_notes(self):
        profiles = load_profile_dir(PROFILES_DIR)
        drafts = ["aflow-core-unit", "doublangu-web-unit"]
        for profile_id in drafts:
            with self.subTest(profile=profile_id):
                rendered = render(profiles[profile_id], source_root="/own/src",
                                  job_id="job-d", report_dir="/jobs/job-d/reports")
                self.assertTrue(any("draft" in note for note in rendered.notes))


if __name__ == "__main__":
    unittest.main()
