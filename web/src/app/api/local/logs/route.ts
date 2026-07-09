import { NextRequest, NextResponse } from "next/server";
import { open, stat } from "node:fs/promises";
import path from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const repoRoot = path.resolve(process.cwd(), "..");
const sources = {
  agent: [
    path.join(repoRoot, "logs", "agent.jsonl"),
    path.join(repoRoot, ".runtime", "logs", "agent.jsonl"),
  ],
  backend: [path.join(repoRoot, ".runtime", "logs", "backend.out.log")],
  "backend-error": [
    path.join(repoRoot, ".runtime", "logs", "backend.err.log"),
  ],
  frontend: [path.join(repoRoot, ".runtime", "logs", "frontend.out.log")],
  "frontend-error": [
    path.join(repoRoot, ".runtime", "logs", "frontend.err.log"),
  ],
} as const;

type LogSource = keyof typeof sources;
type JsonRecord = Record<string, unknown>;

const sensitiveKey =
  /api[-_]?key|authorization|auth[-_]?token|password|secret|private[-_]?key/i;
const bearerToken = /(Bearer\s+)[^\s'",}]+/gi;

function clampLimit(value: string | null) {
  const parsed = Number(value ?? 200);
  return Number.isFinite(parsed) ? Math.max(20, Math.min(500, parsed)) : 200;
}

function sanitize(value: unknown, key = "", depth = 0): unknown {
  if (sensitiveKey.test(key)) return "[REDACTED]";
  if (depth > 8) return "[TRUNCATED]";
  if (typeof value === "string") {
    const redacted = value.replace(bearerToken, "$1[REDACTED]");
    return redacted.length > 20_000
      ? `${redacted.slice(0, 20_000)}\n… [truncated]`
      : redacted;
  }
  if (Array.isArray(value)) {
    return value.slice(0, 100).map((item) => sanitize(item, "", depth + 1));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([childKey, childValue]) => [
        childKey,
        sanitize(childValue, childKey, depth + 1),
      ]),
    );
  }
  return value;
}

async function readTail(filePath: string, maxBytes: number) {
  const fileStat = await stat(filePath);
  const bytes = Math.min(fileStat.size, maxBytes);
  const offset = Math.max(0, fileStat.size - bytes);
  const handle = await open(filePath, "r");
  try {
    const buffer = Buffer.alloc(bytes);
    await handle.read(buffer, 0, bytes, offset);
    let text = buffer.toString("utf8");
    if (offset > 0) {
      const firstNewline = text.indexOf("\n");
      text = firstNewline >= 0 ? text.slice(firstNewline + 1) : "";
    }
    return { text, fileStat };
  } finally {
    await handle.close();
  }
}

async function readFirstAvailable(paths: readonly string[], maxBytes: number) {
  let missingError: unknown;
  for (const filePath of paths) {
    try {
      return await readTail(filePath, maxBytes);
    } catch (error) {
      const code =
        error && typeof error === "object" && "code" in error
          ? String(error.code)
          : "";
      if (code !== "ENOENT") throw error;
      missingError = error;
    }
  }
  throw missingError;
}

export async function GET(request: NextRequest) {
  const requestedSource = request.nextUrl.searchParams.get("source") ?? "agent";
  if (!(requestedSource in sources)) {
    return NextResponse.json({ error: "unknown log source" }, { status: 400 });
  }

  const source = requestedSource as LogSource;
  const limit = clampLimit(request.nextUrl.searchParams.get("limit"));

  try {
    const { text, fileStat } = await readFirstAvailable(
      sources[source],
      source === "agent" ? 5 * 1024 * 1024 : 1024 * 1024,
    );

    if (source !== "agent") {
      return NextResponse.json({
        source,
        exists: true,
        updatedAt: fileStat.mtime.toISOString(),
        text: text.split(/\r?\n/).slice(-limit).join("\n"),
      });
    }

    const events: JsonRecord[] = [];
    let malformedLines = 0;
    for (const line of text.split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        const parsed = JSON.parse(line);
        if (parsed && typeof parsed === "object") {
          events.push(sanitize(parsed) as JsonRecord);
        }
      } catch {
        malformedLines += 1;
      }
    }

    return NextResponse.json({
      source,
      exists: true,
      updatedAt: fileStat.mtime.toISOString(),
      malformedLines,
      events: events.slice(-limit).reverse(),
    });
  } catch (error) {
    const code =
      error && typeof error === "object" && "code" in error
        ? String(error.code)
        : "";
    if (code === "ENOENT") {
      return NextResponse.json({
        source,
        exists: false,
        events: [],
        text: "",
      });
    }
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "failed to read logs" },
      { status: 500 },
    );
  }
}
