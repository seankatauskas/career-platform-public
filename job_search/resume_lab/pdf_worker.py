#!/usr/bin/env python3
"""Isolated pypdf worker. It is executed with ``python -I`` by ``pdf.py``."""

from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path


MAX_PAGES = 5
MAX_CONTENT_STREAM_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_CHARS = 256_000


def extract(path: Path) -> dict:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("resume PDF extraction requires requirements/resume.txt") from exc
    reader = PdfReader(str(path), strict=True)
    if reader.is_encrypted:
        raise ValueError("encrypted resume PDFs are not supported")
    if not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("resume PDF page count is invalid")
    logical: list[str] = []
    layout: list[str] = []
    content_bytes = 0
    for page in reader.pages:
        contents = page.get_contents()
        if contents is not None:
            content_bytes += len(contents.get_data())
            if content_bytes > MAX_CONTENT_STREAM_BYTES:
                raise ValueError("resume PDF content streams exceed the safe limit")
        # Latin Modern's kerned capitals otherwise become "F rameworks" or
        # "T ools" with pypdf's 200-unit fallback when no space glyph exists.
        # A quarter-em fallback preserves words and explicit spaces; the separate
        # layout extraction and unchanged fidelity thresholds still gate output.
        logical.append(page.extract_text(space_width=250) or "")
        layout.append(
            page.extract_text(
                extraction_mode="layout", layout_mode_space_vertically=False
            )
            or ""
        )
    logical_text = "\n".join(logical)
    layout_text = "\n".join(layout)
    if not logical_text.strip() or not layout_text.strip():
        raise ValueError("resume PDF has no extractable text")
    if max(len(logical_text), len(layout_text)) > MAX_OUTPUT_CHARS:
        raise ValueError("resume PDF extracted text exceeds the safe limit")
    return {
        "schema_version": 1,
        "parser": "pypdf",
        "parser_version": version("pypdf"),
        "pages": len(reader.pages),
        "content_stream_bytes": content_bytes,
        "logical_text": logical_text,
        "layout_text": layout_text,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    source, destination = map(Path, argv[1:])
    payload = extract(source)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
