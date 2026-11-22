---
name: translate-doc
description: Translate text or a document into another language, keeping its formatting
  and tone.
version: 1.0.0
author: jarvis (adapted from OpenJarvis)
---

# Translating

1. Ask which language if it was not said. If a file path was given, read it with
   `read_file`.
2. Translate meaning, not word for word. Keep the tone (formal stays formal).
3. Keep all formatting: headings, lists, tables, code blocks and links unchanged.
   Do not translate code, file names or product names.
4. For long text, translate section by section and keep the structure.
5. After the translation, list any phrases that have no clean equivalent and how
   you handled them.
