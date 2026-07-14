"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  Archive,
  CheckCircle2,
  Clock3,
  LoaderCircle,
  MessageSquarePlus,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Rocket,
  Search,
  SquareDashed,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "../ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../ui/card";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Textarea } from "../ui/textarea";

type KanbanStatus = "todo" | "ready" | "running" | "blocked" | "done";

type KanbanRun = {
  id: number;
  status: string;
  outcome: string | null;
  summary: string | null;
  error: string | null;
  started_at: number;
  ended_at: number | null;
};

type KanbanComment = {
  id: number;
  author: string;
  body: string;
  created_at: number;
};

type KanbanTask = {
  id: string;
  title: string;
  body: string;
  assignee: string | null;
  status: KanbanStatus | "archived";
  priority: number;
  created_by: string | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  result: string | null;
  parents?: string[];
  runs?: KanbanRun[];
  comments?: KanbanComment[];
};

const columns: {
  status: KanbanStatus;
  label: string;
  icon: typeof SquareDashed;
}[] = [
  { status: "todo", label: "Todo", icon: SquareDashed },
  { status: "ready", label: "Ready", icon: Clock3 },
  { status: "running", label: "Running", icon: Play },
  { status: "blocked", label: "Blocked", icon: AlertCircle },
  { status: "done", label: "Done", icon: CheckCircle2 },
];

function formatTime(value?: number | null) {
  if (!value) return "";
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error ?? `HTTP ${res.status}`);
  }
  return data as T;
}

