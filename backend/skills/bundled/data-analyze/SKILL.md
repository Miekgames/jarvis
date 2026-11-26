---
name: data-analyze
description: Look at a CSV or table of numbers and report what it shows.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
color: "#20c997"
---

# Analysing data

1. Read the file with `read_file` (or use pasted data). Note the columns, how
   many rows, and any obviously missing or odd values.
2. Work out the basics: totals, averages, ranges, and the biggest and smallest
   items. Use `calculate` for any arithmetic rather than doing it in your head.
3. Look for what stands out: trends over time, outliers, groups that differ.
4. Report:
   - **What the data is**;
   - **Key numbers** -- a short table;
   - **What stands out** -- three to five findings, each with the numbers behind it;
   - **Caveats** -- missing data, small samples, anything that limits the conclusions.
