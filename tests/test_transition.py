from __future__ import annotations

import json
import multiprocessing
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexora_audit.sqlite_state import create_runtime_database
from nexora_audit import transition


def _hold_transition_lock(marker: str, ready: object, release: object) -> None:
    with transition.transition_lock(Path(marker)):
        ready.set()  # type: ignore[attr-defined]
        release.wait(10)  # type: ignore[attr-defined]


class RuntimeTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.marker = transition.marker_path_for(self.root, "synthetic")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _tree(path: Path, value: str) -> None:
        path.mkdir(parents=True)
        (path / "value.txt").write_text(value, encoding="utf-8")

    def _planned_under(self, root: Path) -> tuple[dict[str, object], dict[str, Path], Path]:
        paths: dict[str, Path] = {}
        marker = transition.marker_path_for(root, "synthetic")
        token = transition.new_transition_token()
        for name in transition.RUNTIME_TARGETS:
            live = root / name
            stage, backup = transition.stage_and_backup_for(live, token)
            paths[f"{name}_live"] = live
            paths[f"{name}_stage"] = stage
            paths[f"{name}_backup"] = backup
            if name == "db":
                create_runtime_database(live, publication_id="old")
                create_runtime_database(stage, publication_id="new")
            else:
                self._tree(live, "old")
                self._tree(stage, "new")
        record = transition.plan_transition(
            kind="runtime",
            scope_id="synthetic",
            publication_id="new",
            marker_path=marker,
            targets=tuple(
                (name, paths[f"{name}_live"], paths[f"{name}_stage"], paths[f"{name}_backup"])
                for name in transition.RUNTIME_TARGETS
            ),
        )
        return record, paths, marker

    def _planned(self) -> tuple[dict[str, object], dict[str, Path]]:
        record, paths, _ = self._planned_under(self.root)
        return record, paths

    def test_complete_transition_installs_one_coherent_generation(self) -> None:
        record, paths = self._planned()
        transition.run_transition(self.marker, record)
        self.assertFalse(self.marker.exists())
        for name in transition.RUNTIME_TARGETS:
            self.assertFalse(paths[f"{name}_stage"].exists())
            self.assertFalse(paths[f"{name}_backup"].exists())
        self.assertEqual((paths["thumbs_live"] / "value.txt").read_text(encoding="utf-8"), "new")

    def test_interrupted_precommit_transition_recovers_the_old_generation(self) -> None:
        record, paths = self._planned()
        real_rename = transition._rename
        calls = 0

        def fail_on_second(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic interruption")
            real_rename(source, target)

        with patch.object(transition, "_rename", side_effect=fail_on_second):
            with self.assertRaisesRegex(OSError, "synthetic interruption"):
                transition.run_transition(self.marker, record)
        self.assertTrue(self.marker.exists())
        self.assertEqual(transition.resolve_pending_transition(self.marker), "restored_old")
        self.assertFalse(self.marker.exists())
        self.assertEqual((paths["thumbs_live"] / "value.txt").read_text(encoding="utf-8"), "old")

    def test_every_interrupted_rename_recovers_the_old_generation(self) -> None:
        for failure_at in range(1, 7):
            with self.subTest(failure_at=failure_at):
                root = self.root / f"fault-{failure_at}"
                root.mkdir()
                record, paths, marker = self._planned_under(root)
                real_rename = transition._rename
                calls = 0

                def fail_at_selected_rename(source: Path, target: Path) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == failure_at:
                        raise OSError(f"synthetic interruption {failure_at}")
                    real_rename(source, target)

                with patch.object(transition, "_rename", side_effect=fail_at_selected_rename):
                    with self.assertRaisesRegex(OSError, "synthetic interruption"):
                        transition.run_transition(marker, record)
                self.assertEqual(transition.resolve_pending_transition(marker), "restored_old")
                for name in transition.RUNTIME_TARGETS:
                    if name == "db":
                        continue
                    self.assertEqual(
                        (paths[f"{name}_live"] / "value.txt").read_text(encoding="utf-8"),
                        "old",
                    )

    def test_interrupted_recovery_is_idempotently_resumable(self) -> None:
        record, paths = self._planned()
        real_rename = transition._rename
        run_calls = 0

        def interrupt_run(source: Path, target: Path) -> None:
            nonlocal run_calls
            run_calls += 1
            if run_calls == 2:
                raise OSError("interrupt run")
            real_rename(source, target)

        with patch.object(transition, "_rename", side_effect=interrupt_run):
            with self.assertRaisesRegex(OSError, "interrupt run"):
                transition.run_transition(self.marker, record)

        recovery_calls = 0

        def interrupt_recovery(source: Path, target: Path) -> None:
            nonlocal recovery_calls
            recovery_calls += 1
            if recovery_calls == 1:
                raise OSError("interrupt recovery")
            real_rename(source, target)

        with patch.object(transition, "_rename", side_effect=interrupt_recovery):
            with self.assertRaisesRegex(OSError, "interrupt recovery"):
                transition.resolve_pending_transition(self.marker)
        self.assertTrue(self.marker.exists())
        self.assertEqual(transition.resolve_pending_transition(self.marker), "restored_old")
        self.assertEqual((paths["thumbs_live"] / "value.txt").read_text(encoding="utf-8"), "old")

    def test_postcommit_interruption_keeps_the_verified_new_generation(self) -> None:
        record, paths = self._planned()
        with patch.object(transition, "_delete_residue", side_effect=OSError("after commit")):
            with self.assertRaisesRegex(OSError, "after commit"):
                transition.run_transition(self.marker, record)
        marker = transition.read_marker(self.marker)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["phase"], "cleanup")  # type: ignore[index]
        self.assertEqual(transition.resolve_pending_transition(self.marker), "kept_new")
        self.assertEqual((paths["thumbs_live"] / "value.txt").read_text(encoding="utf-8"), "new")

    def test_recovery_refuses_an_unknown_or_tampered_state(self) -> None:
        record, paths = self._planned()
        transition.write_marker(self.marker, record)
        paths["db_live"].write_bytes(b"tampered")
        with self.assertRaises(transition.TransitionCorruptionError):
            transition.resolve_pending_transition(self.marker)
        self.assertTrue(self.marker.exists())

    def test_marker_rejects_unknown_fields(self) -> None:
        record, _ = self._planned()
        record["unexpected"] = "not allowed"
        self.marker.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(transition.TransitionCorruptionError, "fields"):
            transition.read_marker(self.marker)

    def test_marker_rejects_boolean_schema_version(self) -> None:
        record, _ = self._planned()
        record["marker_schema_version"] = True
        self.marker.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(transition.TransitionCorruptionError, "schema version"):
            transition.read_marker(self.marker)



    def test_marker_write_failures_leave_one_complete_visible_record(self) -> None:
        for helper, expected_phase in (
            ("_write_temp_marker", "prepared"),
            ("_atomic_replace", "prepared"),
            ("_fsync_marker_parent", "intent_backup_db"),
        ):
            with self.subTest(helper=helper):
                root = self.root / helper
                root.mkdir()
                record, _, marker = self._planned_under(root)
                transition.write_marker(marker, record)
                next_record = json.loads(json.dumps(record))
                next_record["phase"] = "intent_backup_db"
                with patch.object(transition, helper, side_effect=OSError("synthetic marker fault")):
                    with self.assertRaisesRegex(OSError, "synthetic marker fault"):
                        with transition.transition_lock(marker):
                            transition._write_marker_unlocked(marker, next_record)
                visible = transition.read_marker(marker)
                self.assertIsNotNone(visible)
                self.assertEqual(visible["phase"], expected_phase)  # type: ignore[index]

    def test_scope_id_cannot_escape_the_transition_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope_id"):
            transition.marker_path_for(self.root, "../escape")

    def test_symlinked_target_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as external_name:
            external = Path(external_name)
            link = self.root / "linked"
            link.symlink_to(external, target_is_directory=True)
            targets = []
            for name in transition.RUNTIME_TARGETS:
                live = link / name
                stage, backup = transition.stage_and_backup_for(live, "token")
                if name == "db":
                    create_runtime_database(live, publication_id="old")
                    create_runtime_database(stage, publication_id="new")
                else:
                    self._tree(live, "old")
                    self._tree(stage, "new")
                targets.append((name, live, stage, backup))
            with self.assertRaisesRegex(transition.TransitionCorruptionError, "symlink|escapes"):
                transition.plan_transition(
                    kind="runtime",
                    scope_id="synthetic",
                    publication_id="new",
                    marker_path=self.marker,
                    targets=tuple(targets),
                )

    def test_corrupt_prepared_marker_cannot_delete_an_incumbent(self) -> None:
        token = transition.new_transition_token()
        paths: dict[str, Path] = {}
        targets = []
        for name in transition.RUNTIME_TARGETS:
            live = self.root / name
            stage, backup = transition.stage_and_backup_for(live, token)
            paths[f"{name}_live"] = live
            if name == "db":
                create_runtime_database(stage, publication_id="new")
            else:
                self._tree(stage, "new")
            targets.append((name, live, stage, backup))
        record = transition.plan_transition(
            kind="runtime",
            scope_id="synthetic",
            publication_id="new",
            marker_path=self.marker,
            targets=tuple(targets),
        )
        incumbent = paths["thumbs_live"]
        self._tree(incumbent, "incumbent")
        with transition.transition_lock(self.marker):
            transition._write_marker_unlocked(self.marker, record)
        with self.assertRaises(transition.TransitionCorruptionError):
            transition.resolve_pending_transition(self.marker)
        self.assertEqual((incumbent / "value.txt").read_text(encoding="utf-8"), "incumbent")

    def test_public_mutations_enforce_exclusive_locking(self) -> None:
        record, _ = self._planned()
        with transition.transition_lock(self.marker):
            with self.assertRaises(transition.TransitionBusyError):
                transition.run_transition(self.marker, record)

    def test_transition_lock_excludes_another_process(self) -> None:
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_transition_lock,
            args=(str(self.marker), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            with self.assertRaises(transition.TransitionBusyError):
                with transition.transition_lock(self.marker):
                    pass
        finally:
            release.set()
            process.join(5)
        self.assertEqual(process.exitcode, 0)
    def test_run_rejects_a_forged_record_outside_the_marker_root(self) -> None:
        marker_root = self.root / "markers"
        marker_root.mkdir()
        marker = transition.marker_path_for(marker_root, "synthetic")
        outside = self.root / "outside"
        outside.mkdir()
        record, paths, _ = self._planned_under(outside)

        with self.assertRaises(transition.TransitionCorruptionError):
            transition.run_transition(marker, record)

        self.assertEqual((paths["thumbs_live"] / "value.txt").read_text(encoding="utf-8"), "old")
        self.assertEqual((paths["thumbs_stage"] / "value.txt").read_text(encoding="utf-8"), "new")
        self.assertFalse(marker.exists())

    def test_run_rejects_stale_live_or_staged_identity_before_mutation(self) -> None:
        for changed_slot in ("live", "stage"):
            with self.subTest(changed_slot=changed_slot):
                root = self.root / changed_slot
                root.mkdir()
                record, paths, marker = self._planned_under(root)
                changed = paths[f"thumbs_{changed_slot}"] / "value.txt"
                changed.write_text("changed-after-planning", encoding="utf-8")

                with self.assertRaises(transition.TransitionCorruptionError):
                    transition.run_transition(marker, record)

                self.assertFalse(marker.exists())
                self.assertEqual(
                    (paths["thumbs_live"] / "value.txt").read_text(encoding="utf-8"),
                    "changed-after-planning" if changed_slot == "live" else "old",
                )
                self.assertTrue(paths["thumbs_stage"].exists())
                self.assertFalse(paths["thumbs_backup"].exists())

    def test_run_refuses_to_clobber_an_existing_marker(self) -> None:
        record, paths = self._planned()
        transition.write_marker(self.marker, record)
        before = self.marker.read_bytes()

        with self.assertRaises(transition.TransitionCorruptionError):
            transition.run_transition(self.marker, record)

        self.assertEqual(self.marker.read_bytes(), before)
        self.assertTrue(paths["thumbs_live"].exists())
        self.assertTrue(paths["thumbs_stage"].exists())
        self.assertFalse(paths["thumbs_backup"].exists())

    def test_run_requires_a_prepared_entry_record(self) -> None:
        record, paths = self._planned()
        record["phase"] = "backed_up_db"

        with self.assertRaises(transition.TransitionCorruptionError):
            transition.run_transition(self.marker, record)

        self.assertFalse(self.marker.exists())
        self.assertTrue(paths["db_live"].exists())
        self.assertTrue(paths["db_stage"].exists())
        self.assertFalse(paths["db_backup"].exists())

    def test_public_marker_write_is_initial_only(self) -> None:
        record, _ = self._planned()
        transition.write_marker(self.marker, record)
        before = self.marker.read_bytes()

        with self.assertRaises(transition.TransitionCorruptionError):
            transition.write_marker(self.marker, record)

        self.assertEqual(self.marker.read_bytes(), before)

    def test_orphan_sweep_rejects_targets_outside_the_marker_root(self) -> None:
        marker_root = self.root / "markers"
        marker_root.mkdir()
        marker = transition.marker_path_for(marker_root, "synthetic")
        outside = self.root / "outside"
        outside.mkdir()
        live = outside / "asset"
        residue = outside / "asset.token.stage"
        residue.write_text("keep", encoding="utf-8")

        with self.assertRaises(transition.TransitionCorruptionError):
            transition.sweep_orphaned_stage_paths(marker, (live,))

        self.assertEqual(residue.read_text(encoding="utf-8"), "keep")

    def test_orphan_sweep_rejects_a_symlinked_target_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as external_name:
            external = Path(external_name)
            link = self.root / "linked"
            link.symlink_to(external, target_is_directory=True)
            live = link / "asset"
            residue = external / "asset.token.stage"
            residue.write_text("keep", encoding="utf-8")

            with self.assertRaises(transition.TransitionCorruptionError):
                transition.sweep_orphaned_stage_paths(self.marker, (live,))

            self.assertEqual(residue.read_text(encoding="utf-8"), "keep")

    def test_orphan_sweep_removes_safe_residue_but_respects_any_root_marker(self) -> None:
        live = self.root / "asset"
        first = self.root / "asset.one.stage"
        first.write_text("remove", encoding="utf-8")
        transition.sweep_orphaned_stage_paths(self.marker, (live,))
        self.assertFalse(first.exists())

        second = self.root / "asset.two.backup"
        second.write_text("keep", encoding="utf-8")
        other_marker = transition.marker_path_for(self.root, "other")
        other_marker.write_text("{}\n", encoding="utf-8")
        transition.sweep_orphaned_stage_paths(self.marker, (live,))
        self.assertEqual(second.read_text(encoding="utf-8"), "keep")

    def test_root_lock_excludes_a_different_scope_in_another_process(self) -> None:
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        other_marker = transition.marker_path_for(self.root, "other")
        process = context.Process(
            target=_hold_transition_lock,
            args=(str(self.marker), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            with self.assertRaises(transition.TransitionBusyError):
                with transition.transition_lock(other_marker):
                    pass
        finally:
            release.set()
            process.join(5)
        self.assertEqual(process.exitcode, 0)
    def test_run_with_recovery_does_not_resolve_a_preexisting_marker(self) -> None:
        record, paths = self._planned()
        transition.write_marker(self.marker, record)
        before = self.marker.read_bytes()

        with self.assertRaises(transition.TransitionCorruptionError):
            transition.run_transition_with_recovery(self.marker, record)

        self.assertEqual(self.marker.read_bytes(), before)
        self.assertTrue(paths["thumbs_live"].exists())
        self.assertTrue(paths["thumbs_stage"].exists())
        self.assertFalse(paths["thumbs_backup"].exists())

    def test_run_refuses_when_another_scope_marker_exists_in_the_root(self) -> None:
        record, paths = self._planned()
        other_marker = transition.marker_path_for(self.root, "other")
        other_marker.write_text("{}\n", encoding="utf-8")
        before = other_marker.read_bytes()

        with self.assertRaises(transition.TransitionCorruptionError):
            transition.run_transition(self.marker, record)

        self.assertEqual(other_marker.read_bytes(), before)
        self.assertTrue(paths["thumbs_live"].exists())
        self.assertTrue(paths["thumbs_stage"].exists())
        self.assertFalse(paths["thumbs_backup"].exists())
    def test_run_with_recovery_uses_one_validated_record_snapshot(self) -> None:
        record, _ = self._planned()
        real_validate = transition._validated_entry_record
        calls = 0

        def count_validations(marker: Path, candidate: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise AssertionError("entry record was re-read after validation")
            return real_validate(marker, candidate)  # type: ignore[arg-type,return-value]

        with patch.object(transition, "_validated_entry_record", side_effect=count_validations):
            transition.run_transition_with_recovery(self.marker, record)
        self.assertEqual(calls, 1)

    def test_db_postcheck_rejects_different_valid_bytes_with_the_same_publication_id(self) -> None:
        record, paths = self._planned()

        class MutatingReporter:
            def __init__(self) -> None:
                self.polls = 0

            def poll(self) -> None:
                self.polls += 1
                if self.polls == 9:
                    with sqlite3.connect(paths["db_live"]) as connection:
                        connection.execute("CREATE TABLE injected (value TEXT)")

        with self.assertRaises(transition.TransitionPostcheckError):
            transition.run_transition(self.marker, record, reporter=MutatingReporter())  # type: ignore[arg-type]

    def test_orphan_sweep_treats_live_names_as_literals(self) -> None:
        live = self.root / "asset[ab]"
        intended = self.root / "asset[ab].token.stage"
        unrelated = self.root / "asseta.token.stage"
        intended.write_text("remove", encoding="utf-8")
        unrelated.write_text("keep", encoding="utf-8")

        transition.sweep_orphaned_stage_paths(self.marker, (live,))

        self.assertFalse(intended.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_orphan_sweep_preflights_every_candidate_before_deletion(self) -> None:
        first_live = self.root / "first"
        first_residue = self.root / "first.token.stage"
        first_residue.write_text("keep", encoding="utf-8")
        second_live = self.root / "second"
        with tempfile.TemporaryDirectory() as external_name:
            external = Path(external_name) / "external"
            external.write_text("external", encoding="utf-8")
            bad_residue = self.root / "second.token.stage"
            bad_residue.symlink_to(external)

            with self.assertRaises(transition.TransitionCorruptionError):
                transition.sweep_orphaned_stage_paths(
                    self.marker,
                    (first_live, second_live),
                )

        self.assertEqual(first_residue.read_text(encoding="utf-8"), "keep")
