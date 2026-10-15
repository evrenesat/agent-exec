"""Unit tests for the two execution identities and component fingerprints."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Running via `python3 -m unittest` from the project root puts the root on
# sys.path; resolve the package through its full path to avoid a top-level
# module name collision with benchmarks.opencode or stdlib names.
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec import fingerprints  # noqa: E402
from agent_exec.fingerprints import (  # noqa: E402
    FingerprintError, UNKNOWN, component_fingerprint, concurrent_edit_detected,
    execution_key, known_fingerprint, recovery_family_key, recovery_goal_key,
    snapshot_changed, snapshot_known, unknown_fingerprint_result)


def exec_key(**overrides) -> str:
    base = dict(
        project="aflow", component="web", operation="unit", variant="default",
        source_fingerprint="meta-src-A", environment_fingerprint="meta-env-1",
        selectors=(), semantic_flags=(), test_seed=None)
    base.update(overrides)
    return execution_key(**base)


def goal_key(**overrides) -> str:
    base = dict(
        project="aflow", component="web", operation_family="unit",
        input_generation="meta-rel-A", environment_class="p100-web",
        intended_scope="full-suite")
    base.update(overrides)
    return recovery_goal_key(**base)


class ExecutionKeyTests(unittest.TestCase):
    def test_deterministic_and_canonical(self):
        self.assertEqual(exec_key(), exec_key())
        self.assertEqual(len(exec_key()), 64)
        self.assertNotEqual(exec_key(), goal_key())

    def test_variant_change_is_a_different_execution(self):
        # Pool/isolation changes are DIFFERENT executions: the variant is
        # part of the key (singleFork vs isolated one-worker forks).
        self.assertNotEqual(exec_key(variant="singleFork"),
                            exec_key(variant="one-worker-forks"))

    def test_source_and_environment_changes_change_the_key(self):
        self.assertNotEqual(exec_key(source_fingerprint="meta-src-B"),
                            exec_key())
        # A runner/package-manager identity change is a different execution.
        self.assertNotEqual(exec_key(environment_fingerprint="meta-env-runner2"),
                            exec_key())

    def test_selector_order_and_values_matter(self):
        self.assertNotEqual(exec_key(selectors=("a.ts", "b.ts")),
                            exec_key(selectors=("b.ts", "a.ts")))
        self.assertNotEqual(exec_key(selectors=("a.ts",)),
                            exec_key(selectors=("a.ts", "b.ts")))

    def test_semantic_flags_are_a_set(self):
        self.assertEqual(exec_key(semantic_flags=("x", "y")),
                         exec_key(semantic_flags=("y", "x")))
        self.assertNotEqual(exec_key(semantic_flags=("x",)),
                            exec_key(semantic_flags=("x", "y")))

    def test_seed_and_compiler_target_are_different_executions(self):
        # A different random seed is a different execution (Design C).
        self.assertNotEqual(exec_key(test_seed="7"), exec_key(test_seed="8"))
        self.assertNotEqual(exec_key(test_seed="7"), exec_key(test_seed=None))
        # A different compiler target arrives via the environment identity.
        self.assertNotEqual(
            exec_key(environment_fingerprint="meta-target-wasm32"),
            exec_key(environment_fingerprint="meta-target-x86_64"))

    def test_validation_rejects_empty_fields(self):
        with self.assertRaises(FingerprintError):
            exec_key(project="")
        with self.assertRaises(FingerprintError):
            exec_key(source_fingerprint=UNKNOWN)
        with self.assertRaises(FingerprintError):
            exec_key(selectors=("",))

    def test_unknown_seed_is_allowed(self):
        self.assertEqual(exec_key(test_seed=None), exec_key(test_seed=None))


class RecoveryGoalKeyTests(unittest.TestCase):
    def test_wrapper_and_presentation_changes_keep_the_same_goal(self):
        # Log filters, deadline bumps and reporter format changes are neither
        # key inputs: vitest|tail -> grep -> nohup groups to the same goal.
        base = goal_key()
        self.assertEqual(base, goal_key())
        self.assertEqual(base, goal_key(input_generation="meta-rel-A"))
        # (wrapper fields cannot even be passed: they are not parameters.)

    def test_goal_changes_with_generation_scope_and_environment_class(self):
        base = goal_key()
        self.assertNotEqual(base, goal_key(input_generation="meta-rel-B"))
        self.assertNotEqual(base, goal_key(intended_scope="focused:src/a.ts"))
        self.assertNotEqual(base, goal_key(environment_class="p100-web-webkit"))
        self.assertNotEqual(base, goal_key(operation_family="build"))

    def test_family_key_equals_goal_minus_scope(self):
        family = recovery_family_key(
            project="aflow", component="web", operation_family="unit",
            input_generation="meta-rel-A", environment_class="p100-web")
        self.assertEqual(
            family,
            recovery_family_key(project="aflow", component="web",
                                operation_family="unit",
                                input_generation="meta-rel-A",
                                environment_class="p100-web"))
        # Chromium and WebKit keep their own lineages (different class).
        self.assertNotEqual(
            family,
            recovery_family_key(project="aflow", component="web",
                                operation_family="unit",
                                input_generation="meta-rel-A",
                                environment_class="p100-web-webkit"))

    def test_unknown_generation_is_explicit_not_guessed(self):
        self.assertNotEqual(goal_key(input_generation=UNKNOWN),
                            goal_key(input_generation="meta-rel-A"))
        self.assertFalse(known_fingerprint(UNKNOWN))
        self.assertFalse(known_fingerprint(None))
        self.assertFalse(known_fingerprint(""))
        self.assertTrue(known_fingerprint("meta-rel-A"))


class ComponentFingerprintTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.ts").write_text("export {};\n")
        (self.root / "src" / "util.ts").write_text("export const x = 1;\n")
        (self.root / "README.md").write_text("docs\n")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "HEAD").write_text("ref\n")
        (self.root / "dist").mkdir()
        (self.root / "dist" / "bundle.js").write_text("built\n")

    def include(self):
        return ["src/**", "*.md"]

    def test_hashes_actual_bytes_including_untracked(self):
        first = component_fingerprint(self.root, self.include())
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["files"], 3)
        self.assertNotEqual(first["sha256"], UNKNOWN)
        # Deterministic and independent of walk order.
        again = component_fingerprint(self.root, self.include())
        self.assertEqual(first["sha256"], again["sha256"])
        self.assertEqual(first["entries"]["src/app.ts"]["sha256"],
                         fingerprints.hashlib.sha256(
                             b"export {};\n").hexdigest())

    def test_excludes_and_git_are_skipped(self):
        result = component_fingerprint(
            self.root, ["**/*"], exclude_globs=["dist/**", "*.md"])
        rels = set(result["entries"])
        self.assertNotIn("dist/bundle.js", rels)
        self.assertNotIn("README.md", rels)
        self.assertNotIn(".git/HEAD", rels)
        self.assertIn("src/app.ts", rels)

    def test_content_change_changes_the_hash(self):
        before = component_fingerprint(self.root, self.include())
        (self.root / "src" / "app.ts").write_text("export { x };\n")
        after = component_fingerprint(self.root, self.include())
        self.assertNotEqual(before["sha256"], after["sha256"])

    def test_missing_root_is_explicitly_unknown(self):
        result = component_fingerprint(self.root / "nope", self.include())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["sha256"], UNKNOWN)
        self.assertEqual(result, unknown_fingerprint_result("root_missing"))
        self.assertFalse(snapshot_known(result))

    def test_unreadable_entry_is_explicitly_unknown(self):
        (self.root / "src" / "broken.ts").symlink_to(
            self.root / "definitely-missing.ts")
        result = component_fingerprint(self.root, ["src/**"])
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["reason"].startswith("unreadable:src/broken.ts"))
        self.assertEqual(result["sha256"], UNKNOWN)

    def test_snapshot_compare_detects_content_mtime_and_added_files(self):
        before = component_fingerprint(self.root, self.include())
        # Same bytes, new mtime: the mtime+size+hash compare must flag it.
        os.utime(self.root / "src" / "app.ts", ns=(1_000_000_000,) * 2)
        touched = component_fingerprint(self.root, self.include())
        self.assertTrue(snapshot_changed(before, touched))
        self.assertEqual(before["sha256"], touched["sha256"])
        # Restored content and mtime: unchanged.
        os.utime(self.root / "src" / "app.ts",
                 ns=(before["entries"]["src/app.ts"]["mtime_ns"],) * 2)
        self.assertFalse(snapshot_changed(before,
                                          component_fingerprint(
                                              self.root, self.include())))
        # A new relevant file is a change.
        (self.root / "src" / "new.ts").write_text("new\n")
        self.assertTrue(concurrent_edit_detected(
            before, component_fingerprint(self.root, self.include())))

    def test_unknown_snapshots_count_as_changed(self):
        unknown = unknown_fingerprint_result("root_missing")
        good = component_fingerprint(self.root, self.include())
        self.assertTrue(snapshot_changed(unknown, good))
        self.assertTrue(snapshot_changed(good, unknown))
        self.assertTrue(concurrent_edit_detected(unknown, unknown))


class DistinguishingExampleTests(unittest.TestCase):
    """Design C: vitest|tail -> grep -> nohup same goal; real changes split."""

    def test_wrapper_variants_group_to_same_goal_not_new_executions(self):
        # Three wrapper variants of one failing full-suite run: identical
        # execution keys (same work) and one recovery goal; wrapper changes
        # are not progress and must not escape the retry budget.
        from agent_exec.policy import LaunchRequest, RecoveryPolicy

        def request(request_id, *tags):
            return LaunchRequest(
                request_id=request_id, project="aflow", component="web",
                operation="unit", operation_family="unit",
                source_fingerprint="meta-src-A",
                relevant_inputs_fingerprint="meta-rel-A",
                environment_fingerprint="meta-env-1",
                environment_class="p100-web", intended_scope="full-suite",
                wrapper_tags=tuple(tags))

        policy = RecoveryPolicy()
        keys, goals = set(), set()
        for index, tags in enumerate((("log-filter:tail",),
                                      ("log-filter:grep",),
                                      ("nohup",))):
            decision = policy.evaluate_launch(request(f"r{index}", *tags))
            policy.report_completion(decision.job_id, outcome="timed_out")
            keys.add(decision.execution_key)
            goals.add(decision.goal_key)
        self.assertEqual(len(keys), 1)
        self.assertEqual(len(goals), 1)
        # The third unchanged attempt is blocked: wrappers were not progress.
        final = policy.evaluate_launch(request("r-final", "nohup"))
        self.assertEqual(final.reason_code, "recovery_budget_exhausted")

    def test_real_differences_are_retained_as_different_executions(self):
        # Different seed, target, isolation and scope are different
        # executions, even when a profile may register them for one goal.
        keys = {
            exec_key(test_seed="7"),
            exec_key(test_seed="8"),
            exec_key(environment_fingerprint="meta-target-wasm32"),
            exec_key(variant="singleFork"),
            exec_key(variant="one-worker-forks"),
        }
        self.assertEqual(len(keys), 5)
        goals = {
            goal_key(),
            goal_key(intended_scope="focused:src/app.test.ts"),
            goal_key(environment_class="p100-web-webkit"),
            goal_key(input_generation="meta-rel-B"),
        }
        self.assertEqual(len(goals), 4)


if __name__ == "__main__":
    unittest.main()
