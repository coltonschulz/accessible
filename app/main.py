"""NDID Examination Report → Accessible Markdown Converter.

FastAPI backend. Accepts .docx uploads, converts to Markdown via MarkItDown,
and runs a WCAG 2.1 AA compliance audit on the output.
"""

import io
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from markitdown import MarkItDown

app = FastAPI(title="NDID Exam Report Converter")

_STATIC_DIR = Path(__file__).parent / "static"
_TMP_DIR = Path("/tmp/docx_converter")
_TMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the single-page UI."""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.post("/convert")
async def convert(file: UploadFile = File(...)) -> dict[str, Any]:
    """Convert an uploaded .docx file to Markdown and run compliance audit.

    Args:
        file: The uploaded .docx file.

    Returns:
        A dict with keys ``filename``, ``markdown``, and ``report``.

    Raises:
        HTTPException: 400 if the file is not a .docx or exceeds size limit.
        HTTPException: 422 if MarkItDown fails to parse the file.
    """
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are accepted.")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 50 MB limit.")

    # Write to tmp so MarkItDown can open it by path
    tmp_path = _TMP_DIR / f"{os.urandom(8).hex()}_{file.filename}"
    try:
        tmp_path.write_bytes(contents)
        md_text = _convert_docx(tmp_path)
        md_text = post_process_markdown(md_text)
        embedded_images = extract_docx_images(tmp_path)
        report = build_compliance_report(md_text, embedded_images)
    finally:
        tmp_path.unlink(missing_ok=True)

    stem = Path(file.filename).stem
    return {"filename": f"{stem}.md", "markdown": md_text, "report": report}


@app.post("/download-markdown")
async def download_markdown(
    filename: str = Form(...),
    markdown: str = Form(...),
) -> Response:
    """Return Markdown content as a downloadable .md file.

    Args:
        filename: Desired download filename (e.g. ``report.md``).
        markdown: Raw Markdown text to send.

    Returns:
        A ``text/markdown`` file response.
    """
    return Response(
        content=markdown.encode("utf-8"),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Conversion helper
# ---------------------------------------------------------------------------


def _convert_docx(path: Path) -> str:
    """Run MarkItDown on a local .docx file and return the Markdown string.

    Args:
        path: Absolute path to the .docx file.

    Raises:
        HTTPException: 422 if MarkItDown raises any exception.
    """
    try:
        md = MarkItDown()
        result = md.convert(str(path))
        return result.text_content or ""
    except Exception as exc:  # MarkItDown surfaces various internal errors
        raise HTTPException(
            status_code=422,
            detail=f"MarkItDown conversion failed: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def post_process_markdown(text: str) -> str:
    """Normalize whitespace and heading spacing in converted Markdown.

    Args:
        text: Raw Markdown from MarkItDown.

    Returns:
        Cleaned Markdown string.
    """
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Ensure a blank line before every ATX heading
    text = re.sub(r"(?<!\n)\n(#{1,6} )", r"\n\n\1", text)
    # Ensure a blank line after every ATX heading
    text = re.sub(r"(#{1,6} .+)\n(?!\n)", r"\1\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Compliance engine
# ---------------------------------------------------------------------------


def check_heading_hierarchy(text: str) -> list[dict[str, Any]]:
    """Detect skipped heading levels (e.g. H1 → H3).

    Args:
        text: Markdown source.

    Returns:
        List of issue dicts (level, line, message).
    """
    issues: list[dict[str, Any]] = []
    headings: list[tuple[int, int]] = []  # (level, line_number)

    for lineno, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"^(#{1,6})\s+\S", line)
        if m:
            headings.append((len(m.group(1)), lineno))

    for i in range(1, len(headings)):
        prev_level, _ = headings[i - 1]
        curr_level, curr_line = headings[i]
        if curr_level > prev_level + 1:
            issues.append(
                {
                    "line": curr_line,
                    "message": (
                        f"Heading level skipped: H{prev_level} → H{curr_level}. "
                        "All heading levels must be used in sequence."
                    ),
                    "wcag": "2.4.6",
                    "severity": "error",
                }
            )
    return issues


def check_has_h1(text: str) -> list[dict[str, Any]]:
    """Verify the document has exactly one H1.

    Args:
        text: Markdown source.

    Returns:
        List of issue dicts.
    """
    issues: list[dict[str, Any]] = []
    h1_lines = [
        lineno
        for lineno, line in enumerate(text.splitlines(), start=1)
        if re.match(r"^# \S", line)
    ]

    if not h1_lines:
        issues.append(
            {
                "line": 1,
                "message": "Document is missing an H1 heading (page title).",
                "wcag": "2.4.2",
                "severity": "error",
            }
        )
    elif len(h1_lines) > 1:
        for ln in h1_lines[1:]:
            issues.append(
                {
                    "line": ln,
                    "message": (
                        f"Multiple H1 headings found (line {h1_lines[0]} and line {ln}). "
                        "Documents should have a single H1."
                    ),
                    "wcag": "2.4.2",
                    "severity": "warning",
                }
            )
    return issues


def check_image_alt_text(text: str) -> list[dict[str, Any]]:
    """Flag images with empty or generic alt text.

    Args:
        text: Markdown source.

    Returns:
        List of issue dicts.
    """
    issues: list[dict[str, Any]] = []
    _GENERIC = {"image", "photo", "figure", "img", "picture", "graphic"}

    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", line):
            alt = m.group(1).strip()
            if not alt:
                issues.append(
                    {
                        "line": lineno,
                        "message": "Image is missing alt text.",
                        "wcag": "1.1.1",
                        "severity": "error",
                    }
                )
            elif alt.lower() in _GENERIC:
                issues.append(
                    {
                        "line": lineno,
                        "message": (
                            f'Image alt text "{alt}" is generic and not descriptive.'
                        ),
                        "wcag": "1.1.1",
                        "severity": "warning",
                    }
                )
    return issues


def extract_docx_images(path: Path) -> list[str]:
    """List embedded media filenames from a .docx (zip) archive.

    Args:
        path: Path to the .docx file.

    Returns:
        Sorted list of ``word/media/*`` filenames, or empty list on error.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return sorted(
                name for name in zf.namelist() if name.startswith("word/media/")
            )
    except Exception:
        return []


def check_empty_links(text: str) -> list[dict[str, Any]]:
    """Flag bare URLs and non-descriptive link text.

    Args:
        text: Markdown source.

    Returns:
        List of issue dicts.
    """
    issues: list[dict[str, Any]] = []
    _BAD_TEXT = {
        "click here",
        "here",
        "link",
        "read more",
        "more",
        "this",
        "url",
        "website",
    }

    for lineno, line in enumerate(text.splitlines(), start=1):
        # Bare URLs not wrapped in <>
        for m in re.finditer(r"(?<!\()(https?://\S+)(?!\))", line):
            issues.append(
                {
                    "line": lineno,
                    "message": f'Bare URL used as link text: "{m.group(1)[:60]}".',
                    "wcag": "2.4.4",
                    "severity": "warning",
                }
            )
        # Non-descriptive link text
        for m in re.finditer(r"\[([^\]]+)\]\(https?://[^)]+\)", line):
            if m.group(1).strip().lower() in _BAD_TEXT:
                issues.append(
                    {
                        "line": lineno,
                        "message": (
                            f'Non-descriptive link text: "{m.group(1)}". '
                            "Describe the link destination."
                        ),
                        "wcag": "2.4.4",
                        "severity": "warning",
                    }
                )
    return issues


def check_table_headers(text: str) -> list[dict[str, Any]]:
    """Flag Markdown tables that are missing a header separator row.

    Args:
        text: Markdown source.

    Returns:
        List of issue dicts.
    """
    issues: list[dict[str, Any]] = []
    lines = text.splitlines()

    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        # A table row: starts and ends with | OR has multiple cells
        if not re.match(r"^\s*\|", line):
            continue
        # Check if the NEXT line is the separator (---) row
        if i + 1 < len(lines):
            next_line = lines[i + 1]
            if re.match(r"^\s*\|[\s|:-]+\|", next_line):
                continue  # proper table
        # If the previous line was NOT a separator, flag this row
        if i > 0 and re.match(r"^\s*\|[\s|:-]+\|", lines[i - 1]):
            continue
        # Looks like a table row without a separator row immediately after
        issues.append(
            {
                "line": i + 1,
                "message": (
                    "Table may be missing a header separator row (`| --- |`). "
                    "Screen readers need headers to interpret table data."
                ),
                "wcag": "1.3.1",
                "severity": "warning",
            }
        )
    # De-duplicate (consecutive table rows produce multiple hits)
    seen: set[int] = set()
    deduped = []
    for issue in issues:
        if issue["line"] not in seen:
            seen.add(issue["line"])
            deduped.append(issue)
    return deduped


def check_empty_headings(text: str) -> list[dict[str, Any]]:
    """Flag headings that contain no visible text.

    Args:
        text: Markdown source.

    Returns:
        List of issue dicts.
    """
    issues: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"^(#{1,6})\s*$", line)
        if m:
            issues.append(
                {
                    "line": lineno,
                    "message": f"H{len(m.group(1))} heading has no content.",
                    "wcag": "1.3.1",
                    "severity": "error",
                }
            )
    return issues


def check_inline_formatting_overuse(text: str) -> list[dict[str, Any]]:
    """Flag paragraphs where the entire content is bold or italic.

    Args:
        text: Markdown source.

    Returns:
        List of issue dicts.
    """
    issues: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if len(stripped) < 20:
            continue
        # Entire line wrapped in ** ... **
        if re.match(r"^\*\*[^*]+\*\*$", stripped):
            issues.append(
                {
                    "line": lineno,
                    "message": (
                        "Entire paragraph is bold. Use bold sparingly for emphasis, "
                        "not as a substitute for headings."
                    ),
                    "wcag": "1.3.1",
                    "severity": "info",
                }
            )
    return issues


def build_compliance_report(
    text: str,
    embedded_images: list[str],
) -> dict[str, Any]:
    """Run all compliance checks and compute a 0–100 score.

    Scoring: 100 base − 15 per error − 5 per warning − 1 per info.

    Args:
        text: Markdown source text.
        embedded_images: List of embedded media filenames from the source DOCX.

    Returns:
        Report dict with keys ``score``, ``issues``, ``wcag_summary``,
        ``embedded_image_count``.
    """
    issues: list[dict[str, Any]] = []
    issues.extend(check_heading_hierarchy(text))
    issues.extend(check_has_h1(text))
    issues.extend(check_image_alt_text(text))
    issues.extend(check_empty_links(text))
    issues.extend(check_table_headers(text))
    issues.extend(check_empty_headings(text))
    issues.extend(check_inline_formatting_overuse(text))

    # Flag embedded images that need manual alt text in the output
    for img_name in embedded_images:
        issues.append(
            {
                "line": None,
                "message": (
                    f'Embedded image "{Path(img_name).name}" was not extracted. '
                    "Host separately and add descriptive alt text in the Markdown."
                ),
                "wcag": "1.1.1",
                "severity": "error",
            }
        )

    score = 100
    for issue in issues:
        if issue["severity"] == "error":
            score -= 15
        elif issue["severity"] == "warning":
            score -= 5
        elif issue["severity"] == "info":
            score -= 1
    score = max(0, score)

    # Summarise which WCAG criteria were affected
    wcag_summary: dict[str, str] = {}
    affected = {i["wcag"] for i in issues}
    all_criteria = {
        "1.1.1": "Non-text Content",
        "1.3.1": "Info & Relationships",
        "2.4.2": "Page Titled",
        "2.4.4": "Link Purpose",
        "2.4.6": "Headings & Labels",
    }
    for crit, label in all_criteria.items():
        wcag_summary[crit] = {
            "label": label,
            "status": "fail" if crit in affected else "pass",
        }

    return {
        "score": score,
        "issues": issues,
        "wcag_summary": wcag_summary,
        "embedded_image_count": len(embedded_images),
    }
