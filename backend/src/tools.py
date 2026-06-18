"""Tools for the agent."""

import random
from langchain.tools import tool


@tool
def get_weather(location: str) -> str:
    """Get the current weather for a given location.

    Args:
        location: The city name to get weather for, e.g. "Beijing", "Shanghai"
    """
    # Mock weather data — replace with a real API call
    conditions = ["晴天", "多云", "小雨", "大雨", "阴天"]
    temps = range(-5, 40)
    return f"{location} 当前天气：{random.choice(conditions)}，气温 {random.choice(temps)}°C"


@tool
def calculate(expression: str) -> str:
    """Evaluate a math expression and return the result.

    Args:
        expression: A math expression, e.g. "2 + 3 * 4"
    """
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        return f"{expression} = {result}"
    except Exception as e:
        return f"计算错误: {e}"


@tool
def search_knowledge(query: str) -> str:
    """Search internal knowledge base for information.

    Args:
        query: The search query string
    """
    # Mock knowledge search — replace with real RAG or search API
    mock_answers = {
        "python": "Python 是一种广泛使用的高级编程语言，以简洁易读著称。",
        "langchain": "LangChain 是一个用于构建 LLM 应用的框架，支持链式调用、Agent 和工具集成。",
        "langgraph": "LangGraph 是 LangChain 的扩展，用于构建有状态的、多角色的 AI 应用，支持循环和分支控制流。",
    }
    for key, value in mock_answers.items():
        if key in query.lower():
            return value
    return f"未找到与 '{query}' 相关的信息。"


ALL_TOOLS = [get_weather, calculate, search_knowledge]
