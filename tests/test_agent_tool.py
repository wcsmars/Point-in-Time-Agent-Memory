"""The agent-facing tool: schema, validation, citations and abstentions."""
import json

from pit_memory.agent_tool import TOOL_SPEC, call_tool
from pit_memory.backends import BackendIndex
from pit_memory.models import Fact


def facts():
    original = Fact("51143", "us-gaap:Revenues", "USD", 77.1, "2019-01-01", "2019-12-31",
                    "2020-02-25", "a-2020", "10-K", "2020-02-26", source_url="https://www.sec.gov/x")
    recast = Fact("51143", "us-gaap:Revenues", "USD", 57.7, "2019-01-01", "2019-12-31",
                  "2022-02-22", "a-2022", "10-K", "2022-02-23")
    return [original, recast]


def retriever():
    index = BackendIndex(facts())
    return lambda query: index.retrieve(query, "bitemporal")


def test_schema_is_plain_json_with_required_fields():
    spec = json.loads(json.dumps(TOOL_SPEC))
    assert spec["parameters"]["required"] == ["cik", "concept", "as_of"]
    assert spec["parameters"]["additionalProperties"] is False


def test_known_answer_carries_citation_and_never_the_later_recast():
    result = call_tool({"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2021-03-15",
                        "period_end": "2019-12-31"}, retriever())
    assert result["status"] == "known"
    assert result["fact"]["value"] == 77.1
    assert result["fact"]["accession"] == "a-2020"
    assert result["fact"]["source_url"] == "https://www.sec.gov/x"
    later = call_tool({"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2022-03-01"}, retriever())
    assert later["fact"]["value"] == 57.7
    json.dumps(result)


def test_abstains_before_filing_is_available():
    result = call_tool({"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2020-02-25"}, retriever())
    assert result["status"] == "not_known"
    assert "fact" not in result


def test_bad_arguments_return_errors_not_exceptions():
    run = retriever()
    assert call_tool({"cik": "51143", "concept": "us-gaap:Revenues"}, run)["status"] == "error"
    assert call_tool({"cik": "51143", "concept": "x", "as_of": "2021-3-1"}, run)["status"] == "error"
    assert call_tool({"cik": "abc", "concept": "x", "as_of": "2021-03-01"}, run)["status"] == "error"
    assert "unknown" in call_tool({"cik": "1", "concept": "x", "as_of": "2021-03-01", "extra": 1}, run)["error"]
    assert call_tool(["not", "a", "dict"], run)["status"] == "error"
