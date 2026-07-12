import sys
import unittest
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_core.messages import AIMessage, HumanMessage

from agent.context_references import (
    AgentContextReferenceMiddleware,
    context_reference_update,
    parse_context_references,
    preprocess_context_references,
)


class AgentContextReferencesTest(unittest.TestCase):
    FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "context_refs"

    def test_parse_file_line_range(self):
        refs = parse_context_references("看看 @file:agent/context.py:10-80")

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].kind, "file")
        self.assertEqual(refs[0].target, "agent/context.py")
        self.assertEqual(refs[0].start, 10)
        self.assertEqual(refs[0].end, 80)

    def test_parse_quoted_file_line_range(self):
        refs = parse_context_references('看看 @file:"docs/example file.md":3-5')

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].target, "docs/example file.md")
        self.assertEqual(refs[0].start, 3)
        self.assertEqual(refs[0].end, 5)

    def test_preprocess_file_reference_attaches_file_content(self):
        result = preprocess_context_references(
            "解释 @file:src/example.py",
            cwd=self.FIXTURE_ROOT,
            allowed_root=self.FIXTURE_ROOT,
        )

        self.assertIn("解释", result.message)
        self.assertNotIn("@file:src/example.py", result.message)
        self.assertIn("--- Attached Context ---", result.message)
        self.assertIn("[file: src/example.py]", result.message)
        self.assertIn("def hello():", result.message)
        self.assertEqual(result.warnings, ())

    def test_preprocess_preserves_trailing_punctuation(self):
        result = preprocess_context_references(
            "请解释（@file:a.txt）。",
            cwd=self.FIXTURE_ROOT,
            allowed_root=self.FIXTURE_ROOT,
        )

        self.assertIn("请解释（）。", result.message)
        self.assertIn("alpha", result.message)

    def test_preprocess_file_reference_applies_line_range(self):
        result = preprocess_context_references(
            "总结 @file:notes.txt:2-3",
            cwd=self.FIXTURE_ROOT,
            allowed_root=self.FIXTURE_ROOT,
        )

        self.assertIn("[file: notes.txt:2-3]", result.message)
        self.assertNotIn("one", result.message)
        self.assertIn("two", result.message)
        self.assertIn("three", result.message)
        self.assertNotIn("four", result.message)

    def test_preprocess_folder_reference_lists_files(self):
        result = preprocess_context_references(
            "看目录 @folder:pkg",
            cwd=self.FIXTURE_ROOT,
            allowed_root=self.FIXTURE_ROOT,
        )

        self.assertIn("[folder: pkg]", result.message)
        self.assertIn("- a.py", result.message)
        self.assertIn("- b.txt", result.message)
        self.assertNotIn("print('a')", result.message)

    def test_preprocess_blocks_paths_outside_workspace(self):
        outside = self.FIXTURE_ROOT.parent / "outside-context-reference-test.txt"
        result = preprocess_context_references(
            f"读 @file:{outside}",
            cwd=self.FIXTURE_ROOT,
            allowed_root=self.FIXTURE_ROOT,
        )

        self.assertIn("Context Reference Warnings", result.message)
        self.assertIn("outside the workspace", result.message)

    def test_context_reference_update_replaces_last_human_message(self):
        state = {"messages": [AIMessage(content="ready"), HumanMessage(content="读 @file:tests/fixtures/context_refs/a.txt", id="m1")]}
        original_cwd = Path.cwd()
        try:
            os.chdir(Path(__file__).resolve().parents[1])
            update = context_reference_update(state)
        finally:
            os.chdir(original_cwd)

        self.assertIsNotNone(update)
        self.assertEqual(len(update["messages"]), 1)
        self.assertEqual(update["messages"][0].id, "m1")
        self.assertIn("alpha", update["messages"][0].content)

    def test_middleware_ignores_non_human_tail(self):
        middleware = AgentContextReferenceMiddleware()
        update = middleware.before_model({"messages": [AIMessage(content="@file:a.txt")]}, runtime=None)

        self.assertIsNone(update)


if __name__ == "__main__":
    unittest.main()
