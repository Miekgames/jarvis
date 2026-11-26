---
name: remind
description: Schedule a notification, once or repeating, from how you'd say it.
version: 1.0.0
author: jarvis
slash_only: true
color: "#4fe0a8"
---

# Scheduling a reminder

The request says what to be reminded about and when, e.g. "call Dad at 7pm" or
"take my tablets every weekday at 7:30".

1. Call `schedule_notification` with what it should say and when, in the owner's words.
2. If the request is to see reminders, call `list_reminders`; to cancel one, `cancel_reminder`.
3. Reply in one sentence with exactly what the tool said: what, when, and the next time.
   If the tool could not tell when, ask the owner -- never pick a time yourself.
