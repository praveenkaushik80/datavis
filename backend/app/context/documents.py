"""Document parser: Docling when installed, lightweight parsers otherwise. Text is passed straight to the
research agent (no RAG / vector search in MVP1)."""
import csv
import io
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)
ALLOWED = {".pdf", ".docx", ".doc", ".json", ".txt", ".csv", ".md"}
MAX_CHARS = 60_000


class DocumentError(ValueError):
    pass


def _docling(path: Path) -> str | None:
    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        return None
    try:
        return DocumentConverter().convert(str(path)).document.export_to_markdown()
    except Exception as exc:  # noqa: BLE001
        log.warning("Docling failed on %s: %s; falling back", path.name, exc)
        return None


def parse_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in ALLOWED:
        raise DocumentError(f"Unsupported file type {ext}. Allowed: {', '.join(sorted(ALLOWED))}")
    if ext in {".pdf", ".docx", ".doc"}:
        text = _docling(path)
        if text is None and ext == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
        if text is None and ext == ".docx":
            import docx
            text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        if text is None:
            raise DocumentError(f"No parser available for {ext}; install docling.")
    elif ext == ".json":
        text = json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=1)
    elif ext == ".csv":
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8", errors="replace"))))
        text = "\n".join(", ".join(r) for r in rows[:500])
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    return text[:MAX_CHARS]


def resolve_shared_path(base: Path, relative: str) -> Path:
    """Only allow files inside the shared docs folder that Apache Hop and the backend both mount."""
    candidate = (base / relative.lstrip("/")).resolve()
    if base.resolve() not in candidate.parents and candidate != base.resolve():
        raise DocumentError("Path must be inside the shared docs folder.")
    if not candidate.is_file():
        raise DocumentError(f"File not found: {relative}")
    return candidate
