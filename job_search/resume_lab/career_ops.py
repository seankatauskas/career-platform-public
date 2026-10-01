"""Small MIT-licensed Career Ops adaptations used by the resume toolchain.

Source: Career Ops v1.30.0, commit
``1c866a9761f141bf341b10fadb38d1e5dc4c3b7d``. The full notice is in
``CAREER_OPS_MIT.txt``. This is a Python adaptation rather than a runtime
dependency on the upstream Node project.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


UPSTREAM_COMMIT = "1c866a9761f141bf341b10fadb38d1e5dc4c3b7d"
TEMPLATE_PATH = Path(__file__).with_name("career_ops_template.tex")

_EMOJI = re.compile(
    "[\U0001f300-\U0001f9ff\u2600-\u26ff\u2700-\u27bf]", re.UNICODE
)
_BULLETS = re.compile("[\u2022\u2023\u25b6\u25c6\u25ca\u25e6\u25aa\u25ab]")


def escape_latex(value: Any) -> str:
    """Escape one scalar for insertion in a TeX text argument.

    The character-by-character behavior follows Career Ops' ``escapeLatex``.
    Objects and collections are rejected instead of stringified into a resume.
    """

    if value is None:
        return ""
    if isinstance(value, (Mapping, Sequence)) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return ""
    if isinstance(value, (bytes, bytearray)):
        return ""
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "^": r"\textasciicircum{}",
        "~": r"\textasciitilde{}",
        "_": r"\_",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "<": r"\textless{}",
        ">": r"\textgreater{}",
        "|": r"\textbar{}",
        "±": r"$\pm$",
        "→": r"$\rightarrow$",
    }
    return "".join(replacements.get(character, character) for character in str(value))


def sanitize_url(value: Any) -> str:
    """Return an HTTP(S)/mailto URL safe for a ``\\href`` argument."""

    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    if "@" in candidate and "/" not in candidate and ":" not in candidate:
        candidate = "mailto:" + candidate
    elif ":" not in candidate.split("/", 1)[0]:
        candidate = "https://" + candidate
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() not in {"http", "https", "mailto"}:
        return ""
    if parsed.scheme.casefold() in {"http", "https"} and not not_missing_hostname(parsed):
        return ""
    if any(character in candidate for character in "{}%$#\\~^\r\n\x00"):
        return ""
    return candidate


def not_missing_hostname(parsed: Any) -> bool:
    """Small helper kept separate for focused URL tests."""

    try:
        return bool(parsed.hostname)
    except ValueError:
        return False


# A named alias keeps the URL condition readable without accepting malformed hosts.
not_missing_hostname.__name__ = "not_missing_hostname"


def sanitize_ats_text(value: Any) -> str:
    """Normalize visible text using Career Ops' ATS plain-text rules."""

    if not isinstance(value, str):
        return ""
    result = _BULLETS.sub("-", value)
    result = result.translate(
        str.maketrans(
            {
                "\u2013": "-",
                "\u2014": "-",
                "\u201c": '"',
                "\u201d": '"',
                "\u2018": "'",
                "\u2019": "'",
                "\u00a0": " ",
            }
        )
    )
    return _EMOJI.sub("", result).strip()


def normalize_ats_tokens(value: str) -> tuple[str, ...]:
    """Tokenize extracted PDF text for deterministic fidelity comparison."""

    normalized = unicodedata.normalize("NFKC", sanitize_ats_text(value)).casefold()
    return tuple(re.findall(r"[\w+#./%-]+", normalized, flags=re.UNICODE))
