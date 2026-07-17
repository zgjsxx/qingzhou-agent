---
name: spreadsheet-reader
description: Read uploaded CSV/TSV/XLSX/XLS spreadsheets with local scripts; preview sheets, rows, columns, and values only when this skill is loaded.
---

# Spreadsheet Reader

Use this skill when the user asks about an uploaded or local spreadsheet,
including files received from Web upload or Feishu/Lark IM.

## Supported Files

- Delimited text: `.csv`, `.tsv`
- Excel workbooks: `.xlsx`, `.xls`

`.xlsx` requires `openpyxl`; `.xls` requires `xlrd`. They are project
dependencies in `requirements.txt`. If the script reports a missing dependency,
tell the user to run the project build/install step.

## Script

Use the bundled script through `run_shell_command`:

```powershell
.\.venv\Scripts\python.exe skills\spreadsheet-reader\scripts\read_spreadsheet.py "PATH_TO_FILE"
```

Set `cwd` to the qingzhou-agent project root when calling `run_shell_command`,
because the shell tool's default cwd is a thread-scoped output directory.

Useful options:

- `--max-sheets 5`
- `--max-rows 50`
- `--max-cols 20`
- `--max-chars 20000`
- `--json` for structured output

## Workflow

1. Start with `scripts/read_spreadsheet.py` using a small preview.
2. Identify sheet names, dimensions, headers, and relevant columns.
3. If the user asks for calculations, inspect enough rows/columns before
   answering. Use a script only for larger aggregation or filtering.
4. Preserve units and column labels from the source file in the answer.
