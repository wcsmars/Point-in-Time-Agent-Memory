"""Offline SDK orchestration and real in-process MCP tests; never call an LLM."""
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pit_memory import claude_agent as agent
from pit_memory.backends import BackendIndex
from pit_memory.models import Fact
from pit_memory.store import DuckDBStore


def _facts():
    return [Fact("51143", "us-gaap:Revenues", "USD", value, "2019-01-01", "2019-12-31",
                 filed, accession, "10-K", available)
            for value, filed, available, accession in [
                (77.1, "2020-02-25", "2020-02-26", "original"),
                (57.7, "2022-02-22", "2022-02-23", "recast")]]


ARGS = {"cik": "51143", "concept": "us-gaap:Revenues", "period_end": "2019-12-31"}


def _retrieve(query):
    return BackendIndex(_facts()).retrieve(query, "bitemporal")


def test_host_cutoff_is_injected_and_cannot_be_advanced():
    result = agent._fixed_lookup(ARGS, _retrieve, "2021-03-15")
    assert result["fact"]["value"] == 77.1
    assert result["as_of"] == "2021-03-15"
    assert "as_of" not in ARGS
    assert agent._fixed_lookup({**ARGS, "as_of": "2023-01-01"}, _retrieve, "2021-03-15")["status"] == "error"
    assert agent._fixed_lookup([], _retrieve, "2021-03-15")["status"] == "error"


@dataclass
class Result:
    subtype: str = "success"
    is_error: bool = False
    session_id: str = "offline-test"
    num_turns: int = 2
    total_cost_usd: float = 0.01
    usage: dict | None = None
    result: str | None = "77.1 USD, original filing."


def _sdk(monkeypatch, *, calls=(ARGS,), result=Result(), failure=None):
    captured = SimpleNamespace(options=None, prompt=None, exited=False)

    def tool(name, description, schema):
        def decorate(handler):
            captured.handler = handler
            captured.schema = schema
            return handler
        return decorate

    class Client:
        def __init__(self, *, options):
            captured.options = options

        async def __aenter__(self):
            assert Path(captured.options.cwd).is_dir()
            return self

        async def __aexit__(self, *exc):
            captured.exited = True

        async def query(self, prompt):
            captured.prompt = prompt

        async def receive_response(self):
            if failure is not None:
                raise failure
            yield SimpleNamespace(content="intermediate text must not become final answer")
            for args in calls:
                await captured.handler(args)
            if result is not None:
                yield result

    sdk = SimpleNamespace(tool=tool, create_sdk_mcp_server=lambda **kw: kw,
                          ClaudeAgentOptions=lambda **kw: SimpleNamespace(**kw),
                          ClaudeSDKClient=Client, ResultMessage=Result, ClaudeSDKError=RuntimeError)
    monkeypatch.setattr(agent, "_load_sdk", lambda: sdk)
    return captured


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "facts.duckdb"
    with DuckDBStore(path) as store:
        store.append(_facts())
    return path


def test_sdk_owns_turns_and_receives_only_the_read_only_tool(monkeypatch, database):
    captured = _sdk(monkeypatch)
    before = database.read_bytes()
    result = asyncio.run(agent.run_agent(database, "Revenue?", as_of="2021-03-15"))
    assert result["status"] == "completed"
    assert result["answer"] == "77.1 USD, original filing."
    assert result["tool_calls"][0]["result"]["fact"]["value"] == 77.1
    assert result["total_cost_usd"] == 0.01
    assert database.read_bytes() == before
    options = captured.options
    assert options.tools == []
    assert options.allowed_tools == [agent.MCP_TOOL_NAME]
    assert set(options.mcp_servers) == {agent.SERVER_NAME}
    assert options.strict_mcp_config is True
    assert options.setting_sources == [] and options.skills == []
    assert options.permission_mode == "dontAsk"
    assert options.extra_args == {"no-session-persistence": None}
    assert options.max_turns == 6 and options.max_budget_usd == 0.50
    assert "as_of" not in captured.schema["properties"]
    assert "as_of" not in captured.schema["required"]
    assert captured.schema["additionalProperties"] is False
    assert "as_of" in agent.TOOL_SPEC["parameters"]["properties"]
    assert captured.exited and not Path(options.cwd).exists()


