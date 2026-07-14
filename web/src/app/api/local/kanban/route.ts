import { NextRequest, NextResponse } from "next/server";
import { spawn } from "node:child_process";
import { access, readFile } from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

const repoRoot = path.resolve(process.cwd(), "..");

async function fileExists(filePath: string) {
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function pythonExecutable() {
  const configured = process.env.PYTHON?.trim();
  if (configured) return configured;
  const windowsVenv = path.join(repoRoot, ".venv", "Scripts", "python.exe");
  if (await fileExists(windowsVenv)) return windowsVenv;
  const posixVenv = path.join(repoRoot, ".venv", "bin", "python");
  if (await fileExists(posixVenv)) return posixVenv;
  return "python";
}

function parseDotenv(raw: string) {
  const values: Record<string, string> = {};
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index <= 0) continue;
    const key = trimmed.slice(0, index).trim();
    let value = trimmed.slice(index + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    values[key] = value;
  }
  return values;
}

async function repoEnv() {
  try {
    return parseDotenv(await readFile(path.join(repoRoot, ".env"), "utf-8"));
  } catch {
    return {};
  }
}

async function runKanban(input: Record<string, unknown>): Promise<unknown> {
  const script = String.raw`
import dataclasses
import json
import sys

from agent import kanban


def task_dict(conn, task):
    payload = kanban.task_to_dict(task)
    payload["parents"] = kanban.parent_ids(conn, task.id)
    return payload


def detail_dict(conn, task):
    payload = task_dict(conn, task)
    payload["runs"] = [dataclasses.asdict(run) for run in kanban.list_runs(conn, task.id)]
    payload["comments"] = [
        {
            "id": int(row["id"]),
            "author": row["author"],
            "body": row["body"],
            "created_at": int(row["created_at"]),
        }
        for row in reversed(kanban.list_comments(conn, task.id, limit=20))
    ]
    return payload


def main():
    request = json.loads(sys.stdin.read() or "{}")
    action = request.get("action") or "list"
    with kanban.connect_closing() as conn:
        if action == "list":
            kanban.reap_expired_running(conn)
            kanban.recompute_ready(conn)
            tasks = [
                task_dict(conn, task)
                for task in kanban.list_tasks(
                    conn,
                    status=request.get("status") or None,
                    assignee=request.get("assignee") or None,
                    include_archived=bool(request.get("includeArchived")),
                )
            ]
            print(json.dumps({"tasks": tasks}, ensure_ascii=False))
            return
        if action == "show":
            kanban.reap_expired_running(conn)
            task = kanban.get_task(conn, str(request.get("taskId") or ""))
            if task is None:
                print(json.dumps({"error": "Task not found"}, ensure_ascii=False))
                sys.exit(4)
            print(json.dumps({"task": detail_dict(conn, task)}, ensure_ascii=False))
            return
        if action == "create":
            task = kanban.create_task(
                conn,
                title=str(request.get("title") or ""),
                body=str(request.get("body") or ""),
                assignee=str(request.get("assignee") or "agent"),
                parents=request.get("parents") or [],
                priority=int(request.get("priority") or 0),
                created_by="web",
            )
            print(json.dumps({"task": detail_dict(conn, task)}, ensure_ascii=False))
            return
        if action == "claim":
            task = kanban.claim_task(
                conn,
                str(request.get("taskId") or ""),
                owner=str(request.get("owner") or "web"),
            )
            if task is None:
                print(json.dumps({"error": "Task is not ready or cannot be claimed"}, ensure_ascii=False))
                sys.exit(4)
            print(json.dumps({"task": detail_dict(conn, task)}, ensure_ascii=False))
            return
        if action == "complete":
            ok = kanban.complete_task(
                conn,
                str(request.get("taskId") or ""),
                summary=str(request.get("summary") or "Completed from web"),
                result=str(request.get("result") or request.get("summary") or ""),
            )
            if not ok:
                print(json.dumps({"error": "Task cannot be completed from its current status"}, ensure_ascii=False))
                sys.exit(4)
            task = kanban.get_task(conn, str(request.get("taskId") or ""))
            print(json.dumps({"task": detail_dict(conn, task)}, ensure_ascii=False))
            return
        if action == "block":
            ok = kanban.block_task(
                conn,
                str(request.get("taskId") or ""),
                reason=str(request.get("reason") or "Blocked from web"),
            )
            if not ok:
                print(json.dumps({"error": "Task cannot be blocked from its current status"}, ensure_ascii=False))
                sys.exit(4)
            task = kanban.get_task(conn, str(request.get("taskId") or ""))
            print(json.dumps({"task": detail_dict(conn, task)}, ensure_ascii=False))
            return
        if action == "retry":
            task = kanban.retry_task(conn, str(request.get("taskId") or ""))
            if task is None:
                print(json.dumps({"error": "Only blocked tasks can be retried"}, ensure_ascii=False))
                sys.exit(4)
            print(json.dumps({"task": detail_dict(conn, task)}, ensure_ascii=False))
            return
        if action == "comment":
            comment_id = kanban.add_comment(
                conn,
                str(request.get("taskId") or ""),
                body=str(request.get("body") or ""),
                author=str(request.get("author") or "web"),
            )
            task = kanban.get_task(conn, str(request.get("taskId") or ""))
            print(json.dumps({"commentId": comment_id, "task": detail_dict(conn, task)}, ensure_ascii=False))
            return
        if action == "archive":
            task_id = str(request.get("taskId") or "")
            with kanban.write_txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET status = 'archived', claim_lock = NULL, claim_expires = NULL WHERE id = ? AND status IN ('done', 'blocked')",
                    (task_id,),
                )
                if cur.rowcount != 1:
                    print(json.dumps({"error": "Only done or blocked tasks can be archived"}, ensure_ascii=False))
                    sys.exit(4)
                kanban._append_event(conn, task_id, "archived", {"source": "web"})
            task = kanban.get_task(conn, task_id)
            print(json.dumps({"task": detail_dict(conn, task)}, ensure_ascii=False))
            return
        if action == "dispatch":
            result = kanban.dispatch_once(
                conn,
                max_tasks=int(request.get("maxTasks") or 1),
                cwd=str(request.get("cwd") or ""),
                mode=str(request.get("mode") or "readonly"),
                max_steps=request.get("maxSteps"),
            )
            print(json.dumps({"dispatch": result}, ensure_ascii=False))
            return
    print(json.dumps({"error": "Unknown action"}, ensure_ascii=False))
    sys.exit(3)


if __name__ == "__main__":
    main()
`;
  const python = await pythonExecutable();
  const envFromFile = await repoEnv();

  return new Promise((resolve, reject) => {
    const child = spawn(python, ["-B", "-c", script], {
      cwd: repoRoot,
      env: {
        ...envFromFile,
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        PYTHONPATH: repoRoot,
      },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk) => stdout.push(Buffer.from(chunk)));
    child.stderr.on("data", (chunk) => stderr.push(Buffer.from(chunk)));
    child.on("error", reject);
    child.on("close", (code) => {
      const rawOut = Buffer.concat(stdout).toString("utf8").trim();
      const rawErr = Buffer.concat(stderr).toString("utf8").trim();
      if (code !== 0) {
        let message = rawErr || rawOut || `kanban command failed with code ${code}`;
        try {
          const parsed = JSON.parse(rawOut);
          if (parsed?.error) message = String(parsed.error);
        } catch {
          // Keep the raw message.
        }
        reject(new Error(message));
        return;
      }
      try {
        resolve(JSON.parse(rawOut || "{}"));
      } catch (error) {
        reject(error);
      }
    });
    child.stdin.end(`${JSON.stringify(input)}\n`, "utf8");
  });
}

export async function GET(request: NextRequest) {
  const search = request.nextUrl.searchParams;
  try {
    const taskId = search.get("taskId");
    const payload = taskId
      ? await runKanban({ action: "show", taskId })
      : await runKanban({
          action: "list",
          status: search.get("status") || "",
          assignee: search.get("assignee") || "",
          includeArchived: search.get("includeArchived") === "true",
        });
    return NextResponse.json(payload);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Kanban request failed" },
      { status: 500 },
    );
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    return NextResponse.json(await runKanban(body));
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Kanban request failed" },
      { status: 500 },
    );
  }
}
