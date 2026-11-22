---
name: todo-from-notes
description: Pull the actual to-dos out of messy notes, an email or a brain dump.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
---

# To-dos from notes

1. Read what the owner gave you and find every concrete thing someone has to do.
   Ignore ideas, background and things already done.
2. Rewrite each as a short task starting with a verb ("Book the venue", not
   "venue?").
3. Group them: **Today**, **This week**, **Later**, **Waiting on someone else**.
   Use dates or urgency from the notes; when there is none, put it under Later.
4. Mark anything that depends on something else.
5. Offer to set reminders for the ones with a time (`set_reminder`) -- only set
   them if the owner says yes.
