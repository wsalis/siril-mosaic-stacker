from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
import numpy as np
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
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-4000:])
            report_path = next((root / 'Siril Mosaic Output').glob('quality_report_*.json'))
            report = json.loads(report_path.read_text())
            self.assertEqual(report['status'], 'complete')
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

    def write_fits_layer_header(self, path: Path, layers: int, exposure: float | None = None) -> None:
        cards = [
            "SIMPLE  =                    T",
            "BITPIX  =                   32",
            f"NAXIS   =                    {2 if layers == 1 else 3}",
            "NAXIS1  =                   10",
            "NAXIS2  =                   10",
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

    def test_coverage_crop_handles_empty_and_invalid_pixels(self) -> None:
        for coverage in (np.zeros((2, 3)), np.empty((0, 0)), np.full((2, 3), np.nan)):
            self.assertIsNone(sirilmosaic.coverage_crop_bounds(coverage, 80))
        coverage = np.array([[np.nan, 10, 10], [np.inf, 10, 10]])
        bounds = sirilmosaic.coverage_crop_bounds(coverage, 80)
        self.assertEqual((bounds['x'], bounds['width'], bounds['height']), (1, 2, 2))

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
        ):
            sirilmosaic.create_cropped_master()
        read.assert_called_once_with(Path('seconds.fit'))
        execute.assert_any_call('boxselect 3 1 4 4')
        self.assertEqual(report['coverage']['crop_reference_coverage'], 300)
        self.assertEqual(report['coverage']['crop_threshold'], 150)
        self.assertEqual(report['coverage']['crop_unit'], 's')

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
            self.assertEqual(float(coverage[2, 2]), 1)
            self.assertEqual(float(seconds[2, 2]), 360)
            self.assertEqual(set(np.unique(seconds)), {0, 60, 300, 360})
            np.testing.assert_allclose(coverage, seconds / 360)
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
            sirilmosaic.validate_parameters()

            self.assertEqual(sirilmosaic.SubStack_nb, 1)
            self.assertEqual(sirilmosaic.SIRIL_MAX_STACK_FRAMES, 8192)

            with patch(
                "sirilmosaic.discover_light_files",
                return_value=[workdir / f"light_{index}.fit" for index in range(8193)],
            ):
                sirilmosaic.validate_parameters()

            self.assertEqual(sirilmosaic.SubStack_nb, 2)

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
                app.skip_failed_frames.set(True)

                command = app.command()
                arguments = sirilmosaic.build_parser().parse_args(command[3:])

                self.assertEqual(arguments.workdir, workdir)
                self.assertEqual(arguments.output_dir, output_dir)
                self.assertEqual(arguments.siril_exe, executable)
                self.assertEqual(arguments.substacks, 3)
                self.assertTrue(arguments.auto_substacks)
                self.assertEqual(arguments.drizzle_scale, "1.5")
                self.assertNotIn('--filter-background', command)
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
                self.assertTrue(arguments.skip_failed_frames)
                self.assertTrue(arguments.debug)
                self.assertFalse(arguments.drizzle)
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
            root = self.gui_root()
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
            with patch('pysiril.siril.Siril'), self.assertRaises(FileExistsError):
                sirilmosaic.run_pipeline(arguments)
            self.assertEqual({path: path.read_bytes() for path in staging.rglob('*') if path.is_file()}, before)
            self.assertFalse((workdir / 'prior_source.fit').exists())

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
            ), patch('sirilmosaic.masterstack'), patch('sirilmosaic.finalize_integration_report'):
                self.assertEqual(sirilmosaic.run_pipeline(arguments), 0)
            self.assertEqual(attempts, [1, 1])
            self.assertEqual(fake.Open.call_count, 2)
            self.assertEqual(len(sirilmosaic.quality_report['substack_attempt_failures']), 1)
            self.assertEqual(len(sirilmosaic.discover_light_files(workdir)), 4)

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