@pytest.mark.parametrize("calls,result,error", [
    ((), Result(), "No successful"),
    (({**ARGS, "as_of": "2023-01-01"},), Result(), "No successful"),
    ((ARGS,), Result(subtype="error_max_turns", is_error=True), "error_max_turns"),
    ((ARGS,), Result(subtype="error_max_budget_usd", is_error=True), "error_max_budget_usd"),
    ((ARGS,), Result(is_error=True), "success"),
    ((ARGS,), Result(result=""), "no final answer"),
    ((ARGS,), None, "without a terminal result"),
])
def test_no_unsupported_or_incomplete_answer_is_reported(monkeypatch, database, calls, result, error):
    _sdk(monkeypatch, calls=calls, result=result)
    response = asyncio.run(agent.run_agent(database, "Revenue?", as_of="2021-03-15"))
    assert response["status"] == "error"
    assert error in response["error"]
    assert "answer" not in response


@pytest.mark.parametrize("kwargs", [
    {"as_of": "2021-3-15"}, {"prompt": ""}, {"max_turns": True},
    {"max_turns": 0}, {"max_turns": 1.5}, {"max_budget_usd": 0},
    {"max_budget_usd": float("nan")}, {"max_budget_usd": float("inf")},
    {"max_budget_usd": True}, {"model": ""},
])
def test_invalid_host_config_fails_before_loading_sdk(monkeypatch, kwargs):
    def fail():
        pytest.fail("Invalid input must fail before loading the SDK")
    monkeypatch.setattr(agent, "_load_sdk", fail)
    with pytest.raises(ValueError):
        asyncio.run(agent.run_agent("missing", **{"prompt": "Revenue?", "as_of": "2021-03-15", **kwargs}))


