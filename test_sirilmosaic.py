from __future__ import annotations

import json
import struct
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock, patch

import sirilmosaic
from sirilmosaic_gui import RunProgressEstimator, SirilMosaicApp, terminate_siril_descendants


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
            (self.cwd.parent / f"{output_name}.fit").touch()
        return command != self.failed_command

    def GetData(self) -> list[str]:
        return [f"log: rejected {self.failed_command}", "status: error"]


class SirilMosaicTests(unittest.TestCase):
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

    def test_output_folder_is_excluded_from_source_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "selected output"
            output.mkdir()
            (root / "source.xisf").touch()
            (root / "master_stack.fit").touch()
            (root / "master_stack_20260912_120000.fit").touch()
            (root / "substack_1_low_rejmap.fit").touch()
            (output / "old_master.fit").touch()

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

    def test_gui_maps_all_core_inputs_to_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir, executable = self.make_project(Path(directory))
            root = tk.Tk()
            root.withdraw()
            try:
                app = SirilMosaicApp(root)
                app.workdir.set(str(workdir))
                output_dir = Path(directory) / "selected output"
                app.output_dir.set(str(output_dir))
                app.siril_exe.set(str(executable))
                app.substacks.set(3)
                app.drizzle_scale.set(1.5)
                app.filter_background.set(94)
                app.debug.set(True)
                app.drizzle.set(False)
                app.bayer_pattern.set("GRBG")
                app.bayer_orientation.set("Bottom-up")
                app.cosmetic_correction.set(True)
                app.cosmetic_cold_sigma.set(4.2)
                app.cosmetic_hot_sigma.set(3.4)
                app.overlap_normalization.set(False)
                app.adaptive_quality_filtering.set(True)
                app.quality_filter_sigma.set(2.8)
                app.background_method.set("RBF")
                app.background_samples.set(30)
                app.background_tolerance.set(1.3)
                app.fast_normalization.set(True)

                command = app.command()
                arguments = sirilmosaic.build_parser().parse_args(command[3:])

                self.assertEqual(arguments.workdir, workdir)
                self.assertEqual(arguments.output_dir, output_dir)
                self.assertEqual(arguments.siril_exe, executable)
                self.assertEqual(arguments.substacks, 3)
                self.assertEqual(arguments.drizzle_scale, "1.5")
                self.assertEqual(arguments.filter_background, 94)
                self.assertEqual(arguments.bayer_pattern, "GRBG")
                self.assertEqual(arguments.bayer_orientation, "bottom-up")
                self.assertTrue(arguments.cosmetic_correction)
                self.assertEqual(arguments.cosmetic_cold_sigma, "4.2")
                self.assertEqual(arguments.cosmetic_hot_sigma, "3.4")
                self.assertFalse(arguments.overlap_normalization)
                self.assertTrue(arguments.adaptive_quality_filtering)
                self.assertEqual(arguments.quality_filter_sigma, 2.8)
                self.assertEqual(arguments.background_method, "rbf")
                self.assertEqual(arguments.background_samples, 30)
                self.assertEqual(arguments.background_tolerance, 1.3)
                self.assertTrue(arguments.fast_normalization)
                self.assertTrue(arguments.debug)
                self.assertFalse(arguments.drizzle)
            finally:
                root.destroy()

    def test_gui_uses_hot_only_cold_sigma_default(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = SirilMosaicApp(root)
            self.assertEqual(app.cosmetic_cold_sigma.get(), 50.0)
            self.assertEqual(app.cosmetic_hot_sigma.get(), 3.0)
        finally:
            root.destroy()

    def test_gui_groups_mosaic_settings_by_processing_stage(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            SirilMosaicApp(root)
            groups = {
                child.cget("text")
                for container in root.winfo_children()
                for child in container.winfo_children()
                if isinstance(child, ttk.LabelFrame)
            }

            self.assertTrue({
                "Capture & CFA",
                "Cosmetic correction",
                "Frame selection",
                "Integration",
                "Background & plate solving",
                "Resources",
            }.issubset(groups))
        finally:
            root.destroy()

    def test_gui_run_profiles_persist_processing_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profiles.json"
            root = tk.Tk()
            root.withdraw()
            try:
                app = SirilMosaicApp(root, profile_path=profile_path)
                app.profile_name.set("OSC drizzle")
                app.drizzle_scale.set(1.7)
                app.bayer_pattern.set("RGGB")
                app.cosmetic_correction.set(False)
                app.cosmetic_cold_sigma.set(4.5)
                app.cosmetic_hot_sigma.set(4.0)
                app.overlap_normalization.set(False)
                app.filter_background.set(93)
                app.adaptive_quality_filtering.set(True)
                app.quality_filter_sigma.set(2.6)
                app.background_method.set("Linear")
                app.fast_normalization.set(True)
                app.debug.set(True)
                app.save_profile(notify=False)

                app.drizzle_scale.set(2.5)
                app.bayer_pattern.set("Auto (header)")
                app.cosmetic_correction.set(True)
                app.cosmetic_cold_sigma.set(2.5)
                app.cosmetic_hot_sigma.set(2.0)
                app.overlap_normalization.set(True)
                app.filter_background.set(99)
                app.adaptive_quality_filtering.set(False)
                app.quality_filter_sigma.set(4.0)
                app.background_method.set("Off")
                app.fast_normalization.set(False)
                app.debug.set(False)
                app.load_profile(notify=False)

                self.assertEqual(app.drizzle_scale.get(), 1.7)
                self.assertEqual(app.bayer_pattern.get(), "RGGB")
                self.assertFalse(app.cosmetic_correction.get())
                self.assertEqual(app.cosmetic_cold_sigma.get(), 4.5)
                self.assertEqual(app.cosmetic_hot_sigma.get(), 4.0)
                self.assertFalse(app.overlap_normalization.get())
                self.assertEqual(app.filter_background.get(), 93)
                self.assertTrue(app.adaptive_quality_filtering.get())
                self.assertEqual(app.quality_filter_sigma.get(), 2.6)
                self.assertEqual(app.background_method.get(), "Linear")
                self.assertTrue(app.fast_normalization.get())
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
            root = tk.Tk()
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

            reopened_root = tk.Tk()
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
            root = tk.Tk()
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
            self.assertFalse((workdir / "Lights_sorted").exists())

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
            self.assertIn("rej linear 2.5 3.5", stacking)
            self.assertIn("-weight=nbstars", stacking)
            self.assertIn("-norm=addscale", stacking)
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
                "seqsubsky pp_light -rbf -samples=32 -tolerance=1.4",
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

    def test_final_cleanup_removes_intermediate_substacks(self) -> None:
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
            fake = FakeSiril()
            sirilmosaic.app = fake

            sirilmosaic.masterstack(1)

            self.assertEqual((sirilmosaic.output_dir / "master_stack_test_run.fit").read_bytes(), b"rgb-stack")
            self.assertEqual(
                (sirilmosaic.output_dir / "master_stack_test_run_low_rejmap.fit").read_bytes(),
                b"low-map",
            )
            self.assertTrue((workdir / "substacks" / "lights" / "substack_1.fit").is_file())
            self.assertEqual(fake.commands, [f"cd {sirilmosaic.siril_path(workdir / 'substacks' / 'lights')}"])

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

            sirilmosaic.masterstack(2)

            stacking = next(command for command in fake.commands if command.startswith("stack "))
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
            fake = FakeSiril()
            sirilmosaic.app = fake

            sirilmosaic.masterstack(4)

            stacking = next(command for command in fake.commands if command.startswith("stack "))
            self.assertIn("rej linear 2.5 3.5", stacking)
            self.assertIn("-weight=nbstack", stacking)
            self.assertIn("-rejmaps", stacking)

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


if __name__ == "__main__":
    unittest.main()