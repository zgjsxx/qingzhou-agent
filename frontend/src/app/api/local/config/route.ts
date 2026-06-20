import { NextRequest, NextResponse } from "next/server";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const repoRoot = path.resolve(process.cwd(), "..");
const backendDir = path.join(repoRoot, "backend");
const configPath = path.join(backendDir, ".agent_config.json");

const defaultConfig = {
  llm: {
    adapterType: "anthropic",
    model: "glm-5.1",
    apiKey: "",
    baseUrl: "",
  },
  ssh: {
    host: "",
    user: "",
    port: 22,
    keyFile: "",
    privateKey: "",
    extraArgs: "",
  },
};

async function readConfig() {
  try {
    const raw = await readFile(configPath, "utf-8");
    const parsed = JSON.parse(raw);
    return {
      llm: { ...defaultConfig.llm, ...(parsed.llm ?? {}) },
      ssh: { ...defaultConfig.ssh, ...(parsed.ssh ?? {}) },
    };
  } catch {
    return defaultConfig;
  }
}

export async function GET() {
  return NextResponse.json(await readConfig());
}

export async function POST(request: NextRequest) {
  const body = await request.json();
  const nextConfig = {
    llm: { ...defaultConfig.llm, ...(body.llm ?? {}) },
    ssh: { ...defaultConfig.ssh, ...(body.ssh ?? {}) },
  };
  await mkdir(backendDir, { recursive: true });
  await writeFile(configPath, `${JSON.stringify(nextConfig, null, 2)}\n`, "utf-8");
  return NextResponse.json(nextConfig);
}
