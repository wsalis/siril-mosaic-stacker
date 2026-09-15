# Changelog

## v2.0 - 2026-09-14

### Changed

- Run History now activates a single selected report automatically and refreshes the downstream analysis and Cropping Workbench views.
- Run Review no longer duplicates Threshold Lab replay or Preview Run integrity scanning; its evidence actions now focus on verification, ledger access, exports, and table filtering.
- Checkpoint and run-lock recovery actions moved to a right-aligned group on the main Start/Resume action row.
- Table filters now use finite-value dropdowns where practical and retain contains search for filenames and paths; documentation and Help describe the behavior.

### Fixed

- Per-cohort export now resets substack completion state between acquisition cohorts, preventing a later cohort's `substack_1` from being skipped after an earlier cohort completed its own `substack_1`.
- Fresh starts now refuse to overwrite an existing checkpoint or `Lights_sorted` staging folder; interrupted runs must use Resume Run or an explicit recovery review.
- Added Abandon Run to restore staged sources and clear temporary recovery state before a deliberate fresh start, while retaining reports and logs.

### Added

- Cropping Workbench for alternate post-run crop thresholds.
- Larger visual crop-preview popup with full-master overlay and selected-region preview.
- Run Verification with report, journal, manifest, artifact, source, reject, and frame-accounting checks.
- Full-frame JSONL ledger with per-frame metrics, stages, exposure, membership, and status.
- Resumable runs with atomic checkpoints and completed-substack reuse.
- Threshold Replay for ledger-based retention and exposure predictions.
- Preflight Integrity Scan for readability, duplicates, dimensions, metadata, CFA warnings, and disk headroom.
- Preview Run integration of the preflight integrity scan.
- Automatic post-run verification artifacts and evidence-bundle export without raw lights.
- Cached integrity scans keyed by input metadata and scan settings.
- Filterable full-frame ledger viewer with CSV export.
- Replay current-versus-proposed deltas, cohort retention warnings, and labeled coverage estimates.
- Checkpoint inspection, explicit discard, per-cohort resume state, ETA, and conservative cleanup review.
- Checkpoint schema-2 loading, SHA-256 resume identity validation, and atomic run locks with explicit recovery.
- Durable fsync-backed evidence writes, Siril watchdog timeouts and artifact postconditions, atomic generated-artifact replacement, bounded GUI event queues, deep FITS scans, and runtime disk guards.
- Resume recovery after forced termination now validates frames directly from checkpointed staging when the original input folder is temporarily empty.
- Added report-driven Run History, Quality Explorer, Cohort Balance, and Coverage Inspector tabs with comparison and archive actions.
- Added Threshold Lab, Frame Inspector, Visual QA, self-contained HTML report export, and sequential Batch Queue processing.
- Expanded Help into a tabbed module guide with detailed selection and workflow explanations.

### Documentation

- Added command cookbook, artifact reference, troubleshooting, compatibility matrix, FAQ, validation matrix, healthy-run example, visual guide, and release checklist.
