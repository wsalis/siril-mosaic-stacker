# Siril Mosaic Stacker

A source-safe graphical workflow for stacking large mosaics and high-frame-count image sets with [Siril](https://siril.org/). It groups source frames into randomized substacks, registers and integrates each group, then creates a final registered master stack.

## Highlights

- Restores source frames to their original locations after success, cancellation, or failure
- Supports FITS (`.fit`, `.fits`, `.fts`) and XISF input
- CFA metadata preflight with explicit Bayer pattern and row-orientation controls
- Optional CFA cosmetic correction with separate cold/hot sigma controls
- Percentage or adaptive k-sigma quality filtering
- Drizzle, overlap normalization, rejection, weighting, and feathering controls
- Optional fast normalization for large light-frame sets
- Progress estimates, cancellation, logs, profiles, and JSON quality reports
- Avoids statistically weak master rejection when fewer than four substacks exist

## Requirements

- Windows 10 or 11
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

## Output

The selected output folder receives the final master stack and a timestamped JSON quality report. Temporary substacks and rejection maps are removed after successful normal runs; use debug mode to retain intermediates for investigation. Run logs are written beneath the source folder in `siril_mosaic_logs`.

The GUI stores profiles and recently selected paths under `%APPDATA%\Siril Mosaic Stacker`; these files are not stored in this repository.

## Tests

```powershell
python -m pytest -q
```

On some Windows Python installations, repeatedly creating Tk interpreters in one pytest process can fail during Tcl initialization. The individual GUI tests pass when run in fresh Python processes.

## Source Safety

The application records every moved source frame in `source_manifest.json` and restores it to its original relative path. Do not manually alter `Lights_sorted` while a run is active. Keeping an independent backup of irreplaceable acquisition data is still recommended.
