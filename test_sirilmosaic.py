from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import time
import tempfile
import zipfile
import tkinter as tk
import unittest
import numpy as np
from queue import Queue
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock, patch

import sirilmosaic
from sirilmosaic_gui import (
    RunProgressEstimator,
    SirilMosaicApp,
    HELP_SECTIONS,
    HELP_SECTION_ORDER,
    cohort_balance_rows,
    completion_summary,
    completed_verification_failure,
    compare_run_summaries,
    quality_metric_summary,
    crop_requires_confirmation,
    report_history_summary,
    terminate_siril_descendants,
)


class FakeSiril:
    def __init__(self, failed_command: str | None = None) -> None:
        self.commands: list[str] = []
        self.failed_command = failed_command

    def Execute(self, command: str) -> bool:
        self.commands.append(command)
        if command.startswith("cd "):
            target = Path(command.removeprefix("cd ").strip('"'))
            self.cwd = target if target.is_absolute() else (self.cwd / target).resolve()
        elif command.startswith("stack ") and "-out=../substack_" in command:
            output_name = command.split("-out=../", 1)[1].split()[0]
            cards = [
                "SIMPLE  =                    T",
                "BITPIX  =                   32",
                "NAXIS   =                    2",
                "NAXIS1  =                   10",
                "NAXIS2  =                   10",
                "END",
            ]
            header = b"".join(card.ljust(80).encode("ascii") for card in cards)
            (self.cwd.parent / f"{output_name}.fit").write_bytes(header.ljust(2880, b" "))
        return command != self.failed_command

    def GetData(self) -> list[str]:
        return [f"log: rejected {self.failed_command}", "status: error"]


