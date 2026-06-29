"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Bot,
  CheckCircle2,
  Clock3,
  RefreshCw,
  Search,
  Terminal,
  Wrench,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type LogEvent = {
  ts?: string;
  event?: string;
  elapsed_ms?: number;
  tool?: string;
  model_name?: string;
  error?: string;
  [key: string]: unknown;
};

type Source =
  | "agent"
  | "backend"
  | "backend-error"
  | "frontend"
  | "frontend-error";

type LogResponse = {
  exists: boolean;
  updatedAt?: string;
  events?: LogEvent[];
  text?: string;
  error?: string;
};

const sourceOptions: { value: Source; label: string }[] = [
  { value: "agent", label: "Agent 事件" },
  { value: "backend", label: "后端输出" },
  { value: "backend-error", label: "后端错误" },
  { value: "frontend", label: "前端输出" },
  { value: "frontend-error", label: "前端错误" },
];

const filters = [
  { value: "all", label: "全部" },
  { value: "agent", label: "Agent" },
  { value: "model", label: "模型" },
  { value: "tool", label: "工具" },
  { value: "error", label: "错误" },
] as const;

function eventGroup(event = "") {
  if (event.endsWith(".error") || event.includes("error")) return "error";
  return event.split(".")[0] || "other";
}

function eventLabel(item: LogEvent) {
  if (item.event?.startsWith("tool.")) {
    return item.tool ? `${item.event} · ${item.tool}` : item.event;
  }
  if (item.event?.startsWith("model.") && item.model_name) {
    return `${item.event} · ${item.model_name}`;
  }
  return item.event ?? "unknown event";
}

