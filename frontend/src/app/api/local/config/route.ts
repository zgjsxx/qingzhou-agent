import { NextRequest, NextResponse } from "next/server";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const repoRoot = path.resolve(process.cwd(), "..");
const backendDir = path.join(repoRoot, "backend");
const configPath = path.join(backendDir, ".agent_config.json");

const defaultHost = {
  host: "",
  user: "",
  port: 22,
  keyFile: "",
  privateKey: "",
  password: "",
  extraArgs: "",
};

const defaultConfig = {
  llm: {
    adapterType: "anthropic",
    model: "glm-5.1",
    apiKey: "",
    baseUrl: "",
  },
  ssh: [] as typeof defaultHost[],
  weixin: {
    enabled: false,
  },
  telegram: {
    enabled: false,
    botToken: "",
    allowedUsers: "",
    requireMention: true,
    mergeWaitSeconds: 3,
  },
};

function migrateSsh(sshRaw: unknown): typeof defaultHost[] {
  if (Array.isArray(sshRaw)) {
    return sshRaw.map((h) => ({ ...defaultHost, ...h }));
  }
  if (sshRaw && typeof sshRaw === "object") {
    return [{ ...defaultHost, ...sshRaw as typeof defaultHost }];
  }
  return [];
}

async function readConfig() {
  try {
    const raw = await readFile(configPath, "utf-8");
    const parsed = JSON.parse(raw);
    return {
      llm: { ...defaultConfig.llm, ...(parsed.llm ?? {}) },
      ssh: migrateSsh(parsed.ssh),
      weixin: { ...defaultConfig.weixin, ...(parsed.weixin ?? {}) },
      telegram: { ...defaultConfig.telegram, ...(parsed.telegram ?? {}) },
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
    ssh: migrateSsh(body.ssh),
    weixin: { ...defaultConfig.weixin, ...(body.weixin ?? {}) },
    telegram: { ...defaultConfig.telegram, ...(body.telegram ?? {}) },
  };
  await mkdir(backendDir, { recursive: true });
  await writeFile(configPath, `${JSON.stringify(nextConfig, null, 2)}\n`, "utf-8");
  return NextResponse.json(nextConfig);
}
