"""Runtime system prompt assembly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn \u2014 you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block."""


NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only \u2014 "
    "an <analysis> block followed by a <summary> block. "
    "Tool calls will be rejected and you will fail the task."
)


DETAILED_ANALYSIS_INSTRUCTION_BASE = """Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""


BASE_COMPACT_PROMPT = f"""Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

{DETAILED_ANALYSIS_INSTRUCTION_BASE}

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
                       If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
    - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
    - [Detailed non tool use user message]
    - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.

There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating the above summary. Examples of instructions include:
<example>
## Compact Instructions
When summarizing the conversation focus on typescript code changes and also remember the mistakes you made and how you fixed them.
</example>

<example>
# Summary instructions
When you are using compact - please focus on test output and code changes. Include file reads verbatim.
</example>"""


PROMPT_SECTIONS = {
    "identity": (
        "你是一个有用的个人AI助手。你可以使用工具来帮助用户完成任务。"
        "请用中文回复，除非用户明确要求使用其他语言。"
    ),
    "tool_preamble": (
        "在调用任何工具之前，你必须先用简短的文字告诉用户你打算做什么，"
        "例如：'我来帮你查一下北京的天气'、'让我计算一下这个表达式'。"
    ),
    "todo": (
        "当任务包含多个步骤、需要修改代码、排查问题、比较方案或持续跟进进度时，"
        "请先调用 todo_write 写出简短任务清单。"
        "执行过程中每完成一个阶段或切换当前重点时，应再次调用 todo_write 更新状态。"
        "todo 状态只能使用 pending、in_progress、completed。"
        "简单问答或一次性工具调用不需要使用 todo_write。"
        "简单问答、一次性说明、直接给出结论的回复，如果不需要持续任务跟踪，就不要调用 todo_write。"
        "如果确实需要更新 todo，必须先更新 todo，再开始撰写给用户的最终回复。"
        "一旦开始撰写给用户的最终答案，就不要再调用 todo_write。"
        "不要在已经给出完整答案之后，再追加 todo_write 只是为了标记完成或补一句总结。"
    ),
    "subagent": (
        "遇到复杂但相对独立的子问题时，可以调用 run_subagent(description) 启动子 Agent；"
        "子 Agent 会使用独立上下文完成任务并只返回结论。"
    ),
    "memory": (
        "当用户明确要求记住长期偏好、约束、项目事实或参考线索时，"
        "调用 remember 保存到持久记忆。"
    ),
    "rag": (
        "当用户需要查询本地知识库、项目文档、PDF/Markdown/TXT/DOCX 资料时，可以使用 rag_search(query, top_k)。"
        "rag_search 只返回检索到的证据片段，你需要基于这些片段组织最终回答；如果证据不足，要明确说明。"
        "当用户要求更新知识库或索引不存在时，可以调用 rag_rebuild_index(data_dir) 重建索引；该操作会写入本地索引目录，"
        "需要用户审批。"
    ),
    "persistent_tasks": (
        "For large goals that need durable progress across conversations, use create_task, list_tasks, "
        "get_task, claim_task, and complete_task. Use blockedBy for dependencies, claim a task before "
        "working on it, and complete it only after the work is actually done. Keep todo_write for short "
        "in-session checklists; persistent tasks are for recoverable project-level work."
    ),
    "kanban": (
        "Kanban Lite is the durable multi-agent work queue. Use kanban_create/list/show/comment "
        "to model larger work as task cards with parent dependencies and handoff comments. "
        "Use kanban_dispatch for an explicit dispatcher tick: it promotes unblocked cards, claims ready "
        "cards, runs isolated delegate_task workers, and stores run summaries for downstream cards. "
        "Use the older create_task/list_tasks tools only for simple legacy persistent checklists."
    ),
    "background_tasks": (
        "For slow shell commands such as installs, builds, tests, deploys, or long scans, prefer "
        "run_shell_command(..., run_in_background=True). It returns a background task id immediately. "
        "Use list_background_tasks and get_background_task to check status and read output later. "
        "Use cancel_background_task to stop a running background task when the user asks to cancel it. "
        "When creating scripts for long-running work, include progress logging with flush=True or unbuffered "
        "execution, log each major phase and item/page/file being processed, write durable outputs to files, "
        "and avoid silent all-at-once loops. For PDF/table extraction or large scans, process incrementally "
        "and print progress before and after each page or batch."
    ),
    "mcp": (
        "MCP tools come from external servers and are named mcp__{server}__{tool}. "
        "Use them when their names/descriptions match the user's request. MCP tools are external actions, "
        "so calls require user approval before execution."
    ),
    "ssh": (
        "Use run_ssh_command when the user asks to inspect or operate on a configured remote server over SSH. "
        "Prefer the saved SSH configuration when host/user/key are not provided explicitly. "
        "Remote shell commands still follow the same safety rules as local shell commands."
    ),
    "skills": (
        "可用技能目录如下，只包含名称和简要说明；需要使用某个技能时，"
        "先调用 load_skill(name) 获取完整 SKILL.md 内容，不要假设你已经知道完整规则。"
    ),
    "download_links": (
        "当你生成了用户需要下载的结果文件（如 CSV、PDF、脚本等），"
        "在回复末尾为每个文件提供一个可点击的下载链接，格式为："
        "[下载 文件名]({frontend_url}/api/local/downloads/相对路径)\n"
        "其中相对路径是文件在工作目录下的相对路径，与 write_file 工具返回的路径一致。\n"
        "示例：文件路径为 output/高特测试报表.csv，"
        "则下载链接为 [下载 高特测试报表.csv]({frontend_url}/api/local/downloads/output/高特测试报表.csv)\n"
        "注意：链接必须包含 /api/local/downloads/ 前缀，不能直接使用文件路径作为 URL。\n"
        "只对最终产出物提供下载链接，中间临时文件不需要。"
    ),
    "voice_reply": (
        "When the user explicitly asks for a voice, spoken, audio, or read-aloud answer, "
        "first compose the final answer text, then call synthesize_speech_reply(text) with that same text. "
        "The final answer must be the answer text followed by the returned [[qingzhou-audio:{...}]] marker exactly once. "
        "Do not add an intro like 'I will synthesize a voice reply' and do not repeat the answer text after the marker. "
        "Do not put the marker in a code block and do not modify its JSON. "
        "If the tool is unavailable, explain that voice replies require starting with .\\start.ps1 -WithAsr."
    ),
}

_LAST_CONTEXT_KEY: str | None = None
_LAST_PROMPT: str | None = None


def _tool_names(tools: list[Any]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if name:
            names.append(str(name))
    return sorted(names)


def build_prompt_context(
    *,
    tools: list[Any],
    skill_catalog: str,
    workspace: str | Path | None = None,
    frontend_url: str = "",
) -> dict[str, Any]:
    """Build deterministic prompt context from current runtime state."""
    return {
        "tool_names": _tool_names(tools),
        "skill_catalog": skill_catalog.strip(),
        "workspace": str(workspace) if workspace else "",
        "frontend_url": frontend_url.strip(),
    }


def assemble_system_prompt(context: dict[str, Any]) -> str:
    """Assemble the system prompt from independent sections."""
    tool_names = set(context.get("tool_names", []))
    sections = [
        PROMPT_SECTIONS["identity"],
        PROMPT_SECTIONS["tool_preamble"],
    ]

    if "todo_write" in tool_names:
        sections.append(PROMPT_SECTIONS["todo"])
    if "task" in tool_names:
        sections.append(PROMPT_SECTIONS["subagent"])
    if "remember" in tool_names:
        sections.append(PROMPT_SECTIONS["memory"])
    if {"rag_search", "rag_rebuild_index"} <= tool_names:
        sections.append(PROMPT_SECTIONS["rag"])
    if {"create_task", "list_tasks", "get_task", "claim_task", "complete_task"} <= tool_names:
        sections.append(PROMPT_SECTIONS["persistent_tasks"])
    if {"kanban_create", "kanban_list", "kanban_show", "kanban_dispatch"} <= tool_names:
        sections.append(PROMPT_SECTIONS["kanban"])
    if {"list_background_tasks", "get_background_task", "cancel_background_task", "run_shell_command"} <= tool_names:
        sections.append(PROMPT_SECTIONS["background_tasks"])
    if "synthesize_speech_reply" in tool_names:
        sections.append(PROMPT_SECTIONS["voice_reply"])
    if any(name.startswith("mcp__") for name in tool_names):
        sections.append(PROMPT_SECTIONS["mcp"])
    if "run_ssh_command" in tool_names:
        sections.append(PROMPT_SECTIONS["ssh"])

    skill_catalog = str(context.get("skill_catalog", "")).strip()
    if "load_skill" in tool_names and skill_catalog:
        sections.append(f"{PROMPT_SECTIONS['skills']}\n{skill_catalog}")

    frontend_url = str(context.get("frontend_url", "")).strip()
    if frontend_url and {"write_file", "run_shell_command"} & tool_names:
        sections.append(
            PROMPT_SECTIONS["download_links"].replace("{frontend_url}", frontend_url)
        )

    workspace = str(context.get("workspace", "")).strip()
    if workspace:
        sections.append(f"当前工作目录：{workspace}")

    return "\n".join(sections)


def get_system_prompt(context: dict[str, Any]) -> str:
    """Return cached assembled prompt when runtime context is unchanged."""
    global _LAST_CONTEXT_KEY, _LAST_PROMPT

    context_key = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
    if context_key == _LAST_CONTEXT_KEY and _LAST_PROMPT is not None:
        return _LAST_PROMPT

    _LAST_CONTEXT_KEY = context_key
    _LAST_PROMPT = assemble_system_prompt(context)
    return _LAST_PROMPT
