"""ATS-oriented TeX rendering, preflight, and isolated Tectonic compilation."""

from __future__ import annotations

import hashlib
import os
import re
import resource
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from job_search.contracts import ContractError

from .process import ProcessOutputLimitError, run_bounded_process

from .career_ops import TEMPLATE_PATH, escape_latex, sanitize_ats_text, sanitize_url


MAX_TEX_BYTES = 512 * 1024
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_COMPILER_OUTPUT_BYTES = 32 * 1024
MAX_COMPILER_FILE_BYTES = 32 * 1024 * 1024
LEGACY_TEMPLATE_VERSION = "career-ops-v1"
DEFAULT_TEMPLATE_VERSION = "jake-v1"
TEMPLATE_VERSIONS = (DEFAULT_TEMPLATE_VERSION, LEGACY_TEMPLATE_VERSION)
JAKE_TEMPLATE_PATH = Path(__file__).with_name("jake_template.tex")
ALLOWED_PACKAGES = frozenset({"geometry", "enumitem", "hyperref", "titlesec",
    "latexsym", "fullpage", "marvosym", "color", "verbatim", "fancyhdr",
    "babel", "tabularx"})
_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")
_USEPACKAGE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{([^}]+)\}")
_BANNED_COMMAND = re.compile(
    r"\\(?:write18|openin|openout|read|input|include|includegraphics|bibliography|"
    r"addbibresource|directlua|special|pdfobj|pdfliteral|scantokens|catcode|everyjob|"
    r"newread|newwrite|filecontents|loop|repeat)(?![A-Za-z@])",
    re.IGNORECASE,
)


class TexToolchainError(RuntimeError):
    """TeX could not be safely rendered or compiled."""


class TexLayoutOverflowError(TexToolchainError):
    """Content exceeds the requested page budget or the template's text area."""


@dataclass(frozen=True)
class TexPreflightReport:
    safe: bool
    issues: tuple[str, ...]
    source_sha256: str
    source_bytes: int


@dataclass(frozen=True)
class RenderedResume:
    tex_source: str
    intended_text: str
    source_sha256: str
    section_names: tuple[str, ...]
    template_version: str = LEGACY_TEMPLATE_VERSION


@dataclass(frozen=True)
class CompiledPdf:
    pdf_bytes: bytes
    pdf_sha256: str
    source_sha256: str
    engine: str
    engine_version: str
    bundle_sha256: str
    log_excerpt: str


def attest_real_resume_links(content: Mapping[str, Any]) -> Mapping[str, Any]:
    """Remove hidden link authority while retaining attested visible labels.

    PDF text extraction can attest the visible contact label, but not a PDF link
    annotation or a model-produced JSON ``url`` value.  Real variants therefore
    retain only the visible label.  The renderer deterministically derives a safe
    HTTP(S)/mailto target from that label; project targets, which have no visible
    source field in the normalization contract, are removed entirely.
    """

    if not isinstance(content, Mapping):
        raise ContractError("resume content must be an object")
    result = deepcopy(dict(content))
    identity = result.get("identity")
    if isinstance(identity, Mapping):
        normalized_identity = dict(identity)
        for key in ("email", "linkedin", "github"):
            item = normalized_identity.get(key)
            if isinstance(item, Mapping):
                display = item.get("display")
                if isinstance(display, str) and display.strip():
                    normalized_identity[key] = {"display": display}
                else:
                    # A URL-only object has no text span that can attest it.
                    normalized_identity.pop(key, None)
        result["identity"] = normalized_identity
    projects = result.get("projects")
    if isinstance(projects, list):
        normalized_projects = []
        for item in projects:
            if isinstance(item, Mapping):
                normalized = dict(item)
                normalized.pop("url", None)
                normalized_projects.append(normalized)
            else:
                normalized_projects.append(item)
        result["projects"] = normalized_projects
    return result


def _without_comments(source: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in source.splitlines())


