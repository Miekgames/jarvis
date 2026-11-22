---
name: compare-docs
description: Compare two documents or versions and say what changed and what matters.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
---

# Comparing documents

1. Read both (`read_file` for paths, or use pasted text).
2. Report:
   - **In short** -- the most important difference in one or two sentences;
   - **Changed** -- section by section, old and new side by side where helpful;
   - **Added** and **Removed** -- separately;
   - **Worth a closer look** -- changes to numbers, dates, obligations, prices or
     anything that shifts responsibility.
3. Ignore pure formatting changes unless asked.
