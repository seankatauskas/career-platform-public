#!/usr/bin/env python3
"""Isolated attachment-to-text helper. Never import this in the main process."""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


MAX_DOCX_FILES = 500
MAX_DOCX_UNCOMPRESSED = 20 * 1024 * 1024
MAX_OUTPUT_CHARS = 256_000


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_DOCX_FILES:
            raise ValueError("unsafe DOCX entry count")
        total = 0
        names = set()
        for info in infos:
            name = info.filename
            if (
                info.flag_bits & 0x1
                or name.startswith(("/", "\\"))
                or ".." in Path(name).parts
                or info.file_size < 0
            ):
                raise ValueError("unsafe DOCX archive entry")
            total += info.file_size
            if total > MAX_DOCX_UNCOMPRESSED:
                raise ValueError("DOCX expands beyond limit")
            if info.compress_size and info.file_size / info.compress_size > 100:
                raise ValueError("DOCX compression ratio exceeds limit")
            names.add(name)
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise ValueError("DOCX package is incomplete")
        document = archive.read("word/document.xml")
    root = ElementTree.fromstring(document)
    parts = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "t" and element.text:
            parts.append(element.text)
        elif tag in {"p", "br", "tab"}:
            parts.append("\n" if tag != "tab" else "\t")
    return "".join(parts)


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("PDF extraction requires pypdf") from exc
    reader = PdfReader(str(path), strict=True)
    if len(reader.pages) > 200:
        raise ValueError("PDF page count exceeds limit")
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _ics_text(path: Path) -> str:
    value = path.read_text(encoding="utf-8-sig")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not re.match(r"^\s*BEGIN:VCALENDAR(?:\n|$)", normalized, re.I):
        raise ValueError("ICS does not start with VCALENDAR")
    if not re.search(r"(?:^|\n)END:VCALENDAR\s*$", normalized, re.I):
        raise ValueError("ICS does not end with VCALENDAR")
    return normalized


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    mime, input_name, output_name = argv[1:]
    source = Path(input_name)
    if mime == "application/pdf":
        text = _pdf_text(source)
    elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        text = _docx_text(source)
    elif mime == "text/calendar":
        text = _ics_text(source)
    else:
        raise ValueError("unsupported attachment MIME")
    Path(output_name).write_text(text[:MAX_OUTPUT_CHARS], encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
