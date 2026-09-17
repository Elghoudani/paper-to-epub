from __future__ import annotations

import argparse
import io
import json
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import BOOKS_DIR, OUTPUT_DIR

ENGINE_NAME    = "paper2epub"
ENGINE_VERSION = "2.0"

MAX_UPLOAD_BYTES = 80 * 1024 * 1024

STAGE_WEIGHTS = {
    "fetch":    (0.00, 0.04),
    "render":   (0.04, 0.14),
    "layout":   (0.14, 0.76),
    "extract":  (0.76, 0.93),
    "assemble": (0.93, 1.00),
}
STAGE_LABELS = {
    "queued":   "Waiting",
    "fetch":    "Fetching paper",
    "render":   "Rendering pages",
    "layout":   "Detecting layout",
    "extract":  "Extracting text and figures",
    "assemble": "Assembling EPUB",
    "done":     "Done",
    "error":    "Failed",
}

_STAGE_RE    = re.compile(r"^\[stage:(\w+)\]\s*(.*)$")
_PROGRESS_RE = re.compile(r"^(\d+)/(\d+)$")


class Job:

    def __init__(self, source: str, label: str):
        self.id       = uuid.uuid4().hex[:12]
        self.source   = source
        self.label    = label
        self.status   = "queued"
        self.stage    = "queued"
        self.percent  = 0.0
        self.log: list[str] = []
        self.error: str | None = None
        self.epub_path: Path | None = None
        self.title: str | None = None
        self.created  = time.time()
        self.lock     = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id":       self.id,
                "status":   self.status,
                "stage":    self.stage,
                "stageLabel": STAGE_LABELS.get(self.stage, self.stage),
                "percent":  round(self.percent * 100, 1),
                "label":    self.label,
                "title":    self.title,
                "error":    self.error,
                "log":      self.log[-40:],
                "filename": self.epub_path.name if self.epub_path else None,
                "size":     self.epub_path.stat().st_size if self.epub_path and self.epub_path.exists() else None,
            }

    def note(self, line: str) -> None:
        with self.lock:
            self.log.append(line)
            if len(self.log) > 400:
                del self.log[:200]


class _JobWriter(io.TextIOBase):

    def __init__(self, job: Job, mirror):
        self.job    = job
        self.mirror = mirror
        self._buf   = ""

    def write(self, text: str) -> int:
        self.mirror.write(text)
        self._buf += text
        while True:
            match = re.search(r"[\r\n]", self._buf)
            if not match:
                break
            line, self._buf = self._buf[: match.start()], self._buf[match.end():]
            line = line.rstrip()
            if line:
                self._handle(line)
        return len(text)

    def flush(self) -> None:
        self.mirror.flush()

    def _handle(self, line: str) -> None:
        job = self.job
        tagged = _STAGE_RE.match(line.strip())
        if not tagged:
            job.note(line.strip())
            return

        stage, message = tagged.group(1), tagged.group(2).strip()

        with job.lock:
            if stage in STAGE_WEIGHTS and stage != job.stage:
                job.stage = stage
                job.percent = STAGE_WEIGHTS[stage][0]

        progress = _PROGRESS_RE.match(message)
        if progress:
            done, total = int(progress.group(1)), max(1, int(progress.group(2)))
            with job.lock:
                low, high = STAGE_WEIGHTS.get(job.stage, (0.0, 1.0))
                job.percent = low + (high - low) * (done / total)
            return

        if message:
            job.note(f"{stage}: {message}")


