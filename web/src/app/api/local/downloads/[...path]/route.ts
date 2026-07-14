import { NextRequest, NextResponse } from "next/server";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

export const runtime = "nodejs";

const repoRoot = path.resolve(process.cwd(), "..");

const MAX_FILE_SIZE = 100 * 1024 * 1024; // 100 MB

// MIME type map for common output formats
const MIME_MAP: Record<string, string> = {
  csv: "text/csv",
  pdf: "application/pdf",
  py: "text/x-python",
  js: "text/javascript",
  ts: "text/typescript",
  json: "application/json",
  txt: "text/plain",
  md: "text/markdown",
  html: "text/html",
  xml: "text/xml",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  png: "image/png",
  jpg: "image/jpeg",
  gif: "image/gif",
  wav: "audio/wav",
  mp3: "audio/mpeg",
  ogg: "audio/ogg",
  webm: "audio/webm",
  zip: "application/zip",
};

function getMimeType(filename: string): string {
  const ext = path.extname(filename).toLowerCase().slice(1);
  return MIME_MAP[ext] || "application/octet-stream";
}

function getAsciiFallback(filename: string): string {
  return filename.replace(/[^\x20-\x7E]/g, "_") || "download";
}

function formatContentDisposition(filename: string, mimeType: string): string {
  const asciiFallback = getAsciiFallback(filename);
  const encoded = encodeURIComponent(filename);
  const disposition = mimeType.startsWith("audio/") ? "inline" : "attachment";
  if (asciiFallback === filename) {
    return `${disposition}; filename="${filename}"`;
  }
  return `${disposition}; filename="${asciiFallback}"; filename*=UTF-8''${encoded}`;
}

/**
 * Search for a file under .agent_outputs/files/ and .agent_outputs/shell/
 * when the direct path doesn't exist. Returns the most recently modified match.
 */
async function findInAgentOutputs(
  relativePath: string,
): Promise<{ filePath: string; mtimeMs: number; isFile: () => boolean; size: number } | null> {
  const agentDirs = [
    path.join(repoRoot, ".agent_outputs", "files"),
    path.join(repoRoot, ".agent_outputs", "shell"),
  ];

  const matches: { filePath: string; mtimeMs: number; isFile: () => boolean; size: number }[] = [];

  for (const agentDir of agentDirs) {
    let threadDirs: string[];
    try {
      threadDirs = await readdir(agentDir);
    } catch {
      // Directory doesn't exist or isn't readable — skip
      continue;
    }

    for (const threadId of threadDirs) {
      const candidate = path.join(agentDir, threadId, relativePath);
      try {
        const s = await stat(candidate);
        if (s.isFile()) {
          // Verify the file is still within the agent output directory (path traversal guard)
          const rel = path.relative(repoRoot, candidate);
          if (!rel.startsWith("..") && !path.isAbsolute(rel)) {
            matches.push({ filePath: candidate, mtimeMs: s.mtimeMs, isFile: () => s.isFile(), size: s.size });
          }
        }
      } catch {
        // File doesn't exist in this thread dir — skip
        continue;
      }
    }
  }

  if (matches.length === 0) return null;

  // Return the most recently modified file
  matches.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return matches[0];
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path: segments } = await params;

  // Reconstruct the relative path from the URL segments
  const relativePath = segments.join("/");

  // Resolve against repo root and ensure no path traversal
  const filePath = path.resolve(repoRoot, relativePath);
  const rel = path.relative(repoRoot, filePath);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    return NextResponse.json({ error: "path traversal denied" }, { status: 403 });
  }

  // Check file exists and get size
  let fileStat;
  let resolvedPath = filePath;
  try {
    fileStat = await stat(filePath);
  } catch {
    // Direct path not found — try fallback search in .agent_outputs
    const fallback = await findInAgentOutputs(relativePath);
    if (!fallback) {
      return NextResponse.json({ error: "file not found" }, { status: 404 });
    }
    resolvedPath = fallback.filePath;
    fileStat = { isFile: fallback.isFile, size: fallback.size, mtimeMs: fallback.mtimeMs };
  }

  if (!fileStat.isFile()) {
    return NextResponse.json({ error: "not a file" }, { status: 400 });
  }

  if (fileStat.size > MAX_FILE_SIZE) {
    return NextResponse.json(
      { error: `file too large; max ${MAX_FILE_SIZE} bytes` },
      { status: 413 },
    );
  }

  // Read and return the file
  const buffer = await readFile(resolvedPath);
  const filename = path.basename(resolvedPath);
  const mimeType = getMimeType(filename);

  return new NextResponse(buffer, {
    status: 200,
    headers: {
      "Content-Type": mimeType,
      "Content-Disposition": formatContentDisposition(filename, mimeType),
      "Content-Length": String(buffer.length),
    },
  });
}