def test_missing_optional_sdk_has_actionable_error(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(name)
    monkeypatch.setattr(agent.importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match=r"pip install.*agent"):
        agent._load_sdk()


def test_missing_database_does_not_start_sdk_or_create_file(monkeypatch, tmp_path):
    captured = _sdk(monkeypatch)
    path = tmp_path / "missing.duckdb"
    with pytest.raises((ValueError, FileNotFoundError)):
        asyncio.run(agent.run_agent(path, "Revenue?", as_of="2021-03-15"))
    assert not path.exists() and captured.options is None


def test_real_sdk_mcp_server_exposes_cutoff_schema_and_error_flag():
    sdk = pytest.importorskip("claude_agent_sdk")
    from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

    trace = []
    server = agent._make_server(sdk, _retrieve, "2021-03-15", trace)["instance"]

    async def check():
        listing = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
        schema = listing.root.tools[0].inputSchema
        assert "as_of" not in schema["properties"]
        assert schema["additionalProperties"] is False
        known = await server.request_handlers[CallToolRequest](CallToolRequest(
            method="tools/call", params=CallToolRequestParams(name=agent.TOOL_NAME, arguments=ARGS)))
        assert not known.root.isError
        assert json.loads(known.root.content[0].text)["fact"]["value"] == 77.1
        # Well-formed schema, invalid domain input: exercises our MCP error flag.
        bad = await server.request_handlers[CallToolRequest](CallToolRequest(
            method="tools/call", params=CallToolRequestParams(
                name=agent.TOOL_NAME, arguments={**ARGS, "cik": "0000000000"})))
        assert bad.root.isError
        assert json.loads(bad.root.content[0].text)["status"] == "error"

    asyncio.run(check())
    assert len(trace) == 2


def test_real_sdk_accepts_options_and_builds_constrained_cli_command(monkeypatch, database):
    sdk = pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    captured = _sdk(monkeypatch)
    asyncio.run(agent.run_agent(database, "Revenue?", as_of="2021-03-15"))
    fields = vars(captured.options).copy()
    # Use the real in-process server; our lightweight orchestration mock stores
    # decorated callables, which are intentionally not CLI-serializable.
    fields["mcp_servers"] = {agent.SERVER_NAME: agent._make_server(sdk, _retrieve, "2021-03-15", [])}
    options = sdk.ClaudeAgentOptions(**fields)
    transport = SubprocessCLITransport(prompt="unused", options=options)
    transport._cli_path = "sdk-runtime"  # Build arguments only, without spawning.
    command = transport._build_command()
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--allowedTools") + 1] == agent.MCP_TOOL_NAME
    assert "--strict-mcp-config" in command
    assert "--setting-sources=" in command
    assert "--no-session-persistence" in command
    assert "--continue" not in command and "--resume" not in command
    mcp_config = json.loads(command[command.index("--mcp-config") + 1])
    assert mcp_config == {"mcpServers": {agent.SERVER_NAME: {
        "type": "sdk", "name": agent.SERVER_NAME}}}


def test_real_bundled_cli_initializes_without_a_model_request(tmp_path):
    sdk = pytest.importorskip("claude_agent_sdk")

    async def check():
        options = sdk.ClaudeAgentOptions(
            tools=[], allowed_tools=[agent.MCP_TOOL_NAME],
            mcp_servers={agent.SERVER_NAME: agent._make_server(sdk, _retrieve, "2021-03-15", [])},
            strict_mcp_config=True, setting_sources=[], skills=[], permission_mode="dontAsk",
            cwd=str(tmp_path), max_turns=1, max_budget_usd=0.01,
            env={"CLAUDE_CONFIG_DIR": str(tmp_path / "config"), "ANTHROPIC_API_KEY": "",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            extra_args={"no-session-persistence": None},
        )
        async with asyncio.timeout(45):
            async with sdk.ClaudeSDKClient(options=options) as client:
                # Initialization exercises the real subprocess/control protocol.
                # Do not call client.query: this test makes zero model requests.
                status = await client.get_mcp_status()
                assert {server["name"] for server in status["mcpServers"]} <= {agent.SERVER_NAME}

    asyncio.run(check())


def test_provider_diagnostics_are_not_exposed_and_resources_close(monkeypatch, database):
    captured = _sdk(monkeypatch, failure=ExceptionGroup(
        "provider stderr contains credential-value", [RuntimeError("credential-value")]))
    with pytest.raises(RuntimeError, match="ExceptionGroup") as error:
        asyncio.run(agent.run_agent(database, "Revenue?", as_of="2021-03-15"))
    assert "credential-value" not in str(error.value)
    assert captured.exited and not Path(captured.options.cwd).exists()


def test_sdk_stderr_is_suppressed(monkeypatch, database, capsys):
    captured = _sdk(monkeypatch)
    asyncio.run(agent.run_agent(database, "Revenue?", as_of="2021-03-15"))
    assert callable(captured.options.stderr)
    captured.options.stderr("provider stderr with credential-value")
    output = capsys.readouterr()
    assert "credential-value" not in output.out + output.err


def test_retriever_error_is_audited_without_forwarding_exception_text(monkeypatch):
    captured = _sdk(monkeypatch)
    trace = []

    def broken(query):
        raise RuntimeError("database path and credential-value")

    agent._make_server(agent._load_sdk(), broken, "2021-03-15", trace)
    result = asyncio.run(captured.handler(ARGS))
    assert result["is_error"] is True
    assert json.loads(result["content"][0]["text"])["status"] == "error"
    assert trace[0]["result"]["error"] == "Point-in-time retrieval failed"
    assert "credential-value" not in json.dumps(result) + json.dumps(trace)
