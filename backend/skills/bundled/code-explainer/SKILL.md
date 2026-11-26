---
name: code-explainer
description: Explain what a piece of code does, in plain language, step by step.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
color: "#9be15d"
---

# Explaining code

1. If the owner gave a file path, read it (`read_file` or `read_source`); if they
   pasted code, use that.
2. Start with one sentence: what the code is for.
3. Walk through it in the order it runs, in small steps. Name the important
   variables and what they hold.
4. Point out anything surprising: edge cases, bugs, slow parts, security issues.
5. Finish with a tiny example of input and the output it produces.

Match the owner's level: if they are learning, define terms as they come up;
if they are experienced, be brief.
