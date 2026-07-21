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

async function findSkillManifests(root: string, relative = ""): Promise<string[]> {
  const dir = path.join(root, relative);
  const entries = await readdir(dir, { withFileTypes: true });
  const manifests: string[] = [];

  for (const entry of entries) {
    const entryRelative = path.join(relative, entry.name);
    if (entry.isDirectory()) {
      manifests.push(...await findSkillManifests(root, entryRelative));
    } else if (entry.isFile() && entry.name === "SKILL.md") {
      manifests.push(entryRelative);
    }
  }

  return manifests.sort((a, b) => a.localeCompare(b));
}

export async function GET() {
  const skillsDir = resolveSkillsDir();
  try {
    const manifests = await findSkillManifests(skillsDir);
    const skills = [];
    for (const manifest of manifests) {
      try {
        const directory = path.dirname(manifest);
        const raw = await readFile(path.join(skillsDir, manifest), "utf-8");
        skills.push(parseSkill(raw, directory));
      } catch {
        // Skip malformed skill folders.
      }
    }
    return NextResponse.json({ skills });
  } catch {
    return NextResponse.json({ skills: [] });
  }
}
