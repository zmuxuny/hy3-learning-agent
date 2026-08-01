"""Small, deterministic evidence file used by the recording walkthrough.

It demonstrates the runtime/tool boundary without containing a model key or
pretending to be the Learning Agent implementation itself.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    execute: Callable[[list[str]], str]


def add(arguments: list[str]) -> str:
    if len(arguments) != 2:
        raise ValueError("add expects exactly two integers")
    return str(int(arguments[0]) + int(arguments[1]))


TOOLS = {
    "add": Tool(
        name="add",
        description="Add two integers.",
        execute=add,
    )
}


def run_agent(command: str) -> str:
    """Parse one tool request, execute it, and return an observed result."""
    tool_name, *arguments = command.split(":")
    tool = TOOLS.get(tool_name)
    if tool is None:
        return f"unknown tool: {tool_name}"
    observation = tool.execute(arguments)
    return f"tool={tool.name}; observation={observation}"


if __name__ == "__main__":
    result = run_agent("add:20:22")
    assert result == "tool=add; observation=42"
    print(f"minimal agent loop passed: {result}")
