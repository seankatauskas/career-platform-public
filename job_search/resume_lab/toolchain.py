"""Small composition layer for structured resume -> TeX -> PDF -> ATS text."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from job_search.contracts import ContractError

from .fidelity import PdfFidelityReport, evaluate_pdf_fidelity
from .pdf import PdfExtraction, PypdfExtractor
from .tex import (CompiledPdf, RenderedResume, TectonicCompiler, render_resume_tex,
    DEFAULT_TEMPLATE_VERSION, TexLayoutOverflowError)


class PdfFidelityError(RuntimeError):
    """A generated artifact did not preserve the intended visible resume."""


@dataclass(frozen=True)
class ResumeArtifactBuild:
    rendered: RenderedResume
    compiled: CompiledPdf
    extracted: PdfExtraction
    fidelity: PdfFidelityReport


class ResumeArtifactToolchain:
    """Create a generated PDF and fail closed when either parser view loses content."""

    def __init__(self, compiler: TectonicCompiler, extractor: PypdfExtractor) -> None:
        self.compiler = compiler
        self.extractor = extractor

    def build(
        self,
        content: Mapping[str, Any],
        *,
        allow_warning: bool = True,
        visible_notice: str = "",
        template_version: str = DEFAULT_TEMPLATE_VERSION,
        max_pages: int | None = None,
    ) -> ResumeArtifactBuild:
        if max_pages is not None and (isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 5):
            raise ContractError("resume page budget must be between one and five")
        rendered = render_resume_tex(content, visible_notice=visible_notice, template_version=template_version)
        compiled = self.compiler.compile(rendered.tex_source)
        if template_version == DEFAULT_TEMPLATE_VERSION and any(
            float(value) > 0.5 for value in re.findall(
                r"Overfull \\[hv]box \(([\d.]+)pt too (?:wide|high)\)", compiled.log_excerpt
            )
        ):
            raise TexLayoutOverflowError("resume content overflows the template text area")
        extracted = self.extractor.extract(compiled.pdf_bytes)
        if extracted.pdf_sha256 != compiled.pdf_sha256:
            raise PdfFidelityError(
                "the extracted PDF differs from the compiled artifact"
            )
        if max_pages is not None and extracted.pages > max_pages:
            raise TexLayoutOverflowError(f"resume exceeds the {max_pages}-page budget")
        fidelity = evaluate_pdf_fidelity(
            rendered.intended_text, extracted, rendered.section_names
        )
        if fidelity.status == "fail" or (
            fidelity.status == "warn" and not allow_warning
        ):
            raise PdfFidelityError("generated PDF failed the ATS text-fidelity gate")
        return ResumeArtifactBuild(rendered, compiled, extracted, fidelity)
