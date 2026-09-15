# Validation Matrix

| Area | Automated evidence | Real-data evidence | Completion signal |
| --- | --- | --- | --- |
| Source restoration | Grouping, rollback, collision tests | Verify source tree after smoke run | Original relative paths intact |
| Frame selection | Metric and discard-report tests | Ledger status distribution | Ledger matches manifest |
| Coverage | Geometry and dimension tests | Master/map dimensions and crop output | Verify Run PASS |
| Cropping Workbench | Crop plan, CLI, GUI, popup tests | Alternate crop on a completed report | New FITS exists; original unchanged |
| Resumability | Checkpoint, recovery, skip tests | Disposable interrupted run | Only remaining groups run |
| Run Verification | PASS/FAIL synthetic reports | Real report audit | No unexplained failures |
| Threshold Replay | Synthetic metric predictions | Replay against completed ledger | Predicted exposure is plausible |
| Integrity Scan | Duplicate/readability/dimension tests | Full input-folder scan | PASS or reviewed WARNs |
| Analysis tables | Sorting, finite-value dropdowns, contains search, reset tests | Manual table workflow | Filter modes match field type; reset restores default rows |
| GUI workflow | Tk construction and action tests | Manual tab walkthrough | No stale process or blocked action |

## Current Baseline

The current project baseline is verified with the repository and operational copies running the same test suite. Optional real-Siril tests remain environment-dependent and are skipped unless their environment variables are configured.
