"""The agent-facing tool: schema, validation, citations and abstentions."""
import json
from dataclasses import replace

import pytest

from pit_memory.agent_tool import TOOL_SPEC, call_tool
from pit_memory.backends import BackendIndex
from pit_memory.models import Fact, Retrieval


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


@pytest.mark.parametrize("field,value", [
    ("cik", 51143), ("cik", "00000051143"), ("concept", 123),
    ("unit", []), ("unit", ""), ("unit", None),
    ("period_end", False), ("period_end", ""), ("period_end", None),
    ("as_of", None),
])
def test_schema_invalid_fields_do_not_change_query_meaning(field, value):
    arguments = {"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2021-03-15"}
    arguments[field] = value
    def must_not_retrieve(query):
        pytest.fail("Invalid arguments must be rejected before retrieval")
    assert call_tool(arguments, must_not_retrieve)["status"] == "error"


def test_non_string_argument_keys_return_an_error():
    assert call_tool({0: "bad", None: "bad"}, retriever())["status"] == "error"


@pytest.mark.parametrize("change", [
    {"cik": "123"}, {"unit": "EUR"}, {"concept": "us-gaap:NetIncomeLoss"},
    {"filed": "2022-02-22", "available_on": "2022-02-23"},
    {"period_start": "2018-01-01", "period_end": "2018-12-31"},
])
def test_tool_rejects_retriever_evidence_outside_scope_or_cutoff(change):
    wrong = replace(facts()[0], **change, fact_id="")
    arguments = {"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2021-03-15",
                 "period_end": "2019-12-31"}
    for status in ("known", "ambiguous"):
        result = call_tool(arguments, lambda q: Retrieval(
            q.query_id, "broken", wrong if status == "known" else None, (wrong,), status))
        assert result["status"] == "error"
        assert "fact" not in result and "candidates" not in result


def test_ambiguous_candidates_exclude_older_context():
    original, recast = facts()
    clash = replace(recast, value=58.0, accession="conflict", fact_id="")
    index = BackendIndex([original, recast, clash])
    result = call_tool({"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2022-03-01"},
                       lambda q: index.retrieve(q, "bitemporal"))
    assert result["status"] == "ambiguous"
    assert {candidate["accession"] for candidate in result["candidates"]} == {"a-2022", "conflict"}


@pytest.mark.parametrize("status,has_fact", [("known", False), ("not_known", True), ("invalid", False)])
def test_inconsistent_retriever_status_is_rejected(status, has_fact):
    original = facts()[0]
    result = call_tool({"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2021-03-15"},
                       lambda q: Retrieval(q.query_id, "broken", original if has_fact else None, (), status))
    assert result["status"] == "error"




# ---- proposed: agent tool must fail closed on ambiguous/inconsistent retriever output
def test_ambiguous_candidates_filed_after_as_of_are_rejected():
    original, recast = facts()
    clash = replace(recast, value=58.0, accession="conflict", fact_id="")
    # Both conflicting rows were filed in 2022; the agent asks about 2021.
    result = call_tool({"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2021-03-15"},
                       lambda q: Retrieval(q.query_id, "broken", None, (recast, clash), "ambiguous"))
    assert result["status"] == "error"
    assert "candidates" not in result


@pytest.mark.parametrize("make", [
    lambda q, f: Retrieval(q.query_id, "broken", None, (f,), "ambiguous"),        # no conflict
    lambda q, f: Retrieval("another-query", "broken", f, (f,), "known"),           # wrong query
    lambda q, f: Retrieval(q.query_id, "broken", None, (f,), "not_known"),         # abstains with evidence
    lambda q, f: Retrieval(q.query_id, "broken", None, ({"value": 1.0},), "ambiguous"),  # not a Fact
])
def test_inconsistent_retriever_output_is_an_error_not_an_answer(make):
    original = facts()[0]
    result = call_tool({"cik": "51143", "concept": "us-gaap:Revenues", "as_of": "2021-03-15"},
                       lambda q: make(q, original))
    assert result["status"] == "error"


def test_tool_definition_in_each_function_calling_layout():
    import json as _json
    from pit_memory.agent_tool import TOOL_SPEC, flat_function_tool, function_tool, input_schema_tool
    nested, flat, schema = function_tool(), flat_function_tool(), input_schema_tool()
    assert nested == {"type": "function", "function": TOOL_SPEC}
    assert flat == {"type": "function", **TOOL_SPEC}
    assert schema == {"name": TOOL_SPEC["name"], "description": TOOL_SPEC["description"],
                      "input_schema": TOOL_SPEC["parameters"]}
    for layout in (nested, flat, schema):
        assert _json.loads(_json.dumps(layout)) == layout
    schema["input_schema"]["required"].append("unit")  # copies: the shared spec is unchanged
    assert TOOL_SPEC["parameters"]["required"] == ["cik", "concept", "as_of"]
