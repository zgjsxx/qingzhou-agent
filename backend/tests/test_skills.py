import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent import skills


class SkillsTest(unittest.TestCase):
    def tearDown(self):
        skills.scan_skills()

    def test_default_skills_dir_is_root_skills(self):
        self.assertEqual(
            skills._skills_dir(),
            Path(__file__).resolve().parents[2] / "skills",
        )

    def test_relative_configured_skills_dir_resolves_from_project_root(self):
        with patch.dict(os.environ, {"AGENT_SKILLS_DIR": "custom-skills"}):
            self.assertEqual(
                skills._skills_dir(),
                Path(__file__).resolve().parents[2] / "custom-skills",
            )

    def test_scan_skills_reads_configured_skills(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp_dir:
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


if __name__ == "__main__":
    unittest.main()
