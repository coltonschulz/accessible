"""Conversion and compliance logic — imported by both main.py and worker.py.

Keeping these functions in a standalone module (no FastAPI imports) lets the
worker subprocess import only what it needs without pulling in the full web
framework.
"""

import re
import zipfile
from pathlib import Path
from typing import Any

from markitdown import MarkItDown


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def convert_file(path: Path) -> str:
    """Run MarkItDown on a local .docx or .pdf file and return Markdown.

    Args:
        path: Absolute path to the file.

    Raises:
        RuntimeError: If MarkItDown raises any exception.
    """
    try:
        md = MarkItDown()
        result = md.convert(str(path))
        return result.text_content or ""
    except Exception as exc:
        raise RuntimeError(f"MarkItDown conversion failed: {exc}") from exc


def post_process_markdown(text: str) -> str:
    """Normalize whitespace and heading spacing in converted Markdown.

    Args:
        text: Raw Markdown from MarkItDown.

    Returns:
        Cleaned Markdown string.
    """
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(?<!\n)\n(#{1,6} )", r"\n\n\1", text)
    text = re.sub(r"(#{1,6} .+)\n(?!\n)", r"\1\n\n", text)
    return text.strip()


def process_document(tmp_path: Path, is_pdf: bool) -> tuple[str, dict[str, Any]]:
    """Run conversion and compliance audit synchronously.

    Called from the worker subprocess — never from an async context.

    Args:
        tmp_path: Path to the uploaded file in /tmp.
        is_pdf: True when the source file is a PDF.

    Returns:
        Tuple of (markdown_text, compliance_report).
    """
    md_text = convert_file(tmp_path)
    md_text = post_process_markdown(md_text)
    embedded_images = [] if is_pdf else extract_docx_images(tmp_path)
    report = build_compliance_report(md_text, embedded_images)
    return md_text, report


# ---------------------------------------------------------------------------
# Readability helpers  (Flesch Reading Ease, pure Python, no deps)
# ---------------------------------------------------------------------------

_VOWELS = frozenset("aeiouy")


def _count_syllables(word: str) -> int:
    """Approximate syllable count for one word.

    Args:
        word: A single word, possibly with trailing punctuation.

    Returns:
        Syllable count (minimum 1).
    """
    word = word.lower().rstrip(".,!?;:'\")")
    if not word:
        return 0
    count = 0
    prev_vowel = False
    for ch in word:
        is_vowel = ch in _VOWELS
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    # Drop silent trailing 'e' (e.g. "rate", "late")
    if word.endswith("e") and len(word) > 2 and word[-2] not in _VOWELS and count > 1:
        count -= 1
    return max(1, count)


def _flesch_reading_ease(text: str) -> float:
    """Compute the Flesch Reading Ease score for a Markdown document.

    Args:
        text: Markdown source.

    Returns:
        Score from 0.0 to 100.0, rounded to one decimal place.
    """
    plain = re.sub(r"[#*`_~\[\]()!>|\\-]", " ", text)
    plain = re.sub(r"https?://\S+", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()

    sentences = [s.strip() for s in re.split(r"[.!?]+\s", plain) if s.strip()]
    words = re.findall(r"\b[a-zA-Z']{1,}\b", plain)

    if not sentences or not words:
        return 0.0

    syllables = sum(_count_syllables(w) for w in words)
    asl = len(words) / len(sentences)
    asw = syllables / len(words)
    score = 206.835 - 1.015 * asl - 84.6 * asw
    return round(max(0.0, min(100.0, score)), 1)


def _reading_ease_label(score: float) -> str:
    """Map a Flesch Reading Ease score to a human-readable label.

    Args:
        score: Flesch score (0–100).

    Returns:
        Short descriptive label.
    """
    if score >= 70:
        return "Easy (grades 6–7)"
    if score >= 60:
        return "Standard (grades 8–9)"
    if score >= 50:
        return "Fairly Difficult (grades 10–12)"
    if score >= 30:
        return "Difficult (college level)"
    return "Very Difficult (post-graduate)"


# ---------------------------------------------------------------------------
# Compliance checks
# ---------------------------------------------------------------------------


def check_heading_hierarchy(text: str) -> list[dict[str, Any]]:
    """Detect skipped heading levels (e.g. H1 → H3)."""
    issues: list[dict[str, Any]] = []
    headings: list[tuple[int, int]] = []

    for lineno, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"^(#{1,6})\s+\S", line)
        if m:
            headings.append((len(m.group(1)), lineno))

    for i in range(1, len(headings)):
        prev_level, _ = headings[i - 1]
        curr_level, curr_line = headings[i]
        if curr_level > prev_level + 1:
            issues.append({
                "line": curr_line,
                "message": (
                    f"Heading level skipped: H{prev_level} → H{curr_level}. "
                    "All heading levels must be used in sequence."
                ),
                "wcag": "2.4.6",
                "severity": "error",
            })
    return issues


def check_has_h1(text: str) -> list[dict[str, Any]]:
    """Verify the document has exactly one H1."""
    issues: list[dict[str, Any]] = []
    h1_lines = [
        lineno
        for lineno, line in enumerate(text.splitlines(), start=1)
        if re.match(r"^# \S", line)
    ]

    if not h1_lines:
        issues.append({
            "line": 1,
            "message": "Document is missing an H1 heading (page title).",
            "wcag": "2.4.2",
            "severity": "error",
        })
    elif len(h1_lines) > 1:
        for ln in h1_lines[1:]:
            issues.append({
                "line": ln,
                "message": (
                    f"Multiple H1 headings found (line {h1_lines[0]} and "
                    f"line {ln}). Documents should have a single H1."
                ),
                "wcag": "2.4.2",
                "severity": "warning",
            })
    return issues


def check_image_alt_text(text: str) -> list[dict[str, Any]]:
    """Flag images with empty or generic alt text."""
    issues: list[dict[str, Any]] = []
    _GENERIC = {"image", "photo", "figure", "img", "picture", "graphic"}

    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", line):
            alt = m.group(1).strip()
            if not alt:
                issues.append({
                    "line": lineno,
                    "message": "Image is missing alt text.",
                    "wcag": "1.1.1",
                    "severity": "error",
                })
            elif alt.lower() in _GENERIC:
                issues.append({
                    "line": lineno,
                    "message": f'Image alt text "{alt}" is generic and not descriptive.',
                    "wcag": "1.1.1",
                    "severity": "warning",
                })
    return issues


def extract_docx_images(path: Path) -> list[str]:
    """List embedded media filenames from a .docx (zip) archive."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return sorted(
                name for name in zf.namelist() if name.startswith("word/media/")
            )
    except Exception:
        return []


def check_empty_links(text: str) -> list[dict[str, Any]]:
    """Flag bare URLs and non-descriptive link text."""
    issues: list[dict[str, Any]] = []
    _BAD_TEXT = {"click here", "here", "link", "read more", "more", "this", "url", "website"}

    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in re.finditer(r"(?<!\()(https?://\S+)(?!\))", line):
            issues.append({
                "line": lineno,
                "message": f'Bare URL used as link text: "{m.group(1)[:60]}".',
                "wcag": "2.4.4",
                "severity": "warning",
            })
        for m in re.finditer(r"\[([^\]]+)\]\(https?://[^)]+\)", line):
            if m.group(1).strip().lower() in _BAD_TEXT:
                issues.append({
                    "line": lineno,
                    "message": (
                        f'Non-descriptive link text: "{m.group(1)}". '
                        "Describe the link destination."
                    ),
                    "wcag": "2.4.4",
                    "severity": "warning",
                })
    return issues


def check_table_headers(text: str) -> list[dict[str, Any]]:
    """Flag Markdown tables that are missing a header separator row."""
    issues: list[dict[str, Any]] = []
    lines = text.splitlines()

    for i, line in enumerate(lines):
        if "|" not in line or not re.match(r"^\s*\|", line):
            continue
        if i + 1 < len(lines) and re.match(r"^\s*\|[\s|:-]+\|", lines[i + 1]):
            continue
        if i > 0 and re.match(r"^\s*\|[\s|:-]+\|", lines[i - 1]):
            continue
        issues.append({
            "line": i + 1,
            "message": (
                "Table may be missing a header separator row (`| --- |`). "
                "Screen readers need headers to interpret table data."
            ),
            "wcag": "1.3.1",
            "severity": "warning",
        })

    seen: set[int] = set()
    deduped = []
    for issue in issues:
        if issue["line"] not in seen:
            seen.add(issue["line"])
            deduped.append(issue)
    return deduped


def check_empty_headings(text: str) -> list[dict[str, Any]]:
    """Flag headings that contain no visible text."""
    issues: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"^(#{1,6})\s*$", line)
        if m:
            issues.append({
                "line": lineno,
                "message": f"H{len(m.group(1))} heading has no content.",
                "wcag": "1.3.1",
                "severity": "error",
            })
    return issues


def check_inline_formatting_overuse(text: str) -> list[dict[str, Any]]:
    """Flag paragraphs where the entire content is bold."""
    issues: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if len(stripped) < 20:
            continue
        if re.match(r"^\*\*[^*]+\*\*$", stripped):
            issues.append({
                "line": lineno,
                "message": (
                    "Entire paragraph is bold. Use bold sparingly for emphasis, "
                    "not as a substitute for headings."
                ),
                "wcag": "1.3.1",
                "severity": "info",
            })
    return issues


def check_reading_level(text: str) -> list[dict[str, Any]]:
    """Check readability using the Flesch Reading Ease scale."""
    score = _flesch_reading_ease(text)
    if score < 20:
        return [{
            "line": None,
            "message": (
                f"Readability score: {score}/100 — Very Difficult. "
                "Summary and findings sections should be readable at a general "
                "public level. Consider plain language review."
            ),
            "wcag": "3.1.5",
            "severity": "warning",
        }]
    if score < 30:
        return [{
            "line": None,
            "message": (
                f"Readability score: {score}/100 — Difficult (post-graduate). "
                "Technical regulatory content is expected here, but consider "
                "simplifying executive summaries."
            ),
            "wcag": "3.1.5",
            "severity": "info",
        }]
    return []


def check_all_caps(text: str) -> list[dict[str, Any]]:
    """Flag lines where the majority of words are in ALL CAPS."""
    issues: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "`", "|")):
            continue
        words = re.findall(r"\b[A-Za-z]{3,}\b", stripped)
        if len(words) < 4:
            continue
        caps_count = sum(1 for w in words if w.isupper())
        if caps_count / len(words) >= 0.6:
            issues.append({
                "line": lineno,
                "message": (
                    "Excessive ALL CAPS text. Screen readers may read each "
                    "letter individually. Use title case or sentence case instead."
                ),
                "wcag": "1.3.1",
                "severity": "warning",
            })
    return issues


def check_color_references(text: str) -> list[dict[str, Any]]:
    """Flag uses of color as the sole means of conveying information."""
    issues: list[dict[str, Any]] = []
    _COLORS = r"(?:red|green|blue|yellow|orange|purple|pink|gr[ae]y|black|white)"
    _PATTERN = re.compile(
        rf"\b(?:shown?\s+in|marked?\s+in|highlighted?\s+in|colored?\s+in|"
        rf"displayed?\s+in|appears?\s+in|indicated?\s+in|"
        rf"the\s+{_COLORS}\s+(?:item|cell|row|column|field|section|text|area|box|"
        rf"highlight|shading)s?)\b",
        re.IGNORECASE,
    )

    for lineno, line in enumerate(text.splitlines(), start=1):
        if _PATTERN.search(line):
            issues.append({
                "line": lineno,
                "message": (
                    "Color appears to be used as the only means of conveying "
                    "information. Provide a text-based alternative "
                    "(e.g. a label, symbol, or note) alongside any color coding."
                ),
                "wcag": "1.4.1",
                "severity": "error",
            })
    return issues


def check_unformatted_lists(text: str) -> list[dict[str, Any]]:
    """Detect lines using manual bullet characters instead of Markdown list syntax."""
    issues: list[dict[str, Any]] = []
    _MANUAL_BULLET = re.compile(r"^[•·▪◦▸►▶❖]\s|^[–—]\s+\w")
    _ALPHA_LIST = re.compile(r"^[a-hj-z]\.\s+\w", re.IGNORECASE)

    last_flagged = -2
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if _MANUAL_BULLET.match(stripped) or _ALPHA_LIST.match(stripped):
            if lineno > last_flagged + 1:
                issues.append({
                    "line": lineno,
                    "message": (
                        "Manual bullet character detected. Use Markdown list "
                        "syntax (`- item` or `1. item`) so assistive technology "
                        "can announce list structure."
                    ),
                    "wcag": "1.3.1",
                    "severity": "error",
                })
            last_flagged = lineno
    return issues


def check_duplicate_headings(text: str) -> list[dict[str, Any]]:
    """Flag heading text that appears more than once in the document."""
    issues: list[dict[str, Any]] = []
    seen: dict[str, int] = {}

    for lineno, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"^(#{1,6})\s+(.+)", line)
        if not m:
            continue
        raw = m.group(2).strip()
        key = re.sub(r"[*_`]", "", raw).lower().strip()
        if key in seen:
            issues.append({
                "line": lineno,
                "message": (
                    f'Duplicate heading "{raw}" (first appears at line '
                    f"{seen[key]}). Each heading must be unique so screen "
                    "reader users can navigate by heading."
                ),
                "wcag": "2.4.6",
                "severity": "warning",
            })
        else:
            seen[key] = lineno
    return issues


def check_table_context(text: str) -> list[dict[str, Any]]:
    """Flag tables not preceded by a heading or descriptive text."""
    issues: list[dict[str, Any]] = []
    lines = text.splitlines()

    for i, line in enumerate(lines):
        if not re.match(r"^\|", line.strip()):
            continue
        if i > 0 and re.match(r"^\|", lines[i - 1].strip()):
            continue
        has_context = any(
            lines[j].strip() and not re.match(r"^\|", lines[j].strip())
            for j in range(max(0, i - 4), i)
        )
        if not has_context:
            issues.append({
                "line": i + 1,
                "message": (
                    "Table appears without a preceding heading or description. "
                    "Add a heading or introductory sentence so users understand "
                    "the table's purpose before encountering it."
                ),
                "wcag": "1.3.1",
                "severity": "warning",
            })
    return issues


# ---------------------------------------------------------------------------
# Report aggregation
# ---------------------------------------------------------------------------

_WCAG_CRITERIA: dict[str, str] = {
    "1.1.1": "Non-text Content",
    "1.3.1": "Info & Relationships",
    "1.4.1": "Use of Color",
    "2.4.2": "Page Titled",
    "2.4.4": "Link Purpose",
    "2.4.6": "Headings & Labels",
    "3.1.5": "Reading Level",
}

_ADVISORY_CRITERIA = {"3.1.5"}


def build_compliance_report(
    text: str,
    embedded_images: list[str],
) -> dict[str, Any]:
    """Run all compliance checks and compute a 0–100 score.

    Args:
        text: Markdown source text.
        embedded_images: List of embedded media filenames from the source DOCX.

    Returns:
        Report dict with keys: ``score``, ``conformance_tier``,
        ``blocking_count``, ``readability``, ``issues``, ``wcag_summary``,
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
    issues.extend(check_all_caps(text))
    issues.extend(check_color_references(text))
    issues.extend(check_unformatted_lists(text))
    issues.extend(check_duplicate_headings(text))
    issues.extend(check_table_context(text))
    issues.extend(check_reading_level(text))

    for img_name in embedded_images:
        issues.append({
            "line": None,
            "message": (
                f'Embedded image "{Path(img_name).name}" was not extracted. '
                "Host separately and add descriptive alt text in the Markdown."
            ),
            "wcag": "1.1.1",
            "severity": "error",
        })

    score = 100
    for issue in issues:
        if issue.get("wcag") in _ADVISORY_CRITERIA:
            continue
        if issue["severity"] == "error":
            score -= 15
        elif issue["severity"] == "warning":
            score -= 5
        elif issue["severity"] == "info":
            score -= 1
    score = max(0, score)

    if score >= 90:
        tier = "Conformant"
    elif score >= 60:
        tier = "Partially Conformant"
    else:
        tier = "Non-Conformant"

    blocking_count = sum(
        1 for i in issues
        if i["severity"] == "error" and i.get("wcag") not in _ADVISORY_CRITERIA
    )

    flesch = _flesch_reading_ease(text)
    readability = {"score": flesch, "label": _reading_ease_label(flesch)}

    affected = {i["wcag"] for i in issues}
    wcag_summary = {
        crit: {
            "label": label,
            "status": "fail" if crit in affected else "pass",
            "advisory": crit in _ADVISORY_CRITERIA,
        }
        for crit, label in _WCAG_CRITERIA.items()
    }

    return {
        "score": score,
        "conformance_tier": tier,
        "blocking_count": blocking_count,
        "readability": readability,
        "issues": issues,
        "wcag_summary": wcag_summary,
        "embedded_image_count": len(embedded_images),
    }
