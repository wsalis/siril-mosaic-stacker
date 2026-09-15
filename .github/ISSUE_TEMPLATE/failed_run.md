---
name: Failed or interrupted run
about: Report a failed, cancelled, or resumability problem
title: ""
labels: run-failure
assignees: ""
---

## Run summary

- Run ID:
- Input frame count:
- Substack count:
- Was this a resume attempt?:
- Exit code or visible error:

## Verification

Paste the output or summary from:

- Run Verification
- Integrity Scan
- Threshold Replay, if relevant

## Artifacts

Attach sanitized copies of the quality report, journal, manifest, ledger, checkpoint, and launcher log when available.

## Source safety

- Were all source frames restored?
- Are any files present in `Lights_sorted`?
- Are any files present in `rejects`?
- Did any destination collision occur?
