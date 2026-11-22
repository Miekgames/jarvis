---
name: meeting-notes
description: Turn rough meeting notes or a transcript into clean notes with decisions
  and action items.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
---

# Meeting notes

From the notes or transcript the owner gives you, produce:

## Summary
Two or three sentences: what the meeting was for and where it landed.

## Decisions
One bullet per decision actually made. Leave out things only discussed.

## Action items
`- [ ] Who — what — by when` for each. If no owner or date was stated, write
"unassigned" or "no date" rather than inventing one.

## Open questions
Anything raised and not resolved.

Use names exactly as they appear. Do not add anything that is not in the notes.
At the end, ask whether to set reminders for any of the action items (use
`set_reminder` only if they say yes).
