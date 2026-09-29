# The organizers' output template

`OUTPUT_TEMPLATE_FINAL_ROUND.xlsx` is the UN Global Hackathon final-round output
template, handed to finalists with the 18 August 2026 orientation materials. It
is vendored here byte for byte because the Evidence Export does not imitate it:
it opens this file, fills the Output Data sheet and saves the result, so the
formulas, validations and the six other sheets stay exactly as the secretariat
wrote them.

- SHA-256: `5a635ea9837a8cadd5b621ff9cd87b84bce67a8fbc4d0fa57287fa39f8ded1d0`
- Size: 45,063 bytes
- Sheets: Output Data, Indicator Reference, Coverage Matrix, Engine Comparison,
  Run Record, Submission Checklist, Instructions

`tests/test_workbook.py` pins the copy against that hash. Never edit this file:
it is the organizers' artifact, not ours. If they publish a revision, replace
the file, update the hash here, and re-run the workbook tests, because the
writer depends on the entry area (rows 9 to 109), the Pillar formula in column
O, and the header row A4:N4.
