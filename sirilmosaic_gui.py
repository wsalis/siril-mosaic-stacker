"""Dark graphical launcher for sirilmosaic.py."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import struct
import time
import tkinter as tk
import psutil
import numpy as np
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Thread
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from astro_dark_theme import DARK_BG, DARK_BORDER, DARK_FIELD, DARK_SURFACE, DARK_TEXT, configure_dark_theme
from sirilmosaic import (
    build_crop_plan,
    build_selection_filters,
    abandon_interrupted_run,
    break_run_lock as break_backend_run_lock,
    debayer_preflight_warnings,
    discover_light_files,
    estimate_peak_storage_bytes,
    create_run_bundle,
    write_experiment_record,
    export_html_run_report,
    delete_stale_artifacts,
    inspect_checkpoint,
    inspect_run_lock,
    REJECTS_DIRECTORY,
    SUPPORTED_FRAME_SUFFIXES,
    DRIZZLE_KERNELS,
    PIXEL_REJECTION_METHODS,
    REJECTION_FRACTION_METHODS,
    STACK_NORMALIZATION_METHODS,
    PLATE_SOLVE_CATALOGS,
    InterruptedRunRecoveryError,
    REGISTRATION_INTERPOLATIONS,
    REGISTRATION_TRANSFORMS,
    summarize_frame_cohorts,
    summarize_input_frames,
    read_fits_preview,
    verify_run as verify_run_report,
    replay_frame_ledger,
    replay_frame_records,
    read_frame_ledger,
    scan_input_integrity,
    stale_artifacts,
    visual_quality_check,
    verification_artifact_path,
)

REJECTION_THRESHOLD_DEFAULTS = {
    "none": (3.0, 3.0),
    "percentile": (0.2, 0.1),
    "sigma": (3.0, 3.0),
    "mad": (3.0, 3.0),
    "median": (3.0, 3.0),
    "linear": (3.0, 3.0),
    "winsorized": (3.0, 3.0),
    "generalized": (0.3, 0.05),
}

PROFILE_FIELDS = (
    "test_frame_count",
    "coverage_map",
    "auto_crop_master",
    "export_per_cohort",
    "cohort_group_camera",
    "cohort_group_filter",
    "cohort_group_exposure",
    "auto_crop_coverage_percent",
    "substacks",
    "auto_substacks",
    "mosaic_aware_star_count",
    "drizzle",
    "drizzle_scale",
    "pixel_fraction",
    "drizzle_kernel",
    "bayer_pattern",
    "bayer_orientation",
    "cosmetic_correction",
    "cosmetic_cold_sigma",
    "cosmetic_hot_sigma",
    "overlap_normalization",
    "stack_normalization",
    "plate_solve_order",
    "plate_solve_downscale",
    "plate_solve_radius",
    "plate_solve_limit_mag",
    "rbf_smoothing",
    "background_dither",
    "registration_transform",
    "registration_minpairs",
    "registration_maxstars",
    "registration_interpolation",
    "random_seed",
    "filter_background",
    "filter_stars",
    "filter_roundness",
    "filter_fwhm",
    "adaptive_quality_filtering",
    "quality_filter_sigma",
    "background_method",
    "background_samples",
    "background_tolerance",
    "weight",
    "feather",
    "rejection_low",
    "rejection_high",
    "rejection_method",
    "low_rejection_map",
    "high_rejection_map",
    "fast_normalization",
    "catalog",
    "memory",
    "cpus",
    "retries",
    "skip_failed_frames",
    "debug",
)

SIRIL_PROCESS_NAMES = {"siril", "siril.exe", "siril-cli", "siril-cli.exe"}
PROGRESS_MARKER = re.compile(
    r"^\[PROGRESS\]\s+(?P<start>[0-9.]+)\s+(?P<end>[0-9.]+)"
    r"(?:\s+files=(?P<file_start>\d+)-(?P<file_end>\d+)/(?P<file_total>\d+))?"
    r"\s+(?P<label>.+)$"
)
SIRIL_PROGRESS = re.compile(r"^\s*progress:\s*([0-9.]+)%", re.IGNORECASE)

HELP_TEXT = """SIRIL MOSAIC STACKER 1.4

QUICK START
1. Install Python 3.10+ with Tk support, Siril 1.3.6+, and the packages in requirements.txt.
2. Choose the folder containing your light frames. FITS (.fit, .fits, .fts) and XISF files are found recursively.
3. Choose a separate output folder and the Siril executable itself.
4. For a first run, leave Test frame count at 0, use one substack if the sequence is below Siril's limit, and keep drizzle disabled.
5. Choose Bayer pattern and row orientation when working with CFA data. Review the preflight count, storage estimate, and warnings.
6. Start the run. The source files are staged temporarily, processed, and restored to their original relative paths. Frames rejected by the frame-selection filters are moved to a mirrored `rejects` folder for inspection; starting a later run restores them first.
7. Inspect the master, the timestamped log, and the JSON quality report in the output folder.

SOURCE SAFETY
The application records every moved file in source_manifest.json. It refuses to use a non-empty Lights_sorted folder, restores sources after success or cancellation, and protects a pre-existing staging folder from rollback cleanup. Frame-selection rejects are preserved under a mirrored rejects folder and restored to their original paths before the next run. Do not edit Lights_sorted or rejects while a run is active. Debug mode retains generated intermediates but still restores source frames; remove those intermediates before another run.

RESUMABLE RUNS
Completed substacks are checkpointed in run_checkpoint.json. Use Resume Run after an interrupted run to validate the checkpoint, verify input fingerprints, recover incomplete staging, and reuse completed substack products. Per-cohort exports retain separate cohort assignments and completion state. The main action row provides checkpoint inspection, discard, lock status, and stale-lock recovery actions.

INPUTS AND GROUPING
Input files may be mixed FITS extensions or XISF. Test frame count randomly samples eligible files while leaving unselected files in place. Substacks split the selected files into randomized, non-empty groups before final integration. Auto substacks detects the selected Siril version and uses its supported sequence limit: 8,192 frames for Siril 1.4+ and 2,048 for older supported versions. More groups require more intermediate storage and currently disable coverage-map autocrop. Integrity scans perform deep FITS payload validation before a preview run.

CFA AND PREPROCESSING
Bayer pattern controls the CFA color layout; Auto reads headers where possible. CFA row order controls sensor orientation; explicit Top-down or Bottom-up can resolve missing or unreliable ROWORDER metadata. Optional cosmetic correction repairs hot/cold CFA pixels before debayering; higher sigma values are more conservative. Background extraction supports Off, RBF, and Quadratic modes with configurable samples and tolerance.

FRAME SELECTION
Percentage mode keeps the requested best whole-number percentage for each enabled criterion: background, star count, roundness, and FWHM. These criteria intersect, so setting every value to 90% does not guarantee 90% overall retention. Adaptive mode replaces all percentage fields with one positive k-sigma threshold. Disabled fields are ignored and do not block validation.
Mosaic-aware star count removes star count from global rejection when frames cover different sky regions; the other criteria remain active. The experimental sky-quality filter is unsupported and values other than 100 are rejected. The relative filter-retention score in reports is diagnostic only, not an objective sky-quality or Bortle measurement.

REGISTRATION AND INTEGRATION
Drizzle increases output sampling and needs substantially more time and disk space. The drizzle kernel controls how each input pixel is distributed; Lanczos3 remains the default. Droplet size controls Siril's pixfrac value from 0.1 to 1.0; 0.8 remains the default. Weighting controls registered-frame contribution. Feather blends mosaic borders. Pixel rejection removes unusual values using the selected method and low/high thresholds. Percentile and generalized rejection use thresholds from 0 to 1; the other methods use sigma-like thresholds. Low map and High map independently follow the rejection method by default, or can be set to Always or Never. Always requests the selected map when Siril supports it; Siril 1.4.4 does not generate rejection maps when rejection is set to none, and reports that direction unavailable. Siril generates rejection maps as a pair; an unselected direction is removed from the deliverable outputs. Stack normalization defaults to addscale, which combines additive background correction with scaling. Overlap normalization can reduce brightness differences between panels but may be slow. Fast normalization can reduce processing time for large sets. Master rejection is avoided for fewer than four substacks because it is statistically weak, but explicit map requests override that map-generation threshold when rejection is active.

ADVANCED SIRIL SETTINGS
The Advanced tab exposes selected Siril controls for plate solving, background extraction, registration, and reproducibility. Defaults preserve the normal workflow. Plate-solve downscale can speed up large images; higher SIP order can model more distortion but may fail on sparse fields. Registration minimum pairs and maximum stars are optional safeguards and are disabled when set to 0.

COVERAGE AND AUTOCROP
Write coverage map creates coverage_map_<run>.fit, a normalized 0-1 viewing map, and integration_time_map_<run>.fit, which stores seconds per pixel. Exposure is counted wherever registered frames contain finite nonzero signal. With multiple substacks, their maps are placed on the final master canvas and summed before cropping. Create auto-cropped master preserves the full master and writes the largest rectangle meeting Crop depth (%) relative to the median nonblank integration time. The default is 50%; this is a coverage threshold, not an area percentage. Positive EXPTIME is required for every registered frame. In per-cohort export mode, each cohort gets its own numbered coverage maps and cropped master.
After a completed run, use the Cropping Workbench tab to load its report, preview alternate crop depths, open a larger visual popup with a full-master overlay and stretched crop thumbnail, and create a new cropped master from the saved integration-time map without rerunning the stack.

REPORTS, RETRIES, AND CANCELLATION
The JSON report schema version 3 records effective filters, acquisition cohorts, stage counts, discarded-frame diagnostics, measured Siril filter metrics when sequence registration data is available, command durations, response tails, retry failures, and exact exposure telemetry when metadata and stack membership are known. Candidate filters without a matching metric are identified as unavailable rather than guessed. Integrated exposure is reported as unavailable when exact membership cannot be established; no proportional estimate is invented. Retries restart failed substack commands from cleaned generated products. Cancellation stops retrying, closes Siril, restores sources, and returns safely.

The Run Review tab's Verify Run action performs a read-only PASS/WARN/FAIL audit of the report, journal, manifest, full-frame ledger, artifacts, frame accounting, source restoration, and rejects. Open Frame Ledger, Export Ledger CSV, and Export Run Bundle provide evidence actions; table headers support sorting and the Filter/Clear Sort & Filter controls support review.
Threshold Lab predicts retention and exposure from alternate keep-best percentages without rerunning Siril. Preview Run performs the integrity scan first and includes its findings in the dry-run preview.

PROFILES AND RESOURCES
Profiles save processing controls and are stored under %APPDATA%\\Siril Mosaic Stacker. The last successfully loaded or saved profile is restored at startup; merely changing the dropdown does not change the remembered profile. Memory fraction, CPU count, retry count, and storage estimates help control resource use. Leave free space for converted, calibrated, registered, and rejection-map products.

ACQUISITION COHORTS
When Export one master per acquisition cohort is enabled, choose Camera model, Filter, and Exposure time to define the grouping key. Each selected field is included in the cohort ID; unselected fields may be mixed. Each cohort is processed independently and receives its own master, coverage maps, and optional cropped master.

PLATFORM NOTES
On Windows, select siril.exe. On macOS, select /Applications/Siril.app/Contents/MacOS/siril. For an ASI533MC with missing ROWORDER metadata, RGGB with Bottom-up is the known-good starting point documented by this project.
"""

HELP_SECTIONS = {
    "Basic": """PURPOSE AND FIRST RUN
Siril Mosaic Stacker prepares mixed FITS/XISF light frames, runs a reproducible Siril processing pipeline, and leaves behind a master plus evidence explaining how it was made. Choose a recursive input folder, a separate output folder, and the Siril executable. Review these Basic controls, use Preview Run for a read-only preflight, then Start Mosaic Stack to stage, process, and restore the source frames.

The output folder receives a master FITS, an atomic JSON quality report, a JSONL journal, a full-frame JSONL ledger, an input manifest, optional coverage/crop products, and an automatic verification artifact. Raw lights are not copied into the output folder. The progress bar combines explicit phase markers and Siril progress; elapsed time and ETA are estimates.

CAPTURE AND GROUPING
SUBSTACKS
Substacks is the number of randomized, non-empty groups processed before final integration. One group is simplest for a sequence below Siril's frame limit. More groups can reduce per-sequence memory and frame-count pressure, but require more intermediate storage and a final integration step.

AUTO SUBSTACKS
Auto substacks detects the selected Siril version and calculates the smallest group count that stays within its sequence limit: 8,192 frames for Siril 1.4+ and 2,048 for older supported versions. More groups currently make full-mosaic coverage autocrop less direct and increase temporary disk use.

BAYER PATTERN
Auto (header) asks Siril to use CFA metadata when available. RGGB, BGGR, GBRG, and GRGR are explicit sensor arrangements; use an explicit value when your files have unreliable or missing Bayer metadata.

CFA ROW ORDER
Auto reads the header when possible. Top-down and Bottom-up control the vertical sensor orientation used during debayering. A wrong row order produces incorrect color structure even when the Bayer pattern is right.

ACQUISITION COHORTS
Export one master per acquisition cohort separates frames using the selected Camera model, Filter, and Exposure time fields. Each cohort gets its own master/report entries and, when enabled, its own coverage maps and crop output.

PREPROCESSING
COSMETIC CORRECTION
Enable cosmetic correction runs CFA hot/cold pixel correction before calibration and debayering. Cold-pixel sigma detects unusually dark pixels; hot-pixel sigma detects unusually bright pixels. Higher sigma values are more conservative and alter fewer candidates.

DRIZZLE
Drizzle increases output sampling and can recover detail when the data and dither support it. It increases runtime, memory, and storage substantially. Drizzle scale controls enlargement, Droplet size is Siril's pixfrac, and Drizzle kernel controls how input pixels are distributed.

WHEN TO LEAVE DRIZZLE OFF
Use normal processing for a first validation run, large collections, or data without enough dither/coverage. Drizzle is not a general sharpening switch and can make sparse coverage more obvious.

FRAME SELECTION
PERCENTAGE FILTERS
Background, Star-count, Roundness, and FWHM filters request the best whole-number percentage for each enabled metric. The criteria intersect, so four 90% settings do not promise 90% total retention.

ADAPTIVE QUALITY FILTERS
Adaptive quality filters replace the percentage fields with one positive sigma threshold derived from the measured population. Smaller sigma is more selective; larger sigma is more permissive. The actual thresholds and measured values are recorded after Siril registration.

MOSAIC-AWARE STAR COUNT
This removes raw star count from global rejection. It is useful when different panels naturally contain different star densities. FWHM, roundness, and background remain active.

IMPORTANT LIMIT
Selection metrics are measured after preprocessing, background extraction, plate solving, and registration. Before a run, the GUI can validate inputs and storage, but it cannot know final FWHM/roundness/background values without a measurement pass.

INTEGRATION
WEIGHTING
wfwhm favors frames with sharper measured FWHM. noise favors noise estimates, nbstars uses star counts, and nbstack favors stack membership/count behavior. Weighting changes each contributing frame's influence; it is different from frame selection.

FEATHER
Feather blends edges where registered frames meet. Larger values soften transitions but can broaden boundary regions.

PIXEL REJECTION
Pixel rejection removes inconsistent pixel values during stacking; it does not reject whole frames. Low and High rejection values are interpreted according to the selected method. Percentile and generalized methods use fractions from 0 to 1; sigma-like methods use larger sigma-style values.

NORMALIZATION
Stack normalization controls brightness/background scaling before combination. addscale is the conservative default for additive background differences plus scaling. Normalize overlaps can improve panel-to-panel brightness but is expensive on large datasets. Fast normalization trades some processing detail for speed.

COVERAGE AND AUTOCROP
Write coverage map writes a normalized viewing map and an integration-time map in seconds per pixel. Create auto-cropped master preserves the full master and writes the largest axis-aligned rectangle meeting Crop depth (%). Crop depth is relative to typical positive integration time, not the percentage of area retained.

RESOURCES AND FAILURE POLICY
MEMORY FRACTION
The fraction of memory offered to Siril. Leave room for Python, the OS, and file caching; a value of 0.8 is not a guarantee that the complete run fits in RAM.

CPU COUNT
The logical processor count offered to Siril. More workers can speed conversion and registration but can increase contention, thermals, and memory pressure.

RETRIES
Maximum retries for a failed substack command. Each retry cleans generated products and reconnects to Siril. Cancellation is not retried.

KEEP INTERMEDIATE FILES
Debug mode retains temporary products for investigation. Source restoration and manifests still apply, but retained folders must be reviewed before another run.

SKIP FAILED FRAMES
Malformed calibrated/debayered frames are excluded from later sequence operations and recorded in the report instead of failing the whole run.""",
    "Advanced": """BACKGROUND EXTRACTION
Off skips gradient extraction. Linear and Quadratic model broad gradients with increasing model complexity. RBF uses smoothing control and can model more flexible backgrounds. Samples controls how many regions are evaluated; tolerance controls sample acceptance; Dither moves sample locations between frames.

PLATE SOLVING
SIP order controls distortion-model complexity. Downscale star detection reduces work on large frames. Search radius limits the sky search when supplied; Limit magnitude limits the faintest catalog stars considered. Plate catalog chooses the local Siril catalog.

REGISTRATION
Transform chooses shift, similarity, affine, or homography. More flexible transforms can model more distortion but need stronger star matches. Interpolation controls resampling quality. Minimum star pairs and Maximum stars are optional bounds; zero means Siril chooses.

REPRODUCIBILITY
Random seed controls randomized grouping and selection. The seed, configuration hash, selected-file manifest, command telemetry, and frame ledger allow a run to be audited and compared later.""",
    "Run Review": """SUMMARY
Run Review shows the loaded report's status, frame accounting, integrated exposure, cohort count, seed, configuration hash, artifact paths, and available rejection-map diagnostics. Rejection-map affected locations count finite positive map values; they are not counts of rejected input samples. Missing or unreadable maps are shown as unavailable rather than zero.

VERIFY RUN
Verify Run is read-only. It checks report status/schema, journal sequence integrity, manifest count, full-frame ledger identity/status/counts, master/map/crop artifacts and dimensions, source restoration, reject agreement, and substack accounting.

TABLE FILTERS
Filter uses a value dropdown for low-cardinality fields such as status, reason, and exposure. File and path fields keep the flexible case-insensitive Contains search. Clear Sort & Filter restores the complete table and default order.

EVIDENCE ACTIONS
Open Frame Ledger shows successful and rejected frame evidence. Export Ledger CSV writes a filtered view. Export Run Bundle creates a ZIP without raw lights. Run Visual QA opens read-only preview diagnostics in a secondary window. Cleanup Review is deliberately conservative and requires confirmation for approved temporary artifacts.

RECOVERY ACTIONS
Checkpoint Status, Discard Checkpoint, Abandon Run, Run Lock Status, and Break Run Lock are grouped on the main action row beside Start Mosaic Stack and Resume Run. Abandon Run restores staged sources, removes temporary staging, and deletes checkpoint metadata; reports and logs are retained.

SAFETY AND RECOVERY
Frames are moved into Lights_sorted with source_manifest.json records. Successful, failed, and cancelled runs restore sources; frame-selection rejects go to a mirrored rejects tree. Never edit Lights_sorted or rejects during a run. If the GUI or Python process is killed, a checkpoint may remain and frames may remain staged; Resume validates fingerprints from original, rejects, or staged locations and hands Siril away from the process directory before rebuilding staging. Stale pipe cleanup is scoped to the same workdir/output pair. Preserve the report, verification artifact, ledger, journal, manifest, logs, and HTML/ZIP bundle for a reproducible baseline.""",
    "Threshold Lab": """HISTORICAL LEDGER MODE
Threshold Lab works from the currently loaded frame ledger. Adjust Background, Roundness, FWHM, and Stars keep-best sliders to recompute predicted thresholds, selected frames, integrated exposure, cohort retention, unknown metrics, and coverage impact.

WHAT IT DOES NOT DO
It does not rerun Siril, remeasure frames, rewrite the report, or change the completed master. It is a fast decision aid for data that already has measured registration metrics.

INTERPRETING THE RESULT
Higher keep percentages are less selective. Compare the proposed delta to the current run, inspect cohort warnings, and remember that a ledger replay predicts selection from recorded metrics; it does not reproduce every downstream registration or stacking decision.""",
    "Coverage Inspector": """COVERAGE MAPS
Coverage Inspector previews the saved integration-time map, not just the normalized display map. Integration values are seconds per pixel and reflect registered frames with finite, nonzero signal and positive EXPTIME.

STATISTICS
The summary shows canvas dimensions, frames counted, maximum integration, normalization seconds, crop threshold, crop depth, crop dimensions, and crop area percentage.

LIMITS
Coverage is pre-pixel-rejection and pre-weighting exposure. Black regions can be intentional edge geometry; use the saved map and Visual QA together rather than treating a single percentage as image quality.""",
    "Cropping Workbench": """LOAD A REPORT
Selecting one run in Run History automatically loads its quality report here. You can also choose a quality report or use Latest. The workbench lists masters paired with their matching integration-time maps.

PREVIEW CROP
Select a Crop depth (%) to calculate the largest clean rectangle meeting that integration threshold. The original master is never modified.

VISUAL PREVIEW
Open Visual Preview shows the full master with the crop rectangle and a stretched selected-region thumbnail. This is useful for rejecting a numerically valid crop that cuts into a target or leaves an awkward composition.

CREATE CROPPED MASTER
Create Cropped Master runs Siril only for the crop operation and writes a new FITS output. It does not rerun conversion, calibration, registration, or stacking. A crop retaining less than 1% of the master area is highlighted and requires confirmation. Crop depth is an integration threshold, not the percentage of area retained.""",
    "Run History": """REPORT ARCHIVE
Run History scans the selected output folder for quality_report_*.json files and lists status, input, stacked/rejected counts, integrated hours, and verification-artifact availability.

SELECT AND COMPARE
Select one report to make it the active source for the analysis tabs; those views refresh automatically. Select exactly two reports and Compare Selected to see frame and exposure deltas. Export Experiment Record saves settings, runtime, output fingerprints, rejection diagnostics, a selected control run, and your conclusion. It requires matching selected-file content fingerprints and does not declare a winner.

EXPORT
Verify Selected performs the read-only audit. Export Selected Bundle creates the evidence ZIP. Export HTML Report creates a self-contained human-readable report with summary, verification, coverage, and settings tables.""",
    "Quality Explorer": """METRIC DISTRIBUTIONS
Choose FWHM, Roundness, Background, or Stars. The histogram uses values recorded in the frame ledger and displays numeric x-axis ticks, a distribution, and the recorded threshold when available.

READING THE PLOT
The summary states the recorded pass direction. FWHM and Background are generally lower-is-better; Roundness and Stars are generally higher-is-better. When ledger records contain different thresholds, the orange line is labeled as their median and the summary shows the count and range. A threshold is a selection boundary from the run's measured registration data, not a universal quality standard.

ZERO VALUES
Nonpositive metric values are treated as missing placeholders rather than real measurements. Siril or a sequence record can emit zero when a metric was unavailable for a frame; Quality Explorer excludes those values from the histogram and reports how many were ignored.

USE WITH FRAME INSPECTOR
Use Quality Explorer to see the population shape, then select individual records in Frame Inspector to understand borderline frames and rejection reasons.""",
    "Frame Inspector": """FRAME EVIDENCE
Frame Inspector lists every ledger record, including stacked and rejected frames. Select a row to see its source path, status, exposure, cohort, substack, stack membership, rejection reason, measured values, thresholds, and completed stages.

SOURCE PREVIEW
For rejected frames, the inspector first checks the mirrored rejects folder. For stacked frames, it checks the original relative workdir path. The preview is a readable stretch for inspection, not the original pixel display.

WHY A FRAME WAS REJECTED
The reason and metric statuses are evidence from the registration sequence. A candidate filter list may identify possible causes; it should not be read as proof when a measured metric is unavailable.""",
    "Cohort Balance": """COHORT TABLE
Cohort Balance groups ledger frames by the report's cohort field and shows total, stacked, rejected, unknown, retention percentage, and stacked exposure hours.

RETENTION
Retention is stacked frames divided by total ledger frames for that cohort. It is a balance diagnostic, not a quality score. A low-retention cohort may reflect a different sky region, focus distribution, exposure setup, or metadata population.

USE FOR MIXED DATA
Compare cohorts before choosing global thresholds. If one cohort loses disproportionate data, use Mosaic-aware star count, separate cohort export, or inspect that cohort's metrics directly.""",
    "Visual QA": """READ-ONLY DIAGNOSTICS
Open Visual QA from Run Review to create a preview-level diagnostic from the loaded master, coverage map, and crop. It never changes files.

CHECKS
It checks artifact readability, finite preview pixels, dynamic range, unusually dark edges, high black/clipped fraction, broad half-frame brightness differences, coverage-map holes, and crop dimensions.

INTERPRETATION
Warnings are prompts for inspection, not automatic rejection. A real mosaic can have dark edges, sparse coverage, gradients, or legitimate bright regions. Use the full-resolution master, coverage map, and astronomy judgment before changing processing settings.""",
    "Batch Queue": """ADDING JOBS
Add Current queues the current input/output pair. Add Folder chooses another input folder and defaults its output to a child named Siril Mosaic Output. Remove Selected and Clear Queue only affect jobs that are not running.

RUNNING THE QUEUE
Start Queue processes jobs sequentially with the current GUI settings. Each job gets its own cancellation marker and launcher log; the existing backend still enforces source safety, locks, checkpoints, and rollback.

IMPORTANT
The queue does not create per-job custom profiles or parallel runs. Review the shared settings before starting. If a job fails, later queued jobs can continue according to the queue status, and its log path identifies the failure.""",
}

HELP_SECTION_ORDER = (
    "Basic",
    "Advanced",
    "Batch Queue",
    "Run History",
    "Run Review",
    "Cohort Balance",
    "Quality Explorer",
    "Threshold Lab",
    "Frame Inspector",
    "Coverage Inspector",
    "Visual QA",
    "Cropping Workbench",
)


