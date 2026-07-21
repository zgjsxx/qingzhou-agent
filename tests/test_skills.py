import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(PROJECT_ROOT))

from agent import skills


class SkillsTest(unittest.TestCase):
    def tearDown(self):
        skills.SKILL_REGISTRY.clear()

    def test_default_skills_dir_is_root_skills(self):
        self.assertEqual(
            skills._skills_dir(),
            PROJECT_ROOT / "skills",
        )

    def test_relative_configured_skills_dir_resolves_from_project_root(self):
        with patch.dict(os.environ, {"AGENT_SKILLS_DIR": "custom-skills"}):
            self.assertEqual(
                skills._skills_dir(),
                PROJECT_ROOT / "custom-skills",
            )

    def test_scan_skills_reads_configured_skills(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "demo"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo\n" "description: demo skill\n---\n# Demo\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"AGENT_SKILLS_DIR": str(root)}):
                registry = skills.scan_skills()

        self.assertIn("demo", registry)
        self.assertEqual(registry["demo"].description, "demo skill")

    def test_scan_skills_reads_nested_skill_manifests(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "ems-skills" / "ems-ops"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: ems-ops\n" "description: nested ems skill\n---\n# EMS Ops\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"AGENT_SKILLS_DIR": str(root)}):
                registry = skills.scan_skills()

        self.assertIn("ems-ops", registry)
        self.assertEqual(registry["ems-ops"].description, "nested ems skill")
        self.assertEqual(registry["ems-ops"].path.name, "SKILL.md")


if __name__ == "__main__":
    unittest.main()
