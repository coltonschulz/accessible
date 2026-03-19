"""Accessible — NDID ADA/WCAG 2.1 AA Compliance Tool.

FastAPI backend. Accepts .docx and .pdf uploads, converts to Markdown via
MarkItDown, and runs a full WCAG 2.1 AA compliance audit on the output.

Conversion runs in a subprocess (worker.py) so that MarkItDown's PDF
parser cannot hold the Python GIL and starve the asyncio event loop.
Job state is stored as JSON files in /tmp/accessible/jobs/ so it is
accessible regardless of how many uvicorn worker processes are running.

Subprocess management intentionally uses subprocess.Popen (not asyncio
subprocess) to keep the asyncio event loop completely free — the asyncio
child-watcher / ThreadedChildWatcher can stall the event loop on Python
3.12 inside Docker containers when the subprocess exits.
"""

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

app = FastAPI(title="Accessible — NDID ADA Compliance Tool")

_STATIC_DIR = Path(__file__).parent / "static"
_WORKER = Path(__file__).parent / "worker.py"

_TMP_DIR = Path("/tmp/accessible")
_JOBS_DIR = Path("/tmp/accessible/jobs")
_TMP_DIR.mkdir(parents=True, exist_ok=True)
_JOBS_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 50 * 1024 * 1024   # 50 MB
MAX_CONVERSION_SECONDS = 180        # kill worker if it stalls
_ACCEPTED_EXTENSIONS = {".docx", ".pdf"}

# In-memory upload staging — keyed by UUID, consumed by POST /convert.
# Safe with a single uvicorn worker; file-based if multi-worker is needed.
_uploads: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Job-file helpers
# ---------------------------------------------------------------------------


def _job_path(job_id: str) -> Path:
    return _JOBS_DIR / f"{job_id}.json"


def _read_job(job_id: str) -> dict[str, Any] | None:
    """Return parsed job dict, or None if the file doesn't exist."""
    p = _job_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_job(job_id: str, data: dict[str, Any]) -> None:
    _job_path(job_id).write_text(json.dumps(data), encoding="utf-8")


def _delete_job(job_id: str) -> None:
    _job_path(job_id).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Worker process management — pure threads, no asyncio involvement
# ---------------------------------------------------------------------------


def _monitor_worker(
    job_id: str,
    proc: subprocess.Popen,
    timeout: int = MAX_CONVERSION_SECONDS,
) -> None:
    """Wait for the worker process in a daemon thread.

    Uses subprocess.Popen.wait() in a plain background thread — completely
    outside the asyncio event loop — so the event loop is never stalled by
    subprocess child-watcher callbacks.

    Args:
        job_id: UUID of the job being monitored.
        proc: The worker subprocess handle (Popen).
        timeout: Seconds before the worker is killed.
    """
    try:
        proc.wait(timeout=timeout)
        if proc.returncode != 0:
            # Worker crashed without writing a terminal state.
            job = _read_job(job_id) or {}
            if job.get("status") == "pending":
                _write_job(job_id, {
                    "status": "error",
                    "detail": "Conversion process exited unexpectedly.",
                })
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()  # reap zombie
        _write_job(job_id, {
            "status": "error",
            "detail": (
                "Conversion timed out after 3 minutes. "
                "The document may be a scanned/image-only PDF that cannot be "
                "processed automatically. Try a text-based PDF or .docx instead."
            ),
        })


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the single-page UI."""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/robots.txt", response_class=Response)
async def robots() -> Response:
    """Disallow all crawlers — this is a tool, not a content site."""
    return Response(
        content="User-agent: *\nDisallow: /\n",
        media_type="text/plain",
    )


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """Receive and stage a file for later conversion.

    Args:
        file: The uploaded .docx or .pdf file.

    Returns:
        A dict with ``file_id``, ``filename``, and ``size`` (bytes).

    Raises:
        HTTPException: 400 if the file type is unsupported or exceeds limit.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    ext = Path(file.filename).suffix.lower()
    if ext not in _ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Only .docx and .pdf files are accepted.",
        )

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 50 MB limit.")

    file_id = str(uuid.uuid4())
    tmp_path = _TMP_DIR / f"{file_id}_{file.filename}"
    tmp_path.write_bytes(contents)

    _uploads[file_id] = {
        "path": tmp_path,
        "filename": file.filename,
        "is_pdf": ext == ".pdf",
        "stem": Path(file.filename).stem,
        "size": len(contents),
    }
    return {"file_id": file_id, "filename": file.filename, "size": len(contents)}


@app.post("/convert")
async def convert(file_id: str = Form(...)) -> dict[str, str]:
    """Enqueue a conversion job for a previously uploaded file.

    Spawns ``worker.py`` as a subprocess (via Popen, not asyncio subprocess)
    so that MarkItDown's PDF parser runs in a separate process with its own
    GIL, keeping the uvicorn event loop free to answer status-poll requests.

    Args:
        file_id: UUID returned by ``POST /upload``.

    Returns:
        A dict with key ``job_id``.

    Raises:
        HTTPException: 404 if the ``file_id`` is unknown or already used.
    """
    upload = _uploads.pop(file_id, None)
    if upload is None:
        raise HTTPException(
            status_code=404,
            detail="Upload not found or already converted. Please re-upload.",
        )

    job_id = str(uuid.uuid4())
    _write_job(job_id, {"status": "pending"})

    proc = subprocess.Popen(  # noqa: S603 — args are internal UUIDs/paths
        [
            sys.executable,
            str(_WORKER),
            job_id,
            str(upload["path"]),
            str(upload["is_pdf"]),
            upload["stem"],
            str(_JOBS_DIR),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Monitor the worker in a daemon thread — no asyncio involvement.
    t = threading.Thread(
        target=_monitor_worker,
        args=(job_id, proc),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id}


@app.get("/convert/status/{job_id}")
async def convert_status(job_id: str) -> dict[str, Any]:
    """Poll conversion job status.

    Args:
        job_id: UUID returned by ``POST /convert``.

    Returns:
        ``{"status": "pending"}`` while running, or the full result dict on
        completion, or ``{"status": "error", "detail": "..."}`` on failure.

    Raises:
        HTTPException: 404 if the job ID is unknown or already expired.
    """
    job = _read_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404, detail="Job not found or already retrieved."
        )
    # Remove the file once a terminal state is delivered to the client.
    if job.get("status") in ("complete", "error"):
        _delete_job(job_id)
    return job


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
