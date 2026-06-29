"use client";

import { useEffect, useState } from "react";
import { Activity, Plug, Settings, Sparkles, XIcon } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";
import { Button } from "../ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../ui/card";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { Textarea } from "../ui/textarea";

type PanelMode = "skills" | "plugins" | "config" | null;

type Skill = {
  name: string;
  description: string;
  directory: string;
};

type Plugin = {
  name: string;
  type: string;
  url: string;
  enabled: boolean;
  configured: boolean;
  headerKeys: string[];
};

type AgentConfig = {
  llm: {
    adapterType: string;
    model: string;
    apiKey: string;
    baseUrl: string;
  };
  ssh: {
    host: string;
    user: string;
    port: number;
    keyFile: string;
    privateKey: string;
    extraArgs: string;
  };
};

const emptyConfig: AgentConfig = {
  llm: { adapterType: "anthropic", model: "glm-5.1", apiKey: "", baseUrl: "" },
  ssh: {
    host: "",
    user: "",
    port: 22,
    keyFile: "",
    privateKey: "",
    extraArgs: "",
  },
};

export function LocalPanels() {
  const [mode, setMode] = useState<PanelMode>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [config, setConfig] = useState<AgentConfig>(emptyConfig);
  const [saving, setSaving] = useState(false);
  const [pluginSavingName, setPluginSavingName] = useState<string | null>(null);

  const loadPlugins = async () => {
    try {
      const res = await fetch("/api/local/plugins");
      const data = await res.json();
      setPlugins(Array.isArray(data.plugins) ? data.plugins : []);
    } catch {
      setPlugins([]);
    }
  };

  useEffect(() => {
    fetch("/api/local/skills")
      .then((res) => res.json())
      .then((data) => setSkills(Array.isArray(data.skills) ? data.skills : []))
      .catch(() => setSkills([]));

    loadPlugins();

    fetch("/api/local/config")
      .then((res) => res.json())
      .then((data) => setConfig(mergeConfig(data)))
      .catch(() => setConfig(emptyConfig));
  }, []);

  const updateConfig = (
    section: keyof AgentConfig,
    key: string,
    value: string | number,
  ) => {
    setConfig((current) => ({
      ...current,
      [section]: {
        ...current[section],
        [key]: value,
      },
    }));
  };

  const saveConfig = async () => {
    setSaving(true);
    try {
      const res = await fetch("/api/local/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setConfig(mergeConfig(await res.json()));
      toast.success("Config saved", {
        description:
          "SSH settings are read live. LLM settings usually require restarting the backend.",
      });
    } catch (error) {
      toast.error("Failed to save config", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setSaving(false);
    }
  };

  const togglePlugin = async (name: string, enabled: boolean) => {
    setPluginSavingName(name);
    setPlugins((current) =>
      current.map((plugin) =>
        plugin.name === name ? { ...plugin, enabled } : plugin,
      ),
    );

    try {
      const res = await fetch("/api/local/plugins", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, enabled }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error ?? `HTTP ${res.status}`);
      }
      await loadPlugins();
      toast.success(enabled ? "Plugin enabled" : "Plugin disabled", {
        description:
          "The config is saved. Restart the backend to apply MCP tool changes.",
      });
    } catch (error) {
      await loadPlugins();
      toast.error("Failed to update plugin", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setPluginSavingName(null);
    }
  };

  return (
    <>
      <div className="flex w-full flex-col gap-1">
        <Button
          variant={mode === "skills" ? "secondary" : "ghost"}
          size="sm"
          className="w-full justify-start gap-2 px-3"
          onClick={() => setMode("skills")}
        >
          <Sparkles className="size-4" />
          &#25216;&#33021;
        </Button>
        <Button
          variant={mode === "config" ? "secondary" : "ghost"}
          size="sm"
          className="w-full justify-start gap-2 px-3"
          onClick={() => setMode("config")}
        >
          <Settings className="size-4" />
          &#37197;&#32622;
        </Button>
        <Button
          variant={mode === "plugins" ? "secondary" : "ghost"}
          size="sm"
          className="w-full justify-start gap-2 px-3"
          onClick={() => setMode("plugins")}
        >
          <Plug className="size-4" />
          鎻掍欢
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start gap-2 px-3"
          asChild
        >
          <Link href="/monitor">
            <Activity className="size-4" />
            杩愯鐩戞帶
          </Link>
        </Button>
      </div>

      {mode && (
        <div className="fixed inset-0 z-50 flex bg-black/45 p-4 backdrop-blur-sm">
          <div className="bg-background mx-auto flex h-full w-full max-w-6xl flex-col overflow-hidden rounded-lg border shadow-xl">
            <div className="flex items-center justify-between border-b px-6 py-4">
              <div>
                <h2 className="text-xl font-semibold">
                  {mode === "skills"
                    ? "Skills"
                    : mode === "plugins"
                      ? "Plugins"
                      : "Configuration"}
                </h2>
                <p className="text-muted-foreground text-sm">
                  {mode === "skills"
                    ? "Project skills available to the agent."
                    : mode === "plugins"
                      ? "Configured MCP plugins available to the main agent."
                      : "Local LLM and SSH settings saved outside git."}
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setMode(null)}
              >
                <XIcon className="size-5" />
                <span className="sr-only">Close</span>
              </Button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto p-6">
              {mode === "skills" ? (
                <SkillsPage skills={skills} />
              ) : mode === "plugins" ? (
                <PluginsPage
                  plugins={plugins}
                  pluginSavingName={pluginSavingName}
                  onToggle={togglePlugin}
                />
              ) : (
                <ConfigPage
                  config={config}
                  saving={saving}
                  onSave={saveConfig}
                  onChange={updateConfig}
                />
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function SkillsPage({ skills }: { skills: Skill[] }) {
  if (skills.length === 0) {
    return (
      <Card className="max-w-xl">
        <CardHeader>
          <CardTitle>No skills found</CardTitle>
          <CardDescription>
            Add skill folders under the project skills directory.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      {skills.map((skill) => (
        <Card
          key={skill.directory}
          className="gap-4 rounded-lg"
        >
          <CardHeader>
            <CardTitle className="text-base">{skill.name}</CardTitle>
            <CardDescription>{skill.directory}</CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm leading-6">{skill.description}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function PluginsPage(props: {
  plugins: Plugin[];
  pluginSavingName: string | null;
  onToggle: (name: string, enabled: boolean) => void;
}) {
  const { plugins, pluginSavingName, onToggle } = props;

  if (plugins.length === 0) {
    return (
      <Card className="max-w-xl">
        <CardHeader>
          <CardTitle>No plugins found</CardTitle>
          <CardDescription>
            Add MCP servers in backend/.mcp.json.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <Card className="max-w-3xl rounded-lg">
        <CardHeader>
          <CardTitle>Plugin Control</CardTitle>
          <CardDescription>
            You can enable or disable configured MCP plugins here. Changes are
            written to <code>backend/.mcp.json</code> and take effect after the
            backend restarts.
          </CardDescription>
        </CardHeader>
      </Card>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {plugins.map((plugin) => (
          <Card
            key={`${plugin.name}-${plugin.url}`}
            className="gap-4 rounded-lg"
          >
            <CardHeader>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <CardTitle className="text-base">{plugin.name}</CardTitle>
                  <CardDescription>
                    {plugin.configured ? "Configured" : "Example"} ·{" "}
                    {plugin.enabled ? "Enabled" : "Disabled"}
                  </CardDescription>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  {plugin.configured ? (
                    <div className="flex items-center gap-2">
                      <Switch
                        checked={plugin.enabled}
                        disabled={pluginSavingName === plugin.name}
                        onCheckedChange={(checked) =>
                          onToggle(plugin.name, checked)
                        }
                        aria-label={`Toggle ${plugin.name}`}
                      />
                      <span className="text-xs text-muted-foreground">
                        {pluginSavingName === plugin.name ? "Saving..." : ""}
                      </span>
                    </div>
                  ) : null}
                  <span className="bg-muted rounded-md px-2 py-1 text-xs font-medium uppercase">
                    {plugin.type}
                  </span>
                </div>
              </div>
            </CardHeader>
            <CardContent className="grid gap-3">
              <div>
                <p className="text-muted-foreground text-xs font-medium">URL</p>
                <p className="font-mono text-xs leading-5 break-all">
                  {plugin.url || "(not set)"}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground text-xs font-medium">
                  Headers
                </p>
                <p className="text-sm leading-6">
                  {plugin.headerKeys.length > 0
                    ? plugin.headerKeys.join(", ")
                    : "(none)"}
                </p>
              </div>
              {!plugin.configured ? (
                <p className="text-muted-foreground text-xs leading-5">
                  Example plugins are read-only. Add them to{" "}
                  <code>backend/.mcp.json</code> before toggling them here.
                </p>
              ) : null}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

function ConfigPage(props: {
  config: AgentConfig;
  saving: boolean;
  onSave: () => void;
  onChange: (
    section: keyof AgentConfig,
    key: string,
    value: string | number,
  ) => void;
}) {
  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card className="rounded-lg">
          <CardHeader>
            <CardTitle>LLM</CardTitle>
            <CardDescription>
              Environment variables still take precedence. Restart the backend
              after changing these values.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <LabeledInput
              label="Adapter"
              value={props.config.llm.adapterType}
              onChange={(value) => props.onChange("llm", "adapterType", value)}
            />
            <LabeledInput
              label="Model"
              value={props.config.llm.model}
              onChange={(value) => props.onChange("llm", "model", value)}
            />
            <LabeledInput
              label="Base URL"
              value={props.config.llm.baseUrl}
              onChange={(value) => props.onChange("llm", "baseUrl", value)}
            />
            <LabeledInput
              label="API Key"
              type="password"
              value={props.config.llm.apiKey}
              onChange={(value) => props.onChange("llm", "apiKey", value)}
            />
          </CardContent>
        </Card>

        <Card className="rounded-lg">
          <CardHeader>
            <CardTitle>SSH</CardTitle>
            <CardDescription>
              Used by run_ssh_command when host, user, or key are omitted.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-[1fr_120px]">
              <LabeledInput
                label="Host"
                value={props.config.ssh.host}
                onChange={(value) => props.onChange("ssh", "host", value)}
              />
              <LabeledInput
                label="Port"
                type="number"
                value={String(props.config.ssh.port || 22)}
                onChange={(value) =>
                  props.onChange("ssh", "port", Number(value || 22))
                }
              />
            </div>
            <LabeledInput
              label="User"
              value={props.config.ssh.user}
              onChange={(value) => props.onChange("ssh", "user", value)}
            />
            <LabeledInput
              label="Key File"
              value={props.config.ssh.keyFile}
              onChange={(value) => props.onChange("ssh", "keyFile", value)}
            />
            <div className="flex flex-col gap-2">
              <Label htmlFor="ssh-private-key">Private Key</Label>
              <Textarea
                id="ssh-private-key"
                value={props.config.ssh.privateKey}
                onChange={(event) =>
                  props.onChange("ssh", "privateKey", event.target.value)
                }
                className="min-h-36 font-mono text-xs"
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
              />
            </div>
            <LabeledInput
              label="Extra Args"
              value={props.config.ssh.extraArgs}
              onChange={(value) => props.onChange("ssh", "extraArgs", value)}
            />
          </CardContent>
        </Card>
      </div>

      <div className="flex justify-end">
        <Button
          size="lg"
          onClick={props.onSave}
          disabled={props.saving}
        >
          {props.saving ? "Saving..." : "Save config"}
        </Button>
      </div>
    </div>
  );
}

function mergeConfig(data: Partial<AgentConfig>): AgentConfig {
  return {
    llm: { ...emptyConfig.llm, ...(data.llm ?? {}) },
    ssh: { ...emptyConfig.ssh, ...(data.ssh ?? {}) },
  };
}

function LabeledInput(props: {
  label: string;
  value: string;
  type?: string;
  onChange: (value: string) => void;
}) {
  const id = props.label.toLowerCase().replace(/\s+/g, "-");
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>{props.label}</Label>
      <Input
        id={id}
        type={props.type ?? "text"}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      />
    </div>
  );
}
