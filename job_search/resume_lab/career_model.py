"""Bounded model contracts for draft extraction and career-bullet wording."""
from __future__ import annotations

from typing import Any, Mapping

from .career_composition import validate_rewrites
from .contracts import ResumeBoundaryError


def rewrite_selected(model: Any, snapshot: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, str]:
    facts = [{"fact_id": r["fact_id"], "text": r["text"], "entry_id": r["entry_id"]}
             for r in snapshot["selected"] if r["kind"] == "bullet"]
    if not facts:
        return {}
    invoke = getattr(model, "_invoke", None)
    if not callable(invoke):
        raise ResumeBoundaryError("career rewriting is not supported by this model adapter")
    output = invoke("rewrite_career_facts", {"job": dict(job), "facts": facts},
        {"rewrites": [{"fact_id": "exact input fact_id", "text": "concise factual wording; keep original if uncertain"}]})
    if not isinstance(output, Mapping) or set(output) != {"rewrites"} or not isinstance(output["rewrites"], list):
        raise ResumeBoundaryError("career model output is invalid")
    rewrites = {}
    for row in output["rewrites"]:
        if not isinstance(row, Mapping) or set(row) != {"fact_id", "text"} or not isinstance(row["fact_id"], str) or row["fact_id"] in rewrites:
            raise ResumeBoundaryError("career model fact references are invalid")
        rewrites[row["fact_id"]] = row["text"]
    if set(rewrites) != {r["fact_id"] for r in facts}:
        raise ResumeBoundaryError("career model omitted source references")
    checked = validate_rewrites(snapshot, rewrites)
    changed = [{**fact, "rewrite": checked[fact["fact_id"]]} for fact in facts if checked[fact["fact_id"]] != fact["text"]]
    if changed:
        support = invoke("assess_career_rewrites", {"facts": changed},
            {"assessments": [{"fact_id": "exact input fact_id", "supported": "boolean: all output claims and their associations follow from this single source"}]})
        if not isinstance(support, Mapping) or set(support) != {"assessments"} or not isinstance(support["assessments"], list):
            raise ResumeBoundaryError("career wording support assessment is invalid")
        confirmed = set()
        for row in support["assessments"]:
            if not isinstance(row, Mapping) or set(row) != {"fact_id", "supported"} or not isinstance(row["fact_id"], str) or row["fact_id"] in confirmed or row["supported"] is not True:
                raise ResumeBoundaryError("career wording is not supported by its source")
            confirmed.add(row["fact_id"])
        if confirmed != {r["fact_id"] for r in changed}:
            raise ResumeBoundaryError("career wording support coverage is incomplete")
    return checked
