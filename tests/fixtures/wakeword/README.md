# Wake-word audio fixtures

Synthesised with the two Windows SAPI voices (David and Zira) at 16 kHz mono
16-bit — the exact format openWakeWord consumes — by
`scripts/make_wakeword_fixtures.ps1`.

`pos_*` must be detected. `neg_*` must not be, with one documented exception:
`neg_drive_us` ("hey, drive us to the shop") is a deliberate phonetic near-miss
that scores 0.99 in the Zira voice and 0.08 in David's. It is kept precisely
because it is the limitation worth knowing about, rather than quietly dropped
to make the suite look clean.

Regenerate with:

    powershell -ExecutionPolicy Bypass -File scripts/make_wakeword_fixtures.ps1
