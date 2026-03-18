"""Conversion worker — executed as a subprocess by the FastAPI app.

Receives job parameters as CLI arguments, runs MarkItDown + compliance
analysis via ``processor.py``, and writes the result to a JSON file in the
jobs directory.

Running as a subprocess (not a thread) gives this process its own GIL,
so heavy PDF parsing cannot starve the uvicorn event loop.

Usage (internal — called by main.py):
    python worker.py <job_id> <tmp_path> <is_pdf> <stem> <jobs_dir>
"""

import json
import sys
from pathlib import Path

# Make the project root importable when invoked directly as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    """Entry point: parse CLI args, run conversion, write result file."""
    if len(sys.argv) != 6:
        print(
            f"Usage: worker.py <job_id> <tmp_path> <is_pdf> <stem> <jobs_dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    job_id, tmp_path_str, is_pdf_str, stem, jobs_dir = sys.argv[1:]
    is_pdf = is_pdf_str == "True"
    tmp_path = Path(tmp_path_str)
    job_path = Path(jobs_dir) / f"{job_id}.json"

    try:
        from app.processor import process_document

        md_text, report = process_document(tmp_path, is_pdf)
        job_path.write_text(
            json.dumps({
                "status": "complete",
                "filename": f"{stem}.md",
                "markdown": md_text,
                "report": report,
                "pdf_quality_warning": is_pdf,
            }),
            encoding="utf-8",
        )
    except Exception as exc:
        job_path.write_text(
            json.dumps({"status": "error", "detail": str(exc)}),
            encoding="utf-8",
        )
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
