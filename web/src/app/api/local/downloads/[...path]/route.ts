import { NextRequest, NextResponse } from "next/server";
import { readFile, stat } from "node:fs/promises";
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
  zip: "application/zip",
};

function getMimeType(filename: string): string {
  const ext = path.extname(filename).toLowerCase().slice(1);
  return MIME_MAP[ext] || "application/octet-stream";
}

function getAsciiFallback(filename: string): string {
  return filename.replace(/[^\x20-\x7E]/g, "_") || "download";
}

function formatContentDisposition(filename: string): string {
  const asciiFallback = getAsciiFallback(filename);
  const encoded = encodeURIComponent(filename);
  if (asciiFallback === filename) {
    return `attachment; filename="${filename}"`;
  }
  return `attachment; filename="${asciiFallback}"; filename*=UTF-8''${encoded}`;
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
  try {
    fileStat = await stat(filePath);
  } catch {
    return NextResponse.json({ error: "file not found" }, { status: 404 });
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
  const buffer = await readFile(filePath);
  const filename = path.basename(filePath);
  const mimeType = getMimeType(filename);

  return new NextResponse(buffer, {
    status: 200,
    headers: {
      "Content-Type": mimeType,
      "Content-Disposition": formatContentDisposition(filename),
      "Content-Length": String(buffer.length),
    },
  });
}
