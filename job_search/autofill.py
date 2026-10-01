"""Privacy-minimized, one-time browser-extension autofill handoffs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import unquote, urlsplit

from .contracts import ConflictError, ContractError, MutationContext, utc_now, validate_identifier


PROFILE_VERSION = 1
HANDOFF_TTL_SECONDS = 5 * 60
SUBMISSION_TTL_SECONDS = 30 * 60
MAX_FIELDS = 100
MAX_OPTIONS_PER_FIELD = 50
MAX_CAPTURED_ANSWERS = 500
SUPPORTED_ATS = frozenset({"ashby", "greenhouse", "lever"})
PRIVATE_CATEGORIES = frozenset(
    {
        "hispanic_latino",
        "disability",
        "veteran",
        "sponsorship",
        "work_authorization",
        "age_eligibility",
        "sexual_orientation",
        "pronouns",
        "gender_identity",
        "race_ethnicity",
    }
)

CONTACT_FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "email",
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "country",
        "linkedin_url",
        "portfolio_url",
    }
)
WORK_FIELDS = frozenset(
    {
        "work_employer",
        "work_title",
        "work_start_month",
        "work_start_year",
        "work_end_month",
        "work_end_year",
        "work_summary",
    }
)
REQUEST_KINDS = CONTACT_FIELDS | WORK_FIELDS | {"approved_answer", "private_answer"}
CONTROL_TYPES = frozenset({"text", "textarea", "select", "radio_group", "checkbox_group"})

WORK_KEY = {
    "work_employer": "employer",
    "work_title": "title",
    "work_start_month": "start_month",
    "work_start_year": "start_year",
    "work_end_month": "end_month",
    "work_end_year": "end_year",
    "work_summary": "summary",
}

PRIVATE_PATTERN = re.compile(
    r"\b(?:"
    r"race|racial|ethnic|ethnicity|gender|sex|sexual orientation|pronouns?|"
    r"transgender|nonbinary|male|female|18 or older|over 18|hispanic|latino|"
    r"native american|pacific islander|equal employment|equal opportunity|"
    r"\beeo\b|demographic|"
    r"disabilit(?:y|ies)|veteran|military status|protected class|"
    r"work authori[sz]ation|legally authori[sz]ed|"
    r"right to work|eligible to work|employment eligibility|"
    r"sponsorship|sponsor|immigration|visa|citizen|citizenship"
    r")\b",
    re.IGNORECASE,
)

FORBIDDEN_PATTERN = re.compile(
    r"\b(?:"
    r"password|passcode|captcha|salary|base salary|compensation|pay expectation|"
    r"desired pay|expected pay|desired annual|desired base|date of birth|birth date|"
    r"marital status|religion|"
    r"certif(?:y|ication)|attest(?:ation)?|acknowledge|electronic signature|"
    r"signature|i agree|consent|background check|terms and conditions|"
    r"privacy policy|truthful|accurate information|"
    r"upload|resume|curriculum vitae|cover letter|portfolio file|"
    r"submit application|final submit"
    r")\b",
    re.IGNORECASE,
)

ATS_HOSTS = {
    "ashby": ("jobs.ashbyhq.com",),
    "greenhouse": (
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    ),
    "lever": ("jobs.lever.co", "jobs.eu.lever.co"),
}

EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")


def _clean_text(value: Any, field: str, maximum: int = 4000) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be text")
    cleaned = unicodedata.normalize("NFC", value).strip()
    if len(cleaned) > maximum:
        raise ContractError(f"{field} is too long")
    return cleaned


def normalize_prompt(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def is_sensitive_prompt(value: str) -> bool:
    normalized = normalize_prompt(value)
    return bool(
        PRIVATE_PATTERN.search(normalized) or FORBIDDEN_PATTERN.search(normalized)
    )


def is_forbidden_prompt(value: str) -> bool:
    return bool(FORBIDDEN_PATTERN.search(normalize_prompt(value)))


def private_category(value: str) -> Optional[str]:
    """Return a stable private-answer category without exposing ATS wording."""

    normalized = normalize_prompt(value)
    if is_forbidden_prompt(normalized):
        return None
    checks = (
        ("hispanic_latino", r"\b(?:hispanic|latino)\b"),
        ("disability", r"\bdisabilit(?:y|ies)\b"),
        ("veteran", r"\b(?:veteran|military status|protected veteran)\b"),
        ("sponsorship", r"\b(?:sponsorship|sponsor|immigration|visa)\b"),
        ("work_authorization", r"\b(?:work authori[sz]ation|legally authori[sz]ed|right to work|eligible to work|employment eligibility|citizen|citizenship)\b"),
        ("age_eligibility", r"\b(?:18 or older|over 18)\b"),
        ("sexual_orientation", r"\bsexual orientation\b"),
        ("pronouns", r"\bpronouns?\b"),
        ("gender_identity", r"\b(?:gender|sex|transgender|nonbinary|male|female)\b"),
        ("race_ethnicity", r"\b(?:race|racial|ethnic|ethnicity|native american|pacific islander)\b"),
    )
    for category, pattern in checks:
        if re.search(pattern, normalized):
            return category
    return None


@dataclass(frozen=True)
class ApprovedAnswer:
    answer_id: str
    prompt: str
    value: str
    ats: Tuple[str, ...]

    @property
    def normalized_prompt(self) -> str:
        return normalize_prompt(self.prompt)


@dataclass(frozen=True)
class AutofillProfile:
    version: int
    contact: Mapping[str, str]
    work_history: Tuple[Mapping[str, str], ...]
    approved_answers: Tuple[ApprovedAnswer, ...]

    @classmethod
    def empty(cls) -> "AutofillProfile":
        return cls(PROFILE_VERSION, {}, (), ())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AutofillProfile":
        if set(value) - {"version", "contact", "work_history", "approved_answers"}:
            raise ContractError("autofill profile contains unknown top-level fields")
        if value.get("version") != PROFILE_VERSION:
            raise ContractError(f"autofill profile version must be {PROFILE_VERSION}")
        raw_contact = value.get("contact", {})
        if not isinstance(raw_contact, Mapping):
            raise ContractError("contact must be an object")
        unknown_contact = set(raw_contact) - CONTACT_FIELDS
        if unknown_contact:
            raise ContractError("contact contains unsupported fields")
        contact = {
            str(name): _clean_text(item, f"contact.{name}", 1000)
            for name, item in raw_contact.items()
            if _clean_text(item, f"contact.{name}", 1000)
        }

        raw_history = value.get("work_history", [])
        if not isinstance(raw_history, list) or len(raw_history) > 10:
            raise ContractError("work_history must contain at most ten entries")
        history = []
        allowed_history = frozenset(WORK_KEY.values())
        for index, item in enumerate(raw_history):
            if not isinstance(item, Mapping) or set(item) - allowed_history:
                raise ContractError("work history entry contains unsupported fields")
            history.append(
                {
                    str(name): _clean_text(raw, f"work_history[{index}].{name}")
                    for name, raw in item.items()
                    if _clean_text(raw, f"work_history[{index}].{name}")
                }
            )

        raw_answers = value.get("approved_answers", [])
        if not isinstance(raw_answers, list) or len(raw_answers) > 100:
            raise ContractError("approved_answers must contain at most one hundred entries")
        answers = []
        seen_prompts = set()
        for index, item in enumerate(raw_answers):
            if not isinstance(item, Mapping):
                raise ContractError("approved answer must be an object")
            if set(item) - {"answer_id", "prompt", "value", "ats"}:
                raise ContractError("approved answer contains unsupported fields")
            answer_id = _clean_text(item.get("answer_id"), "answer_id", 256)
            validate_identifier(answer_id, "answer_id")
            prompt = _clean_text(item.get("prompt"), "approved answer prompt", 500)
            answer = _clean_text(item.get("value"), "approved answer value")
            if not prompt or not answer:
                raise ContractError("approved answers require prompt and value")
            if is_sensitive_prompt(prompt):
                raise ContractError("sensitive prompts cannot be approved for autofill")
            raw_ats = item.get("ats", sorted(SUPPORTED_ATS))
            if not isinstance(raw_ats, list) or not raw_ats:
                raise ContractError("approved answer ATS scope must be a nonempty list")
            ats = tuple(sorted(set(str(name).strip().lower() for name in raw_ats)))
            if any(name not in SUPPORTED_ATS for name in ats):
                raise ContractError("approved answer has an unsupported ATS scope")
            normalized = normalize_prompt(prompt)
            key = (normalized, ats)
            if key in seen_prompts:
                raise ContractError("approved answer prompt and ATS scope must be unique")
            seen_prompts.add(key)
            answers.append(ApprovedAnswer(answer_id, prompt, answer, ats))
        return cls(PROFILE_VERSION, contact, tuple(history), tuple(answers))

    def assignments(
        self, ats: str, descriptors: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, str]]:
        if ats not in SUPPORTED_ATS:
            raise ContractError("unsupported ATS")
        fields = validate_descriptors(descriptors)
        approved = {
            answer.normalized_prompt: answer.value
            for answer in self.approved_answers
            if ats in answer.ats
        }
        result = []
        for field in fields:
            prompt = field["prompt"]
            if is_sensitive_prompt(prompt):
                continue
            kind = field["kind"]
            value = ""
            if kind in CONTACT_FIELDS:
                value = self.contact.get(kind, "")
            elif kind in WORK_FIELDS:
                index = field["history_index"]
                if index is not None and index < len(self.work_history):
                    value = self.work_history[index].get(WORK_KEY[kind], "")
            elif kind == "approved_answer":
                value = approved.get(normalize_prompt(prompt), "")
            if value:
                result.append({"field_id": field["field_id"], "value": value})
        return result


class AutofillVault(Protocol):
    def assignments(
        self, ats: str, descriptors: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, Any]]: ...

    def capture(
        self,
        application_id: str,
        ats: str,
        descriptors: Sequence[Mapping[str, Any]],
        answers: Sequence[Mapping[str, Any]],
        captured_at: str,
    ) -> Mapping[str, int]: ...


class EmptyAutofillVault:
    """Disabled private persistence while retaining the existing safe profile."""

    def assignments(
        self, ats: str, descriptors: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, Any]]:
        del ats, descriptors
        return ()

    def capture(
        self,
        application_id: str,
        ats: str,
        descriptors: Sequence[Mapping[str, Any]],
        answers: Sequence[Mapping[str, Any]],
        captured_at: str,
    ) -> Mapping[str, int]:
        del application_id, ats, descriptors, answers, captured_at
        return {"private_answers": 0, "custom_answers": 0}


def default_autofill_vault_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "job-search" / "autofill-vault.bin"


def _declined(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:decline|prefer not|do not wish|dont wish|choose not|not disclose)\b",
            normalize_prompt(value),
        )
    )


def _yes_no(value: str) -> Optional[str]:
    normalized = normalize_prompt(value)
    if _declined(normalized):
        return "decline"
    if re.search(r"\b(?:no|not|without|dont|do not|will not)\b", normalized):
        return "no"
    if re.search(r"\b(?:yes|have|am|will|require|with)\b", normalized):
        return "yes"
    return None


def canonical_private_option(category: str, value: str) -> str:
    """Normalize a displayed ATS choice for conservative cross-platform matching."""

    normalized = normalize_prompt(value)
    if not normalized:
        return ""
    if _declined(normalized):
        return "decline"
    if category in {
        "hispanic_latino",
        "disability",
        "work_authorization",
        "sponsorship",
        "age_eligibility",
    }:
        result = _yes_no(normalized)
        return result or "label:" + normalized
    if category == "veteran":
        if re.search(r"\bnot (?:a )?protected veteran\b", normalized):
            return "not_protected"
        if re.search(r"\bprotected veteran\b", normalized):
            return "protected"
        result = _yes_no(normalized)
        return result or "label:" + normalized
    if category == "race_ethnicity":
        checks = (
            ("american_indian_alaska_native", r"\b(?:american indian|alaska native)\b"),
            ("asian", r"\basian\b"),
            ("black_african_american", r"\b(?:black|african american)\b"),
            ("native_hawaiian_pacific_islander", r"\b(?:native hawaiian|pacific islander)\b"),
            ("white", r"\bwhite\b"),
            ("two_or_more", r"\b(?:two or more|multiracial|multi racial)\b"),
        )
    elif category == "gender_identity":
        checks = (
            ("non_binary", r"\b(?:nonbinary|non binary|genderqueer|gender non conforming)\b"),
            ("woman", r"\b(?:woman|female)\b"),
            ("man", r"\b(?:man|male)\b"),
            ("self_describe", r"\b(?:self describe|self identify)\b"),
        )
    elif category == "sexual_orientation":
        checks = (
            ("heterosexual", r"\b(?:heterosexual|straight)\b"),
            ("gay_lesbian", r"\b(?:gay|lesbian)\b"),
            ("bisexual", r"\bbisexual\b"),
            ("asexual", r"\basexual\b"),
            ("queer", r"\bqueer\b"),
            ("self_describe", r"\b(?:self describe|self identify)\b"),
        )
    else:
        return "label:" + normalized
    for token, pattern in checks:
        if re.search(pattern, normalized):
            return token
    return "label:" + normalized


class EncryptedAutofillVault:
    """Keychain-backed encrypted persistence for private and learned form values."""

    VERSION = 1

    def __init__(
        self,
        path: Path,
        *,
        persistence: Any = None,
        extensions_module: Any = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if persistence is None:
            if extensions_module is None:
                try:
                    import msal_extensions as extensions_module  # type: ignore[import-not-found]
                except ImportError as exc:
                    raise ContractError(
                        "encrypted autofill requires msal-extensions"
                    ) from exc
            try:
                persistence = extensions_module.build_encrypted_persistence(
                    str(self.path)
                )
            except Exception as exc:
                raise ContractError(
                    "encrypted autofill persistence is unavailable"
                ) from exc
        if not bool(getattr(persistence, "is_encrypted", False)):
            raise ContractError("refusing plaintext autofill persistence")
        self._persistence = persistence
        self._lock = threading.Lock()

    @classmethod
    def _empty(cls) -> Dict[str, Any]:
        return {"version": cls.VERSION, "private_answers": {}, "custom_history": []}

    def _load(self, *, require_existing: bool = False) -> Dict[str, Any]:
        try:
            raw = self._persistence.load()
        except Exception as exc:
            persistence_missing = type(exc).__name__ in {
                "PersistenceNotFound",
                "FileNotFoundError",
            }
            if require_existing:
                if persistence_missing:
                    raise ContractError(
                        "source encrypted autofill vault does not exist"
                    ) from exc
                raise ContractError(
                    "source encrypted autofill vault could not be read"
                ) from exc
            if not self.path.exists() or persistence_missing:
                return self._empty()
            raise ContractError("encrypted autofill vault could not be read") from exc
        if not raw:
            if require_existing:
                raise ContractError(
                    "source encrypted autofill vault does not contain initialized state"
                )
            return self._empty()
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError("encrypted autofill vault is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value) - {"version", "private_answers", "custom_history", "pending_captures"}
            or not {"version", "private_answers", "custom_history"}.issubset(value)
            or value.get("version") != self.VERSION
            or not isinstance(value.get("private_answers"), dict)
            or not isinstance(value.get("custom_history"), list)
        ):
            raise ContractError("encrypted autofill vault schema is invalid")
        for category, answer in value["private_answers"].items():
            if (
                category not in PRIVATE_CATEGORIES
                or not isinstance(answer, dict)
                or set(answer) != {"values", "representation", "updated_at"}
                or answer.get("representation") not in {"text", "options"}
                or not isinstance(answer.get("updated_at"), str)
                or not isinstance(answer.get("values"), list)
                or not 1 <= len(answer["values"]) <= MAX_OPTIONS_PER_FIELD
                or any(
                    not isinstance(item, str) or not item or len(item) > 4000
                    for item in answer["values"]
                )
            ):
                raise ContractError("encrypted autofill vault schema is invalid")
            if answer["representation"] == "text" and len(answer["values"]) != 1:
                raise ContractError("encrypted autofill vault schema is invalid")
        required_history = {
            "capture_key",
            "application_id",
            "ats",
            "prompt",
            "normalized_prompt",
            "control",
            "value",
            "captured_at",
        }
        if len(value["custom_history"]) > MAX_CAPTURED_ANSWERS:
            raise ContractError("encrypted autofill vault schema is invalid")
        for item in value["custom_history"]:
            if (
                not isinstance(item, dict)
                or set(item) != required_history
                or not all(
                    isinstance(item[name], str)
                    for name in required_history - {"value"}
                )
                or not isinstance(item["value"], (str, list))
                or (
                    isinstance(item["value"], list)
                    and any(not isinstance(entry, str) for entry in item["value"])
                )
            ):
                raise ContractError("encrypted autofill vault schema is invalid")
        return value

    def _save(self, value: Mapping[str, Any]) -> None:
        try:
            self._persistence.save(
                json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            )
        except Exception as exc:
            raise ContractError("encrypted autofill vault could not be saved") from exc

    def stage_browser_capture(self, attempt_id, application_id, ats, fields, answers):
        with self._lock:
            value = self._load()
            pending = value.setdefault("pending_captures", {})
            now = time.time()
            pending = {k: v for k, v in pending.items() if v["expires_at"] > now}
            if len(pending) >= 100 and attempt_id not in pending:
                raise ContractError("too many unconfirmed form captures")
            pending[attempt_id] = {"application_id": application_id, "ats": ats,
                "fields": fields, "answers": answers, "captured_at": utc_now(),
                "expires_at": now + 86400}
            value["pending_captures"] = pending
            self._save(value)

    def finish_browser_captures(self, confirmed_application_ids):
        with self._lock:
            value = self._load()
            pending = dict(value.get("pending_captures", {}))
        for key, item in pending.items():
            expired = item["expires_at"] <= time.time()
            if not expired and item["application_id"] in confirmed_application_ids:
                self.capture(item["application_id"], item["ats"], item["fields"], item["answers"], item["captured_at"])
            elif not expired:
                continue
            with self._lock:
                value = self._load()
                if value.get("pending_captures", {}).get(key) == item:
                    del value["pending_captures"][key]
                    self._save(value)

    def copy_encrypted_state_to(
        self, destination: "EncryptedAutofillVault"
    ) -> Mapping[str, int]:
        """Validate and copy state to another encrypted persistence boundary.

        Plaintext remains an in-process implementation detail and is never returned.
        The source persistence is read-only; the destination is read back and compared
        before this method reports success.
        """

        if destination is self or not isinstance(destination, EncryptedAutofillVault):
            raise ContractError("autofill export destination is invalid")
        with self._lock:
            value = self._load(require_existing=True)
        with destination._lock:
            destination._save(value)
            verified = destination._load()
        if verified != value:
            raise ContractError("portable autofill vault verification failed")
        return {
            "private_answers": len(value["private_answers"]),
            "custom_history": len(value["custom_history"]),
        }

    def assignments(
        self, ats: str, descriptors: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, Any]]:
        if ats not in SUPPORTED_ATS:
            raise ContractError("unsupported ATS")
        fields = validate_descriptors(descriptors)
        with self._lock:
            private = self._load()["private_answers"]
        result = []
        for field in fields:
            if field["kind"] != "private_answer":
                continue
            category = private_category(str(field["prompt"]))
            saved = private.get(category or "")
            if not category or not isinstance(saved, Mapping):
                continue
            values = saved.get("values")
            if not isinstance(values, list) or not values:
                continue
            if field["control"] in {"text", "textarea"}:
                if saved.get("representation") == "text" and len(values) == 1:
                    result.append({"field_id": field["field_id"], "value": values[0]})
                continue
            if saved.get("representation") != "options":
                continue
            selected = []
            for desired in values:
                matches = [
                    option["option_id"]
                    for option in field["options"]
                    if canonical_private_option(category, option["label"]) == desired
                ]
                if len(matches) != 1:
                    selected = []
                    break
                selected.append(matches[0])
            if selected:
                result.append({"field_id": field["field_id"], "option_ids": selected})
        return result

    def capture(
        self,
        application_id: str,
        ats: str,
        descriptors: Sequence[Mapping[str, Any]],
        answers: Sequence[Mapping[str, Any]],
        captured_at: str,
    ) -> Mapping[str, int]:
        validate_identifier(application_id, "application_id")
        fields = {item["field_id"]: item for item in validate_descriptors(descriptors)}
        captured = validate_captured_answers(answers, fields)
        private_count = custom_count = 0
        with self._lock:
            value = self._load()
            history = list(value["custom_history"])
            known_history = {str(item.get("capture_key")) for item in history}
            for answer in captured:
                field = fields[answer["field_id"]]
                if field["kind"] == "private_answer":
                    category = private_category(str(field["prompt"]))
                    if not category:
                        continue
                    if "value" in answer:
                        values = [str(answer["value"])]
                        representation = "text"
                    else:
                        selected = set(answer["option_ids"])
                        values = [
                            canonical_private_option(category, option["label"])
                            for option in field["options"]
                            if option["option_id"] in selected
                        ]
                        representation = "options"
                    values = [item for item in dict.fromkeys(values) if item]
                    if len(values) > 1 and "decline" in values:
                        continue
                    if values:
                        value["private_answers"][category] = {
                            "values": values,
                            "representation": representation,
                            "updated_at": captured_at,
                        }
                        private_count += 1
                    continue
                if field["kind"] != "approved_answer":
                    continue
                if "value" in answer:
                    rendered: Any = answer["value"]
                else:
                    selected = set(answer["option_ids"])
                    rendered = [
                        option["label"]
                        for option in field["options"]
                        if option["option_id"] in selected
                    ]
                capture_key = hashlib.sha256(
                    json.dumps(
                        [application_id, ats, normalize_prompt(field["prompt"]), rendered],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if capture_key in known_history:
                    continue
                history.append(
                    {
                        "capture_key": capture_key,
                        "application_id": application_id,
                        "ats": ats,
                        "prompt": field["prompt"],
                        "normalized_prompt": normalize_prompt(field["prompt"]),
                        "control": field["control"],
                        "value": rendered,
                        "captured_at": captured_at,
                    }
                )
                known_history.add(capture_key)
                custom_count += 1
            value["custom_history"] = history[-MAX_CAPTURED_ANSWERS:]
            self._save(value)
        return {"private_answers": private_count, "custom_answers": custom_count}


def load_profile(path: Optional[Path]) -> AutofillProfile:
    if path is None:
        return AutofillProfile.empty()
    resolved = Path(path)
    mode = os.stat(resolved).st_mode & 0o777
    if mode & 0o077:
        raise ContractError("autofill profile must have mode 0600")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("autofill profile is not valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise ContractError("autofill profile must be a JSON object")
    return AutofillProfile.from_mapping(value)


def validate_extension_origin(origin: str) -> str:
    normalized = str(origin or "").strip().lower()
    if not EXTENSION_ORIGIN.fullmatch(normalized):
        raise ContractError("invalid Chromium extension origin")
    return normalized


def validate_ats_page(
    ats: str, page_url: str, expected_job_id: Optional[str] = None
) -> str:
    if ats not in SUPPORTED_ATS:
        raise ContractError("unsupported ATS")
    parsed = urlsplit(str(page_url))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ContractError("ATS page must use HTTPS")
    hostname = parsed.hostname.lower()
    if hostname not in ATS_HOSTS[ats]:
        raise ContractError("ATS page host does not match the application")
    if expected_job_id:
        path_segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
        if str(expected_job_id) not in path_segments:
            raise ContractError("ATS page does not match the application job ID")
    return hostname


def validate_descriptors(
    values: Sequence[Mapping[str, Any]],
) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(values, (list, tuple)) or len(values) > MAX_FIELDS:
        raise ContractError(f"form may request at most {MAX_FIELDS} fields")
    result = []
    seen = set()
    for item in values:
        if not isinstance(item, Mapping):
            raise ContractError("field descriptor must be an object")
        if set(item) - {
            "field_id", "kind", "prompt", "history_index", "control", "options"
        }:
            raise ContractError("field descriptor contains unsupported properties")
        field_id = _clean_text(item.get("field_id"), "field_id", 256)
        validate_identifier(field_id, "field_id")
        if field_id in seen:
            raise ContractError("field IDs must be unique")
        seen.add(field_id)
        kind = str(item.get("kind") or "")
        if kind not in REQUEST_KINDS:
            raise ContractError("field descriptor kind is not autofill-safe")
        prompt = _clean_text(item.get("prompt", ""), "field prompt", 500)
        if is_forbidden_prompt(prompt):
            raise ContractError("field descriptor targets a forbidden prompt")
        category = private_category(prompt)
        if kind == "private_answer" and category is None:
            raise ContractError("private field descriptor has no supported category")
        if kind != "private_answer" and category is not None:
            raise ContractError("private prompt must use the private field kind")
        control = str(item.get("control") or "text")
        if control not in CONTROL_TYPES:
            raise ContractError("field descriptor control is unsupported")
        raw_options = item.get("options", [])
        if not isinstance(raw_options, list) or len(raw_options) > MAX_OPTIONS_PER_FIELD:
            raise ContractError("field options must be a bounded array")
        options = []
        option_ids = set()
        for option in raw_options:
            if not isinstance(option, Mapping) or set(option) != {"option_id", "label"}:
                raise ContractError("field option must contain option_id and label")
            option_id = _clean_text(option.get("option_id"), "option_id", 256)
            validate_identifier(option_id, "option_id")
            label = _clean_text(option.get("label"), "option label", 500)
            if option_id in option_ids or not label:
                raise ContractError("field options must be unique and labeled")
            option_ids.add(option_id)
            options.append({"option_id": option_id, "label": label})
        option_control = control in {"select", "radio_group", "checkbox_group"}
        if option_control != bool(options):
            raise ContractError("option controls require options and text controls forbid them")
        raw_index = item.get("history_index")
        if kind in WORK_FIELDS:
            if isinstance(raw_index, bool):
                raise ContractError("history_index must be an integer")
            try:
                history_index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise ContractError("work field requires history_index") from exc
            if not 0 <= history_index < 10:
                raise ContractError("history_index must be between zero and nine")
        elif raw_index is not None:
            raise ContractError("only work fields may set history_index")
        else:
            history_index = None
        result.append(
            {
                "field_id": field_id,
                "kind": kind,
                "prompt": prompt,
                "history_index": history_index,
                "control": control,
                "options": options,
            }
        )
    return tuple(result)


def validate_captured_answers(
    values: Sequence[Mapping[str, Any]],
    fields: Mapping[str, Mapping[str, Any]],
) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(values, (list, tuple)) or len(values) > MAX_FIELDS:
        raise ContractError(f"capture may contain at most {MAX_FIELDS} answers")
    result = []
    seen = set()
    for item in values:
        if not isinstance(item, Mapping) or set(item) - {
            "field_id", "value", "option_ids"
        }:
            raise ContractError("captured answer has unsupported properties")
        field_id = _clean_text(item.get("field_id"), "field_id", 256)
        if field_id in seen or field_id not in fields:
            raise ContractError("captured answer field is unknown or duplicated")
        seen.add(field_id)
        field = fields[field_id]
        has_value = "value" in item
        has_options = "option_ids" in item
        if has_value == has_options:
            raise ContractError("captured answer must contain one value representation")
        if field["control"] in {"text", "textarea"}:
            if not has_value:
                raise ContractError("text capture requires a value")
            captured = _clean_text(item.get("value"), "captured value")
            if captured:
                result.append({"field_id": field_id, "value": captured})
            continue
        if not has_options or not isinstance(item.get("option_ids"), list):
            raise ContractError("option capture requires option_ids")
        selected = []
        allowed = {option["option_id"] for option in field["options"]}
        for raw in item["option_ids"]:
            option_id = _clean_text(raw, "option_id", 256)
            if option_id not in allowed or option_id in selected:
                raise ContractError("captured option is unknown or duplicated")
            selected.append(option_id)
        if field["control"] != "checkbox_group" and len(selected) > 1:
            raise ContractError("single-choice field captured multiple options")
        if selected:
            result.append({"field_id": field_id, "option_ids": selected})
    return tuple(result)


@dataclass
class _Handoff:
    handoff_id: str
    application_id: str
    ats: str
    job_id: str
    code: str
    code_sha256: str
    issue_key: Tuple[str, str]
    expires_at: float


@dataclass
class _SubmissionReceipt:
    application_id: str
    ats: str
    job_id: str
    extension_origin: str
    handoff_id: str
    descriptors: Tuple[Mapping[str, Any], ...]
    expires_at: float
    staged_answers: Tuple[Mapping[str, Any], ...] = ()
    capture_result: Optional[Mapping[str, int]] = None
    used_idempotency_key: str = ""
    used_resume_decision: str = ""
    submitted_at: str = ""
    response: Optional[Mapping[str, Any]] = None


class AutofillBroker:
    """Process-local broker; raw profile data never crosses this interface."""

    def __init__(
        self,
        ledger: Any,
        profile: AutofillProfile,
        vault: Optional[AutofillVault] = None,
        clock: Callable[[], float] = time.monotonic,
        submission_context: Optional[
            Callable[[str, str], Mapping[str, Any]]
        ] = None,
    ) -> None:
        self._ledger = ledger
        self._profile = profile
        self._vault = vault or EmptyAutofillVault()
        self._clock = clock
        self._submission_context = submission_context
        self._lock = threading.Lock()
        self._handoffs: Dict[str, _Handoff] = {}
        self._issue_keys: Dict[Tuple[str, str], str] = {}
        self._receipts: Dict[str, _SubmissionReceipt] = {}

    @staticmethod
    def _sha(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        expired_codes = [key for key, value in self._handoffs.items() if value.expires_at <= now]
        for key in expired_codes:
            handoff = self._handoffs.pop(key)
            self._issue_keys.pop(handoff.issue_key, None)
        for key in [key for key, value in self._receipts.items() if value.expires_at <= now]:
            self._receipts.pop(key, None)

    def issue(
        self,
        application_id: str,
        browser_session_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        validate_identifier(application_id, "application_id")
        validate_identifier(browser_session_id, "browser_session_id")
        validate_identifier(idempotency_key, "idempotency_key")
        timeline = self._ledger.get_application_timeline(application_id)
        application = timeline["application"]
        if application["current_phase"] == "terminal":
            raise ConflictError("terminal applications cannot create autofill handoffs")
        ats = str(application["ats"])
        if ats not in SUPPORTED_ATS:
            raise ContractError("application ATS is not supported by the extension")
        now = self._clock()
        issue_key = (browser_session_id, idempotency_key)
        with self._lock:
            self._prune(now)
            prior_hash = self._issue_keys.get(issue_key)
            if prior_hash:
                prior = self._handoffs[prior_hash]
                if prior.application_id != application_id:
                    raise ConflictError("handoff idempotency key belongs to another application")
                return self._issued_response(prior, application)
            code = secrets.token_urlsafe(32)
            code_hash = self._sha(code)
            handoff = _Handoff(
                handoff_id=uuid.uuid4().hex,
                application_id=application_id,
                ats=ats,
                job_id=str(application["job_id"]),
                code=code,
                code_sha256=code_hash,
                issue_key=issue_key,
                expires_at=now + HANDOFF_TTL_SECONDS,
            )
            self._handoffs[code_hash] = handoff
            self._issue_keys[issue_key] = code_hash
            return self._issued_response(handoff, application)

    def _issued_response(
        self, handoff: _Handoff, application: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {
            "handoff_id": handoff.handoff_id,
            "pairing_code": handoff.code,
            "expires_in_seconds": max(0, int(handoff.expires_at - self._clock())),
            "application": {
                "application_id": handoff.application_id,
                "ats": handoff.ats,
                "title": application["title_snapshot"],
                "employer": application["employer_snapshot"],
            },
        }

    def exchange(
        self,
        pairing_code: str,
        extension_origin: str,
        ats: str,
        page_url: str,
        descriptors: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        origin = validate_extension_origin(extension_origin)
        ats = str(ats or "").strip().lower()
        fields = validate_descriptors(descriptors)
        code_hash = self._sha(str(pairing_code or ""))
        now = self._clock()
        with self._lock:
            self._prune(now)
            handoff = self._handoffs.get(code_hash)
            if handoff is None or not secrets.compare_digest(
                handoff.code_sha256, code_hash
            ):
                raise ContractError("pairing code is invalid or expired")
            if ats != handoff.ats:
                raise ContractError("pairing code is scoped to a different ATS")
            validate_ats_page(ats, page_url, handoff.job_id)
            assignments = [
                *self._profile.assignments(ats, fields),
                *self._vault.assignments(ats, fields),
            ]
            self._handoffs.pop(code_hash)
            self._issue_keys.pop(handoff.issue_key, None)
            submission_token = secrets.token_urlsafe(32)
            receipt_hash = self._sha(submission_token)
            self._receipts[receipt_hash] = _SubmissionReceipt(
                application_id=handoff.application_id,
                ats=handoff.ats,
                job_id=handoff.job_id,
                extension_origin=origin,
                handoff_id=handoff.handoff_id,
                descriptors=fields,
                expires_at=now + SUBMISSION_TTL_SECONDS,
            )
        application = self._ledger.get_application_timeline(handoff.application_id)[
            "application"
        ]
        return {
            "profile_version": self._profile.version,
            "application": {
                "application_id": handoff.application_id,
                "ats": handoff.ats,
                "job_id": handoff.job_id,
                "title": application["title_snapshot"],
                "employer": application["employer_snapshot"],
            },
            "resume": self._resume_handoff_status(handoff.application_id),
            "assignments": assignments,
            "submission_token": submission_token,
            "submission_expires_in_seconds": SUBMISSION_TTL_SECONDS,
        }

    def _resume_handoff_status(self, application_id: str) -> Mapping[str, Any]:
        """Return a content-free hint for the extension's explicit submit choice."""

        if self._submission_context is None:
            return {"status": "unavailable"}
        try:
            context = self._submission_context(application_id, "automatic")
        except Exception:
            return {"status": "unavailable"}
        if not isinstance(context, Mapping):
            return {"status": "unavailable"}
        resume = context.get("resume")
        if not isinstance(resume, Mapping):
            return {"status": "unavailable"}
        decision = resume.get("decision")
        if decision == "not_tracked":
            return {"status": "not_selected"}
        if decision != "selected":
            return {"status": "unavailable"}
        result: dict[str, Any] = {"status": "selected"}
        name = resume.get("name")
        if isinstance(name, str) and name:
            result["name"] = name[:500]
        comparison_kind = resume.get("comparison_kind")
        if comparison_kind in {"standard", "grounded_rewrite"}:
            result["comparison_kind"] = comparison_kind
        return result

    def stage_capture(
        self,
        submission_token: str,
        extension_origin: str,
        ats: str,
        page_url: str,
        answers: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Stage a bounded final-form snapshot in memory until manual marking."""

        origin = validate_extension_origin(extension_origin)
        receipt_hash = self._sha(str(submission_token or ""))
        now = self._clock()
        with self._lock:
            self._prune(now)
            receipt = self._receipts.get(receipt_hash)
            if receipt is None:
                raise ContractError("submission token is invalid or expired")
            if not secrets.compare_digest(receipt.extension_origin, origin):
                raise ContractError("submission token belongs to another extension")
            if str(ats or "").strip().lower() != receipt.ats:
                raise ContractError("submission token belongs to another ATS")
            validate_ats_page(receipt.ats, page_url, receipt.job_id)
            if receipt.used_idempotency_key or receipt.response is not None:
                raise ConflictError("submission token has already been used")
            fields = {item["field_id"]: item for item in receipt.descriptors}
            captured = validate_captured_answers(answers, fields)
            receipt.staged_answers = captured
            return {"staged": True, "answer_count": len(captured)}

    def mark_submitted(
        self,
        submission_token: str,
        extension_origin: str,
        ats: str,
        page_url: str,
        idempotency_key: str,
        resume_decision: str,
    ) -> Mapping[str, Any]:
        origin = validate_extension_origin(extension_origin)
        validate_identifier(idempotency_key, "idempotency_key")
        if resume_decision not in {"selected", "not_tracked"}:
            raise ContractError("resume_decision must be selected or not_tracked")
        receipt_hash = self._sha(str(submission_token or ""))
        now = self._clock()
        with self._lock:
            self._prune(now)
            receipt = self._receipts.get(receipt_hash)
            if receipt is None:
                raise ContractError("submission token is invalid or expired")
            if not secrets.compare_digest(receipt.extension_origin, origin):
                raise ContractError("submission token belongs to another extension")
            if str(ats or "").strip().lower() != receipt.ats:
                raise ContractError("submission token belongs to another ATS")
            validate_ats_page(receipt.ats, page_url, receipt.job_id)
            if receipt.response is not None:
                if (
                    receipt.used_idempotency_key != idempotency_key
                    or receipt.used_resume_decision != resume_decision
                ):
                    raise ConflictError("submission token has already been used")
                return receipt.response
            if (
                receipt.used_idempotency_key
                and (
                    receipt.used_idempotency_key != idempotency_key
                    or receipt.used_resume_decision != resume_decision
                )
            ):
                raise ConflictError("submission token has already been used")
            if not receipt.used_idempotency_key:
                receipt.used_idempotency_key = idempotency_key
                receipt.used_resume_decision = resume_decision
                receipt.submitted_at = utc_now()
            if receipt.capture_result is None:
                receipt.capture_result = self._vault.capture(
                    receipt.application_id,
                    receipt.ats,
                    receipt.descriptors,
                    receipt.staged_answers,
                    receipt.submitted_at,
                )
                receipt.staged_answers = ()
            payload: dict[str, Any] = {"observed_by": "manual_extension_action"}
            submission_options: dict[str, Any] = {
                "request_payload": {
                    "observed_by": "manual_extension_action",
                    "resume_decision": resume_decision,
                }
            }
            if resume_decision == "selected":
                if self._submission_context is None:
                    raise ConflictError("selected resume tracking is unavailable")

                def payload_factory() -> Mapping[str, Any]:
                    context = self._submission_context(
                        receipt.application_id, "selected"
                    )
                    if not isinstance(context, Mapping):
                        raise ContractError("submission context is invalid")
                    resume = context.get("resume")
                    if (
                        not isinstance(resume, Mapping)
                        or resume.get("decision") != "selected"
                    ):
                        raise ContractError("selected resume context is invalid")
                    return {**dict(context), **payload}

                submission_options["payload_factory"] = payload_factory
            else:
                payload["resume"] = {"decision": "not_tracked"}
            result = self._ledger.record_submission(
                receipt.application_id,
                receipt.submitted_at,
                MutationContext(
                    idempotency_key="extension:"
                    + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
                    actor_kind="user",
                    source_kind="browser_extension",
                    source_ref=receipt.handoff_id,
                ),
                None if resume_decision == "selected" else payload,
                **submission_options,
            )
            receipt.response = {**result, "autofill_capture": receipt.capture_result}
            return receipt.response
