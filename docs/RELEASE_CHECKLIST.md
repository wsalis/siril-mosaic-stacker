# Release Checklist

## Documentation

- [ ] README introduction and overview are current.
- [ ] Command cookbook reflects current flags.
- [ ] Artifact reference includes report, ledger, journal, manifest, and checkpoint.
- [ ] Troubleshooting covers current GUI and resume workflows.
- [ ] Compatibility matrix names tested Python, Siril, and pySiril versions.
- [ ] Changelog describes the release.
- [ ] Screenshots or visual capture notes are current.

## Automated Validation

```powershell
Set-Location W:\SirilMosaicStacker
P:\python.exe -m pytest
P:\python.exe -m py_compile sirilmosaic.py sirilmosaic_gui.py test_sirilmosaic.py
git diff --check
```

Repeat the test and compile commands in `W:\Astro\_scripts`. Confirm the repository and operational copies match.

## Real-Data Smoke Validation

- [ ] Run Integrity Scan on a disposable or copied dataset.
- [ ] Run Preview Run and review the scan findings.
- [ ] Complete a small real Siril run with coverage and auto-crop.
- [ ] Run Verify Run and confirm PASS or understand every warning.
- [ ] Inspect the frame ledger count and status distribution.
- [ ] Open Cropping Workbench and create an alternate crop.
- [ ] Test Resume Run using a disposable interrupted run if resumability changed.
- [ ] Confirm original source files are restored and rejects are accounted for.

## Publishing Hygiene

- [ ] No credentials, private paths, or raw data are committed.
- [ ] Temporary output folders and logs are excluded from the release.
- [ ] Version text is consistent across README and release notes.
- [ ] Requirements are installable in a clean environment.
- [ ] GitHub issue templates are present.
- [ ] Release archive contains only source, docs, tests, launcher, and required metadata.
