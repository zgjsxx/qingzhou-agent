import { NextResponse } from "next/server";
import { readFile, writeFile } from "node:fs/promises";
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

async function readConfiguredJson() {
  const raw = await readJson(configPath);
  if (!raw || typeof raw !== "object") {
    throw new Error("Invalid MCP config");
  }
  return raw as { servers?: Record<string, unknown> };
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

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const name = String(body?.name ?? "").trim();
    const enabled = body?.enabled;

    if (!name) {
      return NextResponse.json({ error: "Plugin name is required." }, { status: 400 });
    }
    if (typeof enabled !== "boolean") {
      return NextResponse.json({ error: "Enabled must be a boolean." }, { status: 400 });
    }

    const config = await readConfiguredJson();
    const servers = config.servers;
    if (!servers || typeof servers !== "object") {
      return NextResponse.json({ error: "No configured MCP servers found." }, { status: 400 });
    }

    const server = servers[name];
    if (!server || typeof server !== "object") {
      return NextResponse.json({ error: `Plugin '${name}' was not found.` }, { status: 404 });
    }

    servers[name] = {
      ...(server as Record<string, unknown>),
      enabled,
    };

    await writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`, "utf-8");

    return NextResponse.json({
      ok: true,
      plugin: toPlugins(config, true).find((item) => item.name === name) ?? null,
      message: "Saved. Restart the backend to reload MCP tools.",
    });
  } catch (error) {
    return NextResponse.json(
      {
        error: error instanceof Error ? error.message : "Failed to update MCP plugin.",
      },
      { status: 500 },
    );
  }
}