class SirilMosaicTests(unittest.TestCase):
    _tk_root = None

    @classmethod
    def gui_root(cls):
        if cls._tk_root is None:
            cls._tk_root = tk.Tk()
            cls._tk_root.withdraw()
        return tk.Toplevel(cls._tk_root)

    @classmethod
    def tearDownClass(cls):
        if cls._tk_root is not None:
            cls._tk_root.destroy()
            cls._tk_root = None

    def setUp(self) -> None:
        settings_directory = tempfile.TemporaryDirectory()
        self.addCleanup(settings_directory.cleanup)
        appdata = patch.dict('os.environ', {'APPDATA': settings_directory.name})
        appdata.start()
        self.addCleanup(appdata.stop)
        state = {
            name: value for name, value in vars(sirilmosaic).items()
            if not name.startswith('__') and (
                value is None or isinstance(value, (str, int, float, bool, Path))
                or name in ('app', 'quality_report')
            )
        }
        self.addCleanup(lambda: vars(sirilmosaic).update(state))
        sirilmosaic.app = None
        sirilmosaic.quality_report = None

    def test_grouping_creates_requested_nonempty_groups_and_restores_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, _ = self.make_project(Path(directory), light_count=5)
            sources = set(sirilmosaic.discover_light_files(workdir))
            with patch.multiple(sirilmosaic, workdir=workdir, SubStack_nb=4, debug=False):
                sirilmosaic.group_files(workdir, list(sources))
                for group_number in range(1, 5):
                    group = workdir / 'Lights_sorted' / f'group_{group_number}'
                    manifest = json.loads((group / sirilmosaic.SOURCE_MANIFEST).read_text())
                    self.assertGreater(len(manifest), 0)
                    self.assertTrue(all(name.startswith('frame_') for name in manifest))
                    sirilmosaic.cleanup(group_number, restore_sources=True)
            self.assertEqual(set(sirilmosaic.discover_light_files(workdir)), sources)

    @unittest.skipUnless(os.environ.get('SIRIL_TEST_CLI'), 'Set SIRIL_TEST_CLI for real Siril conversion')
    def test_real_siril_preserves_numbered_conversion_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lights = root / 'lights'
            process = root / 'process'
            lights.mkdir()
            process.mkdir()
            for number, (suffix, exposure) in enumerate(zip(('.fit', '.fts', '.fits'), (60, 300, 120)), 1):
                source = lights / f'frame_{number:05d}{suffix}'
                self.write_fits_layer_header(source, 1, exposure)
                with source.open('ab') as stream:
                    stream.write(np.ones((10, 10), dtype='>i4').tobytes().ljust(2880, b'\0'))
            script = root / 'convert.ssf'
            script.write_text(
                f'requires 1.3.6\nsetext fit\ncd "{lights.as_posix()}"\nconvert light -out=../process\n',
                encoding='utf-8',
            )
            result = subprocess.run(
                [os.environ['SIRIL_TEST_CLI'], '-s', str(script)], cwd=root,
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-4000:])
            self.assertEqual([
                sirilmosaic.read_exposure_seconds(process / f'light_{number:05d}.fit')
                for number in range(1, 4)
            ], [60, 300, 120])

    @unittest.skipUnless(os.environ.get('SIRIL_TEST_CLI'), 'Set SIRIL_TEST_CLI for real Siril coverage transform test')
    def test_real_siril_resamples_rotated_coverage_landmarks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / 'process'
            local_dir = root / 'local_maps'
            process.mkdir()
            local_dir.mkdir()
            (process / 'pp_light_.seq').write_text(
                "S 'pp_light_' 1 2 2 5 0 7 1 0 0\n"
                'L 3\n'
                'I 1 1 5,5\n'
                'I 2 1 5,5\n'
                'R1 1 1 1 1 1 1 H 1 0 0 0 1 0 0 0 1\n'
                'R1 1 1 1 1 1 1 H 0 -1 4 1 0 0 0 0 1\n',
                encoding='ascii',
            )
            first_map = np.zeros((5, 5), dtype=np.float32)
            second_map = np.zeros((5, 5), dtype=np.float32)
            first_map[1, 1] = 11
            second_map[1, 1] = 22
            sirilmosaic._write_coverage_fits(
                local_dir / 'integration_time_map_substack_1.fit', first_map, unit='s'
            )
            sirilmosaic._write_coverage_fits(
                local_dir / 'integration_time_map_substack_2.fit', second_map, unit='s'
            )
            script = root / 'register_coverage.ssf'

            def execute(command: str) -> None:
                if not command.startswith('seqapplyreg '):
                    return
                script.write_text(
                    'requires 1.3.6\n'
                    'setext fit\n'
                    f'cd "{local_dir.as_posix()}"\n'
                    f'{command}\n',
                    encoding='utf-8',
                )
                result = subprocess.run(
                    [os.environ['SIRIL_TEST_CLI'], '-s', str(script)],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=90,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stdout[-4000:] + result.stderr[-4000:],
                )

            with patch.object(sirilmosaic, 'execute_siril', side_effect=execute):
                registered, records = sirilmosaic.register_substack_coverage_maps(
                    process, local_dir, 2
                )

            self.assertEqual([record['number'] for record in records], [1, 2])
            self.assertEqual(registered[1].shape, (5, 5))
            self.assertEqual(registered[2].shape, (5, 5))
            expected_first = np.zeros((5, 5), dtype=np.float32)
            expected_second = np.zeros((5, 5), dtype=np.float32)
            expected_first[1, 1] = 11
            expected_second[3, 1] = 22
            np.testing.assert_array_equal(registered[1], expected_first)
            np.testing.assert_array_equal(registered[2], expected_second)

            composed = sirilmosaic._compose_coverage_arrays(registered, records)
            landmark_rows, landmark_columns = np.where(composed == 22)
            self.assertEqual(len(landmark_rows), 1)
            master_path = root / 'master.fit'
            integration_path = root / 'integration.fit'
            sirilmosaic._write_coverage_fits(master_path, (composed > 0).astype(np.float32))
            sirilmosaic._write_coverage_fits(integration_path, composed, unit='s')
            crop_plan = sirilmosaic.build_crop_plan(master_path, integration_path, 100)
            self.assertEqual(
                (crop_plan['master_width'], crop_plan['master_height']),
                (composed.shape[1], composed.shape[0]),
            )
            self.assertEqual(crop_plan['crop_bounds'], {
                'x': int(landmark_columns[0]),
                'y': int(landmark_rows[0]),
                'width': 1,
                'height': 1,
            })
            self.assertEqual(crop_plan['crop_siril_selection'], {
                'x': int(landmark_columns[0]),
                'y': composed.shape[0] - int(landmark_rows[0]) - 1,
                'width': 1,
                'height': 1,
            })

    @unittest.skipUnless(os.environ.get('SIRIL_TEST_LIGHTS'), 'Set SIRIL_TEST_LIGHTS for copied-frame smoke test')
    def test_real_pipeline_on_copied_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = sorted(Path(os.environ['SIRIL_TEST_LIGHTS']).glob('*.fit'))[:6]
            self.assertEqual(len(sources), 6)
            for source in sources:
                shutil.copy2(source, root / source.name)
            originals = {path.name: path.read_bytes() for path in root.glob('*.fit')}
            command = [
                sys.executable, str(Path(sirilmosaic.__file__)), '--workdir', str(root),
                '--siril-exe', os.environ['SIRIL_TEST_EXE'], '--substacks', '1',
                '--no-drizzle', '--no-cosmetic-correction', '--no-overlap-normalization',
                '--background-method', 'off', '--bayer-orientation', 'top-down',
                '--coverage-map', '--auto-crop-master', '--auto-crop-coverage-percent', '50',
                '--adaptive-quality-filtering', '--quality-filter-sigma', '3',
                '--mosaic-aware-star-count', '--skip-failed-frames', '--cpus', '4',
                '--plate-solve-order', '3', '--no-plate-solve-downscale',
                '--rbf-smoothing', '0.5', '--background-dither',
                '--registration-transform', 'homography',
                '--registration-interpolation', 'lanczos4', '--seed', '12345',
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-4000:])
            report_path = next((root / 'Siril Mosaic Output').glob('quality_report_*.json'))
            report = json.loads(report_path.read_text())
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(report['report_schema_version'], 3)
            self.assertEqual(report['seed'], 12345)
            self.assertTrue(report['configuration_hash'])
            self.assertTrue(Path(report['input_manifest']).is_file())
            self.assertTrue(Path(report['journal_path']).is_file())
            self.assertTrue(report['environment']['siril_version'])
            self.assertTrue(all('response_tail' in command for command in report['commands']))
            self.assertTrue(report['integration']['exposure_complete'])
            self.assertEqual(report['integration']['integrated_exposure_seconds'],
                             report['integration']['stacked_frames'] * 60)
            self.assertEqual(report['selection']['effective_filters'], {'bkg': '3.0k', 'round': '3.0k', 'fwhm': '3.0k'})
            self.assertNotIn('frames', report['substacks'][0])
            master = Path(report['master']['path'])
            self.assertEqual(sirilmosaic._read_fits_dimensions(master),
                             (report['coverage']['width'], report['coverage']['height']))
            cropped = Path(report['coverage']['cropped_master_path'])
            self.assertTrue(np.isfinite(sirilmosaic._read_fits_array(cropped)).all())
            self.assertEqual({path.name: path.read_bytes() for path in root.glob('*.fit')}, originals)

    def write_fits_layer_header(
        self, path: Path, layers: int, exposure: float | None = None,
        width: int = 10, height: int = 10,
    ) -> None:
        cards = [
            "SIMPLE  =                    T",
            "BITPIX  =                   32",
            f"NAXIS   =                    {2 if layers == 1 else 3}",
            f"NAXIS1  = {width:20d}",
            f"NAXIS2  = {height:20d}",
        ]
        if layers != 1:
            cards.append(f"NAXIS3  =                    {layers}")
        if exposure is not None:
            cards.append(f"EXPTIME = {exposure:20.6f}")
        cards.append("END")
        header = b"".join(card.ljust(80).encode("ascii") for card in cards)
        path.write_bytes(header.ljust(2880, b" "))

    def make_project(self, root: Path, light_count: int = 4) -> tuple[Path, Path]:
        workdir = root / "project"
        workdir.mkdir(parents=True)
        for index in range(light_count):
            extension = ".xisf" if index % 2 else ".fit"
            panel = workdir / f"panel_{index + 1}" / "lights"
            panel.mkdir(parents=True)
            (panel / f"light{extension}").touch()
        executable = root / "siril.exe"
        executable.touch()
        return workdir, executable

    def test_siril_path_quotes_and_normalizes_spaces(self) -> None:
        self.assertEqual(
            sirilmosaic.siril_path(Path(r"W:\Astro\Cygnus Retry\cosmetized")),
            '"W:/Astro/Cygnus Retry/cosmetized"',
        )

    def test_progress_estimator_maps_native_progress_and_never_moves_backward(self) -> None:
        estimator = RunProgressEstimator()

        self.assertTrue(estimator.consume("[PROGRESS] 20.00 40.00 Stacking frames"))
        self.assertEqual(estimator.percent, 20.0)
        self.assertEqual(estimator.phase, "Stacking frames")
        self.assertTrue(estimator.consume("progress: 50.00%"))
        self.assertEqual(estimator.percent, 30.0)
        self.assertTrue(estimator.consume("progress: 10.00%"))
        self.assertEqual(estimator.percent, 30.0)
        self.assertTrue(estimator.consume("[PROGRESS] 40.00 50.00 Finalizing"))
        self.assertEqual(estimator.percent, 40.0)
        self.assertFalse(estimator.consume("log: ordinary Siril output"))

        estimator.complete()
        self.assertEqual(estimator.percent, 100.0)
        self.assertEqual(estimator.phase, "Complete")
        self.assertIsNone(estimator.file_total)

    def test_analysis_summary_helpers_report_history_quality_cohorts_and_deltas(self) -> None:
        report_path = Path("quality_report_demo.json")
        report = {
            "run_id": "demo",
            "status": "complete",
            "updated_at": "2026-09-14T12:00:00",
            "input_frames": 4,
            "integration": {"stacked_frames": 3, "integrated_hours": 0.25},
        }
        summary = report_history_summary(report_path, report)
        records = [
            {"cohort": "A", "status": "stacked", "exposure_seconds": 60, "filter_metrics": {"fwhm": {"value": 2, "threshold": 3}}},
            {"cohort": "A", "status": "rejected", "exposure_seconds": 60, "filter_metrics": {"fwhm": {"value": 4, "threshold": 3}}},
            {"cohort": "B", "status": "stacked", "exposure_seconds": 120, "filter_metrics": {"fwhm": {"value": 3, "threshold": 3}}},
        ]

        metric = quality_metric_summary(records, "fwhm")
        cohorts = cohort_balance_rows(records)
        comparison = compare_run_summaries(summary, {**summary, "run_id": "later", "stacked_frames": 4})

        self.assertEqual(summary["stacked_frames"], 3)
        self.assertEqual(metric["count"], 3)
        self.assertEqual(metric["threshold"], 3)
        self.assertEqual(metric["comparison"], "<=")
        self.assertEqual(metric["distinct_threshold_count"], 1)
        self.assertEqual([row["cohort"] for row in cohorts], ["A", "B"])
        self.assertEqual(comparison["stacked_frames_delta"], 1)

    def test_quality_metric_summary_ignores_nonpositive_placeholders(self) -> None:
        summary = quality_metric_summary([
            {"filter_metrics": {"fwhm": {"value": 0}}},
            {"filter_metrics": {"fwhm": {"value": 2.0, "threshold": 3.0}}},
            {"filter_metrics": {"fwhm": {"value": -1.0}}},
        ], "fwhm")
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["ignored_nonpositive"], 2)

    def test_quality_metric_summary_discloses_varying_thresholds_and_direction(self) -> None:
        summary = quality_metric_summary([
            {"filter_metrics": {"stars": {"value": 30, "threshold": 20, "comparison": ">="}}},
            {"filter_metrics": {"stars": {"value": 40, "threshold": 30, "comparison": ">="}}},
        ], "stars")
        self.assertEqual(summary["comparison"], ">=")
        self.assertEqual(summary["threshold"], 25)
        self.assertEqual(summary["threshold_count"], 2)
        self.assertEqual(summary["distinct_threshold_count"], 2)
        self.assertEqual((summary["threshold_minimum"], summary["threshold_maximum"]), (20, 30))

    def test_completion_summary_reports_accounting_and_artifacts(self) -> None:
        text = completion_summary({
            "input_frames": 10,
            "integration": {"stacked_frames": 8, "integrated_hours": 1.25},
            "verification_status": "PASS",
            "master": {"path": "master.fit"},
            "coverage": {"cropped_master_path": "crop.fit"},
        }, Path("quality.json"), Path("run.log"))
        self.assertIn("8 stacked of 10 input (2 not stacked)", text)
        self.assertIn("Verification: PASS", text)
        self.assertIn("Master: master.fit", text)
        self.assertIn("Crop: crop.fit", text)
        self.assertIn("Quality report: quality.json", text)

    def test_completion_dialog_opens_configured_output_folder(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp.__new__(SirilMosaicApp)
            app.root = root
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                dialog = app.show_completion_dialog("Completed.", output)
                buttons = [
                    child
                    for frame in dialog.winfo_children()
                    for child in frame.winfo_children()
                    if isinstance(child, ttk.Button)
                ]
                folder_button = next(button for button in buttons if button.cget("text") == "Open Output Folder")
                style = ttk.Style(dialog)
                self.assertEqual(folder_button.cget("style"), "Completion.TButton")
                self.assertEqual(style.lookup("Completion.TButton", "background"), "#24272e")
                self.assertTrue(all(button.cget("style") == "Completion.TButton" for button in buttons))
                with patch("sirilmosaic_gui.os.startfile", create=True) as startfile:
                    folder_button.invoke()
                startfile.assert_called_once_with(str(output))
                dialog.destroy()
        finally:
            root.destroy()

    def test_completion_dialog_omits_folder_action_when_directory_is_missing(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp.__new__(SirilMosaicApp)
            app.root = root
            dialog = app.show_completion_dialog(
                "Completed.", Path(tempfile.gettempdir()) / "missing-stack-output-folder"
            )
            buttons = [
                child
                for frame in dialog.winfo_children()
                for child in frame.winfo_children()
                if isinstance(child, ttk.Button)
            ]

            self.assertEqual([button.cget("text") for button in buttons], ["Close"])
            dialog.destroy()
        finally:
            root.destroy()

    def test_quality_explorer_redraws_cached_histogram_on_canvas_resize(self) -> None:
        app = SirilMosaicApp.__new__(SirilMosaicApp)
        summary = {"metric": "fwhm", "values": [2.1, 2.4]}
        app.quality_summary_data = summary
        with patch.object(app, "_draw_quality_histogram") as draw:
            app._quality_canvas_configured()
        draw.assert_called_once_with(summary)

    def test_threshold_sync_updates_basic_selection_and_disables_adaptive(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            app.threshold_vars["background"].set(83)
            app.threshold_vars["roundness"].set(87)
            app.threshold_vars["fwhm"].set(91)
            app.threshold_vars["stars"].set(79)
            app.adaptive_quality_filtering.set(True)
            app.sync_threshold_to_frame_selection()
            self.assertEqual(app.filter_background.get(), 83)
            self.assertEqual(app.filter_roundness.get(), 87)
            self.assertEqual(app.filter_fwhm.get(), 91)
            self.assertEqual(app.filter_stars.get(), 79)
            self.assertFalse(app.adaptive_quality_filtering.get())
        finally:
            root.destroy()

    def test_frame_inspector_has_filter_and_clear_controls(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)

            def button_labels(widget: tk.Misc) -> list[str]:
                labels = []
                for child in widget.winfo_children():
                    if isinstance(child, ttk.Button):
                        labels.append(str(child.cget("text")))
                    labels.extend(button_labels(child))
                return labels

            frame_tab = app.frame_inspector_tree.master.master.master
            labels = button_labels(frame_tab)
            self.assertIn("Filter", labels)
            self.assertIn("Clear Sort & Filter", labels)
        finally:
            root.destroy()

    def test_run_review_keeps_cleanup_after_filter_controls(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            summary_frame = app.review_summary.master
            verification_bar = next(
                child for child in summary_frame.winfo_children()
                if isinstance(child, ttk.Frame)
            )
            filter_group = next(
                child for child in verification_bar.winfo_children()
                if isinstance(child, ttk.Frame)
                and any(
                    isinstance(button, ttk.Button) and button.cget("text") == "Filter"
                    for button in child.winfo_children()
                )
            )
            filter_column = int(filter_group.grid_info()["column"])
            cleanup = next(
                child for child in verification_bar.winfo_children()
                if isinstance(child, ttk.Button) and child.cget("text") == "Cleanup Review"
            )
            self.assertGreater(int(cleanup.grid_info()["column"]), filter_column)
            visible_buttons = {
                str(child.cget("text"))
                for child in verification_bar.winfo_children()
                if isinstance(child, ttk.Button)
            }
            self.assertNotIn("Replay Filters", visible_buttons)
            self.assertNotIn("Integrity Scan", visible_buttons)
        finally:
            root.destroy()

    def test_visual_qa_is_secondary_run_review_window(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            tab_labels = [
                app.settings_notebook.tab(tab_id, "text")
                for tab_id in app.settings_notebook.tabs()
            ]
            self.assertNotIn("Visual QA", tab_labels)

            def find_button(widget: tk.Misc, label: str) -> ttk.Button | None:
                for child in widget.winfo_children():
                    if isinstance(child, ttk.Button) and child.cget("text") == label:
                        return child
                    result = find_button(child, label)
                    if result is not None:
                        return result
                return None

            button = find_button(app.review_summary.master, "Run Visual QA...")
            self.assertIsNotNone(button)
            button.invoke()
            first_window = app.visual_qa_window
            self.assertIsNotNone(first_window)
            self.assertEqual(first_window.title(), "Visual QA")
            app.open_visual_qa_window()
            self.assertIs(app.visual_qa_window, first_window)
            app.close_visual_qa_window()
            self.assertIsNone(app.visual_qa_window)
        finally:
            root.destroy()

    def test_recovery_actions_are_on_main_action_row(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            action_frame = app.start_button.master
            recovery_group = next(
                child for child in action_frame.winfo_children()
                if isinstance(child, ttk.Frame)
                and {
                    str(button.cget("text"))
                    for button in child.winfo_children()
                    if isinstance(button, ttk.Button)
                } == {
                    "Checkpoint Status",
                    "Discard Checkpoint",
                    "Abandon Run",
                    "Run Lock Status",
                    "Break Run Lock",
                }
            )
            self.assertEqual(int(recovery_group.grid_info()["column"]), 7)
            self.assertEqual(recovery_group.grid_info()["sticky"], "e")
        finally:
            root.destroy()

    def test_treeview_headers_toggle_ascending_and_descending(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            tree = ttk.Treeview(root, columns=("value",), show="headings")
            tree.heading("value", text="Value")
            tree.insert("", "end", iid="high", values=("10",))
            tree.insert("", "end", iid="low", values=("2",))
            app._make_treeview_sortable(tree)
            app._sort_treeview(tree, "value")
            self.assertEqual(tree.get_children(), ("low", "high"))
            app._sort_treeview(tree, "value")
            self.assertEqual(tree.get_children(), ("high", "low"))
            app._clear_treeview_sort_filter(tree)
            self.assertEqual(tree.get_children(), ("high", "low"))
        finally:
            root.destroy()

    def test_treeview_filter_uses_dropdowns_for_finite_values(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            tree = ttk.Treeview(root, columns=("status", "exposure", "file", "reason"), show="headings")
            for column in tree["columns"]:
                tree.heading(column, text=column.title())
            tree.insert("", "end", iid="stacked", values=("stacked", "60.0", "M 16_001.fit", ""))
            tree.insert("", "end", iid="rejected", values=("rejected", "60.0", "M 16_002.fit", "FWHM"))
            tree.insert("", "end", iid="failed", values=("failed", "120.0", "M 16_003.fit", "Siril"))
            app._make_treeview_sortable(tree)

            def dialog_buttons(dialog: tk.Toplevel) -> list[ttk.Button]:
                result = []
                for child in dialog.winfo_children():
                    if isinstance(child, ttk.Button):
                        result.append(child)
                    elif isinstance(child, tk.Misc):
                        result.extend(dialog_buttons(child))
                return result

            app._open_treeview_filter(tree)
            root.update_idletasks()
            dialog = next(
                child for child in root.winfo_children()
                if isinstance(child, tk.Toplevel) and child.title() == "Filter table"
            )
            combos = [child for child in dialog.winfo_children() if isinstance(child, ttk.Combobox)]
            column_combo, value_combo = combos
            column_combo.set("Status")
            column_combo.event_generate("<<ComboboxSelected>>")
            root.update_idletasks()
            self.assertEqual(tuple(value_combo["values"]), ("All values", "failed", "rejected", "stacked"))
            value_combo.set("rejected")
            next(button for button in dialog_buttons(dialog) if button.cget("text") == "Apply").invoke()
            self.assertEqual(tree.get_children(), ("rejected",))

            app._clear_treeview_sort_filter(tree)
            app._open_treeview_filter(tree)
            root.update_idletasks()
            dialog = next(
                child for child in root.winfo_children()
                if isinstance(child, tk.Toplevel) and child.title() == "Filter table"
            )
            column_combo = next(child for child in dialog.winfo_children() if isinstance(child, ttk.Combobox))
            entries = [
                child for child in dialog.winfo_children()
                if isinstance(child, ttk.Entry) and not isinstance(child, ttk.Combobox)
            ]
            value_combo = [child for child in dialog.winfo_children() if isinstance(child, ttk.Combobox)][1]
            column_combo.set("File")
            column_combo.event_generate("<<ComboboxSelected>>")
            entries[0].insert(0, "M 16_002")
            self.assertFalse(value_combo.winfo_ismapped())
            next(button for button in dialog_buttons(dialog) if button.cget("text") == "Apply").invoke()
            self.assertEqual(tree.get_children(), ("rejected",))
        finally:
            root.destroy()

    def test_treeview_clear_sort_filter_restores_captured_rows(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            tree = ttk.Treeview(root, columns=("value",), show="headings")
            tree.heading("value", text="Value")
            tree.insert("", "end", iid="one", values=("one",))
            tree.insert("", "end", iid="two", values=("two",))
            app._tree_filter_snapshot[str(tree)] = app._treeview_rows(tree)
            tree.delete("one")
            app._clear_treeview_sort_filter(tree)
            self.assertEqual(tree.get_children(), ("one", "two"))
        finally:
            root.destroy()

    def test_run_history_highlights_loaded_report(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            first = Path("first.json").resolve()
            second = Path("second.json").resolve()
            history_style = ttk.Style(root)
            self.assertEqual(
                history_style.lookup("LoadedHistory.Treeview", "background", ("selected",)),
                "#2f6f4e",
            )
            first_item = app.history_tree.insert("", "end", values=("first",))
            second_item = app.history_tree.insert("", "end", values=("second",))
            app.history_item_paths = {first_item: first, second_item: second}
            app.quality_report_path = second

            app._update_loaded_history_highlight()

            self.assertFalse(app.history_tree.item(first_item, "tags"))
            self.assertEqual(app.history_tree.item(second_item, "tags"), ("loaded_run",))
        finally:
            root.destroy()

    def test_run_history_selection_activates_and_refreshes_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = SirilMosaicApp.__new__(SirilMosaicApp)
            report_path = Path(directory) / "quality_report_selected.json"
            report_path.write_text("{}", encoding="utf-8")
            app.history_tree = Mock()
            app.history_tree.selection.return_value = ("selected",)
            app.history_item_paths = {"selected": report_path}
            app.history_summary = None
            app.quality_report_path = None
            app.crop_report = Mock()
            app.status = Mock()
            app.refresh_run_review = Mock()
            app.load_crop_report = Mock()
            summary = {
                "run_id": "selected",
                "updated_at": "2026-09-14T12:00:00",
                "status": "complete",
                "input_frames": 1,
                "stacked_frames": 1,
                "rejected_frames": 0,
                "verification": "PASS",
            }
            with patch("sirilmosaic_gui.json.loads", return_value={}), patch(
                "sirilmosaic_gui.report_history_summary", return_value=summary
            ):
                app._history_selection_changed()

            self.assertEqual(app.quality_report_path, report_path)
            app.crop_report.set.assert_called_once_with(str(report_path))
            app.load_crop_report.assert_called_once_with(report_path, notify=False)
            app.refresh_run_review.assert_called_once_with(report_path)

    def test_run_history_exports_selected_experiment_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            reports = []
            for run_id in ('control', 'candidate'):
                run_dir = root_path / run_id
                run_dir.mkdir()
                master_path = run_dir / 'master.fit'
                sirilmosaic._write_coverage_fits(
                    master_path, np.ones((2, 3), dtype=np.float32)
                )
                manifest = run_dir / 'manifest.json'
                manifest.write_text(json.dumps({
                    'selected_files': [{'path': 'light.fit', 'size': 3, 'sha256': 'a' * 64}],
                }), encoding='utf-8')
                report_path = run_dir / f'quality_report_{run_id}.json'
                report_path.write_text(json.dumps({
                    'run_id': run_id, 'status': 'complete', 'input_manifest': str(manifest),
                    'master': {'path': str(master_path)},
                }), encoding='utf-8')
                reports.append(report_path)
            destination = root_path / 'experiment.json'
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                items = [app.history_tree.insert('', 'end', values=(path.stem,)) for path in reports]
                app.history_item_paths = dict(zip(items, reports))
                app.history_tree.selection_set(items)
                with patch('sirilmosaic_gui.filedialog.asksaveasfilename', return_value=str(destination)), \
                     patch('sirilmosaic_gui.simpledialog.askstring', side_effect=['control', 'Looks safer.']):
                    app.export_experiment_record()

                saved = json.loads(destination.read_text(encoding='utf-8'))
                self.assertEqual(saved['control_run_id'], 'control')
                self.assertEqual(saved['conclusion'], 'Looks safer.')
                self.assertIn('Shared input identity', app.history_compare_text.get('1.0', 'end'))
            finally:
                root.destroy()

    def test_gui_tooltip_catalog_covers_core_processing_options(self) -> None:
        self.assertIn("Enable drizzle", SirilMosaicApp.TOOLTIP_TEXT)
        self.assertIn("Adaptive quality filters", SirilMosaicApp.TOOLTIP_TEXT)
        self.assertIn("Write coverage map", SirilMosaicApp.TOOLTIP_TEXT)
        self.assertIn("Random seed (blank = generate)", SirilMosaicApp.TOOLTIP_TEXT)
        self.assertGreater(len(SirilMosaicApp.TOOLTIP_TEXT), 30)

    def test_tooltip_hide_clears_shared_popup_and_pending_callback(self) -> None:
        app = SirilMosaicApp.__new__(SirilMosaicApp)
        app.root = Mock()
        app._tooltip_after = "after-id"
        popup = Mock()
        app._tooltip_popup = popup
        app._tooltip_widget = Mock()

        app._hide_tooltip()

        app.root.after_cancel.assert_called_once_with("after-id")
        popup.destroy.assert_called_once_with()
        self.assertIsNone(app._tooltip_after)
        self.assertIsNone(app._tooltip_popup)
        self.assertIsNone(app._tooltip_widget)

    def test_help_sections_cover_each_major_module(self) -> None:
        expected = (
            "Basic", "Advanced", "Batch Queue", "Run History", "Run Review",
            "Cohort Balance", "Quality Explorer", "Threshold Lab", "Frame Inspector",
            "Coverage Inspector", "Visual QA", "Cropping Workbench",
        )
        self.assertEqual(HELP_SECTION_ORDER, expected)
        for section in expected:
            self.assertIn(section, HELP_SECTIONS)
            self.assertGreater(len(HELP_SECTIONS[section]), 100)

    def test_visual_quality_check_and_html_report_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.fit"
            coverage = root / "coverage.fit"
            crop = root / "crop.fit"
            array = np.full((20, 20), 10, dtype=np.float32)
            sirilmosaic._write_coverage_fits(master, array, unit="adu")
            sirilmosaic._write_coverage_fits(coverage, array, unit="s")
            sirilmosaic._write_coverage_fits(crop, array[2:18, 2:18], unit="adu")
            qa = sirilmosaic.visual_quality_check(master, coverage, crop)
            self.assertIn(qa["status"], {"PASS", "WARN"})
            self.assertTrue(any(check["name"] == "master_artifact" for check in qa["checks"]))

            report_path = self._write_verifiable_run(root)
            html_path = sirilmosaic.export_html_run_report(report_path)
            contents = html_path.read_text(encoding="utf-8")
            self.assertIn("<!doctype html>", contents)
            self.assertIn("Verification", contents)
            self.assertNotIn("light_01.fit", contents)

    def test_progress_estimator_interpolates_file_counts(self) -> None:
        estimator = RunProgressEstimator()

        self.assertTrue(
            estimator.consume("[PROGRESS] 20.00 40.00 files=0-5000/5000 Converting frames")
        )
        self.assertEqual(estimator.file_current, 0)
        self.assertEqual(estimator.file_total, 5000)
        self.assertTrue(estimator.consume("progress: 50.00%"))
        self.assertEqual(estimator.file_current, 2500)

        self.assertTrue(
            estimator.consume("[PROGRESS] 40.00 50.00 Source staging complete")
        )
        self.assertIsNone(estimator.file_total)

    def test_output_folder_is_excluded_from_source_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "selected output"
            previous_output = root / "Siril Mosaic Output"
            output.mkdir()
            previous_output.mkdir()
            (root / "source.xisf").touch()
            (root / "master_stack.fit").touch()
            (root / "master_stack_20260912_120000.fit").touch()
            (root / "substack_1_low_rejmap.fit").touch()
            (output / "old_master.fit").touch()
            (previous_output / "coverage_map_20260917_212535.fit").touch()
            (previous_output / "integration_time_map_20260917_212535.fit").touch()

            self.assertEqual(sirilmosaic.discover_light_files(root, (output,)), [root / "source.xisf"])

    def test_xisf_preflight_reads_pattern_and_warns_when_row_order_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "light.xisf"
            xml = (
                b'<xisf><FITSKeyword name="BAYERPAT" value="\'RGGB    \'" '
                b'comment="Bayer pattern"/><FITSKeyword name="INSTRUME" '
                b'value="\'ZWO ASI533MC Pro\'"/></xisf>'
            )
            image.write_bytes(b'XISF0100' + struct.pack('<I', len(xml)) + b'\0\0\0\0' + xml)

            metadata = sirilmosaic.read_cfa_metadata(image)
            warnings = sirilmosaic.debayer_preflight_warnings([image], 'auto', 'auto')

            self.assertEqual(metadata['BAYERPAT'], 'RGGB')
            self.assertEqual(metadata['INSTRUME'], 'ZWO ASI533MC Pro')
            self.assertEqual(len(warnings), 1)
            self.assertIn('ROWORDER metadata is missing from 1/1 frames', warnings[0])

    def test_input_summary_reads_exposure_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.fit"
            second = Path(directory) / "second.fit"
            cards = [
                "SIMPLE  =                    T",
                "BITPIX  =                   16",
                "NAXIS   =                    2",
                "NAXIS1  =                   10",
                "NAXIS2  =                   10",
                "EXPTIME =                 120.0",
                "END",
            ]
            header = b"".join(card.ljust(80).encode("ascii") for card in cards)
            first.write_bytes(header.ljust(2880, b" ") + b"1" * 100)
            second.write_bytes(header.ljust(2880, b" ") + b"2" * 200)

            summary = sirilmosaic.summarize_input_frames([first, second])

            self.assertEqual(summary["frames"], 2)
            self.assertEqual(summary["bytes"], first.stat().st_size + second.stat().st_size)
            self.assertEqual(summary["total_exposure_seconds"], 240.0)
            self.assertEqual(summary["known_exposure_frames"], 2)
            self.assertEqual(sirilmosaic.estimate_peak_storage_bytes(100, False), 800)
            self.assertEqual(sirilmosaic.estimate_peak_storage_bytes(100, True), 1800)

    def test_frame_cohorts_group_by_acquisition_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.fit"
            second = Path(directory) / "second.fit"
            cards = [
                "SIMPLE  =                    T",
                "BITPIX  =                   16",
                "NAXIS   =                    2",
                "NAXIS1  =                 3008",
                "NAXIS2  =                 3008",
                "EXPTIME =                  20.0",
                "GAIN    =                  80.0",
                "FILTER  = 'HaOIII  '",
                "INSTRUME= 'Seestar S50      '",
                "END",
            ]
            header = b"".join(card.ljust(80).encode("ascii") for card in cards)
            first.write_bytes(header.ljust(2880, b" "))
            second.write_bytes(header.ljust(2880, b" "))

            cohorts = sirilmosaic.summarize_frame_cohorts([first, second])

            self.assertEqual(len(cohorts), 1)
            self.assertEqual(cohorts[0]["count"], 2)
            self.assertIn("camera=Seestar S50", cohorts[0]["id"])
            self.assertIn("exposure_seconds=20", cohorts[0]["id"])

    def test_test_frame_count_defaults_to_all_and_validates(self) -> None:
        arguments = sirilmosaic.build_parser().parse_args([])
        self.assertEqual(arguments.test_frame_count, 0)

        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=4)
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--test-frame-count", "2",
            ])
            sirilmosaic.configure(arguments)
            sirilmosaic.validate_parameters()
            self.assertEqual(sirilmosaic.test_frame_count, 2)

    def test_move_replace_overwrites_generated_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "new.fit"
            destination = root / "existing.fit"
            source.write_text("new", encoding="ascii")
            destination.write_text("old", encoding="ascii")

            sirilmosaic.move_replace(source, destination)

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_text(encoding="ascii"), "new")

    def test_move_replace_failure_preserves_existing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "new.fit"
            destination = root / "existing.fit"
            source.write_text("new", encoding="ascii")
            destination.write_text("old", encoding="ascii")

            with patch.object(sirilmosaic.os, 'replace', side_effect=OSError('synthetic replace failure')):
                with self.assertRaises(OSError):
                    sirilmosaic.move_replace(source, destination)
            self.assertTrue(source.exists())
            self.assertEqual(destination.read_text(encoding="ascii"), "old")

    def test_count_sequence_files_ignores_non_sequence_fits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "pp_light_00001.fit").touch()
            (folder / "pp_light_00002.fit").touch()
            (folder / "pp_light_bad.fit").touch()
            (folder / "other_00001.fit").touch()

            self.assertEqual(sirilmosaic.count_sequence_files(folder, "pp_light"), 2)

    def test_coverage_crop_bounds_use_requested_percentage(self) -> None:
        self.assertEqual(sirilmosaic.build_parser().parse_args([]).auto_crop_coverage_percent, 50)
        coverage = np.zeros((8, 12), dtype=np.int32)
        coverage[1:7, 1:11] = 2
        coverage[2:6, 2:10] = 10
        coverage[2:6, 6] = 100

        bounds = sirilmosaic.coverage_crop_bounds(coverage, 80)

        self.assertEqual(bounds["reference_coverage"], 10)
        self.assertEqual(bounds["threshold"], 8)
        self.assertEqual(bounds["x"], 2)
        self.assertEqual(bounds["y"], 2)
        self.assertEqual(bounds["width"], 8)
        self.assertEqual(bounds["height"], 4)
        relaxed = sirilmosaic.coverage_crop_bounds(coverage, 20)
        self.assertEqual((relaxed['width'], relaxed['height']), (10, 6))

    def test_coverage_crop_bounds_handle_normalized_map(self) -> None:
        coverage = np.array([
            [0.0, 0.8, 0.8, 0.0],
            [0.0, 1.0, 1.0, 0.0],
            [0.0, 0.8, 0.8, 0.0],
        ], dtype=np.float32)

        bounds = sirilmosaic.coverage_crop_bounds(coverage, 80)

        self.assertAlmostEqual(bounds["threshold"], 0.64)
        self.assertEqual(bounds["x"], 1)
        self.assertEqual(bounds["y"], 0)
        self.assertEqual(bounds["width"], 2)
        self.assertEqual(bounds["height"], 3)

    def test_coverage_crop_excludes_black_corners_and_holes(self) -> None:
        coverage = np.full((6, 9), 10, dtype=np.int32)
        coverage[0, :3] = 0
        coverage[1, :2] = 0
        coverage[3, 6] = 0
        bounds = sirilmosaic.coverage_crop_bounds(coverage, 80)
        selected = coverage[
            bounds['y']:bounds['y'] + bounds['height'],
            bounds['x']:bounds['x'] + bounds['width'],
        ]
        self.assertTrue(np.all(selected >= bounds['threshold']))
        self.assertGreater(selected.size, 12)

    def test_crop_workbench_plan_matches_master_and_inverts_siril_y(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master_stack.fit"
            integration = root / "integration_time_map.fit"
            sirilmosaic._write_coverage_fits(
                master,
                np.linspace(1, 100, 100, dtype=np.float32).reshape(10, 10),
                unit="adu",
            )
            coverage = np.zeros((10, 10), dtype=np.float32)
            coverage[2:8, 1:9] = 10
            sirilmosaic._write_coverage_fits(integration, coverage, unit="s")

            plan = sirilmosaic.build_crop_plan(master, integration, 50)

            self.assertEqual(plan["crop_bounds"], {"x": 1, "y": 2, "width": 8, "height": 6})
            self.assertEqual(
                plan["crop_siril_selection"],
                {"x": 1, "y": 2, "width": 8, "height": 6},
            )
            self.assertEqual(plan["area_percent"], 48.0)
            full_preview = sirilmosaic.read_fits_preview(master, max_width=4, max_height=4)
            crop_preview = sirilmosaic.read_fits_preview(
                master, plan["crop_bounds"], max_width=4, max_height=4
            )
            self.assertEqual(full_preview.shape, (4, 4, 3))
            self.assertEqual(crop_preview.shape, (3, 4, 3))
            self.assertEqual(full_preview.dtype, np.uint8)

    def test_crop_confirmation_is_required_below_one_percent(self) -> None:
        self.assertTrue(crop_requires_confirmation({'area_percent': 0.999}))
        self.assertFalse(crop_requires_confirmation({'area_percent': 1.0}))
        self.assertTrue(crop_requires_confirmation({'area_percent': None}))

    def test_crop_workbench_cli_writes_distinct_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master_stack.fit"
            integration = root / "integration_time_map.fit"
            output = root / "master_stack_crop_050pct.fit"
            executable = root / "siril.exe"
            executable.touch()
            self.write_fits_layer_header(master, 1)
            coverage = np.zeros((10, 10), dtype=np.float32)
            coverage[2:8, 1:9] = 10
            sirilmosaic._write_coverage_fits(integration, coverage, unit="s")
            fake = Mock()
            fake.Open.return_value = True
            commands = []

            def execute(command: str) -> bool:
                commands.append(command)
                if command.startswith("save "):
                    output.touch()
                return True

            fake.Execute.side_effect = execute
            arguments = sirilmosaic.build_parser().parse_args([
                "--siril-exe", str(executable),
                "--crop-workbench",
                "--crop-workbench-master", str(master),
                "--crop-workbench-coverage", str(integration),
                "--crop-workbench-percent", "50",
                "--crop-workbench-output", str(output),
            ])
            with patch("pysiril.siril.Siril", return_value=fake):
                result = sirilmosaic.main([
                    "--siril-exe", str(executable),
                    "--crop-workbench",
                    "--crop-workbench-master", str(master),
                    "--crop-workbench-coverage", str(integration),
                    "--crop-workbench-percent", "50",
                    "--crop-workbench-output", str(output),
                ])

            self.assertEqual(result, 0)
            self.assertTrue(output.is_file())
            self.assertIn("boxselect 1 2 8 6", commands)
            self.assertEqual(fake.Close.call_count, 1)

    def test_coverage_crop_matches_exhaustive_maximum_area(self) -> None:
        generator = np.random.default_rng(314)
        for _ in range(30):
            coverage = generator.integers(0, 4, size=(4, 5), dtype=np.int32)
            bounds = sirilmosaic.coverage_crop_bounds(coverage, 80)
            threshold = bounds['threshold']
            expected_area = 0
            for top in range(4):
                for bottom in range(top + 1, 5):
                    for left in range(5):
                        for right in range(left + 1, 6):
                            if np.all(coverage[top:bottom, left:right] >= threshold):
                                expected_area = max(expected_area, (bottom - top) * (right - left))
            self.assertEqual(bounds['width'] * bounds['height'], expected_area)

    def test_compose_substack_coverage_maps_preserves_overlap_and_orientation(self) -> None:
        local_maps = {
            1: np.array([[1, 1, 1], [1, 1, 1]], dtype=np.float32),
            2: np.array([[2, 2, 2], [2, 2, 2]], dtype=np.float32),
        }
        placements = [
            {'number': 1, 'included': True, 'width': 3, 'height': 2, 'h02': 0, 'h12': 0},
            {'number': 2, 'included': True, 'width': 3, 'height': 2, 'h02': 2, 'h12': 0},
        ]

        composed = sirilmosaic._compose_coverage_arrays(local_maps, placements)

        self.assertEqual(composed.shape, (3, 6))
        np.testing.assert_array_equal(composed, np.array([
            [1, 1, 3, 2, 2, 0],
            [1, 1, 3, 2, 2, 0],
            [0, 0, 0, 0, 0, 0],
        ], dtype=np.float32))

    def test_coverage_landmarks_survive_shifted_variable_size_composition_and_crop(self) -> None:
        local_maps = {
            1: np.array([
                [11, 0, 0, 0],
                [0, 0, 0, 0],
                [0, 0, 0, 0],
            ], dtype=np.float32),
            2: np.array([
                [0, 0, 22],
                [0, 33, 0],
            ], dtype=np.float32),
        }
        placements = [
            {'number': 1, 'included': True, 'width': 4, 'height': 3, 'h02': 0, 'h12': 0},
            {'number': 2, 'included': True, 'width': 3, 'height': 2, 'h02': 3, 'h12': 1},
        ]
        expected = np.array([
            [11, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 22, 0],
            [0, 0, 0, 0, 33, 0, 0],
            [0, 0, 0, 0, 0, 0, 0],
        ], dtype=np.float32)

        composed = sirilmosaic._compose_coverage_arrays(local_maps, placements)

        np.testing.assert_array_equal(composed, expected)
        self.assertEqual((placements[0]['width'], placements[0]['height']), (4, 3))
        self.assertEqual((placements[1]['width'], placements[1]['height']), (3, 2))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / 'master.fit'
            integration = root / 'integration.fit'
            sirilmosaic._write_coverage_fits(master, np.ones(expected.shape, dtype=np.float32))
            sirilmosaic._write_coverage_fits(integration, composed, unit='s')

            plan = sirilmosaic.build_crop_plan(master, integration, 100)

        self.assertEqual(
            plan['crop_bounds'], {'x': 5, 'y': 1, 'width': 1, 'height': 1}
        )
        self.assertEqual(
            plan['crop_siril_selection'], {'x': 5, 'y': 2, 'width': 1, 'height': 1}
        )

    def test_registered_rotation_and_warp_landmarks_keep_master_grid_coordinates(self) -> None:
        rotated = np.rot90(np.array([
            [0, 11, 0],
            [0, 0, 12],
        ], dtype=np.float32))
        warped = np.array([
            [0, 0, 0, 21],
            [0, 22, 0, 0],
            [23, 0, 0, 0],
        ], dtype=np.float32)
        placements = [
            {'number': 1, 'included': True, 'width': 2, 'height': 3, 'h02': 0, 'h12': 0},
            {'number': 2, 'included': True, 'width': 4, 'height': 3, 'h02': 1, 'h12': -1},
        ]
        expected = np.array([
            [0, 0, 0, 0, 21, 0],
            [0, 12, 22, 0, 0, 0],
            [11, 23, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0],
        ], dtype=np.float32)

        composed = sirilmosaic._compose_coverage_arrays(
            {1: rotated, 2: warped}, placements
        )

        np.testing.assert_array_equal(composed, expected)
        self.assertEqual(composed[2, 0], 11)
        self.assertEqual(composed[1, 1], 12)
        self.assertEqual(composed[0, 4], 21)
        self.assertEqual(composed[1, 2], 22)
        self.assertEqual(composed[2, 1], 23)

    def test_compose_substack_coverage_maps_derives_fixed_sequence_dimensions(self) -> None:
        local_maps = {
            1: np.array([[1, 0, 0], [0, 0, 0]], dtype=np.float32),
            2: np.array([[0, 0, 2], [0, 0, 0]], dtype=np.float32),
        }
        placements = [
            {'number': 1, 'included': True, 'width': None, 'height': None, 'h02': 0, 'h12': 0},
            {'number': 2, 'included': True, 'width': None, 'height': None, 'h02': 2, 'h12': 0},
        ]

        composed = sirilmosaic._compose_coverage_arrays(local_maps, placements)

        self.assertEqual(composed.shape, (3, 6))
        self.assertEqual((placements[0]['width'], placements[0]['height']), (3, 2))
        np.testing.assert_array_equal(composed, np.array([
            [1, 0, 0, 0, 2, 0],
            [0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0],
        ], dtype=np.float32))

    def test_compose_substack_coverage_maps_includes_fractional_edge_pixels(self) -> None:
        local_maps = {
            1: np.ones((2, 3), dtype=np.float32),
            2: np.full((2, 3), 2, dtype=np.float32),
        }
        placements = [
            {'number': 1, 'included': True, 'width': 3, 'height': 2, 'h02': -0.2, 'h12': -0.2},
            {'number': 2, 'included': True, 'width': 3, 'height': 2, 'h02': 2.2, 'h12': 0.2},
        ]

        composed = sirilmosaic._compose_coverage_arrays(local_maps, placements)

        self.assertEqual(composed.shape, (5, 8))

    def test_registered_map_sequence_reuses_one_registration_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'pp_light_.seq'
            destination = root / 'integration_time_map_substack_.seq'
            source.write_text(
                "S 'pp_light_' 1 2 2 5 0 7 1 0 0\n"
                'L 3\n'
                'I 1 1 3,2\n'
                'I 2 1 3,2\n'
                'R1 1 1 1 1 1 1 H 1 0 0 0 1 0 0 0 1\n'
                'R1 1 1 1 1 1 1 H 1 0 2 0 1 0 0 0 1\n'
                'M1-0 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1\n'
                'M2-0 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2\n',
                encoding='ascii',
            )

            sirilmosaic.create_registered_map_sequence(
                source, destination, 'integration_time_map_substack_'
            )

            contents = destination.read_text(encoding='utf-8')
            self.assertIn("S 'integration_time_map_substack_'", contents)
            self.assertIn('\nL 1\n', contents)
            self.assertEqual(contents.count('\nR0 '), 2)
            self.assertNotIn('\nR1 ', contents)
            self.assertIn('\nM0-0 ', contents)
            self.assertNotIn('\nM1-0 ', contents)
            self.assertNotIn('\nM2-0 ', contents)

    def test_register_coverage_preserves_primary_siril_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / 'process'
            local_dir = root / 'local_maps'
            process.mkdir()
            local_dir.mkdir()
            (process / 'pp_light_.seq').write_text(
                "S 'pp_light_' 1 1 1 5 0 7 0 0 0\n"
                'L 1\nI 1 1\n'
                'R0 1 1 1 1 1 1 H 1 0 0 0 1 0 0 0 1\n',
                encoding='ascii',
            )
            sirilmosaic._write_coverage_fits(
                local_dir / 'integration_time_map_substack_1.fit',
                np.ones((2, 3), dtype=np.float32),
                unit='s',
            )
            calls = 0

            def execute(_command):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise sirilmosaic.SirilCommandError('primary transform failure')
                if calls == 3:
                    raise sirilmosaic.SirilCommandError('cleanup cd failure')

            with patch.object(sirilmosaic, 'execute_siril', side_effect=execute):
                with self.assertRaisesRegex(
                    sirilmosaic.SirilCommandError, 'primary transform failure'
                ):
                    sirilmosaic.register_substack_coverage_maps(process, local_dir, 1)

    def test_register_substack_coverage_maps_applies_saved_transforms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / 'process'
            local_dir = root / 'local_maps'
            process.mkdir()
            local_dir.mkdir()
            (process / 'pp_light_.seq').write_text(
                "S 'pp_light_' 1 2 2 5 0 7 1 0 0\n"
                'L 3\nI 1 1 3,2\nI 2 1 3,2\n'
                'R1 1 1 1 1 1 1 H 1 0 0 0 1 0 0 0 1\n'
                'R1 1 1 1 1 1 1 H 1 0 0 0 1 0 0 0 1\n',
                encoding='ascii',
            )
            for number in (1, 2):
                sirilmosaic._write_coverage_fits(
                    local_dir / f'integration_time_map_substack_{number}.fit',
                    np.full((2, 3), number, dtype=np.float32),
                    unit='s',
                )
            commands = []

            def execute(command):
                commands.append(command)
                if command.startswith('seqapplyreg '):
                    for number in (1, 2):
                        shutil.copy2(
                            local_dir / f'coverage_source_{number:05d}.fit',
                            local_dir / f'registered_coverage_source_{number:05d}.fit',
                        )
                    sirilmosaic.create_registered_map_sequence(
                        local_dir / 'coverage_source_.seq',
                        local_dir / 'registered_coverage_source_.seq',
                        'registered_coverage_source_',
                    )

            with patch.object(sirilmosaic, 'execute_siril', side_effect=execute):
                registered, records = sirilmosaic.register_substack_coverage_maps(
                    process, local_dir, 2
                )

            self.assertEqual(sorted(registered), [1, 2])
            self.assertEqual([record['number'] for record in records], [1, 2])
            np.testing.assert_array_equal(registered[1], np.ones((2, 3)))
            np.testing.assert_array_equal(registered[2], np.full((2, 3), 2))
            self.assertEqual(
                commands,
                [
                    f'cd {sirilmosaic.siril_path(local_dir)}',
                    'seqapplyreg coverage_source -prefix=registered_ '
                    '-framing=max -interp=nearest',
                    f'cd {sirilmosaic.siril_path(process)}',
                ],
            )

    def test_compose_substack_coverage_maps_writes_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "process"
            local_dir = root / "local_maps"
            process.mkdir()
            local_dir.mkdir()
            sirilmosaic._write_coverage_fits(
                local_dir / "integration_time_map_substack_1.fit",
                np.ones((2, 3), dtype=np.float32),
                unit="s",
            )
            sirilmosaic._write_coverage_fits(
                local_dir / "integration_time_map_substack_2.fit",
                np.full((2, 3), 2, dtype=np.float32),
                unit="s",
            )
            with patch.multiple(
                sirilmosaic,
                workdir=root,
                output_dir=root / "output",
                run_id="compose",
                quality_report={
                    "substack_coverages": {
                        "1": {"frames_counted": 2, "exposure_range_seconds": [60, 60]},
                        "2": {"frames_counted": 3, "exposure_range_seconds": [60, 300]},
                    }
                },
                coverage_map_enabled=True,
                active_cohort_tag=None,
                active_cohort_id=None,
            ), patch.object(
                sirilmosaic,
                'register_substack_coverage_maps',
                return_value=(
                    {
                        1: np.ones((2, 3), dtype=np.float32),
                        2: np.full((2, 3), 2, dtype=np.float32),
                    },
                    [
                        {'number': 1, 'width': 3, 'height': 2, 'h02': 0, 'h12': 0},
                        {'number': 2, 'width': 3, 'height': 2, 'h02': 0, 'h12': 0},
                    ],
                ),
            ):
                report = sirilmosaic.compose_substack_coverage_maps(process, local_dir, 2)

            self.assertEqual(report["frames_counted"], 5)
            self.assertEqual(report["maximum_integration_seconds"], 3)
            self.assertEqual(report["width"], 4)
            self.assertEqual(report["height"], 3)
            self.assertTrue(Path(report["integration_time_path"]).is_file())
            composed = sirilmosaic._read_fits_array(Path(report["integration_time_path"]))
            np.testing.assert_array_equal(composed, np.array([
                [3, 3, 3, 0],
                [3, 3, 3, 0],
                [0, 0, 0, 0],
            ], dtype=np.float32))

    def test_coverage_crop_handles_empty_and_invalid_pixels(self) -> None:
        for coverage in (np.zeros((2, 3)), np.empty((0, 0)), np.full((2, 3), np.nan)):
            self.assertIsNone(sirilmosaic.coverage_crop_bounds(coverage, 80))
        coverage = np.array([[np.nan, 10, 10], [np.inf, 10, 10]])
        bounds = sirilmosaic.coverage_crop_bounds(coverage, 80)
        self.assertEqual((bounds['x'], bounds['width'], bounds['height']), (1, 2, 2))

    def test_drizzle_footprint_fills_sampling_holes_and_missing_rows(self) -> None:
        valid = np.zeros((7, 10), dtype=bool)
        valid[1, 3:8:2] = True
        valid[3, 2:9:2] = True
        valid[5, 3:8:2] = True

        footprint = sirilmosaic._fill_drizzle_footprint(valid)

        np.testing.assert_array_equal(footprint[1], np.array(
            [False, False, False, True, True, True, True, True, False, False]
        ))
        self.assertTrue(np.all(footprint[2, 3:8]))
        self.assertTrue(np.all(footprint[3, 2:9]))
        self.assertTrue(np.all(footprint[4, 3:8]))
        self.assertFalse(np.any(footprint[[0, 6]]))

    def test_finalize_coverage_maps_matches_master_footprint_and_copies_wcs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_path = root / 'master.fit'
            integration_path = root / 'integration.fit'
            coverage_path = root / 'coverage.fit'
            master = np.zeros((4, 5), dtype=np.float32)
            master[1:3, 1:4] = 1
            wcs_cards = [
                "CTYPE1  = 'RA---TAN'",
                "CTYPE2  = 'DEC--TAN'",
                'CRPIX1  =                  3.0',
                'CRPIX2  =                  2.5',
                'CRVAL1  =                120.0',
                'CRVAL2  =                 45.0',
            ]
            sirilmosaic._write_coverage_fits(
                master_path, master, unit='adu', extra_cards=wcs_cards
            )
            integration = np.full((4, 5), 60, dtype=np.float32)
            sirilmosaic._write_coverage_fits(integration_path, integration, unit='s')
            sirilmosaic._write_coverage_fits(coverage_path, integration / 60, unit='relative')
            report = {'path': str(coverage_path), 'integration_time_path': str(integration_path)}

            with patch.object(sirilmosaic, 'drizzle_enabled', False):
                result = sirilmosaic.finalize_coverage_maps(master_path, report)

            expected = np.zeros((4, 5), dtype=np.float32)
            expected[1:3, 1:4] = 60
            np.testing.assert_array_equal(
                sirilmosaic._read_fits_array(integration_path), expected
            )
            header = dict(
                (card[:8].strip(), sirilmosaic._clean_fits_value(card[10:]))
                for card in sirilmosaic._read_fits_header_cards(coverage_path)
                if card[8:10] == '= '
            )
            self.assertEqual(header['CTYPE1'], 'RA---TAN')
            self.assertEqual(header['CTYPE2'], 'DEC--TAN')
            self.assertEqual(header['BUNIT'], 'relative')
            self.assertEqual(result['coverage_outside_master_footprint_pixels'], 0)
            self.assertEqual(result['master_signal_outside_coverage_pixels'], 0)
            self.assertEqual(result['wcs_status'], 'copied')

    def test_finalize_coverage_maps_allows_configured_edge_fringe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_path = root / 'master.fit'
            integration_path = root / 'integration.fit'
            coverage_path = root / 'coverage.fit'
            master = np.zeros((9, 12), dtype=np.float32)
            master[4, 2] = 1
            master[4, 7] = 1
            integration = np.zeros_like(master)
            integration[4, 2] = 60
            sirilmosaic._write_coverage_fits(master_path, master, unit='adu')
            sirilmosaic._write_coverage_fits(integration_path, integration, unit='s')
            sirilmosaic._write_coverage_fits(coverage_path, integration, unit='relative')
            report = {'path': str(coverage_path), 'integration_time_path': str(integration_path)}

            with (
                patch.object(sirilmosaic, 'drizzle_enabled', False),
                patch.object(sirilmosaic, 'feather_val', '2'),
                patch.object(sirilmosaic, 'registration_interpolation', 'cubic'),
            ):
                result = sirilmosaic.finalize_coverage_maps(master_path, report)

            self.assertEqual(result['master_signal_outside_coverage_pixels'], 1)
            self.assertEqual(result['master_signal_outside_coverage_edge_pixels'], 1)
            self.assertEqual(result['master_signal_outside_coverage_unexplained_pixels'], 0)
            self.assertEqual(result['coverage_edge_allowance_pixels'], 5)
            self.assertEqual(sirilmosaic._read_fits_array(integration_path)[4, 7], 0)

    def test_finalize_coverage_maps_rejects_signal_beyond_edge_fringe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master_path = root / 'master.fit'
            integration_path = root / 'integration.fit'
            coverage_path = root / 'coverage.fit'
            master = np.zeros((9, 12), dtype=np.float32)
            master[4, 2] = 1
            master[4, 8] = 1
            integration = np.zeros_like(master)
            integration[4, 2] = 60
            sirilmosaic._write_coverage_fits(master_path, master, unit='adu')
            sirilmosaic._write_coverage_fits(integration_path, integration, unit='s')
            sirilmosaic._write_coverage_fits(coverage_path, integration, unit='relative')
            report = {'path': str(coverage_path), 'integration_time_path': str(integration_path)}

            with (
                patch.object(sirilmosaic, 'drizzle_enabled', False),
                patch.object(sirilmosaic, 'feather_val', '2'),
                patch.object(sirilmosaic, 'registration_interpolation', 'cubic'),
            ):
                with self.assertRaisesRegex(ValueError, 'beyond the configured 5-pixel'):
                    sirilmosaic.finalize_coverage_maps(master_path, report)

    def test_coverage_options_parse(self) -> None:
        arguments = sirilmosaic.build_parser().parse_args([
            "--coverage-map",
            "--auto-crop-master",
            "--auto-crop-coverage-percent", "85",
        ])
        sirilmosaic.configure(arguments)
        self.assertTrue(sirilmosaic.coverage_map_enabled)
        self.assertTrue(sirilmosaic.auto_crop_enabled)
        self.assertEqual(sirilmosaic.auto_crop_coverage_percent, 85)
        sirilmosaic.coverage_map_enabled = False
        sirilmosaic.auto_crop_enabled = False

    def test_export_per_cohort_option_partitions_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.fit"
            second = root / "second.fit"
            third = root / "third.fit"
            base = [
                "SIMPLE  =                    T", "BITPIX  =                   16",
                "NAXIS   =                    2", "NAXIS1  =                 3008",
                "NAXIS2  =                 3008", "EXPTIME =                  20.0",
                "GAIN    =                  80.0", "FILTER  = 'HaOIII  '",
                "INSTRUME= 'Seestar S50      '", "END",
            ]
            for path in (first, second, third):
                path.write_bytes(b"".join(card.ljust(80).encode("ascii") for card in base).ljust(2880, b" "))
            changed = base.copy()
            changed[5] = "EXPTIME =                  60.0"
            third.write_bytes(b"".join(card.ljust(80).encode("ascii") for card in changed).ljust(2880, b" "))

            partitions = sirilmosaic.partition_frame_cohorts([first, second, third])

            self.assertEqual([len(files) for _, files in partitions], [2, 1])
            self.assertIn("exposure_seconds=20", partitions[0][0])
            self.assertIn("exposure_seconds=60", partitions[1][0])
            tag = sirilmosaic.cohort_filename_tag(partitions[1][1][0])
            self.assertEqual(
                tag,
                "camera-Seestar_S50_exp-60s_gain-80_filter-HaOIII_size-3008x3008",
            )

    def test_partition_frame_cohorts_can_group_by_selected_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.fit"
            second = root / "second.fit"
            cards = [
                "SIMPLE  =                    T", "BITPIX  =                   16",
                "NAXIS   =                    2", "NAXIS1  =                  10",
                "NAXIS2  =                  10", "EXPTIME =                  20.0",
                "GAIN    =                  80.0", "FILTER  = 'LP      '",
                "INSTRUME= 'Seestar S50      '", "END",
            ]
            header = b"".join(card.ljust(80).encode("ascii") for card in cards).ljust(2880, b" ")
            first.write_bytes(header)
            second.write_bytes(header.replace(b"'LP      '", b"'IRCUT   '"))

            partitions = sirilmosaic.partition_frame_cohorts([first, second], ("filter",))

            self.assertEqual([cohort_id for cohort_id, _files in partitions], ["filter=IRCUT", "filter=LP"])

    def test_cohort_summary_and_filename_identify_mixed_exposures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / 'first.fit'
            second = root / 'second.fit'
            self.write_fits_layer_header(first, 1, 10)
            self.write_fits_layer_header(second, 1, 60)
            cohorts = sirilmosaic.summarize_frame_cohorts([first, second], ())

            self.assertEqual(cohorts[0]['metadata']['exposure_seconds'], 'mixed-10-60')
            self.assertEqual(cohorts[0]['metadata_values']['exposure_seconds'], ['10', '60'])
            self.assertIn('exp-mixed-10-60s', sirilmosaic.cohort_filename_tag(
                cohorts[0]['metadata']
            ))

    def test_cohort_filename_tag_budgets_complete_siril_path(self) -> None:
        metadata = {
            'camera': 'Very Long Camera Model ' * 5,
            'exposure_seconds': 'mixed-10-300',
            'gain': '80',
            'filter': 'LP',
            'dimensions': '3008x3008',
        }
        directory = Path('W:/Siril Mosaic Output')

        tag = sirilmosaic.cohort_filename_tag(metadata, directory, '20260916_123456')
        path = directory / f'master_stack_20260916_123456_cohort_001_{tag}_cropped.fit'

        self.assertLessEqual(len(str(path)), sirilmosaic.MAX_SIRIL_ARTIFACT_PATH)
        self.assertRegex(tag, r'camera-.*-[0-9a-f]{10}_exp-mixed-10-300s_')
        self.assertIn('_gain-80_filter-LP_size-3008x3008', tag)

    def test_export_per_cohort_allows_coverage(self) -> None:
        arguments = sirilmosaic.build_parser().parse_args([
            "--export-per-cohort", "--coverage-map", "--auto-crop-master",
        ])
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=2)
            arguments.workdir = workdir
            arguments.siril_exe = executable
            arguments.output_dir = workdir / "output"
            sirilmosaic.configure(arguments)
            sirilmosaic.validate_parameters()
            self.assertTrue(sirilmosaic.coverage_map_enabled)
            self.assertTrue(sirilmosaic.auto_crop_enabled)

    def test_gui_maps_selected_cohort_group_fields_to_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=2)
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.workdir.set(str(workdir))
                app.output_dir.set(str(Path(directory) / "output"))
                app.siril_exe.set(str(executable))
                app.substacks.set(1)
                app.export_per_cohort.set(True)
                app.cohort_group_camera.set(False)
                app.cohort_group_filter.set(True)
                app.cohort_group_exposure.set(False)

                command = app.command()
                arguments = sirilmosaic.build_parser().parse_args(command[3:])

                self.assertEqual(arguments.cohort_group_by, ["filter"])
            finally:
                root.destroy()

    def test_cohort_group_checkboxes_follow_export_toggle(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            self.assertEqual(
                [str(widget.cget("text")) for widget in app.cohort_group_widgets],
                ["Camera model", "Filter", "Exposure time"],
            )
            self.assertTrue(all(widget.instate(("disabled",)) for widget in app.cohort_group_widgets))
            app.export_per_cohort.set(True)
            app.update_cohort_group_controls()
            self.assertTrue(all(widget.instate(("!disabled",)) for widget in app.cohort_group_widgets))
        finally:
            root.destroy()

    def test_export_per_cohort_resets_substack_completion_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "workdir"
            output = root / "output"
            workdir.mkdir()
            output.mkdir()
            first = workdir / "first.fit"
            second = workdir / "second.fit"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            executable = root / "siril.exe"
            executable.touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--output-dir", str(output),
                "--siril-exe", str(executable),
                "--export-per-cohort",
                "--substacks", "1",
            ])
            calls = []

            def fake_substack(number):
                calls.append((number, sirilmosaic.active_cohort_id))
                return True

            def fake_masterstack(_number):
                sirilmosaic.quality_report["master"] = {"path": str(output / "master.fit")}

            def fake_cleanup(_number):
                sirilmosaic.remove_tree(workdir / "Lights_sorted")

            fake_siril = Mock()
            with patch.multiple(
                sirilmosaic,
                discover_light_files=Mock(return_value=[first, second]),
                partition_frame_cohorts=Mock(return_value=[
                    ("cohort-a", [first]),
                    ("cohort-b", [second]),
                ]),
                group_files=Mock(),
                read_group_manifests=Mock(return_value={1: {}}),
                substack=fake_substack,
                masterstack=fake_masterstack,
                cleanup=fake_cleanup,
                final_cleanup=Mock(),
                finalize_integration_report=Mock(),
                write_verification_artifact=Mock(return_value=(
                    output / "verification.json",
                    {"status": "FAIL", "counts": {"PASS": 0, "WARN": 0, "FAIL": 1}},
                )),
                write_quality_report=Mock(),
                append_journal_event=Mock(),
                debayer_preflight_warnings=Mock(return_value=[]),
                open_siril_with_recovery=Mock(),
                execute_siril=Mock(),
                acquire_run_lock=Mock(),
                release_run_lock=Mock(),
                restore_rejected_frames=Mock(return_value=[]),
                move_frame_selection_rejects=Mock(return_value=[]),
                check_runtime_disk_headroom=Mock(),
            ), patch("pysiril.siril.Siril", return_value=fake_siril):
                result = sirilmosaic.run_pipeline(arguments)

            self.assertEqual(result, 1)
            self.assertEqual(calls, [(1, "cohort-a"), (1, "cohort-b")])

    def test_cohort_report_initialization_preserves_completed_records(self) -> None:
        report = {
            'masters': [{'cohort': 'complete-cohort', 'path': 'master.fit'}],
            'coverages': [{'cohort': 'complete-cohort', 'path': 'coverage.fit'}],
        }

        sirilmosaic.initialize_cohort_report_collections(report)

        self.assertEqual(report['masters'], [
            {'cohort': 'complete-cohort', 'path': 'master.fit'},
        ])
        self.assertEqual(report['coverages'], [
            {'cohort': 'complete-cohort', 'path': 'coverage.fit'},
        ])

    def test_cohort_report_upsert_is_idempotent_after_repeated_resume(self) -> None:
        report = {
            'masters': [{'cohort': 'cohort-a', 'path': 'partial-master.fit'}],
            'coverages': [{'cohort': 'cohort-a', 'path': 'partial-coverage.fit'}],
        }

        sirilmosaic.upsert_cohort_report_record(
            report, 'masters', {'cohort': 'cohort-a', 'path': 'master.fit'}
        )
        sirilmosaic.upsert_cohort_report_record(
            report, 'coverages', {'cohort': 'cohort-a', 'path': 'coverage.fit'}
        )
        sirilmosaic.upsert_cohort_report_record(
            report, 'masters', {'cohort': 'cohort-a', 'path': 'master.fit'}
        )

        self.assertEqual(report['masters'], [
            {'cohort': 'cohort-a', 'path': 'master.fit'},
        ])
        self.assertEqual(report['coverages'], [
            {'cohort': 'cohort-a', 'path': 'coverage.fit'},
        ])

    def test_export_per_cohort_skips_zero_plate_solve_cohort_without_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "workdir"
            output = root / "output"
            workdir.mkdir()
            output.mkdir()
            first = workdir / "first.fit"
            second = workdir / "second.fit"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            executable = root / "siril.exe"
            executable.touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--output-dir", str(output),
                "--siril-exe", str(executable),
                "--export-per-cohort",
                "--substacks", "1",
            ])
            calls = []

            def fake_substack(number):
                calls.append((number, sirilmosaic.active_cohort_id))
                if sirilmosaic.active_cohort_id == "cohort-a":
                    raise sirilmosaic.NoPlateSolveFramesError(
                        "seqplatesolve bkg_pp_light -order=3",
                        ["0 images successfully platesolved out of 1 included."],
                    )
                return True

            def fake_masterstack(_number):
                sirilmosaic.quality_report["master"] = {"path": str(output / "master.fit")}

            fake_siril = Mock()
            with patch.multiple(
                sirilmosaic,
                discover_light_files=Mock(return_value=[first, second]),
                partition_frame_cohorts=Mock(return_value=[
                    ("cohort-a", [first]),
                    ("cohort-b", [second]),
                ]),
                group_files=Mock(),
                read_group_manifests=Mock(return_value={1: {}}),
                substack=fake_substack,
                masterstack=fake_masterstack,
                cleanup=Mock(),
                final_cleanup=Mock(),
                finalize_integration_report=Mock(),
                write_verification_artifact=Mock(return_value=(
                    output / "verification.json",
                    {"status": "PASS", "counts": {"PASS": 1, "WARN": 0, "FAIL": 0}},
                )),
                write_quality_report=Mock(),
                append_journal_event=Mock(),
                debayer_preflight_warnings=Mock(return_value=[]),
                open_siril_with_recovery=Mock(),
                execute_siril=Mock(),
                acquire_run_lock=Mock(),
                release_run_lock=Mock(),
                restore_rejected_frames=Mock(return_value=[]),
                move_frame_selection_rejects=Mock(return_value=[]),
                check_runtime_disk_headroom=Mock(),
            ), patch("pysiril.siril.Siril", return_value=fake_siril):
                result = sirilmosaic.run_pipeline(arguments)

            self.assertEqual(result, 0)
            self.assertEqual(calls, [(1, "cohort-a"), (1, "cohort-b")])
            self.assertEqual(
                sirilmosaic.quality_report["skipped_cohorts"][0]["reason_code"],
                "no_plate_solve_frames",
            )

    def test_multicohort_checkpoint_plans_all_assignments_before_first_substack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / 'workdir'
            output = root / 'output'
            workdir.mkdir()
            output.mkdir()
            files = []
            for number in range(4):
                path = workdir / f'light_{number}.fit'
                path.write_bytes(b'light')
                files.append(path)
            executable = root / 'siril.exe'
            executable.touch()
            arguments = sirilmosaic.build_parser().parse_args([
                '--workdir', str(workdir),
                '--output-dir', str(output),
                '--siril-exe', str(executable),
                '--export-per-cohort',
                '--substacks', '1',
            ])

            def interrupt_first_substack(_number):
                checkpoint = json.loads(
                    (output / sirilmosaic.RUN_CHECKPOINT).read_text(encoding='utf-8')
                )
                self.assertEqual(len(checkpoint['cohorts']), 2)
                self.assertTrue(all(cohort['groups'] for cohort in checkpoint['cohorts']))
                raise sirilmosaic.CancellationRequested('synthetic interruption')

            fake_siril = Mock()
            with patch.multiple(
                sirilmosaic,
                discover_light_files=Mock(return_value=files),
                partition_frame_cohorts=Mock(return_value=[
                    ('cohort-a', files[:2]),
                    ('cohort-b', files[2:]),
                ]),
                substack=interrupt_first_substack,
                write_quality_report=Mock(),
                append_journal_event=Mock(),
                debayer_preflight_warnings=Mock(return_value=[]),
                open_siril_with_recovery=Mock(),
                execute_siril=Mock(),
                acquire_run_lock=Mock(),
                release_run_lock=Mock(),
                restore_rejected_frames=Mock(return_value=[]),
                prepare_siril_for_cleanup=Mock(),
            ), patch('pysiril.siril.Siril', return_value=fake_siril):
                result = sirilmosaic.run_pipeline(arguments)

            checkpoint = sirilmosaic.load_run_checkpoint()
            self.assertEqual(result, 2)
            self.assertEqual(checkpoint['global_phase'], 'interrupted')
            self.assertTrue(all(cohort['groups'] for cohort in checkpoint['cohorts']))
            self.assertCountEqual(sirilmosaic.discover_light_files(workdir), files)

    def test_parser_accepts_supported_drizzle_and_rejection_methods(self) -> None:
        parser = sirilmosaic.build_parser()
        self.assertTrue(parser.parse_args(["--dry-run"]).dry_run)
        analysis_arguments = parser.parse_args([
            "--replay-ledger", "ledger.jsonl", "--integrity-scan",
            "--integrity-scan-json", "scan.json",
        ])
        self.assertEqual(analysis_arguments.replay_ledger, Path("ledger.jsonl"))
        self.assertTrue(analysis_arguments.integrity_scan)
        self.assertEqual(analysis_arguments.integrity_scan_json, Path("scan.json"))
        for kernel in sirilmosaic.DRIZZLE_KERNELS:
            arguments = parser.parse_args(["--drizzle-kernel", kernel])
            self.assertEqual(arguments.drizzle_kernel, kernel)
        for method in sirilmosaic.PIXEL_REJECTION_METHODS:
            arguments = parser.parse_args(["--rejection-method", method])
            self.assertEqual(arguments.rejection_method, method)
        default_maps = parser.parse_args([])
        self.assertIsNone(default_maps.low_rejection_map)
        self.assertIsNone(default_maps.high_rejection_map)
        explicit_maps = parser.parse_args([
            "--low-rejection-map", "--no-high-rejection-map",
        ])
        self.assertTrue(explicit_maps.low_rejection_map)
        self.assertFalse(explicit_maps.high_rejection_map)
        for method in sirilmosaic.STACK_NORMALIZATION_METHODS:
            arguments = parser.parse_args(["--stack-normalization", method])
            self.assertEqual(arguments.stack_normalization, method)
        arguments = parser.parse_args([
            "--plate-solve-order", "5",
            "--plate-solve-downscale",
            "--plate-solve-radius", "2.5",
            "--plate-solve-limit-mag", "+1",
            "--rbf-smoothing", "0.75",
            "--no-background-dither",
            "--registration-transform", "similarity",
            "--registration-minpairs", "8",
            "--registration-maxstars", "500",
            "--registration-interpolation", "cubic",
            "--seed", "12345",
        ])
        self.assertEqual(arguments.plate_solve_order, 5)
        self.assertTrue(arguments.plate_solve_downscale)
        self.assertEqual(arguments.plate_solve_radius, 2.5)
        self.assertEqual(arguments.plate_solve_limit_mag, "+1")
        self.assertEqual(arguments.rbf_smoothing, "0.75")
        self.assertFalse(arguments.background_dither)
        self.assertEqual(arguments.registration_transform, "similarity")
        self.assertEqual(arguments.registration_minpairs, 8)
        self.assertEqual(arguments.registration_maxstars, 500)
        self.assertEqual(arguments.registration_interpolation, "cubic")
        self.assertEqual(arguments.seed, 12345)

    def test_dry_run_validates_without_starting_siril_or_moving_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=3)
            sources = {
                path.relative_to(workdir): path.read_bytes()
                for path in sirilmosaic.discover_light_files(workdir)
            }
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--output-dir", str(Path(directory) / "output"),
                "--siril-exe", str(executable),
                "--dry-run", "--seed", "12345",
            ])

            with patch("pysiril.siril.Siril") as siril_class:
                result = sirilmosaic.run_pipeline(arguments)

            self.assertEqual(result, 0)
            siril_class.assert_not_called()
            self.assertEqual(
                {
                    path.relative_to(workdir): path.read_bytes()
                    for path in sirilmosaic.discover_light_files(workdir)
                },
                sources,
            )
            self.assertFalse((workdir / "Lights_sorted").exists())
            self.assertFalse((workdir / "rejects").exists())

    def test_dry_run_counts_pending_rejects_without_restoring_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=4)
            relative_path = Path("panel_1") / "lights" / "light.fit"
            source = workdir / relative_path
            rejects_path = workdir / "rejects" / relative_path
            rejects_path.parent.mkdir(parents=True)
            shutil.move(str(source), str(rejects_path))
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--output-dir", str(Path(directory) / "output"),
                "--siril-exe", str(executable),
                "--dry-run", "--test-frame-count", "4", "--seed", "12345",
            ])

            with patch("pysiril.siril.Siril") as siril_class, patch("builtins.print") as print_mock:
                result = sirilmosaic.run_pipeline(arguments)

            self.assertEqual(result, 0)
            siril_class.assert_not_called()
            output = "\n".join(
                " ".join(str(argument) for argument in call.args)
                for call in print_mock.call_args_list
            )
            self.assertIn(
                "[DRY RUN] Input frames: 4 (3 current + 1 prior rejects to restore)",
                output,
            )
            self.assertFalse(source.exists())
            self.assertTrue(rejects_path.is_file())
            self.assertFalse((workdir / "Lights_sorted").exists())

    def test_advanced_command_builders_use_selected_settings(self) -> None:
        with patch.multiple(
            sirilmosaic,
            background_method="rbf",
            background_samples=24,
            background_tolerance="1.2",
            rbf_smoothing="0.75",
            background_dither=False,
            plate_solve_order=5,
            plate_solve_downscale=True,
            plate_solve_radius=2.5,
            plate_solve_limit_mag="+1",
            registration_transform="similarity",
            registration_minpairs=8,
            registration_maxstars=500,
            registration_interpolation="cubic",
        ):
            self.assertEqual(
                sirilmosaic.build_background_command("pp_light"),
                "seqsubsky pp_light -rbf -samples=24 -tolerance=1.2 -smooth=0.75 -nodither",
            )
            self.assertEqual(
                sirilmosaic.build_plate_solve_command("bkg_pp_light", "gaia"),
                "seqplatesolve bkg_pp_light -order=5 -nocrop -nocache -force "
                "-catalog=gaia -downscale -radius=2.5 -limitmag=+1",
            )
            self.assertEqual(
                sirilmosaic.build_master_registration_command(),
                "register pp_light -2pass -transf=similarity -interp=cubic "
                "-minpairs=8 -maxstars=500",
            )

    def test_advanced_sections_align_within_each_grid_row(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            root.update_idletasks()
            sections = {
                str(widget.cget("text")): widget
                for widget in app.advanced_content.winfo_children()
                if isinstance(widget, ttk.LabelFrame)
            }
            self.assertEqual(
                sections["Plate solving"].winfo_height(),
                sections["Reproducibility"].winfo_height(),
            )
            self.assertEqual(
                sections["Background extraction"].winfo_height(),
                sections["Registration"].winfo_height(),
            )
        finally:
            root.destroy()

    def test_coverage_mismatch_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = Path(directory)
            def write_registered(path: Path, height: int) -> None:
                cards = [
                    "SIMPLE  =                    T",
                    "BITPIX  =                   32",
                    "NAXIS   =                    3",
                    "NAXIS1  =                   10",
                    f"NAXIS2  =                   {height}",
                    "NAXIS3  =                    3",
                    "END",
                ]
                header = b"".join(card.ljust(80).encode("ascii") for card in cards)
                data = np.zeros((3, height, 10), dtype=">i4").tobytes()
                path.write_bytes(header.ljust(2880, b" ") + data.ljust((len(data) + 2879) // 2880 * 2880, b"\0"))

            write_registered(process / "r_light_00001.fit", 10)
            write_registered(process / "r_light_00002.fit", 11)
            sirilmosaic.coverage_map_enabled = True
            sirilmosaic.quality_report = {}
            sirilmosaic.output_dir = process
            sirilmosaic.run_id = "coverage_mismatch"
            self.assertIsNone(sirilmosaic.generate_coverage_map(process, "r_light"))
            self.assertEqual(sirilmosaic.quality_report["coverage"]["status"], "unavailable")
            sirilmosaic.coverage_map_enabled = False

    def test_cropped_master_skips_unavailable_coverage(self) -> None:
        sirilmosaic.quality_report = {
            'coverage': {'status': 'unavailable', 'reason': 'different dimensions'}
        }
        sirilmosaic.auto_crop_enabled = True
        sirilmosaic.create_cropped_master()
        sirilmosaic.auto_crop_enabled = False

    def test_cropped_master_skips_mismatched_coverage_canvas(self) -> None:
        sirilmosaic.quality_report = {'coverage': {'path': 'coverage.fit'}}
        sirilmosaic.output_dir = Path('output')
        sirilmosaic.run_id = 'mismatch'
        sirilmosaic.auto_crop_enabled = True
        with (
            patch.object(sirilmosaic, 'execute_siril') as execute,
            patch.object(sirilmosaic, '_read_fits_array', return_value=np.ones((4, 5))),
            patch.object(sirilmosaic, '_read_fits_dimensions', return_value=(6, 4)),
        ):
            sirilmosaic.create_cropped_master()

        self.assertEqual(
            sirilmosaic.quality_report['coverage']['crop_status'],
            'unavailable',
        )
        self.assertFalse(any(call.args[0].startswith('boxselect ') for call in execute.call_args_list))
        sirilmosaic.auto_crop_enabled = False

    def test_cropped_master_converts_fits_rows_to_siril_selection(self) -> None:
        coverage = np.zeros((9, 12), dtype=np.int32)
        coverage[1:4, 2:10] = 10
        report = {'coverage': {'path': 'coverage.fit'}}
        with (
            patch.multiple(
                sirilmosaic,
                quality_report=report,
                output_dir=Path('output'),
                run_id='orientation',
                auto_crop_enabled=True,
                auto_crop_coverage_percent=80,
            ),
            patch.object(sirilmosaic, 'execute_siril') as execute,
            patch.object(sirilmosaic, '_read_fits_array', return_value=coverage),
            patch.object(sirilmosaic, '_read_fits_dimensions', return_value=(12, 9)),
            patch.object(sirilmosaic, 'require_siril_artifact'),
        ):
            sirilmosaic.create_cropped_master()

        execute.assert_any_call('boxselect 2 5 8 3')
        self.assertEqual(report['coverage']['crop_bounds']['y'], 1)
        self.assertEqual(report['coverage']['crop_siril_selection']['y'], 5)
        self.assertEqual(report['coverage']['crop_reference_coverage'], 10)
        self.assertFalse(any('mirrorx' in call.args[0] for call in execute.call_args_list))
        saved = [call.args[0] for call in execute.call_args_list if call.args[0].startswith('save ')]
        self.assertEqual(len(saved), 1)
        self.assertIn('orientation_cropped', saved[0])

    def test_cropped_master_uses_integration_seconds_not_display_map(self) -> None:
        seconds = np.zeros((6, 8), dtype=np.float32)
        seconds[1:5, 1:7] = 300
        seconds[1:5, 1:3] = 60
        report = {'coverage': {'path': 'view.fit', 'integration_time_path': 'seconds.fit', 'crop_unit': 's'}}
        with (
            patch.multiple(
                sirilmosaic, quality_report=report, output_dir=Path('output'),
                run_id='seconds', auto_crop_enabled=True, auto_crop_coverage_percent=50,
            ),
            patch.object(sirilmosaic, 'execute_siril') as execute,
            patch.object(sirilmosaic, '_read_fits_array', return_value=seconds) as read,
            patch.object(sirilmosaic, '_read_fits_dimensions', return_value=(8, 6)),
            patch.object(sirilmosaic, 'require_siril_artifact'),
        ):
            sirilmosaic.create_cropped_master()
        read.assert_called_once_with(Path('seconds.fit'))
        execute.assert_any_call('boxselect 3 1 4 4')
        self.assertEqual(report['coverage']['crop_reference_coverage'], 300)
        self.assertEqual(report['coverage']['crop_threshold'], 150)
        self.assertEqual(report['coverage']['crop_unit'], 's')

    def test_cropped_master_reuses_valid_recorded_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            cropped = output / 'master_stack_resume_cropped.fit'
            self.write_fits_layer_header(cropped, 3)
            coverage = np.ones((10, 10), dtype=np.float32)
            report = {'coverage': {
                'path': 'coverage.fit',
                'cropped_master_path': str(cropped),
            }}
            with (
                patch.multiple(
                    sirilmosaic, quality_report=report, output_dir=output,
                    run_id='resume', active_master_path=None, auto_crop_enabled=True,
                    auto_crop_coverage_percent=100,
                ),
                patch.object(sirilmosaic, 'execute_siril') as execute,
                patch.object(sirilmosaic, '_read_fits_array', return_value=coverage),
                patch.object(sirilmosaic, '_read_fits_dimensions', return_value=(10, 10)),
            ):
                sirilmosaic.create_cropped_master()

            execute.assert_not_called()

    def test_coverage_map_uses_siril_maximize_placements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = Path(directory)

            def write_frame(path: Path, width: int, height: int, exposure: float) -> None:
                cards = [
                    "SIMPLE  =                    T",
                    "BITPIX  =                   32",
                    "NAXIS   =                    3",
                    f"NAXIS1  = {width:20d}",
                    f"NAXIS2  = {height:20d}",
                    "NAXIS3  =                    3",
                    f"EXPTIME = {exposure:20g}",
                    "END",
                ]
                header = b"".join(card.ljust(80).encode("ascii") for card in cards)
                data = np.ones((3, height, width), dtype=">i4").tobytes()
                path.write_bytes(
                    header.ljust(2880, b" ")
                    + data.ljust((len(data) + 2879) // 2880 * 2880, b"\0")
                )

            write_frame(process / "r_light_00001.fit", 4, 3, 60)
            write_frame(process / "r_light_00002.fit", 4, 3, 300)
            self.assertEqual(
                sirilmosaic._read_fits_dimensions(process / "r_light_00001.fit"),
                (4, 3),
            )
            (process / "r_light_.seq").write_text(
                "S 'r_light_' 1 2 2 5 0 7 1 0 0\n"
                "L 3\n"
                "I 1 1 4,3\n"
                "I 2 1 4,3\n"
                "R0 1 1 1 1 1 1 H 1 0 0 0 1 0 0 0 1\n"
                "R0 1 1 1 1 1 1 H 1 0 2 0 1 1 0 0 1\n",
                encoding="ascii",
            )
            sirilmosaic.coverage_map_enabled = True
            sirilmosaic.output_dir = process
            sirilmosaic.run_id = "coverage_origin_test"
            sirilmosaic.quality_report = {}

            report = sirilmosaic.generate_coverage_map(process, "r_light")
            coverage = sirilmosaic._read_fits_array(process / "coverage_map_coverage_origin_test.fit")

            seconds = sirilmosaic._read_fits_array(Path(report['integration_time_path']))
            self.assertEqual(report["maximum_integration_seconds"], 360)
            self.assertEqual(coverage.shape, (5, 7))
            self.assertEqual(coverage.dtype, np.dtype(">f4"))
            expected_seconds = np.array([
                [60, 60, 60, 60, 0, 0, 0],
                [60, 60, 360, 360, 300, 300, 0],
                [60, 60, 360, 360, 300, 300, 0],
                [0, 0, 300, 300, 300, 300, 0],
                [0, 0, 0, 0, 0, 0, 0],
            ], dtype=np.float32)
            np.testing.assert_array_equal(seconds, expected_seconds)
            self.assertEqual(float(coverage[1, 2]), 1)
            self.assertEqual(set(np.unique(seconds)), {0, 60, 300, 360})
            np.testing.assert_allclose(coverage, seconds / 360)
            master = process / 'master_for_coverage_landmarks.fit'
            sirilmosaic._write_coverage_fits(master, np.ones(expected_seconds.shape))
            crop_plan = sirilmosaic.build_crop_plan(
                master, Path(report['integration_time_path']), 100
            )
            self.assertEqual(
                crop_plan['crop_bounds'], {'x': 2, 'y': 1, 'width': 4, 'height': 3}
            )
            self.assertEqual(
                crop_plan['crop_siril_selection'], {'x': 2, 'y': 1, 'width': 4, 'height': 3}
            )
            self.assertIn(b"BUNIT   = 's'", Path(report['integration_time_path']).read_bytes()[:2880])
            self.assertIn(b"BUNIT   = 'relative'", Path(report['path']).read_bytes()[:2880])
            write_frame(process / "r_light_00002.fit", 4, 3, 0)
            self.assertIsNone(sirilmosaic.generate_coverage_map(process, "r_light"))
            self.assertEqual(sirilmosaic.quality_report['coverage']['status'], 'unavailable')
            sequence_path = process / 'r_light_.seq'
            sequence_path.write_text(sequence_path.read_text().replace('I 2 1 4,3', 'I 2 0 4,3'))
            selected_report = sirilmosaic.generate_coverage_map(process, 'r_light')
            self.assertEqual(selected_report['frames_counted'], 1)
            self.assertEqual(selected_report['maximum_integration_seconds'], 60)
            sirilmosaic.coverage_map_enabled = False

    def test_run_id_avoids_existing_artifact_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "master_stack_20260912_120000.fit").touch()

            self.assertEqual(
                sirilmosaic.choose_run_id(output, "20260912_120000"),
                "20260912_120000_2",
            )

    def test_quality_report_parses_registration_and_stack_metrics(self) -> None:
        registration = [
            "log: Processing images of the sequence with a FWHM lower or equal than 4.27358 (268), "
            "processing images of the sequence with a roundness higher or equal than 0.823642 (273), "
            "processing images of the sequence with a background lower or equal than 0.044956 (268), "
            "processing images of the sequence with a number of stars higher or equal than 560 (273), "
            "for a total of images processed of 255)",
            "log: Using selected images filter (277/281 of the sequence)",
        ]
        stacking = [
            "log: Pixel rejection in channel #0: 0.076% - 0.702%",
            "log: Pixel rejection in channel #1: 0.068% - 0.810%",
            "log: Rejection stacking complete. 255 images have been stacked.",
            "log: Saving FITS: file stack.fit, 3 layer(s), 6655x7221 pixels, 32 bits",
        ]

        registration_summary = sirilmosaic.parse_registration_quality(registration, 281)
        stack_summary = sirilmosaic.parse_stack_quality(stacking)

        self.assertEqual(registration_summary["accepted_frames"], 255)
        self.assertEqual(registration_summary["rejected_frames"], 26)
        self.assertEqual(registration_summary["plate_solved_frames"], 277)
        self.assertEqual(registration_summary["quality_filters"]["fwhm"]["passing_frames"], 268)
        self.assertEqual(stack_summary["stacked_frames"], 255)
        self.assertEqual(stack_summary["pixel_rejection_percent"]["1"]["high"], 0.81)
        self.assertEqual(stack_summary["output"]["width"], 6655)
        rejection_diagnostics = stack_summary['rejection_diagnostics']
        self.assertEqual(rejection_diagnostics['status'], 'partial')
        self.assertEqual(rejection_diagnostics['missing_channels'], ['2'])
        self.assertAlmostEqual(
            rejection_diagnostics['channel_imbalance_percentage_points']['low'][
                'range_percentage_points'
            ],
            0.008,
        )

    def test_rejection_diagnostics_report_channel_ranges_and_unavailable_counts(self) -> None:
        complete = sirilmosaic.summarize_rejection_diagnostics({
            '0': {'low': 0.1, 'high': 0.4},
            '1': {'low': 0.2, 'high': 0.3},
            '2': {'low': 0.3, 'high': 0.5},
        }, expected_channels=3)
        absent = sirilmosaic.summarize_rejection_diagnostics({}, expected_channels=3)

        self.assertEqual(complete['status'], 'available')
        self.assertEqual(complete['channel_count'], 3)
        self.assertEqual(
            complete['channel_imbalance_percentage_points']['low'],
            {'minimum_percent': 0.1, 'maximum_percent': 0.3, 'range_percentage_points': 0.2},
        )
        self.assertEqual(
            complete['channel_imbalance_percentage_points']['high']['range_percentage_points'],
            0.2,
        )
        self.assertEqual(complete['rejected_pixel_counts']['status'], 'unavailable')
        self.assertIn('denominator', complete['rejected_pixel_counts']['reason'])
        self.assertEqual(absent['status'], 'unavailable')
        self.assertEqual(absent['channel_imbalance_percentage_points'], {})

    def test_rejection_map_diagnostics_count_affected_pixels_by_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            low = root / 'low_rejmap.fit'
            high = root / 'high_rejmap.fit'
            low_values = np.array([
                [[0, 0.25], [0.5, 0]],
                [[0.1, 0], [np.nan, 0.4]],
                [[0, 0], [0, 0]],
            ], dtype=np.float32)
            low_cards = [
                'SIMPLE  =                    T',
                'BITPIX  =                  -32',
                'NAXIS   =                    3',
                *[
                    f'NAXIS{axis:<1}  = {size:20d}'
                    for axis, size in enumerate(reversed(low_values.shape), 1)
                ],
                'END',
            ]
            low_header = b''.join(card.ljust(80).encode('ascii') for card in low_cards)
            low_header = low_header.ljust((len(low_header) + 2879) // 2880 * 2880, b' ')
            low_data = np.asarray(low_values, dtype='>f4').tobytes(order='C')
            low.write_bytes(low_header + low_data.ljust((len(low_data) + 2879) // 2880 * 2880, b'\0'))
            sirilmosaic._write_coverage_fits(high, np.zeros((2, 2), dtype=np.float32))

            result = sirilmosaic.rejection_map_diagnostics(
                {'low': low, 'high': high}, requested=True
            )

        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['maps']['low']['channels']['0']['pixel_locations'], 4)
        self.assertEqual(result['maps']['low']['channels']['0']['affected_pixel_locations'], 2)
        self.assertEqual(result['maps']['low']['channels']['0']['affected_pixel_percent'], 50.0)
        self.assertEqual(result['maps']['low']['channels']['1']['finite_pixel_locations'], 3)
        self.assertEqual(result['maps']['low']['channels']['1']['affected_pixel_percent'], 66.666667)
        self.assertEqual(result['maps']['low']['channels']['2']['affected_pixel_locations'], 0)
        self.assertEqual(result['maps']['high']['channels']['0']['affected_pixel_locations'], 0)

    def test_rejection_map_requests_follow_method_and_allow_directional_overrides(self) -> None:
        self.assertEqual(
            sirilmosaic.rejection_map_requests('none'),
            {'low': False, 'high': False},
        )
        self.assertEqual(
            sirilmosaic.rejection_map_requests('linear'),
            {'low': True, 'high': True},
        )
        self.assertEqual(
            sirilmosaic.rejection_map_requests('none', True, False),
            {'low': True, 'high': False},
        )
        self.assertEqual(
            sirilmosaic.rejection_map_requests('linear', False, True),
            {'low': False, 'high': True},
        )
        self.assertEqual(
            sirilmosaic.rejection_map_requests('linear', master=True, use_master_rejection=False),
            {'low': False, 'high': False},
        )
        self.assertEqual(
            sirilmosaic.rejection_map_requests(
                'none', True, False, master=True, use_master_rejection=False
            ),
            {'low': True, 'high': False},
        )

    def test_unrequested_rejection_map_is_removed_from_paired_siril_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            low = root / 'stack_low_rejmap.fit'
            high = root / 'stack_high_rejmap.fit'
            low.touch()
            high.touch()

            sirilmosaic.remove_unrequested_rejection_maps(
                root, 'stack', {'low': True, 'high': False}
            )

            self.assertTrue(low.is_file())
            self.assertFalse(high.exists())

    def test_rejection_map_diagnostics_report_unavailable_and_partial_maps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            low = root / 'low_rejmap.fit'
            sirilmosaic._write_coverage_fits(low, np.ones((2, 2), dtype=np.float32))
            missing = sirilmosaic.rejection_map_diagnostics(
                {'low': low, 'high': root / 'missing_high_rejmap.fit'}, requested=True
            )
            unreadable = root / 'unreadable_high_rejmap.fit'
            unreadable.write_bytes(b'invalid FITS')
            partial = sirilmosaic.rejection_map_diagnostics(
                {'low': low, 'high': unreadable}, requested=True
            )
            not_requested = sirilmosaic.rejection_map_diagnostics({}, requested=False)

        self.assertEqual(missing['status'], 'partial')
        self.assertEqual(missing['maps']['high']['reason'], 'map_missing')
        self.assertEqual(partial['status'], 'partial')
        self.assertEqual(partial['maps']['high']['reason'], 'map_unreadable')
        self.assertEqual(not_requested['status'], 'unavailable')
        self.assertEqual(not_requested['reason'], 'maps_not_requested')

    def test_rejection_map_diagnostics_mark_unselected_direction(self) -> None:
        result = sirilmosaic.rejection_map_diagnostics(
            {}, requested={'low': True, 'high': False}
        )

        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['maps']['low']['reason'], 'map_missing')
        self.assertEqual(result['maps']['high']['status'], 'not_requested')

    def test_rejection_map_diagnostics_explain_disabled_rejection(self) -> None:
        result = sirilmosaic.rejection_map_diagnostics(
            {},
            requested={'low': False, 'high': True},
            unavailable_reason='rejection_disabled',
        )

        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['maps']['low']['status'], 'not_requested')
        self.assertEqual(result['maps']['high']['status'], 'unavailable')
        self.assertEqual(result['maps']['high']['reason'], 'rejection_disabled')

    def test_rejection_map_summarizer_streams_across_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'map_rejmap.fit'
            sirilmosaic._write_coverage_fits(
                path, np.array([[0, 1, 0], [2, np.nan, 3]], dtype=np.float32)
            )

            result = sirilmosaic.summarize_rejection_map(path, chunk_pixels=2)

        channel = result['channels']['0']
        self.assertEqual(channel['pixel_locations'], 6)
        self.assertEqual(channel['finite_pixel_locations'], 5)
        self.assertEqual(channel['affected_pixel_locations'], 3)
        self.assertEqual(channel['affected_pixel_percent'], 60.0)

    def test_rejection_map_spatial_summary_identifies_concentrated_tile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'localized_rejmap.fit'
            values = np.zeros((4, 4), dtype=np.float32)
            values[0, 0] = 1
            sirilmosaic._write_coverage_fits(path, values)

            result = sirilmosaic.summarize_rejection_map(path)

        spatial = result['channels']['0']['spatial_concentration']
        self.assertEqual(spatial['grid_rows'], 4)
        self.assertEqual(spatial['grid_columns'], 4)
        self.assertEqual(spatial['overall_affected_pixel_percent'], 6.25)
        self.assertEqual(spatial['maximum_tile_affected_pixel_percent'], 100.0)
        self.assertEqual(spatial['maximum_tile_to_overall_rate_ratio'], 16.0)
        self.assertEqual(spatial['maximum_tile']['row'], 0)
        self.assertEqual(spatial['maximum_tile']['column'], 0)

    def test_rejection_stack_comparison_reports_candidate_minus_control(self) -> None:
        control = {
            'pixel_rejection_percent': {'0': {'low': 0.1, 'high': 0.2}},
            'rejection_diagnostics': {
                'channel_imbalance_percentage_points': {
                    'low': {'range_percentage_points': 0.05},
                    'high': {'range_percentage_points': 0.1},
                },
            },
            'maps': {
                'low': {'channels': {'0': {
                    'affected_pixel_percent': 10.0,
                    'spatial_concentration': {
                        'maximum_tile_affected_pixel_percent': 40.0,
                        'maximum_tile_to_overall_rate_ratio': 4.0,
                        'tile_affected_pixel_percent': [40.0, 0.0],
                    },
                }}},
            },
        }
        candidate = {
            'pixel_rejection_percent': {'0': {'low': 0.3, 'high': 0.5}},
            'rejection_diagnostics': {
                'channel_imbalance_percentage_points': {
                    'low': {'range_percentage_points': 0.08},
                    'high': {'range_percentage_points': 0.04},
                },
            },
            'maps': {
                'low': {'channels': {'0': {
                    'affected_pixel_percent': 12.5,
                    'spatial_concentration': {
                        'maximum_tile_affected_pixel_percent': 50.0,
                        'maximum_tile_to_overall_rate_ratio': 4.0,
                        'tile_affected_pixel_percent': [50.0, 0.0],
                    },
                }}},
            },
        }

        result = sirilmosaic._compare_rejection_stack(candidate, control)

        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['pixel_rejection_percent_delta']['0'], {'low': 0.2, 'high': 0.3})
        self.assertEqual(
            result['channel_imbalance_percentage_points_delta'],
            {'low': 0.03, 'high': -0.06},
        )
        self.assertEqual(
            result['rejection_map_delta']['low']['0']['tile_affected_pixel_percent'],
            [10.0, 0.0],
        )

    def test_selection_modes_ignore_inactive_values(self) -> None:
        self.assertEqual(
            sirilmosaic.build_selection_filters(True, 3, {}, True),
            {'bkg': '3.0k', 'round': '3.0k', 'fwhm': '3.0k'},
        )
        self.assertEqual(
            sirilmosaic.build_selection_filters(False, 'unused', {
                'bkg': 90, 'round': 95, 'fwhm': 100, 'nbstars': 'unused',
            }, True),
            {'bkg': '90%', 'round': '95%', 'fwhm': '100%'},
        )
        for sigma in (0, -1, float('nan'), float('inf')):
            with self.subTest(sigma=sigma), self.assertRaises(ValueError):
                sirilmosaic.build_selection_filters(True, sigma, {}, False)
        for percent in (0, 101, 1.5, float('nan')):
            with self.subTest(percent=percent), self.assertRaises(ValueError):
                sirilmosaic.build_selection_filters(False, None, {'bkg': percent}, True)

    def test_quality_report_uses_final_registration_and_saved_dimensions(self) -> None:
        registration = sirilmosaic.parse_registration_quality([
            "for a total of images processed of 97)",
            "Using selected images filter (99/100 of the sequence)",
            "96 images processed.",
            "Total: 0 failed, 96 exported.",
        ], 100)
        self.assertEqual(registration["accepted_frames"], 96)
        self.assertEqual(registration["rejected_frames"], 4)
        self.assertEqual(registration["quality_selected_frames"], 97)
        stack = sirilmosaic.parse_stack_quality([
            "Reading FITS: file input.fit, 3 layer(s), 2167x3844 pixels, 32 bits",
            "Saving FITS: file stack.fit, 3 layer(s), 2412x4107 pixels, 32 bits",
        ])
        self.assertEqual(stack["output"]["width"], 2412)
        self.assertEqual(stack["output"]["height"], 4107)

    def test_sky_condition_score_uses_available_relative_metrics(self) -> None:
        score = sirilmosaic.calculate_sky_condition_score([{
            "input_frames": 100,
            "sequence_frames": 100,
            "quality_filters": {
                "background": {"passing_frames": 90},
                "stars": {"passing_frames": 80},
                "fwhm": {"passing_frames": 70},
                "roundness": {"passing_frames": 60},
            },
            "plate_solved_frames": 100,
        }])

        self.assertEqual(score["score"], 81.5)
        self.assertEqual(score["classification"], "Good")
        self.assertIn("not a Bortle", score["basis"])
        self.assertEqual(score["components"]["background"], 90.0)
        self.assertEqual(score["components"]["plate_solved"], 100.0)

    def test_mosaic_aware_score_excludes_raw_star_count(self) -> None:
        report = {
            "input_frames": 100,
            "sequence_frames": 100,
            "quality_filters": {
                "background": {"passing_frames": 100},
                "stars": {"passing_frames": 0},
                "fwhm": {"passing_frames": 100},
                "roundness": {"passing_frames": 100},
            },
            "plate_solved_frames": 100,
        }

        score = sirilmosaic.calculate_sky_condition_score([report], include_star_count=False)

        self.assertEqual(score["score"], 100.0)
        self.assertTrue(score["star_count_normalized"])

    def test_mosaic_aware_star_count_setting_parses(self) -> None:
        arguments = sirilmosaic.build_parser().parse_args(["--mosaic-aware-star-count"])
        sirilmosaic.configure(arguments)
        self.assertTrue(sirilmosaic.mosaic_aware_star_count)

    def test_sky_quality_keep_percentage_parses(self) -> None:
        arguments = sirilmosaic.build_parser().parse_args(["--sky-quality-percent", "90"])
        sirilmosaic.configure(arguments)
        self.assertEqual(sirilmosaic.sky_quality_percent, 90)

    def test_quality_report_is_written_with_unique_run_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sirilmosaic.output_dir = Path(directory)
            sirilmosaic.run_id = "test_run"
            sirilmosaic.quality_report = {"run_id": "test_run"}

            sirilmosaic.write_quality_report("complete")

            report_path = Path(directory) / "quality_report_test_run.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["run_id"], "test_run")

    def test_artifact_inventory_fingerprints_generated_fits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'master.fit'
            sirilmosaic._write_coverage_fits(path, np.ones((2, 3), dtype=np.float32))

            inventory = sirilmosaic.build_artifact_inventory({'master': {'path': str(path)}})

            self.assertEqual(len(inventory), 1)
            self.assertEqual(inventory[0]['role'], 'master')
            self.assertEqual(inventory[0]['bytes'], path.stat().st_size)
            self.assertEqual(inventory[0]['sha256'], sirilmosaic._sha256_file(path))
            self.assertEqual((inventory[0]['width'], inventory[0]['height']), (3, 2))

    def test_experiment_record_requires_identical_input_content_and_captures_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_paths = []
            fingerprints = [
                {'path': 'panel/light_a.fit', 'size': 10, 'sha256': 'a' * 64},
                {'path': 'panel/light_b.fit', 'size': 20, 'sha256': 'b' * 64},
            ]
            for run_id, entries, settings, seconds in (
                ('control', fingerprints, {'rejection_method': 'none'}, 120),
                ('candidate', list(reversed([
                    {**fingerprints[0], 'path': 'copied/light_a.fit'},
                    {**fingerprints[1], 'path': 'copied/light_b.fit'},
                ])), {'rejection_method': 'winsorized'}, 150),
            ):
                output = root / run_id
                output.mkdir()
                master_path = output / 'master.fit'
                rejection_map_path = output / 'master_low_rejmap.fit'
                sirilmosaic._write_coverage_fits(master_path, np.ones((2, 3), dtype=np.float32))
                sirilmosaic._write_coverage_fits(
                    rejection_map_path, np.array([[0, 1, 0], [0, 0, 2]], dtype=np.float32)
                )
                manifest = output / 'manifest.json'
                manifest.write_text(json.dumps({'selected_files': entries}), encoding='utf-8')
                report_path = output / f'quality_report_{run_id}.json'
                report_path.write_text(json.dumps({
                    'run_id': run_id,
                    'status': 'complete',
                    'started_at': '2026-09-23T10:00:00+00:00',
                    'updated_at': f'2026-09-23T10:{seconds // 60:02d}:{seconds % 60:02d}+00:00',
                    'input_manifest': str(manifest),
                    'input_frames': 2,
                    'settings': settings,
                    'configuration_hash': f'hash-{run_id}',
                    'seed': 42,
                    'master': {
                        'path': str(master_path),
                        'rejection_map_diagnostics': {'status': 'partial', 'maps': {}},
                    },
                    'artifact_inventory': [{
                        'role': 'master', 'path': f'{run_id}.fit', 'bytes': 100,
                        'width': 4, 'height': 5, 'sha256': run_id * 4,
                    }],
                    'substacks': [{
                        'number': 1,
                        'stack': {
                            'pixel_rejection_percent': {'0': {'low': 0.1, 'high': 0.2}},
                            'rejection_map_diagnostics': {'status': 'available', 'maps': {}},
                        },
                    }],
                }), encoding='utf-8')
                report_paths.append(report_path)

            destination = root / 'experiment.json'
            destination, record = sirilmosaic.write_experiment_record(
                report_paths, destination, control_run_id='control',
                conclusion='Retain the conservative control pending image review.',
            )

            self.assertTrue(destination.is_file())
            self.assertFalse(destination.with_suffix('.tmp').exists())
            self.assertEqual(record['control_run_id'], 'control')
            self.assertEqual(record['conclusion'], 'Retain the conservative control pending image review.')
            self.assertEqual(record['input_frames'], 2)
            self.assertEqual([run['runtime_seconds'] for run in record['runs']], [120, 150])
            self.assertEqual(record['runs'][1]['settings']['rejection_method'], 'winsorized')
            self.assertEqual(record['runs'][0]['output_identities'][0]['sha256'], 'control' * 4)
            self.assertEqual(record['runs'][0]['control_comparison']['status'], 'control_run')
            candidate_comparison = record['runs'][1]['control_comparison']
            self.assertEqual(candidate_comparison['control_run_id'], 'control')
            self.assertEqual(
                candidate_comparison['substacks'][0]['pixel_rejection_percent_delta']['0'],
                {'low': 0.0, 'high': 0.0},
            )
            map_identity = next(
                item for item in record['runs'][0]['output_identities']
                if item['role'] == 'rejection_map_low'
            )
            self.assertEqual(map_identity['sha256'], sirilmosaic._sha256_file(root / 'control' / 'master_low_rejmap.fit'))
            self.assertIn('pixel_rejection_percent', record['runs'][0]['rejection_diagnostics']['substacks'][0])
            self.assertEqual(
                record['runs'][0]['rejection_diagnostics']['substacks'][0]
                ['rejection_diagnostics']['status'],
                'available',
            )
            self.assertEqual(
                record['runs'][0]['rejection_diagnostics']['master']
                ['rejection_diagnostics']['status'],
                'unavailable',
            )
            self.assertEqual(json.loads(destination.read_text(encoding='utf-8')), record)

    def test_experiment_record_rejects_different_inputs_and_invalid_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = []
            for run_id, fingerprint in (('first', 'a' * 64), ('second', 'c' * 64)):
                output = root / run_id
                output.mkdir()
                manifest = output / 'manifest.json'
                manifest.write_text(json.dumps({
                    'selected_files': [{'path': 'light.fit', 'size': 10, 'sha256': fingerprint}],
                }), encoding='utf-8')
                report = output / 'quality_report.json'
                report.write_text(json.dumps({
                    'run_id': run_id, 'status': 'complete', 'input_manifest': str(manifest),
                }), encoding='utf-8')
                reports.append(report)

            with self.assertRaisesRegex(ValueError, 'same selected input-file identities'):
                sirilmosaic.build_experiment_record(reports)
            second_manifest = root / 'second' / 'manifest.json'
            second_manifest.write_text(json.dumps({
                'selected_files': [{'path': 'light.fit', 'size': 10, 'sha256': 'a' * 64}],
            }), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'control run'):
                sirilmosaic.build_experiment_record(
                    reports, control_run_id='not-selected'
                )

            report = json.loads(reports[0].read_text(encoding='utf-8'))
            report['status'] = 'failed'
            reports[0].write_text(json.dumps(report), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'completed runs'):
                sirilmosaic.build_experiment_record([reports[0], reports[1]])

    def test_experiment_record_accepts_three_runs_with_one_shared_input_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = []
            for run_id in ('control', 'candidate_a', 'candidate_b'):
                run_dir = root / run_id
                run_dir.mkdir()
                master_path = run_dir / 'master.fit'
                sirilmosaic._write_coverage_fits(
                    master_path, np.ones((2, 3), dtype=np.float32)
                )
                manifest = run_dir / 'manifest.json'
                manifest.write_text(json.dumps({
                    'selected_files': [{'path': f'{run_id}/light.fit', 'size': 5, 'sha256': 'd' * 64}],
                }), encoding='utf-8')
                report = run_dir / 'quality_report.json'
                report.write_text(json.dumps({
                    'run_id': run_id, 'status': 'complete', 'input_manifest': str(manifest),
                    'master': {'path': str(master_path)},
                }), encoding='utf-8')
                reports.append(report)

            record = sirilmosaic.build_experiment_record(reports, control_run_id='control')

        self.assertEqual([run['run_id'] for run in record['runs']], ['control', 'candidate_a', 'candidate_b'])
        self.assertEqual(record['control_run_id'], 'control')
        self.assertTrue(all(run['output_identities'] for run in record['runs']))

    def test_experiment_record_requires_master_output_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = []
            for run_id in ('first', 'second'):
                run_dir = root / run_id
                run_dir.mkdir()
                manifest = run_dir / 'manifest.json'
                manifest.write_text(json.dumps({
                    'selected_files': [{'path': 'light.fit', 'size': 5, 'sha256': 'e' * 64}],
                }), encoding='utf-8')
                report = run_dir / 'quality_report.json'
                report.write_text(json.dumps({
                    'run_id': run_id, 'status': 'complete', 'input_manifest': str(manifest),
                }), encoding='utf-8')
                reports.append(report)

            with self.assertRaisesRegex(ValueError, 'master output fingerprint'):
                sirilmosaic.build_experiment_record(reports)

    def test_resume_artifact_reuse_rejects_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'master.fit'
            sirilmosaic._write_coverage_fits(path, np.ones((2, 3), dtype=np.float32))
            artifact = sirilmosaic.require_siril_artifact(path, 'Master')
            saved_report = sirilmosaic.quality_report
            try:
                sirilmosaic.quality_report = {'artifact_postconditions': [artifact]}
                self.assertTrue(sirilmosaic.reusable_report_artifact(
                    {'path': str(path)}, 'path', path, 'Master'
                ))
                data = bytearray(path.read_bytes())
                data[-1] = 1
                path.write_bytes(data)
                self.assertFalse(sirilmosaic.reusable_report_artifact(
                    {'path': str(path)}, 'path', path, 'Master'
                ))
            finally:
                sirilmosaic.quality_report = saved_report

    def test_apply_runtime_settings_restores_canonical_checkpoint_values(self) -> None:
        saved = sirilmosaic.runtime_settings()
        restored = dict(
            saved,
            drizzle=True,
            drizzle_scale='2',
            low_rejection_map=True,
            high_rejection_map=False,
            seed=12345,
        )
        try:
            sirilmosaic.apply_runtime_settings(restored)
            self.assertTrue(sirilmosaic.drizzle_enabled)
            self.assertEqual(sirilmosaic.drizzle_scale, '2')
            self.assertTrue(sirilmosaic.low_rejection_map_enabled)
            self.assertFalse(sirilmosaic.high_rejection_map_enabled)
            self.assertEqual(sirilmosaic.run_seed, 12345)
            self.assertEqual(sirilmosaic.configuration_hash(), sirilmosaic.configuration_hash(restored))
        finally:
            sirilmosaic.apply_runtime_settings(saved)

    def test_frame_ledger_records_all_frame_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "process"
            process.mkdir()
            first = root / "first.fit"
            second = root / "second.fit"
            self.write_fits_layer_header(first, 3, 60)
            self.write_fits_layer_header(second, 3, 300)
            for sequence in ("light", "pp_light", "bkg_pp_light"):
                self.write_fits_layer_header(process / f"{sequence}_00001.fit", 3, 60)
                self.write_fits_layer_header(process / f"{sequence}_00002.fit", 3, 300)
            self.write_fits_layer_header(process / "r_bkg_pp_light_00001.fit", 3, 60)
            sirilmosaic.cosmetic_correction = False
            sirilmosaic.drizzle_enabled = False

            report = sirilmosaic.summarize_substack_frames(
                [first, second], process, "pp_light", "bkg_pp_light",
                {"sequence_frames": 2, "quality_filters": {}},
                {"stacked_frames": 1}, [],
            )

            self.assertEqual(len(report["frame_ledger_records"]), 2)
            statuses = {record["file"]: record["status"] for record in report["frame_ledger_records"]}
            self.assertEqual(statuses["first.fit"], "stacked")
            self.assertEqual(statuses["second.fit"], "rejected")
            self.assertIn("stages", report["frame_ledger_records"][0])

    def test_frame_ledger_writer_appends_jsonl_and_updates_report_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sirilmosaic.output_dir = Path(directory)
            sirilmosaic.run_id = "ledger_test"
            sirilmosaic.frame_ledger_path = Path(directory) / "frame_ledger_ledger_test.jsonl"
            sirilmosaic.quality_report = {}
            sirilmosaic.journal_path = None

            sirilmosaic.append_frame_ledger([
                {"file": "first.fit", "status": "stacked"},
                {"file": "second.fit", "status": "rejected"},
            ])

            records = [
                json.loads(line)
                for line in sirilmosaic.frame_ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([record["file"] for record in records], ["first.fit", "second.fit"])
            self.assertEqual(sirilmosaic.quality_report["frame_ledger_records"], 2)

    def test_replay_frame_ledger_predicts_selected_frames_and_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "frame_ledger.jsonl"
            values = ((2, 0.90, 0.01, 100), (4, 0.80, 0.02, 200), (6, 0.70, 0.03, 300), (8, 0.60, 0.04, 400))
            ledger.write_text("\n".join(json.dumps({
                "file": f"frame_{index}.fit",
                "status": "stacked" if index == 1 else "rejected",
                "cohort": "panel_a" if index < 3 else "panel_b",
                "exposure_seconds": 60,
                "filter_metrics": {
                    "fwhm": {"value": fwhm},
                    "roundness": {"value": roundness},
                    "background": {"value": background},
                    "stars": {"value": stars},
                },
            }) for index, (fwhm, roundness, background, stars) in enumerate(values, 1)) + "\n", encoding="utf-8")

            replay = sirilmosaic.replay_frame_ledger(ledger, {
                "background": 75,
                "roundness": 75,
                "fwhm": 75,
                "stars": 75,
            })

            self.assertEqual(replay["predicted"]["selected_frames"], 2)
            self.assertEqual(replay["predicted"]["excluded_frames"], 2)
            self.assertEqual(replay["predicted"]["integrated_exposure_seconds"], 120.0)
            self.assertEqual(replay["cohorts"]["panel_a"]["selected"], 1)
            self.assertEqual(replay["comparison"]["current_selected_frames"], 1)
            self.assertEqual(replay["comparison"]["selected_frame_delta"], 1)
            self.assertFalse(replay["coverage_impact"]["available"])

    def test_checkpoint_inspection_validates_artifacts_and_requested_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "frames"
            output = root / "output"
            workdir.mkdir()
            output.mkdir()
            manifest = output / "input_manifest.json"
            report = output / "quality_report.json"
            ledger = output / "frame_ledger.jsonl"
            for path in (manifest, report, ledger):
                path.write_text("{}", encoding="utf-8")
            checkpoint = {
                "schema_version": 1,
                "state": "interrupted",
                "workdir": str(workdir),
                "output_dir": str(output),
                "substack_count": 1,
                "completed_substacks": [],
                "groups": {"1": ["light.fit"]},
                "input_manifest": str(manifest),
                "quality_report": str(report),
                "frame_ledger": str(ledger),
            }
            (output / sirilmosaic.RUN_CHECKPOINT).write_text(json.dumps(checkpoint), encoding="utf-8")

            inspected = sirilmosaic.inspect_checkpoint(output, workdir)
            self.assertEqual(inspected["status"], "AVAILABLE")
            self.assertTrue(all(check["status"] != "FAIL" for check in inspected["checks"]))
            mismatched = sirilmosaic.inspect_checkpoint(output / "other", workdir)
            self.assertEqual(mismatched["status"], "NONE")

    def test_cleanup_review_never_deletes_staged_source_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "frames"
            output = root / "output"
            (workdir / "Lights_sorted" / "group_1" / "lights").mkdir(parents=True)
            (workdir / "Lights_sorted" / "group_1" / "lights" / "frame.fit").write_bytes(b"source")
            (workdir / "substacks").mkdir(parents=True)
            output.mkdir()
            temporary = output / "artifact.tmp"
            temporary.write_text("temporary", encoding="utf-8")

            candidates = sirilmosaic.stale_artifacts(workdir, output)
            staged = next(item for item in candidates if item["path"].endswith("Lights_sorted"))
            self.assertFalse(staged["safe_to_delete"])
            with self.assertRaises(ValueError):
                sirilmosaic.delete_stale_artifacts(workdir, output, [staged["path"]])
            sirilmosaic.delete_stale_artifacts(workdir, output, [str(temporary)])
            self.assertFalse(temporary.exists())
            self.assertTrue((workdir / "Lights_sorted" / "group_1" / "lights" / "frame.fit").exists())

    def test_run_lock_is_exclusive_and_owner_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.mkdir()
            sirilmosaic.output_dir = output
            sirilmosaic.workdir = Path(directory) / "frames"
            sirilmosaic.run_id = "lock_test"

            lock_path = sirilmosaic.acquire_run_lock()
            self.assertTrue(lock_path.is_file())
            self.assertEqual(sirilmosaic.inspect_run_lock(output)["status"], "AVAILABLE")
            with self.assertRaisesRegex(RuntimeError, "Another run owns"):
                sirilmosaic.acquire_run_lock()
            sirilmosaic.release_run_lock()
            self.assertEqual(sirilmosaic.inspect_run_lock(output)["status"], "NONE")

    def test_integrity_scan_reports_exact_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "frame_01.fit"
            duplicate = root / "frame_02.fit"
            self.write_fits_layer_header(first, 1, 60)
            shutil.copy2(first, duplicate)

            scan = sirilmosaic.scan_input_integrity(
                root,
                root / "output",
                files=[first, duplicate],
                bayer_pattern="auto",
                orientation="top-down",
                drizzle=False,
            )

            self.assertEqual(scan["status"], "WARN")
            duplicate_check = next(item for item in scan["checks"] if item["name"] == "duplicates")
            self.assertEqual(duplicate_check["status"], "WARN")
            self.assertEqual(len(duplicate_check["groups"]), 1)

    def test_integrity_scan_reuses_cache_until_input_metadata_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            first = root / "frame_01.fit"
            self.write_fits_layer_header(first, 1, 60)

            initial = sirilmosaic.scan_input_integrity(root, output, files=[first])
            cached = sirilmosaic.scan_input_integrity(root, output, files=[first])
            self.assertFalse(initial["cached"])
            self.assertTrue(cached["cached"])
            self.assertTrue((output / "integrity_scan_cache.json").is_file())

            first.write_bytes(first.read_bytes() + b"changed")
            refreshed = sirilmosaic.scan_input_integrity(root, output, files=[first])
            self.assertFalse(refreshed["cached"])

    def test_deep_integrity_scan_rejects_truncated_fits_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            frame = root / "frame.fit"
            self.write_fits_layer_header(frame, 1, 60)

            scan = sirilmosaic.scan_input_integrity(root, output, files=[frame], deep_payload=True)

            self.assertEqual(scan["status"], "FAIL")
            payload_check = next(item for item in scan["checks"] if item["name"] == "deep_payload")
            self.assertEqual(payload_check["status"], "FAIL")

    def test_runtime_disk_headroom_guard_aborts_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sirilmosaic.workdir = Path(directory) / "frames"
            sirilmosaic.output_dir = Path(directory) / "output"
            sirilmosaic.minimum_free_disk_gb = 0.5
            disk_usage = shutil.disk_usage(Path(directory))
            low_space = type(disk_usage)(disk_usage.total, disk_usage.used, 100)
            with patch.object(sirilmosaic.shutil, 'disk_usage', return_value=low_space):
                with self.assertRaisesRegex(RuntimeError, 'disk headroom'):
                    sirilmosaic.check_runtime_disk_headroom()

    def test_preflight_storage_guard_uses_workload_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sirilmosaic.workdir = Path(directory) / 'frames'
            sirilmosaic.output_dir = Path(directory) / 'output'
            sirilmosaic.minimum_free_disk_gb = 0.5
            sirilmosaic.drizzle_enabled = False
            disk_usage = shutil.disk_usage(Path(directory))
            low_space = type(disk_usage)(disk_usage.total, disk_usage.used, 700)
            with patch.object(sirilmosaic.shutil, 'disk_usage', return_value=low_space):
                with self.assertRaisesRegex(RuntimeError, 'No input frames were staged'):
                    sirilmosaic.check_preflight_storage_headroom(100)

    def test_failure_context_includes_traceback_and_recovery_action(self) -> None:
        sirilmosaic.active_phase = 'Master integration'
        sirilmosaic.active_cohort_id = 'camera=test'
        sirilmosaic.quality_report = {'active_substack': 3, 'last_failed_command': 'stack test'}
        try:
            raise TypeError('synthetic failure')
        except TypeError as error:
            context = sirilmosaic.build_failure_context(error)

        self.assertEqual(context['exception_type'], 'TypeError')
        self.assertEqual(context['phase'], 'Master integration')
        self.assertEqual(context['substack'], 3)
        self.assertIn('raise TypeError', context['traceback'])
        self.assertIn('Resume Run', context['recovery_action'])

    def _write_verifiable_run(self, root: Path) -> Path:
        workdir = root / "frames"
        output = root / "output"
        rejects = workdir / "rejects"
        workdir.mkdir()
        output.mkdir()
        rejects.mkdir()
        for name in ("light_01.fit", "light_02.fit"):
            (workdir / name).write_bytes(b"synthetic light")
        master = output / "master_stack_verify.fit"
        integration = output / "integration_time_map_verify.fit"
        cropped = output / "master_stack_verify_cropped.fit"
        for path, shape in ((master, (6, 8)), (integration, (6, 8)), (cropped, (4, 5))):
            sirilmosaic._write_coverage_fits(
                path,
                np.ones(shape, dtype=np.float32),
                unit="s",
            )
        manifest_path = output / "input_manifest_verify.json"
        manifest_path.write_text(json.dumps({
            "selected_files": [
                {"path": "light_01.fit"},
                {"path": "light_02.fit"},
            ],
        }), encoding="utf-8")
        journal_path = output / "run_events_verify.jsonl"
        journal_path.write_text(
            json.dumps({"sequence": 1, "event": "run_started"}) + "\n"
            + json.dumps({"sequence": 2, "event": "run_finished", "status": "complete"}) + "\n",
            encoding="utf-8",
        )
        ledger_path = output / "frame_ledger_verify.jsonl"
        ledger_path.write_text(
            json.dumps({"file": "light_01.fit", "status": "stacked"}) + "\n"
            + json.dumps({"file": "light_02.fit", "status": "stacked"}) + "\n",
            encoding="utf-8",
        )
        report_path = output / "quality_report_verify.json"
        report_path.write_text(json.dumps({
            "report_schema_version": 3,
            "run_id": "verify",
            "status": "complete",
            "input_frames": 2,
            "settings": {
                "auto_crop_master": True,
                "rejects_directory": str(rejects),
            },
            "input_manifest": str(manifest_path),
            "journal_path": str(journal_path),
            "frame_ledger_path": str(ledger_path),
            "master": {"path": str(master)},
            "coverage": {
                "integration_time_path": str(integration),
                "cropped_master_path": str(cropped),
            },
            "rejected_files": [],
            "substacks": [{
                "input_frames": 2,
                "accepted_frames": 2,
                "rejected_frames": 0,
                "stack": {"stacked_frames": 2},
            }],
        }), encoding="utf-8")
        return report_path

    def test_verify_run_passes_consistent_completed_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verification = sirilmosaic.verify_run(self._write_verifiable_run(Path(directory)))

            self.assertEqual(verification["status"], "PASS")
            self.assertEqual(verification["counts"]["FAIL"], 0)
            self.assertGreaterEqual(verification["counts"]["PASS"], 8)

    def test_verify_run_fails_changed_inventoried_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = self._write_verifiable_run(Path(directory))
            report = json.loads(report_path.read_text(encoding='utf-8'))
            report['artifact_inventory'] = sirilmosaic.build_artifact_inventory(report)
            report_path.write_text(json.dumps(report), encoding='utf-8')
            master_path = Path(report['master']['path'])
            data = bytearray(master_path.read_bytes())
            data[-1] = 1
            master_path.write_bytes(data)

            verification = sirilmosaic.verify_run(report_path)

            check = next(
                item for item in verification['checks']
                if item['name'] == 'artifact_inventory'
            )
            self.assertEqual(check['status'], 'FAIL')
            self.assertEqual(verification['status'], 'FAIL')

    def test_verify_run_warns_when_crop_retains_under_one_percent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = self._write_verifiable_run(Path(directory))
            report = json.loads(report_path.read_text(encoding='utf-8'))
            self.write_fits_layer_header(Path(report['master']['path']), 3, width=100, height=100)
            cropped = report_path.parent / 'tiny_cropped.fit'
            self.write_fits_layer_header(cropped, 3, width=5, height=5)
            report['coverage']['cropped_master_path'] = str(cropped)
            report_path.write_text(json.dumps(report), encoding='utf-8')

            verification = sirilmosaic.verify_run(report_path)

            check = next(
                item for item in verification['checks']
                if item['name'] == 'master_crop_retained_area'
            )
            self.assertEqual(check['status'], 'WARN')

    def test_verify_run_accepts_rejected_frame_restored_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = self._write_verifiable_run(Path(directory))
            report = json.loads(report_path.read_text(encoding='utf-8'))
            report['rejected_files'] = [{'path': 'light_02.fit'}]
            report['substacks'][0].update({
                'accepted_frames': 1,
                'rejected_frames': 1,
                'stack': {'stacked_frames': 1},
            })
            report_path.write_text(json.dumps(report), encoding='utf-8')
            ledger_path = Path(report['frame_ledger_path'])
            ledger_path.write_text(
                json.dumps({'file': 'light_01.fit', 'status': 'stacked'}) + '\n'
                + json.dumps({'file': 'light_02.fit', 'status': 'rejected'}) + '\n',
                encoding='utf-8',
            )

            verification = sirilmosaic.verify_run(report_path)
            agreement = next(
                item for item in verification['checks']
                if item['name'] == 'rejected_file_agreement'
            )

            self.assertEqual(agreement['status'], 'PASS')
            self.assertIn('1 were restored to source', agreement['detail'])

    def test_verify_run_fails_missing_required_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = self._write_verifiable_run(Path(directory))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            Path(report["master"]["path"]).unlink()

            verification = sirilmosaic.verify_run(report_path)

            self.assertEqual(verification["status"], "FAIL")
            self.assertTrue(any(check["name"] == "master_artifact" for check in verification["checks"]))

    def test_verify_run_fails_missing_or_duplicate_cohort_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = self._write_verifiable_run(Path(directory))
            report = json.loads(report_path.read_text(encoding='utf-8'))
            report['settings'].update({'export_per_cohort': True, 'coverage_map': True})
            report['cohorts'] = [{'id': 'cohort-a'}, {'id': 'cohort-b'}]
            master = dict(report.pop('master'), cohort='cohort-a')
            coverage = dict(report.pop('coverage'), cohort='cohort-a')
            report['masters'] = [master, dict(master)]
            report['coverages'] = [coverage]
            report_path.write_text(json.dumps(report), encoding='utf-8')

            verification = sirilmosaic.verify_run(report_path)
            checks = {check['name']: check for check in verification['checks']}

            self.assertEqual(verification['status'], 'FAIL')
            self.assertEqual(checks['cohort_master_records']['status'], 'FAIL')
            self.assertIn("missing=['cohort-b']", checks['cohort_master_records']['detail'])
            self.assertIn("duplicates=['cohort-a']", checks['cohort_master_records']['detail'])
            self.assertEqual(checks['cohort_coverage_records']['status'], 'FAIL')


    def test_verification_artifact_and_run_bundle_include_current_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = self._write_verifiable_run(Path(directory))
            verification_path, verification = sirilmosaic.write_verification_artifact(report_path)
            bundle_path = sirilmosaic.create_run_bundle(report_path)

            self.assertEqual(verification["status"], "PASS")
            self.assertTrue(verification_path.is_file())
            with zipfile.ZipFile(bundle_path) as bundle:
                names = set(bundle.namelist())
            self.assertIn(report_path.name, names)
            self.assertIn(verification_path.name, names)
            self.assertIn("frame_ledger_verify.jsonl", names)
            self.assertIn("configuration_verify.json", names)

    def test_integration_report_sums_exact_stacked_exposure(self) -> None:
        sirilmosaic.quality_report = {
            "substacks": [{
                "stack": {"stacked_frames": 2},
                "stage_exposure_seconds": {"stacked": 800.0},
            }]
        }
        sirilmosaic.finalize_integration_report({
            "frames": 10,
            "total_exposure_seconds": 1000.0,
            "known_exposure_frames": 10,
        })

        self.assertEqual(sirilmosaic.quality_report["integration"]["stacked_frames"], 2)
        self.assertEqual(sirilmosaic.quality_report["integration"]["total_input_hours"], 0.278)
        self.assertEqual(sirilmosaic.quality_report["integration"]["integrated_exposure_seconds"], 800.0)
        self.assertEqual(sirilmosaic.quality_report["integration"]["integrated_hours"], 0.222)
        self.assertEqual(
            sirilmosaic.quality_report["integration"]["estimated_integrated_hours"],
            0.222,
        )
        self.assertEqual(
            sirilmosaic.quality_report["integration"]["integrated_exposure_basis"],
            "sum of EXPTIME for frames that reached stacking",
        )

    def test_per_cohort_integration_report_sums_exact_stacked_exposure(self) -> None:
        sirilmosaic.quality_report = {
            "settings": {"export_per_cohort": True},
            "substacks": [
                {
                    "cohort": "cohort-a",
                    "stack": {"stacked_frames": 2},
                    "stage_exposure_seconds": {"stacked": 80.0},
                },
                {
                    "cohort": "cohort-b",
                    "stack": {"stacked_frames": 1},
                    "stage_exposure_seconds": {"stacked": 40.0},
                },
            ],
            "masters": [
                {"cohort": "cohort-a", "method": "single substack copy"},
                {"cohort": "cohort-b", "method": "single substack copy"},
            ],
        }
        sirilmosaic.finalize_integration_report({
            "frames": 4,
            "total_exposure_seconds": 200.0,
            "known_exposure_frames": 4,
        })

        integration = sirilmosaic.quality_report["integration"]
        self.assertTrue(integration["exposure_complete"])
        self.assertEqual(integration["integrated_exposure_seconds"], 120.0)
        self.assertEqual(integration["integrated_hours"], 0.033)
        self.assertEqual(integration["stacked_frames"], 3)

    def test_frame_report_records_exact_exposure_and_rejection_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "process"
            process.mkdir()
            first = root / "first.fit"
            second = root / "second.fit"
            self.write_fits_layer_header(first, 3, 60)
            self.write_fits_layer_header(second, 3, 300)
            for sequence in ("light", "cc_light", "pp_cc_light", "bkg_pp_cc_light"):
                self.write_fits_layer_header(process / f"{sequence}_00001.fit", 3, 60)
                self.write_fits_layer_header(process / f"{sequence}_00002.fit", 3, 300)
            self.write_fits_layer_header(process / "r_bkg_pp_cc_light_00001.fit", 3, 60)
            sirilmosaic.drizzle_enabled = False

            report = sirilmosaic.summarize_substack_frames(
                [second, first],
                process,
                "pp_cc_light",
                "bkg_pp_cc_light",
                {"sequence_frames": 2, "quality_filters": {"fwhm": {"passing_frames": 1}}},
                {"stacked_frames": 1},
                [],
            )

            self.assertEqual(report["stage_exposure_seconds"]["registered"], 60.0)
            self.assertEqual(report["discarded_frame_count"], 1)
            self.assertEqual(report["discarded_frames"][0]["status"], "rejected")
            self.assertEqual(report["discarded_frames"][0]["exposure_seconds"], 300.0)
            self.assertEqual(report["discarded_frames"][0]["reason_code"], "registration_excluded")
            self.assertEqual(report["discarded_frames"][0]["candidate_filters"], ["fwhm"])
            self.assertEqual(report["discarded_frames"][0]["filter_metrics"], {
                "fwhm": {
                    "value": None,
                    "threshold": None,
                    "comparison": "unavailable",
                    "status": "unavailable",
                }
            })
            self.assertEqual(report["stage_exposure_seconds"]["stacked"], 60)

            unknown = sirilmosaic.summarize_substack_frames(
                [second, first], process, 'pp_cc_light', 'bkg_pp_cc_light', {}, {}, [],
            )
            self.assertFalse(unknown['stack_identity_known'])
            self.assertEqual(unknown['unresolved_stack_membership_frames'], 1)
            self.assertEqual(unknown['discarded_frame_count'], 1)
            self.assertIsNone(unknown['stage_exposure_seconds']['stacked'])

            self.write_fits_layer_header(process / 'r_bkg_pp_cc_light_00001.fit', 3)
            missing = sirilmosaic.summarize_substack_frames(
                [second, first], process, 'pp_cc_light', 'bkg_pp_cc_light', {}, {'stacked_frames': 1}, [],
            )
            self.assertEqual(missing['stage_missing_exposure_frames']['registered'], 1)
            self.assertIsNone(missing['stage_exposure_seconds']['stacked'])

    def test_discarded_frame_report_includes_siril_filter_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "process"
            process.mkdir()
            first = root / "first.fit"
            second = root / "second.fit"
            self.write_fits_layer_header(first, 3, 60)
            self.write_fits_layer_header(second, 3, 300)
            sirilmosaic.cosmetic_correction = False
            sirilmosaic.drizzle_enabled = False
            for sequence in ("light", "pp_light", "bkg_pp_light"):
                self.write_fits_layer_header(process / f"{sequence}_00001.fit", 3, 60)
                self.write_fits_layer_header(process / f"{sequence}_00002.fit", 3, 300)
            self.write_fits_layer_header(process / "r_bkg_pp_light_00001.fit", 3, 60)
            (process / "bkg_pp_light_.seq").write_text(
                "S 'bkg_pp_light_' 1 2 2 5 -1 6 0 0 0\n"
                "L 3\n"
                "I 1 1\nI 2 1\n"
                "R1 8.2 8.5 0.42 1.0 0.12 123 H 1 0 0 0 1 0 0 0 1\n"
                "R1 4.1 4.2 0.91 1.0 0.02 456 H 1 0 0 0 1 0 0 0 1\n",
                encoding="utf-8",
            )

            report = sirilmosaic.summarize_substack_frames(
                [first, second],
                process,
                "pp_light",
                "bkg_pp_light",
                {
                    "sequence_frames": 2,
                    "quality_filters": {
                        "fwhm": {"threshold": 6.0, "passing_frames": 1},
                        "roundness": {"threshold": 0.6, "passing_frames": 2},
                        "background": {"threshold": 0.01, "passing_frames": 1},
                        "stars": {"threshold": 500, "passing_frames": 1},
                    },
                },
                {"stacked_frames": 1},
                [],
            )

            discarded = report["discarded_frames"][0]
            self.assertEqual(discarded["filter_metrics"]["fwhm"], {
                "value": 4.1,
                "threshold": 6.0,
                "comparison": "<=",
                "status": "passed",
            })
            self.assertEqual(discarded["filter_metrics"]["roundness"], {
                "value": 0.91,
                "threshold": 0.6,
                "comparison": ">=",
                "status": "passed",
            })
            self.assertEqual(
                discarded["reason"],
                "Excluded during registration; see filter_metrics for measured values",
            )
            self.assertEqual(discarded["filter_metric_source"], "Siril sequence registration data")

    def test_failed_command_telemetry_is_written_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sirilmosaic.output_dir = Path(directory)
            sirilmosaic.run_id = 'command_failure'
            sirilmosaic.quality_report = {'run_id': sirilmosaic.run_id}
            sirilmosaic.app = FakeSiril('set32bits')
            with self.assertRaises(RuntimeError):
                sirilmosaic.execute_siril('set32bits')
            sirilmosaic.write_quality_report('failed')
            report = json.loads(sirilmosaic.quality_report_path().read_text())
            self.assertEqual(report['last_failed_command'], 'set32bits')
            self.assertEqual(report['commands'][0]['status'], 'failed')
            self.assertGreaterEqual(report['commands'][0]['seconds'], 0)
            self.assertFalse(sirilmosaic.quality_report_path().with_suffix('.tmp').exists())

    def test_partial_plate_solve_continues_when_failed_frames_are_skipped(self) -> None:
        class PartialPlateSolve:
            def Execute(self, _command: str) -> bool:
                return False

            def GetData(self) -> list[str]:
                return [
                    'Sequence processing partially succeeded, with 2 images that failed.',
                    '3 images successfully platesolved out of 5 included.',
                    'Image bkg_pp_light_00004 did not solve',
                ]

        sirilmosaic.app = PartialPlateSolve()
        sirilmosaic.quality_report = {'active_substack': 1}
        sirilmosaic.skip_failed_frames = True
        sirilmosaic.siril_command_timeout = 0
        with patch.object(sirilmosaic, 'append_journal_event'):
            response = sirilmosaic.execute_siril(
                'seqplatesolve bkg_pp_light -order=3',
                allow_partial=True,
            )

        self.assertIn('Sequence processing partially succeeded', response[0])
        self.assertEqual(sirilmosaic.quality_report['commands'][0]['status'], 'partial')
        self.assertNotIn('last_failed_command', sirilmosaic.quality_report)

    def test_zero_plate_solve_is_reported_as_unusable_when_failed_frames_are_skipped(self) -> None:
        class NoPlateSolve:
            def Execute(self, _command: str) -> bool:
                return False

            def GetData(self) -> list[str]:
                return [
                    'Sequence processing failed, with 6 images that failed.',
                    '0 images successfully platesolved out of 6 included.',
                    'No stars found in image bkg_pp_light_00001.',
                ]

        sirilmosaic.app = NoPlateSolve()
        sirilmosaic.quality_report = {'active_substack': 1}
        sirilmosaic.skip_failed_frames = True
        sirilmosaic.siril_command_timeout = 0
        with patch.object(sirilmosaic, 'append_journal_event'):
            with self.assertRaises(sirilmosaic.NoPlateSolveFramesError) as context:
                sirilmosaic.execute_siril(
                    'seqplatesolve bkg_pp_light -order=3',
                    allow_partial=True,
                )

        self.assertIn('0 images successfully platesolved', str(context.exception))
        self.assertEqual(sirilmosaic.quality_report['commands'][0]['status'], 'no_usable_frames')
        self.assertIn('last_failed_command', sirilmosaic.quality_report)

    def test_siril_watchdog_times_out_and_marks_pipe_unresponsive(self) -> None:
        class SlowSiril(FakeSiril):
            def Execute(self, command: str) -> bool:
                time.sleep(0.05)
                return super().Execute(command)

        sirilmosaic.app = SlowSiril()
        sirilmosaic.siril_command_timeout = 0.001
        with patch.object(sirilmosaic, 'terminate_siril_processes', return_value=0) as terminate:
            with self.assertRaisesRegex(sirilmosaic.SirilCommandError, 'timed out'):
                sirilmosaic.execute_siril('set32bits')
        terminate.assert_called_once_with()
        self.assertTrue(sirilmosaic.siril_unresponsive)

    def test_siril_open_recovers_once_from_pipe_failure(self) -> None:
        first = Mock()
        first.Open.side_effect = RuntimeError('broken pipe: all pipe instances are busy')
        second = Mock()
        second.Open.return_value = True
        factory = Mock(side_effect=[second])
        sirilmosaic.app = first
        sirilmosaic.siril_open_timeout = 0
        with patch.object(sirilmosaic, 'terminate_siril_processes', return_value=1) as current_cleanup, patch.object(
            sirilmosaic, 'terminate_stale_run_processes', return_value=3
        ) as stale_cleanup:
            sirilmosaic.open_siril_with_recovery(factory)

        current_cleanup.assert_called_once_with()
        stale_cleanup.assert_called_once_with(sirilmosaic.workdir, sirilmosaic.output_dir)
        factory.assert_called_once()
        self.assertIs(sirilmosaic.app, second)

    def test_successful_command_and_manifest_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir, _ = self.make_project(root, light_count=2)
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = root / "output"
            sirilmosaic.output_dir.mkdir()
            sirilmosaic.run_id = "journal_test"
            sirilmosaic.run_seed = 12345
            sirilmosaic.journal_path = sirilmosaic.output_dir / "events.jsonl"
            sirilmosaic.quality_report = {'run_id': sirilmosaic.run_id, 'active_substack': 2}
            sirilmosaic.app = FakeSiril()

            sirilmosaic.execute_siril("set32bits")
            manifest_path = sirilmosaic.write_input_manifest(
                sirilmosaic.discover_light_files(workdir),
                [(None, sirilmosaic.discover_light_files(workdir))],
            )

            command = sirilmosaic.quality_report['commands'][0]
            self.assertEqual(command['status'], 'complete')
            self.assertEqual(command['substack'], 2)
            self.assertTrue(command['response_tail'])
            events = [json.loads(line) for line in sirilmosaic.journal_path.read_text().splitlines()]
            self.assertEqual(events[0]['event'], 'siril_command')
            self.assertEqual(events[0]['response_tail'], command['response_tail'])
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest['seed'], 12345)
            self.assertEqual(len(manifest['selected_files']), 2)
            self.assertEqual(len(manifest['cohorts'][0]['files']), 2)
            self.assertTrue(all('sha256' in entry for entry in manifest['selected_files']))

    def test_resume_input_identity_rejects_changed_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / 'frames'
            output = root / 'output'
            workdir.mkdir()
            output.mkdir()
            frame = workdir / 'light.fit'
            frame.write_bytes(b'original')
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = output
            sirilmosaic.run_id = 'identity_test'
            sirilmosaic.run_seed = 123
            manifest = sirilmosaic.write_input_manifest([frame], [(None, [frame])])
            checkpoint = {
                'input_manifest': str(manifest),
                'selected_files': ['light.fit'],
                'input_frames': 1,
            }
            frame.write_bytes(b'changed!')

            with self.assertRaisesRegex(RuntimeError, 'changed'):
                sirilmosaic.validate_resume_input_identity(checkpoint)

    def test_resume_input_identity_accepts_frame_left_in_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / 'frames'
            output = root / 'output'
            workdir.mkdir()
            output.mkdir()
            source = workdir / 'light.fit'
            self.write_fits_layer_header(source, 1, 60)
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = output
            sirilmosaic.run_id = 'staged_identity_test'
            sirilmosaic.run_seed = 123
            manifest_path = sirilmosaic.write_input_manifest([source], [(None, [source])])
            staged_lights = workdir / 'Lights_sorted' / 'group_1' / 'lights'
            staged_lights.mkdir(parents=True)
            (staged_lights.parent / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({'frame_00001.fit': 'light.fit'}), encoding='utf-8'
            )
            source.rename(staged_lights / 'frame_00001.fit')
            checkpoint = {
                'input_manifest': str(manifest_path),
                'selected_files': ['light.fit'],
                'input_frames': 1,
            }

            paths, _manifest, identity = sirilmosaic.validate_resume_input_identity(checkpoint)

            self.assertEqual(paths, [workdir / 'light.fit'])
            self.assertEqual(identity['staged_files'], ['light.fit'])

    @unittest.skipUnless(os.environ.get('SIRIL_TEST_CLI'), 'Set SIRIL_TEST_CLI for Siril command contract test')
    def test_real_siril_accepts_advanced_command_families(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'advanced_help.ssf'
            script.write_text(
                'requires 1.3.6\n'
                'help seqsubsky\n'
                'help seqplatesolve\n'
                'help register\n',
                encoding='utf-8',
            )
            result = subprocess.run(
                [os.environ['SIRIL_TEST_CLI'], '-s', str(script)],
                cwd=directory,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=90,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output[-6000:])
            for token in ('-smooth=', '-downscale', '-radius=', '-transf=', '-minpairs='):
                self.assertIn(token, output)

    def test_unknown_integration_is_not_proportional_estimate(self) -> None:
        sirilmosaic.quality_report = {'substacks': [{'stack': {'stacked_frames': 1}}]}
        sirilmosaic.finalize_integration_report({
            'frames': 2, 'total_exposure_seconds': 360, 'known_exposure_frames': 2,
        })
        self.assertIsNone(sirilmosaic.quality_report['integration']['integrated_hours'])
        self.assertFalse(sirilmosaic.quality_report['integration']['exposure_complete'])

        substack = {'stack': {'stacked_frames': 1}, 'stage_exposure_seconds': {'stacked': 60}}
        for substacks, master in (([], {}), ([substack, substack], {}),
                                  ([substack, substack], {'stacked_frames': 1})):
            with self.subTest(substacks=len(substacks), master=master):
                sirilmosaic.quality_report = {'substacks': substacks, 'master': master}
                sirilmosaic.finalize_integration_report({
                    'frames': 2, 'total_exposure_seconds': 120, 'known_exposure_frames': 2,
                })
                self.assertIsNone(sirilmosaic.quality_report['integration']['integrated_hours'])

    def test_failed_frames_can_be_unselected_and_reported_with_layers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = Path(directory)
            self.write_fits_layer_header(process / "pp_light_00001.fit", 3)
            self.write_fits_layer_header(process / "pp_light_00002.fit", 1)
            self.write_fits_layer_header(process / "pp_light_00003.fit", 3)
            fake = FakeSiril()
            sirilmosaic.app = fake
            sirilmosaic.skip_failed_frames = True
            sirilmosaic.quality_report = {}
            sirilmosaic.output_dir = process
            sirilmosaic.run_id = "failed_frame_test"

            sirilmosaic.skip_invalid_sequence_frames(process, "pp_light", 3)

            self.assertEqual(
                fake.commands,
                ["select pp_light 1 3", "unselect pp_light 2 2"],
            )
            self.assertEqual(sirilmosaic.quality_report["skipped_frames"][0]["file"], "pp_light_00002.fit")

    def test_failed_frame_skip_setting_is_disabled_by_default(self) -> None:
        arguments = sirilmosaic.build_parser().parse_args([])
        self.assertFalse(arguments.skip_failed_frames)

    def test_auto_substacks_resolves_to_siril_frame_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=4)
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--substacks", "1",
                "--auto-substacks",
            ])
            sirilmosaic.configure(arguments)
            with patch.object(sirilmosaic, "detect_siril_version", return_value="siril 1.4.4"):
                sirilmosaic.validate_parameters()

            self.assertEqual(sirilmosaic.SubStack_nb, 1)
            self.assertEqual(sirilmosaic.SIRIL_MAX_STACK_FRAMES, 8192)
            self.assertEqual(sirilmosaic.siril_max_stack_frames, 8192)

            with patch(
                "sirilmosaic.discover_light_files",
                return_value=[workdir / f"light_{index}.fit" for index in range(8193)],
            ):
                with patch.object(sirilmosaic, "detect_siril_version", return_value="siril 1.4.4"):
                    sirilmosaic.validate_parameters()

            self.assertEqual(sirilmosaic.SubStack_nb, 2)

            with patch(
                "sirilmosaic.discover_light_files",
                return_value=[workdir / f"light_{index}.fit" for index in range(8193)],
            ), patch.object(sirilmosaic, "detect_siril_version", return_value="siril 1.3.6"):
                sirilmosaic.validate_parameters()

            self.assertEqual(sirilmosaic.siril_max_stack_frames, 2048)
            self.assertEqual(sirilmosaic.SubStack_nb, 5)

    def test_siril_version_parser_uses_conservative_limit_when_unavailable(self) -> None:
        self.assertEqual(sirilmosaic.parse_siril_version("siril 1.4.4"), (1, 4, 4))
        self.assertEqual(sirilmosaic.siril_frame_limit_for_version("siril 1.4.4"), 8192)
        self.assertEqual(sirilmosaic.siril_frame_limit_for_version("siril 1.3.6"), 2048)
        self.assertEqual(sirilmosaic.siril_frame_limit_for_version("unavailable"), 2048)

    def test_gui_maps_all_core_inputs_to_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.workdir.set(str(workdir))
                output_dir = Path(directory) / "selected output"
                app.output_dir.set(str(output_dir))
                app.siril_exe.set(str(executable))
                app.substacks.set(3)
                app.auto_substacks.set(True)
                app.drizzle_scale.set(1.5)
                app.drizzle_kernel.set("square")
                app.filter_background.set(94)
                app.debug.set(True)
                app.drizzle.set(False)
                app.bayer_pattern.set("GRBG")
                app.bayer_orientation.set("Bottom-up")
                app.cosmetic_correction.set(True)
                app.cosmetic_cold_sigma.set(4.2)
                app.cosmetic_hot_sigma.set(3.4)
                app.overlap_normalization.set(False)
                app.stack_normalization.set("mulscale")
                app.plate_solve_order.set(5)
                app.plate_solve_downscale.set(True)
                app.plate_solve_radius.set("2.5")
                app.plate_solve_limit_mag.set("+1")
                app.rbf_smoothing.set(0.75)
                app.background_dither.set(False)
                app.registration_transform.set("similarity")
                app.registration_minpairs.set(8)
                app.registration_maxstars.set(500)
                app.registration_interpolation.set("cubic")
                app.random_seed.set("12345")
                app.adaptive_quality_filtering.set(True)
                app.quality_filter_sigma.set(2.8)
                app.background_method.set("RBF")
                app.background_samples.set(30)
                app.background_tolerance.set(1.3)
                app.fast_normalization.set(True)
                app.skip_failed_frames.set(True)
                app.export_per_cohort.set(True)
                app.coverage_map.set(True)
                app.auto_crop_master.set(True)
                app.rejection_method.set("winsorized")
                app.low_rejection_map.set("Always")
                app.high_rejection_map.set("Never")

                command = app.command()
                arguments = sirilmosaic.build_parser().parse_args(command[3:])

                self.assertEqual(arguments.workdir, workdir)
                self.assertEqual(arguments.output_dir, output_dir)
                self.assertEqual(arguments.siril_exe, executable)
                self.assertEqual(arguments.substacks, 3)
                self.assertTrue(arguments.auto_substacks)
                self.assertEqual(arguments.drizzle_scale, "1.5")
                self.assertEqual(arguments.drizzle_kernel, "square")
                self.assertNotIn('--filter-background', command)
                self.assertEqual(arguments.bayer_pattern, "GRBG")
                self.assertEqual(arguments.bayer_orientation, "bottom-up")
                self.assertTrue(arguments.cosmetic_correction)
                self.assertEqual(arguments.cosmetic_cold_sigma, "4.2")
                self.assertEqual(arguments.cosmetic_hot_sigma, "3.4")
                self.assertFalse(arguments.overlap_normalization)
                self.assertEqual(arguments.stack_normalization, "mulscale")
                self.assertEqual(arguments.plate_solve_order, 5)
                self.assertTrue(arguments.plate_solve_downscale)
                self.assertEqual(arguments.plate_solve_radius, 2.5)
                self.assertEqual(arguments.plate_solve_limit_mag, "+1")
                self.assertEqual(arguments.rbf_smoothing, "0.75")
                self.assertFalse(arguments.background_dither)
                self.assertEqual(arguments.registration_transform, "similarity")
                self.assertEqual(arguments.registration_minpairs, 8)
                self.assertEqual(arguments.registration_maxstars, 500)
                self.assertEqual(arguments.registration_interpolation, "cubic")
                self.assertEqual(arguments.seed, 12345)
                self.assertTrue(arguments.adaptive_quality_filtering)
                self.assertEqual(arguments.quality_filter_sigma, 2.8)
                self.assertEqual(arguments.background_method, "rbf")
                self.assertEqual(arguments.background_samples, 30)
                self.assertEqual(arguments.background_tolerance, 1.3)
                self.assertFalse(arguments.fast_normalization)
                self.assertTrue(arguments.skip_failed_frames)
                self.assertTrue(arguments.debug)
                self.assertFalse(arguments.drizzle)
                self.assertTrue(arguments.export_per_cohort)
                self.assertTrue(arguments.coverage_map)
                self.assertTrue(arguments.auto_crop_master)
                self.assertEqual(arguments.rejection_method, "winsorized")
                self.assertTrue(arguments.low_rejection_map)
                self.assertFalse(arguments.high_rejection_map)
            finally:
                root.destroy()

    def test_gui_uses_hot_only_cold_sigma_default(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            self.assertEqual(app.cosmetic_cold_sigma.get(), 50.0)
            self.assertEqual(app.cosmetic_hot_sigma.get(), 3.0)
        finally:
            root.destroy()

    def test_gui_ignores_disabled_filters_and_restores_profile_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.gui_root()
            root.withdraw()
            try:
                workdir, executable = self.make_project(Path(directory))
                app = SirilMosaicApp(root)
                app.workdir.set(str(workdir))
                app.output_dir.set(str(Path(directory) / 'output'))
                app.siril_exe.set(str(executable))
                app.adaptive_quality_filtering.set(True)
                app.filter_background.set('invalid but disabled')
                command = app.command()
                self.assertIn('--quality-filter-sigma', command)
                self.assertNotIn('--filter-background', command)
                app.filter_background.set(90)
                app.adaptive_quality_filtering.set(False)
                app.quality_filter_sigma.set('invalid but disabled')
                app.mosaic_aware_star_count.set(True)
                app.filter_stars.set('invalid but disabled')
                command = app.command()
                self.assertNotIn('--quality-filter-sigma', command)
                self.assertNotIn('--filter-stars', command)
                app.quality_filter_sigma.set(3)
                app.filter_stars.set(97)
                app.coverage_map.set(True)
                app.auto_crop_master.set(True)
                app.auto_crop_coverage_percent.set(80)
                app.profile_name.set('used')
                app.save_profile(notify=False)
                app.profile_name.set('not loaded')
                app._write_last_paths()
                self.assertEqual(app._read_last_paths()['profile_name'], 'used')
                profile_path = app.profile_path
            finally:
                root.destroy()
            reopened = self.gui_root()
            reopened.withdraw()
            try:
                app = SirilMosaicApp(reopened, profile_path=profile_path)
                self.assertEqual(app.profile_name.get(), 'used')
                self.assertTrue(app.auto_crop_master.get())
                self.assertEqual(app.auto_crop_coverage_percent.get(), 80)
                self.assertTrue(all(str(widget.cget('state')) == 'normal' for widget in app.coverage_widgets))
                app.delete_profile(notify=False)
                self.assertEqual(app.last_used_profile, '')
            finally:
                reopened.destroy()

    def test_gui_groups_mosaic_settings_by_processing_stage(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            groups = {
                section.cget("text")
                for column in app.settings_content.winfo_children()
                for section in column.winfo_children()
                if isinstance(section, ttk.LabelFrame)
            }

            self.assertTrue({
                "Capture & CFA",
                "Cosmetic correction",
                "Drizzle Settings",
                "Frame selection",
                "Integration",
                "Background & plate solving",
                "Resources",
            }.issubset(groups))
            section_rows = {
                section.cget("text"): int(section.grid_info()["row"])
                for column in app.settings_content.winfo_children()
                for section in column.winfo_children()
                if isinstance(section, ttk.LabelFrame)
            }
            self.assertEqual(section_rows["Drizzle Settings"], 1)
            self.assertEqual(section_rows["Integration"], 2)
            integration = next(
                section
                for column in app.settings_content.winfo_children()
                for section in column.winfo_children()
                if isinstance(section, ttk.LabelFrame) and section.cget("text") == "Integration"
            )
            high_map_label = next(
                widget for widget in integration.winfo_children()
                if isinstance(widget, ttk.Label) and widget.cget("text") == "High map"
            )
            high_rejection = app.rejection_widgets[1]
            self.assertEqual(
                (int(high_rejection.grid_info()["row"]), int(high_rejection.grid_info()["column"])),
                (3, 1),
            )
            self.assertEqual(
                (int(high_map_label.grid_info()["row"]), int(high_map_label.grid_info()["column"])),
                (3, 2),
            )
        finally:
            root.destroy()

    def test_gui_run_review_shows_discard_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "quality_report_test.json"
            report_path.write_text(json.dumps({
                "status": "complete",
                "run_id": "review_test",
                "input_frames": 10,
                "seed": 123,
                "configuration_hash": "abc123",
                "journal_path": "events.jsonl",
                "input_manifest": "manifest.json",
                "settings": {},
                "cohorts": [],
                "substacks": [{
                    "number": 1,
                    "stack": {
                        "stacked_frames": 9,
                        "rejection_map_diagnostics": {
                            "status": "partial",
                            "maps": {
                                "low": {
                                    "status": "available",
                                    "channels": {
                                        "0": {
                                            "affected_pixel_locations": 12,
                                            "affected_pixel_percent": 0.12,
                                        }
                                    },
                                },
                                "high": {"status": "unavailable", "reason": "map_missing"},
                            },
                        },
                    },
                    "discarded_frames": [{
                        "file": "panel/light_01.fit",
                        "status": "rejected",
                        "reason_code": "registration_excluded",
                        "filter_metrics": {
                            "fwhm": {
                                "value": 8.2,
                                "threshold": 6.0,
                                "comparison": "<=",
                                "status": "failed",
                            }
                        },
                    }],
                }],
            }), encoding="utf-8")
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.refresh_run_review(report_path)
                values = app.review_tree.item(app.review_tree.get_children()[0], "values")
                self.assertEqual(values[1], "panel/light_01.fit")
                self.assertEqual(
                    tuple(app.review_tree["columns"]),
                    ("substack", "file", "status", "reason", "fwhm", "roundness", "background", "stars"),
                )
                self.assertEqual(values[4], "8.200 <= 6.000 [failed]")
                self.assertEqual(values[5], "-")
                summary = app.review_summary.get("1.0", "end")
                self.assertIn("Stacked frames: 9", summary)
                self.assertIn("Rejected: 1", summary)
                self.assertIn("Rejection-map diagnostics:", summary)
                self.assertIn("Substack 1: partial", summary)
                self.assertIn("ch 0: 12 locations (0.1200%)", summary)
                self.assertIn("high: map_missing", summary)
            finally:
                root.destroy()

    def test_gui_run_review_verifies_loaded_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = self._write_verifiable_run(Path(directory))
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.refresh_run_review(report_path)
                app.verify_loaded_run()
                self.assertTrue(app.verification_status.get().startswith("PASS"))
                dialogs = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
                for dialog in dialogs:
                    dialog.destroy()
            finally:
                root.destroy()

    def test_gui_preview_counts_pending_rejects_in_total(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=4)
            rejected_source = workdir / "panel_1" / "lights" / "light.fit"
            rejected_path = workdir / "rejects" / "panel_1" / "lights" / "light.fit"
            rejected_path.parent.mkdir(parents=True)
            shutil.move(str(rejected_source), str(rejected_path))
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.workdir.set(str(workdir))
                app.output_dir.set(str(Path(directory) / "output"))
                app.siril_exe.set(str(executable))
                summary = app._preflight_summary(workdir, Path(directory) / "output")
                self.assertIn("Frames: 4 (3 current + 1 prior rejects to restore)", summary)
                self.assertIn("Frames this run: 4", summary)
                self.assertIsInstance(app.command(), list)
            finally:
                root.destroy()

    def test_gui_preview_run_includes_integrity_scan_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=2)
            output_dir = Path(directory) / "output"
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.workdir.set(str(workdir))
                app.output_dir.set(str(output_dir))
                app.siril_exe.set(str(executable))
                callbacks = []

                def capture_scan(callback):
                    callbacks.append(callback)

                with patch.object(app, "_start_integrity_scan", side_effect=capture_scan):
                    app.preview_run()

                self.assertEqual(len(callbacks), 1)
                callbacks[0]({
                    "status": "WARN",
                    "counts": {"PASS": 6, "WARN": 1, "FAIL": 0},
                    "checks": [{"status": "WARN", "name": "duplicates", "detail": "one group"}],
                })
                dialogs = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
                self.assertTrue(dialogs)
                preview_text = dialogs[-1].winfo_children()[0]
                self.assertIn("Preflight integrity scan: WARN", preview_text.get("1.0", "end"))
                for dialog in dialogs:
                    dialog.destroy()
            finally:
                root.destroy()

    def test_gui_resume_run_dispatches_resume_mode_when_checkpoint_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.output_dir.set(directory)
                (Path(directory) / 'run_checkpoint.json').write_text('{}', encoding='utf-8')
                with patch.object(app, 'start') as start:
                    app.resume_run()
                    start.assert_called_once_with(resume=True)
            finally:
                root.destroy()

    def test_gui_cropping_workbench_loads_report_and_previews_crop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            master = root_path / "master_stack_run.fit"
            integration = root_path / "integration_time_map_run.fit"
            report_path = root_path / "quality_report_run.json"
            sirilmosaic._write_coverage_fits(
                master,
                np.linspace(1, 100, 100, dtype=np.float32).reshape(10, 10),
                unit="adu",
            )
            coverage = np.zeros((10, 10), dtype=np.float32)
            coverage[2:8, 1:9] = 10
            sirilmosaic._write_coverage_fits(integration, coverage, unit="s")
            report_path.write_text(json.dumps({
                "status": "complete",
                "master": {"path": str(master)},
                "coverage": {"integration_time_path": str(integration)},
            }), encoding="utf-8")
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.load_crop_report(report_path, notify=False)
                self.assertEqual(len(app.crop_artifacts), 1)
                deadline = time.time() + 5
                while (
                    (app.crop_plan is None or app.crop_preview_in_progress or app.crop_full_rgb is None)
                    and time.time() < deadline
                ):
                    root.update()
                    time.sleep(0.01)
                self.assertEqual(app.crop_plan["crop_bounds"]["width"], 8)
                self.assertIsNotNone(app.crop_region_rgb)
                self.assertIn("Pixels retained: 48.000%", app.crop_summary.get("1.0", "end"))
                app.set_crop_percent(75)
                deadline = time.time() + 5
                while (
                    app.crop_plan is None
                    or app.crop_plan["crop_coverage_percent"] != 75.0
                    or app.crop_preview_in_progress
                    or app.crop_full_rgb is None
                ):
                    if time.time() >= deadline:
                        break
                    root.update()
                    time.sleep(0.01)
                self.assertEqual(app.crop_plan["crop_coverage_percent"], 75.0)
                self.assertIn("_crop_075pct.fit", app.crop_plan["output_path"])
            finally:
                root.destroy()

    def test_gui_cropping_workbench_starts_create_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            master = root_path / "master_stack_run.fit"
            integration = root_path / "integration_time_map_run.fit"
            report_path = root_path / "quality_report_run.json"
            executable = root_path / "siril.exe"
            executable.touch()
            sirilmosaic._write_coverage_fits(
                master,
                np.linspace(1, 100, 100, dtype=np.float32).reshape(10, 10),
                unit="adu",
            )
            coverage = np.zeros((10, 10), dtype=np.float32)
            coverage[2:8, 1:9] = 10
            sirilmosaic._write_coverage_fits(integration, coverage, unit="s")
            report_path.write_text(json.dumps({
                "status": "complete",
                "master": {"path": str(master)},
                "coverage": {"integration_time_path": str(integration)},
            }), encoding="utf-8")
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.siril_exe.set(str(executable))
                app.load_crop_report(report_path, notify=False)
                deadline = time.time() + 5
                while app.crop_plan is None and time.time() < deadline:
                    root.update()
                    time.sleep(0.01)
                with patch.object(app, "_start_crop_operation") as start_crop:
                    app.create_crop()
                    start_crop.assert_called_once()
                    command, output_path = start_crop.call_args.args
                    self.assertIn("--crop-workbench", command)
                    self.assertTrue(str(output_path).endswith("_crop_050pct.fit"))
            finally:
                root.destroy()

    def test_gui_cropping_workbench_starts_background_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                output_path = Path(directory) / "cropped.fit"
                with patch("sirilmosaic_gui.Thread") as thread_class:
                    app._start_crop_operation(["crop-command"], output_path)
                    thread_class.assert_called_once()
                    thread_class.return_value.start.assert_called_once_with()
            finally:
                root.destroy()

    def test_gui_small_crop_requires_confirmation_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "siril.exe"
            executable.touch()
            master = Path(directory) / "master.fit"
            stale_output = Path(directory) / "master_crop_100pct.fit"
            stale_output.touch()
            app = SirilMosaicApp.__new__(SirilMosaicApp)
            app.siril_exe = Mock()
            app.siril_exe.get.return_value = str(executable)
            app.crop_percent = Mock()
            app.crop_percent.get.return_value = 100
            app.status = Mock()
            app._start_crop_operation = Mock()
            plan = {
                "area_percent": 0.5,
                "output_path": str(stale_output),
                "crop_coverage_percent": 100,
                "master_path": str(master),
                "integration_time_path": str(Path(directory) / "coverage.fit"),
            }

            with patch("sirilmosaic_gui.messagebox.askyesno", return_value=False):
                app._start_crop_from_plan(plan)
            app._start_crop_operation.assert_not_called()
            app.status.set.assert_called_with("Crop creation cancelled; preview retained")

            with patch("sirilmosaic_gui.messagebox.askyesno", return_value=True):
                app._start_crop_from_plan(plan)
            app._start_crop_operation.assert_called_once()
            self.assertEqual(
                app._start_crop_operation.call_args.args[1],
                Path(directory) / "master_crop_100pct_2.fit",
            )
            self.assertEqual(plan["output_path"], str(Path(directory) / "master_crop_100pct_2.fit"))

    def test_gui_run_profiles_persist_processing_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profiles.json"
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root, profile_path=profile_path)
                app.profile_name.set("OSC drizzle")
                app.drizzle_scale.set(1.7)
                app.drizzle_kernel.set("gaussian")
                app.rejection_method.set("winsorized")
                app.low_rejection_map.set("Always")
                app.high_rejection_map.set("Never")
                app.bayer_pattern.set("RGGB")
                app.cosmetic_correction.set(False)
                app.cosmetic_cold_sigma.set(4.5)
                app.cosmetic_hot_sigma.set(4.0)
                app.overlap_normalization.set(False)
                app.stack_normalization.set("mul")
                app.plate_solve_order.set(4)
                app.plate_solve_downscale.set(True)
                app.plate_solve_radius.set("3.5")
                app.plate_solve_limit_mag.set("-1")
                app.rbf_smoothing.set(0.8)
                app.background_dither.set(False)
                app.registration_transform.set("affine")
                app.registration_minpairs.set(6)
                app.registration_maxstars.set(800)
                app.registration_interpolation.set("linear")
                app.random_seed.set("77")
                app.filter_background.set(93)
                app.adaptive_quality_filtering.set(True)
                app.quality_filter_sigma.set(2.6)
                app.background_method.set("Linear")
                app.fast_normalization.set(True)
                app.debug.set(True)
                app.save_profile(notify=False)

                app.drizzle_scale.set(2.5)
                app.drizzle_kernel.set("lanczos2")
                app.rejection_method.set("linear")
                app.bayer_pattern.set("Auto (header)")
                app.cosmetic_correction.set(True)
                app.cosmetic_cold_sigma.set(2.5)
                app.cosmetic_hot_sigma.set(2.0)
                app.overlap_normalization.set(True)
                app.plate_solve_order.set(3)
                app.plate_solve_downscale.set(False)
                app.plate_solve_radius.set("")
                app.plate_solve_limit_mag.set("")
                app.rbf_smoothing.set(0.5)
                app.background_dither.set(True)
                app.registration_transform.set("homography")
                app.registration_minpairs.set(0)
                app.registration_maxstars.set(0)
                app.registration_interpolation.set("lanczos4")
                app.random_seed.set("")
                app.filter_background.set(99)
                app.adaptive_quality_filtering.set(False)
                app.quality_filter_sigma.set(4.0)
                app.background_method.set("Off")
                app.fast_normalization.set(False)
                app.debug.set(False)
                app.load_profile(notify=False)

                self.assertEqual(app.drizzle_scale.get(), 1.7)
                self.assertEqual(app.drizzle_kernel.get(), "gaussian")
                self.assertEqual(app.rejection_method.get(), "winsorized")
                self.assertEqual(app.low_rejection_map.get(), "Always")
                self.assertEqual(app.high_rejection_map.get(), "Never")
                self.assertEqual(app.bayer_pattern.get(), "RGGB")
                self.assertFalse(app.cosmetic_correction.get())
                self.assertEqual(app.cosmetic_cold_sigma.get(), 4.5)
                self.assertEqual(app.cosmetic_hot_sigma.get(), 4.0)
                self.assertFalse(app.overlap_normalization.get())
                self.assertEqual(app.stack_normalization.get(), "mul")
                self.assertEqual(app.plate_solve_order.get(), 4)
                self.assertTrue(app.plate_solve_downscale.get())
                self.assertEqual(app.plate_solve_radius.get(), "3.5")
                self.assertEqual(app.plate_solve_limit_mag.get(), "-1")
                self.assertEqual(app.rbf_smoothing.get(), 0.8)
                self.assertFalse(app.background_dither.get())
                self.assertEqual(app.registration_transform.get(), "affine")
                self.assertEqual(app.registration_minpairs.get(), 6)
                self.assertEqual(app.registration_maxstars.get(), 800)
                self.assertEqual(app.registration_interpolation.get(), "linear")
                self.assertEqual(app.random_seed.get(), "77")
                self.assertEqual(app.filter_background.get(), 93)
                self.assertTrue(app.adaptive_quality_filtering.get())
                self.assertEqual(app.quality_filter_sigma.get(), 2.6)
                self.assertEqual(app.background_method.get(), "Linear")
                self.assertFalse(app.fast_normalization.get())
                self.assertTrue(app.debug.get())
                stored = json.loads(profile_path.read_text(encoding="utf-8"))
                self.assertIn("OSC drizzle", stored["profiles"])
                self.assertNotIn("workdir", stored["profiles"]["OSC drizzle"])
                self.assertNotIn("output_dir", stored["profiles"]["OSC drizzle"])
                app.delete_profile(notify=False)
                stored = json.loads(profile_path.read_text(encoding="utf-8"))
                self.assertNotIn("OSC drizzle", stored["profiles"])
            finally:
                root.destroy()

    def test_gui_remembers_last_input_and_output_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profiles.json"
            selected_input = Path(directory) / "selected input"
            selected_output = Path(directory) / "selected output"
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root, profile_path=profile_path)
                app.workdir.set(str(selected_input))
                app.output_dir.set(str(selected_output))
                app.bayer_pattern.set("RGGB")
                app.bayer_orientation.set("Bottom-up")
                app._write_last_paths()
            finally:
                root.destroy()

            reopened_root = self.gui_root()
            reopened_root.withdraw()
            try:
                reopened_app = SirilMosaicApp(reopened_root, profile_path=profile_path)
                self.assertEqual(reopened_app.workdir.get(), str(selected_input))
                self.assertEqual(reopened_app.output_dir.get(), str(selected_output))
                self.assertEqual(reopened_app.bayer_pattern.get(), "RGGB")
                self.assertEqual(reopened_app.bayer_orientation.get(), "Bottom-up")
            finally:
                reopened_root.destroy()

    def test_gui_warns_before_starting_with_ambiguous_auto_debayer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=1)
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root, profile_path=Path(directory) / "profiles.json")
                app.workdir.set(str(workdir))
                app.output_dir.set(str(Path(directory) / "output"))
                app.siril_exe.set(str(executable))
                app.substacks.set(1)
                app.bayer_pattern.set("Auto (header)")
                app.bayer_orientation.set("Auto")
                with patch(
                    "sirilmosaic_gui.messagebox.askyesno", return_value=False
                ) as askyesno:
                    app.start()

                confirmation = askyesno.call_args.args[1]
                self.assertIn("Debayer preflight warning", confirmation)
                self.assertIn("ROWORDER metadata is missing", confirmation)
                self.assertFalse(app.running)
            finally:
                root.destroy()

    def test_gui_warns_about_overlap_normalization_for_large_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=201)
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root, profile_path=Path(directory) / "profiles.json")
                app.workdir.set(str(workdir))
                app.output_dir.set(str(Path(directory) / "output"))
                app.siril_exe.set(str(executable))
                app.substacks.set(1)
                app.overlap_normalization.set(True)
                with patch(
                    "sirilmosaic_gui.messagebox.askyesno", return_value=False
                ) as askyesno:
                    app.start()

                confirmation = askyesno.call_args.args[1]
                self.assertIn("Performance warning", confirmation)
                self.assertIn("201 light files were detected", confirmation)
                self.assertIn("overlap normalization is enabled", confirmation)
                self.assertFalse(app.running)
            finally:
                root.destroy()

    def test_fast_normalization_requires_overlap_normalization(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            assert app.fast_normalization_widget is not None
            app.fast_normalization.set(True)
            app.overlap_normalization.set(False)
            app.update_normalization_controls()
            self.assertTrue(app.fast_normalization_widget.instate(("disabled",)))
            self.assertFalse(app.fast_normalization.get())
            app.overlap_normalization.set(True)
            app.update_normalization_controls()
            self.assertFalse(app.fast_normalization_widget.instate(("disabled",)))
        finally:
            root.destroy()

    def test_rejection_method_changes_apply_base_thresholds(self) -> None:
        root = self.gui_root()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            expected = {
                "none": (3.0, 3.0),
                "percentile": (0.2, 0.1),
                "sigma": (3.0, 3.0),
                "mad": (3.0, 3.0),
                "median": (3.0, 3.0),
                "linear": (3.0, 3.0),
                "winsorized": (3.0, 3.0),
                "generalized": (0.3, 0.05),
            }
            for method, (low, high) in expected.items():
                app.rejection_method.set(method)
                app.rejection_low.set(99)
                app.rejection_high.set(99)
                app.rejection_method_changed()
                self.assertEqual(app.rejection_low.get(), low)
                self.assertEqual(app.rejection_high.get(), high)
        finally:
            root.destroy()

    @patch("sirilmosaic_gui.psutil.wait_procs", return_value=([], []))
    @patch("sirilmosaic_gui.psutil.Process")
    def test_cancel_targets_only_siril_descendants(self, process_class, wait_procs) -> None:
        siril = Mock()
        siril.name.return_value = "siril.exe"
        updater = Mock()
        updater.name.return_value = "python.exe"
        process_class.return_value.children.return_value = [siril, updater]

        self.assertEqual(terminate_siril_descendants(1234), 1)

        siril.terminate.assert_called_once_with()
        updater.terminate.assert_not_called()
        wait_procs.assert_called_once_with([siril], timeout=5)

    def test_cancel_button_writes_marker_without_terminating_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = SirilMosaicApp.__new__(SirilMosaicApp)
            app.running = True
            app.cancelling = False
            app.process = Mock(pid=1234)
            app.cancel_path = Path(directory) / "run.cancel"
            app.cancel_button = Mock()
            app.status = Mock()
            app.append_log = Mock()
            app.progress_estimator = RunProgressEstimator()
            app.update_progress_display = Mock()
            app.events = Mock()
            with patch("sirilmosaic_gui.messagebox.askyesno", return_value=True), patch(
                "sirilmosaic_gui.Thread"
            ) as thread_class:
                app.cancel()

            self.assertTrue(app.cancel_path.is_file())
            self.assertTrue(app.cancelling)
            self.assertIsNotNone(app.process)
            thread_class.assert_called_once()

    def test_gui_event_queue_sheds_logs_but_preserves_control_event(self) -> None:
        app = SirilMosaicApp.__new__(SirilMosaicApp)
        app.events = Queue(maxsize=2)
        app.dropped_log_events = 0
        app._queue_event('log', 'one')
        app._queue_event('log', 'two')
        app._queue_event('log', 'three')
        app._queue_event('done', 0)

        queued = []
        while not app.events.empty():
            queued.append(app.events.get_nowait())
        self.assertIn(('done', 0), queued)
        self.assertGreater(app.dropped_log_events, 0)

    def test_completed_verification_failure_requires_completed_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / 'quality_report.json'
            report_path.write_text(json.dumps({
                'status': 'complete',
                'verification_status': 'FAIL',
            }), encoding='utf-8')

            self.assertEqual(completed_verification_failure(report_path), 'FAIL')
            report_path.write_text(json.dumps({
                'status': 'failed',
                'verification_status': 'FAIL',
            }), encoding='utf-8')
            self.assertIsNone(completed_verification_failure(report_path))

    def test_partial_staging_can_be_fully_restored_after_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, _ = self.make_project(Path(directory))
            original_paths = set(sirilmosaic.discover_light_files(workdir))
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = Path(directory) / "output"
            sirilmosaic.SubStack_nb = 1
            sirilmosaic.debug = True
            checks = 0

            def cancel_during_staging() -> None:
                nonlocal checks
                checks += 1
                if checks == 4:
                    raise sirilmosaic.CancellationRequested("test cancellation")

            with patch("sirilmosaic.check_cancellation", side_effect=cancel_during_staging):
                with self.assertRaises(sirilmosaic.CancellationRequested):
                    sirilmosaic.group_files(workdir)

            sirilmosaic.cleanup(1, restore_sources=True)

            self.assertEqual(set(sirilmosaic.discover_light_files(workdir)), original_paths)

    def test_frame_selection_rejects_move_to_mirrored_rejects_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, _ = self.make_project(Path(directory))
            sirilmosaic.workdir = workdir
            sirilmosaic.journal_path = None
            relative_path = Path('panel_1') / 'lights' / 'light.fit'
            moved = sirilmosaic.move_frame_selection_rejects([
                {
                    'file': relative_path.as_posix(),
                    'status': 'rejected',
                    'reason_code': 'registration_excluded',
                },
                {
                    'file': 'panel_2/lights/light.xisf',
                    'status': 'failed',
                    'reason_code': 'plate_solving_failed',
                },
            ])

            self.assertEqual(len(moved), 1)
            self.assertFalse((workdir / relative_path).exists())
            self.assertTrue((workdir / 'rejects' / relative_path).is_file())
            self.assertEqual(len(sirilmosaic.discover_light_files(workdir)), 3)

    def test_rejected_frames_restore_before_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, _ = self.make_project(Path(directory))
            sirilmosaic.workdir = workdir
            sirilmosaic.journal_path = None
            relative_path = Path('panel_1') / 'lights' / 'light.fit'
            source = workdir / relative_path
            rejects_path = workdir / 'rejects' / relative_path
            rejects_path.parent.mkdir(parents=True)
            shutil.move(str(source), str(rejects_path))

            restored = sirilmosaic.restore_rejected_frames()

            self.assertEqual(restored, [relative_path.as_posix()])
            self.assertTrue(source.is_file())
            self.assertFalse((workdir / 'rejects').exists())
            self.assertEqual(len(sirilmosaic.discover_light_files(workdir)), 4)

    def test_rejected_frame_restore_refuses_destination_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, _ = self.make_project(Path(directory))
            sirilmosaic.workdir = workdir
            relative_path = Path('panel_1') / 'lights' / 'light.fit'
            rejects_path = workdir / 'rejects' / relative_path
            rejects_path.parent.mkdir(parents=True)
            shutil.copy2(workdir / relative_path, rejects_path)

            with self.assertRaisesRegex(RuntimeError, 'destination files already exist'):
                sirilmosaic.restore_rejected_frames()

    def test_pipeline_reports_successful_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            output_dir = Path(directory) / "output"
            cancel_file = Path(directory) / "run.cancel"
            cancel_file.write_text("cancel\n", encoding="utf-8")
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--output-dir", str(output_dir),
                "--siril-exe", str(executable),
                "--cancel-file", str(cancel_file),
            ])
            fake = Mock()

            with patch("pysiril.siril.Siril", return_value=fake):
                result = sirilmosaic.run_pipeline(arguments)

            self.assertEqual(result, 2)
            self.assertEqual(sirilmosaic.quality_report["status"], "cancelled")
            self.assertFalse(cancel_file.exists())
            self.assertEqual(len(sirilmosaic.discover_light_files(workdir, (output_dir,))), 4)
            fake.Close.assert_called_once_with()

    def test_pipeline_never_cleans_preexisting_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            staging = workdir / 'Lights_sorted' / 'group_1'
            (staging / 'lights').mkdir(parents=True)
            (staging / 'process').mkdir()
            (staging / 'lights' / 'frame_00001.fit').write_bytes(b'existing source')
            (staging / 'process' / 'light_00001.fit').write_bytes(b'existing intermediate')
            (staging / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({'frame_00001.fit': 'prior_source.fit'})
            )
            before = {path: path.read_bytes() for path in staging.rglob('*') if path.is_file()}
            arguments = sirilmosaic.build_parser().parse_args([
                '--workdir', str(workdir), '--siril-exe', str(executable),
            ])
            with patch('pysiril.siril.Siril'), self.assertRaisesRegex(ValueError, 'Resume Run'):
                sirilmosaic.run_pipeline(arguments)
            self.assertEqual({path: path.read_bytes() for path in staging.rglob('*') if path.is_file()}, before)
            self.assertFalse((workdir / 'prior_source.fit').exists())

    def test_abandon_interrupted_run_restores_sources_and_clears_recovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "workdir"
            output = root / "output"
            staged = workdir / "Lights_sorted" / "group_1" / "lights"
            staged.mkdir(parents=True)
            output.mkdir()
            (staged / "frame_00001.fit").write_bytes(b"source")
            (workdir / "Lights_sorted" / "group_1" / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({"frame_00001.fit": "lights/source.fit"}), encoding="utf-8"
            )
            checkpoint = output / "run_checkpoint.json"
            checkpoint.write_text("{}", encoding="utf-8")
            result = sirilmosaic.abandon_interrupted_run(workdir, output)

            self.assertEqual(result["restored"], 1)
            self.assertEqual((workdir / "lights" / "source.fit").read_bytes(), b"source")
            self.assertFalse((workdir / "Lights_sorted").exists())
            self.assertFalse(checkpoint.exists())

    def test_abandon_interrupted_run_can_clear_manifest_only_orphan_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "workdir"
            output = root / "output"
            group = workdir / "Lights_sorted" / "group_1"
            (group / "lights").mkdir(parents=True)
            (group / "process").mkdir()
            output.mkdir()
            missing_path = "dark/missing.fit"
            (group / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({"frame_00001.fit": missing_path}), encoding="utf-8"
            )
            report = output / "quality_report.json"
            report.write_text("{}", encoding="utf-8")

            with self.assertRaises(sirilmosaic.InterruptedRunRecoveryError) as context:
                sirilmosaic.abandon_interrupted_run(workdir, output)

            error = context.exception
            self.assertEqual(error.missing_sources, (str(Path(missing_path)),))
            self.assertTrue(error.orphan_cleanup_allowed)
            result = sirilmosaic.abandon_interrupted_run(workdir, output, allow_missing=True)

            self.assertEqual(result["restored"], 0)
            self.assertEqual(result["missing"], [str(Path(missing_path))])
            self.assertFalse((workdir / "Lights_sorted").exists())
            self.assertTrue(report.exists())

    def test_abandon_interrupted_run_refuses_missing_sources_with_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "workdir"
            output = root / "output"
            group = workdir / "Lights_sorted" / "group_1"
            lights = group / "lights"
            lights.mkdir(parents=True)
            (group / "process").mkdir()
            output.mkdir()
            (group / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({"frame_00001.fit": "dark/missing.fit"}), encoding="utf-8"
            )
            (lights / "unexpected.fit").write_bytes(b"payload")

            with self.assertRaises(sirilmosaic.InterruptedRunRecoveryError) as context:
                sirilmosaic.abandon_interrupted_run(workdir, output)

            self.assertFalse(context.exception.orphan_cleanup_allowed)
            with self.assertRaisesRegex(RuntimeError, "staged payload files"):
                sirilmosaic.abandon_interrupted_run(workdir, output, allow_missing=True)
            self.assertTrue((lights / "unexpected.fit").exists())

    def test_gui_blocks_fresh_start_when_recovery_state_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=1)
            output = Path(directory) / "output"
            output.mkdir()
            (output / "run_checkpoint.json").write_text("{}", encoding="utf-8")
            (workdir / "Lights_sorted").mkdir()
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.workdir.set(str(workdir))
                app.output_dir.set(str(output))
                app.siril_exe.set(str(executable))
                app.substacks.set(1)
                with patch("sirilmosaic_gui.messagebox.showerror") as showerror:
                    app.start()
                showerror.assert_called_once()
                self.assertIn("Use Resume Run", showerror.call_args.args[1])
                self.assertFalse(app.running)
            finally:
                root.destroy()

    def test_gui_confirms_manifest_only_orphan_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            workdir = root_path / "workdir"
            output = root_path / "output"
            group = workdir / "Lights_sorted" / "group_1"
            (group / "lights").mkdir(parents=True)
            (group / "process").mkdir()
            output.mkdir()
            (group / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({"frame_00001.fit": "dark/missing.fit"}), encoding="utf-8"
            )
            root = self.gui_root()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.workdir.set(str(workdir))
                app.output_dir.set(str(output))
                with patch(
                    "sirilmosaic_gui.messagebox.askyesno",
                    side_effect=[True, True],
                ) as askyesno:
                    app.abandon_run()

                self.assertEqual(askyesno.call_count, 2)
                self.assertFalse((workdir / "Lights_sorted").exists())
                self.assertIn("already missing", app.status.get())
            finally:
                root.destroy()

    def test_pipeline_retries_command_failures_with_clean_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            arguments = sirilmosaic.build_parser().parse_args([
                '--workdir', str(workdir), '--siril-exe', str(executable),
                '--substacks', '1', '--retries', '1',
            ])
            fake = Mock()
            fake.GetData.return_value = []
            attempts = []

            def attempt(group_number):
                process = workdir / 'Lights_sorted' / f'group_{group_number}' / 'process'
                stale = process / 'r_light_00001.fit'
                self.assertFalse(stale.exists())
                attempts.append(group_number)
                if len(attempts) == 1:
                    stale.write_bytes(b'incomplete attempt')
                    sirilmosaic.quality_report['substacks'].append({'incomplete': True})
                    raise sirilmosaic.SirilCommandError('synthetic command failure')
                self.assertEqual(sirilmosaic.quality_report['substacks'], [])
                return True

            with patch('pysiril.siril.Siril', return_value=fake), patch(
                'sirilmosaic.substack', side_effect=attempt
            ), patch('sirilmosaic.masterstack'), patch(
                'sirilmosaic.finalize_integration_report'
            ), patch('sirilmosaic.write_verification_artifact', return_value=(
                workdir / 'verification.json',
                {'status': 'PASS', 'counts': {'PASS': 1, 'WARN': 0, 'FAIL': 0}},
            )):
                self.assertEqual(sirilmosaic.run_pipeline(arguments), 0)
            self.assertEqual(attempts, [1, 1])
            self.assertEqual(fake.Open.call_count, 2)
            self.assertEqual(len(sirilmosaic.quality_report['substack_attempt_failures']), 1)
            self.assertEqual(len(sirilmosaic.discover_light_files(workdir)), 4)

    def test_resume_checkpoint_round_trip_recovers_and_restages_incomplete_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / 'frames'
            output = root / 'output'
            workdir.mkdir()
            output.mkdir()
            (workdir / 'light_01.fit').write_bytes(b'complete')
            lights_sorted = workdir / 'Lights_sorted'
            completed = lights_sorted / 'group_1' / 'lights'
            incomplete = lights_sorted / 'group_2' / 'lights'
            completed.mkdir(parents=True)
            incomplete.mkdir(parents=True)
            (lights_sorted / 'group_1' / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({'frame_00001.fit': 'light_01.fit'}), encoding='utf-8'
            )
            (lights_sorted / 'group_1' / 'substack_1.fit').write_bytes(b'substack')
            (lights_sorted / 'group_2' / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({'frame_00001.fit': 'light_02.fit'}), encoding='utf-8'
            )
            (incomplete / 'frame_00001.fit').write_bytes(b'incomplete')
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = output
            checkpoint = {
                'schema_version': 1,
                'run_id': 'resume_test',
                'workdir': str(workdir),
                'output_dir': str(output),
                'substack_count': 2,
                'completed_substacks': [1],
                'groups': {'1': ['light_01.fit'], '2': ['light_02.fit']},
            }
            sirilmosaic.write_run_checkpoint(checkpoint)

            loaded = sirilmosaic.load_run_checkpoint()
            completed_groups = sirilmosaic.prepare_resume_staging(loaded)

            self.assertEqual(completed_groups, {1})
            self.assertTrue((lights_sorted / 'group_1' / 'substack_1.fit').is_file())
            self.assertTrue((lights_sorted / 'group_2' / 'lights' / 'frame_00001.fit').is_file())
            self.assertFalse((workdir / 'light_02.fit').exists())

    def test_checkpoint_normalization_adds_phases_without_mutating_legacy_state(self) -> None:
        checkpoint = {
            'schema_version': 2,
            'state': 'interrupted',
            'cohorts': [
                {'id': 'complete', 'state': 'complete'},
                {'id': 'active', 'state': 'running'},
                {'id': 'waiting', 'state': 'pending', 'phase': 'pending'},
            ],
        }

        normalized = sirilmosaic.normalize_run_checkpoint(checkpoint)

        self.assertEqual(normalized['schema_version'], 2)
        self.assertEqual(normalized['global_phase'], 'interrupted')
        self.assertEqual(
            [cohort['phase'] for cohort in normalized['cohorts']],
            ['complete', 'substacks_running', 'pending'],
        )
        self.assertNotIn('global_phase', checkpoint)
        self.assertNotIn('phase', checkpoint['cohorts'][0])

    def test_checkpoint_normalization_rejects_unknown_schema_and_phase(self) -> None:
        with self.assertRaisesRegex(RuntimeError, 'Unsupported resume checkpoint schema'):
            sirilmosaic.normalize_run_checkpoint({'schema_version': 99})
        with self.assertRaisesRegex(RuntimeError, 'Unsupported cohort checkpoint phase'):
            sirilmosaic.normalize_run_checkpoint({
                'schema_version': 2,
                'state': 'running',
                'cohorts': [{'state': 'running', 'phase': 'mystery'}],
            })

    def test_checkpoint_normalization_accepts_finalization_phases(self) -> None:
        for phase in ('master_written', 'maps_written', 'crop_pending'):
            normalized = sirilmosaic.normalize_run_checkpoint({
                'schema_version': 2,
                'state': 'interrupted',
                'cohorts': [{'state': 'running', 'phase': phase}],
            })
            self.assertEqual(normalized['cohorts'][0]['phase'], phase)

    def test_resume_restores_completed_substack_moved_for_master_stack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / 'frames'
            output = root / 'output'
            workdir.mkdir()
            output.mkdir()
            moved_lights = workdir / 'substacks' / 'lights'
            moved_maps = workdir / 'substacks' / 'rejection_maps'
            moved_lights.mkdir(parents=True)
            moved_maps.mkdir(parents=True)
            (moved_lights / 'substack_1.fit').write_bytes(b'substack')
            (moved_maps / 'substack_1_low_rejmap.fit').write_bytes(b'map')
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = output

            completed = sirilmosaic.prepare_resume_staging({
                'substack_count': 1,
                'completed_substacks': [1],
                'groups': {'1': []},
            })

            group = workdir / 'Lights_sorted' / 'group_1'
            self.assertEqual(completed, {1})
            self.assertTrue((group / 'substack_1.fit').is_file())
            self.assertTrue((group / 'substack_1_low_rejmap.fit').is_file())
            self.assertFalse((moved_lights / 'substack_1.fit').exists())

    def test_divide_group_assignments_matches_staging_group_sizes(self) -> None:
        files = [f'light_{number}.fit' for number in range(1, 6)]

        groups = sirilmosaic.divide_group_assignments(files, 2)

        self.assertEqual(groups, {
            '1': ['light_1.fit', 'light_2.fit', 'light_3.fit'],
            '2': ['light_4.fit', 'light_5.fit'],
        })

    def test_group_assignment_plan_is_seeded_balanced_and_complete(self) -> None:
        root = Path('frames')
        files = [root / f'light_{number}.fit' for number in range(1, 8)]
        sirilmosaic.random.seed(12345)
        first = sirilmosaic.plan_group_assignments(files, root, 3)
        sirilmosaic.random.seed(12345)
        second = sirilmosaic.plan_group_assignments(files, root, 3)

        self.assertEqual(first, second)
        self.assertEqual([len(first[str(number)]) for number in range(1, 4)], [3, 2, 2])
        self.assertCountEqual(
            [relative for group in first.values() for relative in group],
            [path.name for path in files],
        )

    def test_pipeline_resume_skips_completed_substack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir, executable = self.make_project(root, light_count=4)
            output = root / 'output'
            output.mkdir()
            base_arguments = sirilmosaic.build_parser().parse_args([
                '--workdir', str(workdir), '--output-dir', str(output),
                '--siril-exe', str(executable), '--substacks', '2',
                '--no-drizzle', '--no-cosmetic-correction', '--no-overlap-normalization',
                '--background-method', 'off', '--seed', '12345',
            ])
            sirilmosaic.configure(base_arguments)
            selected = sorted(sirilmosaic.discover_light_files(workdir), key=lambda path: path.name)
            input_manifest = sirilmosaic.write_input_manifest(selected, [(None, selected)])
            sirilmosaic.quality_report = sirilmosaic.initialize_quality_report(len(selected))
            sirilmosaic.quality_report['input_summary'] = sirilmosaic.summarize_input_frames(selected)
            sirilmosaic.quality_report['cohorts'] = sirilmosaic.summarize_frame_cohorts(selected)
            sirilmosaic.quality_report['input_manifest'] = str(input_manifest)
            sirilmosaic.quality_report['substacks'] = [{
                'number': 1,
                'input_frames': 2,
                'accepted_frames': 2,
                'rejected_frames': 0,
                'stack': {'stacked_frames': 2},
            }]
            sirilmosaic.write_quality_report('running')
            lights_sorted = workdir / 'Lights_sorted'
            group_one = lights_sorted / 'group_1' / 'lights'
            group_one.mkdir(parents=True)
            (lights_sorted / 'group_1' / sirilmosaic.SOURCE_MANIFEST).write_text(
                json.dumps({}), encoding='utf-8'
            )
            (lights_sorted / 'group_1' / 'substack_1.fit').write_bytes(b'complete')
            checkpoint = {
                'schema_version': 1,
                'state': 'interrupted',
                'run_id': sirilmosaic.run_id,
                'workdir': str(workdir),
                'output_dir': str(output),
                'configuration_hash': sirilmosaic.configuration_hash(),
                'seed': sirilmosaic.run_seed,
                'input_frames': 4,
                'selected_files': [path.relative_to(workdir).as_posix() for path in selected],
                'input_manifest': str(input_manifest),
                'quality_report': str(sirilmosaic.quality_report_path()),
                'frame_ledger': str(sirilmosaic.frame_ledger_file_path()),
                'substack_count': 2,
                'groups': {
                    '1': [path.relative_to(workdir).as_posix() for path in selected[:2]],
                    '2': [path.relative_to(workdir).as_posix() for path in selected[2:]],
                },
                'completed_substacks': [1],
            }
            sirilmosaic.write_run_checkpoint(checkpoint)
            attempts = []

            def resume_substack(group_number):
                attempts.append(group_number)
                sirilmosaic.quality_report['substacks'].append({
                    'number': group_number,
                    'input_frames': 2,
                    'accepted_frames': 2,
                    'rejected_frames': 0,
                    'discarded_frames': [],
                    'stack': {'stacked_frames': 2},
                })
                return True

            resume_arguments = sirilmosaic.build_parser().parse_args([
                '--workdir', str(workdir), '--output-dir', str(output),
                '--siril-exe', str(executable), '--substacks', '2',
                '--no-drizzle', '--no-cosmetic-correction', '--no-overlap-normalization',
                '--background-method', 'off', '--resume',
            ])
            fake = Mock()
            fake.Open.return_value = True
            with patch('pysiril.siril.Siril', return_value=fake), patch(
                'sirilmosaic.substack', side_effect=resume_substack
            ), patch('sirilmosaic.masterstack'), patch(
                'sirilmosaic.finalize_integration_report'
            ), patch('sirilmosaic.write_verification_artifact', return_value=(
                output / 'verification.json',
                {'status': 'PASS', 'counts': {'PASS': 1, 'WARN': 0, 'FAIL': 0}},
            )):
                result = sirilmosaic.run_pipeline(resume_arguments)

            self.assertEqual(result, 0)
            self.assertEqual(attempts, [2])
            self.assertFalse((output / sirilmosaic.RUN_CHECKPOINT).exists())

    def test_pipeline_resume_reuses_valid_master_written_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir, executable = self.make_project(root, light_count=1)
            output = root / 'output'
            output.mkdir()
            base_arguments = sirilmosaic.build_parser().parse_args([
                '--workdir', str(workdir), '--output-dir', str(output),
                '--siril-exe', str(executable), '--export-per-cohort',
                '--substacks', '1', '--no-drizzle', '--no-cosmetic-correction',
                '--no-overlap-normalization', '--background-method', 'off',
                '--seed', '12345',
            ])
            sirilmosaic.configure(base_arguments)
            selected = sirilmosaic.discover_light_files(workdir)
            cohort_id = 'filter=test'
            manifest = sirilmosaic.write_input_manifest(
                selected, [(cohort_id, selected)]
            )
            tag = (
                f'cohort_001_{sirilmosaic.cohort_filename_tag(selected[0], output, sirilmosaic.run_id)}'
            )
            master = output / f'master_stack_{sirilmosaic.run_id}_{tag}.fit'
            self.write_fits_layer_header(master, 3)
            sirilmosaic.quality_report = sirilmosaic.initialize_quality_report(1)
            sirilmosaic.quality_report.update({
                'input_summary': {
                    'frames': 1, 'bytes': 0, 'total_exposure_seconds': 0,
                    'known_exposure_frames': 0, 'missing_exposure_frames': 1,
                },
                'cohorts': [{'id': cohort_id, 'count': 1}],
                'input_manifest': str(manifest),
                'masters': [{'cohort': cohort_id, 'path': str(master)}],
            })
            sirilmosaic.write_quality_report('running')
            moved = workdir / 'substacks' / 'lights' / 'substack_1.fit'
            moved.parent.mkdir(parents=True)
            self.write_fits_layer_header(moved, 3)
            relative = selected[0].relative_to(workdir).as_posix()
            sirilmosaic.write_run_checkpoint({
                'schema_version': 2,
                'state': 'interrupted',
                'global_phase': 'interrupted',
                'run_id': sirilmosaic.run_id,
                'workdir': str(workdir),
                'output_dir': str(output),
                'configuration_hash': sirilmosaic.configuration_hash(),
                'seed': sirilmosaic.run_seed,
                'input_frames': 1,
                'selected_files': [relative],
                'input_manifest': str(manifest),
                'quality_report': str(sirilmosaic.quality_report_path()),
                'frame_ledger': str(sirilmosaic.frame_ledger_file_path()),
                'substack_count': 1,
                'groups': {'1': [relative]},
                'completed_substacks': [1],
                'cohorts': [{
                    'id': cohort_id,
                    'files': [relative],
                    'substack_count': 1,
                    'groups': {'1': [relative]},
                    'completed_substacks': [1],
                    'state': 'running',
                    'phase': 'master_written',
                }],
            })
            resume_arguments = sirilmosaic.build_parser().parse_args([
                '--workdir', str(workdir), '--output-dir', str(output),
                '--siril-exe', str(executable), '--export-per-cohort',
                '--substacks', '1', '--no-drizzle', '--no-cosmetic-correction',
                '--no-overlap-normalization', '--background-method', 'off', '--resume',
            ])
            fake = Mock()
            fake.Open.return_value = True
            masterstack = Mock()
            substack = Mock()
            with patch.multiple(
                sirilmosaic,
                masterstack=masterstack,
                substack=substack,
                final_cleanup=Mock(),
                finalize_integration_report=Mock(),
                write_verification_artifact=Mock(return_value=(
                    output / 'verification.json',
                    {'status': 'PASS', 'counts': {'PASS': 1, 'WARN': 0, 'FAIL': 0}},
                )),
                open_siril_with_recovery=Mock(),
                execute_siril=Mock(),
                acquire_run_lock=Mock(),
                release_run_lock=Mock(),
                restore_rejected_frames=Mock(return_value=[]),
            ), patch('pysiril.siril.Siril', return_value=fake):
                result = sirilmosaic.run_pipeline(resume_arguments)

            self.assertEqual(result, 0)
            masterstack.assert_not_called()
            substack.assert_not_called()
            self.assertFalse((output / sirilmosaic.RUN_CHECKPOINT).exists())

    def test_configuration_hash_ignores_detected_siril_version(self) -> None:
        legacy_settings = {'auto_substacks': True, 'substacks': 5, 'seed': 12345}
        current_settings = dict(legacy_settings, siril_version='siril 1.4.4')

        self.assertEqual(
            sirilmosaic.configuration_hash(legacy_settings),
            sirilmosaic.configuration_hash(current_settings),
        )

    def test_configuration_hash_preserves_automatic_substack_plan(self) -> None:
        saved_settings = {'auto_substacks': True, 'substacks': 5, 'seed': 12345}
        stale_checkpoint_settings = dict(saved_settings, substacks=2)

        self.assertNotEqual(
            sirilmosaic.configuration_hash(saved_settings),
            sirilmosaic.configuration_hash(stale_checkpoint_settings),
        )

    def test_pipeline_closes_siril_before_cancellation_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            output_dir = Path(directory) / "output"
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--output-dir", str(output_dir),
                "--siril-exe", str(executable),
            ])
            fake = Mock()
            fake.Open.return_value = True
            fake.Execute.return_value = True
            fake.GetData.return_value = []
            events = []
            real_cleanup = sirilmosaic.cleanup

            fake.Close.side_effect = lambda: events.append("close")

            def cleanup_after_close(*args, **kwargs):
                self.assertIn("close", events)
                events.append("cleanup")
                return real_cleanup(*args, **kwargs)

            with patch("pysiril.siril.Siril", return_value=fake), patch(
                "sirilmosaic.substack",
                side_effect=sirilmosaic.CancellationRequested("test cancellation"),
            ), patch("sirilmosaic.cleanup", side_effect=cleanup_after_close):
                result = sirilmosaic.run_pipeline(arguments)

            self.assertEqual(result, 2)
            self.assertLess(events.index("close"), events.index("cleanup"))
            self.assertTrue((workdir / "Lights_sorted").exists())
            self.assertTrue((output_dir / sirilmosaic.RUN_CHECKPOINT).is_file())
            self.assertEqual(len(sirilmosaic.discover_light_files(workdir)), 4)

    def test_configured_commands_match_pipeline_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            group_lights = workdir / "Lights_sorted" / "group_1" / "lights"
            group_lights.mkdir(parents=True)
            (group_lights / "light.fit").touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--substacks", "2",
                "--drizzle",
                "--drizzle-scale", "1.5",
                "--pixel-fraction", "0.7",
                "--drizzle-kernel", "square",
                "--bayer-pattern", "BGGR",
                "--bayer-orientation", "bottom-up",
                "--cosmetic-correction",
                "--cosmetic-hot-sigma", "3.0",
                "--filter-background", "94",
                "--filter-stars", "95",
                "--filter-roundness", "96",
                "--filter-fwhm", "97",
                "--no-adaptive-quality-filtering",
                "--background-method", "quadratic",
                "--weight", "nbstars",
                "--feather", "24",
                "--rejection-low", "2.5",
                "--rejection-high", "3.5",
                "--rejection-method", "winsorized",
                "--stack-normalization", "mulscale",
                "--catalog", "gaia",
            ])
            sirilmosaic.configure(arguments)
            fake = FakeSiril()
            sirilmosaic.app = fake

            self.assertTrue(sirilmosaic.substack(1))

            registration = next(command for command in fake.commands if command.startswith("seqapplyreg"))
            stacking = next(command for command in fake.commands if command.startswith("stack "))
            self.assertIn("-filter-bkg=94%", registration)
            self.assertIn("-scale=1.5 -pixfrac=0.7", registration)
            self.assertIn("-kernel=square", registration)
            self.assertIn("rej winsorized 2.5 3.5", stacking)
            self.assertIn("-weight=nbstars", stacking)
            self.assertIn("-norm=mulscale", stacking)
            self.assertIn("-overlap_norm", stacking)
            self.assertIn("-rejmaps", stacking)
            self.assertNotIn("-rejmap ", stacking)
            self.assertNotIn("-norm=linear -scale", stacking)
            self.assertIn("-feather=24", stacking)
            self.assertIn("-catalog=gaia", " ".join(fake.commands))
            self.assertIn("set32bits", fake.commands)
            self.assertIn("set debayer.use_bayer_header=false", fake.commands)
            self.assertIn("set debayer.pattern=1", fake.commands)
            self.assertIn("set debayer.orientation=3", fake.commands)
            self.assertIn("seqfind_cosme_cfa light 3.0 3.0 -prefix=cc_", fake.commands)
            self.assertIn("calibrate cc_light", fake.commands)
            self.assertNotIn("calibrate cc_light -debayer", fake.commands)
            self.assertTrue(any(command.startswith("seqsubsky pp_cc_light") for command in fake.commands))
            self.assertTrue(any(command.startswith("seqplatesolve bkg_pp_cc_light") for command in fake.commands))
            self.assertNotIn("setfloat 1", fake.commands)

    def test_substack_surfaces_failed_siril_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            group_lights = workdir / "Lights_sorted" / "group_1" / "lights"
            group_lights.mkdir(parents=True)
            (group_lights / "light.xisf").touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
            ])
            sirilmosaic.configure(arguments)
            sirilmosaic.app = FakeSiril(failed_command="set32bits")

            with self.assertRaisesRegex(RuntimeError, "(?s)set32bits.*rejected"):
                sirilmosaic.substack(1)

    def test_substack_skips_rejection_maps_when_rejection_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            group_lights = workdir / "Lights_sorted" / "group_1" / "lights"
            group_lights.mkdir(parents=True)
            (group_lights / "light.fit").touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--rejection-method", "none",
                "--low-rejection-map",
                "--no-high-rejection-map",
            ])
            sirilmosaic.configure(arguments)
            fake = FakeSiril()
            sirilmosaic.app = fake

            self.assertTrue(sirilmosaic.substack(1))

            stacking = next(command for command in fake.commands if command.startswith("stack "))
            self.assertIn("rej none", stacking)
            self.assertNotIn("-rejmaps", stacking)

    def test_no_drizzle_omits_drizzle_registration_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            group_lights = workdir / "Lights_sorted" / "group_1" / "lights"
            group_lights.mkdir(parents=True)
            (group_lights / "light.xisf").touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--substacks", "2",
                "--no-drizzle",
                "--no-cosmetic-correction",
                "--no-overlap-normalization",
            ])
            sirilmosaic.configure(arguments)
            fake = FakeSiril()
            sirilmosaic.app = fake

            self.assertTrue(sirilmosaic.substack(1))

            registration = next(command for command in fake.commands if command.startswith("seqapplyreg"))
            stacking = next(command for command in fake.commands if command.startswith("stack "))
            self.assertNotIn("-drizzle", registration)
            self.assertNotIn("-scale=", registration)
            self.assertNotIn("-pixfrac=", registration)
            self.assertIn("set debayer.use_bayer_header=true", fake.commands)
            self.assertIn("set debayer.pattern=0", fake.commands)
            self.assertIn("set debayer.orientation=0", fake.commands)
            self.assertIn("calibrate light -debayer", fake.commands)
            self.assertNotIn("-overlap_norm", stacking)

    def test_adaptive_filters_background_options_and_fast_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            group_lights = workdir / "Lights_sorted" / "group_1" / "lights"
            group_lights.mkdir(parents=True)
            (group_lights / "light.xisf").touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--no-drizzle",
                "--no-cosmetic-correction",
                "--adaptive-quality-filtering",
                "--quality-filter-sigma", "2.75",
                "--background-method", "rbf",
                "--background-samples", "32",
                "--background-tolerance", "1.4",
                "--fast-normalization",
            ])
            sirilmosaic.configure(arguments)
            fake = FakeSiril()
            sirilmosaic.app = fake

            self.assertTrue(sirilmosaic.substack(1))

            background = next(command for command in fake.commands if command.startswith("seqsubsky"))
            registration = next(command for command in fake.commands if command.startswith("seqapplyreg"))
            stacking = next(command for command in fake.commands if command.startswith("stack "))
            self.assertEqual(
                background,
                "seqsubsky pp_light -rbf -samples=32 -tolerance=1.4 -smooth=0.5",
            )
            self.assertIn("-filter-bkg=2.75k", registration)
            self.assertIn("-filter-nbstars=2.75k", registration)
            self.assertIn("-filter-round=2.75k", registration)
            self.assertIn("-filter-fwhm=2.75k", registration)
            self.assertIn("-fastnorm", stacking)

    def test_background_extraction_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            group_lights = workdir / "Lights_sorted" / "group_1" / "lights"
            group_lights.mkdir(parents=True)
            (group_lights / "light.xisf").touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--no-drizzle",
                "--no-cosmetic-correction",
                "--background-method", "off",
            ])
            sirilmosaic.configure(arguments)
            fake = FakeSiril()
            sirilmosaic.app = fake

            self.assertTrue(sirilmosaic.substack(1))

            self.assertFalse(any(command.startswith("seqsubsky") for command in fake.commands))
            self.assertTrue(any(command.startswith("seqplatesolve pp_light") for command in fake.commands))
            self.assertTrue(any(command.startswith("seqapplyreg pp_light") for command in fake.commands))

    def test_hot_pixel_correction_precedes_non_drizzle_debayer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            group_lights = workdir / "Lights_sorted" / "group_1" / "lights"
            group_lights.mkdir(parents=True)
            (group_lights / "light.xisf").touch()
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--no-drizzle",
                "--cosmetic-correction",
                "--cosmetic-hot-sigma", "3.0",
                "--background-method", "quadratic",
            ])
            sirilmosaic.configure(arguments)
            fake = FakeSiril()
            sirilmosaic.app = fake

            self.assertTrue(sirilmosaic.substack(1))

            correction = "seqfind_cosme_cfa light 3.0 3.0 -prefix=cc_"
            debayer = "calibrate cc_light -debayer"
            self.assertLess(fake.commands.index(correction), fake.commands.index(debayer))
            self.assertTrue(any(command.startswith("seqapplyreg bkg_pp_cc_light") for command in fake.commands))
            self.assertTrue(any(command.startswith("stack r_bkg_pp_cc_light") for command in fake.commands))

    def test_grouping_and_cleanup_restore_fits_and_xisf_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, _ = self.make_project(Path(directory))
            (workdir / "notes.txt").touch()
            original_paths = {
                path.relative_to(workdir)
                for path in sirilmosaic.discover_light_files(workdir)
            }
            sirilmosaic.workdir = workdir
            sirilmosaic.SubStack_nb = 2
            sirilmosaic.debug = False

            sirilmosaic.group_files(workdir)
            grouped = {
                path.suffix.lower()
                for path in (workdir / "Lights_sorted").glob("group_*/lights/*")
            }
            sirilmosaic.cleanup(1)
            sirilmosaic.cleanup(2)
            sirilmosaic.cleanup(1)
            sirilmosaic.cleanup(2)

            restored = {path.suffix.lower() for path in workdir.iterdir() if path.is_file()}
            self.assertEqual(grouped, {".fit", ".xisf"})
            self.assertEqual(
                {path.relative_to(workdir) for path in sirilmosaic.discover_light_files(workdir)},
                original_paths,
            )
            self.assertIn(".txt", restored)

    def test_debug_cleanup_retains_intermediates_and_restores_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, _ = self.make_project(Path(directory))
            sources = set(sirilmosaic.discover_light_files(workdir))
            with patch.multiple(sirilmosaic, workdir=workdir, SubStack_nb=1, debug=True):
                sirilmosaic.group_files(workdir, list(sources))
                intermediate = workdir / 'Lights_sorted' / 'group_1' / 'process' / 'light_00001.fit'
                intermediate.write_bytes(b'debug intermediate')
                sirilmosaic.cleanup(1)
                self.assertEqual(intermediate.read_bytes(), b'debug intermediate')
                self.assertEqual(set(sirilmosaic.discover_light_files(workdir)), sources)

    def test_final_cleanup_removes_generated_substack_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "project"
            lights = workdir / "substacks" / "lights"
            process = workdir / "substacks" / "process"
            lights.mkdir(parents=True)
            process.mkdir()
            (workdir / "Lights_sorted").mkdir()
            (lights / "substack_1.fit").touch()
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = workdir / "chosen output"
            sirilmosaic.SubStack_nb = 1
            sirilmosaic.run_id = "test_run"

            sirilmosaic.final_cleanup()

            self.assertFalse((sirilmosaic.output_dir / "substacks").exists())
            self.assertFalse((workdir / "substacks").exists())
            self.assertFalse((workdir / "Lights_sorted").exists())

    def test_final_cleanup_moves_siril_cwd_before_removing_generated_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "project"
            (workdir / "substacks" / "lights").mkdir(parents=True)
            (workdir / "Lights_sorted").mkdir()
            output_dir = workdir / "chosen output"
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = output_dir
            sirilmosaic.app = Mock()
            sirilmosaic.siril_unresponsive = False

            with patch.object(sirilmosaic, "execute_siril") as execute:
                sirilmosaic.final_cleanup()

            execute.assert_called_once_with(f"cd {sirilmosaic.siril_path(output_dir)}")

    def test_single_substack_becomes_master_without_siril_restack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "project"
            group = workdir / "Lights_sorted" / "group_1"
            group.mkdir(parents=True)
            (group / "substack_1.fit").write_bytes(b"rgb-stack")
            (group / "substack_1_low_rejmap.fit").write_bytes(b"low-map")
            (group / "substack_1_high_rejmap.fit").write_bytes(b"high-map")
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = workdir / "chosen output"
            sirilmosaic.SubStack_nb = 1
            sirilmosaic.debug = False
            sirilmosaic.run_id = "test_run"
            sirilmosaic.stacking_weight = "noise"
            sirilmosaic.pixel_rejection_method = "winsorized"
            sirilmosaic.quality_report = {"substacks": []}
            fake = FakeSiril()
            sirilmosaic.app = fake

            artifact = {
                "path": str(sirilmosaic.output_dir / "master_stack_test_run.fit"),
                "bytes": 9,
                "width": 10,
                "height": 10,
            }
            with patch.object(sirilmosaic, 'require_siril_artifact', return_value=artifact):
                sirilmosaic.masterstack(1)

            self.assertEqual((sirilmosaic.output_dir / "master_stack_test_run.fit").read_bytes(), b"rgb-stack")
            self.assertEqual(
                (sirilmosaic.output_dir / "master_stack_test_run_low_rejmap.fit").read_bytes(),
                b"low-map",
            )
            self.assertTrue((workdir / "substacks" / "lights" / "substack_1.fit").is_file())
            self.assertEqual(fake.commands, [f"cd {sirilmosaic.siril_path(workdir / 'substacks' / 'lights')}"])
            master_report = sirilmosaic.quality_report["master"]
            self.assertEqual(master_report['stacked_frames'], 1)
            self.assertEqual(master_report['output'], {'width': 10, 'height': 10})
            self.assertEqual(master_report['method'], 'single substack copy')
            self.assertEqual(master_report['path'], str(sirilmosaic.output_dir / "master_stack_test_run.fit"))
            self.assertEqual(master_report['weight'], 'noise')
            self.assertEqual(master_report['rejection'], 'winsorized')
            self.assertEqual(master_report['rejection_map_diagnostics']['status'], 'unavailable')
            self.assertEqual(
                master_report['rejection_map_diagnostics']['maps']['low']['reason'],
                'map_unreadable',
            )
            self.assertEqual(sirilmosaic.quality_report["artifact_postconditions"], [artifact])

            sirilmosaic.final_cleanup()

            self.assertFalse((sirilmosaic.output_dir / "substacks").exists())
            self.assertFalse((workdir / "substacks").exists())

    def test_multi_substack_master_uses_selected_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "project"
            for index in (1, 2):
                group = workdir / "Lights_sorted" / f"group_{index}"
                group.mkdir(parents=True)
                (group / f"substack_{index}.fit").touch()
                (group / f"substack_{index}_low_rejmap.fit").touch()
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = Path(directory) / "selected output"
            sirilmosaic.debug = False
            sirilmosaic.run_id = "test_run"
            sirilmosaic.overlap_normalization = False
            sirilmosaic.fast_normalization = True
            fake = FakeSiril()
            sirilmosaic.app = fake

            with patch.object(sirilmosaic, 'require_siril_artifact'):
                sirilmosaic.masterstack(2)

            registration = next(
                command for command in fake.commands if command.startswith('seqapplyreg pp_light')
            )
            stacking = next(command for command in fake.commands if command.startswith("stack "))
            self.assertIn('-framing=max', registration)
            self.assertNotIn("-overlap_norm", stacking)
            self.assertIn("rej none", stacking)
            self.assertIn("-weight=nbstack", stacking)
            self.assertNotIn("-rejmaps", stacking)
            self.assertIn("-fastnorm", stacking)
            self.assertIn(
                sirilmosaic.siril_path_option(
                    "out", sirilmosaic.output_dir / "master_stack_test_run"
                ),
                stacking,
            )
            self.assertIn(f"cd {sirilmosaic.siril_path(sirilmosaic.output_dir)}", fake.commands)
            self.assertTrue(
                (workdir / "substacks" / "rejection_maps" / "substack_1_low_rejmap.fit").is_file()
            )

    def test_four_substacks_enable_master_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "project"
            for index in range(1, 5):
                group = workdir / "Lights_sorted" / f"group_{index}"
                group.mkdir(parents=True)
                (group / f"substack_{index}.fit").touch()
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = Path(directory) / "selected output"
            sirilmosaic.debug = False
            sirilmosaic.run_id = "test_run"
            sirilmosaic.overlap_normalization = False
            sirilmosaic.fast_normalization = False
            sirilmosaic.rej_low = "2.5"
            sirilmosaic.rej_high = "3.5"
            sirilmosaic.pixel_rejection_method = "winsorized"
            fake = FakeSiril()
            sirilmosaic.app = fake

            with patch.object(sirilmosaic, 'require_siril_artifact'):
                sirilmosaic.masterstack(4)

            stacking = next(command for command in fake.commands if command.startswith("stack "))
            self.assertIn("rej winsorized 2.5 3.5", stacking)
            self.assertIn("-weight=nbstack", stacking)
            self.assertIn("-rejmaps", stacking)

    def test_master_skips_rejection_maps_when_rejection_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "project"
            for index in (1, 2):
                group = workdir / "Lights_sorted" / f"group_{index}"
                group.mkdir(parents=True)
                (group / f"substack_{index}.fit").touch()
            sirilmosaic.workdir = workdir
            sirilmosaic.output_dir = Path(directory) / "selected output"
            sirilmosaic.debug = False
            sirilmosaic.run_id = "test_run"
            sirilmosaic.overlap_normalization = False
            sirilmosaic.fast_normalization = False
            sirilmosaic.pixel_rejection_method = "none"
            sirilmosaic.low_rejection_map_enabled = True
            sirilmosaic.high_rejection_map_enabled = False
            sirilmosaic.quality_report = None
            fake = FakeSiril()
            sirilmosaic.app = fake

            with patch.object(sirilmosaic, 'require_siril_artifact'):
                sirilmosaic.masterstack(2)

            stacking = next(command for command in fake.commands if command.startswith("stack "))
            self.assertIn("rej none", stacking)
            self.assertNotIn("-rejmaps", stacking)
            self.assertFalse(
                (sirilmosaic.output_dir / "master_stack_test_run_high_rejmap.fit").exists()
            )

    def test_validation_rejects_too_many_substacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory), light_count=2)
            arguments = sirilmosaic.build_parser().parse_args([
                "--workdir", str(workdir),
                "--siril-exe", str(executable),
                "--substacks", "3",
            ])
            sirilmosaic.configure(arguments)
            with self.assertRaisesRegex(ValueError, "cannot exceed"):
                sirilmosaic.validate_parameters()

    def test_require_siril_artifact_validates_fits_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / 'valid.fit'
            invalid = Path(directory) / 'invalid.fit'
            self.write_fits_layer_header(valid, 1)
            invalid.write_bytes(b'not fits')

            artifact = sirilmosaic.require_siril_artifact(valid, 'test artifact')
            self.assertEqual((artifact['width'], artifact['height']), (10, 10))
            with self.assertRaises(sirilmosaic.SirilCommandError):
                sirilmosaic.require_siril_artifact(invalid, 'test artifact')


if __name__ == "__main__":
    unittest.main()