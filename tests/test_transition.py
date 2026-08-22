from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexora_audit import transition
from nexora_audit.sqlite_state import create_runtime_database


def _hold_transition_lock(marker: str, ready: object, release: object) -> None:
    with transition.transition_lock(Path(marker)):
        ready.set()  # type: ignore[attr-defined]
        release.wait(10)  # type: ignore[attr-defined]


class _StopAtPhaseReporter:
    def __init__(self, marker: str, phase: str, reached: object) -> None:
        self.marker = Path(marker)
        self.phase = phase
        self.reached = reached

    def poll(self) -> None:
        current = transition.read_marker(self.marker)
        if current is not None and current["phase"] == self.phase:
            self.reached.set()  # type: ignore[attr-defined]
            signal.pause()

    def step(self, _label: str, _message: str) -> None:
        return


def _run_transition_to_phase(
    marker: str,
    record: dict[str, object],
    phase: str,
    reached: object,
) -> None:
    transition.run_transition(
        Path(marker),
        record,
        reporter=_StopAtPhaseReporter(marker, phase, reached),  # type: ignore[arg-type]
    )


class RuntimeTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
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

    @staticmethod
    def _build_tree(path: Path, publication_id: str) -> None:
        path.mkdir(parents=True)
        for entry in transition.BUILD_REQUIRED_ENTRIES:
            if entry == transition.BUILD_PUBLICATION_FILE:
                (path / entry).write_text(
                    json.dumps({"publication_id": publication_id}) + "\n",
                    encoding="utf-8",
                )
            else:
                (path / entry).write_text(f"{publication_id}:{entry}\n", encoding="utf-8")

    def _planned_build_under(
        self,
        root: Path,
        *,
        nested: bool = False,
    ) -> tuple[dict[str, object], dict[str, Path], Path]:
        marker = transition.marker_path_for(root, "build-synthetic")
        token = transition.new_transition_token()
        parent = root / "nested" if nested else root
        parent.mkdir(parents=True, exist_ok=True)
        live = parent / "build"
        stage, backup = transition.stage_and_backup_for(live, token)
        self._build_tree(live, "old")
        self._build_tree(stage, "new")
        record = transition.plan_transition(
            kind="build",
            scope_id="build-synthetic",
            publication_id="new",
            marker_path=marker,
            targets=(("build", live, stage, backup),),
        )
        return record, {"live": live, "stage": stage, "backup": backup}, marker

    def test_complete_transition_installs_one_coherent_generation(self) -> None:
        record, paths = self._planned()
        transition.run_transition(self.marker, record)
        self.assertFalse(self.marker.exists())
        for name in transition.RUNTIME_TARGETS:
            self.assertFalse(paths[f"{name}_stage"].exists())
            self.assertFalse(paths[f"{name}_backup"].exists())
        self.assertEqual((paths["thumbs_live"] / "value.txt").read_text(encoding="utf-8"), "new")

    def test_complete_build_transition_installs_one_coherent_generation(self) -> None:
        record, paths, marker = self._planned_build_under(self.root)

        transition.run_transition(marker, record)

        self.assertFalse(marker.exists())
        self.assertFalse(paths["stage"].exists())
        self.assertFalse(paths["backup"].exists())
        self.assertEqual(
            json.loads((paths["live"] / transition.BUILD_PUBLICATION_FILE).read_text(encoding="utf-8")),
            {"publication_id": "new"},
        )

    def test_every_interrupted_build_rename_recovers_the_old_generation(self) -> None:
        for failure_at in (1, 2):
            with self.subTest(failure_at=failure_at):
                root = self.root / f"build-fault-{failure_at}"
                root.mkdir()
                record, paths, marker = self._planned_build_under(root)
                real_rename = transition._rename
                calls = 0

                def fail_at_selected_rename(source: Path, target: Path) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == failure_at:
                        raise OSError(f"synthetic build interruption {failure_at}")
                    real_rename(source, target)

                with patch.object(transition, "_rename", side_effect=fail_at_selected_rename):
                    with self.assertRaisesRegex(OSError, "synthetic build interruption"):
                        transition.run_transition(marker, record)

                self.assertEqual(transition.resolve_pending_transition(marker), "restored_old")
                self.assertEqual(
                    json.loads(
                        (paths["live"] / transition.BUILD_PUBLICATION_FILE).read_text(encoding="utf-8")
                    ),
                    {"publication_id": "old"},
                )

    def test_completed_phases_follow_nested_target_parent_synchronization(self) -> None:
        record, paths, marker = self._planned_build_under(self.root, nested=True)
        events: list[tuple[str, object]] = []
        real_sync = transition._fsync_dir
        real_write = transition._write_marker_unlocked

        def record_sync(path: Path) -> None:
            events.append(("sync", path))
            real_sync(path)

        def record_phase(path: Path, current: object) -> None:
            events.append(("phase", current["phase"]))  # type: ignore[index]
            real_write(path, current)  # type: ignore[arg-type]

        with (
            patch.object(transition, "_fsync_dir", side_effect=record_sync),
            patch.object(transition, "_write_marker_unlocked", side_effect=record_phase),
        ):
            transition.run_transition(marker, record)

        parent = paths["live"].parent
        for completed_phase in ("backed_up_build", "installed_build"):
            phase_index = events.index(("phase", completed_phase))
            prior_phase = (
                "intent_backup_build"
                if completed_phase == "backed_up_build"
                else "intent_install_build"
            )
            prior_index = events.index(("phase", prior_phase))
            self.assertIn(("sync", parent), events[prior_index + 1 : phase_index])

    def test_entry_synchronizes_live_and_stage_contents_before_prepared_marker(self) -> None:
        record, paths, marker = self._planned_build_under(self.root, nested=True)
        for slot in (paths["live"], paths["stage"]):
            nested = slot / "nested"
            nested.mkdir()
            (nested / "metadata.txt").write_text("durable\n", encoding="utf-8")
        record = transition.plan_transition(
            kind="build",
            scope_id="build-synthetic",
            publication_id="new",
            marker_path=marker,
            targets=(("build", paths["live"], paths["stage"], paths["backup"]),),
        )

        events: list[tuple[str, object]] = []
        expected_files = {
            entry
            for slot in (paths["live"], paths["stage"])
            for entry in slot.rglob("*")
            if entry.is_file()
        }
        expected_directories = {
            entry
            for slot in (paths["live"], paths["stage"])
            for entry in slot.rglob("*")
            if entry.is_dir()
        }
        for slot in (paths["live"], paths["stage"]):
            expected_directories.add(slot)
            expected_directories.update(transition._ancestor_chain(slot.parent, marker.parent))
        current = marker.parent
        while True:
            expected_directories.add(current)
            parent = current.parent
            if parent == current:
                break
            current = parent

        real_file_sync = transition._fsync_regular_file
        real_dir_sync = transition._fsync_dir
        real_write = transition._write_marker_unlocked

        def record_file_sync(path: Path) -> None:
            events.append(("file-sync", path))
            real_file_sync(path)

        def record_dir_sync(path: Path) -> None:
            events.append(("dir-sync", path))
            real_dir_sync(path)

        def record_phase(path: Path, current: object) -> None:
            events.append(("phase", current["phase"]))  # type: ignore[index]
            real_write(path, current)  # type: ignore[arg-type]

        with (
            patch.object(transition, "_fsync_regular_file", side_effect=record_file_sync),
            patch.object(transition, "_fsync_dir", side_effect=record_dir_sync),
            patch.object(transition, "_write_marker_unlocked", side_effect=record_phase),
        ):
            transition.run_transition(marker, record)

        prepared_index = events.index(("phase", "prepared"))
        observed_files = {
            value for kind, value in events[:prepared_index] if kind == "file-sync"
        }
        observed_directories = {
            value for kind, value in events[:prepared_index] if kind == "dir-sync"
        }
        self.assertEqual(observed_files, expected_files)
        self.assertEqual(observed_directories, expected_directories)

    def test_input_content_sync_failure_leaves_no_marker_or_mutation(self) -> None:
        record, paths, marker = self._planned_build_under(self.root)
        rejected = paths["stage"] / "app.js"
        real_sync = transition._fsync_regular_file

        def fail_selected_file(path: Path) -> None:
            if path == rejected:
                raise OSError("synthetic input-content sync failure")
            real_sync(path)

        with patch.object(transition, "_fsync_regular_file", side_effect=fail_selected_file):
            with self.assertRaisesRegex(OSError, "input-content sync failure"):
                transition.run_transition(marker, record)

        self.assertFalse(marker.exists())
        self.assertEqual(
            json.loads((paths["live"] / transition.BUILD_PUBLICATION_FILE).read_text()),
            {"publication_id": "old"},
        )
        self.assertEqual(
            json.loads((paths["stage"] / transition.BUILD_PUBLICATION_FILE).read_text()),
            {"publication_id": "new"},
        )
        self.assertFalse(paths["backup"].exists())

    def test_entry_revalidates_identity_after_content_synchronization(self) -> None:
        record, paths, marker = self._planned_build_under(self.root)
        real_sync = transition._synchronize_entry_targets

        def mutate_after_sync(marker_path: Path, current: object) -> None:
            real_sync(marker_path, current)  # type: ignore[arg-type]
            (paths["stage"] / "app.js").write_text("changed after synchronization\n")

        with patch.object(transition, "_synchronize_entry_targets", side_effect=mutate_after_sync):
            with self.assertRaises(transition.TransitionCorruptionError):
                transition.run_transition(marker, record)

        self.assertFalse(marker.exists())
        self.assertTrue(paths["live"].exists())
        self.assertTrue(paths["stage"].exists())
        self.assertFalse(paths["backup"].exists())

    def test_target_parent_sync_failure_leaves_an_intent_phase_recoverable(self) -> None:
        record, paths, marker = self._planned_build_under(self.root, nested=True)
        parent = paths["live"].parent
        real_sync = transition._fsync_dir
        failed = False

        def fail_first_post_rename_target_parent_sync(path: Path) -> None:
            nonlocal failed
            if path == parent and paths["backup"].exists() and not failed:
                failed = True
                raise OSError("synthetic target-parent sync failure")
            real_sync(path)

        with patch.object(
            transition,
            "_fsync_dir",
            side_effect=fail_first_post_rename_target_parent_sync,
        ):
            with self.assertRaisesRegex(OSError, "target-parent sync failure"):
                transition.run_transition(marker, record)

        visible = transition.read_marker(marker)
        self.assertIsNotNone(visible)
        self.assertEqual(visible["phase"], "intent_backup_build")  # type: ignore[index]
        self.assertEqual(transition.resolve_pending_transition(marker), "restored_old")
        self.assertEqual(
            json.loads((paths["live"] / transition.BUILD_PUBLICATION_FILE).read_text(encoding="utf-8")),
            {"publication_id": "old"},
        )

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

    def test_every_marker_publication_interruption_reaches_a_coherent_terminal_state(self) -> None:
        outcomes = {"old": 0, "new": 0}
        phase_count = len(transition.phase_sequence(transition.RUNTIME_TARGETS))
        for failure_at in range(1, phase_count + 1):
            for timing in ("before", "after"):
                with self.subTest(failure_at=failure_at, timing=timing):
                    root = self.root / f"marker-{failure_at}-{timing}"
                    root.mkdir()
                    record, _paths, marker = self._planned_under(root)
                    real_write = transition._write_marker_unlocked
                    calls = 0

                    def interrupt_selected_publication(
                        path: Path,
                        current: object,
                    ) -> None:
                        nonlocal calls
                        calls += 1
                        if calls == failure_at and timing == "before":
                            raise OSError(f"synthetic marker interruption {failure_at} before")
                        real_write(path, current)  # type: ignore[arg-type]
                        if calls == failure_at and timing == "after":
                            raise OSError(f"synthetic marker interruption {failure_at} after")

                    with patch.object(
                        transition,
                        "_write_marker_unlocked",
                        side_effect=interrupt_selected_publication,
                    ):
                        with self.assertRaisesRegex(OSError, "synthetic marker interruption"):
                            transition.run_transition(marker, record)

                    if marker.exists():
                        transition.resolve_pending_transition(marker)
                    self.assertFalse(marker.exists())

                    expected = (
                        "new"
                        if (failure_at == phase_count or (failure_at == phase_count - 1 and timing == "after"))
                        else "old"
                    )
                    outcomes[expected] += 1
                    expected_identity_key = "new_identity" if expected == "new" else "old_identity"
                    for target in record["targets"]:  # type: ignore[index]
                        self.assertEqual(
                            transition.identity_of(Path(target["live"])),
                            target[expected_identity_key],
                        )

        self.assertEqual(outcomes, {"old": 27, "new": 3})

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

    def test_process_death_at_precommit_and_postcommit_phases_recovers_coherently(self) -> None:
        context = multiprocessing.get_context("fork")

        for phase, expected_outcome, expected_identity in (
            ("installed_thumbs", "restored_old", "old_identity"),
            ("new_verified", "kept_new", "new_identity"),
        ):
            with self.subTest(phase=phase):
                root = self.root / f"process-death-{phase}"
                root.mkdir()
                record, _paths, marker = self._planned_under(root)
                reached = context.Event()
                process = context.Process(
                    target=_run_transition_to_phase,
                    args=(str(marker), record, phase, reached),
                )

                try:
                    process.start()
                    self.assertTrue(reached.wait(20), "child did not reach durable phase")
                    process.kill()
                    process.join(10)
                    self.assertEqual(process.exitcode, -signal.SIGKILL)
                finally:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)

                visible = transition.read_marker(marker)
                self.assertIsNotNone(visible)
                self.assertEqual(visible["phase"], phase)  # type: ignore[index]
                self.assertEqual(transition.resolve_pending_transition(marker), expected_outcome)
                self.assertIsNone(transition.resolve_pending_transition(marker))
                self.assertFalse(marker.exists())

                for target in record["targets"]:  # type: ignore[index]
                    self.assertEqual(
                        transition.identity_of(Path(target["live"])),
                        target[expected_identity],
                    )
                    self.assertFalse(Path(target["stage"]).exists())
                    self.assertFalse(Path(target["backup"]).exists())

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

    def test_plan_rejects_transition_control_paths_and_namespaces(self) -> None:
        for control_index in range(4):
            for slot_name in ("live", "stage", "backup"):
                with self.subTest(control_index=control_index, slot=slot_name):
                    root = self.root / f"control-{control_index}-{slot_name}"
                    root.mkdir()
                    marker = transition.marker_path_for(root, "synthetic")
                    live = root / "build"
                    stage, backup = transition.stage_and_backup_for(live, "token")
                    self._build_tree(live, "old")
                    self._build_tree(stage, "new")
                    candidates = (
                        marker,
                        root / transition.LOCK_FILE_NAME,
                        transition.marker_path_for(root, "other"),
                        root / f"{marker.name}.{'0' * 32}.tmp",
                    )
                    slots = {"live": live, "stage": stage, "backup": backup}
                    slots[slot_name] = candidates[control_index]

                    with self.assertRaisesRegex(
                        transition.TransitionCorruptionError,
                        "control|reserved",
                    ):
                        transition.plan_transition(
                            kind="build",
                            scope_id="synthetic",
                            publication_id="new",
                            marker_path=marker,
                            targets=(("build", slots["live"], slots["stage"], slots["backup"]),),
                        )

    def test_plan_allows_marker_like_data_name_outside_the_reserved_namespace(self) -> None:
        live = self.root / "asset.transition.json.data"
        stage, backup = transition.stage_and_backup_for(live, "token")
        self._build_tree(live, "old")
        self._build_tree(stage, "new")

        record = transition.plan_transition(
            kind="build",
            scope_id="synthetic",
            publication_id="new",
            marker_path=self.marker,
            targets=(("build", live, stage, backup),),
        )

        self.assertEqual(record["phase"], "prepared")

    def test_run_rejects_a_forged_control_alias_before_mutation(self) -> None:
        record, paths, marker = self._planned_build_under(self.root)
        forged = json.loads(json.dumps(record))
        forged["targets"][0]["live"] = str(marker)
        forged["targets"][0]["old_existed"] = False
        forged["targets"][0]["old_identity"] = dict(transition.ABSENT)
        staged_before = transition.identity_of(paths["stage"])

        with self.assertRaisesRegex(transition.TransitionCorruptionError, "control|reserved"):
            transition.run_transition(marker, forged)

        self.assertFalse(marker.exists())
        self.assertEqual(transition.identity_of(paths["stage"]), staged_before)

    def test_plan_allows_nested_but_disjoint_runtime_target_parents(self) -> None:
        token = transition.new_transition_token()
        targets = []
        for name, live in (
            ("db", self.root / "state" / "runtime.db"),
            ("thumbs", self.root / "assets" / "thumbs"),
            ("photos", self.root / "assets" / "photos"),
        ):
            live.parent.mkdir(parents=True, exist_ok=True)
            stage, backup = transition.stage_and_backup_for(live, token)
            if name == "db":
                create_runtime_database(live, publication_id="old")
                create_runtime_database(stage, publication_id="new")
            else:
                self._tree(live, "old")
                self._tree(stage, "new")
            targets.append((name, live, stage, backup))

        record = transition.plan_transition(
            kind="runtime",
            scope_id="synthetic",
            publication_id="new",
            marker_path=self.marker,
            targets=tuple(targets),
        )

        self.assertEqual([target["name"] for target in record["targets"]], list(transition.RUNTIME_TARGETS))

    def test_plan_rejects_ancestor_descendant_target_slots(self) -> None:
        token = transition.new_transition_token()
        db_live = self.root / "db"
        db_stage, db_backup = transition.stage_and_backup_for(db_live, token)
        create_runtime_database(db_live, publication_id="old")
        create_runtime_database(db_stage, publication_id="new")

        thumbs_live = self.root / "thumbs"
        thumbs_stage, thumbs_backup = transition.stage_and_backup_for(thumbs_live, token)
        self._tree(thumbs_live / "photos", "old-photos")
        self._tree(thumbs_live / f"photos.{token}.stage", "new-photos")
        self._tree(thumbs_stage, "new-thumbs")
        photos_live = thumbs_live / "photos"
        photos_stage = thumbs_live / f"photos.{token}.stage"
        photos_backup = thumbs_live / f"photos.{token}.backup"

        with self.assertRaisesRegex(transition.TransitionCorruptionError, "overlap|ancestor"):
            transition.plan_transition(
                kind="runtime",
                scope_id="synthetic",
                publication_id="new",
                marker_path=self.marker,
                targets=(
                    ("db", db_live, db_stage, db_backup),
                    ("thumbs", thumbs_live, thumbs_stage, thumbs_backup),
                    ("photos", photos_live, photos_stage, photos_backup),
                ),
            )

    def test_non_object_build_marker_is_a_controlled_postcheck_failure(self) -> None:
        _record, paths, marker = self._planned_build_under(self.root)
        (paths["stage"] / transition.BUILD_PUBLICATION_FILE).write_text("[]\n", encoding="utf-8")
        record = transition.plan_transition(
            kind="build",
            scope_id="build-synthetic",
            publication_id="new",
            marker_path=marker,
            targets=(("build", paths["live"], paths["stage"], paths["backup"]),),
        )

        with self.assertRaisesRegex(transition.TransitionPostcheckError, "JSON object"):
            transition.run_transition_with_recovery(marker, record)

        self.assertFalse(marker.exists())
        self.assertEqual(
            json.loads((paths["live"] / transition.BUILD_PUBLICATION_FILE).read_text(encoding="utf-8")),
            {"publication_id": "old"},
        )

    def test_build_postchecks_fail_closed_for_missing_malformed_or_mismatched_metadata(self) -> None:
        cases = (
            (
                "missing-entry",
                lambda stage: (stage / "app.js").unlink(),
                "without app.js",
            ),
            (
                "malformed-json",
                lambda stage: (stage / transition.BUILD_PUBLICATION_FILE).write_text(
                    "{\n", encoding="utf-8"
                ),
                "unreadable",
            ),
            (
                "mismatched-publication",
                lambda stage: (stage / transition.BUILD_PUBLICATION_FILE).write_text(
                    '{"publication_id": "other"}\n', encoding="utf-8"
                ),
                "names publication",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name):
                root = self.root / name
                root.mkdir()
                _record, paths, marker = self._planned_build_under(root)
                mutate(paths["stage"])
                record = transition.plan_transition(
                    kind="build",
                    scope_id="build-synthetic",
                    publication_id="new",
                    marker_path=marker,
                    targets=(("build", paths["live"], paths["stage"], paths["backup"]),),
                )

                with self.assertRaisesRegex(transition.TransitionPostcheckError, message):
                    transition.run_transition_with_recovery(marker, record)

                self.assertFalse(marker.exists())
                self.assertEqual(
                    json.loads(
                        (paths["live"] / transition.BUILD_PUBLICATION_FILE).read_text(
                            encoding="utf-8"
                        )
                    ),
                    {"publication_id": "old"},
                )

    def test_postcommit_build_cleanup_interruption_keeps_verified_new_generation(self) -> None:
        record, paths, marker = self._planned_build_under(self.root)

        with patch.object(
            transition,
            "_delete_residue",
            side_effect=OSError("synthetic postcommit cleanup interruption"),
        ):
            with self.assertRaisesRegex(OSError, "cleanup interruption"):
                transition.run_transition(marker, record)

        visible = transition.read_marker(marker)
        self.assertIsNotNone(visible)
        self.assertEqual(visible["phase"], "cleanup")  # type: ignore[index]
        self.assertEqual(transition.resolve_pending_transition(marker), "kept_new")
        self.assertEqual(
            json.loads(
                (paths["live"] / transition.BUILD_PUBLICATION_FILE).read_text(encoding="utf-8")
            ),
            {"publication_id": "new"},
        )

    def test_identity_rejects_special_entries_instead_of_opening_them(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as posix_name:
            target = Path(posix_name) / "target"
            target.mkdir()
            os.mkfifo(target / "pipe")

            with self.assertRaisesRegex(transition.TransitionCorruptionError, "non-regular"):
                transition.identity_of(target)

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

    def test_new_transition_root_is_durably_linked_from_its_parent(self) -> None:
        owned_root = self.root / "outer" / "owned"
        marker = transition.marker_path_for(owned_root, "synthetic")
        observed: list[Path] = []
        real_sync = transition._fsync_dir

        def record(path: Path) -> None:
            observed.append(path)
            real_sync(path)

        with patch.object(transition, "_fsync_dir", side_effect=record):
            with transition.transition_lock(marker):
                pass

        self.assertIn(self.root, observed)
        self.assertIn(self.root / "outer", observed)


    def test_root_creation_retry_resynchronizes_prior_ancestor_links(self) -> None:
        owned_root = self.root / "outer" / "owned"
        marker = transition.marker_path_for(owned_root, "synthetic")
        real_sync = transition._fsync_dir
        failed = False

        def fail_once(path: Path) -> None:
            nonlocal failed
            if path == self.root and not failed:
                failed = True
                raise OSError("synthetic ancestor sync failure")
            real_sync(path)

        with patch.object(transition, "_fsync_dir", side_effect=fail_once):
            with self.assertRaises(OSError):
                with transition.transition_lock(marker):
                    pass

        observed: list[Path] = []

        def record(path: Path) -> None:
            observed.append(path)
            real_sync(path)

        with patch.object(transition, "_fsync_dir", side_effect=record):
            with transition.transition_lock(marker):
                pass

        self.assertIn(self.root, observed)
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

    def test_marker_path_must_be_canonical_absolute_without_parent_segments(self) -> None:
        record, paths = self._planned()
        noncanonical = self.root / "nested" / ".." / self.marker.name

        with patch.object(
            transition,
            "identity_of",
            side_effect=AssertionError("noncanonical marker must fail before target hashing"),
        ):
            with self.assertRaisesRegex(transition.TransitionCorruptionError, "canonical absolute"):
                transition.plan_transition(
                    kind="runtime",
                    scope_id="synthetic",
                    publication_id="new",
                    marker_path=noncanonical,
                    targets=tuple(
                        (
                            name,
                            paths[f"{name}_live"],
                            paths[f"{name}_stage"],
                            paths[f"{name}_backup"],
                        )
                        for name in transition.RUNTIME_TARGETS
                    ),
                )

        with self.assertRaisesRegex(transition.TransitionCorruptionError, "canonical absolute"):
            transition.run_transition(noncanonical, record)

        self.assertFalse(self.marker.exists())
        self.assertTrue(paths["db_live"].exists())
        self.assertTrue(paths["db_stage"].exists())
        self.assertFalse(paths["db_backup"].exists())


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
