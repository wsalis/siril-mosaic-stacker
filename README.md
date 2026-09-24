# Siril Mosaic Stacker

**Version 2.2**

In a nutshell, this is an automated Python front end into [Siril](https://siril.org/) specifically for stacking. This project came about because I wanted and needed a way to stack mixed mosaic astrophotography data that doesn't get nicely grouped into tiles for traditional stacking. For example, data gathered of the same deep sky object over multiple years where orientation and equipment may differ.

It cosmetic corrects light files, corrects for gradients, registers and integrates each group, then creates a final registered master stack.

There are also auto-filtering features to help identify potentially poor frames in large datasets. Optional integration-time coverage maps support auto-cropping both single- and multi-substack masters.

Overall, this gives a pretty good single-click process and let your computer do the work. You just load up your raw light files into a directory, point the application to that directory, and run. After a while you will get a master stack, a cropped stack (if you chose to autocrop), high/low rejection files, a mosaic coverage map (indicates which parts of the master stack have the most integration time), and a frame selection quality report.

This is suitable for both dedicated rig data (larger files, longer exposures) and large numbers of smart scope lights (smaller files, shorter exposures).

Small word of warning, though using the overlap normalization feature sounds nice, you really only want to use it when you are stacking dedicated rig data where you aren't dealing with thousands of files. If you enable this when you have lots of lights to process, it runs that process very slowly. There's a fast normalization option but really you don't want to enable this if you're doing work with a bunch of smart scope light frames. It's better for when you're working with a few hundred light files.

Happy stacking!

## Highlights

- Restores source frames to their original locations after success, cancellation, or failure
- Supports FITS (`.fit`, `.fits`, `.fts`) and XISF input
- CFA metadata preflight with explicit Bayer pattern and row-orientation controls
- Optional CFA cosmetic correction with separate cold/hot sigma controls
- Percentage or adaptive k-sigma quality filtering
- Drizzle kernel, overlap normalization, pixel rejection method, weighting, and feathering controls
- Optional fast normalization for large light-frame sets
- Optional failed-frame skipping that records excluded calibrated frames and partial plate-solve failures in logs and JSON reports
- Version-aware automatic substack sizing for large sequences (2,048 on older supported Siril versions; 8,192 on Siril 1.4+)
- Preflight storage estimates and integrated-exposure-hour reporting
- Informational relative sky-condition scoring
- Selectable acquisition-cohort grouping by Camera model, Filter, and Exposure time
- Optional registered-frame coverage maps and coverage-based auto-cropped masters
- Optional test-mode frame sampling for quick runs without changing directories
- Progress estimates, cancellation, logs, profiles, and JSON quality reports
- Avoids statistically weak master rejection when fewer than four substacks exist
- Advanced Siril controls for plate solving, background extraction, registration, and reproducible seeds

## Requirements

- Windows 10 or 11, or macOS with a compatible Siril installation
- Python 3.10 or newer with Tk support
- Siril 1.3.6 or newer; Siril 1.4+ supports 8,192-frame sequences and older supported versions use 2,048

Install Python dependencies:

```powershell
py -m pip install -r requirements.txt
```

## Windows Executable

Windows users who do not want to install Python can download the standalone `SirilMosaicStacker.exe` from the [Releases](https://github.com/wsalis/siril-mosaic-stacker/releases) page instead of cloning the repository. It still requires a separate Siril installation. Build it yourself from source with:

```powershell
py -m pip install pyinstaller -r requirements.txt
py -m PyInstaller --noconfirm --clean SirilMosaicStacker.spec
```

The executable is written to `dist\SirilMosaicStacker.exe`.

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

1. Install Python 3.10 or newer with Tk support, Siril 1.3.6 or newer, and the packages in `requirements.txt`.
2. Launch `sirilmosaic_gui.py` or use `run_siril_mosaic.bat`.
3. Select the input folder, a separate output folder, and the Siril executable.
4. Review the detected frame count, CFA preflight warnings, and storage estimate.
5. For a first run, leave `Test frame count` at `0`, keep drizzle disabled, and use one substack unless the sequence exceeds Siril's frame limit.
6. Choose either percentage or adaptive quality filtering, then start the run.
7. Review the master stack, timestamped log, and `quality_report_<run_id>.json` in the output folder.

The application temporarily stages source frames and restores them to their original relative paths. Do not edit `Lights_sorted` during a run. Keep independent backups of irreplaceable acquisitions.

Each run records a human-readable log, a JSON quality report, a JSONL event journal, and an input manifest containing the selected files, acquisition cohorts, configuration hash, and random seed. These artifacts make large or failed runs easier to inspect and reproduce.

## Documentation

- [Command Cookbook](docs/COMMAND_COOKBOOK.md)
- [Artifact Reference](docs/ARTIFACT_REFERENCE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Compatibility Matrix](docs/COMPATIBILITY.md)
- [FAQ](docs/FAQ.md)
- [Validation Matrix](docs/VALIDATION_MATRIX.md)
- [Healthy Run Example](docs/HEALTHY_RUN_EXAMPLE.md)
- [Visual Guide](docs/VISUAL_GUIDE.md)
- [Release Checklist](docs/RELEASE_CHECKLIST.md)
- [Next Version Parking Lot](docs/NEXT_VERSION_PARKING_LOT.md)
- [Changelog](CHANGELOG.md)

Drizzle uses Siril's selectable `point`, `turbo`, `square`, `gaussian`, `lanczos2`, or `lanczos3` kernels; `lanczos3` is the default. Droplet size controls Siril's `pixfrac` value from 0.1 to 1.0; `0.8` is the default. Pixel rejection supports `none`, `percentile`, `sigma`, `mad`, `median`, `linear`, `winsorized`, and `generalized`. `linear` is the default. Percentile and generalized rejection use low/high parameters from 0 to 1; the other active rejection methods use the existing sigma-like threshold fields. The selected method applies to substack integration and, when at least four substacks are present, final master integration.

The Advanced tab exposes selected Siril 1.4 controls without changing the safe Basic workflow: SIP plate-solve order, downscale and search hints, RBF smoothing and dithering, registration transform/interpolation, minimum star pairs, maximum stars, and an optional reproducibility seed. Values left at their defaults preserve the normal command path.

Stack normalization supports Siril's `add`, `mul`, `addscale`, and `mulscale` modes. The default is `addscale`, which combines additive background correction with scaling. The selected mode applies to substack and final-master integration.

During file-processing phases, the progress text estimates completed files as `current/total`; phases such as master integration and final output writing do not show a file count.

## Large Collections

Siril's Windows builds support different sequence limits by version. Siril 1.4+ supports up to 8,192 files in a sequence; older supported versions use a 2,048-file limit. For large collections such as Seestar captures, enable `Auto substacks (detected Siril limit)`. The app checks the selected executable's version and divides eligible FITS/XISF inputs into enough substacks to stay within its limit.

Recommended starting settings for very large collections:

- Enable automatic substacks
- Keep drizzle disabled unless the larger output is needed
- Enable failed-frame skipping for unattended runs
- Keep debug mode disabled to reduce temporary storage
- Leave enough free disk space for converted, calibrated, registered, and rejection-map products

## Output

The selected output folder receives the final master stack and a timestamped JSON quality report. Temporary substacks and rejection maps are removed after successful normal runs; use debug mode to retain intermediates for investigation. Run logs are written beneath the source folder in `siril_mosaic_logs`.

Before starting, the GUI estimates peak working storage from the input data size and checks both the work and output drives. The estimate is conservative and accounts for the larger temporary products created during processing; it is a planning estimate, not a guarantee.

At completion, the JSON report and GUI completion dialog report stacked/input frame accounting, verification status, master and crop counts, artifact paths, and integrated exposure. Run Review is refreshed with verification and artifact actions. Exposure is summed from finite, positive `EXPTIME` values from the actual registered files selected for stacking. Exact integrated seconds/hours are `null` when metadata or stack membership is incomplete; there is no frame-proportional fallback. `exposure_complete` and `integrated_exposure_basis` explain availability. Input exposure totals cover only readable positive metadata, with missing-frame counts reported separately. These times describe contributing frames before per-pixel rejection and weighting.

Each completed substack includes a compact `discarded_frames` list: original relative filename, sequence image number, exposure, stage completion flags, status, and `reason_code`. For frame-selection exclusions, `filter_metrics` includes the measured Siril value, threshold, comparison, and pass/fail status for every active quality filter when sequence registration records are available, including metrics that passed on the rejected frame. `filter_metric_source` identifies whether those values came from Siril sequence registration data or were unavailable. Successful filenames are omitted. Explicit plate-solving failures are identified; otherwise `candidate_filters` are run-wide possibilities, not confirmed individual rejection causes. Unknown stack membership is counted separately and is never labeled as a known discard. Numbered staging names and the source manifest preserve source identity across filename extensions. `stage_exposure_seconds` is exact or `null`; `stage_known_exposure_seconds` and `stage_missing_exposure_frames` expose incomplete metadata.

Each run also writes `frame_ledger_<run_id>.jsonl`, one record per selected frame. It includes the original relative filename, cohort, substack, exposure, every active filter metric, stage completion flags, stack membership, final status, and rejection reason where applicable. The ledger is appended after each successful substack and is independently checked by Run Verification.

Report schema version 3 records the effective selection mode and Siril flags, command durations/status, response tails, reproducibility metadata, and substack attempt failures. Reports are replaced atomically. Final registration export totals take precedence over preliminary filter counts; stack dimensions come from the saved output. A command failure before a substack completes may have command-level diagnostics without a complete per-frame summary. Multi-substack integrated exposure is withheld unless all contributing substacks are confirmed in the final stack.

The `Run Review` tab includes `Verify Run`, which performs a read-only PASS/WARN/FAIL audit of the report status, journal JSONL integrity, input manifest, frame accounting, master and coverage dimensions, crop artifacts, source restoration, and reject-folder agreement. New completed reports also inventory every durable generated FITS artifact with its role, path, byte size, dimensions, and SHA-256 fingerprint; verification fails if an inventoried artifact is missing or changed. Completed runs automatically write `verification_<run_id>.json` beside the quality report. The same audit is available from the CLI with `--verify-run <quality_report.json>`.

`Open Frame Ledger` provides filterable full-frame evidence by status, metric failure, and filename; `Export Ledger CSV` writes the filtered view for inspection outside the GUI. Low-cardinality fields use value dropdowns, while file/path fields keep case-insensitive `Contains` search. Table headers sort values, and `Clear Sort & Filter` restores the complete table and default order.

`Preview Run` runs the same integrity scan first and includes its PASS/WARN/FAIL findings alongside the dry-run command and storage/cohort summary.

The analysis workspace adds report-driven views. `Run History` scans the selected output folder, compares two completed runs, verifies a selected report, and exports ZIP or self-contained HTML evidence. Selecting exactly one history row makes it active and automatically refreshes Run Review, Threshold Lab, Quality Explorer, Frame Inspector, Cohort Balance, Coverage Inspector, Visual QA, and Cropping Workbench. `Threshold Lab` interactively replays keep-best targets from one cached ledger. `Quality Explorer` plots measured FWHM, roundness, background, or star metrics, states whether values pass above or below the cutoff, and discloses when the plotted line is the median of varying recorded thresholds. `Frame Inspector` links each ledger row to its source preview, metrics, thresholds, rejection reason, and stage state. `Cohort Balance` compares per-cohort totals, retention, rejects, and integrated exposure. `Coverage Inspector` previews the saved integration-time map and summarizes canvas, crop, and integration statistics. `Visual QA` performs preview-level checks for blank edges, clipping, broad gradients, coverage holes, and artifact geometry. `Batch Queue` processes multiple folders sequentially using the current settings.

The Help button opens a tabbed module guide. Each Help tab corresponds to a major application area and explains its selections, tradeoffs, evidence, safety behavior, and recovery workflow.

Run Review can export a ZIP evidence bundle containing the report, automatic verification artifact, manifest, frame ledger, configuration summary, journal, and relevant logs without including raw light frames. `Cleanup Review` lists temporary staging, checkpoints, locks, `.tmp` files, and launcher logs without deleting anything automatically. Staged source folders, active locks, and rejects are review-only and cannot be removed by that action. The main Start/Resume action row holds the less-frequent `Checkpoint Status`, `Discard Checkpoint`, `Abandon Run`, `Run Lock Status`, and `Break Run Lock` controls. `Abandon Run` restores staged sources, removes temporary staging/substacks and checkpoint metadata, and retains reports/logs before a fresh start. If a failed run left only manifests and empty staging folders, Abandon Run offers a second confirmation to remove that orphan metadata; it reports manifest entries that were already missing and does not claim to restore them. `Preview Run` performs the deep integrity scan first and includes PASS/WARN findings in its preview; the CLI also exposes `--integrity-scan` for standalone preflight use.

The backend watchdog defaults to a 60-second Siril open timeout and no command timeout. Override them with `--siril-open-timeout` and `--siril-command-timeout`; set a positive command timeout when external supervision requires one. `--minimum-free-disk-gb` defaults to 0.5 and is checked after each completed substack. A timed-out Siril pipe is marked unhealthy, descendant Siril processes are terminated, and the run rolls back rather than continuing with an uncertain command state.

When `Skip failed frames` is enabled, partial plate-solving success continues with the solved frames. A cohort with zero successfully plate-solved frames is recorded as skipped, its staged sources are restored, and a per-cohort run continues with later cohorts. A non-cohort run still fails cleanly because it has no usable frames for a master stack.

The completion output includes a relative filter-retention score from 0 to 100 under the legacy `sky_condition` report key. It combines background, FWHM, roundness, and plate-solving pass fractions; mosaic-aware mode excludes raw star count. It depends on the selected filters and input population, so it is not an objective measure of sky quality, a Bortle classification, or a selector itself.

## Frame Selection

- Adaptive mode replaces percentage thresholds with the selected positive k-sigma threshold. Disabled percentage values do not affect the command or validation.
- Percentage mode keeps the best requested percentage for each enabled criterion. Values must be whole numbers from 1 through 100. The criteria intersect with each other and plate-solving success, so 90% on several criteria does not promise 90% overall retention. The disabled sigma field is ignored.
- Mosaic-aware mode disables the star-count criterion in both modes, including its validation. Background, roundness, and FWHM still apply globally.
- The experimental `--sky-quality-percent` filter is unsupported; values other than 100 are rejected instead of sending an unreliable Siril flag.

Enable `Mosaic-aware star count` when one run contains frames from different sky regions. Star count remains available in registration diagnostics, but raw star count is removed from global rejection and sky scoring so naturally sparse fields are not penalized.

The preflight summary and JSON report group frames into acquisition cohorts using the checked grouping fields: Camera model, Filter, and Exposure time. All three are enabled by default. This is useful for mixed Seestar S30/S50 and Pro collections. Enable `Export one master per acquisition cohort`, then choose the grouping fields on the same Basic-tab line, to process each cohort independently. When coverage is enabled, each cohort also receives matching tagged coverage maps and an optional cropped master.

Enable `Write coverage map` to sum each registered frame's `EXPTIME` over its transformed detector footprint on the full mosaic canvas. For drizzled registrations, interior point-sampling holes are filled for coverage accounting while the transformed outer footprint is preserved. A 60-second frame overlapping a 300-second frame contributes 360 seconds, not two equal frame counts. Two FITS outputs are written:

- `coverage_map_<run_id>[_<cohort_tag>].fit`: a floating-point 0-1 viewing map. Black means no data; white marks the greatest integration time. Multiply a pixel by the report's `normalization_seconds` to recover seconds.
- `integration_time_map_<run_id>[_<cohort_tag>].fit`: seconds per pixel (`BUNIT='s'`), used for measurement and autocropping. Raw second values may saturate an image viewer's default 0-1 display; use the normalized map for viewing.

Both maps cover the **entire uncropped master**, independent of the crop percentage. After the master is complete, map support is reconciled to its detector footprint. Nonzero master pixels extending beyond measured coverage only within the configured feather/interpolation fringe are reported but remain at zero in the coverage map; larger mismatches prevent finalization. The master's celestial WCS cards are copied to both maps. Point-drizzle sampling holes are treated as interior detector footprint rather than gaps. Enable `Create auto-cropped master` to preserve that master and write a second master containing the largest axis-aligned rectangle entirely meeting the minimum integration time. `Crop depth (%)` sets that minimum relative to the median integration time of nonblank pixels, not the deepest panel overlap. The default 50% requires half that typical integration; lower values keep more field, while higher values reject more shallow coverage. This percentage is not the percentage of image area retained. Black corners and internal gaps are excluded, so an irregular footprint can still require sacrificing some usable field to form a clean rectangle. Run Verification warns when a crop retains less than 1% of the master area. Integration times are before per-pixel rejection and stacking weights, not effective weighted exposure or a direct noise measurement. If any registered frame lacks a finite positive `EXPTIME`, coverage and autocrop are skipped with a warning rather than guessing its contribution.

The maps use Siril's sequence placement data and maximize-framing canvas, and their pixel reduction is streamed through NumPy to avoid loading the full sequence at once. With multiple substacks, each substack map is placed on the final master registration canvas and summed before cropping. Crop reports record the reference integration time and cutoff in seconds, FITS-array bounds, and converted Siril selection coordinates. Existing crop-percentage profile values now refer to typical integration time rather than peak frame coverage. Previously generated count maps are not retroactively converted to integration-time maps; an already running process also keeps the code it loaded at startup.

After a completed run, the GUI's `Cropping Workbench` tab can load the quality report, select a single master or cohort master, preview alternate crop depths, open a larger visual popup with the crop rectangle overlaid on the full master and a stretched selected-region thumbnail, and create a new cropped master from the saved integration-time map. A preview retaining less than 1% of the master area is highlighted and requires explicit confirmation before Siril starts; crop depth and retained area are separate measurements. The workbench does not rerun stacking or alter the original master. The same operation is available to scripts with `--crop-workbench` and its master, coverage, percentage, and output arguments.

Set `Test frame count (0 = all)` to randomly sample a smaller number of eligible frames for a quick test. Only the sampled frames are moved and processed; unselected source files remain in place. Use `0` for a normal full run.

The experimental Sky quality keep filter is currently hidden from the GUI because Siril does not reliably provide the underlying per-frame quality metric in this workflow. The robust Adaptive quality filters remain the recommended active selection method.

Enable `Skip failed frames (log only)` when a single frame may fail during calibration or debayering. The stacker excludes malformed calibrated frames from later Siril sequence operations and records each skipped sequence filename, image number, stage, and reason in the run log and JSON report. The option is disabled by default; a run still fails normally when it is unchecked.

Enable `Auto substacks (detected Siril limit)` for large collections. The app checks the selected Siril version and calculates the smallest number of substacks that keeps every Siril sequence within its supported limit: 8,192 frames for Siril 1.4+ or 2,048 for older supported versions. Manual substack selection remains available when this option is disabled.

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

## Source Safety

The application records every moved source frame in `source_manifest.json` and restores it to its original relative path, including in debug mode. Frames rejected by frame-selection filters are moved into a mirrored `rejects` folder after their substack completes, so they can be inspected separately. When a new run starts, those files are restored to their original paths before input discovery. It refuses a preexisting `Lights_sorted` directory without cleaning that directory. Debug mode retains generated intermediates for investigation; move or remove those retained products before starting another run. Retries restart failed Siril substack commands from clean generated files, record the failed attempt, and never retry cancellation. Do not manually alter `Lights_sorted` or `rejects` while a run is active. Keeping an independent backup of irreplaceable acquisition data is still recommended.
After each completed substack, the application writes an atomic `run_checkpoint.json`. New checkpoints contain the complete effective processing-settings snapshot; Resume restores that canonical snapshot before building commands, so changed GUI controls or CLI defaults cannot silently alter an interrupted run. Legacy checkpoints without the snapshot retain strict configuration-hash matching. Resume also verifies the selected-file manifest and reusable generated artifacts with SHA-256 fingerprints when available, recovers incomplete staging, reuses completed substack products, and processes only the remaining groups. Per-cohort exports store cohort-local group assignments and completion state, so resume can skip already completed cohorts and continue the interrupted cohort without mixing staging. Use `--run-lock-status` to inspect an active/stale run lock and `--break-run-lock` only after confirming no process is running.
