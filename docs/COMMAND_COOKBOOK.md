# Command Cookbook

This cookbook assumes Windows PowerShell and the project interpreter at `P:\python.exe`. Replace the example paths with your own folders.

## Launch the GUI

```powershell
Set-Location W:\Astro\_scripts
P:\python.exe sirilmosaic_gui.py
```

Use a separate output directory from the source directory.

## Small Smoke Run

Use a copied or disposable input set first:

```powershell
P:\python.exe sirilmosaic.py `
  --workdir 'W:\Astro\Cygnus Retry\Frames' `
  --output-dir 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output' `
  --siril-exe 'C:\Program Files\Siril\bin\siril.exe' `
  --test-frame-count 12 `
  --substacks 1 `
  --no-drizzle `
  --no-overlap-normalization `
  --coverage-map `
  --auto-crop-master `
  --adaptive-quality-filtering `
  --quality-filter-sigma 3 `
  --seed 12345
```

## Full Run

```powershell
P:\python.exe sirilmosaic.py `
  --workdir 'W:\Astro\Cygnus Retry\Frames' `
  --output-dir 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output' `
  --siril-exe 'C:\Program Files\Siril\bin\siril.exe' `
  --auto-substacks `
  --no-drizzle `
  --coverage-map `
  --auto-crop-master `
  --skip-failed-frames `
  --seed 12345
```

## Preview Run

The GUI Preview Run performs the integrity scan first. The CLI dry run validates settings without starting Siril or moving files:

```powershell
P:\python.exe sirilmosaic.py `
  --workdir 'W:\Astro\Cygnus Retry\Frames' `
  --output-dir 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output' `
  --siril-exe 'C:\Program Files\Siril\bin\siril.exe' `
  --dry-run `
  --auto-substacks `
  --coverage-map `
  --auto-crop-master
```

## Resume an Interrupted Run

Resume uses `run_checkpoint.json` in the output folder. Use the same input/output folders and processing settings:

```powershell
P:\python.exe sirilmosaic.py `
  --workdir 'W:\Astro\Cygnus Retry\Frames' `
  --output-dir 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output' `
  --siril-exe 'C:\Program Files\Siril\bin\siril.exe' `
  --resume
```

Per-cohort exports retain cohort-local checkpoint state, so the same command can skip completed cohorts and continue the interrupted cohort. The GUI exposes Camera model, Filter, and Exposure time grouping checkboxes; the CLI equivalent repeats `--cohort-group-by`, for example `--cohort-group-by filter --cohort-group-by exposure_seconds`.

## Inspect Checkpoint and Cleanup Candidates

These commands are read-only unless `--discard-checkpoint` is explicitly supplied. Cleanup review only prints candidates; it does not delete them:

```powershell
P:\python.exe sirilmosaic.py `
  --workdir 'W:\Astro\Cygnus Retry\Frames' `
  --output-dir 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output' `
  --checkpoint-status `
  --cleanup-review
```

To remove only the checkpoint metadata after reviewing it:

```powershell
P:\python.exe sirilmosaic.py `
  --workdir 'W:\Astro\Cygnus Retry\Frames' `
  --output-dir 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output' `
  --discard-checkpoint
```

## Verify a Completed Run

```powershell
P:\python.exe sirilmosaic.py `
  --verify-run 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\quality_report_<run_id>.json'
```

The exit code is nonzero when verification fails.

Completed runs write `verification_<run_id>.json` automatically. Export the evidence without raw lights:

```powershell
P:\python.exe sirilmosaic.py `
  --bundle-report 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\quality_report_<run_id>.json' `
  --bundle-output 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\run_evidence.zip'
```

Export a self-contained HTML report for sharing or archiving:

```powershell
P:\python.exe sirilmosaic.py `
  --html-report 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\quality_report_<run_id>.json' `
  --html-output 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\run_<run_id>.html'
```

## Replay Filter Thresholds

```powershell
P:\python.exe sirilmosaic.py `
  --replay-ledger 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\frame_ledger_<run_id>.jsonl' `
  --replay-background 80 `
  --replay-roundness 80 `
  --replay-fwhm 80 `
  --replay-stars 80
```

Replay predicts retention and exposure from recorded metrics. It does not invoke Siril or rewrite the run.

## Integrity Scan

```powershell
P:\python.exe sirilmosaic.py `
  --workdir 'W:\Astro\Cygnus Retry\Frames' `
  --output-dir 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output' `
  --integrity-scan `
  --deep-integrity-scan `
  --integrity-scan-json 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\integrity_scan.json'
```

The scan checks readability, FITS payload lengths, duplicates, dimensions, exposure metadata, CFA warnings, and free disk space without modifying inputs. The GUI uses the deep mode automatically.

## Run Lock and Runtime Guards

```powershell
P:\python.exe sirilmosaic.py `
  --workdir 'W:\Astro\Cygnus Retry\Frames' `
  --output-dir 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output' `
  --run-lock-status
```

Use `--break-run-lock` only after confirming no stacker or Siril process is active. For long runs, the defaults are a 60-second Siril open timeout, a four-hour command timeout, and 0.5 GB minimum free space; override with `--siril-open-timeout`, `--siril-command-timeout`, and `--minimum-free-disk-gb`.

## Crop an Existing Master

```powershell
P:\python.exe sirilmosaic.py `
  --crop-workbench `
  --siril-exe 'C:\Program Files\Siril\bin\siril.exe' `
  --crop-workbench-master 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\master_stack_<run_id>.fit' `
  --crop-workbench-coverage 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\integration_time_map_<run_id>.fit' `
  --crop-workbench-percent 75 `
  --crop-workbench-output 'W:\Astro\Cygnus Retry\Frames\Siril Mosaic Output\master_stack_<run_id>_crop_075pct.fit'
```

This writes a new crop and leaves the original master untouched.
