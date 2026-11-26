---
name: countdown
description: Put a live countdown to an event on the main screen, with the latest news a click away.
version: 1.0.0
author: jarvis
slash_only: true
---

# Counting down to an event

The request is the name of an event, sometimes with a date or a time.

1. If it names an event, call `add_countdown` with just the event's name. Do not
   supply a date unless the owner wrote one: the tool finds it and checks that
   websites agree.
2. If it asks to take one off, call `remove_countdown`.
3. If it names nothing, call `list_countdowns`.
4. Reply with what the tool said, in one or two sentences: the date, how long is
   left, and where the date came from. If the tool could not settle the date, say
   exactly that and ask the owner. Never offer a date of your own.
