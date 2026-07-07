import { NextResponse } from "next/server";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

const repoRoot = path.resolve(process.cwd(), "..");

function resolveSkillsDir() {
  const configured = process.env.AGENT_SKILLS_DIR?.trim();
  if (!configured) {
    return path.join(repoRoot, "skills");
  }
  return path.isAbsolute(configured)
    ? configured
    : path.resolve(repoRoot, configured);
}

function parseSkill(raw: string, directory: string) {
  const frontmatter = raw.startsWith("---") ? raw.split("---", 3)[1] : "";
  const meta: Record<string, string> = {};
  for (const line of frontmatter.split(/\r?\n/)) {
    const index = line.indexOf(":");
    if (index === -1) continue;
    meta[line.slice(0, index).trim()] = line.slice(index + 1).trim().replace(/^['"]|['"]$/g, "");
  }

  const fallback =
    raw
      .split(/\r?\n/)
      .map((line) => line.trim())
      .find((line) => line.startsWith("#"))
      ?.replace(/^#+\s*/, "") || directory;

  return {
    name: meta.name || directory,
    description: meta.description || meta.when_to_use || fallback,
    directory,
  };
}

export async function GET() {
  const skillsDir = resolveSkillsDir();
  try {
    const entries = await readdir(skillsDir, { withFileTypes: true });
    const skills = [];
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      try {
        const raw = await readFile(path.join(skillsDir, entry.name, "SKILL.md"), "utf-8");
        skills.push(parseSkill(raw, entry.name));
      } catch {
        // Skip malformed skill folders.
      }
    }
    return NextResponse.json({ skills });
  } catch {
    return NextResponse.json({ skills: [] });
  }
}
