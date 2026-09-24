# Next Version Parking Lot

Target release: v2.2

This is a clean list of v2.2 work that is not complete in v2.1. Some items have supporting infrastructure already; that does not count as completion until the stated workflow and acceptance evidence exist.

## Status Key

- **Partial foundation:** supporting code or measurements exist, but the requested workflow or acceptance gate is incomplete.
- **Open:** no end-user implementation has been identified.
- **Deferred:** intentionally excluded from v2.2 unless explicitly promoted.

## Principles

- Prefer measurement and comparison before automation.
- Preserve the conservative processing style.
- Do not optimize for smoothness, low noise, or visual appearance alone.
- Use synthetic fixtures and copied real-run metadata before touching live acquisitions.

## Candidate Work

### Rejection Diagnostics

Status: **Partial implementation**. Reports derive low/high channel-imbalance ranges from Siril's per-channel rejection percentages, identify missing or unexpected channels when output dimensions are known, and summarize rejection-map affected-location percentages and spatial concentration in a fixed 4x4 grid. Experiment records compare matching substack/cohort and master rejection percentages, channel-imbalance ranges, and map/spatial rates against an explicitly selected control using candidate-minus-control deltas. Exact rejected-sample counts, blank-sky background/noise, and residual hot/cold indicators remain explicitly unavailable without denominators, sky masks, or defect references.

Add report-only measurements for rejection behavior:

- Per-channel rejection percentages and rejected-pixel counts
- Channel imbalance
- Blank-sky background and noise estimates
- Residual hot/cold pixel indicators
- Spatial concentration of rejected pixels
- Difference from a no-rejection or conservative control

The implementation records evidence only. It does not automatically choose rejection settings or reject a run. Synthetic tests validate map aggregation and delta arithmetic; they do not establish Siril-native map semantics or scientific image quality.
Remaining acceptance: validate spatial summaries and selected-control comparisons with copied real-run evidence. Do not infer counts or image-quality measurements when source data are missing.

### Rejection Comparison Workflow

Status: **Partial foundation**. Run History can now export a report-only JSON experiment record for two or more completed runs, and it requires identical selected-file size/SHA-256 fingerprints. It records each run's settings, configuration hash, seed, elapsed runtime when available, output fingerprints, and rejection diagnostics, plus an explicit control run and conclusion.

Define a repeatable way to compare several rejection configurations against the same copied input set. Capture settings, runtime, output identities, diagnostics, and control comparisons together so experiments remain reproducible.
Existing run IDs, settings hashes, seeds, and ordinary quality reports are supporting metadata, not this workflow.
Remaining acceptance: validate the exported record with copied real-run reports, confirm output fingerprints are complete for legacy and current reports, and document how control and conclusion fields are reviewed. The record is descriptive only and does not rank runs or select settings.

### Independent Rejection-Map Controls

Status: **Implemented; real-run validated with rejection active**.

Low and high maps now each support Follow method, Always, and Never. Follow method preserves the prior behavior: substacks request maps for non-`none` rejection, while master maps also retain the four-substack threshold. Siril's paired `-rejmaps` output is filtered so only selected directions remain in the deliverable artifacts. Copied Siril 1.4.4 runs verified each direction independently with rejection active. Siril explicitly ignores `-rejmaps` when rejection is `none`; the app now skips that unsupported flag and reports explicitly selected directions as unavailable with reason `rejection_disabled`.
Remaining acceptance: no additional test is needed for forcing maps with rejection disabled; Siril reports this combination unsupported. Continue validating selected-direction artifacts in representative substack and master runs where rejection is active.

### Coverage Landmark Validation

Status: **Partial; Siril 1.4.4 transform resampling validated**. In addition to the synthetic composition tests, the opt-in `test_real_siril_resamples_rotated_coverage_landmarks` test runs the production map-sequence preparation and `seqapplyreg` through the Siril CLI. A known 90-degree transform is applied to a small coverage map; exact landmark pixels are checked after resampling and composition, then crop bounds and Siril selection coordinates are checked against a master on the composed grid. This passed against the installed Siril 1.4.4 CLI.

Remaining acceptance: validate a copied multi-substack run whose transforms are generated by Siril's star registration, then compare its coverage landmarks and crop selection to that actual final master. The current integration fixture supplies a known transform and uses a synthetic master with the composed map dimensions; it validates Siril's map resampling and our coordinate conversion, not real transform estimation or full-stack master framing.

### Experiment Records

Status: **Partial foundation**. Run History now creates the explicit comparison record described here. Copied real-run validation remains open.

Consider a small explicit record for experiment input identity, settings, seed, control choice, diagnostics, and conclusion without turning the normal run workflow into an optimizer.

### Visual QA Tab Simplification

Status: **Implemented**. Visual QA is no longer a primary tab; Run Review opens it as a secondary, reusable window with the same read-only preview and warning checks. The Crop Workbench remains the primary place for visual crop inspection.

## Explicitly Deferred

- Automatic rejection-setting selection
- Automatic rejection guardrails based only on empty or sparse rejection maps
- A single composite score that declares one setting scientifically perfect
- Any tuning that can erase faint structure without an independent control

## Promotion Rule

Move an item into active implementation only when its purpose, focused test, real-data validation plan, and failure behavior are written down.

### Show Output Folder After Success

Status: **Implemented; GUI acceptance tests pass**.

The successful-stack completion dialog includes an `Open Output Folder` button when the configured output directory exists. It opens that exact directory without changing or rerunning the completed run; missing directories omit the action.

Automated GUI acceptance tests cover opening the configured directory and omitting the button when the directory is missing.

### Long-Running Stack Timeout

Status: **Partial foundation**.

The default per-command Siril timeout is `0`, which waits indefinitely and supports stacks that run for many hours. Siril startup still has a separate timeout, and an explicitly configured positive command timeout remains available as a watchdog.

Keep the unlimited command-timeout default unless a future change defines a separate, evidence-backed supervision policy. Test that long-running commands are not capped by the default.

### General Run Statistics

Status: **Partial foundation**.

Completion summaries and quality reports already provide frame accounting, integrated data, verification, artifact locations, quality metrics, settings, and run identity. This does not replace the rejection-specific diagnostics or comparison workflow above.

Before promoting this into active implementation, define which additional statistics are wanted, their source data, and a focused acceptance test.
