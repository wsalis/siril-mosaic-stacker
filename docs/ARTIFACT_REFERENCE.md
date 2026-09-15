# Artifact Reference

A completed run is more than the final master. The artifacts below are designed to make the result inspectable and reproducible.

| Artifact | Purpose | Safe to edit? |
| --- | --- | --- |
| `master_stack_<run_id>.fit` | Full registered master stack. | No |
| `master_stack_<run_id>_cropped.fit` | Optional crop produced from the integration-time map. | No |
| `coverage_map_<run_id>.fit` | Normalized 0-1 view of integration coverage. | No |
| `integration_time_map_<run_id>.fit` | Seconds per pixel used for measurement and cropping. | No |
| `*_low_rejmap.fit`, `*_high_rejmap.fit` | Pixel-rejection diagnostics. | No |
| `quality_report_<run_id>.json` | Run settings, outcomes, metrics, cohorts, exposure, and artifact paths. | No |
| `verification_<run_id>.json` | Automatic PASS/WARN/FAIL audit of the completed report and its evidence. | No |
| `input_manifest_<run_id>.json` | Selected files, sizes, timestamps, SHA-256 fingerprints, cohorts, seed, and configuration hash. | No |
| `frame_ledger_<run_id>.jsonl` | One evidence record per selected frame. | No |
| `run_events_<run_id>.jsonl` | Ordered command and lifecycle journal. | No |
| `integrity_scan_cache.json` | Cached read-only preflight result keyed by input metadata and scan settings. | No |
| `run_checkpoint.json` | Interrupted-run state used by Resume Run. Removed after success. | No |
| `run.lock` | Atomic ownership marker preventing concurrent mutation of the same work/output folders. | Inspect only |
| `quality_report_<run_id>_bundle.zip` | Optional evidence bundle containing report, verification, configuration summary, manifest, ledger, journal, and relevant launcher logs; raw lights are excluded. | No |
| `quality_report_<run_id>.html` | Self-contained human-readable report with summary, verification, coverage, and configuration tables. | No |
| `siril_mosaic_logs\*.log` | Human-readable launcher logs and cancellation markers. | Inspect only |

## Lifecycle Folders

- `Lights_sorted` is temporary staging. Do not edit it during a run or before resuming.
- `rejects` contains frame-selection rejects in their original relative layout. A later run restores these frames before discovery.
- Normal successful runs remove temporary staging. Resume checkpoints intentionally preserve the staging needed to continue.

## Important Distinctions

- The normalized coverage map is for viewing. The integration-time map is for measurement and crop decisions.
- Pixel rejection maps describe per-pixel rejection, not frame-selection rejects.
- `discarded_frames` is a compact report section. The full-frame ledger includes successful frames too.
- A report from before ledger support may verify with a compatibility warning rather than a ledger PASS.
- A checkpoint is evidence of an interrupted run, not a completed output.
- The frame ledger viewer reads the JSONL once per loaded report and can export a filtered CSV.
- Replay coverage impact is a coarse registered-placement estimate, not a replacement for a saved integration-time map.
- Integrity cache keys include an explicit scan version so future scan logic changes invalidate old results.
- Deep integrity scans add FITS payload-length validation; header-only scans remain available for lightweight callers.
- A timed-out Siril call records command telemetry, marks the pipe unhealthy, and triggers descendant cleanup before rollback.
- `Lights_sorted` may contain source frames during resume and is always review-only in Cleanup Review. `rejects` is never a cleanup candidate.
