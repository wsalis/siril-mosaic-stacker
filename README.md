# Siril Mosaic Stacker

**Version 1.4**

A source-safe graphical workflow for stacking large mosaics and high-frame-count image sets with [Siril](https://siril.org/). It groups source frames into randomized substacks, registers and integrates each group, then creates a final registered master stack.

## Highlights

- Restores source frames to their original locations after success, cancellation, or failure
- Supports FITS (`.fit`, `.fits`, `.fts`) and XISF input
- CFA metadata preflight with explicit Bayer pattern and row-orientation controls
- Optional CFA cosmetic correction with separate cold/hot sigma controls
- Percentage or adaptive k-sigma quality filtering
- Drizzle, overlap normalization, rejection, weighting, and feathering controls
- Optional fast normalization for large light-frame sets
- Optional failed-frame skipping that records excluded calibrated frames in logs and JSON reports
- Optional automatic substack sizing for large sequences (8192-frame Siril UCRT64 limit)
- Preflight storage estimates and integrated-exposure-hour reporting
- Informational relative sky-condition scoring
- Acquisition-cohort reporting for mixed camera/exposure/gain/filter collections
- Optional registered-frame coverage maps and coverage-based auto-cropped masters
- Optional test-mode frame sampling for quick runs without changing directories
- Progress estimates, cancellation, logs, profiles, and JSON quality reports
- Avoids statistically weak master rejection when fewer than four substacks exist

## Requirements

- Windows 10 or 11, or macOS with a compatible Siril installation
- Python 3.10 or newer with Tk support
- Siril 1.4 or newer

Install Python dependencies:

```powershell
py -m pip install -r requirements.txt
```

## Run

Double-click `run_siril_mosaic.bat`, or run:

```powershell
python sirilmosaic_gui.py
```

Choose the folder containing the source lights, a separate output folder, and the Siril executable. Review the detected frame count and CFA warning before starting.

For the ZWO ASI533MC, `RGGB` with `Bottom-up` orientation is the known-good explicit selection when `ROWORDER` metadata is absent.

On macOS, select the Siril executable inside the application bundle, for example:

```text
/Applications/Siril.app/Contents/MacOS/siril
```

The same Python GUI and processing pipeline can be launched with `python3 sirilmosaic_gui.py` after installing the requirements in a virtual environment.

## Quick Start

1. Install Python 3.10 or newer with Tk support, Siril 1.4 or newer, and the packages in `requirements.txt`.
2. Launch `sirilmosaic_gui.py` or use `run_siril_mosaic.bat`.
3. Select the input folder, a separate output folder, and the Siril executable.
4. Review the detected frame count, CFA preflight warnings, and storage estimate.
5. For a first run, leave `Test frame count` at `0`, keep drizzle disabled, and use one substack unless the sequence exceeds Siril's frame limit.
6. Choose either percentage or adaptive quality filtering, then start the run.
7. Review the master stack, timestamped log, and `quality_report_<run_id>.json` in the output folder.

The application temporarily stages source frames and restores them to their original relative paths. Do not edit `Lights_sorted` during a run. Keep independent backups of irreplaceable acquisitions.

## Large Collections

Siril's experimental Windows UCRT64 build supports up to 8,192 files in a sequence. For large collections such as Seestar captures, enable `Auto substacks (max 8192 frames each)`. The app counts eligible FITS/XISF inputs and divides them into enough substacks to stay within that limit.

Recommended starting settings for very large collections:

- Enable automatic substacks
- Keep drizzle disabled unless the larger output is needed
- Enable failed-frame skipping for unattended runs
- Keep debug mode disabled to reduce temporary storage
- Leave enough free disk space for converted, calibrated, registered, and rejection-map products

## Output

The selected output folder receives the final master stack and a timestamped JSON quality report. Temporary substacks and rejection maps are removed after successful normal runs; use debug mode to retain intermediates for investigation. Run logs are written beneath the source folder in `siril_mosaic_logs`.

Before starting, the GUI estimates peak working storage from the input data size and checks both the work and output drives. The estimate is conservative and accounts for the larger temporary products created during processing; it is a planning estimate, not a guarantee.

At completion, the JSON report and GUI completion dialog report integrated exposure by summing finite, positive `EXPTIME` values from the actual registered files selected for stacking. Exact integrated seconds/hours are `null` when metadata or stack membership is incomplete; there is no frame-proportional fallback. `exposure_complete` and `integrated_exposure_basis` explain availability. Input exposure totals cover only readable positive metadata, with missing-frame counts reported separately. These times describe contributing frames before per-pixel rejection and weighting.

Each completed substack includes a compact `discarded_frames` list: original relative filename, sequence image number, exposure, stage completion flags, status, and `reason_code`. Successful filenames are omitted. Explicit plate-solving failures are identified; otherwise `candidate_filters` are run-wide possibilities, not confirmed individual rejection causes. Unknown stack membership is counted separately and is never labeled as a known discard. Numbered staging names and the source manifest preserve source identity across filename extensions. `stage_exposure_seconds` is exact or `null`; `stage_known_exposure_seconds` and `stage_missing_exposure_frames` expose incomplete metadata.

Report schema version 2 records the effective selection mode and Siril flags, command durations/status, failed-command response tails, and substack attempt failures. Reports are replaced atomically. Final registration export totals take precedence over preliminary filter counts; stack dimensions come from the saved output. A command failure before a substack completes may have command-level diagnostics without a complete per-frame summary. Multi-substack integrated exposure is withheld unless all contributing substacks are confirmed in the final stack.

The completion output includes a relative filter-retention score from 0 to 100 under the legacy `sky_condition` report key. It combines background, FWHM, roundness, and plate-solving pass fractions; mosaic-aware mode excludes raw star count. It depends on the selected filters and input population, so it is not an objective measure of sky quality, a Bortle classification, or a selector itself.

## Frame Selection

- Adaptive mode replaces percentage thresholds with the selected positive k-sigma threshold. Disabled percentage values do not affect the command or validation.
- Percentage mode keeps the best requested percentage for each enabled criterion. Values must be whole numbers from 1 through 100. The criteria intersect with each other and plate-solving success, so 90% on several criteria does not promise 90% overall retention. The disabled sigma field is ignored.
- Mosaic-aware mode disables the star-count criterion in both modes, including its validation. Background, roundness, and FWHM still apply globally.
- The experimental `--sky-quality-percent` filter is unsupported; values other than 100 are rejected instead of sending an unreliable Siril flag.

Enable `Mosaic-aware star count` when one run contains frames from different sky regions. Star count remains available in registration diagnostics, but raw star count is removed from global rejection and sky scoring so naturally sparse fields are not penalized.

The preflight summary and JSON report group frames into acquisition cohorts using camera model, exposure time, gain, filter, and image dimensions. This is useful for mixed Seestar S30/S50 and Pro collections. The current cohort feature is diagnostic only: grouping remains randomized and quality filters are still global.

Enable `Write coverage map` to sum each registered frame's `EXPTIME` wherever it has finite, nonzero signal on the full mosaic canvas. A 60-second frame overlapping a 300-second frame contributes 360 seconds, not two equal frame counts. Two FITS outputs are written:

- `coverage_map_<run_id>.fit`: a floating-point 0-1 viewing map. Black means no data; white marks the greatest integration time. Multiply a pixel by the report's `normalization_seconds` to recover seconds.
- `integration_time_map_<run_id>.fit`: seconds per pixel (`BUNIT='s'`), used for measurement and autocropping. Raw second values may saturate an image viewer's default 0-1 display; use the normalized map for viewing.

Both maps cover the **entire uncropped master**, independent of the crop percentage. Enable `Create auto-cropped master` to preserve that master and write a second master containing the largest axis-aligned rectangle entirely meeting the minimum integration time. `Crop depth (%)` sets that minimum relative to the median integration time of nonblank pixels, not the deepest panel overlap. The default 50% requires half that typical integration; lower values keep more field, while higher values reject more shallow coverage. This percentage is not the percentage of image area retained. Black corners and internal gaps are excluded, so an irregular footprint can still require sacrificing some usable field to form a clean rectangle. Integration times are before per-pixel rejection and stacking weights, not effective weighted exposure or a direct noise measurement. If any registered frame lacks a finite positive `EXPTIME`, coverage and autocrop are skipped with a warning rather than guessing its contribution.

The maps use Siril's sequence placement data and maximize-framing canvas, and their pixel reduction is streamed through NumPy to avoid loading the full sequence at once. Crop reports record the reference integration time and cutoff in seconds, FITS-array bounds, and converted Siril selection coordinates. Existing crop-percentage profile values now refer to typical integration time rather than peak frame coverage. Coverage cropping currently requires a single substack. Previously generated count maps are not retroactively converted to integration-time maps; an already running process also keeps the code it loaded at startup.

Set `Test frame count (0 = all)` to randomly sample a smaller number of eligible frames for a quick test. Only the sampled frames are moved and processed; unselected source files remain in place. Use `0` for a normal full run.

The experimental Sky quality keep filter is currently hidden from the GUI because Siril does not reliably provide the underlying per-frame quality metric in this workflow. The robust Adaptive quality filters remain the recommended active selection method.

Enable `Skip failed frames (log only)` when a single frame may fail during calibration or debayering. The stacker excludes malformed calibrated frames from later Siril sequence operations and records each skipped sequence filename, image number, stage, and reason in the run log and JSON report. The option is disabled by default; a run still fails normally when it is unchecked.

Enable `Auto substacks (max 8192 frames each)` for large collections. The app counts eligible input frames and calculates the smallest number of substacks that keeps every Siril sequence at or below 8192 frames. Manual substack selection remains available when this option is disabled.

The GUI stores profiles and recently selected paths under `%APPDATA%\Siril Mosaic Stacker`; the last successfully loaded or saved profile is restored at startup. Merely changing the profile dropdown does not change that remembered profile. These files are not stored in this repository.

## Tests

```powershell
python -m pytest -q
```

GUI tests share one Tcl/Tk interpreter with fresh windows and isolated application settings. Two optional real-Siril tests are skipped by default:

```powershell
$env:SIRIL_TEST_CLI = 'C:\Program Files\Siril\bin\siril-cli.exe'
python -m pytest -q -k real_siril_preserves
$env:SIRIL_TEST_EXE = 'C:\Program Files\Siril\bin\siril.exe'
$env:SIRIL_TEST_LIGHTS = 'W:\Astro\LBN576'
python -m pytest -q -k real_pipeline_on_copied_frames
```

The conversion test uses synthetic mixed-extension FITS with different exposures. The pipeline smoke test copies six 60-second LBN 576 frames into temporary storage and verifies integration, coverage, crop dimensions, and byte-for-byte restoration of the copies. It never stages the original dataset. Close other Siril sessions before these tests. Other cameras, XISF conversion, and multi-substack/drizzle combinations need separate real-data validation.

## Parking Lot

- **Multi-substack integration-time coverage and autocrop:** generate a coverage map for each registered substack, then compose those maps onto the final master canvas using the final substack-registration placements. Run the existing integration-time threshold and largest-rectangle crop algorithm on the composed map. Validate the coordinate transforms and vertical orientation with synthetic pixel-placement tests before enabling it.

## Source Safety

The application records every moved source frame in `source_manifest.json` and restores it to its original relative path, including in debug mode. It refuses a preexisting `Lights_sorted` directory without cleaning that directory. Debug mode retains generated intermediates for investigation; move or remove those retained products before starting another run. Retries restart failed Siril substack commands from clean generated files, record the failed attempt, and never retry cancellation. Do not manually alter `Lights_sorted` while a run is active. Keeping an independent backup of irreplaceable acquisition data is still recommended.
