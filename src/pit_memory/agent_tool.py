"""Point-in-time retrieval packaged as a tool for an LLM research agent.

``TOOL_SPEC`` is a JSON Schema function definition in the shape most
function-calling APIs accept (name, description, parameters). ``call_tool``
validates the arguments the model produced, runs point-in-time retrieval and
returns a JSON-serialisable result that always carries a citation or an
explicit abstention, so the agent can quote its source or say it does not know.

The agent sets ``as_of`` to its simulated knowledge date (for example the
rebalance date in a backtest). The tool never returns a fact that was not yet
available on that date, and it never fills a missing year with a later one.
"""
from __future__ import annotations

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
    unknown = sorted(set(arguments) - _ALLOWED)
    if unknown:
        return {"status": "error", "error": f"unknown argument(s): {', '.join(unknown)}"}
    missing = [key for key in TOOL_SPEC["parameters"]["required"] if not arguments.get(key)]
    if missing:
        return {"status": "error", "error": f"missing argument(s): {', '.join(missing)}"}
    try:
        query = Query(f"tool:{arguments['cik']}:{arguments['as_of']}", str(arguments["cik"]),
                      str(arguments["concept"]), str(arguments.get("unit") or "USD"),
                      str(arguments["as_of"]), arguments.get("period_end") or None)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    result = retrieve(query)
    response: dict[str, Any] = {"status": result.status, "as_of": query.as_of,
                                "requested_period_end": query.period_end}
    if result.status == "known" and result.fact is not None:
        response["fact"] = _citation(result.fact)
    elif result.status == "ambiguous":
        response["candidates"] = [_citation(fact) for fact in result.context]
        response["message"] = result.reason
    else:
        response["message"] = "No such fact had been filed and become available by as_of."
    return response
