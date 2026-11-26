---
name: debug-error
description: Work out why an error happened and how to fix it, from an error message
  or stack trace.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
color: "#ff5c6e"
---

# Debugging an error

1. Read the whole error. The useful line is usually the last one of the stack
   trace plus the first frame inside the owner's own code.
2. Use `explain_error` for a first read, and `read_file` / `find_in_code` to look
   at the line it points to.
3. Say, in one or two sentences, what actually went wrong.
4. Give the most likely fix first, as a concrete change. Then other possible
   causes, most likely first.
5. Say how to confirm it is fixed (what to run, what should happen).

Do not change files unless the owner asks. If they do, prefer
`propose_code_change` so they can review it.
