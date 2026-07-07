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
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
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

export async function POST(request: NextRequest) {
  const formData = await request.formData();
  const file = formData.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ error: "file is required" }, { status: 400 });
  }

  if (!supportedTypes.has(file.type)) {
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
    mimeType: file.type,
    size: file.size,
    path: filePath,
  });
}
