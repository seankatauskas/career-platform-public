"""Deterministic job-description requirement graph extraction."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    JobSnapshot,
    Requirement,
    RequirementGraph,
    RequirementKind,
    RequirementPriority,
    ResumeLabError,
    canonical_json,
    sha256_text,
)


GRAPH_REVISION = "requirements-v4-bounded-non-fit-sections"
MAX_REQUIREMENTS = 200


class RequirementGraphLimitError(ResumeLabError):
    """The job would create more criteria than the scorer can safely evaluate."""

_SECTION_REQUIRED = re.compile(
    r"^(?:(?:minimum|required|basic|must[- ]have) (?:qualifications?|requirements?)|"
    r"qualifications?|requirements?|what you bring|who you are|your background|"
    r"about you|experience and skills):?$",
    re.I,
)
_SECTION_PREFERRED = re.compile(
    r"^(?:preferred|desired|nice[- ]to[- ]have) (?:qualifications?|requirements?|skills?)?:?$",
    re.I,
)
_SECTION_RESPONSIBILITY = re.compile(
    r"^(?:responsibilities|what you(?:'|’)ll do|what you will do|the role|role overview|"
    r"duties|the opportunity|position summary|job summary|your impact):?$",
    re.I,
)
_SECTION_END = re.compile(
    r"^(?:benefits|compensation|about us|about the company|equal opportunity|"
    r"physical requirements|our values|perks|diversity|inclusion|accommodations?|"
    r"privacy notice|legal notice):?$",
    re.I,
)
_BULLET = re.compile(r"^[\s\-–—*•▪◦]+")
_YEARS = re.compile(r"\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", re.I)

_REQUIRED_CUES = re.compile(
    r"\b(?:must|required|minimum|at least|need to|proficien(?:t|cy)|demonstrated|"
    r"strong knowledge|hands[- ]on|experience (?:with|in|building|developing|leading))\b",
    re.I,
)
_PREFERRED_CUES = re.compile(
    r"\b(?:preferred|ideally|nice to have|bonus|desired|a plus)\b", re.I
)
_RESPONSIBILITY_CUES = re.compile(
    r"\b(?:you will|you'll|responsible for|design|build|develop|lead|own|operate|"
    r"maintain|collaborate|deliver|implement|manage)\b",
    re.I,
)
_ELIGIBILITY = re.compile(
    r"\b(?:work authori[sz]ation|authori[sz]ed to work|visa sponsorship|clearance|"
    r"citizen(?:ship)?|eligible to work|right to work|resid(?:e|ing)|"
    r"liv(?:e|ing) in|(?:located|based) (?:in|within|near)|travel)\b",
    re.I,
)
_EDUCATION = re.compile(
    r"\b(?:bachelor(?:'s)?|master(?:'s)?|ph\.?d\.?|doctorate|degree|b\.?s\.?|m\.?s\.?)\b",
    re.I,
)
_CERTIFICATION = re.compile(
    r"\b(?:certification|certified|license|licensed|cpa|pmp|cissp|security\+)\b",
    re.I,
)
_BOILERPLATE = re.compile(
    r"\b(?:equal opportunity employer|affirmative action|reasonable accommodation)\b",
    re.I,
)
_PROTECTED_ATTRIBUTE_LABEL = re.compile(
    r"\b(?:age|ancestry|birth date|citizenship status|color|creed|date of birth|"
    r"disabilit(?:y|ies)|ethnic(?:ity)?|family status|gender(?: identity| expression)?|"
    r"genetic information|marital status|military status|national origin|pregnan(?:cy|t)|"
    r"pronouns?|race|racial|religion|religious|sex|sexual orientation|transgender|"
    r"nonbinary|veteran status)\b",
    re.I,
)
_PROTECTED_FIELD_LABEL = re.compile(
    r"^\s*(?:self[- ]identification\s+)?(?:age|ancestry|birth date|citizenship status|"
    r"color|creed|date of birth|disabilit(?:y|ies)|ethnic(?:ity)?|family status|"
    r"gender(?: identity| expression)?|genetic information|marital status|"
    r"military status|national origin|pregnan(?:cy|t)|pronouns?|race|racial identity|"
    r"religion|religious affiliation|sex|sexual orientation|transgender status|"
    r"nonbinary status|veteran status)\s*[:=\-]",
    re.I,
)
_EEO_CONTEXT = re.compile(
    r"\b(?:equal (?:employment )?opportunity|without regard to|regardless of|"
    r"do(?:es)? not discriminate|protected class|affirmative action)\b",
    re.I,
)
_COMPENSATION_DISCLOSURE = re.compile(
    r"^\s*(?:(?:annual|base|expected|target|total)\s+)?"
    r"(?:salary|compensation|pay\s+range|salary\s+range|total\s+rewards?)\b"
    r"(?:\s+range)?\s*(?::|is\b|from\b|between\b|of\b).*"
    r"(?:[$€£]\s?\d[\d,.]*|\d[\d,.]*\s*(?:usd|eur|gbp)\b)",
    re.I,
)
_BENEFITS_DISCLOSURE = re.compile(
    r"^\s*(?:benefits? include|our benefits?|we (?:offer|provide)|perks? include)\b",
    re.I,
)
_PROTECTED_IDENTITY_VALUE = re.compile(
    r"\b(?:asian|black|white|caucasian|hispanic|latin[oaex]?|indigenous|"
    r"native american|pacific islander|middle eastern|male|female|nonbinary|"
    r"transgender|cisgender|gay|lesbian|bisexual|disabled|veterans?)\b",
    re.I,
)
_IDENTITY_ONLY = re.compile(
    r"^(?:(?:required|preferred|must(?: be)?|we (?:seek|prefer)|seeking)\s*:?[ ]*)?"
    r"(?:an?\s+)?(?:[\w'’-]+\s+){0,6}(?:candidates?|applicants?|individuals?|"
    r"persons?|people)\s*[.!]?$",
    re.I,
)

# Canonical concepts and literal aliases.  This list intentionally remains small and
# auditable; taxonomy/embedding expansion belongs in a later model-assisted layer.
ALIASES = {
    "python": ("python",),
    "java": ("java",),
    "javascript": ("javascript", "java script", "js"),
    "typescript": ("typescript", "type script", "ts"),
    "go": ("golang", "go language"),
    "rust": ("rust",),
    "c++": ("c++", "cpp"),
    "c#": ("c#", "c sharp"),
    ".net": (".net", "dotnet", "dot net"),
    "react": ("react", "react.js", "reactjs"),
    "node.js": ("node.js", "nodejs", "node js"),
    "sql": ("sql", "structured query language"),
    "postgresql": ("postgresql", "postgres", "postgre sql"),
    "mysql": ("mysql", "my sql"),
    "aws": ("aws", "amazon web services"),
    "azure": ("azure", "microsoft azure"),
    "gcp": ("gcp", "google cloud", "google cloud platform"),
    "kubernetes": ("kubernetes", "k8s"),
    "docker": ("docker", "containers", "containerization"),
    "terraform": ("terraform", "infrastructure as code", "iac"),
    "linux": ("linux",),
    "git": ("git", "version control"),
    "rest api": ("rest api", "restful api", "restful services"),
    "graphql": ("graphql", "graph ql"),
    "distributed systems": ("distributed systems", "distributed system"),
    "machine learning": ("machine learning", "ml"),
    "large language models": (
        "large language models",
        "large language model",
        "llm",
        "llms",
    ),
    "natural language processing": ("natural language processing", "nlp"),
    "data pipelines": ("data pipelines", "data pipeline", "etl", "elt"),
    "ci/cd": (
        "ci/cd",
        "continuous integration",
        "continuous delivery",
        "continuous deployment",
    ),
    "observability": ("observability", "monitoring", "telemetry"),
    "leadership": ("leadership", "led", "leading", "managed", "mentored"),
}

_STOPWORDS = frozenset(
    "a an and are as at be by can do for from have in into is it of on or our that "
    "the their this to using we who will with you your years year experience ability "
    "knowledge strong demonstrated required preferred minimum qualifications skills "
    "responsible work working role team including plus must".split()
)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("’", "'")
    return " ".join(re.sub(r"[^a-z0-9+#./'-]+", " ", normalized).split())


def contains_protected_attribute_label(value: str) -> bool:
    """Identify an explicitly labelled protected-attribute field or clause."""

    return bool(_PROTECTED_FIELD_LABEL.search(value))


def is_non_fit_clause(value: str) -> bool:
    """Return whether text belongs outside resume fit regardless of its keywords."""

    return bool(
        _BOILERPLATE.search(value)
        or _PROTECTED_FIELD_LABEL.search(value)
        or (
            _EEO_CONTEXT.search(value)
            and _PROTECTED_ATTRIBUTE_LABEL.search(value)
        )
        or _COMPENSATION_DISCLOSURE.search(value)
        or _BENEFITS_DISCLOSURE.search(value)
        or (
            _IDENTITY_ONLY.fullmatch(" ".join(value.strip().split()))
            and (
                _PROTECTED_ATTRIBUTE_LABEL.search(value)
                or _PROTECTED_IDENTITY_VALUE.search(value)
            )
        )
    )


def _has_section_recovery_cue(value: str) -> bool:
    return bool(
        _REQUIRED_CUES.search(value)
        or _PREFERRED_CUES.search(value)
        or _RESPONSIBILITY_CUES.search(value)
        or _YEARS.search(value)
    )


def _looks_like_heading(value: str) -> bool:
    """Recognize a bounded display heading without treating prose as a boundary."""

    cleaned = value.strip().rstrip(":").strip()
    if not cleaned or len(cleaned) > 100 or re.search(r"[.!?]", cleaned):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'’/&-]*", cleaned)
    if not words or len(words) > 10:
        return False
    if len(words) == 1:
        return words[0].casefold() in {
            "qualifications",
            "requirements",
            "responsibilities",
            "skills",
            "experience",
            "overview",
            "opportunity",
        }
    minor = {"a", "an", "and", "for", "of", "the", "to", "you", "your"}
    return all(
        word.casefold() in minor or word.isupper() or word[0].isupper()
        for word in words
    )


def _source_in_ignored_section(description: str, source_start: int) -> bool:
    """Resolve the section state immediately before one exact model source span."""

    ignored = False
    for piece in _pieces(description[:source_start]):
        heading = piece.strip()
        if (
            _SECTION_REQUIRED.fullmatch(heading)
            or _SECTION_PREFERRED.fullmatch(heading)
            or _SECTION_RESPONSIBILITY.fullmatch(heading)
        ):
            ignored = False
        elif _SECTION_END.fullmatch(heading):
            ignored = True
        elif ignored and _looks_like_heading(heading) and not is_non_fit_clause(
            heading
        ):
            ignored = False
        elif ignored and not is_non_fit_clause(piece) and _has_section_recovery_cue(
            piece
        ):
            ignored = False
    return ignored


def _plain_description(value: str) -> str:
    text = re.sub(r"(?i)<\s*(?:li|p|br|h[1-6])\b[^>]*>", "\n", value)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text).replace("\r", "\n")


def _pieces(description: str) -> Iterable[str]:
    for raw_line in _plain_description(description).splitlines():
        line = " ".join(_BULLET.sub("", raw_line).split())
        if not line:
            continue
        # Long prose paragraphs often contain several independently screenable rules.
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line)
        for value in parts:
            cleaned = value.strip(" \t;-–—")
            if cleaned:
                yield cleaned


def _priority_for(
    section: RequirementPriority | None, text: str
) -> RequirementPriority:
    if _PREFERRED_CUES.search(text):
        return RequirementPriority.PREFERRED
    if _REQUIRED_CUES.search(text):
        return RequirementPriority.REQUIRED
    return section or RequirementPriority.RESPONSIBILITY


def _kind_for(text: str, minimum_years: float | None) -> RequirementKind:
    if _ELIGIBILITY.search(text):
        return RequirementKind.ELIGIBILITY
    if _EDUCATION.search(text):
        return RequirementKind.EDUCATION
    if _CERTIFICATION.search(text):
        return RequirementKind.CERTIFICATION
    if minimum_years is not None or re.search(r"\bexperience\b", text, re.I):
        return RequirementKind.EXPERIENCE
    if any(_contains_alias(text, aliases) for aliases in ALIASES.values()):
        return RequirementKind.SKILL
    return RequirementKind.OTHER


def _contains_literal(normalized: str, literal: str) -> bool:
    wanted = normalize_text(literal)
    if not wanted:
        return False
    return bool(
        re.search(r"(?<![a-z0-9])" + re.escape(wanted) + r"(?![a-z0-9])", normalized)
    )


def _contains_alias(text: str, aliases: Sequence[str]) -> bool:
    normalized = normalize_text(text)
    return any(_contains_literal(normalized, alias) for alias in aliases)


def _known_matches(text: str) -> List[Tuple[int, str, Tuple[str, ...]]]:
    normalized = normalize_text(text)
    result = []
    for canonical, aliases in ALIASES.items():
        positions = []
        for alias in aliases:
            wanted = normalize_text(alias)
            match = re.search(
                r"(?<![a-z0-9])" + re.escape(wanted) + r"(?![a-z0-9])",
                normalized,
            )
            if match:
                positions.append(match.start())
        if positions:
            values = tuple(dict.fromkeys((canonical, *aliases)))
            result.append((min(positions), canonical, values))
    return sorted(result)


def _fallback_groups(text: str) -> Tuple[Tuple[str, ...], ...]:
    tokens = []
    for token in normalize_text(text).split():
        token = token.strip("./'-")
        if len(token) < 3 or token in _STOPWORDS or token.isdigit():
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) == 6:
            break
    return tuple((token,) for token in tokens)


def _term_groups(text: str, kind: RequirementKind) -> Tuple[Tuple[str, ...], ...]:
    if kind is RequirementKind.EDUCATION:
        degree = (
            "bachelor degree",
            "bachelor's degree",
            "bachelors degree",
            "bs degree",
        )
        graduate = (
            "master degree",
            "master's degree",
            "masters degree",
            "ms degree",
            "phd",
        )
        normalized = normalize_text(text)
        selected = (
            graduate
            if re.search(r"\b(?:master|phd|doctorate|m s)\b", normalized)
            else degree
        )
        if re.search(r"\b(?:or equivalent|equivalent experience)\b", normalized):
            return (tuple((*selected, "equivalent experience")),)
        return (selected,)

    matches = _known_matches(text)
    if not matches:
        return _fallback_groups(text)
    groups: List[Tuple[str, ...]] = []
    normalized = normalize_text(text)
    previous_position = None
    for position, _canonical, aliases in matches:
        if previous_position is not None:
            between = normalized[previous_position:position]
            if re.search(r"\bor\b|/", between) and groups:
                groups[-1] = tuple(dict.fromkeys((*groups[-1], *aliases)))
                previous_position = position
                continue
        groups.append(aliases)
        previous_position = position
    return tuple(groups)


def _make_requirement(
    source_text: str,
    priority: RequirementPriority,
    kind: RequirementKind,
    *,
    source_start: Optional[int] = None,
    source_end: Optional[int] = None,
    extraction_origin: str = "deterministic",
) -> Optional[Requirement]:
    years = _YEARS.search(source_text)
    minimum_years = float(years.group(1)) if years else None
    groups = _term_groups(source_text, kind)
    if not groups and minimum_years is None:
        return None
    normalized_piece = normalize_text(source_text)
    requirement_id = (
        "req_"
        + sha256_text(
            canonical_json(
                {
                    "source": normalized_piece,
                    "priority": priority,
                    "kind": kind,
                    "groups": groups,
                    "minimum_years": minimum_years,
                }
            )
        )[:20]
    )
    return Requirement(
        requirement_id=requirement_id,
        priority=priority,
        kind=kind,
        source_text=source_text,
        term_groups=groups,
        minimum_years=minimum_years,
        source_start=source_start,
        source_end=source_end,
        extraction_origin=extraction_origin,
    )


def _graph(job: JobSnapshot, requirements: Sequence[Requirement]) -> RequirementGraph:
    if len(requirements) > MAX_REQUIREMENTS:
        raise RequirementGraphLimitError(
            f"job description exceeds the {MAX_REQUIREMENTS}-requirement graph limit"
        )
    payload = {
        "revision": GRAPH_REVISION,
        "job_fingerprint": job.fingerprint,
        "requirements": requirements,
    }
    return RequirementGraph(
        graph_revision=GRAPH_REVISION,
        job_fingerprint=job.fingerprint,
        requirements=tuple(requirements),
        fingerprint=sha256_text(canonical_json(payload)),
    )


def extract_requirement_graph(job: JobSnapshot) -> RequirementGraph:
    """Extract an auditable AND-of-OR graph from one immutable job snapshot."""

    job.validate()
    section: RequirementPriority | None = None
    ignored_section = False
    requirements = []
    seen = set()
    for piece in _pieces(job.description):
        heading = piece.strip()
        if _SECTION_REQUIRED.fullmatch(heading):
            section = RequirementPriority.REQUIRED
            ignored_section = False
            continue
        if _SECTION_PREFERRED.fullmatch(heading):
            section = RequirementPriority.PREFERRED
            ignored_section = False
            continue
        if _SECTION_RESPONSIBILITY.fullmatch(heading):
            section = RequirementPriority.RESPONSIBILITY
            ignored_section = False
            continue
        if _SECTION_END.fullmatch(heading):
            section = None
            ignored_section = True
            continue
        if (
            ignored_section
            and _looks_like_heading(heading)
            and not is_non_fit_clause(heading)
        ):
            ignored_section = False
        if (
            ignored_section
            and not is_non_fit_clause(piece)
            and _has_section_recovery_cue(piece)
        ):
            # Many postings put ordinary role prose immediately after an About block
            # without another visual heading.  A strong screening/action cue is a
            # deterministic boundary back into job-fit content.
            ignored_section = False
        if ignored_section or is_non_fit_clause(piece):
            continue
        screenable = bool(
            section
            or _REQUIRED_CUES.search(piece)
            or _PREFERRED_CUES.search(piece)
            or _RESPONSIBILITY_CUES.search(piece)
            or _ELIGIBILITY.search(piece)
            or _EDUCATION.search(piece)
            or _CERTIFICATION.search(piece)
            or _known_matches(piece)
        )
        if not screenable:
            continue
        normalized_piece = normalize_text(piece)
        if normalized_piece in seen:
            continue
        seen.add(normalized_piece)
        priority = _priority_for(section, piece)
        years = _YEARS.search(piece)
        minimum_years = float(years.group(1)) if years else None
        kind = _kind_for(piece, minimum_years)
        source_start = job.description.find(piece)
        requirement = _make_requirement(
            piece,
            priority,
            kind,
            source_start=source_start if source_start >= 0 else None,
            source_end=source_start + len(piece) if source_start >= 0 else None,
        )
        if requirement is not None:
            requirements.append(requirement)
            if len(requirements) > MAX_REQUIREMENTS:
                raise RequirementGraphLimitError(
                    f"job description exceeds the {MAX_REQUIREMENTS}-requirement graph limit"
                )
    return _graph(job, requirements)


_CLAUSE_KINDS = frozenset(("required", "preferred", "responsibility", "eligibility"))
_CLAUSE_KEYS = frozenset(
    (
        "source_span",
        "source_text",
        "text",
        "source_start",
        "start",
        "source_end",
        "end",
        "kind",
        "priority",
        "requirement_kind",
    )
)


def _one_value(clause: Mapping[str, Any], *names: str) -> Any:
    values = [clause[name] for name in names if name in clause]
    if len(values) > 1 and any(value != values[0] for value in values[1:]):
        raise ResumeLabError(f"model clause has conflicting {names[0]} values")
    return values[0] if values else None


def _model_span(description: str, clause: Mapping[str, Any]) -> Tuple[str, int, int]:
    span = clause.get("source_span")
    if span is not None and not isinstance(span, Mapping):
        raise ResumeLabError("model clause source_span must be an object")
    span = span or {}
    start = _one_value(clause, "source_start", "start")
    end = _one_value(clause, "source_end", "end")
    source_text = _one_value(clause, "source_text", "text")
    if "start" in span:
        if start is not None and start != span["start"]:
            raise ResumeLabError("model clause has conflicting source_start values")
        start = span["start"]
    if "end" in span:
        if end is not None and end != span["end"]:
            raise ResumeLabError("model clause has conflicting source_end values")
        end = span["end"]
    if "text" in span:
        if source_text is not None and source_text != span["text"]:
            raise ResumeLabError("model clause has conflicting source_text values")
        source_text = span["text"]
    if set(span) - {"start", "end", "text"}:
        raise ResumeLabError("model clause source_span has unsupported fields")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end <= start
        or end > len(description)
    ):
        raise ResumeLabError("model clause source span is invalid")
    exact = description[start:end]
    if source_text is None:
        source_text = exact
    if not isinstance(source_text, str) or source_text != exact:
        raise ResumeLabError("model clause text must exactly match its source span")
    if not source_text.strip() or len(source_text) > 20_000 or "\x00" in source_text:
        raise ResumeLabError("model clause source text is invalid")
    return source_text, start, end


def _model_priority_and_kind(
    clause: Mapping[str, Any], source_text: str
) -> Tuple[RequirementPriority, RequirementKind]:
    raw_kind = clause.get("kind")
    raw_priority = clause.get("priority")
    raw_requirement_kind = clause.get("requirement_kind")
    role: Optional[str] = None
    if raw_kind is not None:
        try:
            kind_text = str(raw_kind.value if hasattr(raw_kind, "value") else raw_kind)
        except Exception as exc:  # pragma: no cover - defensive custom enum objects
            raise ResumeLabError("model clause kind is invalid") from exc
        if kind_text in _CLAUSE_KINDS:
            role = kind_text
        elif kind_text in {value.value for value in RequirementKind}:
            if raw_requirement_kind is not None:
                raise ResumeLabError("model clause has conflicting requirement kinds")
            raw_requirement_kind = kind_text
        else:
            raise ResumeLabError("model clause kind is invalid")
    if role == "eligibility":
        priority = RequirementPriority.REQUIRED
    elif role is not None:
        priority = RequirementPriority(role)
    elif raw_priority is not None:
        try:
            priority = RequirementPriority(
                str(
                    raw_priority.value
                    if hasattr(raw_priority, "value")
                    else raw_priority
                )
            )
        except ValueError:
            raise ResumeLabError("model clause priority is invalid") from None
    else:
        raise ResumeLabError(
            "model clause needs a required/preferred/responsibility kind"
        )
    if raw_priority is not None:
        try:
            declared_priority = RequirementPriority(
                str(
                    raw_priority.value
                    if hasattr(raw_priority, "value")
                    else raw_priority
                )
            )
        except ValueError:
            raise ResumeLabError("model clause priority is invalid") from None
        if declared_priority is not priority:
            raise ResumeLabError("model clause kind conflicts with priority")

    years = _YEARS.search(source_text)
    inferred = _kind_for(source_text, float(years.group(1)) if years else None)
    if role == "eligibility":
        kind = RequirementKind.ELIGIBILITY
    elif raw_requirement_kind is not None:
        try:
            kind = RequirementKind(
                str(
                    raw_requirement_kind.value
                    if hasattr(raw_requirement_kind, "value")
                    else raw_requirement_kind
                )
            )
        except ValueError:
            raise ResumeLabError("model clause requirement_kind is invalid") from None
    else:
        kind = inferred
    if role == "eligibility" and kind is not RequirementKind.ELIGIBILITY:
        raise ResumeLabError("eligibility clause must use eligibility requirement_kind")
    return priority, kind


def requirement_graph_from_clauses(
    job: JobSnapshot,
    clauses: Optional[Sequence[Mapping[str, Any]]],
    *,
    include_fallback: bool = True,
) -> RequirementGraph:
    """Validate model clauses, derive terms locally, and merge deterministic fallback.

    The model may identify only an exact source span and its role (``required``,
    ``preferred``, ``responsibility``, or ``eligibility``).  It cannot introduce
    requirement text or arbitrary search terms: aliases, minimum years, IDs, and the
    final fingerprint are all derived deterministically in this module.
    """

    job.validate()
    if clauses is None:
        clauses = ()
    if isinstance(clauses, (str, bytes)) or not isinstance(clauses, Sequence):
        raise ResumeLabError("model clauses must be a sequence")
    if len(clauses) > MAX_REQUIREMENTS:
        raise ResumeLabError("model clauses exceed the supported limit")
    if not clauses:
        return extract_requirement_graph(job) if include_fallback else _graph(job, ())

    model_requirements: List[Requirement] = []
    seen_spans: list[tuple[int, int]] = []
    for clause in clauses:
        if not isinstance(clause, Mapping):
            raise ResumeLabError("each model clause must be an object")
        if set(clause) - _CLAUSE_KEYS:
            raise ResumeLabError("model clause contains unsupported fields")
        source_text, source_start, source_end = _model_span(job.description, clause)
        if is_non_fit_clause(source_text) or (
            _source_in_ignored_section(job.description, source_start)
            and not _has_section_recovery_cue(source_text)
        ):
            raise ResumeLabError("model clause points to non-screening boilerplate")
        priority, kind = _model_priority_and_kind(clause, source_text)
        if any(
            source_start < previous_end and previous_start < source_end
            for previous_start, previous_end in seen_spans
        ):
            raise ResumeLabError("model clause source spans must not overlap")
        seen_spans.append((source_start, source_end))
        requirement = _make_requirement(
            source_text,
            priority,
            kind,
            source_start=source_start,
            source_end=source_end,
            extraction_origin="model_clause",
        )
        if requirement is not None:
            model_requirements.append(requirement)

    priority_order = {
        RequirementPriority.REQUIRED: 0,
        RequirementPriority.PREFERRED: 1,
        RequirementPriority.RESPONSIBILITY: 2,
    }
    model_requirements.sort(
        key=lambda item: (
            item.source_start
            if item.source_start is not None
            else len(job.description),
            item.source_end if item.source_end is not None else len(job.description),
            priority_order[item.priority],
            item.kind.value,
            item.requirement_id,
        )
    )

    if not include_fallback:
        return _graph(job, model_requirements)
    fallback = extract_requirement_graph(job)
    # A nested model quotation and its enclosing deterministic sentence are one
    # screening criterion, not two. Prefer the complete deterministic sentence for
    # partial overlaps so no sibling term (for example Kubernetes beside Python) is
    # lost; exact-span model classifications still win below.
    fallback_spans = {
        (item.source_start, item.source_end)
        for item in fallback.requirements
        if item.source_start is not None and item.source_end is not None
    }
    model_requirements = [
        item
        for item in model_requirements
        if item.source_start is None
        or item.source_end is None
        or not any(
            item.source_start < fallback_end
            and fallback_start < item.source_end
            and (item.source_start, item.source_end)
            != (fallback_start, fallback_end)
            for fallback_start, fallback_end in fallback_spans
        )
    ]
    seen_text = {
        normalize_text(_plain_description(item.source_text))
        for item in model_requirements
    }
    merged = list(model_requirements)
    for requirement in fallback.requirements:
        key = normalize_text(_plain_description(requirement.source_text))
        overlaps_model = any(
            requirement.source_start is not None
            and requirement.source_end is not None
            and model.source_start is not None
            and model.source_end is not None
            and requirement.source_start < model.source_end
            and model.source_start < requirement.source_end
            for model in model_requirements
        )
        if key not in seen_text and not overlaps_model:
            seen_text.add(key)
            merged.append(requirement)
    return _graph(job, merged)


def merge_requirement_clauses(
    job: JobSnapshot,
    clauses: Optional[Sequence[Mapping[str, Any]]],
) -> RequirementGraph:
    """Public convenience alias for validated model clauses plus local fallback."""

    return requirement_graph_from_clauses(job, clauses, include_fallback=True)


__all__ = [
    "ALIASES",
    "GRAPH_REVISION",
    "MAX_REQUIREMENTS",
    "RequirementGraphLimitError",
    "extract_requirement_graph",
    "merge_requirement_clauses",
    "contains_protected_attribute_label",
    "is_non_fit_clause",
    "normalize_text",
    "requirement_graph_from_clauses",
]
