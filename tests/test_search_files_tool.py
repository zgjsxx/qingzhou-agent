import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import registry
from tools.registry import _search_files_impl


class SearchFilesToolTest(unittest.TestCase):
    FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "search_files"

    def test_search_files_content_mode_returns_context(self):
        result = _search_files_impl("needle", cwd=str(self.FIXTURE_ROOT), include="**/*.py", context_lines=1)

        self.assertIn("pkg/a.py:2", result)
        self.assertIn("1: alpha", result)
        self.assertIn("2: needle here", result)
        self.assertIn("3: omega", result)
        self.assertNotIn("node_modules", result)

    def test_search_files_files_with_matches_mode(self):
        result = _search_files_impl("needle", cwd=str(self.FIXTURE_ROOT), include="**/*.py,**/*.txt", mode="files_with_matches")

        self.assertIn("pkg/a.py", result)
        self.assertIn("pkg/b.txt", result)
        self.assertNotIn("pkg/skip.log", result)

    def test_search_files_count_mode_reports_totals(self):
        result = _search_files_impl("needle", cwd=str(self.FIXTURE_ROOT), include="**/*", exclude="**/*.log", mode="count")

        self.assertIn("Files with matches: 3", result)
        self.assertIn("Total matches: 3", result)

    def test_search_files_default_include_matches_root_files(self):
        result = _search_files_impl("needle at root", cwd=str(self.FIXTURE_ROOT), mode="files_with_matches")

        self.assertIn("root.txt", result)

    def test_search_files_regex_and_case_sensitive(self):
        result = _search_files_impl(r"Needle\s+again", cwd=str(self.FIXTURE_ROOT), include="**/*.txt", regex=True, case_sensitive=True)

        self.assertIn("pkg/b.txt:2", result)

    def test_search_files_rejects_invalid_mode(self):
        result = _search_files_impl("needle", cwd=str(self.FIXTURE_ROOT), mode="summary")

        self.assertIn("mode must be one of", result)

    def test_search_files_tool_is_registered(self):
        tool_names = {tool.name for tool in registry.ALL_TOOLS}

        self.assertIn("search_files", tool_names)


if __name__ == "__main__":
    unittest.main()
