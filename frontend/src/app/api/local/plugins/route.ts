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
  credentialConfigured: boolean;
  defaultParameters: {
    maxResults: number;
    searchDepth: "basic" | "advanced";
  } | null;
};

const TAVILY_NAME = "tavily";
const TAVILY_URL = "https://mcp.tavily.com/mcp/";

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

async function readConfiguredJsonOrDefault() {
  try {
    return await readConfiguredJson();
  } catch {
    return { servers: {} as Record<string, unknown> };
  }
}

function parseDefaultParameters(value: unknown): Plugin["defaultParameters"] {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>;
    const maxResults = Number(parsed.max_results);
    const searchDepth =
      parsed.search_depth === "advanced" ? "advanced" : "basic";
    return {
      maxResults: Number.isFinite(maxResults)
        ? Math.max(1, Math.min(20, maxResults))
        : 8,
      searchDepth,
    };
  } catch {
    return null;
  }
}

function toPlugins(raw: unknown, configured: boolean): Plugin[] {
  if (!raw || typeof raw !== "object") return [];
  const servers = (raw as { servers?: unknown }).servers;
  if (!servers || typeof servers !== "object") return [];

  return Object.entries(servers).map(([name, value]) => {
    const server =
      value && typeof value === "object"
        ? (value as Record<string, unknown>)
        : {};
    const headers =
      server.headers && typeof server.headers === "object"
        ? (server.headers as Record<string, unknown>)
        : {};
    const authorization = String(headers.Authorization ?? "").trim();
    return {
      name,
      type: String(server.type ?? "http"),
      url: String(server.url ?? ""),
      enabled: server.enabled !== false,
      configured,
      headerKeys: Object.keys(headers),
      credentialConfigured:
        authorization.startsWith("Bearer ") &&
        authorization.slice("Bearer ".length).trim().length > 0 &&
        !authorization.includes("${"),
      defaultParameters: parseDefaultParameters(headers.DEFAULT_PARAMETERS),
    };
  });
}

export async function GET() {
  let configured: Plugin[] = [];
  try {
    configured = toPlugins(await readJson(configPath), true);
  } catch {
    configured = [];
  }

  let examples: Plugin[] = [];
  try {
    examples = toPlugins(await readJson(exampleConfigPath), false);
  } catch {
    examples = [];
  }

  const configuredNames = new Set(configured.map((plugin) => plugin.name));
  const missingExamples = examples.filter(
    (plugin) => !configuredNames.has(plugin.name),
  );
  return NextResponse.json({
    plugins: [...configured, ...missingExamples],
    source:
      configured.length > 0
        ? "configured"
        : examples.length > 0
          ? "example"
          : "none",
  });
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const name = String(body?.name ?? "").trim();
    const enabled = body?.enabled;
    const action = String(body?.action ?? "toggle");

    if (!name) {
      return NextResponse.json(
        { error: "Plugin name is required." },
        { status: 400 },
      );
    }
    if (typeof enabled !== "boolean") {
      return NextResponse.json(
        { error: "Enabled must be a boolean." },
        { status: 400 },
      );
    }

    if (action === "configure" && name === TAVILY_NAME) {
      const apiKey = String(body?.apiKey ?? "").trim();
      const maxResults = Number(body?.maxResults ?? 8);
      const searchDepth =
        body?.searchDepth === "advanced" ? "advanced" : "basic";
      if (apiKey && (apiKey.length > 512 || /\s/.test(apiKey))) {
        return NextResponse.json(
          {
            error:
              "Tavily API Key must not contain whitespace and must be at most 512 characters.",
          },
          { status: 400 },
        );
      }
      if (!Number.isInteger(maxResults) || maxResults < 1 || maxResults > 20) {
        return NextResponse.json(
          { error: "Max results must be an integer between 1 and 20." },
          { status: 400 },
        );
      }

      const config = await readConfiguredJsonOrDefault();
      const servers = config.servers ?? {};
      const existing =
        servers[name] && typeof servers[name] === "object"
          ? (servers[name] as Record<string, unknown>)
          : {};
      const existingHeaders =
        existing.headers && typeof existing.headers === "object"
          ? (existing.headers as Record<string, unknown>)
          : {};
      const existingAuthorization = String(
        existingHeaders.Authorization ?? "",
      ).trim();
      if (!apiKey && !existingAuthorization) {
        return NextResponse.json(
          {
            error: "Tavily API Key is required for the initial configuration.",
          },
          { status: 400 },
        );
      }

      servers[name] = {
        ...existing,
        enabled,
        type: "http",
        url: TAVILY_URL,
        headers: {
          ...existingHeaders,
          Authorization: apiKey ? `Bearer ${apiKey}` : existingAuthorization,
          DEFAULT_PARAMETERS: JSON.stringify({
            max_results: maxResults,
            search_depth: searchDepth,
          }),
        },
      };
      config.servers = servers;
      await writeFile(
        configPath,
        `${JSON.stringify(config, null, 2)}\n`,
        "utf-8",
      );
      return NextResponse.json({
        ok: true,
        plugin:
          toPlugins(config, true).find((item) => item.name === name) ?? null,
        message: "Saved. Restart the backend to load Tavily MCP tools.",
      });
    }

    const config = await readConfiguredJson();
    const servers = config.servers;
    if (!servers || typeof servers !== "object") {
      return NextResponse.json(
        { error: "No configured MCP servers found." },
        { status: 400 },
      );
    }

    const server = servers[name];
    if (!server || typeof server !== "object") {
      return NextResponse.json(
        { error: `Plugin '${name}' was not found.` },
        { status: 404 },
      );
    }

    servers[name] = {
      ...(server as Record<string, unknown>),
      enabled,
    };

    await writeFile(
      configPath,
      `${JSON.stringify(config, null, 2)}\n`,
      "utf-8",
    );

    return NextResponse.json({
      ok: true,
      plugin:
        toPlugins(config, true).find((item) => item.name === name) ?? null,
      message: "Saved. Restart the backend to reload MCP tools.",
    });
  } catch (error) {
    return NextResponse.json(
      {
        error:
          error instanceof Error
            ? error.message
            : "Failed to update MCP plugin.",
      },
      { status: 500 },
    );
  }
}
