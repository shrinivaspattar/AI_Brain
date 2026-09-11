from app.tools.registry import Tool, ToolRegistry


def _tool(name: str = "echo") -> Tool:
    return Tool(
        name=name,
        description="Echoes its input.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=lambda text: text,
    )


def test_register_and_get() -> None:
    registry = ToolRegistry()
    tool = _tool()

    registry.register(tool)

    assert registry.get("echo") is tool
    assert registry.get("missing") is None


def test_list_tools_returns_all_registered() -> None:
    registry = ToolRegistry()
    registry.register(_tool("a"))
    registry.register(_tool("b"))

    names = {tool.name for tool in registry.list_tools()}

    assert names == {"a", "b"}


def test_to_ollama_schema_shape() -> None:
    registry = ToolRegistry()
    registry.register(_tool())

    schema = registry.to_ollama_schema()

    assert schema == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echoes its input.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]


def test_call_invokes_handler_with_arguments() -> None:
    registry = ToolRegistry()
    registry.register(_tool())

    result = registry.call("echo", {"text": "hello"})

    assert result.content == "hello"
    assert result.is_error is False
    assert result.error is None
    assert result.duration_ms >= 0


def test_call_returns_error_result_for_unknown_tool() -> None:
    registry = ToolRegistry()

    result = registry.call("does_not_exist", {})

    assert result.is_error is True
    assert "unknown tool" in result.content
    assert "does_not_exist" in result.content
    assert "unknown tool" in result.error


def test_call_returns_error_result_when_handler_raises() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="broken",
            description="Always fails.",
            parameters={"type": "object", "properties": {}},
            handler=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    )

    result = registry.call("broken", {})

    assert result.is_error is True
    assert "Error running tool 'broken'" in result.content
    assert result.error == "boom"
    assert result.duration_ms >= 0
