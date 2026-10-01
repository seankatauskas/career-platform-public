"""Fail-closed grounding checks at the real-artifact persistence boundary.

Model output validation is necessary but is not the authority for whether generated
wording may be stored as a real resume artifact.  This module re-validates that
boundary from immutable source claims, a small reviewed equivalence table, and the
exact structured content which produced the artifact's intended text.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from job_search.contracts import ContractError

from .contracts import (
    ClaimOrigin,
    ResumeBoundaryError,
    ResumeClaim,
    ResumeLabError,
    ResumePurpose,
    sha256_text,
    validate_identifier,
)
from .tex import RenderedResume, render_resume_tex


GROUNDING_VALIDATOR_REVISION = "grounding-boundary-v5-relational"
GROUNDING_EQUIVALENCE_REVISION = "grounding-equivalences-v3"

# These groups are deliberately narrower than search/scoring aliases.  Membership
# means the spellings are interchangeable without adding a skill or broadening a
# claim.  Job descriptions and model output are never allowed to extend this table.
CURATED_GROUNDING_EQUIVALENCES: tuple[tuple[str, ...], ...] = (
    ("javascript", "js"),
    ("typescript", "ts"),
    ("c++", "cpp"),
    ("c#", "c sharp"),
    (".net", "dotnet", "dot net"),
    ("node.js", "nodejs", "node js"),
    ("postgresql", "postgres", "postgre sql"),
    ("aws", "amazon web services"),
    ("gcp", "google cloud platform"),
    ("kubernetes", "k8s"),
    ("infrastructure as code", "iac"),
    ("large language model", "llm"),
    ("large language models", "llms"),
    ("natural language processing", "nlp"),
    ("build", "built"),
    ("develop", "developed"),
    ("create", "created"),
    ("implement", "implemented"),
    ("maintain", "maintained"),
    ("improve", "improved"),
    ("enhance", "enhanced"),
    ("optimize", "optimized"),
    ("automate", "automated"),
    ("streamline", "streamlined"),
    ("analyze", "analyzed"),
    ("evaluate", "evaluated"),
    ("assess", "assessed"),
    ("collaborate", "collaborated"),
    ("partner", "partnered"),
    ("service", "services"),
    ("api", "apis", "application programming interface", "application programming interfaces"),
)

_TOKEN = re.compile(
    r"(?:\.[a-z0-9]+|[a-z0-9]+(?:\.[a-z0-9]+)+|[a-z0-9]+(?:\+\+|#)?|\w+)",
    re.UNICODE,
)
_METRIC = re.compile(
    r"(?<![\w])(?:[$€£])?\d(?:[\d,]*\d)?(?:\.\d+)?(?:[kmb])?(?:%|\+)?(?![\w])",
    re.IGNORECASE,
)
# Punctuation is normally presentation-only, but these characters carry factual
# meaning in resume claims.  Their canonical value and position relative to the
# conserved lexical tokens must not change.  Typography variants of the *same*
# operator are collapsed (for example ``≤`` and ``<=``).
_MULTI_CHARACTER_OPERATORS: tuple[tuple[str, str], ...] = (
    ("<=>", "<=>"),
    ("<->", "<->"),
    ("<=", "<="),
    (">=", ">="),
    ("!=", "!="),
    ("==", "=="),
    ("~=", "~="),
    ("->", "->"),
    ("<-", "<-"),
    ("=>", "=>"),
)
_SINGLE_CHARACTER_OPERATORS = {
    "<": "<",
    ">": ">",
    "=": "=",
    "≤": "<=",
    "≥": ">=",
    "≠": "!=",
    "≈": "~=",
    "+": "+",
    "±": "+/-",
    "∓": "-/+",
    "*": "*",
    "×": "*",
    "÷": "/",
    "/": "/",
    "\\": "\\",
    "^": "^",
    "~": "~",
    "%": "%",
    "‰": "permille",
    "‱": "permyriad",
    "&": "&",
    "|": "|",
    "→": "->",
    "←": "<-",
    "↔": "<->",
    "?": "?",
    "!": "!",
    "#": "#",
    "@": "@",
}
_RANGE_OR_MINUS = frozenset("-‐‑‒–—−")
_BIDI_CONTROL_CLASSES = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
)
_SENIORITY_TERMS = (
    "intern",
    "junior",
    "mid",
    "senior",
    "staff",
    "principal",
    "lead",
    "manager",
    "director",
    "vp",
    "vice president",
    "chief",
)
_SCOPE_TERMS = (
    "enterprise",
    "global",
    "production",
    "large scale",
    "large-scale",
    "high scale",
    "high-scale",
    "millions",
    "billions",
    "managed",
    "led",
    "owned",
    "architected",
)
# Articles may change without changing who did what to whom. Prepositions and
# conjunctions are intentionally conserved: changing ``from``/``to`` or moving an
# ``and`` can invert a factual relationship even when every noun and number survives.
_IGNORABLE_ARTICLES = frozenset({"a", "an", "the"})
_TRUTH_QUALIFIERS = (
    "not",
    "never",
    "no",
    "without",
    "cannot",
    "unable",
    "assisted",
    "helped",
    "contributed",
    "supported",
    "participated",
    "up to",
    "at least",
    "at most",
)


def _truth_qualifier_positions(value: str) -> tuple[tuple[int, str], ...]:
    tokens = _tokens(value)
    positions = []
    for qualifier in _TRUTH_QUALIFIERS:
        candidate = _tokens(qualifier)
        width = len(candidate)
        positions.extend(
            (index, qualifier)
            for index in range(max(0, len(tokens) - width + 1))
            if tuple(tokens[index : index + width]) == candidate
        )
    return tuple(sorted(positions))


class GroundingValidationError(ResumeBoundaryError):
    """Generated wording could not be proven from immutable source facts."""


@dataclass(frozen=True)
class GroundedArtifactValidation:
    """Validated values safe to pass into real-artifact persistence."""

    output: Mapping[str, Any]
    claims: tuple[ResumeClaim, ...]
    rendered: RenderedResume
    intended_text_sha256: str
    validator_revision: str = GROUNDING_VALIDATOR_REVISION
    equivalence_revision: str = GROUNDING_EQUIVALENCE_REVISION


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _validate_grounding_characters(value: str) -> None:
    """Reject characters that can conceal or visually reorder factual wording."""

    for character in value:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if (
            category in {"Cc", "Cf", "Cs", "Co"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROL_CLASSES
            or 0xFE00 <= codepoint <= 0xFE0F
            or 0xE0100 <= codepoint <= 0xE01EF
        ):
            raise GroundingValidationError(
                "grounded wording contains a control, invisible, private-use, "
                "or bidirectional formatting character"
            )


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _TOKEN.finditer(_normalize(value)))


def _equivalence_sequences() -> tuple[tuple[tuple[str, ...], str], ...]:
    rows: list[tuple[tuple[str, ...], str]] = []
    for index, group in enumerate(CURATED_GROUNDING_EQUIVALENCES):
        marker = f"equivalence:{index}"
        for term in group:
            tokenized = _tokens(term)
            if not tokenized:
                raise RuntimeError("grounding equivalence terms must have tokens")
            rows.append((tokenized, marker))
    rows.sort(key=lambda item: (-len(item[0]), item[0], item[1]))
    return tuple(rows)


_EQUIVALENCE_SEQUENCES = _equivalence_sequences()


def _canonical_tokens(value: str) -> tuple[str, ...]:
    """Collapse reviewed equivalent spellings into one conserved concept token."""

    raw = _tokens(value)
    result: list[str] = []
    index = 0
    while index < len(raw):
        match = next(
            (
                (candidate, marker)
                for candidate, marker in _EQUIVALENCE_SEQUENCES
                if raw[index : index + len(candidate)] == candidate
            ),
            None,
        )
        if match is None:
            result.append(raw[index])
            index += 1
        else:
            candidate, marker = match
            result.append(marker)
            index += len(candidate)
    return tuple(result)


def canonical_grounding_tokens(value: str) -> tuple[str, ...]:
    """Return the reviewed, order-preserving token form for factual rewrites."""

    if not isinstance(value, str):
        raise GroundingValidationError("grounded wording must be text")
    _validate_grounding_characters(value)
    return _canonical_tokens(value)


def _canonical_token_boundaries(value: str) -> tuple[list[re.Match[str]], list[int]]:
    """Map each raw lexical boundary to its collapsed canonical token count."""

    normalized = _normalize(value)
    matches = list(_TOKEN.finditer(normalized))
    raw = tuple(match.group(0) for match in matches)
    boundary_counts = [0] * (len(raw) + 1)
    raw_index = 0
    canonical_count = 0
    while raw_index < len(raw):
        match = next(
            (
                candidate
                for candidate, _marker in _EQUIVALENCE_SEQUENCES
                if raw[raw_index : raw_index + len(candidate)] == candidate
            ),
            None,
        )
        width = len(match) if match is not None else 1
        # Operators cannot occur inside a curated multi-token spelling, so mapping
        # its interior boundaries to the preceding concept is unambiguous.
        for boundary in range(raw_index + 1, raw_index + width):
            boundary_counts[boundary] = canonical_count
        canonical_count += 1
        boundary_counts[raw_index + width] = canonical_count
        raw_index += width
    return matches, boundary_counts


def canonical_grounding_operators(value: str) -> tuple[tuple[int, str], ...]:
    """Return meaning-bearing marks positioned among canonical lexical tokens."""

    if not isinstance(value, str):
        raise GroundingValidationError("grounded wording must be text")
    _validate_grounding_characters(value)
    normalized = _normalize(value)
    token_matches, boundary_counts = _canonical_token_boundaries(value)
    token_starts = [match.start() for match in token_matches]
    token_ends = [match.end() for match in token_matches]
    result: list[tuple[int, str]] = []
    position = 0
    while position < len(normalized):
        raw_index = bisect_right(token_starts, position) - 1
        if raw_index >= 0 and position < token_ends[raw_index]:
            position = token_ends[raw_index]
            continue

        operator = next(
            (
                (literal, canonical)
                for literal, canonical in _MULTI_CHARACTER_OPERATORS
                if normalized.startswith(literal, position)
            ),
            None,
        )
        width = 1
        canonical: str | None = None
        if operator is not None:
            literal, canonical = operator
            width = len(literal)
        else:
            character = normalized[position]
            canonical = _SINGLE_CHARACTER_OPERATORS.get(character)
            category = unicodedata.category(character)
            if category == "Sc":
                canonical = "currency:" + character
            elif character in _RANGE_OR_MINUS:
                # A hyphen can be either word punctuation or a range/minus.  That
                # ambiguity is not safe to guess across a factual trust boundary.
                canonical = "-"
            elif character == ":":
                previous = next(
                    (part for part in reversed(normalized[:position]) if not part.isspace()),
                    "",
                )
                following = next(
                    (part for part in normalized[position + 1 :] if not part.isspace()),
                    "",
                )
                canonical = ":" if previous.isdigit() and following.isdigit() else None
            elif canonical is None and category == "Sm":
                canonical = "math:" + character

        if canonical is not None:
            preceding_raw_tokens = bisect_right(token_ends, position)
            result.append((boundary_counts[preceding_raw_tokens], canonical))
        position += width
    return tuple(result)


def _grounding_operator_contexts(
    value: str,
) -> tuple[tuple[str, str | None, str | None], ...]:
    tokens = canonical_grounding_tokens(value)
    return tuple(
        (
            operator,
            tokens[position - 1] if position > 0 else None,
            tokens[position] if position < len(tokens) else None,
        )
        for position, operator in canonical_grounding_operators(value)
    )


def _contains_sequence(tokens: Sequence[str], candidate: Sequence[str]) -> bool:
    if not candidate or len(candidate) > len(tokens):
        return False
    width = len(candidate)
    return any(
        tuple(tokens[index : index + width]) == tuple(candidate)
        for index in range(len(tokens) - width + 1)
    )


def _term_counter(value: str, terms: Sequence[str]) -> Counter[str]:
    tokens = _tokens(value)
    result: Counter[str] = Counter()
    for term in terms:
        candidate = _tokens(term)
        if not candidate:
            continue
        width = len(candidate)
        result[term] = sum(
            tuple(tokens[index : index + width]) == candidate
            for index in range(max(0, len(tokens) - width + 1))
        )
    return result


def _metric_contexts(
    value: str,
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    """Bind each metric to neighboring conserved concepts in its local clause."""

    tokens = canonical_grounding_tokens(value)
    numeric_positions = [
        index
        for index, token in enumerate(tokens)
        if re.fullmatch(r"\d[\d,.]*", token)
    ]
    contexts = []
    cursor = 0
    for metric in _METRIC.finditer(_normalize(value)):
        core = metric.group(0).casefold().lstrip("$€£").rstrip("%+")
        position = next(
            (
                index
                for index in numeric_positions
                if index >= cursor
                and tokens[index].replace(",", "") == core.replace(",", "")
            ),
            None,
        )
        if position is None:
            raise GroundingValidationError(
                "grounded wording metric context is invalid"
            )
        cursor = position + 1
        previous_metric = max(
            (index for index in numeric_positions if index < position), default=-1
        )
        next_metric = min(
            (index for index in numeric_positions if index > position),
            default=len(tokens),
        )
        before = tuple(
            token
            for token in tokens[previous_metric + 1 : position]
            if token not in _IGNORABLE_ARTICLES
        )[-3:]
        after = tuple(
            token
            for token in tokens[position + 1 : next_metric]
            if token not in _IGNORABLE_ARTICLES
        )[:3]
        contexts.append((metric.group(0).casefold(), before, after))
    return tuple(contexts)


def _is_subsequence(required: Sequence[str], candidate: Sequence[str]) -> bool:
    cursor = iter(candidate)
    return all(any(value == wanted for value in cursor) for wanted in required)


def _source_value(raw: Any) -> tuple[str, str, str | None]:
    if isinstance(raw, ResumeClaim):
        if raw.origin is not ClaimOrigin.USER_ATTESTED:
            raise GroundingValidationError(
                "immutable grounding sources must be user-attested claims"
            )
        claim_id, text, path = raw.claim_id, raw.text, None
    elif isinstance(raw, Mapping):
        claim_id, text, path = raw.get("claim_id"), raw.get("text"), raw.get("path")
        origin = raw.get("origin")
        if isinstance(origin, ClaimOrigin):
            origin = origin.value
        if origin is not None and str(origin) not in {
            ClaimOrigin.USER_ATTESTED.value,
            "source",
        }:
            raise GroundingValidationError(
                "immutable grounding sources must be user-attested claims"
            )
    else:
        raise GroundingValidationError("immutable source claims must be objects")
    try:
        validate_identifier(claim_id, "claim_id")
    except (TypeError, ValueError, ResumeLabError) as exc:
        raise GroundingValidationError("immutable source claim id is invalid") from exc
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > 8_000
        or "\x00" in text
    ):
        raise GroundingValidationError("immutable source claim text is invalid")
    _validate_grounding_characters(text)
    if path is not None and (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 1_000
        or "\x00" in path
    ):
        raise GroundingValidationError("immutable source claim path is invalid")
    return str(claim_id), text, path


def _source_index(source_claims: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    if isinstance(source_claims, (str, bytes)) or not isinstance(
        source_claims, Sequence
    ):
        raise GroundingValidationError("immutable source claims must be an array")
    if not source_claims or len(source_claims) > 500:
        raise GroundingValidationError(
            "immutable source claims must be a non-empty bounded array"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for raw in source_claims:
        claim_id, text, path = _source_value(raw)
        if claim_id in result:
            raise GroundingValidationError("immutable source claim ids must be unique")
        result[claim_id] = {"claim_id": claim_id, "text": text, "path": path}
    return result


def _supported_equivalences(text: str) -> list[str]:
    source_tokens = _tokens(text)
    supported: set[str] = set()
    for group in CURATED_GROUNDING_EQUIVALENCES:
        if any(_contains_sequence(source_tokens, _tokens(term)) for term in group):
            supported.update(term.casefold() for term in group)
    return sorted(supported)


def curated_source_claims(source_claims: Sequence[Any]) -> list[Mapping[str, Any]]:
    """Return model-validator sources derived only from immutable claim text.

    Any aliases present on the input mappings are intentionally ignored.  This makes
    the reviewed module constant, rather than a job, model, or database field, the
    sole authority for vocabulary substitutions.
    """

    source = _source_index(source_claims)
    result = []
    for claim_id, item in source.items():
        value = {
            "claim_id": claim_id,
            "text": item["text"],
            "allowed_equivalent_terms": _supported_equivalences(str(item["text"])),
        }
        if item["path"] is not None:
            value["path"] = item["path"]
        result.append(value)
    return result


def validate_grounded_rewrite_text(
    text: str, source_texts: Sequence[str]
) -> None:
    """Allow useful rewording while conserving every factual vocabulary source.

    The first source is the claim being rewritten. Additional sources may only be
    explicitly permitted skill claims (enforced by the callers). Reviewed equivalent
    spellings, verb forms, articles, and punctuation may change. Conserved concepts
    must remain in source order and none may be dropped; new vocabulary must already
    occur in an explicitly cited source.
    """

    if (
        not isinstance(text, str)
        or not text.strip()
        or isinstance(source_texts, (str, bytes))
        or not isinstance(source_texts, Sequence)
        or not source_texts
        or any(not isinstance(value, str) or not value.strip() for value in source_texts)
    ):
        raise GroundingValidationError("grounded rewrite sources are invalid")
    _validate_grounding_characters(text)
    for value in source_texts:
        _validate_grounding_characters(value)
    primary = source_texts[0]
    if _metric_contexts(text) != _metric_contexts(primary):
        raise GroundingValidationError(
            "grounded wording changed, removed, introduced, or reassociated a metric"
        )
    for terms, label in (
        (_SENIORITY_TERMS, "seniority"),
        (_SCOPE_TERMS, "scope"),
    ):
        available = Counter()
        for source_text in source_texts:
            available.update(_term_counter(source_text, terms))
        if _term_counter(text, terms) - available:
            raise GroundingValidationError(
                f"grounded wording introduced new {label}"
            )
    if _truth_qualifier_positions(text) != _truth_qualifier_positions(primary):
        raise GroundingValidationError(
            "grounded wording changed a negation, qualification, or ownership marker"
        )
    available_tokens: Counter[str] = Counter()
    for source_text in source_texts:
        available_tokens.update(canonical_grounding_tokens(source_text))
    output_tokens = Counter(canonical_grounding_tokens(text))
    unsupported = Counter(
        {
            token: count - available_tokens[token]
            for token, count in output_tokens.items()
            if token not in _IGNORABLE_ARTICLES and count > available_tokens[token]
        }
    )
    if unsupported:
        raise GroundingValidationError(
            "grounded wording introduced substantive lexical tokens without a cited "
            "user-attested source"
        )
    primary_meaningful = tuple(
        token
        for token in canonical_grounding_tokens(primary)
        if token not in _IGNORABLE_ARTICLES
    )
    output_meaningful = tuple(
        token
        for token in canonical_grounding_tokens(text)
        if token not in _IGNORABLE_ARTICLES
    )
    if not _is_subsequence(primary_meaningful, output_meaningful):
        raise GroundingValidationError(
            "grounded wording removed or reordered conserved source relationships"
        )
    if _grounding_operator_contexts(text) != _grounding_operator_contexts(primary):
        raise GroundingValidationError(
            "grounded wording must preserve comparison, math, currency, range, "
            "and other meaning-bearing operators at their source positions"
        )


def validate_generated_wording_claim(
    claim: ResumeClaim, source_claims: Sequence[Any]
) -> None:
    """Prove one persisted generated claim is a conservative source rewrite."""

    if not isinstance(claim, ResumeClaim):
        raise GroundingValidationError(
            "persisted grounded claims must be ResumeClaim values"
        )
    try:
        claim.validate(ResumePurpose.REAL_APPLICATION, grounded=True)
    except ResumeLabError as exc:
        raise GroundingValidationError(str(exc)) from exc
    if claim.origin is not ClaimOrigin.GENERATED_WORDING:
        raise GroundingValidationError(
            "persisted grounded claims must retain generated-wording origin"
        )

    source = _source_index(source_claims)
    if len(set(claim.source_fact_ids)) != len(claim.source_fact_ids):
        raise GroundingValidationError("grounded source claim ids must be unique")
    if not claim.source_fact_ids or any(
        source_id not in source for source_id in claim.source_fact_ids
    ):
        raise GroundingValidationError(
            "grounded wording must cite known immutable source claims"
        )
    primary_path = source[claim.source_fact_ids[0]]["path"]
    support_paths = [
        source[source_id]["path"] for source_id in claim.source_fact_ids[1:]
    ]
    if support_paths and (
        not isinstance(primary_path, str)
        or not (primary_path == "/summary" or primary_path.startswith("/skills/"))
    ):
        raise GroundingValidationError(
            "skill support cannot be projected into historical experience or projects"
        )
    if any(
        not isinstance(path, str) or not path.startswith("/skills/")
        for path in support_paths
    ):
        raise GroundingValidationError(
            "grounded supporting sources must be user-attested skill claims"
        )
    validate_grounded_rewrite_text(
        claim.text,
        [str(source[source_id]["text"]) for source_id in claim.source_fact_ids],
    )


def validate_grounded_artifact_boundary(
    output: Mapping[str, Any],
    base: Mapping[str, Any],
    immutable_source_claims: Sequence[Any],
    generated_claims: Sequence[ResumeClaim],
    artifact_intended_text: str,
    *, template_version: str | None = None,
) -> GroundedArtifactValidation:
    """Validate and bind a complete grounded bundle before real persistence.

    The check is intentionally independent of job requirements: the job may guide a
    model's choice of wording, but it can never become evidence for that wording.
    """

    if not isinstance(base, Mapping):
        raise GroundingValidationError("grounded base standard must be an object")
    curated = curated_source_claims(immutable_source_claims)
    immutable_by_id = _source_index(immutable_source_claims)
    try:
        # Import lazily so the model validator can use the canonicalization helper
        # from this module without creating an import cycle.
        from .model import ResumeModelError, validate_variant_output

        validated = validate_variant_output(
            output,
            "grounded_rewrite",
            base,
            curated,
            (),  # Search/JD aliases are not grounding authority.
        )
        rendered = render_resume_tex(validated["content"], **({"template_version": template_version} if template_version else {}))
        if template_version is None and rendered.intended_text != artifact_intended_text:
            rendered = render_resume_tex(validated["content"], template_version="career-ops-v1")
    except (ContractError, ResumeModelError, ResumeLabError) as exc:
        raise GroundingValidationError(str(exc)) from exc

    if not isinstance(artifact_intended_text, str) or (
        rendered.intended_text != artifact_intended_text
    ):
        raise GroundingValidationError(
            "grounded structured content does not exactly match artifact intended text"
        )
    if isinstance(generated_claims, (str, bytes)) or not isinstance(
        generated_claims, Sequence
    ):
        raise GroundingValidationError("persisted grounded claims must be an array")

    claims = tuple(generated_claims)
    expected = {str(item["claim_id"]): item for item in validated["claims"]}
    if len(claims) != len(expected):
        raise GroundingValidationError(
            "persisted grounded claims differ from generated provenance"
        )
    seen: set[str] = set()
    used_sources: set[str] = set()
    for claim in claims:
        if claim.claim_id in seen or claim.claim_id not in expected:
            raise GroundingValidationError(
                "persisted grounded claim ids differ from generated provenance"
            )
        seen.add(claim.claim_id)
        model_claim = expected[claim.claim_id]
        source_ids = tuple(model_claim["source_claim_ids"])
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise GroundingValidationError(
                "grounded wording must cite unique immutable source claims"
            )
        if claim.text != model_claim["text"] or tuple(
            claim.source_fact_ids
        ) != source_ids:
            raise GroundingValidationError(
                "persisted grounded claims differ from generated provenance"
            )
        validate_generated_wording_claim(claim, immutable_source_claims)
        source_id = source_ids[0]
        source_path = immutable_by_id[source_id]["path"]
        if source_path is None:
            raise GroundingValidationError(
                "grounded source claim is missing immutable normalization path"
            )
        if model_claim["path"] != source_path:
            raise GroundingValidationError(
                "grounded wording must remain at its immutable source path"
            )
        support_paths = [immutable_by_id[value]["path"] for value in source_ids[1:]]
        if support_paths and not (
            source_path == "/summary" or source_path.startswith("/skills/")
        ):
            raise GroundingValidationError(
                "skill support cannot be projected into historical experience or projects"
            )
        if any(
            not isinstance(path, str) or not path.startswith("/skills/")
            for path in support_paths
        ):
            raise GroundingValidationError(
                "grounded supporting sources must be user-attested skill claims"
            )
        if source_id in used_sources:
            raise GroundingValidationError(
                "an immutable source claim may be used by only one grounded slot"
            )
        used_sources.add(source_id)
    if seen != set(expected):
        raise GroundingValidationError(
            "persisted grounded claims differ from generated provenance"
        )

    return GroundedArtifactValidation(
        output=validated,
        claims=claims,
        rendered=rendered,
        intended_text_sha256=sha256_text(rendered.intended_text),
    )


__all__ = [
    "CURATED_GROUNDING_EQUIVALENCES",
    "GROUNDING_EQUIVALENCE_REVISION",
    "GROUNDING_VALIDATOR_REVISION",
    "GroundedArtifactValidation",
    "GroundingValidationError",
    "curated_source_claims",
    "canonical_grounding_operators",
    "canonical_grounding_tokens",
    "validate_generated_wording_claim",
    "validate_grounded_artifact_boundary",
    "validate_grounded_rewrite_text",
]
