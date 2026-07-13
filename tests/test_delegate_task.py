import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

from agent import subagent
from tools import registry


class DelegateTaskTest(unittest.TestCase):
    def test_delegate_task_requires_goal_or_tasks(self):
        result = subagent.delegate_task()

        self.assertIn("goal", result)
        self.assertIn("tasks", result)

    def test_delegate_task_single_returns_json_result(self):
        with patch(
            "agent.subagent._invoke_subagent_in_isolated_thread",
            return_value={"messages": [AIMessage(content="Summary:\nDone")]},
        ) as invoke:
            result = subagent.delegate_task(goal="Inspect context compaction", context="Respond in Chinese", cwd=".")

        payload = json.loads(result)
        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(payload["results"][0]["goal"], "Inspect context compaction")
        self.assertIn("Done", payload["results"][0]["summary"])
        system_prompt = invoke.call_args.kwargs["system_prompt"]
        self.assertIn("Inspect context compaction", system_prompt)
        self.assertIn("Respond in Chinese", system_prompt)
        self.assertIn("Readonly mode", system_prompt)

    def test_delegate_task_batch_preserves_task_order(self):
        def fake_invoke(_user_content, _cwd, _recursion_limit, **kwargs):
            prompt = kwargs["system_prompt"]
            if "Task B" in prompt:
                return {"messages": [AIMessage(content="B result")]}
            return {"messages": [AIMessage(content="A result")]}

        with patch.dict("os.environ", {"AGENT_DELEGATE_SPAWN_STAGGER_SECONDS": "0"}, clear=False):
            with patch("agent.subagent._invoke_subagent_in_isolated_thread", side_effect=fake_invoke):
                result = subagent.delegate_task(
                    tasks=[
                        {"goal": "Task A", "context": "A context"},
                        {"goal": "Task B", "context": "B context"},
                    ]
                )

        payload = json.loads(result)
        self.assertEqual([item["goal"] for item in payload["results"]], ["Task A", "Task B"])
        self.assertEqual([item["summary"] for item in payload["results"]], ["A result", "B result"])

    def test_delegate_task_batches_more_tasks_than_workers(self):
        with patch.dict(
            "os.environ",
            {"AGENT_DELEGATE_MAX_WORKERS": "2", "AGENT_DELEGATE_SPAWN_STAGGER_SECONDS": "0"},
            clear=False,
        ):
            with patch(
                "agent.subagent._invoke_subagent_in_isolated_thread",
                return_value={"messages": [AIMessage(content="ok")]},
            ):
                result = subagent.delegate_task(
                    tasks=[
                        {"goal": "Task A"},
                        {"goal": "Task B"},
                        {"goal": "Task C"},
                    ]
                )

        payload = json.loads(result)
        self.assertEqual([item["goal"] for item in payload["results"]], ["Task A", "Task B", "Task C"])
        self.assertEqual([item["status"] for item in payload["results"]], ["ok", "ok", "ok"])

    def test_rate_limit_retry_retries_transient_throttling(self):
        calls = {"count": 0}

        def flaky_call():
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("RateLimitError: Error code: 429 - Throttling")
            return "ok"

        with patch.dict(
            "os.environ",
            {
                "AGENT_SUBAGENT_RATE_LIMIT_RETRIES": "3",
                "AGENT_SUBAGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS": "0.01",
            },
            clear=False,
        ):
            with patch("agent.subagent.time.sleep") as sleep:
                result = subagent._run_with_rate_limit_retry(flaky_call)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_rate_limit_retry_does_not_retry_non_rate_limit_errors(self):
        calls = {"count": 0}

        def broken_call():
            calls["count"] += 1
            raise RuntimeError("plain failure")

        with self.assertRaises(RuntimeError):
            subagent._run_with_rate_limit_retry(broken_call)

        self.assertEqual(calls["count"], 1)

    def test_delegate_task_respects_max_tasks(self):
        with patch.dict("os.environ", {"AGENT_DELEGATE_MAX_TASKS": "2"}, clear=False):
            result = subagent.delegate_task(
                tasks=[
                    {"goal": "Task A", "context": "A context"},
                    {"goal": "Task B", "context": "B context"},
                    {"goal": "Task C", "context": "C context"},
                ]
            )

        self.assertIn("3", result)
        self.assertIn("2", result)

    def test_delegate_task_mode_controls_tools(self):
        readonly_names = {tool.name for tool in subagent._subagent_tools_for_mode("readonly")}
        write_names = {tool.name for tool in subagent._subagent_tools_for_mode("workspace_write")}

        self.assertIn("search_files", readonly_names)
        self.assertNotIn("edit_file", readonly_names)
        self.assertIn("edit_file", write_names)
        self.assertNotIn("delegate_task", write_names)

    def test_delegate_task_tool_is_registered(self):
        tool_names = {tool.name for tool in registry.ALL_TOOLS}

        self.assertIn("delegate_task", tool_names)
        self.assertIn("run_subagent", tool_names)

    def test_subagent_model_uses_anthropic_auth_token_headers(self):
        with patch.dict(
            "os.environ",
            {
                "LLM_ADAPTER_TYPE": "anthropic",
                "LLM_MODEL": "glm-5.1",
                "LLM_AUTH_TOKEN": "auth-token",
                "LLM_BASE_URL": "https://example.test/anthropic",
            },
            clear=False,
        ):
            with patch("agent.subagent.init_chat_model", return_value="model-object") as init_model:
                model = subagent._llm_model()

        self.assertEqual(model, "model-object")
        self.assertEqual(init_model.call_args.args[0], "anthropic:glm-5.1")
        self.assertEqual(
            init_model.call_args.kwargs["default_headers"],
            {"Authorization": "Bearer auth-token"},
        )
        self.assertEqual(init_model.call_args.kwargs["base_url"], "https://example.test/anthropic")

    def test_subagent_recursion_limit_falls_back_to_no_tool_summary(self):
        class FakeGraph:
            def stream(self, _input_state, config, stream_mode):
                self.config = config
                self.stream_mode = stream_mode
                yield {"messages": [AIMessage(content="Found enough evidence from files.")]}
                raise GraphRecursionError("Recursion limit of 62 reached without hitting a stop condition.")

        class FakeModel:
            def invoke(self, messages):
                self.messages = messages
                return AIMessage(content="Summary:\nFallback answer from existing evidence.")

        fake_graph = FakeGraph()
        fake_model = FakeModel()

        with patch("agent.subagent.create_agent", return_value=fake_graph):
            with patch("agent.subagent._direct_llm_model", return_value=fake_model):
                result = subagent._invoke_subagent_in_isolated_thread(
                    "Start.",
                    ".",
                    62,
                    tools=[],
                    system_prompt="system",
                )

        self.assertEqual(result["_subagent_status"], "partial")
        self.assertIn("GRAPH_RECURSION_LIMIT", result["_subagent_warning"])
        self.assertIn("Fallback answer", subagent._extract_final_text(result))
        self.assertIn("Found enough evidence", fake_model.messages[1]["content"])
        self.assertEqual(fake_graph.stream_mode, "values")


if __name__ == "__main__":
    unittest.main()