def preflight_tex(source: str) -> TexPreflightReport:
    """Reject unsafe or structurally invalid generated TeX before compilation."""

    if not isinstance(source, str):
        raise ContractError("TeX source must be text")
    encoded = source.encode("utf-8")
    issues: list[str] = []
    if not encoded or len(encoded) > MAX_TEX_BYTES:
        issues.append("TeX source size is invalid")
    if "\x00" in source or any(ord(ch) < 9 for ch in source):
        issues.append("TeX source contains control bytes")
    active = _without_comments(source)
    if active.count(r"\begin{document}") != 1 or active.count(r"\end{document}") != 1:
        issues.append("TeX must contain exactly one document environment")
    elif active.index(r"\begin{document}") > active.index(r"\end{document}"):
        issues.append("TeX document environment is out of order")
    classes = re.findall(r"\\documentclass(?:\[[^\]]*\])?\{([^}]+)\}", active)
    if classes != ["article"]:
        issues.append("TeX document class must be article")
    packages = {
        package.strip()
        for group in _USEPACKAGE.findall(active)
        for package in group.split(",")
    }
    unexpected = packages - ALLOWED_PACKAGES
    if unexpected:
        issues.append("TeX uses unsupported packages: " + ", ".join(sorted(unexpected)))
    if _BANNED_COMMAND.search(active):
        issues.append("TeX contains a file, shell, or dynamic execution command")
    unresolved = sorted(set(_PLACEHOLDER.findall(active)))
    if unresolved:
        issues.append("TeX contains unresolved placeholders: " + ", ".join(unresolved))
    return TexPreflightReport(
        not issues,
        tuple(issues),
        hashlib.sha256(encoded).hexdigest(),
        len(encoded),
    )


def _bounded_text(value: Any, field: str, maximum: int = 4000) -> str:
    if isinstance(value, Mapping):
        value = value.get("text")
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ContractError(f"{field} must be text")
    result = str(value).strip()
    if len(result) > maximum or "\x00" in result:
        raise ContractError(f"{field} is too long or invalid")
    return result


