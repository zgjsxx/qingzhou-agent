import { NextRequest, NextResponse } from "next/server";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";

export const runtime = "nodejs";

const repoRoot = path.resolve(process.cwd(), "..");
const uploadRoot = path.join(repoRoot, ".agent_uploads");
const maxUploadBytes = 50 * 1024 * 1024;

const supportedTypes = new Set([
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/csv",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
]);

const supportedExtensionTypes = new Map([
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".png", "image/png"],
  [".gif", "image/gif"],
  [".webp", "image/webp"],
  [".pdf", "application/pdf"],
  [".txt", "text/plain"],
  [".md", "text/markdown"],
  [".markdown", "text/markdown"],
  [".csv", "text/csv"],
  [".doc", "application/msword"],
  [
    ".docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ],
  [".xls", "application/vnd.ms-excel"],
  [
    ".xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ],
]);

function safeFilename(name: string) {
  const base = path.basename(name || "upload");
  const invalidFilenameChars = '<>:"/\\|?*';
  const cleaned = Array.from(base, (char) => {
    const code = char.charCodeAt(0);
    return code <= 0x1f || invalidFilenameChars.includes(char) ? "_" : char;
  })
    .join("")
    .trim();
  return cleaned || "upload";
}

function getSupportedMimeType(file: File) {
  if (supportedTypes.has(file.type)) {
    return file.type;
  }
  const extension = path.extname(file.name || "").toLowerCase();
  return supportedExtensionTypes.get(extension) || "";
}

export async function POST(request: NextRequest) {
  const formData = await request.formData();
  const file = formData.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ error: "file is required" }, { status: 400 });
  }

  const mimeType = getSupportedMimeType(file);
  if (!mimeType) {
    return NextResponse.json(
      { error: `unsupported file type: ${file.type}` },
      { status: 400 },
    );
  }

  if (file.size > maxUploadBytes) {
    return NextResponse.json(
      { error: `file is too large; max ${maxUploadBytes} bytes` },
      { status: 400 },
    );
  }

  const uploadId = crypto.randomUUID();
  const filename = safeFilename(file.name);
  const directory = path.join(uploadRoot, uploadId);
  const filePath = path.join(directory, filename);
  await mkdir(directory, { recursive: true });
  await writeFile(filePath, Buffer.from(await file.arrayBuffer()));

  return NextResponse.json({
    uploadId,
    filename,
    mimeType,
    size: file.size,
    path: filePath,
  });
}
