"""Point-in-time retrieval packaged as a tool for an LLM research agent.

``TOOL_SPEC`` is a JSON Schema function definition (name, description,
parameters). Function-calling APIs wrap it differently: ``input_schema_tool()``
returns it with the schema under ``input_schema``, ``function_tool()`` nests it
under ``{"type": "function", "function": ...}``, and ``flat_function_tool()``
adds ``"type": "function"`` beside the name. ``call_tool``
validates the arguments the model produced, runs point-in-time retrieval and
returns a JSON-serialisable result that always carries a citation or an
explicit abstention, so the agent can quote its source or say it does not know.

The agent sets ``as_of`` to its simulated knowledge date (for example the
rebalance date in a backtest). The tool never returns a fact that was not yet
available on that date, and it never fills a missing year with a later one.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from .models import Fact, Query, Retrieval

TOOL_NAME = "point_in_time_fact"

TOOL_SPEC: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Look up one annual financial fact from SEC filings exactly as it was publicly "
        "available on a given date. Returns the value with its filing citation, or "
        "'not_known' if nothing had been filed by that date, or 'ambiguous' with the "
        "conflicting candidates. Never use a value from a later date in a backtest."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cik": {"type": "string", "pattern": "^[0-9]{1,10}$",
                    "description": "SEC Central Index Key of the company, digits only, e.g. '320193'."},
            "concept": {"type": "string",
                        "description": "Namespace-qualified XBRL concept, e.g. 'us-gaap:Revenues'."},
            "unit": {"type": "string", "default": "USD", "description": "Reporting unit, e.g. 'USD'."},
            "as_of": {"type": "string", "format": "date",
                      "description": "Knowledge cutoff YYYY-MM-DD. Only facts available by this date are used."},
            "period_end": {"type": "string", "format": "date",
                           "description": "Optional fiscal year end YYYY-MM-DD; omit for the latest annual period."},
        },
        "required": ["cik", "concept", "as_of"],
        "additionalProperties": False,
    },
}

_ALLOWED = set(TOOL_SPEC["parameters"]["properties"])


def input_schema_tool() -> dict[str, Any]:
    """The tool for APIs that expect ``name``, ``description`` and ``input_schema``."""
    return {"name": TOOL_NAME, "description": TOOL_SPEC["description"],
            "input_schema": copy.deepcopy(TOOL_SPEC["parameters"])}


def function_tool() -> dict[str, Any]:
    """The tool for APIs that expect ``{"type": "function", "function": {...}}``."""
    return {"type": "function", "function": copy.deepcopy(TOOL_SPEC)}


def flat_function_tool() -> dict[str, Any]:
    """The tool for APIs that expect ``type``, ``name``, ``description`` and ``parameters`` together."""
    return {"type": "function", **copy.deepcopy(TOOL_SPEC)}


def _citation(fact: Fact) -> dict[str, Any]:
    return {
        "value": fact.value, "unit": fact.unit, "concept": fact.concept, "cik": fact.cik,
        "fiscal_period": {"start": fact.period_start, "end": fact.period_end},
        "form": fact.form, "accession": fact.accession, "filed": fact.filed,
        "available_on": fact.available_on, "source_url": fact.source_url,
    }


def call_tool(arguments: Mapping[str, Any], retrieve: Callable[[Query], Retrieval]) -> dict[str, Any]:
    """Run the tool on model-produced arguments.

    ``retrieve`` is any point-in-time retriever, for example
    ``DuckDBStore(...).retrieve``. Invalid arguments produce
    ``{"status": "error", ...}`` instead of an exception, so the agent can
    correct its call.
    """
    if not isinstance(arguments, Mapping):
        return {"status": "error", "error": "arguments must be a JSON object"}
    if any(not isinstance(key, str) for key in arguments):
        return {"status": "error", "error": "argument names must be strings"}
    unknown = sorted(set(arguments) - _ALLOWED)
    if unknown:
        return {"status": "error", "error": f"unknown argument(s): {', '.join(unknown)}"}
    missing = [key for key in TOOL_SPEC["parameters"]["required"] if key not in arguments]
    if missing:
        return {"status": "error", "error": f"missing argument(s): {', '.join(missing)}"}
    # Match the published JSON Schema. Coercion would silently turn an invalid
    # period into a latest-year query, or an invalid unit into USD.
    for key, value in arguments.items():
        if not isinstance(value, str) or not value.strip():
            return {"status": "error", "error": f"{key} must be a nonempty string"}
    try:
        query = Query(f"tool:{arguments['cik']}:{arguments['as_of']}", arguments["cik"],
                      arguments["concept"], arguments.get("unit", "USD"),
                      arguments["as_of"], arguments.get("period_end"))
    except (TypeError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    result = retrieve(query)
    if (not isinstance(result, Retrieval) or result.query_id != query.query_id
            or not isinstance(result.status, str)
            or result.status not in {"known", "not_known", "ambiguous"}
            or not isinstance(result.context, (tuple, list))
            or not isinstance(result.reason, str)
            or (result.status == "known") != (result.fact is not None)
            or (result.status == "not_known" and result.context)):
        return {"status": "error", "error": "retriever returned an inconsistent result"}
    evidence = (*result.context, *((result.fact,) if result.fact is not None else ()))
    if any(not isinstance(fact, Fact) or fact.cik != query.cik
           or fact.concept != query.concept or fact.unit != query.unit
           or fact.available_on > query.as_of or fact.period_end > query.as_of
           or (query.period_end is not None and fact.period_end != query.period_end)
           for fact in evidence):
        return {"status": "error", "error": "retriever returned evidence outside the requested scope or cutoff"}
    response: dict[str, Any] = {"status": result.status, "as_of": query.as_of,
                                "requested_period_end": query.period_end}
    if result.status == "known" and result.fact is not None:
        # An in-scope answer can still be wrong: a retriever might select an
        # older filing or overlook a conflict in its own context. Fail closed
        # rather than turn that inconsistent evidence into an agent citation.
        newest = max(((fact.period_end, fact.filed) for fact in result.context), default=None)
        candidates = [fact for fact in result.context if (fact.period_end, fact.filed) == newest]
        if (result.fact.fact_id not in {fact.fact_id for fact in candidates}
                or len({(fact.period_start, fact.value) for fact in candidates}) != 1):
            return {"status": "error", "error": "retriever selected an answer inconsistent with its evidence"}
        response["fact"] = _citation(result.fact)
    elif result.status == "ambiguous":
        # The in-memory backend can include older eligible context. Only the
        # newest period/filing group actually causes the ambiguous answer.
        newest = max(((fact.period_end, fact.filed) for fact in result.context), default=None)
        candidates = [fact for fact in result.context if (fact.period_end, fact.filed) == newest]
        if len({(fact.period_start, fact.value) for fact in candidates}) < 2:
            return {"status": "error", "error": "retriever returned ambiguity without conflicting candidates"}
        response["candidates"] = [_citation(fact) for fact in candidates]
        response["message"] = result.reason
    else:
        response["message"] = "No such fact had been filed and become available by as_of."
    return response