def _bounded_list(value: Any, field: str, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ContractError(f"{field} must be a bounded array")
    return value


def _strict_mapping(value: Any, field: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not set(value) <= allowed:
        raise ContractError(f"{field} has an invalid shape")
    return value


def _bullet(value: Any, field: str, *, escape: Callable[[Any], str] = escape_latex) -> tuple[str, str]:
    raw = _bounded_text(value, field)
    escaped = escape(raw)
    # Career Ops behavior: escape first, then restore only paired markdown bold.
    escaped = re.sub(
        r"\*\*([^*]+?)\*\*", lambda match: rf"\textbf{{{match.group(1)}}}", escaped
    )
    plain = raw.replace("**", "")
    return escaped, plain


def _section(title: str, lines: Sequence[str]) -> str:
    return "\n".join((rf"\section{{{escape_latex(title)}}}", *lines))


def _contact(identity: Mapping[str, Any], *, escape: Callable[[Any], str] = escape_latex) -> tuple[str, str]:
    rendered: list[str] = []
    plain: list[str] = []
    contact_line = _bounded_text(
        identity.get("contact_line"), "identity.contact_line", 1000
    )
    if contact_line:
        rendered.append(escape(contact_line))
        plain.append(contact_line)
    for key in ("email", "linkedin", "github"):
        item = identity.get(key)
        if not item:
            continue
        if isinstance(item, str):
            display = item
            url = sanitize_url(item)
        else:
            item = _strict_mapping(item, f"identity.{key}", {"display", "url"})
            display = _bounded_text(
                item.get("display") or item.get("url"), f"identity.{key}.display", 1000
            )
            url = sanitize_url(item.get("url") or display)
        if not display:
            continue
        rendered.append(
            rf"\href{{{url}}}{{{escape(display)}}}"
            if url
            else escape(display)
        )
        plain.append(display)
    return r" \textbar{} ".join(rendered), " | ".join(plain)


def _render_legacy_tex(
    content: Mapping[str, Any], *, visible_notice: str = ""
) -> RenderedResume:
    """Render the strict structured resume shape through the vendored template."""

    content = _strict_mapping(
        content,
        "resume content",
        {"identity", "summary", "experience", "projects", "education", "skills"},
    )
    identity = _strict_mapping(
        content.get("identity", {}),
        "identity",
        {"name", "contact_line", "email", "linkedin", "github"},
    )
    name = _bounded_text(identity.get("name"), "identity.name", 500)
    if not name:
        raise ContractError("identity.name must not be empty")
    contact_tex, contact_plain = _contact(identity)
    substitutions: dict[str, str] = {
        "NAME": escape_latex(name),
        "CONTACT": contact_tex,
    }
    intended: list[str] = [name]
    if contact_plain:
        intended.append(contact_plain)
    notice = _bounded_text(visible_notice, "visible_notice", 500)
    substitutions["NOTICE"] = (
        rf"\begin{{center}}\textbf{{{escape_latex(notice)}}}\end{{center}}"
        if notice
        else ""
    )
    if notice:
        intended.append(notice)
    section_names: list[str] = []

    summary = _bounded_text(content.get("summary"), "summary")
    if summary:
        section_names.append("Summary")
        substitutions["SUMMARY_SECTION"] = _section("Summary", [escape_latex(summary)])
        intended.extend(("Summary", summary))
    else:
        substitutions["SUMMARY_SECTION"] = ""

    experience_lines: list[str] = []
    experience_plain: list[str] = []
    for index, raw in enumerate(
        _bounded_list(content.get("experience"), "experience", 20)
    ):
        entry = _strict_mapping(
            raw,
            f"experience[{index}]",
            {"company", "role", "location", "dates", "bullets"},
        )
        company = _bounded_text(
            entry.get("company"), f"experience[{index}].company", 500
        )
        role = _bounded_text(entry.get("role"), f"experience[{index}].role", 500)
        dates = _bounded_text(entry.get("dates"), f"experience[{index}].dates", 500)
        location = _bounded_text(
            entry.get("location"), f"experience[{index}].location", 500
        )
        if not company or not role:
            raise ContractError("experience entries require company and role")
        experience_lines.append(
            rf"\textbf{{{escape_latex(company)}}}\hfill {escape_latex(dates)}\\"
        )
        experience_lines.append(
            rf"\emph{{{escape_latex(role)}}}\hfill {escape_latex(location)}"
        )
        experience_plain.extend(
            (
                " | ".join(part for part in (company, dates) if part),
                " | ".join(part for part in (role, location) if part),
            )
        )
        bullets = _bounded_list(
            entry.get("bullets"), f"experience[{index}].bullets", 20
        )
        if bullets:
            experience_lines.append(r"\begin{itemize}")
            for bullet_index, value in enumerate(bullets):
                rendered, plain = _bullet(
                    value, f"experience[{index}].bullets[{bullet_index}]"
                )
                experience_lines.append(rf"\item {rendered}")
                experience_plain.append("- " + plain)
            experience_lines.append(r"\end{itemize}")
    if experience_lines:
        section_names.append("Work Experience")
        substitutions["EXPERIENCE_SECTION"] = _section(
            "Work Experience", experience_lines
        )
        intended.extend(("Work Experience", *experience_plain))
    else:
        substitutions["EXPERIENCE_SECTION"] = ""

    project_lines: list[str] = []
    project_plain: list[str] = []
    for index, raw in enumerate(_bounded_list(content.get("projects"), "projects", 20)):
        entry = _strict_mapping(
            raw, f"projects[{index}]", {"name", "context", "dates", "url", "bullets"}
        )
        project_name = _bounded_text(entry.get("name"), f"projects[{index}].name", 500)
        context = _bounded_text(
            entry.get("context"), f"projects[{index}].context", 1000
        )
        dates = _bounded_text(entry.get("dates"), f"projects[{index}].dates", 500)
        url = sanitize_url(entry.get("url"))
        if not project_name:
            raise ContractError("project entries require a name")
        linked_name = (
            rf"\href{{{url}}}{{\textbf{{{escape_latex(project_name)}}}}}"
            if url
            else rf"\textbf{{{escape_latex(project_name)}}}"
        )
        heading = linked_name + (
            rf" --- \emph{{{escape_latex(context)}}}" if context else ""
        )
        project_lines.append(rf"{heading}\hfill {escape_latex(dates)}\\")
        project_plain.append(
            " | ".join(part for part in (project_name, context, dates) if part)
        )
        bullets = _bounded_list(entry.get("bullets"), f"projects[{index}].bullets", 20)
        if bullets:
            project_lines.append(r"\begin{itemize}")
            for bullet_index, value in enumerate(bullets):
                rendered, plain = _bullet(
                    value, f"projects[{index}].bullets[{bullet_index}]"
                )
                project_lines.append(rf"\item {rendered}")
                project_plain.append("- " + plain)
            project_lines.append(r"\end{itemize}")
    if project_lines:
        section_names.append("Projects")
        substitutions["PROJECTS_SECTION"] = _section("Projects", project_lines)
        intended.extend(("Projects", *project_plain))
    else:
        substitutions["PROJECTS_SECTION"] = ""

    education_lines: list[str] = []
    education_plain: list[str] = []
    for index, raw in enumerate(
        _bounded_list(content.get("education"), "education", 20)
    ):
        entry = _strict_mapping(
            raw,
            f"education[{index}]",
            {"institution", "degree", "location", "dates", "details"},
        )
        institution = _bounded_text(
            entry.get("institution"), f"education[{index}].institution", 500
        )
        degree = _bounded_text(entry.get("degree"), f"education[{index}].degree", 1000)
        location = _bounded_text(
            entry.get("location"), f"education[{index}].location", 500
        )
        dates = _bounded_text(entry.get("dates"), f"education[{index}].dates", 500)
        details = _bounded_text(entry.get("details"), f"education[{index}].details")
        if not institution or not degree:
            raise ContractError("education entries require institution and degree")
        education_lines.append(
            rf"\textbf{{{escape_latex(institution)}}}\hfill {escape_latex(dates)}\\"
        )
        education_lines.append(
            rf"{escape_latex(degree)}\hfill {escape_latex(location)}"
        )
        education_plain.extend(
            (
                " | ".join(part for part in (institution, dates) if part),
                " | ".join(part for part in (degree, location) if part),
            )
        )
        if details:
            education_lines.append(rf"\\{escape_latex(details)}")
            education_plain.append(details)
    if education_lines:
        section_names.append("Education")
        substitutions["EDUCATION_SECTION"] = _section("Education", education_lines)
        intended.extend(("Education", *education_plain))
    else:
        substitutions["EDUCATION_SECTION"] = ""

    skill_lines: list[str] = []
    skill_plain: list[str] = []
    for index, raw in enumerate(_bounded_list(content.get("skills"), "skills", 30)):
        entry = _strict_mapping(raw, f"skills[{index}]", {"category", "items"})
        category = _bounded_text(
            entry.get("category"), f"skills[{index}].category", 500
        )
        items = [
            _bounded_text(value, f"skills[{index}].items", 500)
            for value in _bounded_list(
                entry.get("items"), f"skills[{index}].items", 100
            )
        ]
        items = [value for value in items if value]
        if category and items:
            skill_lines.append(
                rf"\textbf{{{escape_latex(category)}}}: {escape_latex(', '.join(items))}\\"
            )
            skill_plain.append(f"{category}: {', '.join(items)}")
    if skill_lines:
        section_names.append("Skills")
        substitutions["SKILLS_SECTION"] = _section("Skills", skill_lines)
        intended.extend(("Skills", *skill_plain))
    else:
        substitutions["SKILLS_SECTION"] = ""

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, replacement in substitutions.items():
        template = template.replace("{{" + key + "}}", replacement)
    report = preflight_tex(template)
    if not report.safe:
        raise TexToolchainError(
            "rendered TeX failed preflight: " + "; ".join(report.issues)
        )
    return RenderedResume(
        template,
        sanitize_ats_text("\n".join(filter(None, intended))),
        report.source_sha256,
        tuple(section_names),
    )


def render_resume_tex(
    content: Mapping[str, Any], *, visible_notice: str = "",
    template_version: str = DEFAULT_TEMPLATE_VERSION,
) -> RenderedResume:
    """Render escaped content with a named, bundled, immutable layout version.

    Historical artifacts can explicitly select the original renderer. New
    documents use the user-supplied Jake layout, including its reading order.
    """
    if template_version not in TEMPLATE_VERSIONS:
        raise ContractError("unknown resume template version")
    # Keep one strict content contract across the two layouts.
    legacy = _render_legacy_tex(content, visible_notice=visible_notice)
    if template_version == LEGACY_TEMPLATE_VERSION:
        return legacy
    identity = content["identity"]
    def escape(value: Any) -> str:
        # Career records are plain text: prevent TeX from silently converting
        # consecutive literal hyphens (dates, flags, identifiers) into an en dash.
        return escape_latex(value).replace("--", "-{}-")

    contact_tex, contact_plain = _contact(identity, escape=escape)
    name = _bounded_text(identity["name"], "identity.name", 500)
    intended = [name]
    if contact_plain:
        intended.append(contact_plain)
    notice = _bounded_text(visible_notice, "visible_notice", 500)
    substitutions = {
        "NAME": escape(name), "CONTACT": contact_tex,
        "NOTICE": (rf"\begin{{center}}\textbf{{{escape(notice)}}}\end{{center}}" if notice else ""),
    }
    if notice:
        intended.append(notice)
    sections = []
    summary = _bounded_text(content.get("summary"), "summary")
    substitutions["SUMMARY_SECTION"] = _section("Summary", [escape(summary)]) if summary else ""
    if summary:
        sections.append("Summary")
        intended.extend(("Summary", summary))

    def append_section(key: str, title: str, lines: list[str], plain: list[str]) -> None:
        if lines:
            substitutions[key] = _section(title, [r"\resumeSubHeadingListStart", *lines, r"\resumeSubHeadingListEnd"])
            intended.extend((title, *plain))
            sections.append(title)
        else:
            substitutions[key] = ""

    def bullets(values: list[Any], lines: list[str], plain: list[str]) -> None:
        if values:
            lines.append(r"\resumeItemListStart")
            for value in values:
                escaped, text = _bullet(value, "bullet", escape=escape)
                lines.append(rf"\resumeItem{{{escaped}}}")
                plain.append("- " + text)
            lines.append(r"\resumeItemListEnd")

    lines, plain = [], []
    for entry in content.get("education") or []:
        values = [_bounded_text(entry.get(key), key) for key in ("institution", "location", "degree", "dates")]
        lines.append(r"\resumeSubheading" + "".join("{" + escape(value) + "}" for value in values))
        plain.extend((" | ".join(filter(None, values[:2])), " | ".join(filter(None, values[2:]))))
        details = _bounded_text(entry.get("details"), "details")
        if details:
            bullets([details], lines, plain)
    append_section("EDUCATION_SECTION", "Education", lines, plain)

    lines, plain = [], []
    for entry in content.get("experience") or []:
        values = [_bounded_text(entry.get(key), key) for key in ("role", "dates", "company", "location")]
        lines.append(r"\resumeSubheading" + "".join("{" + escape(value) + "}" for value in values))
        plain.extend((" | ".join(filter(None, values[:2])), " | ".join(filter(None, values[2:]))))
        bullets(entry.get("bullets") or [], lines, plain)
    append_section("EXPERIENCE_SECTION", "Experience", lines, plain)

    lines, plain = [], []
    for entry in content.get("projects") or []:
        project_name, context, dates = [_bounded_text(entry.get(key), key) for key in ("name", "context", "dates")]
        heading = rf"\textbf{{{escape(project_name)}}}"
        url = sanitize_url(entry.get("url"))
        if url:
            heading = rf"\href{{{url}}}{{{heading}}}"
        if context:
            heading += rf" $|$ \emph{{{escape(context)}}}"
        lines.append(rf"\resumeProjectHeading{{{heading}}}{{{escape(dates)}}}")
        plain.append(" | ".join(filter(None, (project_name, context, dates))))
        bullets(entry.get("bullets") or [], lines, plain)
    append_section("PROJECTS_SECTION", "Projects", lines, plain)

    lines, plain = [], []
    for entry in content.get("skills") or []:
        category = _bounded_text(entry.get("category"), "category")
        items = [_bounded_text(value, "skill") for value in entry.get("items") or []]
        items = [value for value in items if value]
        if category and items:
            lines.append(rf"\textbf{{{escape(category)}}}{{: {escape(', '.join(items))}}}")
            plain.append(f"{category}: {', '.join(items)}")
    substitutions["SKILLS_SECTION"] = ""
    if lines:
        substitutions["SKILLS_SECTION"] = _section("Technical Skills", [
            r"\begin{itemize}[leftmargin=0.15in, label={}]",
            r"\small{\item{" + " \\\\\n".join(lines) + "}}", r"\end{itemize}",
        ])
        sections.append("Technical Skills")
        intended.extend(("Technical Skills", *plain))
    template = JAKE_TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, replacement in substitutions.items():
        template = template.replace("{{" + key + "}}", replacement)
    report = preflight_tex(template)
    if not report.safe:
        raise TexToolchainError("rendered TeX failed preflight: " + "; ".join(report.issues))
    return RenderedResume(template, sanitize_ats_text("\n".join(filter(None, intended))),
        report.source_sha256, tuple(sections), template_version)


def _sandbox_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def macos_tectonic_sandbox(
    command: Sequence[str], directory: Path, executable: Path, bundle: Path
) -> tuple[str, ...]:
    """Wrap Tectonic in the same deny-by-default macOS boundary used elsewhere."""

    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.exists():
        raise TexToolchainError("Tectonic sandbox is unavailable on this host")
    logical_directory = Path(directory).expanduser().absolute()
    directory = logical_directory.resolve()
    write_directories = {logical_directory, directory}
    executable = executable.resolve()
    read_paths = {
        Path("/System"),
        Path("/usr/bin"),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path("/Library/Fonts"),
        executable,
        bundle.resolve(),
    }
    if str(executable).startswith("/opt/homebrew/"):
        read_paths.add(Path("/opt/homebrew/Cellar"))
        read_paths.add(Path("/opt/homebrew/opt"))
    clauses = [
        "(version 1)",
        "(deny default)",
        "(deny network*)",
        "(allow process-info*)",
        # Rust's alternate signal stack needs the actual ARM macOS page size.
        # Without this narrow read, Tectonic aborts before opening the document.
        '(allow sysctl-read (sysctl-name "hw.pagesize") (sysctl-name "hw.pagesize_compat"))',
        # The dynamic loader must stat the root and the ancestors of explicitly
        # allowed paths. These literal grants do not expose their descendants.
        '(allow file-read* (literal "/"))',
        f'(allow process-exec (literal "{_sandbox_literal(str(executable))}"))',
        '(allow file-read* (literal "/dev/null") (literal "/dev/urandom"))',
    ]
    for writable in sorted(write_directories, key=str):
        clauses.append(
            f'(allow file-read* file-write* (subpath "{_sandbox_literal(str(writable))}"))'
        )
    resolved_paths = {path.resolve() for path in read_paths}
    ancestors = {
        parent
        for path in (*resolved_paths, *write_directories)
        for parent in path.parents
        if parent != Path("/")
    }
    for ancestor in sorted(ancestors, key=str):
        clauses.append(
            f'(allow file-read* (literal "{_sandbox_literal(str(ancestor))}"))'
        )
    for path in sorted(resolved_paths, key=str):
        kind = "subpath" if path.is_dir() else "literal"
        clauses.append(f'(allow file-read* ({kind} "{_sandbox_literal(str(path))}"))')
    return (str(sandbox), "-p", "\n".join(clauses), "--", str(executable), *command[1:])


def _compiler_limits() -> None:
    for name, requested in (
        (resource.RLIMIT_CPU, 20),
        # A fresh isolated HOME builds a roughly 23 MiB LaTeX format cache.
        # The final PDF retains its independent 5 MiB validation below.
        (resource.RLIMIT_FSIZE, MAX_COMPILER_FILE_BYTES),
        (resource.RLIMIT_NOFILE, 48),
    ):
        _soft, hard = resource.getrlimit(name)
        value = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(name, (value, hard))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TectonicCompiler:
    """Compile preflighted TeX using a pinned local bundle and no network."""

    def __init__(
        self,
        executable: Path,
        bundle: Path,
        engine_version: str,
        *,
        timeout_seconds: float = 30.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        isolation_builder: Callable[
            [Sequence[str], Path, Path, Path], Sequence[str]
        ] = macos_tectonic_sandbox,
    ) -> None:
        self.executable = Path(executable).expanduser().resolve()
        self.bundle = Path(bundle).expanduser().resolve()
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise ContractError(
                "Tectonic executable must be an executable absolute file"
            )
        if not self.bundle.is_file():
            raise ContractError("Tectonic bundle must be an existing local file")
        if (
            not isinstance(engine_version, str)
            or not engine_version.strip()
            or len(engine_version) > 200
        ):
            raise ContractError("Tectonic engine version is invalid")
        if not 1 <= float(timeout_seconds) <= 60:
            raise ContractError("Tectonic timeout must be between 1 and 60 seconds")
        self.engine_version = engine_version.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.bundle_sha256 = _file_sha256(self.bundle)
        self.runner = runner
        self.isolation_builder = isolation_builder

    def compile(self, source: str) -> CompiledPdf:
        report = preflight_tex(source)
        if not report.safe:
            raise TexToolchainError("TeX preflight failed: " + "; ".join(report.issues))
        with tempfile.TemporaryDirectory(prefix="job-resume-tex-") as name:
            directory = Path(name).resolve()
            tex_path = directory / "resume.tex"
            descriptor = os.open(tex_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(source)
            command = (
                str(self.executable),
                "-X",
                "compile",
                "--untrusted",
                "--only-cached",
                "--bundle",
                str(self.bundle),
                "--keep-logs",
                "--outdir",
                str(directory),
                str(tex_path),
            )
            isolated = tuple(
                self.isolation_builder(command, directory, self.executable, self.bundle)
            )
            if not isolated:
                raise TexToolchainError("Tectonic isolation returned an empty command")
            try:
                environment = {
                    "HOME": str(directory),
                    "TMPDIR": str(directory),
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "SOURCE_DATE_EPOCH": "946684800",
                    "TECTONIC_UNTRUSTED_MODE": "1",
                }
                if self.runner is None:
                    completed = run_bounded_process(
                        isolated,
                        timeout=self.timeout_seconds,
                        cwd=str(directory),
                        env=environment,
                        preexec_fn=_compiler_limits,
                        max_output_bytes=MAX_COMPILER_OUTPUT_BYTES,
                    )
                else:
                    completed = self.runner(
                        isolated,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=self.timeout_seconds,
                        check=False,
                        shell=False,
                        cwd=str(directory),
                        env=environment,
                        preexec_fn=_compiler_limits,
                    )
            except (
                OSError,
                ProcessOutputLimitError,
                subprocess.TimeoutExpired,
            ) as exc:
                raise TexToolchainError("Tectonic compilation failed safely") from exc
            if completed.returncode != 0:
                raise TexToolchainError(
                    f"Tectonic exited with status {completed.returncode}"
                )
            pdf_path = directory / "resume.pdf"
            try:
                info = pdf_path.lstat()
                pdf = pdf_path.read_bytes()
            except OSError as exc:
                raise TexToolchainError("Tectonic did not produce resume.pdf") from exc
            if not stat.S_ISREG(info.st_mode) or pdf_path.is_symlink():
                raise TexToolchainError("Tectonic output is not a regular file")
            if not pdf or len(pdf) > MAX_PDF_BYTES:
                raise TexToolchainError("Tectonic PDF size is invalid")
            if not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-4096:]:
                raise TexToolchainError("Tectonic output is not a complete PDF")
            logs = "\n".join(
                value
                for value in (completed.stdout, completed.stderr)
                if isinstance(value, str)
            )
            # Tectonic keeps detailed overfull-box diagnostics in the TeX log;
            # bounded reads let the caller reject clipped/wide generated output.
            log_path = directory / "resume.log"
            if log_path.is_file() and not log_path.is_symlink():
                with log_path.open("rb") as handle:
                    tex_log = handle.read(MAX_COMPILER_OUTPUT_BYTES).decode("utf-8", "replace")
                overflow_lines = "\n".join(line for line in tex_log.splitlines() if "Overfull" in line)
                logs = overflow_lines + "\n" + logs
            log_excerpt = logs.encode("utf-8")[:MAX_COMPILER_OUTPUT_BYTES].decode(
                "utf-8", "replace"
            )
            return CompiledPdf(
                pdf,
                hashlib.sha256(pdf).hexdigest(),
                report.source_sha256,
                "tectonic",
                self.engine_version,
                self.bundle_sha256,
                log_excerpt,
            )