class RunProgressEstimator:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.percent = 0.0
        self.phase_start = 0.0
        self.phase_end = 0.0
        self.phase = "Waiting to start"
        self.file_current: int | None = None
        self.file_total: int | None = None
        self.file_start = 0
        self.file_end = 0
        self.started_at: float | None = None
        self.phase_started_at: float | None = None
        self.elapsed_seconds = 0.0
        self.estimated_total_seconds: float | None = None
        self.remaining_seconds: float | None = None
        self.phase_durations: dict[str, float] = {}

    def _tick(self) -> float:
        now = time.monotonic()
        if self.started_at is None:
            self.started_at = now
        self.elapsed_seconds = max(0.0, now - self.started_at)
        if self.percent > 0.5 and self.elapsed_seconds > 0:
            self.estimated_total_seconds = self.elapsed_seconds * 100 / self.percent
            self.remaining_seconds = max(0.0, self.estimated_total_seconds - self.elapsed_seconds)
        return now

    def consume(self, message: str) -> bool:
        marker = PROGRESS_MARKER.match(message)
        if marker:
            now = self._tick()
            if self.phase_started_at is not None:
                self.phase_durations[self.phase] = self.phase_durations.get(self.phase, 0.0) + max(
                    0.0, now - self.phase_started_at
                )
            self.phase_started_at = now
            requested_start = min(100.0, max(0.0, float(marker.group("start"))))
            requested_end = min(100.0, max(requested_start, float(marker.group("end"))))
            self.phase_start = max(self.percent, requested_start)
            self.phase_end = max(self.phase_start, requested_end)
            self.percent = self.phase_start
            if marker.group("file_total") is None:
                self.file_current = None
                self.file_total = None
                self.file_start = 0
                self.file_end = 0
            else:
                self.file_start = int(marker.group("file_start"))
                self.file_end = int(marker.group("file_end"))
                self.file_total = int(marker.group("file_total"))
                self.file_current = min(self.file_total, max(0, self.file_start))
            self.phase = marker.group("label")
            self._tick()
            return True
        native = SIRIL_PROGRESS.match(message)
        if native:
            command_percent = min(100.0, max(0.0, float(native.group(1))))
            estimate = self.phase_start + (
                (self.phase_end - self.phase_start) * command_percent / 100
            )
            self.percent = max(self.percent, estimate)
            if self.file_total is not None:
                estimated_files = self.file_start + (
                    (self.file_end - self.file_start) * command_percent / 100
                )
                self.file_current = min(self.file_total, max(0, round(estimated_files)))
            self._tick()
            return True
        return False

    def complete(self) -> None:
        now = self._tick()
        if self.phase_started_at is not None:
            self.phase_durations[self.phase] = self.phase_durations.get(self.phase, 0.0) + max(
                0.0, now - self.phase_started_at
            )
        self.percent = 100.0
        self.phase_start = 100.0
        self.phase_end = 100.0
        self.phase = "Complete"
        self.file_current = None
        self.file_total = None
        self.file_start = 0
        self.file_end = 0
        self.remaining_seconds = 0.0
        self.estimated_total_seconds = self.elapsed_seconds