class Engine:

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.queue: queue.Queue[Job] = queue.Queue()
        self.current: Job | None = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def submit(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.id] = job
        self.queue.put(job)

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def status(self) -> dict:
        with self.lock:
            current = self.current
            return {
                "busy":  current is not None,
                "queue": self.queue.qsize(),
                "current": current.id if current else None,
            }

    def _loop(self) -> None:
        while True:
            job = self.queue.get()
            with self.lock:
                self.current = job
            try:
                self._run(job)
            except Exception:
                with job.lock:
                    job.status = "error"
                    job.stage  = "error"
                    job.error  = traceback.format_exc(limit=3)
            finally:
                with self.lock:
                    self.current = None
                self.queue.task_done()

    def _run(self, job: Job) -> None:
        with job.lock:
            job.status = "running"

        writer = _JobWriter(job, sys.__stdout__)
        with redirect_stdout(writer):
            from convert import convert_paper
            from progress import Reporter

            source = self._resolve(job)
            if source.metadata and source.metadata.get("title"):
                with job.lock:
                    job.title = source.metadata["title"]
            epub_path = convert_paper(source, force=True, reporter=Reporter())

        with job.lock:
            job.epub_path = Path(epub_path)
            job.status    = "done"
            job.stage     = "done"
            job.percent   = 1.0
            if not job.title:
                job.title = Path(epub_path).stem

    def _resolve(self, job: Job):
        from sources import PaperSource, resolve

        if job.source == "upload":
            return PaperSource(pdf_path=Path(job.label))

        with job.lock:
            job.stage = "fetch"
        from progress import Reporter
        return resolve(job.label, Reporter())


ENGINE = Engine()


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_book_name(raw: str) -> str:
    name = Path(raw or "paper.pdf").name
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = _SAFE_NAME.sub("_", name).strip(" ._") or "paper"
    return name[:120]


class Handler(BaseHTTPRequestHandler):
    server_version = f"{ENGINE_NAME}/{ENGINE_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")

        if path == "":
            self._send_ui()
            return

        if path == "/health":
            self._json({
                "ok":      True,
                "name":    ENGINE_NAME,
                "version": ENGINE_VERSION,
                **ENGINE.status(),
            })
            return

        match = re.fullmatch(r"/jobs/([a-f0-9]{12})", path)
        if match:
            job = ENGINE.get(match.group(1))
            if not job:
                self._json({"error": "no such job"}, 404)
                return
            self._json(job.snapshot())
            return

        match = re.fullmatch(r"/jobs/([a-f0-9]{12})/download", path)
        if match:
            job = ENGINE.get(match.group(1))
            if not job or not job.epub_path or not job.epub_path.exists():
                self._json({"error": "nothing to download yet"}, 404)
                return
            self._send_file(job.epub_path)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        if path != "/convert":
            self._json({"error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json({"error": "empty request"}, 400)
            return
        if length > MAX_UPLOAD_BYTES:
            self._json({"error": f"file is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB"}, 413)
            return

        body = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()

        if ctype == "application/json":
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._json({"error": "invalid JSON"}, 400)
                return
            target = (payload.get("arxiv") or payload.get("url") or "").strip()
            if not target:
                self._json({"error": "give an arXiv id/URL or a PDF URL"}, 400)
                return
            job = Job(source="arxiv" if "arxiv" in payload else "url", label=target)
            ENGINE.submit(job)
            self._json(job.snapshot(), 202)
            return

        if not body.startswith(b"%PDF"):
            self._json({"error": "that doesn't look like a PDF"}, 415)
            return

        name = safe_book_name(self.headers.get("X-Filename", "paper.pdf"))
        BOOKS_DIR.mkdir(parents=True, exist_ok=True)
        dest = BOOKS_DIR / f"{name}.pdf"
        dest.write_bytes(body)

        job = Job(source="upload", label=str(dest))
        job.title = name
        ENGINE.submit(job)
        self._json(job.snapshot(), 202)

    def _send_ui(self) -> None:
        page = Path(__file__).parent / "web" / "index.html"
        if not page.exists():
            self._json({"ok": True, "name": ENGINE_NAME, "version": ENGINE_VERSION})
            return
        body = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/epub+zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{_SAFE_NAME.sub("_", path.name)}"',
        )
        self._cors()
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local PDF → EPUB conversion engine.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback by default. Change it and the engine is reachable from "
             "your network — only do that on a network you control.",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"{ENGINE_NAME} {ENGINE_VERSION} listening on http://{args.host}:{args.port}")
    print(f"  open http://{args.host}:{args.port} in a browser to convert a paper")
    print("  finished files also land in ./output")
    print("  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