export function KanbanPanel() {
  const [tasks, setTasks] = useState<KanbanTask[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<KanbanTask | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [assignee, setAssignee] = useState("agent");
  const [priority, setPriority] = useState(0);
  const [parents, setParents] = useState("");
  const [comment, setComment] = useState("");
  const [query, setQuery] = useState("");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [showArchived, setShowArchived] = useState(false);

  const loadTasks = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (assigneeFilter.trim()) params.set("assignee", assigneeFilter.trim());
      if (showArchived) params.set("includeArchived", "true");
      const suffix = params.toString() ? `?${params.toString()}` : "";
      const data = await fetchJson<{ tasks: KanbanTask[] }>(
        `/api/local/kanban${suffix}`,
      );
      setTasks(data.tasks ?? []);
    } catch (error) {
      toast.error("Failed to load kanban", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setLoading(false);
    }
  }, [assigneeFilter, showArchived]);

  const loadSelected = useCallback(async (taskId: string | null) => {
    if (!taskId) {
      setSelected(null);
      return;
    }
    try {
      const data = await fetchJson<{ task: KanbanTask }>(
        `/api/local/kanban?taskId=${encodeURIComponent(taskId)}`,
      );
      setSelected(data.task);
    } catch (error) {
      setSelected(null);
      toast.error("Failed to load task", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    }
  }, []);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  useEffect(() => {
    void loadSelected(selectedId);
  }, [loadSelected, selectedId]);

  const grouped = useMemo(() => {
    const map = new Map<KanbanStatus, KanbanTask[]>();
    for (const column of columns) map.set(column.status, []);
    const normalizedQuery = query.trim().toLowerCase();
    for (const task of tasks) {
      if (task.status === "archived") continue;
      if (
        normalizedQuery &&
        ![task.id, task.title, task.body, task.assignee ?? ""]
          .join("\n")
          .toLowerCase()
          .includes(normalizedQuery)
      ) {
        continue;
      }
      map.get(task.status)?.push(task);
    }
    return map;
  }, [query, tasks]);

  const archivedTasks = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return tasks.filter((task) => {
      if (task.status !== "archived") return false;
      if (!normalizedQuery) return true;
      return [task.id, task.title, task.body, task.assignee ?? ""]
        .join("\n")
        .toLowerCase()
        .includes(normalizedQuery);
    });
  }, [query, tasks]);

  const runAction = async (action: Record<string, unknown>, success: string) => {
    const key = `${action.action}:${action.taskId ?? "new"}`;
    setActionLoading(key);
    try {
      const data = await fetchJson<{ task?: KanbanTask }>("/api/local/kanban", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action),
      });
      toast.success(success);
      if (data.task) {
        setSelectedId(data.task.id);
        setSelected(data.task);
      }
      await loadTasks();
      if (selectedId) await loadSelected(selectedId);
    } catch (error) {
      toast.error("Kanban action failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setActionLoading(null);
    }
  };

  const createTask = async () => {
    if (!title.trim()) return;
    await runAction(
      {
        action: "create",
        title,
        body,
        assignee,
        priority,
        parents: parents
          .split(/[\s,]+/)
          .map((item) => item.trim())
          .filter(Boolean),
      },
      "Task created",
    );
    setTitle("");
    setBody("");
    setParents("");
    setPriority(0);
  };

  const addComment = async () => {
    if (!selected || !comment.trim()) return;
    await runAction(
      { action: "comment", taskId: selected.id, body: comment, author: "web" },
      "Comment added",
    );
    setComment("");
  };

  const dispatchReady = async () => {
    setActionLoading("dispatch");
    try {
      const data = await fetchJson<{
        dispatch?: {
          promoted?: string[];
          dispatched?: { task_id: string; status: string; summary: string }[];
          errors?: { task_id: string; error: string }[];
        };
      }>("/api/local/kanban", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "dispatch", maxTasks: 1 }),
      });
      const dispatched = data.dispatch?.dispatched?.length ?? 0;
      const errors = data.dispatch?.errors?.length ?? 0;
      toast.success("Dispatch finished", {
        description: `${dispatched} completed, ${errors} blocked or failed.`,
      });
      await loadTasks();
      if (selectedId) await loadSelected(selectedId);
    } catch (error) {
      toast.error("Dispatch failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setActionLoading(null);
    }
  };

  return (
    <div className="grid min-h-[720px] grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="flex min-w-0 flex-col gap-4">
        <Card className="rounded-lg">
          <CardHeader className="pb-3">
            <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
              <div>
                <CardTitle>Kanban Lite</CardTitle>
                <CardDescription>
                  Durable task queue for staged multi-agent work.
                </CardDescription>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="default"
                  size="sm"
                  onClick={() => void dispatchReady()}
                  disabled={actionLoading !== null}
                >
                  <Rocket className="size-4" />
                  Dispatch Ready
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void loadTasks()}
                  disabled={loading}
                >
                  <RefreshCw
                    className={`size-4 ${loading ? "animate-spin" : ""}`}
                  />
                  Refresh
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-3 lg:grid-cols-[minmax(220px,1fr)_180px_140px]">
              <div className="grid gap-2">
                <Label htmlFor="kanban-search">Search</Label>
                <div className="relative">
                  <Search className="text-muted-foreground pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2" />
                  <Input
                    id="kanban-search"
                    className="pl-9"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Filter by id, title, body, assignee"
                  />
                </div>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="kanban-assignee-filter">Assignee</Label>
                <Input
                  id="kanban-assignee-filter"
                  value={assigneeFilter}
                  onChange={(event) => setAssigneeFilter(event.target.value)}
                  onBlur={() => void loadTasks()}
                  placeholder="All assignees"
                />
              </div>
              <div className="flex items-end">
                <Button
                  type="button"
                  variant={showArchived ? "secondary" : "outline"}
                  className="w-full"
                  onClick={() => setShowArchived((value) => !value)}
                >
                  <Archive className="size-4" />
                  {showArchived ? "Hide archived" : "Show archived"}
                </Button>
              </div>
            </div>

            <div className="grid gap-3 lg:grid-cols-[minmax(180px,1fr)_minmax(220px,1.4fr)_110px_110px]">
            <div className="grid gap-2">
              <Label htmlFor="kanban-title">Title</Label>
              <Input
                id="kanban-title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="Implement a small feature"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="kanban-body">Body</Label>
              <Input
                id="kanban-body"
                value={body}
                onChange={(event) => setBody(event.target.value)}
                placeholder="Optional details"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="kanban-assignee">Assignee</Label>
              <Input
                id="kanban-assignee"
                value={assignee}
                onChange={(event) => setAssignee(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="kanban-priority">Priority</Label>
              <Input
                id="kanban-priority"
                type="number"
                value={priority}
                onChange={(event) => setPriority(Number(event.target.value))}
              />
            </div>
            <div className="grid gap-2 lg:col-span-3">
              <Label htmlFor="kanban-parents">Parent task IDs</Label>
              <Input
                id="kanban-parents"
                value={parents}
                onChange={(event) => setParents(event.target.value)}
                placeholder="Optional, separated by spaces or commas"
              />
            </div>
            <div className="flex items-end">
              <Button
                className="w-full"
                disabled={!title.trim() || actionLoading !== null}
                onClick={() => void createTask()}
              >
                <Plus className="size-4" />
                Create
              </Button>
            </div>
            </div>
          </CardContent>
        </Card>

        <div className="grid min-w-0 grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-5">
          {columns.map((column) => {
            const Icon = column.icon;
            const items = grouped.get(column.status) ?? [];
            return (
              <section
                key={column.status}
                className="bg-muted/35 min-h-[420px] rounded-lg border"
              >
                <div className="flex items-center justify-between border-b px-3 py-2">
                  <div className="flex items-center gap-2 text-sm font-semibold">
                    <Icon className="size-4" />
                    {column.label}
                  </div>
                  <span className="text-muted-foreground text-xs">
                    {items.length}
                  </span>
                </div>
                <div className="flex flex-col gap-2 p-2">
                  {items.map((task) => (
                    <button
                      key={task.id}
                      type="button"
                      onClick={() => setSelectedId(task.id)}
                      className={`bg-background rounded-md border p-3 text-left shadow-sm transition hover:border-foreground/30 ${
                        selectedId === task.id ? "ring-ring ring-2" : ""
                      }`}
                    >
                      <div className="line-clamp-2 text-sm font-medium">
                        {task.title}
                      </div>
                      <div className="text-muted-foreground mt-2 flex items-center justify-between gap-2 text-xs">
                        <span className="truncate">{task.assignee || "agent"}</span>
                        <span>p{task.priority}</span>
                      </div>
                      <div className="text-muted-foreground mt-1 flex items-center justify-between gap-2 text-[11px]">
                        <span>{(task.parents ?? []).length} deps</span>
                        <span>{formatTime(task.created_at)}</span>
                      </div>
                      <div className="text-muted-foreground mt-1 truncate font-mono text-[11px]">
                        {task.id}
                      </div>
                    </button>
                  ))}
                  {items.length === 0 ? (
                    <div className="text-muted-foreground px-2 py-8 text-center text-xs">
                      No tasks
                    </div>
                  ) : null}
                </div>
              </section>
            );
          })}
        </div>

        {showArchived ? (
          <section className="bg-muted/25 rounded-lg border">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Archive className="size-4" />
                Archived
              </div>
              <span className="text-muted-foreground text-xs">
                {archivedTasks.length}
              </span>
            </div>
            <div className="grid gap-2 p-2 md:grid-cols-2 xl:grid-cols-4">
              {archivedTasks.map((task) => (
                <button
                  key={task.id}
                  type="button"
                  onClick={() => setSelectedId(task.id)}
                  className={`bg-background rounded-md border p-3 text-left shadow-sm transition hover:border-foreground/30 ${
                    selectedId === task.id ? "ring-ring ring-2" : ""
                  }`}
                >
                  <div className="line-clamp-2 text-sm font-medium">
                    {task.title}
                  </div>
                  <div className="text-muted-foreground mt-2 truncate font-mono text-[11px]">
                    {task.id}
                  </div>
                </button>
              ))}
              {archivedTasks.length === 0 ? (
                <div className="text-muted-foreground p-6 text-center text-xs">
                  No archived tasks
                </div>
              ) : null}
            </div>
          </section>
        ) : null}
      </div>

      <aside className="bg-background min-w-0 rounded-lg border">
        {selected ? (
          <div className="flex h-full flex-col">
            <div className="border-b p-4">
              <div className="text-muted-foreground font-mono text-xs">
                {selected.id}
              </div>
              <h3 className="mt-2 text-base font-semibold leading-6">
                {selected.title}
              </h3>
              <div className="text-muted-foreground mt-2 flex flex-wrap gap-2 text-xs">
                <span>{selected.status}</span>
                <span>@{selected.assignee || "agent"}</span>
                <span>priority {selected.priority}</span>
              </div>
            </div>

            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
              {selected.body ? (
                <section>
                  <h4 className="text-sm font-semibold">Body</h4>
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-6">
                    {selected.body}
                  </p>
                </section>
              ) : null}

              {selected.parents && selected.parents.length > 0 ? (
                <section>
                  <h4 className="text-sm font-semibold">Parents</h4>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {selected.parents.map((parent) => (
                      <span
                        key={parent}
                        className="bg-muted rounded px-2 py-1 font-mono text-xs"
                      >
                        {parent}
                      </span>
                    ))}
                  </div>
                </section>
              ) : null}

              <section className="grid grid-cols-2 gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={selected.status !== "ready" || actionLoading !== null}
                  onClick={() =>
                    void runAction(
                      { action: "claim", taskId: selected.id, owner: "web" },
                      "Task claimed",
                    )
                  }
                >
                  <Play className="size-4" />
                  Claim
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={
                    !["running", "ready", "blocked"].includes(selected.status) ||
                    actionLoading !== null
                  }
                  onClick={() =>
                    void runAction(
                      {
                        action: "complete",
                        taskId: selected.id,
                        summary: "Completed from web",
                      },
                      "Task completed",
                    )
                  }
                >
                  <CheckCircle2 className="size-4" />
                  Done
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={
                    !["todo", "ready", "running"].includes(selected.status) ||
                    actionLoading !== null
                  }
                  onClick={() =>
                    void runAction(
                      {
                        action: "block",
                        taskId: selected.id,
                        reason: "Blocked from web",
                      },
                      "Task blocked",
                    )
                  }
                >
                  <AlertCircle className="size-4" />
                  Block
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={selected.status !== "blocked" || actionLoading !== null}
                  onClick={() =>
                    void runAction(
                      { action: "retry", taskId: selected.id },
                      "Task moved back to queue",
                    )
                  }
                >
                  <RotateCcw className="size-4" />
                  Retry
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="col-span-2"
                  disabled={
                    !["done", "blocked"].includes(selected.status) ||
                    actionLoading !== null
                  }
                  onClick={() =>
                    void runAction(
                      { action: "archive", taskId: selected.id },
                      "Task archived",
                    )
                  }
                >
                  <Archive className="size-4" />
                  Archive
                </Button>
              </section>

              {selected.result ? (
                <section>
                  <h4 className="text-sm font-semibold">Result</h4>
                  <p className="bg-muted mt-2 max-h-48 overflow-y-auto rounded-md p-3 whitespace-pre-wrap text-xs leading-5">
                    {selected.result}
                  </p>
                </section>
              ) : null}

              <section>
                <h4 className="text-sm font-semibold">Comments</h4>
                <div className="mt-2 grid gap-2">
                  <Textarea
                    value={comment}
                    onChange={(event) => setComment(event.target.value)}
                    placeholder="Add a short note"
                    rows={3}
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!comment.trim() || actionLoading !== null}
                    onClick={() => void addComment()}
                  >
                    <MessageSquarePlus className="size-4" />
                    Add comment
                  </Button>
                </div>
                <div className="mt-3 space-y-2">
                  {(selected.comments ?? []).map((item) => (
                    <div
                      key={item.id}
                      className="rounded-md border p-3 text-sm"
                    >
                      <div className="text-muted-foreground flex justify-between gap-2 text-xs">
                        <span>{item.author}</span>
                        <span>{formatTime(item.created_at)}</span>
                      </div>
                      <p className="mt-2 whitespace-pre-wrap leading-5">
                        {item.body}
                      </p>
                    </div>
                  ))}
                </div>
              </section>

              <section>
                <h4 className="text-sm font-semibold">Runs</h4>
                <div className="mt-2 space-y-2">
                  {(selected.runs ?? []).map((run) => (
                    <div
                      key={run.id}
                      className="rounded-md border p-3 text-xs leading-5"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium">run #{run.id}</span>
                        <span>{run.outcome || run.status}</span>
                      </div>
                      <div className="text-muted-foreground">
                        {formatTime(run.started_at)}
                      </div>
                      {run.summary ? <p className="mt-1">{run.summary}</p> : null}
                      {run.error ? (
                        <p className="text-destructive mt-1">{run.error}</p>
                      ) : null}
                    </div>
                  ))}
                  {(selected.runs ?? []).length === 0 ? (
                    <p className="text-muted-foreground text-xs">No runs yet</p>
                  ) : null}
                </div>
              </section>
            </div>
          </div>
        ) : (
          <div className="flex h-full min-h-[520px] flex-col items-center justify-center p-6 text-center">
            {loading ? (
              <LoaderCircle className="text-muted-foreground size-6 animate-spin" />
            ) : (
              <>
                <SquareDashed className="text-muted-foreground size-8" />
                <h3 className="mt-3 text-sm font-semibold">Select a task</h3>
                <p className="text-muted-foreground mt-1 text-xs leading-5">
                  Pick a card to see its details, runs, and comments.
                </p>
              </>
            )}
          </div>
        )}
      </aside>
    </div>
  );
}
