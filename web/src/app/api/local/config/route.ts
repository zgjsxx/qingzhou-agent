import { NextRequest, NextResponse } from "next/server";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const repoRoot = path.resolve(process.cwd(), "..");
const configDir = path.join(repoRoot, "config");
const configPath = path.join(configDir, "xu-agent.json");

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
  discord: {
    enabled: false,
    botToken: "",
    allowedUsers: "",
    requireMention: true,
    mergeWaitSeconds: 3,
    proxy: "",
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
      discord: { ...defaultConfig.discord, ...(parsed.discord ?? {}) },
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
  let existing: Record<string, unknown> = {};
  try {
    const raw = await readFile(configPath, "utf-8");
    const parsed = JSON.parse(raw);
    existing = parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    existing = {};
  }
  const nextConfig = {
    ...existing,
    llm: { ...defaultConfig.llm, ...(body.llm ?? {}) },
    ssh: migrateSsh(body.ssh),
    weixin: { ...defaultConfig.weixin, ...(body.weixin ?? {}) },
    telegram: { ...defaultConfig.telegram, ...(body.telegram ?? {}) },
    discord: { ...defaultConfig.discord, ...(body.discord ?? {}) },
  };
  await mkdir(configDir, { recursive: true });
  await writeFile(configPath, `${JSON.stringify(nextConfig, null, 2)}\n`, "utf-8");
  return NextResponse.json(nextConfig);
}
