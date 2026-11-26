---
name: file-organizer
description: Plan a tidy-up of a messy folder, then carry it out only once the owner
  agrees.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
color: "#d4a373"
---

# Organising a folder

1. Look at the folder with `list_folder` (and `find_duplicate_files` if it is
   large). Do not move anything yet.
2. Propose a plan:
   - the folders to create (a handful, named plainly);
   - which files go where, by type, project or date;
   - duplicates that could be removed, listed separately.
3. Show the plan as a short table or list and ask the owner to confirm or change
   it.
4. Only after they agree: create folders with `create_folder` and move files with
   `move_file`. Never delete -- put unwanted files in the bin with `trash`, and
   only the ones they named.
5. Report what moved and anything that could not be moved.