function formatTime(ts?: string) {
  if (!ts) return "--:--:--";
  const date = new Date(ts);
  return Number.isNaN(date.getTime())
    ? ts
    : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function formatDuration(value?: number) {
  if (value === undefined) return null;
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`;
}

function EventIcon({ group }: { group: string }) {
  if (group === "error") return <AlertTriangle className="size-4" />;
  if (group === "tool") return <Wrench className="size-4" />;
  if (group === "model") return <Bot className="size-4" />;
  if (group === "agent") return <Activity className="size-4" />;
  return <CheckCircle2 className="size-4" />;
}

export default function MonitorPage() {
  const [source, setSource] = useState<Source>("agent");
  const [data, setData] = useState<LogResponse>({ exists: false, events: [] });
  const [filter, setFilter] =
    useState<(typeof filters)[number]["value"]>("all");
  const [query, setQuery] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [loading, setLoading] = useState(true);

  const loadLogs = useCallback(async () => {
    try {
      const response = await fetch(
        `/api/local/logs?source=${source}&limit=300`,
        { cache: "no-store" },
      );
      const payload = (await response.json()) as LogResponse;
      if (!response.ok)
        throw new Error(payload.error ?? `HTTP ${response.status}`);
      setData(payload);
    } catch (error) {
      setData({
        exists: false,
        error: error instanceof Error ? error.message : "读取日志失败",
      });
    } finally {
      setLoading(false);
    }
  }, [source]);

  useEffect(() => {
    setLoading(true);
    void loadLogs();
  }, [loadLogs]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void loadLogs(), 2500);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadLogs]);

  const events = useMemo(() => data.events ?? [], [data.events]);
  const filteredEvents = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return events.filter((item) => {
      const group = eventGroup(item.event);
      if (filter !== "all" && group !== filter) return false;
      if (!normalizedQuery) return true;
      return JSON.stringify(item).toLowerCase().includes(normalizedQuery);
    });
  }, [events, filter, query]);

  const stats = useMemo(
    () => ({
      total: events.length,
      models: events.filter((item) => item.event === "model.end").length,
      tools: events.filter((item) => item.event === "tool.end").length,
      errors: events.filter((item) => eventGroup(item.event) === "error")
        .length,
    }),
    [events],
  );

  return (
    <main className="bg-muted/25 min-h-screen">
      <header className="bg-background sticky top-0 z-20 border-b">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-5 py-4">
          <div className="flex items-center gap-3">
            <Button
              variant="ghost"
              size="icon"
              asChild
            >
              <Link
                href="/"
                aria-label="返回聊天"
              >
                <ArrowLeft className="size-5" />
              </Link>
            </Button>
            <div>
              <h1 className="text-lg font-semibold">运行监控</h1>
              <p className="text-muted-foreground text-xs">
                本地 Agent、模型与工具调用时间线
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant={autoRefresh ? "secondary" : "outline"}
              size="sm"
              onClick={() => setAutoRefresh((value) => !value)}
            >
              <Activity className="mr-2 size-4" />
              {autoRefresh ? "自动刷新中" : "自动刷新"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void loadLogs()}
            >
              <RefreshCw
                className={`mr-2 size-4 ${loading ? "animate-spin" : ""}`}
              />
              刷新
            </Button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-7xl space-y-5 px-5 py-6">
        <section className="flex flex-wrap items-center gap-2">
          {sourceOptions.map((option) => (
            <Button
              key={option.value}
              variant={source === option.value ? "default" : "outline"}
              size="sm"
              onClick={() => setSource(option.value)}
            >
              {option.value === "agent" ? (
                <Activity className="mr-2 size-4" />
              ) : (
                <Terminal className="mr-2 size-4" />
              )}
              {option.label}
            </Button>
          ))}
          <span className="text-muted-foreground ml-auto text-xs">
            {data.updatedAt
              ? `更新于 ${new Date(data.updatedAt).toLocaleTimeString("zh-CN", { hour12: false })}`
              : ""}
          </span>
        </section>

        {source === "agent" ? (
          <>
            <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {[
                ["事件", stats.total, Activity],
                ["模型调用", stats.models, Bot],
                ["工具调用", stats.tools, Wrench],
                ["错误", stats.errors, AlertTriangle],
              ].map(([label, value, Icon]) => {
                const IconComponent = Icon as typeof Activity;
                return (
                  <div
                    key={String(label)}
                    className="bg-card rounded-xl border p-4 shadow-sm"
                  >
                    <div className="text-muted-foreground flex items-center gap-2 text-sm">
                      <IconComponent className="size-4" />
                      {String(label)}
                    </div>
                    <div className="mt-2 text-3xl font-semibold">
                      {String(value)}
                    </div>
                  </div>
                );
              })}
            </section>

            <section className="bg-card overflow-hidden rounded-xl border shadow-sm">
              <div className="flex flex-col gap-3 border-b p-4 md:flex-row md:items-center">
                <div className="flex flex-wrap gap-2">
                  {filters.map((item) => (
                    <Button
                      key={item.value}
                      variant={filter === item.value ? "secondary" : "ghost"}
                      size="sm"
                      onClick={() => setFilter(item.value)}
                    >
                      {item.label}
                    </Button>
                  ))}
                </div>
                <div className="relative md:ml-auto md:w-72">
                  <Search className="text-muted-foreground absolute top-2.5 left-3 size-4" />
                  <Input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="搜索事件、工具或错误"
                    className="pl-9"
                  />
                </div>
              </div>

              <div className="divide-y">
                {filteredEvents.map((item, index) => {
                  const group = eventGroup(item.event);
                  const duration = formatDuration(item.elapsed_ms);
                  return (
                    <details
                      key={`${item.ts}-${item.event}-${index}`}
                      className="group"
                    >
                      <summary className="hover:bg-muted/50 flex cursor-pointer list-none items-center gap-3 px-4 py-3">
                        <span
                          className={`flex size-8 shrink-0 items-center justify-center rounded-full ${
                            group === "error"
                              ? "bg-red-100 text-red-700"
                              : group === "tool"
                                ? "bg-amber-100 text-amber-700"
                                : group === "model"
                                  ? "bg-violet-100 text-violet-700"
                                  : "bg-blue-100 text-blue-700"
                          }`}
                        >
                          <EventIcon group={group} />
                        </span>
                        <span className="text-muted-foreground w-20 shrink-0 font-mono text-xs">
                          {formatTime(item.ts)}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-sm font-medium">
                          {eventLabel(item)}
                        </span>
                        {duration && (
                          <span className="text-muted-foreground flex items-center gap-1 text-xs">
                            <Clock3 className="size-3.5" />
                            {duration}
                          </span>
                        )}
                      </summary>
                      <pre className="bg-muted/50 max-h-[32rem] overflow-auto border-t p-4 text-xs leading-5">
                        {JSON.stringify(item, null, 2)}
                      </pre>
                    </details>
                  );
                })}
              </div>
            </section>
          </>
        ) : (
          <section className="overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950 shadow-sm">
            <div className="border-b border-zinc-800 px-4 py-3 text-sm text-zinc-300">
              {sourceOptions.find((item) => item.value === source)?.label}
            </div>
            <pre className="max-h-[70vh] min-h-96 overflow-auto p-4 font-mono text-xs leading-5 whitespace-pre-wrap text-zinc-200">
              {data.text || "暂无日志"}
            </pre>
          </section>
        )}

        {!data.exists && !loading && (
          <div className="rounded-xl border border-dashed p-10 text-center">
            <Activity className="text-muted-foreground mx-auto mb-3 size-8" />
            <p className="font-medium">暂时没有日志</p>
            <p className="text-muted-foreground mt-1 text-sm">
              Agent 事件日志需要在 backend/.env 中设置 AGENT_LOG_ENABLED=true，
              然后重启后端。
            </p>
          </div>
        )}
        {data.error && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {data.error}
          </div>
        )}
      </div>
    </main>
  );
}
