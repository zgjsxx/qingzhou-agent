---
name: document-reader
description: Read uploaded TXT/Markdown/JSON/YAML/XML and Word .docx documents with local scripts; extract text and table content only when this skill is loaded.
---

# Document Reader

Use this skill when the user asks about an uploaded or local text/Word document,
including files received from Web upload or Feishu/Lark IM.

## Supported Files

- Text-like files: `.txt`, `.md`, `.markdown`, `.log`, `.json`, `.yaml`, `.yml`, `.xml`
- Word documents: `.docx`

Legacy `.doc` files are not directly supported. Ask the user to convert them to
`.docx`, or use a shell/LibreOffice conversion path only if it is available and
appropriate.

## Script

Use the bundled script through `run_shell_command`:

```powershell
.\.venv\Scripts\python.exe skills\document-reader\scripts\read_document.py "PATH_TO_FILE"
```

Set `cwd` to the qingzhou-agent project root when calling `run_shell_command`,
because the shell tool's default cwd is a thread-scoped output directory.

Useful options:

- `--max-chars 20000`
- `--max-rows 50`
- `--max-cols 20`
- `--no-tables` for DOCX paragraph text only
- `--json` for structured output

When a Feishu/Web message contains a local `path`, pass that exact path to the
script.

## Workflow

1. Read the file with `scripts/read_document.py`.
2. If the result is truncated, ask a narrower question or reread with a larger
   `max_chars`.
3. For `.docx`, pay attention to both paragraph text and table rows.
4. Answer from the extracted content. Do not invent content that was not present.