def report_history_summary(report_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    integration = report.get("integration") or {}
    substacks = report.get("substacks") or []
    stacked = integration.get("stacked_frames")
    if stacked is None:
        stacked = sum(item.get("stack", {}).get("stacked_frames", 0) or 0 for item in substacks)
    rejected = sum(item.get("rejected_frames", 0) or 0 for item in substacks)
    return {
        "path": report_path,
        "run_id": report.get("run_id") or report_path.stem,
        "status": report.get("status", "unknown"),
        "updated_at": report.get("updated_at") or report.get("started_at", ""),
        "input_frames": report.get("input_frames", 0) or 0,
        "stacked_frames": stacked or 0,
        "rejected_frames": rejected,
        "integrated_hours": integration.get("integrated_hours"),
        "verification": "PASS" if report_path.with_name(
            f"verification_{report_path.stem.removeprefix('quality_report_')}.json"
        ).is_file() else "-",
    }


def quality_metric_summary(records: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    values = []
    thresholds = []
    comparisons = []
    ignored_nonpositive = 0
    for record in records:
        detail = record.get("filter_metrics", {}).get(metric, {})
        try:
            value = float(detail.get("value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= 0:
            ignored_nonpositive += 1
            continue
        values.append(value)
        try:
            threshold = float(detail.get("threshold"))
        except (TypeError, ValueError):
            threshold = None
        if threshold is not None and math.isfinite(threshold):
            thresholds.append(threshold)
        comparison = detail.get("comparison")
        if comparison in ("<=", ">="):
            comparisons.append(comparison)
    comparison = max(set(comparisons), key=comparisons.count) if comparisons else (
        ">=" if metric in ("roundness", "stars") else "<="
    )
    distinct_thresholds = sorted(set(thresholds))
    if not values:
        return {
            "metric": metric,
            "count": 0,
            "values": [],
            "threshold": None,
            "threshold_count": 0,
            "distinct_threshold_count": 0,
            "threshold_minimum": None,
            "threshold_maximum": None,
            "comparison": comparison,
            "ignored_nonpositive": ignored_nonpositive,
        }
    return {
        "metric": metric,
        "count": len(values),
        "values": values,
        "threshold": float(np.median(thresholds)) if thresholds else None,
        "threshold_count": len(thresholds),
        "distinct_threshold_count": len(distinct_thresholds),
        "threshold_minimum": min(thresholds) if thresholds else None,
        "threshold_maximum": max(thresholds) if thresholds else None,
        "comparison": comparison,
        "minimum": min(values),
        "maximum": max(values),
        "median": float(np.median(values)),
        "mean": float(np.mean(values)),
        "ignored_nonpositive": ignored_nonpositive,
    }


def crop_requires_confirmation(plan: dict[str, Any], minimum_area_percent: float = 1.0) -> bool:
    try:
        area_percent = float(plan.get('area_percent'))
    except (TypeError, ValueError):
        return True
    return not math.isfinite(area_percent) or area_percent < minimum_area_percent


def completion_summary(report: dict[str, Any], report_path: Path | None, log_path: Path | None) -> str:
    integration = report.get("integration") or {}
    input_frames = int(report.get("input_frames", 0) or 0)
    stacked_frames = integration.get("stacked_frames")
    if stacked_frames is None:
        stacked_frames = sum(
            int((item.get("stack") or {}).get("stacked_frames", 0) or 0)
            for item in (report.get("substacks") or [])
        )
    stacked_frames = int(stacked_frames or 0)
    lines = [
        "Mosaic stack completed successfully.",
        "",
        f"Frames: {stacked_frames} stacked of {input_frames} input "
        f"({max(input_frames - stacked_frames, 0)} not stacked)",
    ]
    integrated_hours = integration.get("integrated_hours")
    lines.append(
        f"Integrated data: {float(integrated_hours):.2f} hours"
        if integrated_hours is not None
        else "Integrated data: unavailable (see quality report)"
    )
    sky_condition = report.get("sky_condition") or {}
    if sky_condition.get("score") is not None:
        lines.append(
            f"Relative filter retention: {float(sky_condition['score']):.1f}/100 "
            f"({sky_condition.get('classification', 'Unknown')})"
        )
    lines.append(f"Verification: {report.get('verification_status', 'not recorded')}")

    masters = report.get("masters") or ([report.get("master")] if report.get("master") else [])
    coverages = report.get("coverages") or ([report.get("coverage")] if report.get("coverage") else [])
    master_paths = [str(item["path"]) for item in masters if item and item.get("path")]
    crop_paths = [str(item["cropped_master_path"]) for item in coverages if item and item.get("cropped_master_path")]
    lines.append(f"Masters: {len(master_paths)}")
    if len(master_paths) == 1:
        lines.append(f"Master: {master_paths[0]}")
    lines.append(f"Cropped masters: {len(crop_paths)}")
    if len(crop_paths) == 1:
        lines.append(f"Crop: {crop_paths[0]}")
    lines.extend((
        "",
        f"Quality report: {report_path}" if report_path is not None else "Quality report: unavailable",
        f"Log: {log_path}" if log_path is not None else "Log: unavailable",
        "Run Review is loaded with verification and artifact actions.",
    ))
    return "\n".join(lines)


def cohort_balance_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        cohort = record.get("cohort") or "Unassigned"
        row = groups.setdefault(cohort, {
            "cohort": cohort, "total": 0, "stacked": 0,
            "rejected": 0, "unknown": 0, "exposure_seconds": 0.0,
        })
        row["total"] += 1
        status = record.get("status")
        if status == "stacked":
            row["stacked"] += 1
        elif status == "rejected":
            row["rejected"] += 1
        else:
            row["unknown"] += 1
        try:
            exposure_value = record.get("exposure_seconds")
            exposure = float(exposure_value) if exposure_value is not None else 0.0
        except (TypeError, ValueError):
            exposure = 0.0
        if status == "stacked" and math.isfinite(exposure):
            row["exposure_seconds"] += exposure
    for row in groups.values():
        row["retention_percent"] = (
            row["stacked"] / row["total"] * 100 if row["total"] else 0.0
        )
    return sorted(groups.values(), key=lambda item: (-item["total"], item["cohort"]))


def coverage_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    coverage = report.get("coverage") or {}
    bounds = coverage.get("crop_bounds") or {}
    master = report.get("master") or {}
    master_path = master.get("path")
    width = coverage.get("width")
    height = coverage.get("height")
    crop_width = bounds.get("width")
    crop_height = bounds.get("height")
    crop_area_percent = None
    if (
        isinstance(crop_width, (int, float))
        and isinstance(crop_height, (int, float))
        and isinstance(width, (int, float))
        and isinstance(height, (int, float))
        and crop_width > 0
        and crop_height > 0
        and width > 0
        and height > 0
    ):
        crop_area_percent = float(crop_width) * float(crop_height) / (float(width) * float(height)) * 100
    return {
        "status": coverage.get("status", "available" if coverage else "unavailable"),
        "master_path": master_path,
        "width": width,
        "height": height,
        "frames_counted": coverage.get("frames_counted"),
        "maximum_integration_seconds": coverage.get("maximum_integration_seconds"),
        "normalization_seconds": coverage.get("normalization_seconds"),
        "crop_percent": coverage.get("crop_coverage_percent"),
        "crop_threshold": coverage.get("crop_threshold"),
        "crop_width": crop_width,
        "crop_height": crop_height,
        "crop_area_percent": crop_area_percent,
    }


def compare_run_summaries(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    def delta(key: str) -> float | int | None:
        left, right = first.get(key), second.get(key)
        if left is None or right is None:
            return None
        return right - left

    return {
        "first_run": first.get("run_id"),
        "second_run": second.get("run_id"),
        "input_frames_delta": delta("input_frames"),
        "stacked_frames_delta": delta("stacked_frames"),
        "rejected_frames_delta": delta("rejected_frames"),
        "integrated_hours_delta": delta("integrated_hours"),
    }


def resolve_frame_source_path(report_path: Path, report: dict[str, Any], record: dict[str, Any]) -> Path:
    rejects_directory = (report.get("settings") or {}).get("rejects_directory")
    if rejects_directory:
        workdir = Path(str(rejects_directory)).expanduser().resolve().parent
    else:
        workdir = report_path.parent.resolve().parent
    relative = Path(str(record.get("file", "")))
    candidates = []
    if record.get("status") == "rejected":
        candidates.append(workdir / REJECTS_DIRECTORY / relative)
    candidates.append(workdir / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if candidates else workdir / relative


def terminate_siril_descendants(parent_pid: int) -> int:
    try:
        descendants = psutil.Process(parent_pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return 0
    targets = []
    for process in descendants:
        try:
            if process.name().lower() in SIRIL_PROCESS_NAMES:
                targets.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    for process in targets:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    return len(targets)


def completed_verification_failure(report_path: Path | None) -> str | None:
    if report_path is None or not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    verification_status = report.get('verification_status')
    if report.get('status') == 'complete' and verification_status in {'FAIL', 'ERROR'}:
        return verification_status
    return None


class SirilMosaicApp:
    def __init__(self, root: tk.Tk, profile_path: Path | None = None) -> None:
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.log_path: Path | None = None
        self.cancel_path: Path | None = None
        self.quality_report_path: Path | None = None
        self.review_summary: tk.Text | None = None
        self.review_tree: ttk.Treeview | None = None
        self.history_tree: ttk.Treeview | None = None
        self.history_summary: tk.Text | None = None
        self.history_records: dict[str, dict[str, Any]] = {}
        self.history_item_paths: dict[str, Path] = {}
        self.history_compare_text: tk.Text | None = None
        self.quality_metric = tk.StringVar(value="fwhm")
        self.threshold_vars: dict[str, tk.DoubleVar] = {}
        self.threshold_value_labels: dict[str, ttk.Label] = {}
        self.threshold_summary: tk.Text | None = None
        self.threshold_records: list[dict[str, Any]] = []
        self._threshold_ledger_path: Path | None = None
        self.threshold_update_job: str | None = None
        self.quality_canvas: tk.Canvas | None = None
        self.quality_summary_data: dict[str, Any] | None = None
        self.quality_summary: tk.Text | None = None
        self.quality_records: list[dict[str, Any]] = []
        self.cohort_tree: ttk.Treeview | None = None
        self.cohort_canvas: tk.Canvas | None = None
        self.coverage_summary: tk.Text | None = None
        self.coverage_canvas: tk.Canvas | None = None
        self.coverage_photo: tk.PhotoImage | None = None
        self.visual_qa_summary: tk.Text | None = None
        self.visual_qa_canvas: tk.Canvas | None = None
        self.visual_qa_photo: tk.PhotoImage | None = None
        self.visual_qa_window: tk.Toplevel | None = None
        self.batch_tree: ttk.Treeview | None = None
        self.batch_jobs: list[dict[str, Any]] = []
        self.batch_running = False
        self.frame_inspector_tree: ttk.Treeview | None = None
        self.frame_inspector_summary: tk.Text | None = None
        self.frame_inspector_canvas: tk.Canvas | None = None
        self.frame_inspector_photo: tk.PhotoImage | None = None
        self.frame_inspector_records: list[dict[str, Any]] = []
        self.frame_inspector_report: tuple[Path, dict[str, Any]] | None = None
        self.verification_status = tk.StringVar(value="Not verified")
        self.analysis_dialog: tk.Toplevel | None = None
        self.analysis_text: tk.Text | None = None
        self.integrity_scan_running = False
        self.ledger_rows: list[dict[str, Any]] = []
        self.ledger_tree: ttk.Treeview | None = None
        self.ledger_status_filter = tk.StringVar(value="All")
        self.ledger_metric_filter = tk.StringVar(value="All")
        self.ledger_search = tk.StringVar(value="")
        self.crop_report = tk.StringVar(value="")
        self.crop_artifact = tk.StringVar(value="")
        self.crop_percent = tk.IntVar(value=50)
        self.crop_summary: tk.Text | None = None
        self.crop_full_rgb: Any | None = None
        self.crop_region_rgb: Any | None = None
        self.crop_visual_button: ttk.Button | None = None
        self.crop_popup_photos: dict[int, tuple[tk.PhotoImage, tk.PhotoImage]] = {}
        self.crop_artifact_combo: ttk.Combobox | None = None
        self.crop_preview_button: ttk.Button | None = None
        self.crop_create_button: ttk.Button | None = None
        self.crop_artifacts: list[dict[str, Any]] = []
        self.crop_plan: dict[str, Any] | None = None
        self.crop_operation = False
        self.crop_log_path: Path | None = None
        self.crop_output_path: Path | None = None
        self.crop_preview_generation = 0
        self.crop_preview_in_progress = False
        self.running = False
        self.cancelling = False
        self.progress_estimator = RunProgressEstimator()
        self.events: Queue[tuple[str, Any]] = Queue(maxsize=5000)
        self.dropped_log_events = 0
        self._tree_sort_state: dict[str, tuple[str, bool]] = {}
        self._tree_filter_snapshot: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
        self._tree_default_rows: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
        self._tooltip_popup: tk.Toplevel | None = None
        self._tooltip_after: str | None = None
        self._tooltip_widget: tk.Misc | None = None

        root.title("Siril Mosaic Stacker")
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        root.geometry(
            f"{min(1120, max(800, screen_width - 40))}x"
            f"{min(850, max(520, screen_height - 80))}"
        )
        root.minsize(760, 520)
        configure_dark_theme(root)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        root.rowconfigure(4, weight=0)
        root.protocol("WM_DELETE_WINDOW", self.close_window)

        profile_root = Path(os.environ.get("APPDATA", Path.home())) / "Siril Mosaic Stacker"
        self.profile_path = profile_path or (profile_root / "profiles.json")
        self.last_paths_path = self.profile_path.with_name("last_paths.json")
        last_paths = self._read_last_paths()

        self.workdir = tk.StringVar(value=last_paths.get("workdir", r"G:\Rosette"))
        self.siril_exe = tk.StringVar(value=r"C:\Program Files\Siril\bin\siril.exe")
        self.test_frame_count = tk.IntVar(value=0)
        self.coverage_map = tk.BooleanVar(value=False)
        self.auto_crop_master = tk.BooleanVar(value=False)
        self.export_per_cohort = tk.BooleanVar(value=False)
        self.cohort_group_camera = tk.BooleanVar(value=True)
        self.cohort_group_filter = tk.BooleanVar(value=True)
        self.cohort_group_exposure = tk.BooleanVar(value=True)
        self.auto_crop_coverage_percent = tk.IntVar(value=50)
        self.substacks = tk.IntVar(value=2)
        self.auto_substacks = tk.BooleanVar(value=False)
        self.mosaic_aware_star_count = tk.BooleanVar(value=False)
        self.drizzle = tk.BooleanVar(value=True)
        self.drizzle_scale = tk.DoubleVar(value=2.0)
        self.pixel_fraction = tk.DoubleVar(value=0.8)
        self.drizzle_kernel = tk.StringVar(value="lanczos3")
        self.bayer_pattern = tk.StringVar(value=last_paths.get("bayer_pattern", "Auto (header)"))
        self.bayer_orientation = tk.StringVar(
            value=last_paths.get("bayer_orientation", "Bottom-up")
        )
        self.cosmetic_correction = tk.BooleanVar(value=True)
        self.cosmetic_cold_sigma = tk.DoubleVar(value=50.0)
        self.cosmetic_hot_sigma = tk.DoubleVar(value=3.0)
        self.overlap_normalization = tk.BooleanVar(value=True)
        self.stack_normalization = tk.StringVar(value="addscale")
        self.plate_solve_order = tk.IntVar(value=3)
        self.plate_solve_downscale = tk.BooleanVar(value=False)
        self.plate_solve_radius = tk.StringVar(value="")
        self.plate_solve_limit_mag = tk.StringVar(value="")
        self.rbf_smoothing = tk.DoubleVar(value=0.5)
        self.background_dither = tk.BooleanVar(value=True)
        self.registration_transform = tk.StringVar(value="homography")
        self.registration_minpairs = tk.IntVar(value=0)
        self.registration_maxstars = tk.IntVar(value=0)
        self.registration_interpolation = tk.StringVar(value="lanczos4")
        self.random_seed = tk.StringVar(value="")
        self.filter_background = tk.IntVar(value=97)
        self.filter_stars = tk.IntVar(value=97)
        self.filter_roundness = tk.IntVar(value=97)
        self.filter_fwhm = tk.IntVar(value=97)
        self.adaptive_quality_filtering = tk.BooleanVar(value=False)
        self.quality_filter_sigma = tk.DoubleVar(value=3.0)
        self.background_method = tk.StringVar(value="Quadratic")
        self.background_samples = tk.IntVar(value=20)
        self.background_tolerance = tk.DoubleVar(value=1.0)
        self.weight = tk.StringVar(value="wfwhm")
        self.feather = tk.IntVar(value=20)
        self.rejection_low = tk.DoubleVar(value=3.0)
        self.rejection_high = tk.DoubleVar(value=3.0)
        self.rejection_method = tk.StringVar(value="linear")
        self.low_rejection_map = tk.StringVar(value="Follow method")
        self.high_rejection_map = tk.StringVar(value="Follow method")
        self.fast_normalization = tk.BooleanVar(value=False)
        self.catalog = tk.StringVar(value="localgaia")
        self.memory = tk.DoubleVar(value=0.8)
        self.cpus = tk.IntVar(value=min(28, os.cpu_count() or 1))
        self.retries = tk.IntVar(value=5)
        self.skip_failed_frames = tk.BooleanVar(value=False)
        self.debug = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Choose a folder; FITS and XISF frames in its subfolders are included.")
        self.progress_text = tk.StringVar(value="0.0% estimated - Waiting to start")
        self.drizzle_widgets: list[tk.Widget] = []
        self.rejection_widgets: list[ttk.Spinbox] = []
        self.cosmetic_widgets: list[ttk.Spinbox] = []
        self.quality_percent_widgets: list[ttk.Spinbox] = []
        self.quality_sigma_widgets: list[ttk.Spinbox] = []
        self.background_widgets: list[ttk.Spinbox] = []
        self.substacks_widget: ttk.Spinbox | None = None
        self.star_count_widget: ttk.Spinbox | None = None
        self.coverage_widgets: list[tk.Widget] = []
        self.cohort_group_widgets: list[tk.Widget] = []
        self.rbf_smoothing_widget: ttk.Spinbox | None = None
        self.background_dither_widget: tk.Widget | None = None
        self.fast_normalization_widget: tk.Widget | None = None
        self.output_dir = tk.StringVar(
            value=last_paths.get("output_dir", r"G:\Rosette\Siril Mosaic Output")
        )
        self.profile_name = tk.StringVar(value=last_paths.get("profile_name", ""))
        self.profiles = self._read_profiles()
        self.last_used_profile = self.profile_name.get() if self.profile_name.get() in self.profiles else ""
        self.profile_name.set(self.last_used_profile)

        self._build_profiles()
        self._build_paths()
        self._build_settings()
        self.update_drizzle_controls()
        self.update_rejection_controls()
        self.update_substack_controls()
        self.update_cosmetic_controls()
        self.update_quality_filter_controls()
        self.update_coverage_controls()
        self.update_cohort_group_controls()
        self.update_background_controls()
        self._build_actions()
        self._build_log()
        if self.profile_name.get() in self.profiles:
            self.load_profile(notify=False)
        self.install_tooltips()
        self.install_treeview_sorting()
        root.after(100, self.poll_events)

    TOOLTIP_TEXT = {
        "Run profile": "Saved processing presets and recent paths. Loading a profile changes the controls; saving preserves the current choices for later runs.",
        "Profile": "Choose a saved processing profile. Profiles store processing controls, not raw light frames or generated outputs.",
        "Load": "Load the selected profile into the current controls, replacing the current unsaved choices.",
        "Save": "Save the current processing controls under the selected profile name.",
        "Delete": "Delete the selected saved profile after confirmation. This does not delete any run outputs.",
        "Locations": "Input, output, and Siril executable paths used by the next run.",
        "Choose...": "Browse for the path associated with this row.",
        "Basic": "Common capture, selection, integration, coverage, and resource controls.",
        "Advanced": "Less frequently changed background, plate-solving, registration, and reproducibility controls.",
        "Run Review": "Inspect a completed report, discarded frames, verification results, ledgers, bundles, and table filters.",
        "Threshold Lab": "Explore alternate keep-best targets against a recorded frame ledger without invoking Siril.",
        "Coverage Inspector": "Preview the saved integration-time map and inspect canvas, crop, and exposure statistics.",
        "Visual QA": "Open read-only preview diagnostics from Run Review for edges, clipping, broad gradients, coverage holes, and crop artifacts.",
        "Cropping Workbench": "Preview and create alternate crops from an existing master without rerunning the stack.",
        "Run History": "Browse completed reports, compare runs, verify evidence, and export bundles or HTML reports.",
        "Quality Explorer": "Plot measured per-frame quality metrics and compare their distributions with recorded thresholds.",
        "Frame Inspector": "Inspect an individual ledger frame, its source path, metrics, stages, rejection reason, and preview.",
        "Cohort Balance": "Compare frame retention and integrated exposure across acquisition cohorts.",
        "Batch Queue": "Queue multiple input folders and process them sequentially with the current settings.",
        "Run summary": "High-level accounting and artifact paths for the loaded completed run.",
        "Verify Run": "Perform a read-only consistency audit of the report, journal, manifest, ledger, artifacts, sources, and rejects.",
        "Open Frame Ledger": "Open all frame evidence in a filterable table, including successful and rejected frames.",
        "Export Ledger CSV": "Write the currently filtered frame ledger view to a CSV file for spreadsheet or analysis work.",
        "Export Run Bundle": "Create a ZIP evidence package containing reports and diagnostics, excluding raw light frames.",
        "Checkpoint Status": "Inspect whether an interrupted-run checkpoint is valid and which artifacts it references.",
        "Discard Checkpoint": "Remove only the saved checkpoint metadata after confirmation; staged source files are left untouched.",
        "Abandon Run": "Restore staged source files, remove temporary staging and checkpoint metadata, and leave reports/logs for audit before starting fresh.",
        "Run Lock Status": "Inspect the process and project recorded in the run lock that prevents concurrent mutation.",
        "Break Run Lock": "Remove a stale run lock after confirmation. Use only after verifying no stacker or Siril process is active.",
        "Cleanup Review": "Review temporary processing artifacts and logs. Nothing is deleted automatically; staged sources and rejects are protected.",
        "Keep-best targets": "Target percentages for the interactive threshold replay. Higher values keep more frames and are less selective.",
        "Refresh History": "Rescan the selected output folder for completed quality reports.",
        "Compare Selected": "Compare exactly two selected reports by frame counts and integrated exposure.",
        "Export Experiment Record": "Save a reproducible comparison of selected completed runs with identical selected input-file fingerprints.",
        "Verify Selected": "Run the read-only verifier against the selected history report.",
        "Export Selected Bundle": "Export the selected report's evidence bundle without raw lights.",
        "Export HTML Report": "Export the selected run as a self-contained HTML summary for sharing or archiving.",
        "Run Visual QA": "Run the preview-level visual diagnostics for the loaded report.",
        "Run Visual QA...": "Open read-only preview diagnostics in a secondary window for the loaded report.",
        "Add Current": "Add the current input/output folder pair to the sequential batch queue.",
        "Add Folder": "Choose another input folder and add it to the batch queue with a default output folder.",
        "Remove Selected": "Remove selected queued jobs that are not currently running.",
        "Clear Queue": "Remove every queued batch job. This is disabled while the queue is active.",
        "Start Queue": "Run queued folders one at a time using the current controls; each job gets its own log and report.",
        "Preview Crop": "Calculate crop geometry from the saved integration-time map without writing a new master.",
        "Create Cropped Master": "Run the selected crop against the existing master and write a new output file.",
        "Open Visual Preview": "Show the full master with the proposed crop rectangle and a selected-region preview.",
        "Browse...": "Choose a completed quality report for the Cropping Workbench.",
        "Latest": "Load the newest quality report found in the selected output folder.",
        "Master artifact": "Select which master and matching integration-time map to inspect or crop.",
        "Replay": "Run the current threshold replay settings against the loaded ledger.",
        "Close": "Close this analysis or preview window without changing run artifacts.",
        "Start Mosaic Stack": "Validate the current settings, stage source frames, run Siril, write evidence, and restore sources.",
        "Resume Run": "Continue an interrupted run from its checkpoint after validating staged files and configuration.",
        "Preview Run": "Run preflight and show the planned processing command without moving frames or starting Siril.",
        "Cancel Run": "Request safe cancellation. The backend finishes the current safe point and restores source frames.",
        "Help": "Open the project help and safety guidance.",
        "Filter": "Filter the current table by a value dropdown for finite fields or case-insensitive Contains text for file/path fields and all-column searches. Filtering does not modify the underlying report.",
        "Clear Sort & Filter": "Restore all rows and clear the active column sort for this table.",
        "Input folder (recursive)": "Root folder containing light frames. FITS and XISF files are discovered recursively, while generated folders and output artifacts are excluded.",
        "Output folder": "Separate destination for masters, maps, reports, journals, ledgers, checkpoints, and bundles. Keeping it outside the input tree reduces accidental reprocessing.",
        "Siril executable": "Path to the Siril executable used by pySiril. The launcher validates this path before starting a run.",
        "Test frame count (0 = all)": "Use a random subset for a disposable smoke run. Zero processes every eligible frame; unselected source files remain in place.",
        "Substacks": "Number of randomized non-empty groups processed before final integration. More groups reduce per-sequence load but increase temporary storage and processing overhead.",
        "Bayer pattern": "CFA color arrangement used when debayering. Auto reads the FITS/XISF header; choose an explicit pattern when metadata is missing or unreliable.",
        "CFA row order": "Sensor row orientation used by debayering. Auto reads metadata; Top-down or Bottom-up can correct missing or incorrect ROWORDER headers.",
        "Auto substacks (detected Siril limit)": "Detects the selected Siril version and chooses the smallest number of groups that stays within its supported sequence limit: 8,192 frames for Siril 1.4+ or 2,048 for older supported versions.",
        "Export one master per acquisition cohort": "Writes one master per selected acquisition grouping. Choose Camera model, Filter, and Exposure time on the same line.",
        "Camera model": "Groups cohort exports by the camera model recorded in frame metadata.",
        "Filter": "Groups cohort exports by the recorded optical filter when cohort export is enabled.",
        "Exposure time": "Groups cohort exports by the recorded exposure time in seconds.",
        "Enable cosmetic correction": "Runs CFA cosmetic correction before calibration/debayering to reduce hot and cold pixel defects. It can add processing time and should be validated visually.",
        "Cold-pixel sigma": "Detection threshold for unusually dark CFA pixels. Higher values are more conservative and correct fewer pixels. A value of 50.0 effectively disables cold-pixel correction.",
        "Hot-pixel sigma": "Detection threshold for unusually bright CFA pixels. Lower values correct more candidates but can risk altering real signal. A value of 50.0 effectively disables hot-pixel correction.",
        "Enable drizzle": "Enables Siril drizzle processing for larger output sampling. It substantially increases storage, memory, and runtime requirements.",
        "Drizzle scale": "Output enlargement factor for drizzle. Larger values increase resolution and resource use.",
        "Droplet size": "Drizzle pixel fraction. Smaller values can preserve detail but may increase sparsity and sensitivity to coverage gaps.",
        "Drizzle kernel": "Drizzle interpolation kernel controlling how input pixels contribute to the output canvas.",
        "Background filter (%)": "Percentage-mode background quality target. The best requested percentage is retained before criteria are intersected.",
        "Star-count filter (%)": "Percentage-mode star-count target. Disable it with Mosaic-aware star count when panels naturally contain different star densities.",
        "Roundness filter (%)": "Percentage-mode roundness target. Higher retention keeps more frames but permits poorer stellar shapes.",
        "FWHM filter (%)": "Percentage-mode FWHM target. Lower FWHM generally means tighter stars; the best requested percentage is retained.",
        "Adaptive quality filters": "Replaces percentage targets with a positive sigma threshold derived from the measured frame population.",
        "Quality filter sigma": "Sigma distance used by adaptive quality filtering. Larger values are more permissive; smaller values are more selective.",
        "Mosaic-aware star count": "Stops using raw star count as a global rejection criterion, avoiding unfair rejection of naturally sparse mosaic panels.",
        "Weighting": "Frame weighting method used during stacking. wfwhm favors sharper frames; noise, star count, and stack-count methods emphasize different evidence.",
        "Feather": "Blending width used around integration boundaries. Larger values soften seams but can broaden edge transitions.",
        "Pixel rejection": "Per-pixel outlier rejection algorithm used during stacking. This rejects inconsistent pixel values, not whole frames.",
        "Low rejection": "Lower rejection parameter passed to the selected pixel rejection algorithm.",
        "High rejection": "Upper rejection parameter passed to the selected pixel rejection algorithm.",
        "Low map": "Controls whether Siril's low-rejection map is requested and retained. Follow method follows the method's normal behavior; Always requests it when supported; Never omits it. Siril cannot create rejection maps when Pixel rejection is none, so that selection is reported unavailable. This does not change rejection thresholds.",
        "High map": "Controls whether Siril's high-rejection map is requested and retained. Follow method follows the method's normal behavior; Always requests it when supported; Never omits it. Siril cannot create rejection maps when Pixel rejection is none, so that selection is reported unavailable. This does not change rejection thresholds.",
        "Normalize overlaps": "Normalizes brightness differences in overlapping mosaic regions. It can improve panel continuity but is expensive for large collections.",
        "Stack normalization": "Siril normalization mode used before combining frames. The choice affects brightness scaling and background behavior.",
        "Fast normalization": "Uses Siril's faster normalization path when available. It can reduce runtime with a possible quality tradeoff.",
        "Write coverage map": "Writes normalized coverage and integration-time FITS maps across the full mosaic canvas.",
        "Create auto-cropped master": "Creates a second master containing the largest rectangular region meeting the requested integration-depth threshold. The original master is preserved.",
        "Crop depth (%)": "Minimum integration depth relative to the median positive integration time. This is not the percentage of image area retained.",
        "Background extraction": "Selects the gradient-removal method applied after calibration/debayering and before plate solving.",
        "Plate catalog": "Star catalog used by Siril plate solving. Availability depends on the local Siril installation and catalog configuration.",
        "Background samples": "Number of sample regions used by background extraction. More samples can model broad gradients better but increase work.",
        "Background tolerance": "Tolerance controlling background sample acceptance. Smaller values are stricter about sample consistency.",
        "Memory fraction": "Fraction of available memory given to Siril for stacking. Leave headroom for Python, the OS, and file caching.",
        "CPU count": "Logical processor count offered to Siril. More workers can speed processing but may increase contention and memory pressure.",
        "Retries": "Maximum retry count for a failed substack command. Each retry cleans generated intermediates before reconnecting to Siril.",
        "Keep intermediate files": "Debug mode that retains temporary staging and products for investigation. Source frames are still restored, but cleanup must be handled before another run.",
        "Skip failed frames (log only)": "Excludes malformed calibrated/debayered frames from later sequence operations and records them instead of failing the whole run.",
        "RBF smoothing": "Smoothing control for RBF background extraction. Higher values produce a smoother model with less local structure.",
        "Dither background samples": "Moves background sample positions between frames to reduce persistent sampling artifacts.",
        "SIP order": "Polynomial order used by plate solving for the SIP distortion model. Higher orders model more complex distortion but can be less stable.",
        "Downscale star detection": "Downscales images during star detection to reduce plate-solving work on large frames.",
        "Search radius": "Optional plate-solving search radius in degrees. Blank lets Siril determine the search behavior.",
        "Limit magnitude": "Optional faintest catalog magnitude considered during plate solving. Blank uses Siril's default.",
        "Transform": "Registration transform model: shift, similarity, affine, or homography. More flexible models handle distortion but need stronger evidence.",
        "Interpolation": "Pixel interpolation used during registration. Higher-quality kernels preserve detail at additional computational cost.",
        "Minimum star pairs (0 = auto)": "Minimum matched star pairs required for registration. Zero lets Siril choose automatically.",
        "Maximum stars (0 = auto)": "Maximum stars used by registration. Zero lets Siril choose automatically; limiting the count can reduce work.",
        "Random seed (blank = generate)": "Seed controlling randomized frame grouping and selection. Keeping it records a reproducible run; blank generates a new seed.",
    }

    def add_tooltip(self, widget: tk.Misc, text: str) -> None:
        def hide(_event=None) -> None:
            self._hide_tooltip()

        def schedule(_event=None) -> None:
            self._hide_tooltip()
            self._tooltip_widget = widget
            self._tooltip_after = self.root.after(
                550,
                lambda: self._show_tooltip(widget, text),
            )

        widget.bind("<Enter>", schedule, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress>", hide, add="+")

    def _hide_tooltip(self) -> None:
        if self._tooltip_after is not None:
            try:
                self.root.after_cancel(self._tooltip_after)
            except tk.TclError:
                pass
            self._tooltip_after = None
        if self._tooltip_popup is not None:
            try:
                self._tooltip_popup.destroy()
            except tk.TclError:
                pass
            self._tooltip_popup = None
        self._tooltip_widget = None

    def _show_tooltip(self, widget: tk.Misc, text: str) -> None:
        self._tooltip_after = None
        if self._tooltip_widget is not widget or not widget.winfo_exists():
            return
        self._hide_tooltip()
        self._tooltip_widget = widget
        popup = tk.Toplevel(self.root)
        popup.wm_overrideredirect(True)
        popup.attributes("-topmost", True)
        label = tk.Label(
            popup,
            text=text,
            justify="left",
            wraplength=360,
            background="#2b303b",
            foreground="#f0f1f3",
            relief="solid",
            borderwidth=1,
            padx=9,
            pady=7,
        )
        label.pack()
        popup.update_idletasks()
        pointer_x = self.root.winfo_pointerx()
        pointer_y = self.root.winfo_pointery()
        popup_width = popup.winfo_reqwidth()
        popup_height = popup.winfo_reqheight()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = pointer_x + 14
        y = pointer_y + 18
        if x + popup_width > screen_width - 8:
            x = max(8, pointer_x - popup_width - 14)
        if y + popup_height > screen_height - 8:
            y = max(8, pointer_y - popup_height - 14)
        popup.geometry(f"+{x}+{y}")
        self._tooltip_popup = popup

    def install_tooltips(self) -> None:
        for widget in self.root.winfo_children():
            self._install_widget_tooltips(widget)

    def _install_widget_tooltips(self, widget: tk.Misc) -> None:
        text = ""
        try:
            text = str(widget.cget("text"))
        except (tk.TclError, TypeError):
            pass
        if text in self.TOOLTIP_TEXT:
            self.add_tooltip(widget, self.TOOLTIP_TEXT[text])
        for child in widget.winfo_children():
            self._install_widget_tooltips(child)

    def install_treeview_sorting(self) -> None:
        self._install_treeview_sorting(self.root)

    def _install_treeview_sorting(self, widget: tk.Misc) -> None:
        if isinstance(widget, ttk.Treeview):
            self._make_treeview_sortable(widget)
        for child in widget.winfo_children():
            self._install_treeview_sorting(child)

    def _make_treeview_sortable(self, tree: ttk.Treeview) -> None:
        key = str(tree)
        if key not in self._tree_default_rows:
            rows = self._treeview_rows(tree)
            if rows:
                self._tree_default_rows[key] = rows
        for column in tree["columns"]:
            tree.heading(
                column,
                command=lambda column=column, tree=tree: self._sort_treeview(tree, column),
            )

    def _treeview_filter_controls(self, parent: ttk.Frame, tree: ttk.Treeview | None) -> ttk.Frame:
        controls = ttk.Frame(parent)
        if tree is None:
            return controls
        ttk.Button(controls, text="Filter", command=lambda: self._open_treeview_filter(tree)).pack(side="left")
        ttk.Button(controls, text="Clear Sort & Filter", command=lambda: self._clear_treeview_sort_filter(tree)).pack(side="left", padx=(6, 0))
        return controls

    def _treeview_rows(self, tree: ttk.Treeview) -> list[tuple[str, tuple[str, ...]]]:
        return [
            (item, tuple(str(value) for value in tree.item(item, "values")))
            for item in tree.get_children("")
        ]

    @staticmethod
    def _finite_treeview_filter_values(
        source_rows: list[tuple[str, tuple[str, ...]]],
        column_index: int,
        column_name: str,
    ) -> list[str]:
        name = column_name.casefold()
        if any(token in name for token in ("file", "frame", "path", "folder", "directory", "workdir", "output", "input", "report", "log")):
            return []
        values = {row_values[column_index] for _item, row_values in source_rows if row_values[column_index]}
        if not values or len(values) > 20:
            return []
        return sorted(values, key=lambda value: (value.casefold(), value))

    def _open_treeview_filter(self, tree: ttk.Treeview) -> None:
        key = str(tree)
        source_rows = (
            self._tree_filter_snapshot.get(key)
            or self._tree_default_rows.get(key)
            or self._treeview_rows(tree)
        )
        if not source_rows:
            messagebox.showinfo("Filter", "This table has no rows to filter.")
            return
        columns = list(tree["columns"])
        heading_names = [str(tree.heading(column, "text") or column) for column in columns]
        dialog = tk.Toplevel(self.root)
        dialog.title("Filter table")
        dialog.configure(background=DARK_BG)
        dialog.geometry("430x150")
        dialog.minsize(360, 140)
        dialog.transient(self.root)
        dialog.columnconfigure(1, weight=1)
        ttk.Label(dialog, text="Column").grid(row=0, column=0, padx=12, pady=(14, 6), sticky="w")
        column_var = tk.StringVar(value="All columns")
        column_combo = ttk.Combobox(
            dialog,
            textvariable=column_var,
            values=("All columns", *heading_names),
            state="readonly",
            width=28,
        )
        column_combo.grid(row=0, column=1, padx=(0, 12), pady=(14, 6), sticky="ew")
        filter_label = ttk.Label(dialog, text="Contains")
        filter_label.grid(row=1, column=0, padx=12, pady=6, sticky="w")
        query_var = tk.StringVar()
        query_entry = ttk.Entry(dialog, textvariable=query_var)
        query_entry.grid(row=1, column=1, padx=(0, 12), pady=6, sticky="ew")
        value_var = tk.StringVar(value="All values")
        value_combo = ttk.Combobox(dialog, textvariable=value_var, state="readonly", width=28)
        value_combo.grid(row=1, column=1, padx=(0, 12), pady=6, sticky="ew")
        value_combo.grid_remove()

        def update_filter_input(_event: tk.Event | None = None) -> None:
            selected_column = column_var.get()
            column_index = heading_names.index(selected_column) if selected_column in heading_names else None
            finite_values = (
                self._finite_treeview_filter_values(source_rows, column_index, selected_column)
                if column_index is not None else []
            )
            if finite_values:
                filter_label.configure(text="Value")
                value_combo.configure(values=("All values", *finite_values))
                value_var.set("All values")
                query_entry.grid_remove()
                value_combo.grid()
            else:
                filter_label.configure(text="Contains")
                value_combo.grid_remove()
                query_entry.grid()

        column_combo.bind("<<ComboboxSelected>>", update_filter_input)
        update_filter_input()

        def apply_filter() -> None:
            selected_column = column_var.get()
            column_index = heading_names.index(selected_column) if selected_column in heading_names else None
            finite_values = (
                self._finite_treeview_filter_values(source_rows, column_index, selected_column)
                if column_index is not None else []
            )
            if finite_values:
                selected_value = value_var.get()
                if selected_value == "All values":
                    self._clear_treeview_sort_filter(tree)
                    dialog.destroy()
                    return
                assert column_index is not None
                matches = [
                    (item, values)
                    for item, values in source_rows
                    if values[column_index].casefold() == selected_value.casefold()
                ]
            else:
                query = query_var.get().strip().casefold()
                if not query:
                    self._clear_treeview_sort_filter(tree)
                    dialog.destroy()
                    return
                matches = []
                for item, values in source_rows:
                    haystack = values[column_index] if column_index is not None else " ".join(values)
                    if query in haystack.casefold():
                        matches.append((item, values))
            self._tree_filter_snapshot[key] = source_rows
            for item in tree.get_children(""):
                tree.delete(item)
            for item, values in matches:
                tree.insert("", "end", iid=item, values=values)
            self._tree_sort_state.pop(key, None)
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, columnspan=2, pady=(8, 12))
        ttk.Button(buttons, text="Apply", command=apply_filter).pack(side="left", padx=4)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left", padx=4)
        dialog.bind("<Return>", lambda _event: apply_filter())

    def _clear_treeview_sort_filter(self, tree: ttk.Treeview) -> None:
        key = str(tree)
        snapshot = self._tree_filter_snapshot.pop(key, None) or self._tree_default_rows.get(key)
        if snapshot is not None:
            for item in tree.get_children(""):
                tree.delete(item)
            for item, values in snapshot:
                tree.insert("", "end", iid=item, values=values)
        self._tree_sort_state.pop(key, None)

    def _sort_treeview(self, tree: ttk.Treeview, column: str) -> None:
        key = str(tree)
        if key not in self._tree_default_rows:
            self._tree_default_rows[key] = self._treeview_rows(tree)
        previous_column, descending = self._tree_sort_state.get(key, ("", False))
        descending = not descending if previous_column == column else False
        self._tree_sort_state[key] = (column, descending)
        rows = [(tree.set(item, column), item) for item in tree.get_children("")]

        def sort_key(row: tuple[str, str]) -> tuple[int, Any]:
            value = row[0].strip()
            try:
                return (0, float(value))
            except ValueError:
                return (1, value.casefold())

        rows.sort(key=sort_key, reverse=descending)
        for index, (_value, item) in enumerate(rows):
            tree.move(item, "", index)

    def _queue_event(self, event: str, payload: Any) -> None:
        item = (event, payload)
        try:
            self.events.put_nowait(item)
            return
        except Full:
            pass
        if event == "log":
            self.dropped_log_events += 1
            return
        preserved = []
        while True:
            try:
                queued = self.events.get_nowait()
            except Empty:
                break
            if queued[0] == "log":
                self.dropped_log_events += 1
            else:
                preserved.append(queued)
        for queued in preserved:
            try:
                self.events.put_nowait(queued)
            except Full:
                break
        try:
            self.events.put_nowait(item)
        except Full:
            self.dropped_log_events += 1

    def _build_profiles(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Run profile", padding=12)
        frame.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 10))
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Profile").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.profile_combo = ttk.Combobox(
            frame,
            textvariable=self.profile_name,
            values=sorted(self.profiles, key=str.casefold),
        )
        self.profile_combo.grid(row=0, column=1, sticky="ew")
        ttk.Button(frame, text="Load", command=self.load_profile).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(frame, text="Save", command=self.save_profile).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(frame, text="Delete", command=self.delete_profile).grid(row=0, column=4, padx=(8, 0))

    def _build_paths(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Locations", padding=12)
        frame.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Input folder (recursive)").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.workdir).grid(row=0, column=1, sticky="ew")
        ttk.Button(frame, text="Choose...", command=self.choose_workdir).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(frame, text="Output folder").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(frame, text="Choose...", command=self.choose_output_dir).grid(
            row=1, column=2, padx=(8, 0), pady=(8, 0)
        )

        ttk.Label(frame, text="Siril executable").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.siril_exe).grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(frame, text="Choose...", command=self.choose_siril).grid(row=2, column=2, padx=(8, 0), pady=(8, 0))

        ttk.Label(frame, text="Test frame count (0 = all)").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        ttk.Spinbox(
            frame,
            from_=0,
            to=1000000,
            increment=1,
            textvariable=self.test_frame_count,
            width=12,
        ).grid(row=3, column=1, sticky="w", pady=(8, 0))

    def _spinbox(
        self,
        parent: Any,
        row: int,
        pair: int,
        label: str,
        variable: tk.Variable,
        lower: float,
        upper: float,
        increment: float,
    ) -> ttk.Spinbox:
        column = pair * 2
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 6), pady=4)
        spinbox = ttk.Spinbox(
            parent,
            from_=lower,
            to=upper,
            increment=increment,
            textvariable=variable,
            width=10,
        )
        spinbox.grid(row=row, column=column + 1, sticky="ew", padx=(0, 16), pady=2)
        return spinbox

    def _build_settings(self) -> None:
        frame = ttk.Frame(self.root)
        frame.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 10))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(frame)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.settings_notebook = notebook
        basic_tab = ttk.Frame(notebook)
        advanced_tab = ttk.Frame(notebook)
        crop_tab = ttk.Frame(notebook)
        review_tab = ttk.Frame(notebook)
        threshold_tab = ttk.Frame(notebook)
        history_tab = ttk.Frame(notebook)
        quality_tab = ttk.Frame(notebook)
        frame_tab = ttk.Frame(notebook)
        cohort_tab = ttk.Frame(notebook)
        coverage_tab = ttk.Frame(notebook)
        batch_tab = ttk.Frame(notebook)
        notebook.add(basic_tab, text="Basic")
        notebook.add(advanced_tab, text="Advanced")
        notebook.add(batch_tab, text="Batch Queue")
        notebook.add(history_tab, text="Run History")
        notebook.add(review_tab, text="Run Review")
        notebook.add(cohort_tab, text="Cohort Balance")
        notebook.add(quality_tab, text="Quality Explorer")
        notebook.add(threshold_tab, text="Threshold Lab")
        notebook.add(frame_tab, text="Frame Inspector")
        notebook.add(coverage_tab, text="Coverage Inspector")
        notebook.add(crop_tab, text="Cropping Workbench")
        self._build_crop_workbench_tab(crop_tab)
        self._build_threshold_lab_tab(threshold_tab)
        self._build_history_tab(history_tab)
        self._build_quality_explorer_tab(quality_tab)
        self._build_frame_inspector_tab(frame_tab)
        self._build_cohort_balance_tab(cohort_tab)
        self._build_coverage_inspector_tab(coverage_tab)
        self._build_batch_queue_tab(batch_tab)
        basic_tab.columnconfigure(0, weight=1)
        basic_tab.rowconfigure(0, weight=1)

        canvas = tk.Canvas(
            basic_tab,
            background=self.root.cget("background"),
            borderwidth=0,
            highlightthickness=0,
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(basic_tab, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        canvas.configure(yscrollcommand=scrollbar.set)

        content = ttk.Frame(canvas)
        content.columnconfigure(0, weight=1, uniform="settings")
        content.columnconfigure(1, weight=1, uniform="settings")
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window, width=event.width),
        )
        self.settings_canvas = canvas
        self.settings_content = content

        left_column = ttk.Frame(content)
        left_column.grid(row=0, column=0, sticky="new", padx=(0, 5))
        left_column.columnconfigure(0, weight=1)
        right_column = ttk.Frame(content)
        right_column.grid(row=0, column=1, sticky="new", padx=(5, 0))
        right_column.columnconfigure(0, weight=1)

        def section(parent: ttk.Frame, title: str, row: int) -> ttk.LabelFrame:
            group = ttk.LabelFrame(parent, text=title, padding=(8, 4))
            group.grid(
                row=row,
                column=0,
                sticky="ew",
                pady=(0, 6),
            )
            for value_column in (1, 3):
                group.columnconfigure(value_column, weight=1)
            return group

        capture = section(left_column, "Capture & CFA", 0)
        self.substacks_widget = self._spinbox(capture, 0, 0, "Substacks", self.substacks, 1, 100, 1)
        ttk.Label(capture, text="Bayer pattern").grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            capture,
            textvariable=self.bayer_pattern,
            values=("Auto (header)", "RGGB", "BGGR", "GBRG", "GRBG"),
            state="readonly",
            width=13,
        ).grid(row=0, column=3, sticky="ew", padx=(0, 16), pady=4)
        ttk.Label(capture, text="CFA row order").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            capture,
            textvariable=self.bayer_orientation,
            values=("Auto", "Top-down", "Bottom-up"),
            state="readonly",
            width=13,
        ).grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=4)
        ttk.Checkbutton(
            capture,
            text="Auto substacks (detected Siril limit)",
            variable=self.auto_substacks,
            command=self.update_substack_controls,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=4)
        ttk.Checkbutton(
            capture,
            text="Export one master per acquisition cohort",
            variable=self.export_per_cohort,
            command=self.update_cohort_group_controls,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        cohort_group_frame = ttk.Frame(capture)
        cohort_group_frame.grid(row=3, column=2, columnspan=2, sticky="w", pady=4)
        for text, variable in (
            ("Camera model", self.cohort_group_camera),
            ("Filter", self.cohort_group_filter),
            ("Exposure time", self.cohort_group_exposure),
        ):
            widget = ttk.Checkbutton(cohort_group_frame, text=text, variable=variable)
            widget.pack(side="left", padx=(0, 8))
            self.cohort_group_widgets.append(widget)
        self.update_cohort_group_controls()

        cosmetic = section(right_column, "Cosmetic correction", 0)
        ttk.Checkbutton(
            cosmetic,
            text="Enable cosmetic correction",
            variable=self.cosmetic_correction,
            command=self.update_cosmetic_controls,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=4)
        self.cosmetic_widgets.extend((
            self._spinbox(
                cosmetic, 1, 0, "Cold-pixel sigma", self.cosmetic_cold_sigma, 0.1, 50.0, 0.1
            ),
            self._spinbox(
                cosmetic, 1, 1, "Hot-pixel sigma", self.cosmetic_hot_sigma, 0.1, 20.0, 0.1
            ),
        ))

        drizzle = section(right_column, "Drizzle Settings", 1)
        ttk.Checkbutton(
            drizzle,
            text="Enable drizzle",
            variable=self.drizzle,
            command=self.update_drizzle_controls,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=4)
        self.drizzle_widgets.extend((
            self._spinbox(drizzle, 1, 0, "Drizzle scale", self.drizzle_scale, 0.1, 3.0, 0.1),
            self._spinbox(drizzle, 1, 1, "Droplet size", self.pixel_fraction, 0.1, 1.0, 0.1),
        ))
        ttk.Label(drizzle, text="Drizzle kernel").grid(
            row=1, column=2, sticky="w", padx=(0, 6), pady=4
        )
        drizzle_kernel_widget = ttk.Combobox(
            drizzle,
            textvariable=self.drizzle_kernel,
            values=DRIZZLE_KERNELS,
            state="readonly",
            width=13,
        )
        drizzle_kernel_widget.grid(row=1, column=3, sticky="ew", padx=(0, 16), pady=4)
        self.drizzle_widgets.append(drizzle_kernel_widget)

        selection = section(left_column, "Frame selection", 1)
        for row, pair, label, variable in (
            (0, 0, "Background filter (%)", self.filter_background),
            (0, 1, "Star-count filter (%)", self.filter_stars),
            (1, 0, "Roundness filter (%)", self.filter_roundness),
            (1, 1, "FWHM filter (%)", self.filter_fwhm),
        ):
            widget = self._spinbox(selection, row, pair, label, variable, 1, 100, 1)
            self.quality_percent_widgets.append(widget)
            if label == "Star-count filter (%)":
                self.star_count_widget = widget
        ttk.Checkbutton(
            selection,
            text="Adaptive quality filters",
            variable=self.adaptive_quality_filtering,
            command=self.update_quality_filter_controls,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(
            selection,
            text="Mosaic-aware star count",
            variable=self.mosaic_aware_star_count,
            command=self.update_quality_filter_controls,
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=4)
        self.quality_sigma_widgets.append(
            self._spinbox(
                selection, 2, 1, "Quality filter sigma", self.quality_filter_sigma, 0.1, 20.0, 0.1
            )
        )

        integration = section(right_column, "Integration", 2)
        ttk.Label(integration, text="Weighting").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            integration,
            textvariable=self.weight,
            values=("wfwhm", "noise", "nbstars", "nbstack"),
            state="readonly",
            width=13,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=4)
        self._spinbox(integration, 0, 1, "Feather", self.feather, 0, 1000, 1)
        ttk.Label(integration, text="Pixel rejection").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=4
        )
        rejection_method_widget = ttk.Combobox(
            integration,
            textvariable=self.rejection_method,
            values=PIXEL_REJECTION_METHODS,
            state="readonly",
            width=13,
        )
        rejection_method_widget.grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=4)
        rejection_method_widget.bind(
            "<<ComboboxSelected>>", lambda _event: self.rejection_method_changed()
        )
        self.rejection_widgets.extend((
            self._spinbox(integration, 2, 0, "Low rejection", self.rejection_low, 0.1, 20.0, 0.1),
            self._spinbox(integration, 3, 0, "High rejection", self.rejection_high, 0.1, 20.0, 0.1),
        ))
        for row, label, variable in (
            (2, "Low map", self.low_rejection_map),
            (3, "High map", self.high_rejection_map),
        ):
            ttk.Label(integration, text=label).grid(
                row=row, column=2, sticky="w", padx=(0, 6), pady=4
            )
            ttk.Combobox(
                integration,
                textvariable=variable,
                values=("Follow method", "Always", "Never"),
                state="readonly",
                width=13,
            ).grid(row=row, column=3, sticky="ew", pady=4)
        overlap_check = ttk.Checkbutton(
            integration,
            text="Normalize overlaps",
            variable=self.overlap_normalization,
            command=self.update_normalization_controls,
        )
        overlap_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(integration, text="Stack normalization").grid(
            row=4, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            integration,
            textvariable=self.stack_normalization,
            values=STACK_NORMALIZATION_METHODS,
            state="readonly",
            width=13,
        ).grid(row=4, column=1, sticky="ew", padx=(0, 16), pady=4)
        self.fast_normalization_widget = ttk.Checkbutton(
            integration,
            text="Fast normalization",
            variable=self.fast_normalization,
        )
        self.fast_normalization_widget.grid(row=5, column=2, columnspan=2, sticky="w", pady=4)
        coverage_check = ttk.Checkbutton(
            integration,
            text="Write coverage map",
            variable=self.coverage_map,
            command=self.update_coverage_controls,
        )
        coverage_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=4)
        crop_check = ttk.Checkbutton(
            integration,
            text="Create auto-cropped master",
            variable=self.auto_crop_master,
            command=self.update_coverage_controls,
        )
        crop_check.grid(row=6, column=2, columnspan=2, sticky="w", pady=4)
        crop_percent = self._spinbox(
            integration, 7, 0, "Crop depth (%)", self.auto_crop_coverage_percent, 1, 100, 1
        )
        self.coverage_widgets.extend((coverage_check, crop_check, crop_percent))
        self.update_coverage_controls()
        self.update_normalization_controls()

        background = section(left_column, "Background & plate solving", 2)
        ttk.Label(background, text="Background extraction").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            background,
            textvariable=self.background_method,
            values=("Off", "Linear", "Quadratic", "RBF"),
            state="readonly",
            width=13,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=4)
        ttk.Label(background, text="Plate catalog").grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            background,
            textvariable=self.catalog,
            values=PLATE_SOLVE_CATALOGS,
            state="readonly",
            width=13,
        ).grid(row=0, column=3, sticky="ew", padx=(0, 16), pady=4)
        self.background_widgets.extend((
            self._spinbox(
                background, 1, 0, "Background samples", self.background_samples, 1, 100, 1
            ),
            self._spinbox(
                background,
                1,
                1,
                "Background tolerance",
                self.background_tolerance,
                0.1,
                10.0,
                0.1,
            ),
        ))
        self.background_method.trace_add("write", lambda *_: self.update_background_controls())

        resources = section(right_column, "Resources", 3)
        self._spinbox(resources, 0, 0, "Memory fraction", self.memory, 0.1, 1.0, 0.1)
        self._spinbox(resources, 0, 1, "CPU count", self.cpus, 1, 256, 1)
        self._spinbox(resources, 1, 0, "Retries", self.retries, 0, 20, 1)
        ttk.Checkbutton(
            resources,
            text="Keep intermediate files",
            variable=self.debug,
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(
            resources,
            text="Skip failed frames (log only)",
            variable=self.skip_failed_frames,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=4)

        advanced_tab.columnconfigure(0, weight=1)
        advanced_tab.rowconfigure(0, weight=1)
        advanced_canvas = tk.Canvas(
            advanced_tab,
            background=self.root.cget("background"),
            borderwidth=0,
            highlightthickness=0,
        )
        advanced_canvas.grid(row=0, column=0, sticky="nsew")
        advanced_scrollbar = ttk.Scrollbar(
            advanced_tab, orient="vertical", command=advanced_canvas.yview
        )
        advanced_scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        advanced_canvas.configure(yscrollcommand=advanced_scrollbar.set)
        advanced_content = ttk.Frame(advanced_canvas)
        advanced_content.columnconfigure(0, weight=1, uniform="advanced")
        advanced_content.columnconfigure(1, weight=1, uniform="advanced")
        advanced_window = advanced_canvas.create_window(
            (0, 0), window=advanced_content, anchor="nw"
        )
        advanced_content.bind(
            "<Configure>",
            lambda _event: advanced_canvas.configure(
                scrollregion=advanced_canvas.bbox("all")
            ),
        )
        advanced_canvas.bind(
            "<Configure>",
            lambda event: advanced_canvas.itemconfigure(advanced_window, width=event.width),
        )
        self.advanced_content = advanced_content

        def advanced_section(title: str, row: int, column: int) -> ttk.LabelFrame:
            group = ttk.LabelFrame(advanced_content, text=title, padding=(8, 4))
            group.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=(0, 5) if column == 0 else (5, 0),
                pady=(0, 6),
            )
            for value_column in (1, 3):
                group.columnconfigure(value_column, weight=1)
            return group

        background_advanced = advanced_section("Background extraction", 0, 0)
        self.rbf_smoothing_widget = self._spinbox(
            background_advanced, 0, 0, "RBF smoothing", self.rbf_smoothing, 0.01, 10.0, 0.01
        )
        self.background_dither_widget = ttk.Checkbutton(
            background_advanced,
            text="Dither background samples",
            variable=self.background_dither,
        )
        self.background_dither_widget.grid(row=1, column=0, columnspan=4, sticky="w", pady=2)

        plate_solving = advanced_section("Plate solving", 1, 0)
        self._spinbox(plate_solving, 0, 0, "SIP order", self.plate_solve_order, 1, 5, 1)
        ttk.Checkbutton(
            plate_solving,
            text="Downscale star detection",
            variable=self.plate_solve_downscale,
        ).grid(row=0, column=2, columnspan=2, sticky="w", pady=2)
        ttk.Label(plate_solving, text="Search radius").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=2
        )
        ttk.Entry(plate_solving, textvariable=self.plate_solve_radius, width=10).grid(
            row=1, column=1, sticky="ew", padx=(0, 16), pady=2
        )
        ttk.Label(plate_solving, text="Limit magnitude").grid(
            row=1, column=2, sticky="w", padx=(0, 6), pady=2
        )
        ttk.Entry(plate_solving, textvariable=self.plate_solve_limit_mag, width=10).grid(
            row=1, column=3, sticky="ew", padx=(0, 16), pady=2
        )

        registration = advanced_section("Registration", 0, 1)
        ttk.Label(registration, text="Transform").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=2
        )
        ttk.Combobox(
            registration,
            textvariable=self.registration_transform,
            values=REGISTRATION_TRANSFORMS,
            state="readonly",
            width=13,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=2)
        ttk.Label(registration, text="Interpolation").grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=2
        )
        ttk.Combobox(
            registration,
            textvariable=self.registration_interpolation,
            values=REGISTRATION_INTERPOLATIONS,
            state="readonly",
            width=13,
        ).grid(row=0, column=3, sticky="ew", padx=(0, 16), pady=2)
        self._spinbox(registration, 1, 0, "Minimum star pairs (0 = auto)", self.registration_minpairs, 0, 200, 1)
        self._spinbox(registration, 1, 1, "Maximum stars (0 = auto)", self.registration_maxstars, 0, 2000, 100)

        reproducibility = advanced_section("Reproducibility", 1, 1)
        ttk.Label(reproducibility, text="Random seed (blank = generate)").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=(0, 6), pady=2
        )
        ttk.Entry(reproducibility, textvariable=self.random_seed).grid(
            row=0, column=2, columnspan=2, sticky="ew", padx=(0, 16), pady=2
        )
        ttk.Label(
            reproducibility,
            text="The seed and selected-file manifest are saved with each run.",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=2)

        self._build_run_review(review_tab)

    def _build_run_review(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        summary_frame = ttk.LabelFrame(parent, text="Run summary", padding=(8, 4))
        summary_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        summary_frame.columnconfigure(0, weight=1)
        self.review_summary = tk.Text(
            summary_frame,
            height=8,
            wrap="word",
            state="disabled",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            insertbackground=DARK_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=DARK_BORDER,
            font=("Consolas", 9),
        )
        self.review_summary.grid(row=0, column=0, sticky="ew")
        verification_bar = ttk.Frame(summary_frame)
        verification_bar.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(
            verification_bar,
            text="Verify Run",
            command=self.verify_loaded_run,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            verification_bar,
            textvariable=self.verification_status,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Button(
            verification_bar,
            text="Open Frame Ledger",
            command=self.open_ledger_viewer,
        ).grid(row=0, column=2, sticky="w", padx=(14, 0))
        ttk.Button(
            verification_bar,
            text="Export Ledger CSV",
            command=self.export_ledger_csv,
        ).grid(row=0, column=3, sticky="w", padx=(8, 0))
        ttk.Button(
            verification_bar,
            text="Export Run Bundle",
            command=self.export_run_bundle,
        ).grid(row=0, column=4, sticky="w", padx=(8, 0))
        rejected_frame = ttk.LabelFrame(parent, text="Discarded frames", padding=(8, 4))
        rejected_frame.grid(row=1, column=0, sticky="nsew")
        rejected_frame.columnconfigure(0, weight=1)
        rejected_frame.rowconfigure(0, weight=1)
        columns = ("substack", "file", "status", "reason", "fwhm", "roundness", "background", "stars")
        self.review_tree = ttk.Treeview(
            rejected_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "substack": ("Substack", 80),
            "file": ("Frame", 300),
            "status": ("Status", 90),
            "reason": ("Reason", 220),
            "fwhm": ("FWHM", 175),
            "roundness": ("Roundness", 190),
            "background": ("Background", 200),
            "stars": ("Stars", 180),
        }
        for column, (heading, width) in headings.items():
            self.review_tree.heading(column, text=heading)
            self.review_tree.column(column, width=width, minwidth=60, anchor="w")
        self.review_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(rejected_frame, orient="vertical", command=self.review_tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(rejected_frame, orient="horizontal", command=self.review_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.review_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        review_table_controls = self._treeview_filter_controls(verification_bar, self.review_tree)
        review_table_controls.grid(row=0, column=5, sticky="w", padx=(8, 0))
        ttk.Button(
            verification_bar,
            text="Cleanup Review",
            command=self.show_cleanup_review,
        ).grid(row=0, column=6, sticky="w", padx=(8, 0))
        visual_qa_bar = ttk.Frame(summary_frame)
        visual_qa_bar.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(
            visual_qa_bar,
            text="Run Visual QA...",
            command=self.open_visual_qa_window,
        ).pack(side="left")
        ttk.Label(
            visual_qa_bar,
            text="Read-only preview diagnostics",
        ).pack(side="left", padx=(8, 0))
        self._set_review_summary("No completed run loaded.")

    def _set_text(self, widget: tk.Text | None, text: str) -> None:
        if widget is None or not widget.winfo_exists():
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _build_threshold_lab_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.LabelFrame(parent, text="Keep-best targets", padding=12)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        controls.columnconfigure(1, weight=1)
        for row, name in enumerate(("background", "roundness", "fwhm", "stars")):
            variable = tk.DoubleVar(value=80)
            self.threshold_vars[name] = variable
            ttk.Label(controls, text=name.title(), width=14).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            slider = ttk.Scale(
                controls,
                from_=1,
                to=100,
                variable=variable,
                command=lambda value, metric=name: self._threshold_changed(metric, value),
            )
            slider.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=4)
            label = ttk.Label(controls, text="80%", width=7)
            label.grid(row=row, column=2, sticky="e", pady=4)
            self.threshold_value_labels[name] = label
        ttk.Button(
            controls,
            text="Sync to Frame Selection",
            command=self.sync_threshold_to_frame_selection,
            style="Primary.TButton",
        ).grid(row=4, column=2, sticky="e", pady=(8, 0))
        self.threshold_summary = tk.Text(
            parent,
            wrap="word",
            state="disabled",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            relief="flat",
            font=("Consolas", 9),
        )
        self.threshold_summary.grid(row=1, column=0, sticky="nsew")
        self.refresh_threshold_lab()

    def _threshold_changed(self, metric: str, value: str) -> None:
        try:
            percentage = max(1, min(100, round(float(value))))
        except (TypeError, ValueError):
            percentage = 80
        label = self.threshold_value_labels.get(metric)
        if label is not None:
            label.configure(text=f"{percentage}%")
        if self.threshold_update_job is not None:
            self.root.after_cancel(self.threshold_update_job)
        self.threshold_update_job = self.root.after(120, self.refresh_threshold_lab)

    def sync_threshold_to_frame_selection(self) -> None:
        mapping = {
            "background": self.filter_background,
            "roundness": self.filter_roundness,
            "fwhm": self.filter_fwhm,
            "stars": self.filter_stars,
        }
        for metric, variable in self.threshold_vars.items():
            variable_target = mapping.get(metric)
            if variable_target is not None:
                variable_target.set(max(1, min(100, round(variable.get()))))
        self.adaptive_quality_filtering.set(False)
        self.update_quality_filter_controls()
        notebook = getattr(self, "settings_notebook", None)
        if notebook is not None:
            notebook.select(0)
        self.status.set("Threshold Lab values synced to Basic frame selection.")

    def refresh_threshold_lab(self) -> None:
        self.threshold_update_job = None
        loaded = self._loaded_report()
        if loaded is None:
            self._set_text(self.threshold_summary, "No completed report loaded. Load one from Run History.")
            return
        report_path, report = loaded
        ledger_path = self._resolve_report_artifact_path(report_path, report.get("frame_ledger_path"))
        if ledger_path is None or not ledger_path.is_file():
            self._set_text(self.threshold_summary, "The loaded run has no usable frame ledger.")
            return
        try:
            if not self.threshold_records or self._threshold_ledger_path != ledger_path:
                self.threshold_records = read_frame_ledger(ledger_path)
                self._threshold_ledger_path = ledger_path
            result = replay_frame_records(
                self.threshold_records,
                {
                    metric: max(1, min(100, round(variable.get())))
                    for metric, variable in self.threshold_vars.items()
                },
                ledger_path,
            )
            self._set_text(self.threshold_summary, self._replay_text(result))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._set_text(self.threshold_summary, f"Threshold tuning unavailable:\n{error}")

    @staticmethod
    def _resolve_report_artifact_path(report_path: Path, value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else report_path.parent / path

    def _build_history_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Button(controls, text="Refresh History", command=self.refresh_history).pack(side="left")
        ttk.Button(controls, text="Compare Selected", command=self.compare_selected_history).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Export Experiment Record", command=self.export_experiment_record).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Verify Selected", command=self.verify_selected_history).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Export Selected Bundle", command=self.export_selected_history_bundle).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Export HTML Report", command=self.export_selected_html_report).pack(side="left", padx=(8, 0))
        self._treeview_filter_controls(controls, self.history_tree).pack(side="left", padx=(8, 0))
        body = ttk.Panedwindow(parent, orient="vertical")
        body.grid(row=1, column=0, sticky="nsew")
        table_frame = ttk.Frame(body)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("run_id", "status", "updated", "input", "stacked", "rejected", "hours", "verify")
        history_style = ttk.Style(self.root)
        history_style.map(
            "LoadedHistory.Treeview",
            background=[("selected", "#2f6f4e")],
            foreground=[("selected", "#ffffff")],
        )
        self.history_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="LoadedHistory.Treeview",
        )
        self.history_tree.tag_configure("loaded_run", background="#2f6f4e", foreground="#ffffff")
        headings = {
            "run_id": ("Run", 180), "status": ("Status", 90), "updated": ("Updated", 155),
            "input": ("Input", 70), "stacked": ("Stacked", 75), "rejected": ("Rejected", 80),
            "hours": ("Integrated h", 95), "verify": ("Verify", 70),
        }
        for column, (heading, width) in headings.items():
            self.history_tree.heading(column, text=heading)
            self.history_tree.column(column, width=width, minwidth=55, anchor="w")
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.history_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.bind("<<TreeviewSelect>>", lambda _event: self._history_selection_changed())
        body.add(table_frame, weight=3)
        details = ttk.Frame(body)
        details.columnconfigure(0, weight=1)
        details.rowconfigure(0, weight=1)
        self.history_summary = tk.Text(details, height=8, wrap="word", state="disabled", background=DARK_FIELD, foreground=DARK_TEXT, relief="flat", font=("Consolas", 9))
        self.history_summary.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.history_compare_text = tk.Text(details, height=8, wrap="word", state="disabled", background=DARK_FIELD, foreground=DARK_TEXT, relief="flat", font=("Consolas", 9))
        self.history_compare_text.grid(row=0, column=1, sticky="nsew")
        details.columnconfigure(0, weight=1)
        details.columnconfigure(1, weight=1)
        body.add(details, weight=1)
        self.refresh_history()

    def _history_report_paths(self) -> list[Path]:
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        if not output_dir.is_dir():
            return []
        return sorted(output_dir.glob("quality_report_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)

    def refresh_history(self) -> None:
        if self.history_tree is None:
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        self.history_records = {}
        self.history_item_paths = {}
        for report_path in self._history_report_paths():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            summary = report_history_summary(report_path, report)
            item = self.history_tree.insert("", "end", values=(
                summary["run_id"], summary["status"], summary["updated_at"].replace("T", " "),
                summary["input_frames"], summary["stacked_frames"], summary["rejected_frames"],
                "" if summary["integrated_hours"] is None else f"{summary['integrated_hours']:.3f}",
                summary["verification"],
            ))
            self.history_records[item] = summary
            self.history_item_paths[item] = report_path
        self._update_loaded_history_highlight()

    def _update_loaded_history_highlight(self) -> None:
        if self.history_tree is None:
            return
        loaded_path = self.quality_report_path
        for item, report_path in self.history_item_paths.items():
            is_loaded = False
            if loaded_path is not None:
                try:
                    is_loaded = report_path.resolve() == loaded_path.resolve()
                except OSError:
                    is_loaded = report_path == loaded_path
            self.history_tree.item(item, tags=("loaded_run",) if is_loaded else ())

    def _history_selected_paths(self) -> list[Path]:
        if self.history_tree is None:
            return []
        return [self.history_item_paths[item] for item in self.history_tree.selection() if item in self.history_item_paths]

    def _history_selection_changed(self) -> None:
        paths = self._history_selected_paths()
        if len(paths) != 1:
            return
        try:
            report = json.loads(paths[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        summary = report_history_summary(paths[0], report)
        lines = [
            f"Run: {summary['run_id']}", f"Report: {paths[0]}",
            f"Status: {summary['status']}", f"Input: {summary['input_frames']}",
            f"Stacked: {summary['stacked_frames']}", f"Rejected: {summary['rejected_frames']}",
            f"Verification artifact: {summary['verification']}",
            "", "Selecting one row makes this run the active analysis report and refreshes the analysis tabs.",
        ]
        self._set_text(self.history_summary, "\n".join(lines))
        if self.quality_report_path != paths[0]:
            self.quality_report_path = paths[0]
            self.crop_report.set(str(paths[0]))
            self.load_crop_report(paths[0], notify=False)
            self.refresh_run_review(paths[0])
            self.status.set(f"Loaded run {paths[0].stem.removeprefix('quality_report_')}")

    def compare_selected_history(self) -> None:
        paths = self._history_selected_paths()
        if len(paths) != 2:
            messagebox.showwarning("Run History", "Select exactly two runs to compare.")
            return
        reports = []
        for path in paths:
            try:
                reports.append(report_history_summary(path, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                messagebox.showerror("Run History", f"Could not read {path}")
                return
        comparison = compare_run_summaries(reports[0], reports[1])
        text = [
            "RUN COMPARISON",
            f"First:  {comparison['first_run']}",
            f"Second: {comparison['second_run']}",
            "",
            f"Input frames delta: {comparison['input_frames_delta']:+}",
            f"Stacked frames delta: {comparison['stacked_frames_delta']:+}",
            f"Rejected frames delta: {comparison['rejected_frames_delta']:+}",
            f"Integrated hours delta: {comparison['integrated_hours_delta']:+.3f}" if comparison['integrated_hours_delta'] is not None else "Integrated hours delta: unavailable",
        ]
        self._set_text(self.history_compare_text, "\n".join(text))

    def export_experiment_record(self) -> None:
        paths = self._history_selected_paths()
        if len(paths) < 2:
            messagebox.showwarning("Run History", "Select at least two completed runs.")
            return
        try:
            reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        except (OSError, json.JSONDecodeError) as error:
            messagebox.showerror("Run History", f"Could not read selected reports:\n\n{error}")
            return
        run_ids = [report.get("run_id") or path.stem for path, report in zip(paths, reports)]
        destination = filedialog.asksaveasfilename(
            title="Export experiment comparison record",
            initialfile=(
                f"experiment_{run_ids[0]}_vs_{run_ids[1]}"
                f"{'_plus_' + str(len(run_ids) - 2) if len(run_ids) > 2 else ''}.json"
            ),
            defaultextension=".json",
            filetypes=(("JSON", "*.json"), ("All files", "*.*")),
        )
        if not destination:
            return
        control_run_id = simpledialog.askstring(
            "Experiment Control",
            f"Enter the control run ID ({', '.join(run_ids)}):",
            initialvalue=run_ids[0],
            parent=self.root,
        )
        if control_run_id is None:
            return
        conclusion = simpledialog.askstring(
            "Experiment Conclusion",
            "Record your interpretation (leave blank if pending):",
            initialvalue="",
            parent=self.root,
        )
        if conclusion is None:
            return
        try:
            record_path, record = write_experiment_record(
                paths,
                destination,
                control_run_id=control_run_id.strip(),
                conclusion=conclusion,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("Run History", f"Could not export experiment record:\n\n{error}")
            return
        self._set_text(
            self.history_compare_text,
            "EXPERIMENT RECORD\n"
            f"Runs: {', '.join(run_ids)}\n"
            f"Control: {record['control_run_id']}\n"
            f"Shared input identity: {record['input_identity']}\n"
            f"Record: {record_path}",
        )
        self.status.set(f"Exported experiment record: {record_path}")

    def verify_selected_history(self) -> None:
        paths = self._history_selected_paths()
        if len(paths) != 1:
            messagebox.showwarning("Run History", "Select exactly one run to verify.")
            return
        verification = verify_run_report(paths[0])
        self._set_text(self.history_compare_text, self._verification_text(verification))

    def export_selected_history_bundle(self) -> None:
        paths = self._history_selected_paths()
        if len(paths) != 1:
            messagebox.showwarning("Run History", "Select exactly one run to export.")
            return
        destination = filedialog.asksaveasfilename(
            title="Export run evidence bundle",
            initialfile=f"{paths[0].stem}_bundle.zip",
            defaultextension=".zip",
            filetypes=(("ZIP", "*.zip"), ("All files", "*.*")),
        )
        if not destination:
            return
        try:
            bundle = create_run_bundle(paths[0], destination)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("Run History", f"Could not create bundle:\n\n{error}")
            return
        self.status.set(f"Exported run bundle: {bundle}")

    def export_selected_html_report(self) -> None:
        paths = self._history_selected_paths()
        if len(paths) != 1:
            messagebox.showwarning("Run History", "Select exactly one run to export.")
            return
        destination = filedialog.asksaveasfilename(
            title="Export HTML run report",
            initialfile=f"{paths[0].stem}.html",
            defaultextension=".html",
            filetypes=(("HTML", "*.html"), ("All files", "*.*")),
        )
        if not destination:
            return
        try:
            output = export_html_run_report(paths[0], destination)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("Run History", f"Could not create HTML report:\n\n{error}")
            return
        self.status.set(f"Exported HTML report: {output}")

    def _build_quality_explorer_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Label(controls, text="Metric").pack(side="left")
        ttk.Combobox(controls, textvariable=self.quality_metric, values=("fwhm", "roundness", "background", "stars"), state="readonly", width=14).pack(side="left", padx=(6, 10))
        self.quality_summary = tk.Text(parent, height=7, wrap="word", state="disabled", background=DARK_FIELD, foreground=DARK_TEXT, relief="flat", font=("Consolas", 9))
        self.quality_summary.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.quality_canvas = tk.Canvas(parent, background=DARK_FIELD, highlightthickness=1, highlightbackground=DARK_BORDER, height=360)
        self.quality_canvas.grid(row=1, column=0, sticky="nsew")
        self.quality_canvas.bind("<Configure>", self._quality_canvas_configured)
        self.quality_metric.trace_add("write", lambda *_args: self.refresh_quality_explorer())
        self.refresh_quality_explorer()

    def _quality_canvas_configured(self, _event: tk.Event | None = None) -> None:
        if self.quality_summary_data is not None:
            self._draw_quality_histogram(self.quality_summary_data)

    def refresh_quality_explorer(self) -> None:
        loaded = self._loaded_report()
        if loaded is None:
            self.quality_summary_data = None
            self._set_text(self.quality_summary, "No completed report loaded.")
            if self.quality_canvas is not None:
                self.quality_canvas.delete("all")
                self.quality_canvas.create_text(20, 20, anchor="nw", text="Load a run from Run History.", fill=DARK_TEXT)
            return
        report_path, report = loaded
        ledger = self._resolve_report_artifact_path(report_path, report.get("frame_ledger_path"))
        if ledger is None or not ledger.is_file():
            self.quality_summary_data = None
            self._set_text(self.quality_summary, "The loaded run has no usable frame ledger.")
            return
        try:
            self.quality_records = read_frame_ledger(ledger)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self.quality_summary_data = None
            self._set_text(self.quality_summary, f"Could not read ledger: {error}")
            return
        summary = quality_metric_summary(self.quality_records, self.quality_metric.get())
        if not summary["count"]:
            self.quality_summary_data = None
            self._set_text(self.quality_summary, f"No measured {self.quality_metric.get()} values in this ledger.")
            return
        self.quality_summary_data = summary
        if summary["threshold"] is None:
            threshold_text = "Recorded cutoff: unavailable"
        elif summary["distinct_threshold_count"] > 1:
            threshold_text = (
                f"Orange line: median cutoff {summary['threshold']:.5g} across "
                f"{summary['threshold_count']} records (range {summary['threshold_minimum']:.5g} "
                f"to {summary['threshold_maximum']:.5g})"
            )
        else:
            threshold_text = f"Orange line: recorded cutoff {summary['threshold']:.5g}"
        self._set_text(self.quality_summary, "\n".join((
            f"Run: {report.get('run_id', report_path.stem)}",
            f"Metric: {summary['metric']}   measured: {summary['count']}",
            f"Range: {summary['minimum']:.5g} to {summary['maximum']:.5g}   median: {summary['median']:.5g}   mean: {summary['mean']:.5g}",
            f"Ignored nonpositive/unavailable placeholders: {summary.get('ignored_nonpositive', 0)}",
            f"Pass rule: value {summary['comparison']} cutoff",
            threshold_text,
        )))
        self._draw_quality_histogram(summary)

    def _draw_quality_histogram(self, summary: dict[str, Any]) -> None:
        canvas = self.quality_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 520)
        height = max(canvas.winfo_height(), 360)
        left, top, right, bottom = 55, 25, width - 25, height - 45
        values = summary["values"]
        bins = min(28, max(8, int(math.sqrt(len(values)))))
        counts, edges = np.histogram(values, bins=bins)
        max_count = max(int(counts.max()), 1)
        canvas.create_line(left, bottom, right, bottom, fill=DARK_TEXT)
        canvas.create_line(left, top, left, bottom, fill=DARK_TEXT)
        for index, count in enumerate(counts):
            x0 = left + (right - left) * index / bins
            x1 = left + (right - left) * (index + 1) / bins - 1
            y1 = bottom - (bottom - top) * int(count) / max_count
            canvas.create_rectangle(x0, y1, x1, bottom, fill="#4b8f8c", outline="")
        value_min = float(summary["minimum"])
        value_max = float(summary["maximum"])
        value_span = value_max - value_min
        for tick_index in range(5):
            fraction = tick_index / 4
            x = left + (right - left) * fraction
            value = value_min + value_span * fraction
            canvas.create_line(x, top, x, bottom, fill="#353a44", dash=(2, 4))
            canvas.create_line(x, bottom, x, bottom + 4, fill=DARK_TEXT)
            canvas.create_text(
                x,
                bottom + 9,
                anchor="n",
                text=f"{value:.4g}",
                fill=DARK_TEXT,
            )
            y = bottom - (bottom - top) * fraction
            count_value = round(max_count * fraction)
            canvas.create_line(left - 4, y, left, y, fill=DARK_TEXT)
            canvas.create_text(left - 8, y, anchor="e", text=str(count_value), fill=DARK_TEXT)
        if summary["threshold"] is not None and summary["maximum"] > summary["minimum"]:
            x = left + (summary["threshold"] - summary["minimum"]) / (summary["maximum"] - summary["minimum"]) * (right - left)
            canvas.create_line(x, top, x, bottom, fill="#e8a85c", width=2)
            threshold_label = (
                f"median cutoff ({summary['comparison']})"
                if summary.get("distinct_threshold_count", 0) > 1
                else f"cutoff ({summary['comparison']})"
            )
            if x > right - 110:
                canvas.create_text(min(x - 5, right - 5), top, anchor="ne", text=threshold_label, fill="#e8a85c")
            else:
                canvas.create_text(x + 5, top, anchor="nw", text=threshold_label, fill="#e8a85c")
        canvas.create_text((left + right) / 2, height - 10, anchor="s", text=summary["metric"], fill=DARK_TEXT)
        canvas.create_text(12, top - 16, anchor="nw", text="count", fill=DARK_TEXT)

    def _build_frame_inspector_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        body = ttk.Panedwindow(parent, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")
        table_frame = ttk.Frame(body)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("status", "file", "exposure", "reason")
        self.frame_inspector_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {"status": ("Status", 85), "file": ("Frame", 360), "exposure": ("Exposure s", 85), "reason": ("Reason", 190)}
        for column, (heading, width) in headings.items():
            self.frame_inspector_tree.heading(column, text=heading)
            self.frame_inspector_tree.column(column, width=width, minwidth=55, anchor="w")
        self.frame_inspector_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.frame_inspector_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.frame_inspector_tree.configure(yscrollcommand=scrollbar.set)
        self._treeview_filter_controls(controls, self.frame_inspector_tree).pack(side="left", padx=(8, 0))
        self.frame_inspector_tree.bind("<<TreeviewSelect>>", lambda _event: self._inspect_selected_frame())
        body.add(table_frame, weight=2)
        detail_frame = ttk.Frame(body)
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(1, weight=1)
        self.frame_inspector_summary = tk.Text(detail_frame, height=14, wrap="word", state="disabled", background=DARK_FIELD, foreground=DARK_TEXT, relief="flat", font=("Consolas", 9))
        self.frame_inspector_summary.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.frame_inspector_canvas = tk.Canvas(detail_frame, background=DARK_FIELD, highlightthickness=1, highlightbackground=DARK_BORDER, height=300)
        self.frame_inspector_canvas.grid(row=1, column=0, sticky="nsew")
        body.add(detail_frame, weight=1)
        self.refresh_frame_inspector()

    def refresh_frame_inspector(self) -> None:
        loaded = self._loaded_report()
        if loaded is None or self.frame_inspector_tree is None:
            self._set_text(self.frame_inspector_summary, "No completed report loaded.")
            return
        report_path, report = loaded
        ledger_path = self._resolve_report_artifact_path(report_path, report.get("frame_ledger_path"))
        if ledger_path is None or not ledger_path.is_file():
            self._set_text(self.frame_inspector_summary, "The loaded run has no usable frame ledger.")
            return
        try:
            self.frame_inspector_records = read_frame_ledger(ledger_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._set_text(self.frame_inspector_summary, f"Could not read ledger: {error}")
            return
        self.frame_inspector_report = (report_path, report)
        for item in self.frame_inspector_tree.get_children():
            self.frame_inspector_tree.delete(item)
        for index, record in enumerate(self.frame_inspector_records):
            self.frame_inspector_tree.insert("", "end", iid=str(index), values=(
                record.get("status", "unknown"), record.get("file", ""),
                record.get("exposure_seconds", ""), record.get("reason_code", record.get("reason", "")),
            ))
        if self.frame_inspector_records:
            first = self.frame_inspector_tree.get_children()[0]
            self.frame_inspector_tree.selection_set(first)
            self.frame_inspector_tree.focus(first)
            self._inspect_selected_frame()

    def _inspect_selected_frame(self) -> None:
        if self.frame_inspector_tree is None or self.frame_inspector_report is None:
            return
        selected = self.frame_inspector_tree.selection()
        if not selected:
            return
        index = int(selected[0])
        if index < 0 or index >= len(self.frame_inspector_records):
            return
        record = self.frame_inspector_records[index]
        report_path, report = self.frame_inspector_report
        source = resolve_frame_source_path(report_path, report, record)
        lines = [
            f"Frame: {record.get('file', 'unknown')}",
            f"Status: {record.get('status', 'unknown')}",
            f"Source: {source}",
            f"Exposure: {record.get('exposure_seconds', 'unknown')} s",
            f"Cohort: {record.get('cohort') or 'Unassigned'}",
            f"Substack: {record.get('substack', 'unknown')}",
            f"Stack membership: {record.get('stack_membership', 'unknown')}",
            f"Reason: {record.get('reason', record.get('reason_code', ''))}",
            "",
            "Metrics:",
        ]
        for name, detail in (record.get("filter_metrics") or {}).items():
            lines.append(
                f"- {name}: {detail.get('value', 'unavailable')} "
                f"{detail.get('comparison', '?')} {detail.get('threshold', 'unavailable')} "
                f"[{detail.get('status', 'unknown')}]"
            )
        lines.extend(("", "Stages: " + ", ".join(
            name for name, complete in (record.get("stages") or {}).items() if complete
        )))
        self._set_text(self.frame_inspector_summary, "\n".join(lines))
        canvas = self.frame_inspector_canvas
        if canvas is None:
            return
        canvas.delete("all")
        if not source.is_file():
            canvas.create_text(20, 20, anchor="nw", text="Source file is not available.", fill=DARK_TEXT)
            return
        try:
            rgb = read_fits_preview(source, max_width=560, max_height=300)
            self.frame_inspector_photo = self._photo_from_rgb(rgb)
            canvas.create_image(10, 10, image=self.frame_inspector_photo, anchor="nw")
        except (OSError, ValueError, struct.error) as error:
            canvas.create_text(20, 20, anchor="nw", text=f"Preview unavailable: {error}", fill=DARK_TEXT)

    def _build_cohort_balance_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        self._treeview_filter_controls(controls, self.cohort_tree).pack(side="left", padx=(8, 0))
        body = ttk.Panedwindow(parent, orient="vertical")
        body.grid(row=1, column=0, sticky="nsew")
        table_frame = ttk.Frame(body)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("cohort", "total", "stacked", "rejected", "unknown", "retention", "hours")
        self.cohort_tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"cohort": ("Cohort", 380), "total": ("Total", 70), "stacked": ("Stacked", 75), "rejected": ("Rejected", 80), "unknown": ("Unknown", 75), "retention": ("Retention", 85), "hours": ("Exposure h", 95)}
        for column, (heading, width) in headings.items():
            self.cohort_tree.heading(column, text=heading)
            self.cohort_tree.column(column, width=width, minwidth=55, anchor="w")
        self.cohort_tree.grid(row=0, column=0, sticky="nsew")
        body.add(table_frame, weight=2)
        self.cohort_canvas = tk.Canvas(body, background=DARK_FIELD, highlightthickness=1, highlightbackground=DARK_BORDER, height=260)
        body.add(self.cohort_canvas, weight=1)
        self.refresh_cohort_balance()

    def refresh_cohort_balance(self) -> None:
        loaded = self._loaded_report()
        if loaded is None or self.cohort_tree is None:
            return
        report_path, report = loaded
        ledger_path = self._resolve_report_artifact_path(report_path, report.get("frame_ledger_path"))
        records = []
        if ledger_path is not None and ledger_path.is_file():
            try:
                records = read_frame_ledger(ledger_path)
            except (OSError, ValueError, json.JSONDecodeError):
                records = []
        rows = cohort_balance_rows(records)
        for item in self.cohort_tree.get_children():
            self.cohort_tree.delete(item)
        for row in rows:
            self.cohort_tree.insert("", "end", values=(
                row["cohort"], row["total"], row["stacked"], row["rejected"], row["unknown"],
                f"{row['retention_percent']:.1f}%", f"{row['exposure_seconds'] / 3600:.3f}",
            ))
        self._draw_cohort_bars(rows)

    def _draw_cohort_bars(self, rows: list[dict[str, Any]]) -> None:
        canvas = self.cohort_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 520)
        y = 20
        for row in rows[:12]:
            label = str(row["cohort"])
            if len(label) > 48:
                label = label[:45] + "..."
            bar_width = max(width - 230, 240)
            stacked_width = bar_width * row["stacked"] / max(row["total"], 1)
            canvas.create_text(8, y + 8, anchor="w", text=label, fill=DARK_TEXT)
            canvas.create_rectangle(210, y, 210 + bar_width, y + 16, fill="#633f4d", outline="")
            canvas.create_rectangle(210, y, 210 + stacked_width, y + 16, fill="#4b8f8c", outline="")
            canvas.create_text(220 + bar_width, y + 8, anchor="w", text=f"{row['retention_percent']:.1f}%", fill=DARK_TEXT)
            y += 28

    def _build_coverage_inspector_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        self.coverage_canvas = tk.Canvas(parent, background=DARK_FIELD, highlightthickness=1, highlightbackground=DARK_BORDER, height=360)
        self.coverage_canvas.grid(row=1, column=0, sticky="nsew")
        self.coverage_summary = tk.Text(parent, height=10, wrap="word", state="disabled", background=DARK_FIELD, foreground=DARK_TEXT, relief="flat", font=("Consolas", 9))
        self.coverage_summary.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.refresh_coverage_inspector()

    def refresh_coverage_inspector(self) -> None:
        loaded = self._loaded_report()
        if loaded is None:
            self._set_text(self.coverage_summary, "No completed report loaded.")
            return
        report_path, report = loaded
        summary = coverage_report_summary(report)
        lines = [
            f"Run: {report.get('run_id', report_path.stem)}",
            f"Status: {summary['status']}",
            f"Canvas: {summary['width']}x{summary['height']}",
            f"Frames counted: {summary['frames_counted']}",
            f"Maximum integration: {summary['maximum_integration_seconds']} seconds",
            f"Normalization: {summary['normalization_seconds']} seconds",
            f"Crop threshold: {summary['crop_threshold']} seconds at {summary['crop_percent']}% depth",
            f"Crop: {summary['crop_width']}x{summary['crop_height']} ({summary['crop_area_percent']:.2f}% of canvas)" if summary['crop_area_percent'] is not None else "Crop: unavailable",
        ]
        self._set_text(self.coverage_summary, "\n".join(lines))
        canvas = self.coverage_canvas
        if canvas is None:
            return
        canvas.delete("all")
        integration_path = self._resolve_report_artifact_path(report_path, (report.get("coverage") or {}).get("integration_time_path"))
        if integration_path is None or not integration_path.is_file():
            canvas.create_text(20, 20, anchor="nw", text="No integration-time map is recorded.", fill=DARK_TEXT)
            return
        try:
            rgb = read_fits_preview(integration_path, max_width=760, max_height=360)
            self.coverage_photo = self._photo_from_rgb(rgb)
            canvas.create_image(10, 10, image=self.coverage_photo, anchor="nw")
        except (OSError, ValueError, struct.error) as error:
            canvas.create_text(20, 20, anchor="nw", text=f"Coverage preview unavailable: {error}", fill=DARK_TEXT)

    def _build_visual_qa_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Button(controls, text="Run Visual QA", command=self.refresh_visual_qa).pack(side="left")
        self.visual_qa_canvas = tk.Canvas(parent, background=DARK_FIELD, highlightthickness=1, highlightbackground=DARK_BORDER, height=360)
        self.visual_qa_canvas.grid(row=1, column=0, sticky="nsew")
        self.visual_qa_summary = tk.Text(parent, height=12, wrap="word", state="disabled", background=DARK_FIELD, foreground=DARK_TEXT, relief="flat", font=("Consolas", 9))
        self.visual_qa_summary.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.refresh_visual_qa()

    def open_visual_qa_window(self) -> None:
        if self.visual_qa_window is not None:
            try:
                if self.visual_qa_window.winfo_exists():
                    self.visual_qa_window.deiconify()
                    self.visual_qa_window.lift()
                    self.refresh_visual_qa()
                    return
            except tk.TclError:
                pass
        window = tk.Toplevel(self.root)
        window.title("Visual QA")
        window.geometry("900x700")
        window.minsize(700, 540)
        self.visual_qa_window = window
        panel = ttk.Frame(window, padding=10)
        panel.pack(fill="both", expand=True)
        self._build_visual_qa_tab(panel)
        window.protocol("WM_DELETE_WINDOW", self.close_visual_qa_window)

    def close_visual_qa_window(self) -> None:
        window = self.visual_qa_window
        self.visual_qa_window = None
        self.visual_qa_canvas = None
        self.visual_qa_summary = None
        self.visual_qa_photo = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def refresh_visual_qa(self) -> None:
        loaded = self._loaded_report()
        if loaded is None:
            self._set_text(self.visual_qa_summary, "No completed report loaded.")
            return
        report_path, report = loaded
        master_path = self._resolve_report_artifact_path(report_path, (report.get("master") or {}).get("path"))
        coverage = report.get("coverage") or {}
        coverage_path = self._resolve_report_artifact_path(report_path, coverage.get("integration_time_path", coverage.get("path")))
        crop_path = self._resolve_report_artifact_path(report_path, coverage.get("cropped_master_path"))
        try:
            result = visual_quality_check(master_path, coverage_path, crop_path) if master_path else {"status": "FAIL", "checks": [{"name": "master_artifact", "status": "FAIL", "detail": "No master path recorded."}]}
        except (OSError, ValueError, struct.error) as error:
            result = {"status": "FAIL", "checks": [{"name": "visual_qa", "status": "FAIL", "detail": str(error)}]}
        lines = [
            "VISUAL QA",
            f"Run: {report.get('run_id', report_path.stem)}",
            f"Overall: {result.get('status', 'unknown')}",
            "",
        ]
        for check in result.get("checks", []):
            lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
        lines.append("\nThese are preview-level diagnostics, not a substitute for visual inspection of the full-resolution master.")
        self._set_text(self.visual_qa_summary, "\n".join(lines))
        canvas = self.visual_qa_canvas
        if canvas is None:
            return
        canvas.delete("all")
        if master_path is None or not master_path.is_file():
            canvas.create_text(20, 20, anchor="nw", text="Master preview unavailable.", fill=DARK_TEXT)
            return
        try:
            rgb = read_fits_preview(master_path, max_width=760, max_height=360)
            self.visual_qa_photo = self._photo_from_rgb(rgb)
            canvas.create_image(10, 10, image=self.visual_qa_photo, anchor="nw")
        except (OSError, ValueError, struct.error) as error:
            canvas.create_text(20, 20, anchor="nw", text=f"Master preview unavailable: {error}", fill=DARK_TEXT)

    def _build_batch_queue_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Button(controls, text="Add Current", command=self.add_current_batch_job).pack(side="left")
        ttk.Button(controls, text="Add Folder", command=self.add_batch_folder).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Remove Selected", command=self.remove_batch_job).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Clear Queue", command=self.clear_batch_queue).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Start Queue", command=self.start_batch_queue).pack(side="left", padx=(8, 0))
        self._treeview_filter_controls(controls, self.batch_tree).pack(side="left", padx=(8, 0))
        table_frame = ttk.Frame(parent)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("status", "workdir", "output")
        self.batch_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        for column, heading, width in (("status", "Status", 100), ("workdir", "Input folder", 480), ("output", "Output folder", 480)):
            self.batch_tree.heading(column, text=heading)
            self.batch_tree.column(column, width=width, minwidth=100, anchor="w")
        self.batch_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.batch_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.batch_tree.configure(yscrollcommand=scrollbar.set)
        self._set_batch_summary("Queue is empty. Jobs run sequentially with the current processing settings.")

    def _set_batch_summary(self, text: str) -> None:
        self.status.set(text)

    def _refresh_batch_tree(self) -> None:
        if self.batch_tree is None:
            return
        for item in self.batch_tree.get_children():
            self.batch_tree.delete(item)
        for index, job in enumerate(self.batch_jobs):
            self.batch_tree.insert("", "end", iid=str(index), values=(job.get("status", "queued"), job["workdir"], job["output_dir"]))

    def add_current_batch_job(self) -> None:
        workdir = Path(self.workdir.get().strip()).expanduser().resolve()
        output = Path(self.output_dir.get().strip()).expanduser().resolve()
        if not workdir.is_dir():
            messagebox.showerror("Batch Queue", "Choose an existing input folder first.")
            return
        self.batch_jobs.append({"workdir": str(workdir), "output_dir": str(output), "status": "queued"})
        self._refresh_batch_tree()

    def add_batch_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose batch input folder")
        if not selected:
            return
        workdir = Path(selected).expanduser().resolve()
        output = workdir / "Siril Mosaic Output"
        self.batch_jobs.append({"workdir": str(workdir), "output_dir": str(output), "status": "queued"})
        self._refresh_batch_tree()

    def remove_batch_job(self) -> None:
        if self.batch_tree is None:
            return
        selected = sorted((int(item) for item in self.batch_tree.selection()), reverse=True)
        for index in selected:
            if 0 <= index < len(self.batch_jobs) and self.batch_jobs[index].get("status") != "running":
                self.batch_jobs.pop(index)
        self._refresh_batch_tree()

    def clear_batch_queue(self) -> None:
        if self.batch_running:
            messagebox.showwarning("Batch Queue", "Wait for the active batch job to finish or cancel it first.")
            return
        self.batch_jobs.clear()
        self._refresh_batch_tree()

    def _batch_command_for_paths(self, workdir: Path, output_dir: Path) -> list[str]:
        original_workdir = self.workdir.get()
        original_output = self.output_dir.get()
        try:
            self.workdir.set(str(workdir))
            self.output_dir.set(str(output_dir))
            return self.command()
        finally:
            self.workdir.set(original_workdir)
            self.output_dir.set(original_output)

    def start_batch_queue(self) -> None:
        if self.batch_running:
            messagebox.showwarning("Batch Queue", "A batch queue is already running.")
            return
        pending = [job for job in self.batch_jobs if job.get("status") in {"queued", "failed"}]
        if not pending:
            messagebox.showwarning("Batch Queue", "Add at least one queued folder first.")
            return
        if not messagebox.askyesno("Start Batch Queue", f"Process {len(pending)} folder(s) sequentially using the current settings?"):
            return
        for job in pending:
            try:
                job["command"] = self._batch_command_for_paths(Path(job["workdir"]), Path(job["output_dir"]))
            except (tk.TclError, ValueError) as error:
                messagebox.showerror("Batch Queue", str(error))
                return
        self.batch_running = True
        self.running = True
        self.cancelling = False
        self.start_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set("Batch queue starting...")

        def worker() -> None:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            for index, job in enumerate(self.batch_jobs):
                if job.get("status") not in {"queued", "failed"}:
                    continue
                workdir = Path(job["workdir"])
                output_dir = Path(job["output_dir"])
                log_dir = workdir / "siril_mosaic_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                stamp = f"{datetime.now():%Y%m%d_%H%M%S}_{index + 1:02d}"
                log_path = log_dir / f"batch_{stamp}.log"
                cancel_path = log_dir / f"batch_{stamp}.cancel"
                try:
                    command = list(job["command"])
                    command.extend(("--cancel-file", str(cancel_path)))
                    self.cancel_path = cancel_path
                    self.log_path = log_path
                    self._queue_event("batch_status", (index, "running", str(log_path)))
                    with log_path.open("w", encoding="utf-8", buffering=1) as log_file:
                        self.process = subprocess.Popen(
                            command,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            bufsize=1,
                            creationflags=creation_flags,
                        )
                        assert self.process.stdout is not None
                        for line in self.process.stdout:
                            message = line.rstrip()
                            log_file.write(message + "\n")
                            self._queue_event("batch_log", (index, message))
                        return_code = self.process.wait()
                        log_file.write(f"\nExit code: {return_code}\n")
                    self._queue_event("batch_status", (index, "complete" if return_code == 0 else "failed", str(log_path), return_code))
                    if return_code == 2:
                        break
                except Exception as error:
                    self._queue_event("batch_status", (index, "failed", str(log_path), error))
                finally:
                    self.process = None
                    cancel_path.unlink(missing_ok=True)
            self._queue_event("batch_finished", None)

        Thread(target=worker, daemon=True).start()

    def refresh_analysis_tabs(self) -> None:
        self.refresh_threshold_lab()
        self.refresh_quality_explorer()
        self.refresh_frame_inspector()
        self.refresh_cohort_balance()
        self.refresh_coverage_inspector()
        self.refresh_visual_qa()

    @staticmethod
    def _verification_text(verification: dict[str, Any]) -> str:
        lines = [
            f"Run verification: {verification['status']}",
            f"Report: {verification.get('report_path', 'unknown')}",
            f"Artifact: {verification_artifact_path(verification['report_path'])}",
            f"Run ID: {verification.get('run_id', 'unknown')}",
            "",
        ]
        for check in verification.get("checks", []):
            lines.append(
                f"[{check['status']}] {check['name']}: {check['detail']}"
            )
        return "\n".join(lines)

    def verify_loaded_run(self) -> None:
        report_path = self.quality_report_path
        if report_path is None or not report_path.is_file():
            crop_report = Path(self.crop_report.get().strip()).expanduser()
            report_path = crop_report if crop_report.is_file() else None
        if report_path is None:
            self.verification_status.set("No completed report loaded")
            messagebox.showerror(
                "Run Verification",
                "Load or complete a quality report before verifying the run.",
            )
            return
        verification = verify_run_report(report_path)
        counts = verification["counts"]
        self.verification_status.set(
            f"{verification['status']} ({counts['PASS']} pass, "
            f"{counts['WARN']} warn, {counts['FAIL']} fail)"
        )
        dialog = tk.Toplevel(self.root)
        dialog.title("Run Verification")
        dialog.geometry("980x650")
        dialog.minsize(720, 450)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        details = tk.Text(
            dialog,
            wrap="word",
            state="normal",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            insertbackground=DARK_TEXT,
            relief="flat",
            font=("Consolas", 9),
        )
        details.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=12)
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=details.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 12), pady=12)
        details.configure(yscrollcommand=scrollbar.set)
        details.insert("1.0", self._verification_text(verification))
        details.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).grid(
            row=1, column=0, columnspan=2, pady=(0, 12)
        )

    def _loaded_report(self) -> tuple[Path, dict[str, Any]] | None:
        report_path = self.quality_report_path
        if report_path is None or not report_path.is_file():
            candidate = Path(self.crop_report.get().strip()).expanduser()
            report_path = candidate if candidate.is_file() else None
        if report_path is None:
            return None
        try:
            return report_path, json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _replay_text(result: dict[str, Any]) -> str:
        predicted = result["predicted"]
        lines = [
            "THRESHOLD REPLAY",
            f"Ledger: {result['ledger_path']}",
            f"Records: {result['record_count']}",
            "",
            f"Predicted selected: {predicted['selected_frames']}",
            f"Predicted excluded: {predicted['excluded_frames']}",
            f"Predicted unknown: {predicted['unknown_frames']}",
            f"Predicted exposure: {predicted['integrated_hours']:.3f} hours "
            f"({predicted['integrated_exposure_seconds']:.1f} seconds)",
            f"Current statuses: {result['current_status_counts']}",
            f"Current selected: {result['comparison']['current_selected_frames']} frames "
            f"({result['comparison']['current_exposure_seconds']:.1f}s)",
            f"Proposed delta: {result['comparison']['selected_frame_delta']:+d} frames, "
            f"{result['comparison']['exposure_delta_seconds']:+.1f}s",
            "",
            "Derived thresholds:",
        ]
        for name, detail in result["thresholds"].items():
            lines.append(
                f"- {name}: {detail['threshold']:.6g} {detail['comparison']} "
                f"({detail['keep_percent']:.0f}% keep target; "
                f"{detail['measured_values']} measured)"
            )
        if result.get("unavailable_metrics"):
            lines.extend(("", "Unavailable metrics: " + ", ".join(result["unavailable_metrics"])))
        lines.extend(("", "By cohort:"))
        for cohort, detail in result["cohorts"].items():
            comparison = result.get("cohort_comparison", {}).get(cohort, {})
            warning = " WARNING: disproportionate retention loss" if comparison.get("warning") else ""
            lines.append(
                f"- {cohort}: {detail['selected']} selected, {detail['excluded']} excluded, "
                f"{detail['unknown']} unknown, {detail['exposure_seconds']:.1f}s selected exposure; "
                f"current {comparison.get('current_selected', 0)}, "
                f"delta {comparison.get('selected_delta', 0):+d}{warning}"
            )
        coverage = result.get("coverage_impact", {})
        if coverage.get("available"):
            lines.extend((
                "",
                "Coverage impact estimate (registered placement rectangles):",
                f"- Current footprint: {coverage['current']['width']}x{coverage['current']['height']} "
                f"({coverage['current']['area_percent']:.3f}% coarse area)",
                f"- Proposed footprint: {coverage['predicted']['width']}x{coverage['predicted']['height']} "
                f"({coverage['predicted']['area_percent']:.3f}% coarse area)",
            ))
        else:
            lines.extend(("", "Coverage impact: unavailable", f"- {coverage.get('reason', 'No placement geometry recorded.')}"))
        lines.extend(("", "This is a ledger-based prediction; Siril registration and stacking were not rerun."))
        return "\n".join(lines)

    @staticmethod
    def _integrity_text(result: dict[str, Any]) -> str:
        lines = [
            "PREFLIGHT INTEGRITY SCAN",
            f"Status: {result['status']}",
            f"Root: {result['root_folder']}",
            f"Inputs: {result['input_count']}",
            f"Input bytes: {result['total_bytes']}",
            f"Cache: {'reused' if result.get('cached') else 'written'}"
            + (f" ({result.get('cache_path')})" if result.get('cache_path') else ""),
            "",
        ]
        for check in result.get("checks", []):
            lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
            if check.get("groups"):
                for group in check["groups"]:
                    lines.append("  - " + ", ".join(group))
        lines.append("\nNo files were modified by this scan.")
        return "\n".join(lines)

    def _open_analysis_dialog(self, title: str, initial_text: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("980x700")
        dialog.minsize(720, 480)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        text = tk.Text(
            dialog,
            wrap="word",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            insertbackground=DARK_TEXT,
            relief="flat",
            font=("Consolas", 9),
        )
        text.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=12)
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 12), pady=12)
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", initial_text)
        text.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).grid(
            row=1, column=0, columnspan=2, pady=(0, 12)
        )
        self.analysis_dialog = dialog
        self.analysis_text = text

    def _loaded_ledger_path(self) -> Path | None:
        loaded = self._loaded_report()
        if loaded is None:
            return None
        report_path, report = loaded
        value = report.get("frame_ledger_path")
        if not value:
            return None
        path = Path(str(value))
        return path if path.is_absolute() else report_path.parent / path

    def _filtered_ledger_rows(self) -> list[dict[str, Any]]:
        status = self.ledger_status_filter.get()
        metric = self.ledger_metric_filter.get()
        search = self.ledger_search.get().strip().casefold()
        filtered = []
        for row in self.ledger_rows:
            if status != "All" and row.get("status") != status.casefold():
                continue
            metrics = row.get("filter_metrics", {})
            if metric == "Any failed" and not any(
                detail.get("status") == "failed" for detail in metrics.values()
            ):
                continue
            if metric not in {"All", "Any failed"}:
                if metrics.get(metric.casefold(), {}).get("status") != "failed":
                    continue
            if search and search not in str(row.get("file", "")).casefold():
                continue
            filtered.append(row)
        return filtered

    def _populate_ledger_tree(self) -> None:
        if self.ledger_tree is None:
            return
        for item in self.ledger_tree.get_children():
            self.ledger_tree.delete(item)
        for row in self._filtered_ledger_rows():
            failed = [name for name, detail in row.get("filter_metrics", {}).items() if detail.get("status") == "failed"]
            missing = [name for name, detail in row.get("filter_metrics", {}).items() if detail.get("status") == "unavailable"]
            self.ledger_tree.insert(
                "", "end",
                values=(
                    row.get("file", ""),
                    row.get("status", ""),
                    row.get("cohort") or "",
                    row.get("substack") or "",
                    row.get("exposure_seconds") or "",
                    row.get("stack_membership") or "",
                    ", ".join(failed),
                    ", ".join(missing),
                ),
            )

    def open_ledger_viewer(self) -> None:
        ledger_path = self._loaded_ledger_path()
        if ledger_path is None or not ledger_path.is_file():
            messagebox.showerror("Frame Ledger", "Load a completed report with a frame ledger first.")
            return
        try:
            self.ledger_rows = read_frame_ledger(ledger_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("Frame Ledger", f"Could not read the ledger:\n\n{error}")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Frame Ledger")
        dialog.geometry("1250x720")
        dialog.minsize(900, 500)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        controls = ttk.Frame(dialog, padding=10)
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Label(controls, text="Status").grid(row=0, column=0, padx=(0, 6))
        ttk.Combobox(
            controls, textvariable=self.ledger_status_filter,
            values=("All", "stacked", "registered", "rejected", "failed"),
            state="readonly", width=12,
        ).grid(row=0, column=1, padx=(0, 12))
        ttk.Label(controls, text="Metric").grid(row=0, column=2, padx=(0, 6))
        ttk.Combobox(
            controls, textvariable=self.ledger_metric_filter,
            values=("All", "Any failed", "FWHM", "Roundness", "Background", "Stars"),
            state="readonly", width=14,
        ).grid(row=0, column=3, padx=(0, 12))
        ttk.Label(controls, text="File contains").grid(row=0, column=4, padx=(0, 6))
        ttk.Entry(controls, textvariable=self.ledger_search, width=28).grid(row=0, column=5, padx=(0, 12))
        tree_frame = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ("file", "status", "cohort", "substack", "exposure", "membership", "failed", "missing")
        self.ledger_tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        headings = {
            "file": ("Frame", 360), "status": ("Status", 95), "cohort": ("Cohort", 190),
            "substack": ("Substack", 80), "exposure": ("Exposure s", 90),
            "membership": ("Stack membership", 140), "failed": ("Failed metrics", 150),
            "missing": ("Unavailable metrics", 170),
        }
        for column, (heading, width) in headings.items():
            self.ledger_tree.heading(column, text=heading)
            self.ledger_tree.column(column, width=width, minwidth=60, anchor="w")
        self._make_treeview_sortable(self.ledger_tree)
        self.ledger_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(tree_frame, orient="vertical", command=self.ledger_tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.ledger_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.ledger_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self._treeview_filter_controls(controls, self.ledger_tree).grid(row=0, column=6, padx=(8, 0), sticky="w")
        for variable in (self.ledger_status_filter, self.ledger_metric_filter, self.ledger_search):
            variable.trace_add("write", lambda *_args: self._populate_ledger_tree())
        self._populate_ledger_tree()
        ttk.Button(dialog, text="Close", command=dialog.destroy).grid(
            row=2, column=0, pady=(0, 10)
        )

    def export_ledger_csv(self) -> None:
        ledger_path = self._loaded_ledger_path()
        if ledger_path is None or not ledger_path.is_file():
            messagebox.showerror("Frame Ledger", "Load a completed report with a frame ledger first.")
            return
        try:
            self.ledger_rows = read_frame_ledger(ledger_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("Frame Ledger", f"Could not read the ledger:\n\n{error}")
            return
        destination = filedialog.asksaveasfilename(
            title="Export frame ledger CSV",
            defaultextension=".csv",
            filetypes=(("CSV", "*.csv"), ("All files", "*.*")),
        )
        if not destination:
            return
        fields = (
            "file", "status", "cohort", "substack", "exposure_seconds",
            "stack_membership", "failed_metrics", "missing_metrics",
        )
        try:
            with Path(destination).open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in self._filtered_ledger_rows():
                    metrics = row.get("filter_metrics", {})
                    writer.writerow({
                        "file": row.get("file", ""),
                        "status": row.get("status", ""),
                        "cohort": row.get("cohort", ""),
                        "substack": row.get("substack", ""),
                        "exposure_seconds": row.get("exposure_seconds", ""),
                        "stack_membership": row.get("stack_membership", ""),
                        "failed_metrics": ", ".join(
                            name for name, detail in metrics.items() if detail.get("status") == "failed"
                        ),
                        "missing_metrics": ", ".join(
                            name for name, detail in metrics.items() if detail.get("status") == "unavailable"
                        ),
                    })
        except OSError as error:
            messagebox.showerror("Frame Ledger", f"Could not export CSV:\n\n{error}")
            return
        self.status.set(f"Exported ledger CSV: {destination}")

    def export_run_bundle(self) -> None:
        loaded = self._loaded_report()
        if loaded is None:
            messagebox.showerror("Run Bundle", "Load a completed quality report first.")
            return
        report_path, _report = loaded
        destination = filedialog.asksaveasfilename(
            title="Export run evidence bundle",
            initialfile=f"{report_path.stem}_bundle.zip",
            defaultextension=".zip",
            filetypes=(("ZIP", "*.zip"), ("All files", "*.*")),
        )
        if not destination:
            return
        try:
            bundle_path = create_run_bundle(report_path, destination)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("Run Bundle", f"Could not create bundle:\n\n{error}")
            return
        self.status.set(f"Exported run bundle: {bundle_path}")

    def show_checkpoint_status(self) -> None:
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        info = inspect_checkpoint(output_dir, workdir)
        lines = [
            "CHECKPOINT STATUS",
            f"Status: {info['status']}",
            f"Path: {info['path']}",
        ]
        if info.get("age_seconds") is not None:
            lines.append(f"Age: {info['age_seconds'] / 3600:.2f} hours")
        for check in info.get("checks", []):
            lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
        if info["status"] == "NONE":
            lines.append("\nNo interrupted run is available to resume.")
        elif info["status"] == "INVALID":
            lines.append("\nDo not resume until the failed checkpoint checks are resolved.")
        else:
            lines.append("\nResume validates the checkpoint again before moving any files.")
        messagebox.showinfo("Checkpoint Status", "\n".join(lines))

    def discard_checkpoint(self) -> None:
        checkpoint = Path(self.output_dir.get().strip()).expanduser() / "run_checkpoint.json"
        if not checkpoint.is_file():
            messagebox.showinfo("Discard Checkpoint", "No checkpoint was found.")
            return
        if not messagebox.askyesno(
            "Discard Checkpoint",
            "Discard this checkpoint and start fresh?\n\n"
            "Only the checkpoint metadata will be removed. Staged source files are left untouched.",
        ):
            return
        checkpoint.unlink(missing_ok=True)
        self.status.set("Checkpoint discarded; staged files were not removed.")

    def abandon_run(self) -> None:
        if self.running:
            messagebox.showwarning("Abandon Run", "Wait for the active run to finish or cancel it first.")
            return
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        if not (workdir / "Lights_sorted").exists() and not (output_dir / "run_checkpoint.json").is_file():
            messagebox.showinfo("Abandon Run", "No checkpoint or staged source folder was found.")
            return
        if not messagebox.askyesno(
            "Abandon Run",
            "Restore all staged source files and remove temporary recovery state?\n\n"
            "Reports and logs will be retained. This cannot be undone.",
        ):
            return
        try:
            result = abandon_interrupted_run(workdir, output_dir)
        except InterruptedRunRecoveryError as error:
            if not error.orphan_cleanup_allowed:
                messagebox.showerror(
                    "Abandon Run",
                    f"Recovery was not completed; no fresh run was started:\n\n{error}",
                )
                return
            missing_paths = "\n".join(f"  {path}" for path in error.missing_sources)
            if not messagebox.askyesno(
                "Abandon Run: Missing Sources",
                f"{len(error.missing_sources)} manifest-referenced source file(s) are already missing.\n\n"
                f"{missing_paths}\n\n"
                "No staged frame/process payloads or checkpoint remain. Remove only the empty recovery state?\n"
                "The listed files will not be restored, and reports/logs will be retained.",
            ):
                return
            try:
                result = abandon_interrupted_run(workdir, output_dir, allow_missing=True)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as cleanup_error:
                messagebox.showerror(
                    "Abandon Run",
                    f"Recovery was not completed; no fresh run was started:\n\n{cleanup_error}",
                )
                return
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("Abandon Run", f"Recovery was not completed; no fresh run was started:\n\n{error}")
            return
        if result.get("missing"):
            self.status.set(
                f"Abandoned orphaned run; removed empty recovery state. "
                f"{len(result['missing'])} source file(s) were already missing and were not deleted. "
                "Reports and logs were retained."
            )
        else:
            self.status.set(
                f"Abandoned run; restored {result['restored']} source file(s). Reports and logs were retained."
            )

    def show_run_lock_status(self) -> None:
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        info = inspect_run_lock(output_dir)
        lines = [
            "RUN LOCK STATUS",
            f"Status: {info['status']}",
            f"Path: {info['path']}",
        ]
        if info.get("age_seconds") is not None:
            lines.append(f"Age: {info['age_seconds'] / 3600:.2f} hours")
        if info.get("lock"):
            lines.extend(f"{key}: {value}" for key, value in info["lock"].items())
        if info.get("error"):
            lines.append(f"Error: {info['error']}")
        messagebox.showinfo("Run Lock Status", "\n".join(lines))

    def break_run_lock(self) -> None:
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        info = inspect_run_lock(output_dir)
        if info["status"] == "NONE":
            messagebox.showinfo("Break Run Lock", "No run lock was found.")
            return
        if not messagebox.askyesno(
            "Break Run Lock",
            "Remove this run lock?\n\nConfirm that no stacker or Siril process is active first.",
        ):
            return
        break_backend_run_lock(output_dir)
        self.status.set("Run lock removed; verify no process is still active before starting.")

    def show_cleanup_review(self) -> None:
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        candidates = stale_artifacts(workdir, output_dir)
        if not candidates:
            messagebox.showinfo("Cleanup Review", "No stale processing artifacts were found.")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Cleanup Review")
        dialog.geometry("980x460")
        dialog.minsize(700, 320)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        tree = ttk.Treeview(dialog, columns=("kind", "age", "safe", "path"), show="headings", selectmode="extended")
        for column, heading, width in (
            ("kind", "Kind", 160), ("age", "Age", 100), ("safe", "Deletion", 120), ("path", "Path", 560)
        ):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=80, anchor="w")
        self._make_treeview_sortable(tree)
        tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=10)
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=10)
        tree.configure(yscrollcommand=scrollbar.set)
        candidate_by_item = {}
        for candidate in candidates:
            item = tree.insert("", "end", values=(
                candidate["kind"],
                f"{candidate['age_seconds'] / 3600:.1f}h",
                "allowed" if candidate.get("safe_to_delete") else "review only",
                candidate["path"],
            ))
            candidate_by_item[item] = candidate

        def delete_selected() -> None:
            selected = [candidate_by_item[item] for item in tree.selection()]
            if not selected:
                messagebox.showwarning("Cleanup Review", "Select one or more artifacts first.", parent=dialog)
                return
            if any(not item.get("safe_to_delete") for item in selected):
                messagebox.showwarning(
                    "Cleanup Review",
                    "The selection includes review-only paths. Lights_sorted and source-bearing paths cannot be deleted here.",
                    parent=dialog,
                )
                return
            if not messagebox.askyesno(
                "Confirm Cleanup",
                f"Delete {len(selected)} approved temporary artifact(s)?\n\nRaw lights and rejects are never included.",
                parent=dialog,
            ):
                return
            try:
                delete_stale_artifacts(workdir, output_dir, [item["path"] for item in selected])
            except (OSError, ValueError) as error:
                messagebox.showerror("Cleanup Review", f"Cleanup failed:\n\n{error}", parent=dialog)
                return
            dialog.destroy()
            self.status.set(f"Deleted {len(selected)} approved temporary artifact(s).")

        buttons = ttk.Frame(dialog)
        buttons.grid(row=1, column=0, columnspan=2, pady=(0, 10))
        self._treeview_filter_controls(buttons, tree).pack(side="left", padx=5)
        ttk.Button(buttons, text="Delete Selected", command=delete_selected).pack(side="left", padx=5)
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="left", padx=5)

    def replay_loaded_ledger(self) -> None:
        loaded = self._loaded_report()
        if loaded is None:
            messagebox.showerror("Threshold Replay", "Load a completed quality report first.")
            return
        report_path, report = loaded
        ledger_value = report.get("frame_ledger_path")
        ledger_path = Path(str(ledger_value)) if ledger_value else None
        if ledger_path is not None and not ledger_path.is_absolute():
            ledger_path = report_path.parent / ledger_path
        if ledger_path is None or not ledger_path.is_file():
            messagebox.showerror("Threshold Replay", "This report has no usable frame ledger.")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Threshold Replay")
        dialog.geometry("980x760")
        dialog.minsize(760, 520)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(2, weight=1)
        controls = ttk.LabelFrame(dialog, text="Keep-best targets (%)", padding=10)
        controls.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        variables = {name: tk.IntVar(value=80) for name in ("background", "roundness", "fwhm", "stars")}
        for column, name in enumerate(variables):
            ttk.Label(controls, text=name.title()).grid(row=0, column=column * 2, padx=(0, 6))
            ttk.Spinbox(controls, from_=1, to=100, increment=1, textvariable=variables[name], width=7).grid(
                row=0, column=column * 2 + 1, padx=(0, 12)
            )
        result_text = tk.Text(
            dialog, wrap="word", state="disabled", background=DARK_FIELD,
            foreground=DARK_TEXT, insertbackground=DARK_TEXT, relief="flat",
            font=("Consolas", 9),
        )
        result_text.grid(row=2, column=0, sticky="nsew", padx=(12, 0), pady=(0, 12))
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=result_text.yview)
        scrollbar.grid(row=2, column=1, sticky="ns", padx=(0, 12), pady=(0, 12))
        result_text.configure(yscrollcommand=scrollbar.set)

        def run_replay() -> None:
            try:
                result = replay_frame_ledger(ledger_path, {name: variable.get() for name, variable in variables.items()})
                text = self._replay_text(result)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                text = f"Threshold replay unavailable:\n{error}"
            result_text.configure(state="normal")
            result_text.delete("1.0", "end")
            result_text.insert("1.0", text)
            result_text.configure(state="disabled")

        ttk.Button(dialog, text="Replay", command=run_replay).grid(row=1, column=0, padx=12, pady=(0, 8), sticky="w")
        run_replay()

    def start_integrity_scan(self) -> None:
        self._start_integrity_scan(preview_callback=None)

    def _start_integrity_scan(self, preview_callback) -> None:
        if self.integrity_scan_running:
            return
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        if not workdir.is_dir():
            messagebox.showerror("Integrity Scan", "Choose an existing input folder first.")
            return
        self.integrity_scan_running = True
        if preview_callback is None:
            self._open_analysis_dialog("Preflight Integrity Scan", "Scanning inputs...\nThis may take a while for large datasets.")
        pattern = "auto" if self.bayer_pattern.get() == "Auto (header)" else self.bayer_pattern.get()
        orientation = self.bayer_orientation.get().lower()
        drizzle = self.drizzle.get()

        def worker() -> None:
            try:
                result = scan_input_integrity(
                    workdir, output_dir,
                    bayer_pattern=pattern.lower(),
                    orientation=orientation,
                    drizzle=drizzle,
                    deep_payload=True,
                )
            except Exception as error:
                result = {
                    "status": "FAIL", "root_folder": str(workdir),
                    "input_count": 0, "total_bytes": 0,
                    "counts": {"PASS": 0, "WARN": 0, "FAIL": 1},
                    "checks": [{"status": "FAIL", "name": "scan", "detail": str(error)}],
                }
            self._queue_event("integrity_done", (result, preview_callback))

        Thread(target=worker, daemon=True).start()

    def _build_crop_workbench_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        source = ttk.LabelFrame(tab, text="Completed run", padding=12)
        source.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        source.columnconfigure(1, weight=1)
        ttk.Label(source, text="Quality report").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(source, textvariable=self.crop_report).grid(row=0, column=1, sticky="ew")
        ttk.Button(source, text="Browse...", command=self.choose_crop_report).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(source, text="Load", command=self.load_crop_report).grid(
            row=0, column=3, padx=(8, 0)
        )
        ttk.Button(source, text="Latest", command=self.load_latest_crop_report).grid(
            row=0, column=4, padx=(8, 0)
        )

        controls = ttk.LabelFrame(tab, text="Crop selection", padding=12)
        controls.grid(row=1, column=0, sticky="new", padx=12, pady=(0, 8))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Master artifact").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.crop_artifact_combo = ttk.Combobox(
            controls,
            textvariable=self.crop_artifact,
            state="readonly",
        )
        self.crop_artifact_combo.grid(row=0, column=1, columnspan=3, sticky="ew")
        self.crop_artifact_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.preview_crop()
        )
        ttk.Label(controls, text="Crop depth (%)").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0)
        )
        ttk.Spinbox(
            controls,
            from_=1,
            to=100,
            increment=1,
            textvariable=self.crop_percent,
            width=10,
            command=self.preview_crop,
        ).grid(row=1, column=1, sticky="w", pady=(10, 0))
        for column, value in enumerate((25, 50, 75, 100), 2):
            ttk.Button(
                controls,
                text=f"{value}%",
                command=lambda value=value: self.set_crop_percent(value),
                width=6,
            ).grid(row=1, column=column, padx=(8, 0), pady=(10, 0))

        actions = ttk.Frame(tab, padding=(12, 0, 12, 8))
        actions.grid(row=2, column=0, sticky="ew")
        self.crop_preview_button = ttk.Button(
            actions, text="Preview Crop", command=self.preview_crop
        )
        self.crop_preview_button.grid(row=0, column=0)
        self.crop_visual_button = ttk.Button(
            actions, text="Open Visual Preview", command=self.open_crop_visual_preview
        )
        self.crop_visual_button.grid(row=0, column=1, padx=(8, 0))
        self.crop_create_button = ttk.Button(
            actions, text="Create Cropped Master", command=self.create_crop
        )
        self.crop_create_button.grid(row=0, column=2, padx=(8, 0))

        preview_frame = ttk.LabelFrame(tab, text="Crop preview", padding=8)
        preview_frame.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.crop_summary = tk.Text(
            preview_frame,
            height=12,
            wrap="word",
            state="disabled",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            insertbackground=DARK_TEXT,
            relief="flat",
            font=("Consolas", 9),
        )
        self.crop_summary.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(preview_frame, orient="vertical", command=self.crop_summary.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.crop_summary.configure(yscrollcommand=scrollbar.set)
        self._set_crop_summary(
            "Load a completed quality report with an integration-time map to begin."
        )

    def _set_crop_summary(self, text: str) -> None:
        if self.crop_summary is None:
            return
        self.crop_summary.configure(state="normal")
        self.crop_summary.delete("1.0", "end")
        self.crop_summary.insert("1.0", text)
        self.crop_summary.configure(state="disabled")

    @staticmethod
    def _photo_from_rgb(rgb: Any) -> tk.PhotoImage:
        height, width, _channels = rgb.shape
        ppm = f"P6\n{width} {height}\n255\n".encode("ascii") + rgb.tobytes(order="C")
        return tk.PhotoImage(
            data=ppm.decode("latin1"),
            format="PPM",
        )

    def _apply_crop_visuals(self, full_rgb: Any, crop_rgb: Any, plan: dict[str, Any]) -> None:
        self.crop_full_rgb = full_rgb
        self.crop_region_rgb = crop_rgb

    def open_crop_visual_preview(self) -> None:
        if self.crop_full_rgb is None or self.crop_region_rgb is None or self.crop_plan is None:
            messagebox.showinfo(
                "Cropping Workbench",
                "Run Preview Crop and wait for the visual preview to finish first.",
            )
            return
        plan = self.crop_plan
        dialog = tk.Toplevel(self.root)
        dialog.title("Crop Visual Preview")
        dialog.geometry(
            f"{min(1500, max(900, self.root.winfo_screenwidth() - 80))}x"
            f"{min(900, max(600, self.root.winfo_screenheight() - 120))}"
        )
        dialog.minsize(900, 600)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text=(
                f"{plan['crop_coverage_percent']:.0f}% depth | "
                f"{plan['cropped_width']} x {plan['cropped_height']} crop from "
                f"{plan['master_width']} x {plan['master_height']} master"
            ),
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 6))
        visual_frame = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        visual_frame.grid(row=1, column=0, sticky="nsew")
        visual_frame.columnconfigure(0, weight=1)
        visual_frame.columnconfigure(1, weight=1)
        visual_frame.rowconfigure(1, weight=1)
        ttk.Label(visual_frame, text="Full master and proposed crop").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6)
        )
        ttk.Label(visual_frame, text="Selected crop preview").grid(
            row=0, column=1, sticky="w", padx=(8, 0), pady=(0, 6)
        )
        full_canvas = tk.Canvas(
            visual_frame,
            width=700,
            height=520,
            background="#050608",
            highlightthickness=1,
            highlightbackground=DARK_BORDER,
        )
        full_canvas.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        crop_canvas = tk.Canvas(
            visual_frame,
            width=700,
            height=520,
            background="#050608",
            highlightthickness=1,
            highlightbackground=DARK_BORDER,
        )
        crop_canvas.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        full_photo = self._photo_from_rgb(self.crop_full_rgb)
        crop_photo = self._photo_from_rgb(self.crop_region_rgb)
        popup_key = id(dialog)
        self.crop_popup_photos[popup_key] = (full_photo, crop_photo)
        dialog.bind(
            "<Destroy>",
            lambda _event, key=popup_key: self.crop_popup_photos.pop(key, None),
            add="+",
        )
        full_canvas.create_image(0, 0, anchor="nw", image=full_photo)
        image_height, image_width, _ = self.crop_full_rgb.shape
        bounds = plan["crop_bounds"]
        scale_x = image_width / plan["master_width"]
        scale_y = image_height / plan["master_height"]
        full_canvas.create_rectangle(
            bounds["x"] * scale_x,
            bounds["y"] * scale_y,
            (bounds["x"] + bounds["width"]) * scale_x,
            (bounds["y"] + bounds["height"]) * scale_y,
            outline="#ffd166",
            width=3,
        )
        crop_canvas.create_image(0, 0, anchor="nw", image=crop_photo)
        ttk.Button(dialog, text="Close", command=dialog.destroy).grid(
            row=2, column=0, pady=(0, 12)
        )

    @staticmethod
    def _resolve_report_artifact(report_path: Path, value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        return path if path.is_absolute() else report_path.parent / path

    def _crop_artifacts_from_report(self, report_path: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        masters = report.get("masters") or []
        coverages = report.get("coverages") or []
        if masters:
            for index, master in enumerate(masters):
                cohort = master.get("cohort")
                coverage = next(
                    (item for item in coverages if cohort and item.get("cohort") == cohort),
                    coverages[index] if index < len(coverages) else {},
                )
                master_path = self._resolve_report_artifact(report_path, master.get("path"))
                coverage_path = self._resolve_report_artifact(
                    report_path,
                    coverage.get("integration_time_path", coverage.get("path")),
                )
                if master_path and coverage_path:
                    label = f"{index + 1}: {master_path.name}"
                    if cohort:
                        label += f" | {cohort}"
                    artifacts.append({
                        "label": label,
                        "master_path": master_path,
                        "coverage_path": coverage_path,
                    })
            return artifacts

        master = report.get("master", {})
        coverage = report.get("coverage", {})
        master_path = self._resolve_report_artifact(report_path, master.get("path"))
        coverage_path = self._resolve_report_artifact(
            report_path,
            coverage.get("integration_time_path", coverage.get("path")),
        )
        if master_path and coverage_path:
            artifacts.append({
                "label": master_path.name,
                "master_path": master_path,
                "coverage_path": coverage_path,
            })
        return artifacts

    def choose_crop_report(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose completed quality report",
            filetypes=(("Quality report", "quality_report_*.json"), ("JSON", "*.json"), ("All files", "*.*")),
        )
        if selected:
            self.crop_report.set(selected)
            self.load_crop_report()

    def load_latest_crop_report(self) -> None:
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        reports = sorted(output_dir.glob("quality_report_*.json"), key=lambda path: path.stat().st_mtime)
        if not reports:
            messagebox.showerror("Cropping Workbench", "No quality reports were found in the output folder.")
            return
        self.crop_report.set(str(reports[-1]))
        self.load_crop_report()

    def load_crop_report(self, report_path: Path | None = None, notify: bool = True) -> None:
        if report_path is not None:
            self.crop_report.set(str(report_path))
        report_path = Path(self.crop_report.get().strip()).expanduser()
        if not report_path.is_file():
            if notify:
                messagebox.showerror("Cropping Workbench", "Choose an existing completed quality report.")
            return
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            artifacts = self._crop_artifacts_from_report(report_path, report)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            if notify:
                messagebox.showerror("Cropping Workbench", f"Could not load the quality report:\n\n{error}")
            return
        if not artifacts:
            message = "The report has no master paired with an integration-time coverage map."
            if notify:
                messagebox.showerror("Cropping Workbench", message)
            self.crop_artifacts = []
            self.crop_plan = None
            self._set_crop_summary(message)
            return
        self.crop_report.set(str(report_path))
        self.crop_artifacts = artifacts
        labels = [artifact["label"] for artifact in artifacts]
        assert self.crop_artifact_combo is not None
        self.crop_artifact_combo.configure(values=labels)
        self.crop_artifact_combo.current(0)
        self.crop_artifact.set(labels[0])
        self.preview_crop()
        self.status.set(
            f"Loaded {len(artifacts)} crop artifact(s); calculating preview from {report_path.name}..."
        )

    def set_crop_percent(self, value: int) -> None:
        self.crop_percent.set(value)
        self.preview_crop()

    def _selected_crop_artifact(self) -> dict[str, Any] | None:
        if not self.crop_artifacts:
            return None
        index = self.crop_artifact_combo.current() if self.crop_artifact_combo else -1
        if index < 0 or index >= len(self.crop_artifacts):
            index = 0
        return self.crop_artifacts[index]

    @staticmethod
    def _next_crop_output_path(master_path: Path, percent: int) -> Path:
        candidate = master_path.with_name(f"{master_path.stem}_crop_{percent:03d}pct.fit")
        counter = 2
        while candidate.exists():
            candidate = master_path.with_name(
                f"{master_path.stem}_crop_{percent:03d}pct_{counter}.fit"
            )
            counter += 1
        return candidate

    def _set_crop_preview_busy(self, busy: bool) -> None:
        self.crop_preview_in_progress = busy
        state = "disabled" if busy else "normal"
        if self.crop_preview_button is not None:
            self.crop_preview_button.configure(state=state)
        if self.crop_create_button is not None:
            self.crop_create_button.configure(state=state)
        if self.crop_visual_button is not None:
            self.crop_visual_button.configure(state=state)
        self.start_button.configure(state="disabled" if busy else "normal")

    def _apply_crop_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        percent = int(plan["crop_coverage_percent"])
        output_path = self._next_crop_output_path(Path(plan["master_path"]), percent)
        plan["output_path"] = str(output_path)
        self.crop_plan = plan
        bounds = plan["crop_bounds"]
        selection = plan["crop_siril_selection"]
        retained_warning = (
            "\nWARNING: This crop retains less than 1% of the master. Review the visual preview before creating it."
            if crop_requires_confirmation(plan) else ""
        )
        self._set_crop_summary(
            "CROP PREVIEW\n"
            f"Master: {plan['master_path']}\n"
            f"Integration-time map: {plan['integration_time_path']}\n"
            f"Canvas: {plan['master_width']} x {plan['master_height']} pixels\n"
            f"Crop depth: {percent}% of median positive integration time\n"
            f"Reference integration: {plan['crop_reference_coverage']:.3f} s\n"
            f"Required integration: {plan['crop_threshold']:.3f} s\n"
            f"FITS bounds: x={bounds['x']}, y={bounds['y']}, "
            f"width={bounds['width']}, height={bounds['height']}\n"
            f"Siril selection: x={selection['x']}, y={selection['y']}, "
            f"width={selection['width']}, height={selection['height']}\n"
            f"Output: {output_path}\n"
            f"Pixels retained: {plan['area_percent']:.3f}%"
            f"{retained_warning}"
        )
        return plan

    def _start_crop_visual_preview(
        self,
        generation: int,
        plan: dict[str, Any],
        create_after: bool,
    ) -> None:
        self.status.set("Rendering master and crop previews...")

        def worker() -> None:
            try:
                full_rgb = read_fits_preview(plan["master_path"], max_width=720, max_height=520)
                crop_rgb = read_fits_preview(
                    plan["master_path"],
                    plan["crop_bounds"],
                    max_width=720,
                    max_height=520,
                )
                self._queue_event(
                    "crop_visual_done",
                    (generation, full_rgb, crop_rgb, None, create_after),
                )
            except Exception as error:
                self._queue_event(
                    "crop_visual_done",
                    (generation, None, None, error, create_after),
                )

        Thread(target=worker, daemon=True).start()

    def preview_crop(self, create_after: bool = False) -> dict[str, Any] | None:
        artifact = self._selected_crop_artifact()
        if artifact is None:
            self.crop_plan = None
            self._set_crop_summary("Load a completed quality report with an integration-time map to begin.")
            return None
        try:
            percent = int(self.crop_percent.get())
            if not 1 <= percent <= 100:
                raise ValueError("Crop depth must be from 1 to 100 percent.")
            if self.crop_preview_in_progress:
                return None
            self.crop_preview_generation += 1
            generation = self.crop_preview_generation
        except (tk.TclError, OSError, ValueError) as error:
            self.crop_plan = None
            self._set_crop_summary(f"Crop preview unavailable:\n{error}")
            return None
        self.crop_plan = None
        self._set_crop_preview_busy(True)
        self.status.set("Calculating crop bounds from the integration-time map...")
        self._set_crop_summary(
            "Calculating crop bounds from the integration-time map...\n"
            "The original master is not being modified."
        )

        def worker() -> None:
            try:
                plan = build_crop_plan(artifact["master_path"], artifact["coverage_path"], percent)
                self._queue_event("crop_preview_done", (generation, plan, None, create_after))
            except Exception as error:
                self._queue_event("crop_preview_done", (generation, None, error, create_after))

        Thread(target=worker, daemon=True).start()
        return None

    def _start_crop_from_plan(self, plan: dict[str, Any]) -> None:
        if crop_requires_confirmation(plan) and not messagebox.askyesno(
            "Confirm Very Small Crop",
            f"This crop retains only {plan.get('area_percent', 0):.3f}% of the master area.\n\n"
            "This can indicate unsuitable coverage geometry or an overly strict crop depth. "
            "Create it anyway?",
        ):
            self.status.set("Crop creation cancelled; preview retained")
            return
        executable = Path(self.siril_exe.get().strip()).expanduser()
        if not executable.is_file():
            messagebox.showerror("Cropping Workbench", "Choose an existing Siril executable.")
            return
        output_path = self._next_crop_output_path(
            Path(plan["master_path"]), int(plan["crop_coverage_percent"])
        )
        plan["output_path"] = str(output_path)
        output_path = Path(plan["output_path"])
        command = self._backend_command([
            "--crop-workbench",
            "--siril-exe", str(executable),
            "--crop-workbench-master", plan["master_path"],
            "--crop-workbench-coverage", plan["integration_time_path"],
            "--crop-workbench-percent", str(self.crop_percent.get()),
            "--crop-workbench-output", str(output_path),
        ])
        self._start_crop_operation(command, output_path)

    def create_crop(self) -> None:
        if self.running:
            return
        artifact = self._selected_crop_artifact()
        if artifact is None:
            return
        percent = int(self.crop_percent.get())
        plan = self.crop_plan
        if (
            plan is None
            or plan["master_path"] != str(artifact["master_path"])
            or plan["integration_time_path"] != str(artifact["coverage_path"])
            or plan["crop_coverage_percent"] != percent
        ):
            self.preview_crop(create_after=True)
            return
        self._start_crop_from_plan(plan)

    def _start_crop_operation(self, command: list[str], output_path: Path) -> None:
        self.crop_operation = True
        self.running = True
        self.crop_output_path = output_path
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        if self.crop_preview_button is not None:
            self.crop_preview_button.configure(state="disabled")
        if self.crop_create_button is not None:
            self.crop_create_button.configure(state="disabled")
        if self.crop_visual_button is not None:
            self.crop_visual_button.configure(state="disabled")
        self.status.set("Creating cropped master...")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.crop_log_path = output_path.parent / f"crop_workbench_{datetime.now():%Y%m%d_%H%M%S}.log"
        self.crop_log_path.write_text(
            f"Command: {subprocess.list2cmdline(command)}\n\n", encoding="utf-8"
        )

        def worker() -> None:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                log_path = self.crop_log_path
                if log_path is None:
                    raise RuntimeError("Cropping workbench log path is unavailable.")
                with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
                    process = subprocess.Popen(
                        command,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=creation_flags,
                    )
                    self.process = process
                    return_code = process.wait()
                    log_file.write(f"\nExit code: {return_code}\n")
                self._queue_event("crop_done", return_code)
            except Exception as error:
                self._queue_event("crop_error", error)
            finally:
                self.process = None

        Thread(target=worker, daemon=True).start()

    def _finish_crop_operation(self) -> None:
        self.crop_operation = False
        self.running = False
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        if self.crop_preview_button is not None:
            self.crop_preview_button.configure(state="normal")
        if self.crop_create_button is not None:
            self.crop_create_button.configure(state="normal")
        if self.crop_visual_button is not None:
            self.crop_visual_button.configure(state="normal")

    def _set_review_summary(self, text: str) -> None:
        if self.review_summary is None:
            return
        self.review_summary.configure(state="normal")
        self.review_summary.delete("1.0", "end")
        self.review_summary.insert("1.0", text)
        self.review_summary.configure(state="disabled")

    @staticmethod
    def _format_review_metric(name: str, detail: dict[str, Any], include_label: bool = True) -> str:
        labels = {
            "fwhm": "FWHM",
            "roundness": "Roundness",
            "background": "Background",
            "stars": "Stars",
        }
        label = labels.get(name, name.title())
        value = detail.get("value")
        threshold = detail.get("threshold")
        status = detail.get("status", "unavailable")
        if value is None:
            text = f"unavailable (threshold {threshold})"
            return f"{label}: {text}" if include_label else text
        value_text = f"{value:.3f}" if isinstance(value, float) else str(value)
        threshold_text = "unknown" if threshold is None else f"{threshold:.3f}"
        text = f"{value_text} {detail.get('comparison', '?')} {threshold_text} [{status}]"
        return f"{label} {text}" if include_label else text

    def refresh_run_review(self, report_path: Path | None = None) -> None:
        if report_path is not None:
            self.quality_report_path = report_path
        self._update_loaded_history_highlight()
        if self.review_tree is None:
            return
        for item in self.review_tree.get_children():
            self.review_tree.delete(item)
        path = self.quality_report_path
        if path is None or not path.is_file():
            self._set_review_summary("No completed run loaded.")
            self.review_tree.insert("", "end", values=("-", "No discarded frames recorded", "", "", "", "", "", ""))
            self.refresh_analysis_tabs()
            return
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self._set_review_summary(f"Could not read quality report:\n{error}")
            self.review_tree.insert("", "end", values=("-", "Report unavailable", "", "", "", "", "", ""))
            self.refresh_analysis_tabs()
            return

        substacks = report.get("substacks", [])
        discarded = []
        for substack in substacks:
            for frame in substack.get("discarded_frames", []):
                discarded.append((substack, frame))
        rejection_summaries = []
        for substack in substacks:
            diagnostics = (substack.get('stack') or {}).get('rejection_map_diagnostics')
            if diagnostics:
                rejection_summaries.append((f"Substack {substack.get('number', '?')}", diagnostics))
        master_diagnostics = (report.get('master') or {}).get('rejection_map_diagnostics')
        if master_diagnostics:
            rejection_summaries.append(('Master', master_diagnostics))
        rejected = sum(frame.get("status") == "rejected" for _, frame in discarded)
        failed = sum(frame.get("status") == "failed" for _, frame in discarded)
        skipped = len(report.get("skipped_frames", []))
        stacked = sum(
            substack.get("stack", {}).get("stacked_frames", 0) or 0
            for substack in substacks
        )
        integration = report.get("integration", {})
        integrated = integration.get("integrated_hours")
        settings = report.get("settings", {})
        warnings = report.get("preflight_warnings", [])
        summary_lines = [
            f"Status: {report.get('status', 'unknown')}",
            f"Run ID: {report.get('run_id', 'unknown')}",
            f"Input frames: {report.get('input_frames', report.get('input_summary', {}).get('frames', 'unknown'))}",
            f"Stacked frames: {stacked}    Rejected: {rejected}    Failed: {failed}    Skipped: {skipped}",
            f"Substacks: {len(substacks)}    Cohorts: {len(report.get('cohorts', []))}",
            f"Integrated exposure: {integrated:.2f} hours" if integrated is not None else "Integrated exposure: unavailable",
            f"Rejects moved: {len(report.get('rejected_files', []))}    Restored before run: {len(report.get('restored_rejected_files', []))}",
            f"Seed: {report.get('seed', settings.get('seed', 'unknown'))}",
            f"Configuration hash: {report.get('configuration_hash', 'unknown')}",
            f"Quality report: {path}",
            f"Journal: {report.get('journal_path', 'unavailable')}",
            f"Manifest: {report.get('input_manifest', 'unavailable')}",
            f"Frame ledger: {report.get('frame_ledger_path', 'unavailable')}",
            f"Log: {self.log_path or 'unavailable'}",
        ]
        summary_lines.append('Rejection-map diagnostics:')
        if not rejection_summaries:
            summary_lines.append('  unavailable (not recorded in this report)')
        for label, diagnostics in rejection_summaries:
            summary_lines.append(f"  {label}: {diagnostics.get('status', 'unknown')}")
            for direction, result in (diagnostics.get('maps') or {}).items():
                if result.get('status') != 'available':
                    summary_lines.append(
                        f"    {direction}: {result.get('reason', result.get('status', 'unavailable'))}"
                    )
                    continue
                channel_text = ', '.join(
                    f"ch {channel}: {stats['affected_pixel_locations']:,} locations "
                    f"({stats['affected_pixel_percent']:.4f}%)"
                    for channel, stats in result.get('channels', {}).items()
                )
                summary_lines.append(f"    {direction}: {channel_text or 'no channel metrics'}")
        if warnings:
            summary_lines.append("Warnings: " + " | ".join(str(warning) for warning in warnings))
        self._set_review_summary("\n".join(summary_lines))
        for substack, frame in discarded:
            metrics = frame.get("filter_metrics", {})
            metric_cells = [
                self._format_review_metric(name, metrics[name], include_label=False)
                if name in metrics else "-"
                for name in ("fwhm", "roundness", "background", "stars")
            ]
            self.review_tree.insert(
                "", "end",
                values=(
                    substack.get("number", "-"),
                    frame.get("file", "unknown"),
                    frame.get("status", "unknown"),
                    frame.get("reason_code", frame.get("reason", "")),
                    *metric_cells,
                ),
            )
        if not discarded:
            self.review_tree.insert("", "end", values=("-", "No discarded frames recorded", "", "", "", "", "", ""))
        self.refresh_analysis_tabs()

    def _build_actions(self) -> None:
        frame = ttk.Frame(self.root, padding=(14, 0, 14, 10))
        frame.grid(row=3, column=0, sticky="ew")
        frame.columnconfigure(6, weight=1)
        self.start_button = ttk.Button(frame, text="Start Mosaic Stack", command=self.start, style="Primary.TButton")
        self.start_button.grid(row=0, column=0)
        self.resume_button = ttk.Button(frame, text="Resume Run", command=self.resume_run)
        self.resume_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(frame, text="Preview Run", command=self.preview_run).grid(row=0, column=2, padx=(8, 0))
        self.cancel_button = ttk.Button(frame, text="Cancel Run", command=self.cancel, state="disabled")
        self.cancel_button.grid(row=0, column=3, padx=(8, 0))
        ttk.Button(frame, text="Help", command=self.show_help).grid(row=0, column=4, padx=(8, 0))
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100, length=210)
        self.progress.grid(row=0, column=5, padx=(12, 0))
        ttk.Label(frame, textvariable=self.progress_text).grid(row=0, column=6, sticky="w", padx=(12, 0))
        recovery_actions = ttk.Frame(frame)
        recovery_actions.grid(row=0, column=7, sticky="e", padx=(12, 0))
        for label, command in (
            ("Checkpoint Status", self.show_checkpoint_status),
            ("Discard Checkpoint", self.discard_checkpoint),
            ("Abandon Run", self.abandon_run),
            ("Run Lock Status", self.show_run_lock_status),
            ("Break Run Lock", self.break_run_lock),
        ):
            ttk.Button(recovery_actions, text=label, command=command).pack(side="left", padx=(0, 6))
        ttk.Label(frame, textvariable=self.status).grid(
            row=1, column=0, columnspan=8, sticky="w", pady=(6, 0)
        )

    def show_help(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Siril Mosaic Stacker Help")
        dialog.geometry("1080x760")
        dialog.minsize(760, 520)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(dialog)
        notebook.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        for title in HELP_SECTION_ORDER:
            content = HELP_SECTIONS[title]
            page = ttk.Frame(notebook)
            page.columnconfigure(0, weight=1)
            page.rowconfigure(0, weight=1)
            text = tk.Text(
                page,
                wrap="word",
                padx=16,
                pady=14,
                background=DARK_FIELD,
                foreground=DARK_TEXT,
                insertbackground=DARK_TEXT,
                relief="flat",
                font=("Segoe UI", 10),
            )
            text.grid(row=0, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(page, orient="vertical", command=text.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            text.configure(yscrollcommand=scrollbar.set)
            text.insert("1.0", content)
            text.configure(state="disabled")
            notebook.add(page, text=title)
        ttk.Button(dialog, text="Close", command=dialog.destroy).grid(
            row=1, column=0, pady=(0, 12)
        )

    def show_completion_dialog(self, summary: str, output_dir: str | Path | None) -> tk.Toplevel:
        dialog = tk.Toplevel(self.root)
        configure_dark_theme(dialog)
        dialog.title("Siril Mosaic Stacker")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        text = tk.Text(
            dialog,
            width=78,
            height=18,
            wrap="word",
            padx=14,
            pady=12,
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            relief="flat",
            font=("Segoe UI", 10),
        )
        text.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        text.insert("1.0", summary)
        text.configure(state="disabled")
        style = ttk.Style(dialog)
        style.configure(
            "Completion.TButton",
            background=DARK_SURFACE,
            foreground=DARK_TEXT,
            bordercolor=DARK_BORDER,
            focuscolor="#3f86d9",
            focusthickness=1,
            padding=(8, 4),
        )
        style.map(
            "Completion.TButton",
            background=[("active", "#30343d"), ("pressed", DARK_FIELD)],
            foreground=[("disabled", "#747b86")],
        )
        actions = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        actions.grid(row=1, column=0, sticky="e")
        folder = Path(output_dir).expanduser() if output_dir else None
        if folder is not None and folder.is_dir():
            def open_folder() -> None:
                try:
                    if not folder.is_dir():
                        raise FileNotFoundError(f"Output folder no longer exists: {folder}")
                    if sys.platform == "win32":
                        getattr(os, "startfile")(str(folder))
                    else:
                        opener = "open" if sys.platform == "darwin" else "xdg-open"
                        subprocess.Popen([opener, str(folder)])
                except OSError as error:
                    messagebox.showerror(
                        "Open Output Folder", f"Could not open {folder}:\n{error}", parent=dialog
                    )

            ttk.Button(
                actions, text="Open Output Folder", command=open_folder,
                style="Completion.TButton",
            ).pack(
                side="right", padx=(8, 0)
            )
        ttk.Button(
            actions, text="Close", command=dialog.destroy, style="Completion.TButton"
        ).pack(side="right")
        dialog.update_idletasks()
        dialog.minsize(560, 320)
        dialog.resizable(True, True)
        return dialog

    def _build_log(self) -> None:
        frame = ttk.Frame(self.root, padding=(14, 0, 14, 14))
        frame.grid(row=4, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        self.log = tk.Text(
            frame,
            height=6,
            wrap="word",
            state="disabled",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            insertbackground=DARK_TEXT,
            selectbackground="#285f9e",
            relief="flat",
            highlightthickness=1,
            highlightbackground=DARK_BORDER,
            font=("Consolas", 9),
        )
        self.log.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _read_profiles(self) -> dict[str, dict[str, Any]]:
        if not self.profile_path.is_file():
            return {}
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
        return profiles if isinstance(profiles, dict) else {}

    def _read_last_paths(self) -> dict[str, str]:
        if not self.last_paths_path.is_file():
            return {}
        try:
            data = json.loads(self.last_paths_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            name: value
            for name in ("workdir", "output_dir", "bayer_pattern", "bayer_orientation", "profile_name")
            if isinstance((value := data.get(name)), str) and value
        }

    def _write_last_paths(self) -> None:
        self.last_paths_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.last_paths_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "workdir": self.workdir.get().strip(),
                    "output_dir": self.output_dir.get().strip(),
                    "bayer_pattern": self.bayer_pattern.get(),
                    "bayer_orientation": self.bayer_orientation.get(),
                    "profile_name": self.last_used_profile,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.last_paths_path)

    def _write_profiles(self) -> None:
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.profile_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 2, "profiles": self.profiles}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.profile_path)
        self.profile_combo.configure(values=sorted(self.profiles, key=str.casefold))

    def _profile_values(self) -> dict[str, Any]:
        return {name: getattr(self, name).get() for name in PROFILE_FIELDS}

    def save_profile(self, notify: bool = True) -> None:
        name = self.profile_name.get().strip()
        if not name:
            if notify:
                messagebox.showerror("Run profile", "Enter a profile name.")
            return
        if notify and name in self.profiles and not messagebox.askyesno(
            "Run profile", f"Replace profile '{name}'?"
        ):
            return
        self.profiles[name] = self._profile_values()
        self._write_profiles()
        self.profile_name.set(name)
        self.last_used_profile = name
        self._write_last_paths()
        self.status.set(f"Saved profile: {name}")

    def load_profile(self, notify: bool = True) -> None:
        name = self.profile_name.get().strip()
        profile = self.profiles.get(name)
        if profile is None:
            if notify:
                messagebox.showerror("Run profile", "Choose an existing profile.")
            return
        for field in PROFILE_FIELDS:
            if field in profile:
                getattr(self, field).set(profile[field])
        self.update_substack_controls()
        self.update_drizzle_controls()
        self.update_rejection_controls()
        self.update_cosmetic_controls()
        self.update_quality_filter_controls()
        self.update_coverage_controls()
        self.update_cohort_group_controls()
        self.update_background_controls()
        self.update_normalization_controls()
        self.last_used_profile = name
        self._write_last_paths()
        self.status.set(f"Loaded profile: {name}")

    def delete_profile(self, notify: bool = True) -> None:
        name = self.profile_name.get().strip()
        if name not in self.profiles:
            if notify:
                messagebox.showerror("Run profile", "Choose an existing profile.")
            return
        if notify and not messagebox.askyesno("Run profile", f"Delete profile '{name}'?"):
            return
        del self.profiles[name]
        self._write_profiles()
        self.profile_name.set("")
        if self.last_used_profile == name:
            self.last_used_profile = ""
        self._write_last_paths()
        self.status.set(f"Deleted profile: {name}")

    def choose_workdir(self) -> None:
        selected = filedialog.askdirectory(title="Choose folder to scan recursively for FITS or XISF frames")
        if selected:
            self.workdir.set(selected)
            default_output = Path(selected) / "Siril Mosaic Output"
            self.output_dir.set(str(default_output))
            self._write_last_paths()
            frame_count = len(discover_light_files(Path(selected), (default_output,)))
            self.status.set(f"Found {frame_count} supported frame(s), including subfolders.")

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(title="Choose output folder")
        if selected:
            self.output_dir.set(selected)
            self._write_last_paths()

    def choose_siril(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose Siril executable",
            filetypes=(("Executable", "*.exe"), ("All files", "*.*")),
        )
        if selected:
            self.siril_exe.set(selected)

    def update_drizzle_controls(self) -> None:
        state = "normal" if self.drizzle.get() else "disabled"
        for widget in self.drizzle_widgets:
            widget.configure(state=state)

    def update_rejection_controls(self) -> None:
        method = self.rejection_method.get()
        state = "disabled" if method == "none" else "normal"
        if method in REJECTION_FRACTION_METHODS:
            minimum, maximum, increment = 0.01, 1.0, 0.01
        else:
            minimum, maximum, increment = 0.1, 20.0, 0.1
        for widget in self.rejection_widgets:
            widget.configure(
                state=state,
                from_=minimum,
                to=maximum,
                increment=increment,
            )

    def rejection_method_changed(self) -> None:
        method = self.rejection_method.get()
        low, high = REJECTION_THRESHOLD_DEFAULTS.get(method, (3.0, 3.0))
        self.rejection_low.set(low)
        self.rejection_high.set(high)
        self.update_rejection_controls()

    def update_substack_controls(self) -> None:
        if self.substacks_widget is not None:
            state = "disabled" if self.auto_substacks.get() else "normal"
            self.substacks_widget.configure(state=state)

    def update_cosmetic_controls(self) -> None:
        state = "normal" if self.cosmetic_correction.get() else "disabled"
        for widget in self.cosmetic_widgets:
            widget.configure(state=state)

    def update_quality_filter_controls(self) -> None:
        percent_state = "disabled" if self.adaptive_quality_filtering.get() else "normal"
        sigma_state = "normal" if self.adaptive_quality_filtering.get() else "disabled"
        for widget in self.quality_percent_widgets:
            widget.configure(state=percent_state)
        for widget in self.quality_sigma_widgets:
            widget.configure(state=sigma_state)
        if self.star_count_widget is not None:
            star_state = "disabled" if self.mosaic_aware_star_count.get() else percent_state
            self.star_count_widget.configure(state=star_state)

    def update_coverage_controls(self) -> None:
        coverage_state = "normal"
        crop_state = "normal" if self.coverage_map.get() else "disabled"
        self.auto_crop_master.set(self.auto_crop_master.get() and self.coverage_map.get())
        self.coverage_widgets[0].configure(state=coverage_state)
        for widget in self.coverage_widgets[1:]:
            widget.configure(state=crop_state)

    def _selected_cohort_group_fields(self) -> tuple[str, ...]:
        return tuple(
            field for field, variable in (
                ("camera", self.cohort_group_camera),
                ("filter", self.cohort_group_filter),
                ("exposure_seconds", self.cohort_group_exposure),
            ) if variable.get()
        )

    def update_cohort_group_controls(self) -> None:
        if self.coverage_widgets:
            self.update_coverage_controls()
        state = "normal" if self.export_per_cohort.get() else "disabled"
        for widget in self.cohort_group_widgets:
            widget.configure(state=state)

    def update_background_controls(self) -> None:
        method = self.background_method.get()
        state = "disabled" if method == "Off" else "normal"
        for widget in self.background_widgets:
            widget.configure(state=state)
        if self.rbf_smoothing_widget is not None:
            self.rbf_smoothing_widget.configure(
                state="normal" if method == "RBF" else "disabled"
            )
        if self.background_dither_widget is not None:
            self.background_dither_widget.configure(state=state)

    def update_normalization_controls(self) -> None:
        if not self.overlap_normalization.get():
            self.fast_normalization.set(False)
        if self.fast_normalization_widget is not None:
            self.fast_normalization_widget.configure(
                state="normal" if self.overlap_normalization.get() else "disabled"
            )

    @staticmethod
    def _backend_command(arguments: list[str]) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--backend", *arguments]
        return [sys.executable, "-u", str(Path(__file__).with_name("sirilmosaic.py")), *arguments]

    def command(self, allow_resume: bool = False) -> list[str]:
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        executable = Path(self.siril_exe.get().strip()).expanduser()
        if not workdir.is_dir():
            raise ValueError("Choose an existing input folder.")
        if not self.output_dir.get().strip():
            raise ValueError("Choose an output folder.")
        if output_dir.resolve() == workdir.resolve():
            raise ValueError("Output folder must be different from the input folder.")
        frame_count = len(self._input_files_after_reject_restore(workdir, output_dir))
        if frame_count == 0 and not allow_resume:
            raise ValueError("Input folder contains no supported FITS or XISF files.")
        if not allow_resume and not 0 <= self.test_frame_count.get() <= frame_count:
            raise ValueError("Test frame count must be 0 or no more than the input frame count.")
        if not executable.is_file():
            raise ValueError("Choose an existing Siril executable.")
        if not allow_resume and not self.auto_substacks.get() and not 1 <= self.substacks.get() <= frame_count:
            raise ValueError("Substacks must be between 1 and the number of input frames.")
        if self.export_per_cohort.get() and not self._selected_cohort_group_fields():
            raise ValueError("Select at least one cohort grouping field.")
        if self.auto_crop_master.get() and not self.coverage_map.get():
            raise ValueError("Auto-cropped master requires Write coverage map.")
        if not 1 <= self.auto_crop_coverage_percent.get() <= 100:
            raise ValueError("Crop coverage percentage must be from 1 to 100.")
        if self.drizzle.get():
            if not 0 < self.drizzle_scale.get() <= 3:
                raise ValueError("Drizzle scale must be greater than 0 and no more than 3.")
            if not 0 < self.pixel_fraction.get() <= 1:
                raise ValueError("Pixel fraction must be greater than 0 and no more than 1.")
        if self.rejection_method.get() in REJECTION_FRACTION_METHODS and (
            self.rejection_low.get() > 1 or self.rejection_high.get() > 1
        ):
            raise ValueError("This rejection method requires thresholds from 0 to 1.")
        adaptive = self.adaptive_quality_filtering.get()
        mosaic_aware = self.mosaic_aware_star_count.get()
        percentages = {} if adaptive else {
            'bkg': self.filter_background.get(),
            'round': self.filter_roundness.get(),
            'fwhm': self.filter_fwhm.get(),
        }
        if not adaptive and not mosaic_aware:
            percentages['nbstars'] = self.filter_stars.get()
        sigma = self.quality_filter_sigma.get() if adaptive else None
        build_selection_filters(adaptive, sigma, percentages, mosaic_aware)
        if self.feather.get() < 0:
            raise ValueError("Feather cannot be negative.")
        if self.rejection_low.get() <= 0 or self.rejection_high.get() <= 0:
            raise ValueError("Rejection thresholds must be greater than 0.")
        if self.cosmetic_correction.get():
            if self.cosmetic_cold_sigma.get() <= 0:
                raise ValueError("Cold-pixel sigma must be greater than 0.")
            if self.cosmetic_hot_sigma.get() <= 0:
                raise ValueError("Hot-pixel sigma must be greater than 0.")
        if self.background_method.get() != "Off":
            if self.background_samples.get() < 1:
                raise ValueError("Background samples must be at least 1.")
            if self.background_tolerance.get() <= 0:
                raise ValueError("Background tolerance must be greater than 0.")
        if not 1 <= self.plate_solve_order.get() <= 5:
            raise ValueError("Plate-solve order must be from 1 to 5.")
        if self.plate_solve_radius.get().strip():
            try:
                radius = float(self.plate_solve_radius.get())
            except ValueError as error:
                raise ValueError("Plate-solve radius must be numeric.") from error
            if not radius > 0:
                raise ValueError("Plate-solve radius must be greater than 0.")
        if self.plate_solve_limit_mag.get().strip():
            try:
                float(self.plate_solve_limit_mag.get())
            except ValueError as error:
                raise ValueError("Plate-solve limit magnitude must be numeric.") from error
        if self.rbf_smoothing.get() <= 0:
            raise ValueError("RBF smoothing must be greater than 0.")
        if self.registration_minpairs.get() < 0:
            raise ValueError("Registration minimum pairs cannot be negative.")
        if self.registration_maxstars.get() and not 100 <= self.registration_maxstars.get() <= 2000:
            raise ValueError("Registration maximum stars must be 0 or from 100 to 2000.")
        if self.random_seed.get().strip():
            try:
                int(self.random_seed.get())
            except ValueError as error:
                raise ValueError("Random seed must be an integer.") from error
        if not 0 < self.memory.get() <= 1:
            raise ValueError("Memory fraction must be greater than 0 and no more than 1.")
        if self.cpus.get() < 1:
            raise ValueError("CPU count must be at least 1.")
        if self.retries.get() < 0:
            raise ValueError("Retries cannot be negative.")

        bayer_pattern = "auto" if self.bayer_pattern.get() == "Auto (header)" else self.bayer_pattern.get()
        bayer_orientation = self.bayer_orientation.get().lower()
        command = self._backend_command([
            "--workdir", str(workdir),
            "--output-dir", str(output_dir),
            "--siril-exe", str(executable),
            "--test-frame-count", str(self.test_frame_count.get()),
            "--substacks", str(self.substacks.get()),
            "--auto-substacks" if self.auto_substacks.get() else "--no-auto-substacks",
            "--drizzle" if self.drizzle.get() else "--no-drizzle",
            "--drizzle-scale", str(self.drizzle_scale.get()),
            "--pixel-fraction", str(self.pixel_fraction.get()),
            "--drizzle-kernel", self.drizzle_kernel.get(),
            "--bayer-pattern", bayer_pattern,
            "--bayer-orientation", bayer_orientation,
            "--cosmetic-correction" if self.cosmetic_correction.get() else "--no-cosmetic-correction",
            "--cosmetic-cold-sigma", str(self.cosmetic_cold_sigma.get()),
            "--cosmetic-hot-sigma", str(self.cosmetic_hot_sigma.get()),
            "--overlap-normalization" if self.overlap_normalization.get() else "--no-overlap-normalization",
            "--stack-normalization", self.stack_normalization.get(),
            "--plate-solve-order", str(self.plate_solve_order.get()),
            "--plate-solve-downscale" if self.plate_solve_downscale.get()
            else "--no-plate-solve-downscale",
            "--rbf-smoothing", str(self.rbf_smoothing.get()),
            "--background-dither" if self.background_dither.get() else "--no-background-dither",
            "--registration-transform", self.registration_transform.get(),
            "--registration-minpairs", str(self.registration_minpairs.get()),
            "--registration-maxstars", str(self.registration_maxstars.get()),
            "--registration-interpolation", self.registration_interpolation.get(),
            "--adaptive-quality-filtering" if self.adaptive_quality_filtering.get()
            else "--no-adaptive-quality-filtering",
            "--background-method", self.background_method.get().lower(),
            "--background-samples", str(self.background_samples.get()),
            "--background-tolerance", str(self.background_tolerance.get()),
            "--weight", self.weight.get(),
            "--feather", str(self.feather.get()),
            "--rejection-low", str(self.rejection_low.get()),
            "--rejection-high", str(self.rejection_high.get()),
            "--rejection-method", self.rejection_method.get(),
            "--fast-normalization" if self.overlap_normalization.get() and self.fast_normalization.get() else "--no-fast-normalization",
            "--catalog", self.catalog.get(),
            "--memory", str(self.memory.get()),
            "--cpus", str(self.cpus.get()),
            "--retries", str(self.retries.get()),
            "--skip-failed-frames" if self.skip_failed_frames.get() else "--no-skip-failed-frames",
            "--mosaic-aware-star-count"
            if self.mosaic_aware_star_count.get() else "--no-mosaic-aware-star-count",
            "--coverage-map" if self.coverage_map.get() else "--no-coverage-map",
            "--auto-crop-master" if self.auto_crop_master.get() else "--no-auto-crop-master",
            "--auto-crop-coverage-percent", str(self.auto_crop_coverage_percent.get()),
            "--export-per-cohort" if self.export_per_cohort.get() else "--no-export-per-cohort",
        ])
        if self.export_per_cohort.get():
            for field in self._selected_cohort_group_fields():
                command.extend(("--cohort-group-by", field))
        for option, variable in (
            ("--low-rejection-map", self.low_rejection_map),
            ("--high-rejection-map", self.high_rejection_map),
        ):
            value = variable.get()
            if value != "Follow method":
                command.append(option if value == "Always" else f"--no-{option[2:]}")
        if self.plate_solve_radius.get().strip():
            command.extend(("--plate-solve-radius", self.plate_solve_radius.get().strip()))
        if self.plate_solve_limit_mag.get().strip():
            command.extend(("--plate-solve-limit-mag", self.plate_solve_limit_mag.get().strip()))
        if self.random_seed.get().strip():
            command.extend(("--seed", self.random_seed.get().strip()))
        if adaptive:
            command.extend(("--quality-filter-sigma", str(sigma)))
        else:
            for name, option in (
                ('bkg', 'background'), ('nbstars', 'stars'), ('round', 'roundness'), ('fwhm', 'fwhm')
            ):
                if name in percentages:
                    command.extend((f"--filter-{option}", str(percentages[name])))
        if self.debug.get():
            command.append("--debug")
        return command

    def append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{amount:.1f} TB"

    @staticmethod
    def _available_space(path: Path) -> int:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return shutil.disk_usage(candidate).free

    @staticmethod
    def _pending_reject_files(workdir: Path) -> list[Path]:
        rejects_folder = workdir / REJECTS_DIRECTORY
        if not rejects_folder.is_dir():
            return []
        return sorted(
            path for path in rejects_folder.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_FRAME_SUFFIXES
        )

    def _input_files_after_reject_restore(self, workdir: Path, output_dir: Path) -> list[Path]:
        return discover_light_files(workdir, (output_dir,)) + self._pending_reject_files(workdir)

    def _preflight_summary(self, workdir: Path, output_dir: Path) -> str:
        current_files = discover_light_files(workdir, (output_dir,))
        pending_rejects = self._pending_reject_files(workdir)
        files = current_files + pending_rejects
        summary = summarize_input_frames(files)
        requested_test_count = self.test_frame_count.get()
        effective_count = requested_test_count or summary["frames"]
        estimate = estimate_peak_storage_bytes(summary["bytes"], self.drizzle.get())
        work_available = self._available_space(workdir)
        output_available = self._available_space(output_dir)
        available = min(work_available, output_available)
        exposure_text = (
            f"{summary['total_exposure_seconds'] / 3600:.2f} hours"
            if summary["known_exposure_frames"] else "unknown"
        )
        capacity = "SUFFICIENT" if available >= estimate else "LOW"
        return (
            f"Frames: {summary['frames']} ({len(current_files)} current + "
            f"{len(pending_rejects)} prior rejects to restore)\n"
            f"Frames this run: {effective_count}\n"
            f"Input data: {self._format_bytes(summary['bytes'])}\n"
            f"Estimated peak working space: {self._format_bytes(estimate)}\n"
            f"Available on work drive: {self._format_bytes(work_available)}\n"
            f"Available on output drive: {self._format_bytes(output_available)}\n"
            f"Peak-space check across both drives: {capacity}\n"
            f"Known exposure time: {exposure_text} "
            f"({summary['known_exposure_frames']}/{summary['frames']} frames)\n"
            f"Previously rejected frames to restore: {len(pending_rejects)}"
        )

    def _cohort_summary(self, workdir: Path, output_dir: Path) -> str:
        files = self._input_files_after_reject_restore(workdir, output_dir)
        cohorts = summarize_frame_cohorts(files, self._selected_cohort_group_fields())
        lines = [f"Acquisition cohorts: {len(cohorts)}"]
        for cohort in cohorts[:12]:
            lines.append(f"- {cohort['count']} frames: {cohort['id']}")
        if len(cohorts) > 12:
            lines.append(f"- ... {len(cohorts) - 12} more cohorts")
        lines.append(
            "Cohorts will be processed independently and exported separately."
            if self.export_per_cohort.get()
            else "Cohorts are reported for review; current grouping remains randomized."
        )
        return "\n".join(lines)

    def preview_run(self) -> None:
        try:
            command = self.command()
        except (tk.TclError, ValueError) as error:
            messagebox.showerror("Siril Mosaic Stacker", str(error))
            return
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        self._start_integrity_scan(
            lambda result: self._show_run_preview(command, workdir, output_dir, result)
        )

    def _show_run_preview(
        self,
        command: list[str],
        workdir: Path,
        output_dir: Path,
        integrity: dict[str, Any],
    ) -> None:
        current_input_files = discover_light_files(workdir, (output_dir,))
        pending_rejects = self._pending_reject_files(workdir)
        input_files = current_input_files + pending_rejects
        summary = self._preflight_summary(workdir, output_dir)
        cohorts = self._cohort_summary(workdir, output_dir)
        command_text = subprocess.list2cmdline(command + ["--dry-run"])
        expected = [
            str(output_dir / "master_stack_<run_id>.fit"),
            str(output_dir / "quality_report_<run_id>.json"),
            str(output_dir / "run_events_<run_id>.jsonl"),
            str(output_dir / "input_manifest_<run_id>.json"),
        ]
        if self.coverage_map.get():
            expected.extend((
                str(output_dir / "coverage_map_<run_id>.fit"),
                str(output_dir / "integration_time_map_<run_id>.fit"),
            ))
        if self.auto_crop_master.get():
            expected.append(str(output_dir / "master_stack_<run_id>_cropped.fit"))
        if self.rejection_method.get() != "none":
            expected.append("<master_stack>_{low,high}_rejmap.fit")
        text = (
            "DRY RUN PREVIEW\n"
            "No files will be moved, no rejects will be restored, and Siril will not start.\n\n"
            f"Input files after restore: {len(current_input_files)} current + "
            f"{len(pending_rejects)} prior rejects = {len(input_files)} total\n\n"
            f"{summary}\n\n"
            f"{cohorts}\n\n"
            f"Preflight integrity scan: {integrity['status']} "
            f"({integrity['counts']['PASS']} pass, {integrity['counts']['WARN']} warn, "
            f"{integrity['counts']['FAIL']} fail)\n"
            + "\n".join(
                f"- [{check['status']}] {check['name']}: {check['detail']}"
                for check in integrity.get('checks', [])
            )
            + "\n\n"
            "Generated command:\n"
            f"{command_text}\n\n"
            "Expected output patterns:\n"
            + "\n".join(f"- {path}" for path in expected)
        )
        dialog = tk.Toplevel(self.root)
        dialog.title("Dry Run Preview")
        dialog.geometry("900x700")
        dialog.minsize(700, 500)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        preview = tk.Text(
            dialog,
            wrap="word",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            insertbackground=DARK_TEXT,
            relief="flat",
            font=("Consolas", 9),
        )
        preview.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=12)
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=preview.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 12), pady=12)
        preview.configure(yscrollcommand=scrollbar.set)
        preview.insert("1.0", text)
        preview.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).grid(
            row=1, column=0, columnspan=2, pady=(0, 12)
        )

    def update_progress_display(self) -> None:
        self.progress.configure(value=self.progress_estimator.percent)
        file_progress = ""
        if self.progress_estimator.file_total is not None:
            file_progress = (
                f" - {self.progress_estimator.file_current}/"
                f"{self.progress_estimator.file_total} files"
            )
        elapsed = self.progress_estimator.elapsed_seconds
        elapsed_text = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        remaining = self.progress_estimator.remaining_seconds
        eta_text = "calculating" if remaining is None else f"{int(remaining // 60):02d}:{int(remaining % 60):02d}"
        self.progress_text.set(
            f"{self.progress_estimator.percent:.1f}% estimated{file_progress} - "
            f"{self.progress_estimator.phase} - Elapsed {elapsed_text} - ETA {eta_text}"
        )

    def resume_run(self) -> None:
        checkpoint = Path(self.output_dir.get().strip()).expanduser() / "run_checkpoint.json"
        if not checkpoint.is_file():
            messagebox.showerror(
                "Resume Run",
                f"No resumable run checkpoint was found at:\n\n{checkpoint}",
            )
            return
        self.start(resume=True)

    def start(self, resume: bool = False) -> None:
        try:
            command = self.command(allow_resume=resume)
        except (tk.TclError, ValueError) as error:
            messagebox.showerror("Siril Mosaic Stacker", str(error))
            return
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        if not resume:
            checkpoint = output_dir / "run_checkpoint.json"
            staging = workdir / "Lights_sorted"
            if checkpoint.is_file() or staging.exists():
                messagebox.showerror(
                    "Resume Run Required",
                    "An interrupted run checkpoint or Lights_sorted staging folder already exists.\n\n"
                    "Use Resume Run to continue it, or inspect and clear the recovery state before starting fresh.",
                )
                return
        if resume:
            confirmation = (
                "Resume the interrupted run from its saved checkpoint?\n\n"
                f"Checkpoint: {output_dir / 'run_checkpoint.json'}\n\n"
                "Completed substacks will be reused after checkpoint validation. "
                "Frames left in Lights_sorted after a forced close will be recovered safely."
            )
        else:
            pattern = "auto" if self.bayer_pattern.get() == "Auto (header)" else self.bayer_pattern.get()
            orientation = self.bayer_orientation.get().lower()
            input_files = self._input_files_after_reject_restore(workdir, output_dir)
            warnings = debayer_preflight_warnings(input_files, pattern, orientation)
            performance_warning = ""
            if len(input_files) > 200 and self.overlap_normalization.get():
                performance_warning = (
                    "Performance warning:\n\n"
                    f"{len(input_files)} light files were detected and overlap normalization is enabled. "
                    "This can add substantial processing time for large sequences. "
                    "Consider disabling Overlap normalization before continuing.\n\n"
                )
            storage_summary = self._preflight_summary(workdir, output_dir)
            cohort_summary = self._cohort_summary(workdir, output_dir)
            confirmation = (
                "Frames in the selected folder will be moved temporarily into randomized substack folders. "
                "Do not interrupt disk operations.\n\n"
                f"Storage and exposure preflight:\n{storage_summary}\n\n"
                f"{cohort_summary}\n\nContinue?"
            )
            if warnings:
                confirmation = "Debayer preflight warning:\n\n" + "\n".join(
                    f"- {warning}" for warning in warnings
                ) + "\n\n" + confirmation
            if performance_warning:
                confirmation = performance_warning + confirmation
        if not messagebox.askyesno(
            "Start mosaic stack?",
            confirmation,
        ):
            return
        self._write_last_paths()

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress_estimator.reset()
        self.update_progress_display()
        self.status.set("Resuming..." if resume else "Processing...")
        self.running = True
        self.cancelling = False
        self.quality_report_path = None
        self._update_loaded_history_highlight()
        log_directory = Path(self.workdir.get().strip()).expanduser() / "siril_mosaic_logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        run_stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        self.log_path = log_directory / f"siril_mosaic_{run_stamp}.log"
        self.cancel_path = log_directory / f"siril_mosaic_{run_stamp}.cancel"
        self.cancel_path.unlink(missing_ok=True)
        if resume:
            command.append("--resume")
        command.extend(("--cancel-file", str(self.cancel_path)))
        self.log_path.write_text(
            f"Command: {subprocess.list2cmdline(command)}\n\n",
            encoding="utf-8",
        )
        self.append_log(f"[INFO] Run log: {self.log_path}")

        def worker() -> None:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                assert self.log_path is not None
                with self.log_path.open("a", encoding="utf-8", buffering=1) as log_file:
                    self.process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        creationflags=creation_flags,
                    )
                    assert self.process.stdout is not None
                    for line in self.process.stdout:
                        message = line.rstrip()
                        log_file.write(message + "\n")
                        self._queue_event("log", message)
                    return_code = self.process.wait()
                    log_file.write(f"\nExit code: {return_code}\n")
                self._queue_event("done", return_code)
            except Exception as error:
                if self.log_path is not None:
                    with self.log_path.open("a", encoding="utf-8") as log_file:
                        log_file.write(f"\nLauncher error: {error}\n")
                self._queue_event("error", error)
            finally:
                self.process = None
                if self.cancel_path is not None:
                    self.cancel_path.unlink(missing_ok=True)

        Thread(target=worker, daemon=True).start()

    def cancel(self) -> None:
        if not self.running or self.cancelling:
            return
        if not messagebox.askyesno(
            "Cancel mosaic stack?",
            "The current Siril operation will stop, then source files will be restored before the run exits.",
        ):
            return
        if self.cancel_path is None:
            messagebox.showerror("Cancel mosaic stack", "The cancellation marker is unavailable.")
            return
        try:
            self.cancel_path.write_text("cancel\n", encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Cancel mosaic stack", f"Could not request cancellation:\n\n{error}")
            return
        self.cancelling = True
        self.cancel_button.configure(state="disabled")
        self.status.set("Cancelling safely; restoring sources...")
        self.progress_estimator.phase = "Cancelling safely"
        self.update_progress_display()
        self.append_log("[INFO] Safe cancellation requested; waiting for source rollback...")
        process = self.process
        if process is None:
            return

        def interrupt_siril() -> None:
            try:
                count = terminate_siril_descendants(process.pid)
                self._queue_event("cancel_interrupt", count)
            except Exception as error:
                self._queue_event("cancel_error", error)

        Thread(target=interrupt_siril, daemon=True).start()

    def poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "crop_preview_done":
                    generation, plan, error, create_after = payload
                    if generation != self.crop_preview_generation:
                        continue
                    if error is not None or plan is None:
                        self._set_crop_preview_busy(False)
                        self.crop_plan = None
                        self.status.set("Crop preview unavailable")
                        self._set_crop_summary(f"Crop preview unavailable:\n{error}")
                    else:
                        self._apply_crop_plan(plan)
                        self._start_crop_visual_preview(generation, plan, create_after)
                elif event == "crop_visual_done":
                    generation, full_rgb, crop_rgb, error, create_after = payload
                    if generation != self.crop_preview_generation:
                        continue
                    self._set_crop_preview_busy(False)
                    if error is not None or full_rgb is None or crop_rgb is None:
                        self.status.set("Crop geometry ready; image preview unavailable")
                        summary = self.crop_summary
                        if summary is not None:
                            self._set_crop_summary(
                                summary.get("1.0", "end").rstrip()
                                + f"\n\nImage preview unavailable: {error}"
                            )
                    else:
                        plan = self.crop_plan
                        if plan is None:
                            continue
                        self._apply_crop_visuals(full_rgb, crop_rgb, plan)
                        self.status.set("Crop preview ready")
                    if create_after and self.crop_plan is not None:
                        self._start_crop_from_plan(self.crop_plan)
                elif event == "crop_log":
                    message = str(payload)
                    self.append_log(message)
                    if message:
                        self.status.set(message)
                elif event == "crop_done":
                    self._finish_crop_operation()
                    if payload == 0:
                        self.status.set("Cropped master created")
                        if self.crop_output_path is not None:
                            summary = self.crop_summary
                            if summary is not None:
                                self._set_crop_summary(
                                    summary.get("1.0", "end").rstrip()
                                    + f"\n\nCreated: {self.crop_output_path}"
                                )
                        messagebox.showinfo(
                            "Cropping Workbench",
                            f"Cropped master created.\n\nOutput: {self.crop_output_path}"
                            f"\nLog: {self.crop_log_path}",
                        )
                    else:
                        self.status.set(f"Crop failed (exit code {payload})")
                        messagebox.showerror(
                            "Cropping Workbench",
                            f"The crop operation failed.\n\nLog: {self.crop_log_path}",
                        )
                elif event == "crop_error":
                    self._finish_crop_operation()
                    self.status.set("Crop failed")
                    self.append_log(f"[ERROR] {payload}")
                    messagebox.showerror("Cropping Workbench", str(payload))
                elif event == "integrity_done":
                    if isinstance(payload, tuple):
                        integrity_result, preview_callback = payload
                    else:
                        integrity_result, preview_callback = payload, None
                    self.integrity_scan_running = False
                    if preview_callback is not None:
                        preview_callback(integrity_result)
                    elif self.analysis_text is not None and self.analysis_text.winfo_exists():
                        self.analysis_text.configure(state="normal")
                        self.analysis_text.delete("1.0", "end")
                        self.analysis_text.insert("1.0", self._integrity_text(integrity_result))
                        self.analysis_text.configure(state="disabled")
                    cache_state = "cached" if integrity_result.get("cached") else "fresh"
                    self.status.set(f"Integrity scan: {integrity_result.get('status', 'unknown')} ({cache_state})")
                elif event == "batch_status":
                    index, status, log_path, *detail = payload
                    if 0 <= index < len(self.batch_jobs):
                        self.batch_jobs[index]["status"] = status
                        self.batch_jobs[index]["log_path"] = log_path
                    self._refresh_batch_tree()
                    if status == "running":
                        self.status.set(f"Batch job {index + 1} running")
                    elif status == "complete":
                        self.status.set(f"Batch job {index + 1} complete")
                    else:
                        self.status.set(f"Batch job {index + 1} failed; see {log_path}")
                        if detail:
                            self.append_log(f"[BATCH {index + 1}] {detail[-1]}")
                elif event == "batch_log":
                    index, message = payload
                    self.append_log(f"[BATCH {index + 1}] {message}")
                elif event == "batch_finished":
                    self.batch_running = False
                    self.running = False
                    self.cancelling = False
                    self.process = None
                    self.start_button.configure(state="normal")
                    self.resume_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.refresh_history()
                    self.status.set("Batch queue complete")
                elif event == "log":
                    message = str(payload)
                    self.append_log(message)
                    progress_changed = self.progress_estimator.consume(message)
                    if progress_changed:
                        self.update_progress_display()
                    report_prefix = "[INFO] Quality report: "
                    if message.startswith(report_prefix):
                        self.quality_report_path = Path(message[len(report_prefix):])
                        self.crop_report.set(str(self.quality_report_path))
                    if message and not progress_changed:
                        self.status.set(message)
                elif event == "done":
                    self.running = False
                    self.cancelling = False
                    self.start_button.configure(state="normal")
                    self.resume_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.refresh_run_review()
                    verification_failure = completed_verification_failure(
                        self.quality_report_path
                    )
                    if payload == 2:
                        self.status.set("Cancelled; source files restored")
                        self.progress_estimator.phase = "Cancelled"
                        self.update_progress_display()
                        messagebox.showinfo(
                            "Siril Mosaic Stacker",
                            f"Run cancelled safely. Source files were restored.\n\nLog: {self.log_path}",
                        )
                    elif payload == 0:
                        self.progress_estimator.complete()
                        self.update_progress_display()
                        self.status.set("Complete")
                        report = {}
                        if self.quality_report_path is not None and self.quality_report_path.is_file():
                            try:
                                report = json.loads(
                                    self.quality_report_path.read_text(encoding="utf-8")
                                )
                            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                                pass
                        output_dir = self.output_dir.get().strip()
                        self.show_completion_dialog(
                            completion_summary(report, self.quality_report_path, self.log_path),
                            output_dir or None,
                        )
                    elif verification_failure:
                        self.progress_estimator.complete()
                        self.update_progress_display()
                        self.status.set(f"Complete; verification {verification_failure.lower()}")
                        checkpoint = (
                            Path(self.output_dir.get().strip()).expanduser()
                            / 'run_checkpoint.json'
                        )
                        if not checkpoint.is_file():
                            self.resume_button.configure(state='disabled')
                        messagebox.showerror(
                            "Siril Mosaic Stacker",
                            "Processing completed and artifacts were preserved, but final "
                            f"verification reported {verification_failure}.\n\n"
                            f"Review: {self.quality_report_path}\nLog: {self.log_path}",
                        )
                    else:
                        self.status.set(f"Failed (exit code {payload})")
                        messagebox.showerror(
                            "Siril Mosaic Stacker",
                            f"Processing failed.\n\nLog: {self.log_path}",
                        )
                elif event == "error":
                    self.running = False
                    self.cancelling = False
                    self.start_button.configure(state="normal")
                    self.resume_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.refresh_run_review()
                    self.status.set("Failed")
                    self.progress_estimator.phase = "Failed"
                    self.update_progress_display()
                    self.append_log(f"[ERROR] {payload}")
                    messagebox.showerror("Siril Mosaic Stacker", str(payload))
                elif event == "cancel_interrupt":
                    if payload:
                        self.append_log("[INFO] Siril stopped; backend rollback is running...")
                    else:
                        self.append_log("[INFO] Cancellation queued; waiting for the current safe checkpoint...")
                elif event == "cancel_error":
                    self.append_log(f"[WARNING] Could not stop Siril immediately: {payload}")
        except Empty:
            pass
        if self.dropped_log_events:
            dropped = self.dropped_log_events
            self.dropped_log_events = 0
            self.append_log(f"[WARNING] GUI omitted {dropped} low-priority log event(s) while processing.")
        self.root.after(100, self.poll_events)

    def close_window(self) -> None:
        if self.running:
            messagebox.showwarning(
                "Siril Mosaic Stacker",
                "Processing is still running. Keep this window open until it finishes.",
            )
            return
        self.root.destroy()


def launch_gui() -> None:
    root = tk.Tk()
    SirilMosaicApp(root)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--backend":
        from sirilmosaic import main as backend_main

        raise SystemExit(backend_main(sys.argv[2:]))
    launch_gui()
