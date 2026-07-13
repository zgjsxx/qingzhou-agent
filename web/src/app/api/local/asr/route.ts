import { NextRequest, NextResponse } from "next/server";
import crypto from "node:crypto";
import { execFile } from "node:child_process";
import { mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

export const runtime = "nodejs";

const execFileAsync = promisify(execFile);
const repoRoot = path.resolve(process.cwd(), "..");
const asrRoot = path.join(repoRoot, ".agent_outputs", "asr", "uploads");
const defaultAsrServerUrl = "http://127.0.0.1:8765";
const maxUploadBytes = 25 * 1024 * 1024;
const supportedTypes = new Set([
  "audio/webm",
  "audio/wav",
  "audio/wave",
  "audio/x-wav",
  "audio/mpeg",
  "audio/mp4",
  "audio/ogg",
  "video/webm",
]);

function safeFilename(name: string) {
  const base = path.basename(name || "recording.webm");
  const invalidFilenameChars = '<>:"/\\|?*';
  const cleaned = Array.from(base, (char) => {
    const code = char.charCodeAt(0);
    return code <= 0x1f || invalidFilenameChars.includes(char) ? "_" : char;
  })
    .join("")
    .trim();
  return cleaned || "recording.webm";
}

function pythonExecutable() {
  if (process.env.QINGZHOU_ASR_PYTHON) {
    return process.env.QINGZHOU_ASR_PYTHON;
  }
  return process.platform === "win32"
    ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
    : path.join(repoRoot, ".venv", "bin", "python");
}

function parseJsonOutput(stdout: string) {
  const trimmed = stdout.trim();
  if (!trimmed) {
    return null;
  }
  const lastLine = trimmed.split(/\r?\n/).at(-1) ?? trimmed;
  try {
    return JSON.parse(lastLine);
  } catch {
    return null;
  }
}

function asrServerUrl() {
  return (process.env.QINGZHOU_ASR_URL || defaultAsrServerUrl).replace(/\/+$/, "");
}

async function transcribeWithAsrServer(file: File, audioBuffer: Buffer, language: string) {
  const serverFormData = new FormData();
  serverFormData.append(
    "file",
    new Blob([audioBuffer], { type: file.type || "audio/webm" }),
    safeFilename(file.name),
  );
  serverFormData.append("language", language);

  const response = await fetch(`${asrServerUrl()}/transcribe`, {
    method: "POST",
    body: serverFormData,
    signal: AbortSignal.timeout(10 * 60 * 1000),
  });

  let body: any = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const message = body?.detail || body?.error || "speech recognition failed";
    const error = new Error(message);
    (error as any).status = response.status;
    throw error;
  }

  if (!body?.ok) {
    const error = new Error(body?.error || "speech recognition failed");
    (error as any).status = 500;
    throw error;
  }

  return body;
}

async function transcribeWithPython(file: File, audioBuffer: Buffer, language: string) {
  const uploadId = crypto.randomUUID();
  const directory = path.join(asrRoot, uploadId);
  const filePath = path.join(directory, safeFilename(file.name));
  await mkdir(directory, { recursive: true });
  await writeFile(filePath, audioBuffer);

  try {
    const { stdout } = await execFileAsync(
      pythonExecutable(),
      ["-m", "agent.asr", "--audio", filePath, "--language", language],
      {
        cwd: repoRoot,
        env: {
          ...process.env,
          PYTHONUTF8: "1",
          PYTHONIOENCODING: "utf-8",
        },
        maxBuffer: 20 * 1024 * 1024,
        timeout: 10 * 60 * 1000,
        windowsHide: true,
      },
    );
    return parseJsonOutput(stdout);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

export async function POST(request: NextRequest) {
  const formData = await request.formData();
  const file = formData.get("file");
  const language = String(formData.get("language") || "auto");
  if (!(file instanceof File)) {
    return NextResponse.json({ error: "file is required" }, { status: 400 });
  }

  const mimeType = (file.type || "audio/webm").split(";")[0].toLowerCase();
  if (!supportedTypes.has(mimeType)) {
    return NextResponse.json(
      { error: `unsupported audio type: ${file.type || "unknown"}` },
      { status: 400 },
    );
  }

  if (file.size > maxUploadBytes) {
    return NextResponse.json(
      { error: `audio file is too large; max ${maxUploadBytes} bytes` },
      { status: 400 },
    );
  }

  const audioBuffer = Buffer.from(await file.arrayBuffer());

  try {
    let body: any = null;
    try {
      body = await transcribeWithAsrServer(file, audioBuffer, language);
    } catch (error: any) {
      if (error?.status) {
        throw error;
      }
      body = await transcribeWithPython(file, audioBuffer, language);
    }

    if (!body?.ok) {
      return NextResponse.json(
        { error: body?.error || "speech recognition failed" },
        { status: 500 },
      );
    }

    return NextResponse.json({
      text: body.text || "",
      language: body.language,
      model: body.model,
      device: body.device,
    });
  } catch (error: any) {
    const body = parseJsonOutput(String(error?.stdout || ""));
    return NextResponse.json(
      {
        error:
          body?.error ||
          error?.message ||
          "speech recognition process failed",
      },
      { status: error?.status || (body?.error_type === "AsrDependencyError" ? 501 : 500) },
    );
  }
}
