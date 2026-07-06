"use client";

import { useEffect, useState } from "react";
import {
  Activity,
  ChevronRight,
  Plus,
  Plug,
  Save,
  Settings,
  Sparkles,
  Trash2,
  XIcon,
} from "lucide-react";
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
  credentialConfigured: boolean;
  defaultParameters: {
    maxResults: number;
    searchDepth: "basic" | "advanced";
  } | null;
};

type TavilyConfig = {
  apiKey: string;
  enabled: boolean;
  maxResults: number;
  searchDepth: "basic" | "advanced";
};

type SshHost = {
  host: string;
  user: string;
  port: number;
  keyFile: string;
  privateKey: string;
  password: string;
  extraArgs: string;
};

type AgentConfig = {
  llm: {
    adapterType: string;
    model: string;
    apiKey: string;
    baseUrl: string;
  };
  ssh: SshHost[];
  weixin: {
    enabled: boolean;
  };
  telegram: {
    enabled: boolean;
    botToken: string;
    allowedUsers: string;
    requireMention: boolean;
    mergeWaitSeconds: number;
  };
};

const emptyHost: SshHost = {
  host: "",
  user: "",
  port: 22,
  keyFile: "",
  privateKey: "",
  password: "",
  extraArgs: "",
};

const emptyConfig: AgentConfig = {
  llm: { adapterType: "anthropic", model: "glm-5.1", apiKey: "", baseUrl: "" },
  ssh: [],
  weixin: { enabled: false },
  telegram: {
    enabled: false,
    botToken: "",
    allowedUsers: "",
    requireMention: true,
    mergeWaitSeconds: 3,
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
    value: string | number | boolean,
  ) => {
    setConfig((current) => ({
      ...current,
      [section]: {
        ...current[section],
        [key]: value,
      },
    }));
  };

  const updateSshHost = (
    index: number,
    key: keyof SshHost,
    value: string | number,
  ) => {
    setConfig((current) => {
      const hosts = [...current.ssh];
      hosts[index] = { ...hosts[index], [key]: value };
      return { ...current, ssh: hosts };
    });
  };

  const addSshHost = () => {
    setConfig((current) => ({
      ...current,
      ssh: [...current.ssh, { ...emptyHost }],
    }));
  };

  const removeSshHost = (index: number) => {
    setConfig((current) => ({
      ...current,
      ssh: current.ssh.filter((_, i) => i !== index),
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

  const configureTavily = async (settings: TavilyConfig) => {
    setPluginSavingName("tavily");
    try {
      const res = await fetch("/api/local/plugins", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "configure",
          name: "tavily",
          ...settings,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error ?? `HTTP ${res.status}`);
      }
      await loadPlugins();
      toast.success("Tavily Web Search saved", {
        description: "Restart the backend to load the Tavily MCP tools.",
      });
    } catch (error) {
      toast.error("Failed to save Tavily", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
      throw error;
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
          {/* 这里故意使用 HTML 数字实体，而不是直接写中文“插件”。
              这两个菜单项之前曾被某次 Windows/编辑器编码转换保存成 Mojibake
              （例如“插件”变成“鎻掍欢”），实体写法能让源码保持 ASCII，
              浏览器渲染时仍显示正确中文，避免再次被错误编码污染。 */}
          &#25554;&#20214;
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start gap-2 px-3"
          asChild
        >
          <Link href="/monitor">
            <Activity className="size-4" />
            {/* 同上，数字实体对应“运行监控”。保留这段注释是为了提醒后续维护者：
                看到实体不要改回直接中文，否则在当前 Windows 工具链/终端编码组合下，
                有机会再次被保存或构建成乱码。 */}
            &#36816;&#34892;&#30417;&#25511;
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
                  onConfigureTavily={configureTavily}
                />
              ) : (
                <ConfigPage
                  config={config}
                  saving={saving}
                  onSave={saveConfig}
                  onChange={updateConfig}
                  onUpdateSshHost={updateSshHost}
                  onAddSshHost={addSshHost}
                  onRemoveSshHost={removeSshHost}
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
  onConfigureTavily: (settings: TavilyConfig) => Promise<void>;
}) {
  const { plugins, pluginSavingName, onToggle, onConfigureTavily } = props;

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
        {plugins.map((plugin) =>
          plugin.name === "tavily" ? (
            <TavilyPluginCard
              key="tavily"
              plugin={plugin}
              saving={pluginSavingName === "tavily"}
              onSave={onConfigureTavily}
            />
          ) : (
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
                        <span className="text-muted-foreground text-xs">
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
                  <p className="text-muted-foreground text-xs font-medium">
                    URL
                  </p>
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
          ),
        )}
      </div>
    </div>
  );
}

function TavilyPluginCard(props: {
  plugin: Plugin;
  saving: boolean;
  onSave: (settings: TavilyConfig) => Promise<void>;
}) {
  const { plugin, saving, onSave } = props;
  const [apiKey, setApiKey] = useState("");
  const [enabled, setEnabled] = useState(
    plugin.configured ? plugin.enabled : true,
  );
  const [maxResults, setMaxResults] = useState(
    plugin.defaultParameters?.maxResults ?? 8,
  );
  const [searchDepth, setSearchDepth] = useState<"basic" | "advanced">(
    plugin.defaultParameters?.searchDepth ?? "basic",
  );

  useEffect(() => {
    setEnabled(plugin.configured ? plugin.enabled : true);
    setMaxResults(plugin.defaultParameters?.maxResults ?? 8);
    setSearchDepth(plugin.defaultParameters?.searchDepth ?? "basic");
    setApiKey("");
  }, [plugin]);

  return (
    <Card className="gap-4 rounded-lg md:col-span-2">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="text-base">Tavily Web Search</CardTitle>
            <CardDescription>
              Search, extract, map, and crawl the live web through MCP.
            </CardDescription>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Switch
              checked={enabled}
              disabled={saving}
              onCheckedChange={setEnabled}
              aria-label="Enable Tavily Web Search"
            />
            <span className="text-xs">{enabled ? "Enabled" : "Disabled"}</span>
          </div>
        </div>
      </CardHeader>
      <CardContent className="grid gap-5 sm:grid-cols-2">
        <div className="grid gap-2 sm:col-span-2">
          <Label htmlFor="tavily-api-key">API Key</Label>
          <Input
            id="tavily-api-key"
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={
              plugin.credentialConfigured
                ? "Configured; leave blank to keep the current key"
                : "tvly-..."
            }
            autoComplete="off"
          />
          <p className="text-muted-foreground text-xs">
            Stored only in <code>backend/.mcp.json</code> and never returned by
            the configuration API.
          </p>
        </div>

        <div className="grid gap-2">
          <Label htmlFor="tavily-max-results">Default results</Label>
          <Input
            id="tavily-max-results"
            type="number"
            min={1}
            max={20}
            value={maxResults}
            onChange={(event) => setMaxResults(Number(event.target.value))}
          />
        </div>

        <div className="grid gap-2">
          <Label>Search depth</Label>
          <div className="bg-muted grid grid-cols-2 gap-1 rounded-md p-1">
            {(["basic", "advanced"] as const).map((depth) => (
              <Button
                key={depth}
                type="button"
                size="sm"
                variant={searchDepth === depth ? "secondary" : "ghost"}
                onClick={() => setSearchDepth(depth)}
                disabled={saving}
                className="capitalize"
              >
                {depth}
              </Button>
            ))}
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 border-t pt-4 sm:col-span-2">
          <p className="text-muted-foreground text-xs">
            Changes take effect after the backend restarts.
          </p>
          <Button
            type="button"
            disabled={saving || maxResults < 1 || maxResults > 20}
            onClick={() =>
              onSave({ apiKey, enabled, maxResults, searchDepth }).catch(
                () => undefined,
              )
            }
          >
            <Save className="size-4" />
            {saving ? "Saving..." : "Save"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function ConfigPage(props: {
  config: AgentConfig;
  saving: boolean;
  onSave: () => void;
  onChange: (
    section: keyof AgentConfig,
    key: string,
    value: string | number | boolean,
  ) => void;
  onUpdateSshHost: (
    index: number,
    key: keyof SshHost,
    value: string | number,
  ) => void;
  onAddSshHost: () => void;
  onRemoveSshHost: (index: number) => void;
}) {
  const [activeSection, setActiveSection] = useState<
    "llm" | "ssh" | "weixin" | "telegram"
  >("llm");
  const [selectedSshIndex, setSelectedSshIndex] = useState(0);

  useEffect(() => {
    /* SSH 主机现在支持动态增删。
       这里在列表长度变化后主动修正选中下标，避免删除最后一项或切换数据源后，
       右侧详情仍指向一个已经不存在的 index，造成空白表单或受控组件告警。 */
    if (props.config.ssh.length === 0) {
      setSelectedSshIndex(0);
      return;
    }
    if (selectedSshIndex >= props.config.ssh.length) {
      setSelectedSshIndex(props.config.ssh.length - 1);
    }
  }, [props.config.ssh.length, selectedSshIndex]);

  const selectedSsh = props.config.ssh[selectedSshIndex] ?? null;

  const navItems = [
    {
      key: "llm" as const,
      label: "LLM",
      summary:
        props.config.llm.model ||
        props.config.llm.adapterType ||
        "Not configured",
      count: null,
    },
    {
      key: "ssh" as const,
      label: "SSH Hosts",
      summary:
        props.config.ssh.length > 0
          ? `${props.config.ssh.length} host${props.config.ssh.length > 1 ? "s" : ""}`
          : "No hosts",
      count: props.config.ssh.length,
    },
    {
      key: "weixin" as const,
      label: "Weixin",
      summary: props.config.weixin.enabled ? "Enabled" : "Disabled",
      count: null,
    },
    {
      key: "telegram" as const,
      label: "Telegram",
      summary: props.config.telegram.enabled ? "Enabled" : "Disabled",
      count: null,
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <div className="grid min-h-[620px] grid-cols-1 gap-5 lg:grid-cols-[200px_minmax(0,1fr)]">
        <aside className="bg-muted/30 flex flex-col rounded-lg border p-2">
          <div className="px-3 py-2">
            <h3 className="text-sm font-semibold">Settings</h3>
            <p className="text-muted-foreground text-xs leading-5">
              Select a section to edit.
            </p>
          </div>

          <div className="mt-1 flex flex-col gap-1">
            {navItems.map((item) => {
              const active = activeSection === item.key;
              return (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setActiveSection(item.key)}
                  className={`flex items-center justify-between rounded-md px-3 py-2 text-left transition ${
                    active
                      ? "bg-background text-foreground shadow-sm"
                      : "text-muted-foreground hover:bg-background/70 hover:text-foreground"
                  }`}
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium">{item.label}</div>
                    <div className="truncate text-xs">{item.summary}</div>
                  </div>
                  <div className="ml-3 flex shrink-0 items-center gap-2">
                    {item.count !== null ? (
                      <span className="bg-muted rounded px-1.5 py-0.5 text-[11px] font-medium">
                        {item.count}
                      </span>
                    ) : null}
                    <ChevronRight
                      className={`size-4 ${active ? "opacity-100" : "opacity-50"}`}
                    />
                  </div>
                </button>
              );
            })}
          </div>
        </aside>

        <div className="min-w-0">
          {activeSection === "llm" ? (
            <Card className="overflow-hidden rounded-lg">
              <CardHeader>
                <CardTitle>LLM</CardTitle>
                <CardDescription>
                  Environment variables still take precedence. Restart the
                  backend after changing these values.
                </CardDescription>
              </CardHeader>
              <CardContent className="grid max-w-2xl gap-4">
                <LabeledInput
                  label="Adapter"
                  value={props.config.llm.adapterType}
                  onChange={(value) =>
                    props.onChange("llm", "adapterType", value)
                  }
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
          ) : activeSection === "ssh" ? (
            <Card className="rounded-lg">
              <CardHeader>
                <CardTitle>SSH Hosts</CardTitle>
                <CardDescription>
                  Configure one or more SSH hosts. The agent auto-selects by
                  host matching; the first entry is the default.
                </CardDescription>
              </CardHeader>
              <CardContent className="grid min-w-0 gap-5 2xl:grid-cols-[240px_minmax(0,1fr)]">
                <div className="flex flex-col gap-3">
                  <div className="space-y-1">
                    <div className="text-sm font-medium">Host list</div>
                    <div className="text-muted-foreground text-xs leading-5">
                      Pick a host on the left, then edit its details on the
                      right.
                    </div>
                  </div>

                  <div className="flex max-h-[520px] flex-col gap-2 overflow-y-auto pr-1">
                    {props.config.ssh.map((ssh, index) => {
                      const active = selectedSshIndex === index;
                      return (
                        <button
                          key={index}
                          type="button"
                          onClick={() => setSelectedSshIndex(index)}
                          className={`rounded-md border px-3 py-3 text-left transition ${
                            active
                              ? "border-foreground/20 bg-muted"
                              : "hover:bg-muted/60"
                          }`}
                        >
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="truncate text-sm font-medium">
                                {ssh.host || `未命名主机 ${index + 1}`}
                              </div>
                              <div className="text-muted-foreground truncate text-xs">
                                {ssh.user || "user not set"}
                                {ssh.port ? `:${ssh.port}` : ""}
                              </div>
                            </div>
                            {index === 0 ? (
                              <span className="bg-muted rounded px-1.5 py-0.5 text-[11px] font-medium">
                                Default
                              </span>
                            ) : null}
                          </div>
                          <div className="text-muted-foreground mt-2 flex flex-wrap gap-2 text-[11px]">
                            <span>
                              {ssh.keyFile || ssh.privateKey ? "Key" : "No key"}
                            </span>
                            <span>
                              {ssh.password ? "Password" : "No password"}
                            </span>
                            <span>
                              {ssh.extraArgs ? "Extra args" : "Standard"}
                            </span>
                          </div>
                        </button>
                      );
                    })}
                  </div>

                  <Button
                    variant="outline"
                    size="sm"
                    className="w-full"
                    onClick={() => {
                      /* 新增主机后立即切到 SSH 页，并把焦点选中到新项。
                         这样用户点击 “Add host” 后会直接看到新建条目的详情，不需要再额外点一次列表。 */
                      props.onAddSshHost();
                      setActiveSection("ssh");
                      setSelectedSshIndex(props.config.ssh.length);
                    }}
                  >
                    <Plus className="size-4" />
                    Add host
                  </Button>
                </div>

                <div className="min-w-0 overflow-hidden">
                  {selectedSsh ? (
                    <div className="grid gap-5">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div>
                          <div className="text-base font-semibold">
                            {selectedSsh.host ||
                              `未命名主机 ${selectedSshIndex + 1}`}
                          </div>
                          <div className="text-muted-foreground text-sm">
                            Edit connection, authentication, and advanced
                            options.
                          </div>
                        </div>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-destructive hover:text-destructive gap-2"
                          onClick={() =>
                            props.onRemoveSshHost(selectedSshIndex)
                          }
                          disabled={props.config.ssh.length <= 1}
                        >
                          <Trash2 className="size-4" />
                          Remove host
                        </Button>
                      </div>

                      <section className="grid gap-4">
                        <div className="text-sm font-medium">Connection</div>
                        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_96px]">
                          <LabeledInput
                            label="Host"
                            value={selectedSsh.host}
                            onChange={(value) =>
                              props.onUpdateSshHost(
                                selectedSshIndex,
                                "host",
                                value,
                              )
                            }
                          />
                          <LabeledInput
                            label="Port"
                            type="number"
                            value={String(selectedSsh.port || 22)}
                            onChange={(value) =>
                              props.onUpdateSshHost(
                                selectedSshIndex,
                                "port",
                                Number(value || 22),
                              )
                            }
                          />
                        </div>
                        <LabeledInput
                          label="User"
                          value={selectedSsh.user}
                          onChange={(value) =>
                            props.onUpdateSshHost(
                              selectedSshIndex,
                              "user",
                              value,
                            )
                          }
                        />
                      </section>

                      <section className="grid gap-4">
                        <div className="text-sm font-medium">
                          Authentication
                        </div>
                        <LabeledInput
                          label="Key File"
                          value={selectedSsh.keyFile}
                          onChange={(value) =>
                            props.onUpdateSshHost(
                              selectedSshIndex,
                              "keyFile",
                              value,
                            )
                          }
                        />
                        <div className="flex flex-col gap-2">
                          <Label
                            htmlFor={`ssh-private-key-${selectedSshIndex}`}
                          >
                            Private Key
                          </Label>
                          <Textarea
                            id={`ssh-private-key-${selectedSshIndex}`}
                            value={selectedSsh.privateKey}
                            onChange={(event) =>
                              props.onUpdateSshHost(
                                selectedSshIndex,
                                "privateKey",
                                event.target.value,
                              )
                            }
                            className="min-h-44 font-mono text-xs"
                            placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                          />
                        </div>
                        <LabeledInput
                          label="Password"
                          type="password"
                          value={selectedSsh.password}
                          onChange={(value) =>
                            props.onUpdateSshHost(
                              selectedSshIndex,
                              "password",
                              value,
                            )
                          }
                        />
                      </section>

                      <section className="grid gap-4">
                        <div className="text-sm font-medium">Advanced</div>
                        <LabeledInput
                          label="Extra Args"
                          value={selectedSsh.extraArgs}
                          onChange={(value) =>
                            props.onUpdateSshHost(
                              selectedSshIndex,
                              "extraArgs",
                              value,
                            )
                          }
                        />
                      </section>
                    </div>
                  ) : (
                    <div className="text-muted-foreground flex min-h-[320px] items-center justify-center rounded-md border border-dashed text-sm">
                      No SSH host selected.
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          ) : activeSection === "weixin" ? (
            <Card className="overflow-hidden rounded-lg">
              <CardHeader>
                <CardTitle>Weixin iLink</CardTitle>
                <CardDescription>
                  Personal WeChat direct messages through an iLink bot account.
                </CardDescription>
              </CardHeader>
              <CardContent className="grid max-w-2xl gap-5">
                <div className="flex items-center justify-between gap-4 rounded-md border p-4">
                  <div>
                    <Label htmlFor="weixin-enabled">Enable Weixin bridge</Label>
                    <p className="text-muted-foreground mt-1 text-xs">
                      The backend loads saved QR-login credentials after restart.
                    </p>
                  </div>
                  <Switch
                    id="weixin-enabled"
                    checked={props.config.weixin.enabled}
                    onCheckedChange={(checked) =>
                      props.onChange("weixin", "enabled", checked)
                    }
                  />
                </div>
                <div className="bg-muted/40 rounded-md border px-4 py-3 font-mono text-xs">
                  python scripts/weixin_login.py
                </div>
              </CardContent>
            </Card>
          ) : (
            <Card className="overflow-hidden rounded-lg">
              <CardHeader>
                <CardTitle>Telegram Bot</CardTitle>
                <CardDescription>
                  Official Telegram Bot API connection using long polling.
                </CardDescription>
              </CardHeader>
              <CardContent className="grid max-w-2xl gap-5">
                <div className="flex items-center justify-between gap-4 rounded-md border p-4">
                  <Label htmlFor="telegram-enabled">
                    Enable Telegram bridge
                  </Label>
                  <Switch
                    id="telegram-enabled"
                    checked={props.config.telegram.enabled}
                    onCheckedChange={(checked) =>
                      props.onChange("telegram", "enabled", checked)
                    }
                  />
                </div>
                <LabeledInput
                  label="Bot Token"
                  type="password"
                  value={props.config.telegram.botToken}
                  onChange={(value) =>
                    props.onChange("telegram", "botToken", value)
                  }
                />
                <LabeledInput
                  label="Allowed User IDs"
                  value={props.config.telegram.allowedUsers}
                  onChange={(value) =>
                    props.onChange("telegram", "allowedUsers", value)
                  }
                />
                <div className="grid gap-2 sm:grid-cols-2">
                  <LabeledInput
                    label="Merge Wait (seconds)"
                    type="number"
                    value={String(
                      props.config.telegram.mergeWaitSeconds,
                    )}
                    onChange={(value) =>
                      props.onChange(
                        "telegram",
                        "mergeWaitSeconds",
                        Number(value),
                      )
                    }
                  />
                  <div className="flex items-center justify-between gap-4 rounded-md border px-4 py-3">
                    <Label htmlFor="telegram-require-mention">
                      Require group mention
                    </Label>
                    <Switch
                      id="telegram-require-mention"
                      checked={props.config.telegram.requireMention}
                      onCheckedChange={(checked) =>
                        props.onChange(
                          "telegram",
                          "requireMention",
                          checked,
                        )
                      }
                    />
                  </div>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
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
  // Backward-compat: old format had ssh as a dict, wrap it into an array
  const sshRaw = data.ssh ?? [];
  const ssh: SshHost[] = Array.isArray(sshRaw)
    ? sshRaw.map((h) => ({ ...emptyHost, ...h }))
    : [{ ...emptyHost, ...(sshRaw as unknown as SshHost) }];
  return {
    llm: { ...emptyConfig.llm, ...(data.llm ?? {}) },
    ssh,
    weixin: { ...emptyConfig.weixin, ...(data.weixin ?? {}) },
    telegram: { ...emptyConfig.telegram, ...(data.telegram ?? {}) },
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
