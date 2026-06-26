import { NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import path from "node:path";

const repoRoot = path.resolve(process.cwd(), "..");
const backendDir = path.join(repoRoot, "backend");
const configPath = path.join(backendDir, ".mcp.json");
const exampleConfigPath = path.join(backendDir, ".mcp.example.json");

type Plugin = {
  name: string;
  type: string;
  url: string;
  enabled: boolean;
  configured: boolean;
  headerKeys: string[];
};

async function readJson(filePath: string) {
  const raw = await readFile(filePath, "utf-8");
  return JSON.parse(raw);
}

function toPlugins(raw: unknown, configured: boolean): Plugin[] {
  if (!raw || typeof raw !== "object") return [];
  const servers = (raw as { servers?: unknown }).servers;
  if (!servers || typeof servers !== "object") return [];

  return Object.entries(servers).map(([name, value]) => {
    const server = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
    const headers = server.headers && typeof server.headers === "object" ? server.headers : {};
    return {
      name,
      type: String(server.type ?? "http"),
      url: String(server.url ?? ""),
      enabled: server.enabled !== false,
      configured,
      headerKeys: Object.keys(headers),
    };
  });
}

export async function GET() {
  try {
    const configured = toPlugins(await readJson(configPath), true);
    if (configured.length > 0) {
      return NextResponse.json({ plugins: configured, source: "configured" });
    }
  } catch {
    // Fall back to examples below.
  }

  try {
    const examples = toPlugins(await readJson(exampleConfigPath), false);
    return NextResponse.json({ plugins: examples, source: "example" });
  } catch {
    return NextResponse.json({ plugins: [], source: "none" });
  }
}
