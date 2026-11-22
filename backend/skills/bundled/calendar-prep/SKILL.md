---
name: calendar-prep
description: Prepare a short briefing for an upcoming meeting or event.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
---

# Preparing for an event

1. Find the event with `calendar_view` (today or the next few days). If there is
   more than one candidate, pick the next one and say which.
2. Gather context:
   - `recall` the attendees' names and the topic for anything the owner has said
     before;
   - look for related notes or files with `find_files` if the title suggests a
     project.
3. Write the briefing:
   - **When and where** -- time in the owner's local time, place or link;
   - **Who** -- attendees and anything known about them;
   - **Purpose** -- what the meeting is for;
   - **Talking points** -- three to five;
   - **To bring or check beforehand**.
4. If there is free time before it to prepare, say how much (`find_free_time`).
