# FAQ

## Does an 80% filter setting retain 80% of all frames?

No. Each enabled metric selects its own population and the criteria intersect with plate-solving and registration success. Overall retention can be much lower.

## Is crop depth the percentage of image area retained?

No. Crop depth is the minimum integration-time threshold relative to the median positive coverage. The retained rectangle area is a consequence of the coverage footprint.

## Is integration time the same as effective exposure or noise performance?

No. Integration-time maps sum positive `EXPTIME` contributions before per-pixel rejection and stacking weights. They are useful for coverage decisions, not a direct noise estimate.

## What is the difference between frame rejects and rejection maps?

Frame rejects are whole input frames excluded during registration or earlier stages. Rejection maps show pixels rejected during stacking. They answer different questions.

## Does Preview Run move files?

No. Preview Run, Integrity Scan, Threshold Lab/CLI replay, and Run Verification are read-only. A normal run temporarily stages sources and restores them afterward.

## What does Resume Run reuse?

It reuses only completed substack products validated by `run_checkpoint.json`. Incomplete groups are recovered and restaged. Resume currently supports one combined run, not per-cohort export.

## Why is Threshold Lab/replay unavailable for a report?

Threshold Lab and CLI replay need a current frame ledger containing measured filter metrics. Partial runs, old reports, and runs without usable registration measurements may not have enough evidence.

## Why does Run Verification warn about an old report?

The report may predate the manifest, ledger, checkpoint, or current artifact contract. The warning describes missing historical evidence; it does not rewrite the old run.

## Can I change the original master in Cropping Workbench?

No. Workbench outputs use a new crop-specific filename. The original master and integration-time map remain unchanged.

## When should I enable overlap normalization?

Use it for smaller dedicated-rig collections where panel brightness normalization is worth the extra computation. Avoid it for thousands of smart-scope frames.
