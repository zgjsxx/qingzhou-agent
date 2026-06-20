"use client";

import { useEffect, useState } from "react";
import { Settings, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "../ui/sheet";
import { Textarea } from "../ui/textarea";

type Skill = {
  name: string;
  description: string;
  directory: string;
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
  const [skills, setSkills] = useState<Skill[]>([]);
  const [config, setConfig] = useState<AgentConfig>(emptyConfig);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetch("/api/local/skills")
      .then((res) => res.json())
      .then((data) => setSkills(Array.isArray(data.skills) ? data.skills : []))
      .catch(() => setSkills([]));

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

  return (
    <div className="flex items-center gap-1">
      <Sheet>
        <SheetTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5"
          >
            <Sparkles className="size-4" />
            &#25216;&#33021;
          </Button>
        </SheetTrigger>
        <SheetContent
          side="left"
          className="w-[360px] overflow-y-auto sm:max-w-md"
        >
          <SheetHeader>
            <SheetTitle>&#25216;&#33021;</SheetTitle>
            <SheetDescription>
              Skills currently available to the agent.
            </SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-3 px-4 pb-4">
            {skills.length === 0 ? (
              <p className="text-muted-foreground text-sm">No skills found.</p>
            ) : (
              skills.map((skill) => (
                <div
                  key={skill.directory}
                  className="border-b pb-3 last:border-b-0"
                >
                  <div className="text-sm font-medium">{skill.name}</div>
                  <div className="text-muted-foreground mt-1 text-sm">
                    {skill.description}
                  </div>
                  <div className="text-muted-foreground mt-1 text-xs">
                    {skill.directory}
                  </div>
                </div>
              ))
            )}
          </div>
        </SheetContent>
      </Sheet>

      <Sheet>
        <SheetTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5"
          >
            <Settings className="size-4" />
            &#37197;&#32622;
          </Button>
        </SheetTrigger>
        <SheetContent
          side="left"
          className="w-[420px] overflow-y-auto sm:max-w-lg"
        >
          <SheetHeader>
            <SheetTitle>&#37197;&#32622;</SheetTitle>
            <SheetDescription>
              Saved to backend/.agent_config.json and ignored by git.
            </SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-6 px-4 pb-4">
            <section className="flex flex-col gap-3">
              <h3 className="text-sm font-semibold">LLM</h3>
              <LabeledInput
                label="Adapter"
                value={config.llm.adapterType}
                onChange={(value) => updateConfig("llm", "adapterType", value)}
              />
              <LabeledInput
                label="Model"
                value={config.llm.model}
                onChange={(value) => updateConfig("llm", "model", value)}
              />
              <LabeledInput
                label="Base URL"
                value={config.llm.baseUrl}
                onChange={(value) => updateConfig("llm", "baseUrl", value)}
              />
              <LabeledInput
                label="API Key"
                type="password"
                value={config.llm.apiKey}
                onChange={(value) => updateConfig("llm", "apiKey", value)}
              />
              <p className="text-muted-foreground text-xs">
                Environment variables take precedence over these LLM settings.
                Restart the LangGraph backend after changing them.
              </p>
            </section>

            <section className="flex flex-col gap-3">
              <h3 className="text-sm font-semibold">SSH</h3>
              <LabeledInput
                label="Host"
                value={config.ssh.host}
                onChange={(value) => updateConfig("ssh", "host", value)}
              />
              <LabeledInput
                label="User"
                value={config.ssh.user}
                onChange={(value) => updateConfig("ssh", "user", value)}
              />
              <LabeledInput
                label="Port"
                type="number"
                value={String(config.ssh.port || 22)}
                onChange={(value) => updateConfig("ssh", "port", Number(value || 22))}
              />
              <LabeledInput
                label="Key File"
                value={config.ssh.keyFile}
                onChange={(value) => updateConfig("ssh", "keyFile", value)}
              />
              <div className="flex flex-col gap-2">
                <Label htmlFor="ssh-private-key">Private Key</Label>
                <Textarea
                  id="ssh-private-key"
                  value={config.ssh.privateKey}
                  onChange={(event) =>
                    updateConfig("ssh", "privateKey", event.target.value)
                  }
                  className="min-h-32 font-mono text-xs"
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                />
              </div>
              <LabeledInput
                label="Extra Args"
                value={config.ssh.extraArgs}
                onChange={(value) => updateConfig("ssh", "extraArgs", value)}
              />
            </section>

            <Button
              onClick={saveConfig}
              disabled={saving}
            >
              {saving ? "Saving..." : "Save config"}
            </Button>
          </div>
        </SheetContent>
      </Sheet>
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
