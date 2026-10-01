"""Job-specific selection and source-checked wording from an approved career bank."""
from __future__ import annotations

import copy
import re
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from .contracts import (ArtifactInput, ClaimOrigin, JobSnapshot, ResumeBoundaryError,
    ResumeClaim, ResumeLabError, ResumePurpose, canonical_json, content_sha256)
from .career_ops import normalize_ats_tokens
from .career_runs import CAREER_GROUNDING_REVISION
from .requirements import extract_requirement_graph
from .tex import render_resume_tex


class CareerPageOverflow(ResumeLabError):
    """Pinned/required content cannot fit the one-page template."""


def _tokens(text: str) -> set[str]:
    return set(normalize_ats_tokens(text))


def _date_key(entry: Mapping[str, Any]) -> tuple[int, str]:
    dates = str(entry.get("dates") or "")
    years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", dates)]
    return (9999 if re.search(r"\b(present|current|now)\b", dates, re.I) else max(years, default=0), dates)


def source_rows(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    fields = {"experience": ("company", "role", "dates", "location"),
              "projects": ("name", "context", "dates", "url"),
              "education": ("institution", "degree", "dates", "location", "details"),
              "skills": ("category",)}
    for section, keys in fields.items():
        for index, entry in enumerate(content.get(section) or []):
            if entry.get("retired"):
                continue
            eid = str(entry["entry_id"])
            header = " · ".join(str(entry.get(k) or "") for k in keys if entry.get(k))
            rows.append({"fact_id": eid, "entry_id": eid, "section": section,
                "text": header, "kind": "entry", "index": index})
            for j, fact in enumerate(entry.get("items" if section == "skills" else "bullets") or []):
                if fact.get("retired"):
                    continue
                rows.append({"fact_id": str(fact["fact_id"]), "entry_id": eid,
                    "section": section, "text": str(fact["text"]), "kind": "skill" if section == "skills" else "bullet",
                    "index": index, "item_index": j})
    return rows


def select_composition(revision: Mapping[str, Any], job: JobSnapshot, *,
                       pinned_fact_ids: Sequence[str] = (), excluded_fact_ids: Sequence[str] = (),
                       template_version: str = "jake-v1") -> dict[str, Any]:
    content = copy.deepcopy(revision["content"])
    rows = source_rows(content)
    all_ids = {r["fact_id"] for r in rows}
    for values in (pinned_fact_ids, excluded_fact_ids):
        if not isinstance(values, (list, tuple)) or len(values) > 2000 or any(not isinstance(v, str) for v in values) or len(values) != len(set(values)):
            raise ResumeLabError("career selections must be bounded unique fact IDs")
        if not set(values) <= all_ids:
            raise ResumeBoundaryError("career selection contains unavailable facts")
    pins, exclusions = set(pinned_fact_ids), set(excluded_fact_ids)
    pinned_entries = {r["entry_id"] for r in rows if r["kind"] == "entry" and r["fact_id"] in pins}
    pins |= {r["fact_id"] for r in rows if r["entry_id"] in pinned_entries}
    excluded_entries = {r["entry_id"] for r in rows if r["kind"] == "entry" and r["fact_id"] in exclusions}
    exclusions |= {r["fact_id"] for r in rows if r["entry_id"] in excluded_entries}
    if pins & exclusions:
        raise ResumeLabError("the same career fact cannot be pinned and excluded")
    entries_with_facts = {r["entry_id"] for r in rows if r["kind"] != "entry" and r["fact_id"] not in exclusions}
    if any(r["section"] == "skills" and r["kind"] == "entry" and r["fact_id"] in pins
           and r["entry_id"] not in entries_with_facts for r in rows):
        raise ResumeLabError("add at least one individual skill before pinning a skill category")
    graph = extract_requirement_graph(job)
    job_terms = _tokens(job.title + " " + job.description)
    for row in rows:
        terms = _tokens(row["text"])
        matched = sorted(terms & job_terms)
        # The existing requirement graph supplies reviewed alias groups. Multiword
        # requirements are scored as a group rather than keyword repetition.
        requirement_matches = []
        score = len(matched) / max(1, len(terms))
        for req in graph.requirements:
            groups = getattr(req, "term_groups", ())
            if groups and all(any(_tokens(term) <= terms for term in group) for group in groups):
                score += 3 if str(getattr(req.priority, "value", req.priority)) == "required" else 1
                requirement_matches.append(req.source_text)
        row.update(score=round(score, 4), pinned=row["fact_id"] in pins,
            reason=("Pinned by you" if row["fact_id"] in pins else "Excluded by you" if row["fact_id"] in exclusions
                    else "Matches: " + ", ".join(matched[:8]) if matched else "Additional career context"),
            matched_requirements=requirement_matches[:6])
    selected = {r["fact_id"] for r in rows if r["pinned"]}
    # Education is concise, factual context. Work/project headings can also stand
    # alone when the approved record has no eligible accomplishment bullets.
    education = [r for r in rows if r["section"] == "education" and r["kind"] == "entry" and r["fact_id"] not in exclusions]
    education.sort(key=lambda r: (-r["score"], -_date_key(content["education"][r["index"]])[0], r["index"]))
    for row in education[:3]:
        selected.add(row["fact_id"])
    budget = 410
    used = sum(len(r["text"].split()) for r in rows if r["fact_id"] in selected) + 30
    candidates = [r for r in rows
                  if (r["kind"] != "entry" or
                      r["section"] in {"experience", "projects"} and r["entry_id"] not in entries_with_facts)
                  and r["fact_id"] not in exclusions | selected]
    candidates.sort(key=lambda r: (-r["score"], -_date_key(content[r["section"]][r["index"]])[0], r["index"], r.get("item_index", 0), r["fact_id"]))
    by_id = {r["fact_id"]: r for r in rows}
    for row in candidates:
        fact_limit = 100 if row["section"] == "skills" else 20
        if sum(r["kind"] != "entry" and r["entry_id"] == row["entry_id"] and r["fact_id"] in selected for r in rows) >= fact_limit:
            continue
        cost = len(row["text"].split()) + (8 if row["kind"] == "entry" else 0)
        if row["entry_id"] not in selected:
            if row["kind"] != "entry":
                cost += len(by_id[row["entry_id"]]["text"].split()) + 8
            entry_limit = 30 if row["section"] == "skills" else 20
            if sum(r["kind"] == "entry" and r["section"] == row["section"] and r["fact_id"] in selected for r in rows) >= entry_limit:
                continue
        if used + cost > budget:
            continue
        selected.add(row["fact_id"])
        selected.add(row["entry_id"])
        used += cost
    # A pinned bullet implies its heading, even when the heading itself was not pinned.
    selected |= {r["entry_id"] for r in rows if r["fact_id"] in selected}
    if any(r["fact_id"] in exclusions for r in rows if r["fact_id"] in selected):
        raise ResumeLabError("a pinned fact belongs to an excluded entry")
    if not any(r["fact_id"] in selected for r in rows):
        raise ResumeLabError("include at least one printable education, experience, project, or individual skill; add content or remove exclusions")
    return {"profile_revision_id": revision["revision_id"], "profile_sha256": revision["content_sha256"],
        "profile_content": content, "job_fingerprint": job.fingerprint,
        "template_version": template_version, "pinned_fact_ids": sorted(set(pinned_fact_ids)),
        "excluded_fact_ids": sorted(set(excluded_fact_ids)),
        "selected": [r for r in rows if r["fact_id"] in selected],
        "omitted": [r for r in rows if r["fact_id"] not in selected],
        "page_target": 1, "selection_revision": "career-selection-v1"}


def compose_content(snapshot: Mapping[str, Any], selected_ids: Sequence[str] | None = None,
                    rewrites: Mapping[str, str] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    profile = snapshot["profile_content"]
    selected = {r["fact_id"] for r in snapshot["selected"]} if selected_ids is None else set(selected_ids)
    outputs = dict(rewrites or {})
    content: dict[str, Any] = {"identity": copy.deepcopy(profile["identity"]), "summary": ""}
    mapping: list[dict[str, Any]] = []
    for section in ("education", "experience", "projects", "skills"):
        entries = [e for e in profile.get(section, []) if e["entry_id"] in selected and not e.get("retired")]
        if len(entries) > (30 if section == "skills" else 20):
            raise CareerPageOverflow("too many pinned entries for the standard resume; unpin some entries")
        if section in {"education", "experience"}:
            entries.sort(key=_date_key, reverse=True)
        elif section == "projects":
            weights = {r["entry_id"]: max((x["score"] for x in snapshot["selected"] if x["entry_id"] == r["entry_id"]), default=0) for r in snapshot["selected"]}
            entries.sort(key=lambda e: (-weights.get(e["entry_id"], 0), e["entry_id"]))
        rendered_entries = []
        for entry in entries:
            values = {k: copy.deepcopy(v) for k, v in entry.items()
                      if k in {"institution", "degree", "dates", "location", "company", "role", "name", "context", "category", "url"}}
            key = "items" if section == "skills" else "bullets"
            facts = [f for f in entry.get(key, []) if f["fact_id"] in selected and not f.get("retired")]
            if len(facts) > (100 if section == "skills" else 20):
                raise CareerPageOverflow("too many pinned facts for one entry; unpin or shorten its content")
            if section == "skills":
                scores = {r["fact_id"]: r["score"] for r in snapshot["selected"]}
                facts.sort(key=lambda f: (-scores.get(f["fact_id"], 0), f["fact_id"]))
            if section == "education":
                values["details"] = str(entry.get("details") or "")
            else:
                values[key] = [outputs.get(f["fact_id"], f["text"]) for f in facts]
            index = len(rendered_entries)
            for j, f in enumerate(facts):
                mapping.append({"fact_id": f["fact_id"], "entry_id": entry["entry_id"],
                    "path": f"/{section}/{index}/{key}/{j}", "source_text": f["text"],
                    "output_text": outputs.get(f["fact_id"], f["text"])})
            rendered_entries.append(values)
        content[section] = rendered_entries
    return content, mapping


_NUMBERS = re.compile(r"(?<!\w)[\$€£]?\d[\d,]*(?:\.\d+)?(?:%|[kKmMbB])?(?!\w)")
_MEANING = re.compile(r"\b(?:not|never|no|without|assisted|supported|led|owned|managed|senior|junior|intern|approximately|up to|at least)\b", re.I)
_REWRITE_WORDS = set("a an the and or of for to with by in on into using through across from as that which while built build building developed develop developing created create creating implemented implement implementing delivered deliver delivering improved improve improving reduced reduce reducing increased increase increasing designed design designing maintained maintain maintaining collaborated collaborate collaborating automated automate automating optimized optimize optimizing enabled enable enabling provided provide providing resulting resulted result achieved achieve achieving".split())


def validate_rewrites(snapshot: Mapping[str, Any], rewrites: Mapping[str, str]) -> dict[str, str]:
    allowed = {r["fact_id"]: r for r in snapshot["selected"] if r["kind"] == "bullet"}
    if not isinstance(rewrites, Mapping) or set(rewrites) - set(allowed):
        raise ResumeBoundaryError("rewrites must reference selected accomplishment facts")
    skills = {str(f["text"]).casefold() for e in snapshot["profile_content"].get("skills", []) for f in e.get("items", []) if not f.get("retired")}
    result = {}
    for fid, text in rewrites.items():
        source = allowed[fid]["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ResumeBoundaryError("career rewrite text is invalid")
        if sorted(_NUMBERS.findall(text)) != sorted(_NUMBERS.findall(source)):
            raise ResumeBoundaryError("career rewrite changed a quantity")
        if sorted(x.casefold() for x in _MEANING.findall(text)) != sorted(x.casefold() for x in _MEANING.findall(source)):
            raise ResumeBoundaryError("career rewrite changed scope or qualification")
        # New proper names, technologies, qualifications and result nouns cannot be
        # smuggled in under a merely plausible citation. Allow source vocabulary,
        # reviewed equivalent terms, and a small connective/action-word vocabulary.
        from .grounding import CURATED_GROUNDING_EQUIVALENCES
        source_tokens = _tokens(source)
        vocabulary = set(source_tokens) | _REWRITE_WORDS
        for group in CURATED_GROUNDING_EQUIVALENCES:
            if any(_tokens(term) <= source_tokens for term in group):
                for term in group:
                    vocabulary.update(_tokens(term))
        if _tokens(text) - vocabulary:
            raise ResumeBoundaryError("career rewrite introduced unsupported terminology")
        for skill in skills:
            if re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", text, re.I) and not re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", source, re.I):
                raise ResumeBoundaryError("career rewrite moved a skill into unsupported history")
        result[fid] = text.strip()
    return result


def artifact_claims(mapping: Sequence[Mapping[str, Any]], content: Mapping[str, Any]) -> tuple[ResumeClaim, ...]:
    claims = [ResumeClaim("career_" + content_sha256({"fact": r["fact_id"], "path": r["path"]})[:24],
        str(r["output_text"]), ClaimOrigin.GENERATED_WORDING if r["output_text"] != r["source_text"] else ClaimOrigin.USER_ATTESTED,
        (str(r["fact_id"]),)) for r in mapping]
    # Education-only resumes still carry attested fixed facts.
    if not claims:
        claims = [ResumeClaim("career_fixed_" + content_sha256(content)[:24],
            str(content["identity"].get("name") or "Career profile"), ClaimOrigin.USER_ATTESTED)]
    return tuple(claims)


def validate_career_artifact(value: ArtifactInput, composition: Mapping[str, Any]) -> None:
    if composition["job_fingerprint"] != value.job.fingerprint:
        raise ResumeBoundaryError("career artifact belongs to another job")
    if value.purpose is ResumePurpose.SYNTHETIC_RESEARCH:
        return
    metadata = value.metadata
    if metadata.get("grounding_validator_revision") != CAREER_GROUNDING_REVISION:
        raise ResumeBoundaryError("career grounding revision is missing")
    snapshot = composition["snapshot"]
    chosen = metadata.get("selected_fact_ids")
    if not isinstance(chosen, list) or any(not isinstance(x, str) for x in chosen) or len(chosen) != len(set(chosen)):
        raise ResumeBoundaryError("career artifact selection is invalid")
    selected = {r["fact_id"] for r in snapshot["selected"]}
    if not set(chosen) <= selected:
        raise ResumeBoundaryError("career artifact contains unselected facts")
    if not chosen or any(r["fact_id"] in chosen and r["entry_id"] not in chosen for r in snapshot["selected"]):
        raise ResumeBoundaryError("career artifact has empty or detached source facts")
    if any(r["pinned"] and r["fact_id"] not in chosen for r in snapshot["selected"]):
        raise ResumeBoundaryError("career artifact dropped pinned facts")
    rewrites = validate_rewrites(snapshot, metadata.get("rewrites") or {})
    content, mapping = compose_content(snapshot, chosen, rewrites)
    rendered = render_resume_tex(content, template_version=snapshot["template_version"])
    if (rendered.tex_source != value.tex_source or rendered.intended_text != value.intended_text
            or canonical_json(value.claims) != canonical_json(artifact_claims(mapping, content))
            or metadata.get("pages") != 1):
        raise ResumeBoundaryError("career artifact differs from its selected facts or one-page contract")
