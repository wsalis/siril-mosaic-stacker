# Healthy Run Example

This is a compact example of what a healthy current run looks like. Values are from a real 282-frame Cygnus smoke/full workflow and are intended as interpretation guidance, not hard-coded acceptance thresholds.

- Report status: `complete`
- Input frames: `282`
- Substacks: `1`
- Confirmed stacked frames: `135`
- Frame-selection rejects: `147`
- Frame ledger records: `282`
- Ledger status distribution: `135 stacked`, `147 rejected`
- Ledger and manifest identity: exact match
- Master canvas: `6619 x 7223`
- Integration-time map canvas: `6619 x 7223`
- Cropped master: `4239 x 5111`
- Journal events: `33`, sequential and complete
- Resume checkpoint after success: absent
- Run Verification: `PASS`

## What This Demonstrates

The rejected frame count is not automatically a sign of failure. It should agree with the selected filtering strategy and be inspectable in the ledger and mirrored `rejects` folder. The important trust signals are conservation, identity agreement, artifact dimensions, journal closure, and source restoration.
