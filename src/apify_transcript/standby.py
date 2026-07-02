from __future__ import annotations

import json
import os
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from apify_client import ApifyClient

from .actor import ACTOR_ID
from .jobs import (
    DEFAULT_CHUNK_SIZE,
    JOB_STORE_NAME,
    JobStore,
    create_upload_job,
    create_url_job,
    record_uploaded_chunk,
    upload_is_complete,
    validate_job_id,
    worker_input_for_job,
)


WORKER_TIMEOUT_SECONDS = 14_400


def is_standby_origin(actor: object) -> bool:
    env_origin = os.environ.get("APIFY_META_ORIGIN")
    config = getattr(actor, "configuration", None)
    config_origin = getattr(config, "meta_origin", None)
    return env_origin == "STANDBY" or config_origin == "STANDBY"


def standby_port(actor: object) -> int:
    config = getattr(actor, "configuration", None)
    for value in (
        os.environ.get("ACTOR_WEB_SERVER_PORT"),
        os.environ.get("APIFY_CONTAINER_PORT"),
        getattr(config, "standby_port", None),
        getattr(config, "web_server_port", None),
    ):
        if value:
            return int(value)
    return 4321


def serve_standby(actor: object) -> None:
    token = os.environ.get("APIFY_TOKEN") or ""
    actor_id = os.environ.get("APIFY_ACTOR_ID") or ACTOR_ID
    server = TranscriptStandbyServer(
        ("", standby_port(actor)),
        TranscriptStandbyHandler,
        token=token,
        actor_id=actor_id,
        job_store_name=JOB_STORE_NAME,
    )
    actor.log.info("Standby server listening on port %s", server.server_address[1])
    server.serve_forever()


class TranscriptStandbyServer(ThreadingHTTPServer):
    def __init__(self, *args: Any, token: str, actor_id: str, job_store_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.token = token
        self.actor_id = actor_id
        self.apify_client = ApifyClient(token)
        self.job_store = JobStore.from_token(token, job_store_name)
        self.job_store_name = job_store_name


class TranscriptStandbyHandler(BaseHTTPRequestHandler):
    server: TranscriptStandbyServer

    def do_GET(self) -> None:
        if self.headers.get("x-apify-container-server-readiness-probe") is not None:
            self.send_text("ready")
            return
        path = urlparse(self.path).path
        if path == "/":
            self.send_html(render_home_page())
            return
        if path.startswith("/jobs/") and path.endswith("/status"):
            job_id = path.removeprefix("/jobs/").removesuffix("/status").strip("/")
            self.handle_job_api(job_id)
            return
        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/").strip("/")
            self.handle_job_api(job_id)
            return
        if path.startswith("/jobs/"):
            job_id = path.removeprefix("/jobs/").strip("/")
            self.handle_job_page(job_id)
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path in {"/jobs", "/api/jobs"}:
            self.handle_create_job()
            return
        if path.startswith("/jobs/") and path.endswith("/complete"):
            job_id = path.removeprefix("/jobs/").removesuffix("/complete").strip("/")
            self.handle_complete_upload(job_id)
            return
        if path.startswith("/api/jobs/") and path.endswith("/complete"):
            job_id = path.removeprefix("/api/jobs/").removesuffix("/complete").strip("/")
            self.handle_complete_upload(job_id)
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "chunks":
            self.handle_upload_chunk(parts[1], parts[3])
            return
        if len(parts) == 5 and parts[:2] == ["api", "jobs"] and parts[3] == "chunks":
            self.handle_upload_chunk(parts[2], parts[4])
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or "0")
        if length <= 0:
            raise ValueError("request body is required")
        return self.rfile.read(length)

    def base_url(self) -> str:
        proto = self.headers.get("x-forwarded-proto") or "https"
        host = self.headers.get("x-forwarded-host") or self.headers.get("host") or "localhost"
        return f"{proto}://{host}"

    def job_url(self, job_id: str) -> str:
        return f"{self.base_url()}/jobs/{job_id}"

    def handle_create_job(self) -> None:
        try:
            payload = self.read_json()
            media_url = str(payload.get("mediaUrl") or payload.get("url") or "").strip()
            if media_url:
                self.create_direct_url_job(media_url)
                return
            file_name = str(payload.get("fileName") or "").strip()
            file_size = int(payload.get("fileSize") or 0)
            content_type = str(payload.get("contentType") or "application/octet-stream")
            if not file_name:
                raise ValueError("Provide a media URL or choose a file")
            job = create_upload_job(file_name, file_size, content_type, DEFAULT_CHUNK_SIZE)
            self.server.job_store.save_job(job)
            self.send_json({"job": job, "jobId": job["jobId"], "jobUrl": self.job_url(job["jobId"])})
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def create_direct_url_job(self, media_url: str) -> None:
        parsed = urlparse(media_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Media URL must start with http:// or https://")
        job = create_url_job(media_url)
        self.server.job_store.save_job(job)
        job = self.start_worker(job)
        self.send_json({"job": job, "jobId": job["jobId"], "jobUrl": self.job_url(job["jobId"])})

    def handle_upload_chunk(self, job_id: str, index_text: str) -> None:
        try:
            job_id = validate_job_id(job_id)
            index = int(index_text)
            job = self.server.job_store.get_job(job_id)
            if not job:
                raise FileNotFoundError("job not found")
            if job.get("status") != "uploading":
                raise ValueError("job is not accepting upload chunks")
            body = self.read_body()
            self.server.job_store.set_chunk(job_id, index, body)
            record_uploaded_chunk(job, index, len(body))
            self.server.job_store.save_job(job)
            self.send_json({"job": job})
        except FileNotFoundError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_complete_upload(self, job_id: str) -> None:
        try:
            job_id = validate_job_id(job_id)
            job = self.server.job_store.get_job(job_id)
            if not job:
                raise FileNotFoundError("job not found")
            if not upload_is_complete(job):
                raise ValueError("upload is not complete")
            job = self.start_worker(job)
            self.send_json({"job": job, "jobId": job["jobId"], "jobUrl": self.job_url(job["jobId"])})
        except FileNotFoundError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_job_api(self, job_id: str) -> None:
        try:
            job = self.server.job_store.get_job(validate_job_id(job_id))
            if not job:
                raise FileNotFoundError("job not found")
            self.send_json({"job": job})
        except FileNotFoundError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_job_page(self, job_id: str) -> None:
        try:
            validate_job_id(job_id)
            self.send_html(render_job_page(job_id))
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def start_worker(self, job: dict[str, Any]) -> dict[str, Any]:
        job["status"] = "queued"
        job["message"] = "Transcript worker is starting."
        self.server.job_store.save_job(job)
        try:
            run = self.server.apify_client.actor(self.server.actor_id).start(
                run_input=worker_input_for_job(job, self.server.job_store_name),
                content_type="application/json; charset=utf-8",
                build="latest",
                timeout_secs=WORKER_TIMEOUT_SECONDS,
                wait_for_finish=0,
            )
            run_id = run.get("id")
            job.update(
                {
                    "status": "queued",
                    "runId": run_id,
                    "runUrl": f"https://console.apify.com/actors/{self.server.actor_id}/runs/{run_id}" if run_id else None,
                    "message": "Transcript worker queued. You can leave this page and come back later.",
                }
            )
        except Exception as exc:
            job.update({"status": "failed", "error": str(exc), "message": "Could not start transcript worker."})
        return self.server.job_store.save_job(job)

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status)


def render_home_page() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Large Video to Transcript</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    <section class="panel">
      <p class="eyebrow">Large Video to Transcript</p>
      <h1>Submit media. Get transcript exports.</h1>
      <p class="sub">Upload a video/audio file or paste a direct media URL. We create the MP3, transcript, subtitles, JSON, quality report, and ZIP bundle.</p>
      <form id="job-form" class="form">
        <label>Direct media URL<input id="media-url" type="url" placeholder="https://example.com/recording.mp4"></label>
        <div class="divider">or</div>
        <label>Upload video/audio<input id="media-file" type="file" accept="audio/*,video/*,.mp3,.mp4,.mov,.m4a,.wav,.webm,.mkv"></label>
        <button id="submit-button" type="submit">Submit</button>
      </form>
      <div id="status" class="status">Ready.</div>
      <div class="progress"><div id="bar"></div></div>
      <p id="job-link" class="job-link"></p>
    </section>
  </main>
  <script>{HOME_JS}</script>
</body>
</html>"""


def render_job_page(job_id: str) -> str:
    escaped_job_id = escape(job_id)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Transcript Job</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    <section class="panel">
      <p class="eyebrow">Transcript job</p>
      <h1 id="title">Processing</h1>
      <p id="message" class="sub">Loading job {escaped_job_id}...</p>
      <div class="progress"><div id="bar"></div></div>
      <dl id="meta" class="meta"></dl>
      <div id="links" class="links"></div>
    </section>
  </main>
  <script>const JOB_ID = {json.dumps(job_id)};{JOB_JS}</script>
</body>
</html>"""


BASE_CSS = """
:root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #20242c; background: #f6f7f8; }
body { margin: 0; }
.shell { min-height: 100vh; display: grid; place-items: center; padding: 32px; box-sizing: border-box; }
.panel { width: min(760px, 100%); background: #fff; border: 1px solid #d8dde5; border-radius: 10px; padding: 34px; box-sizing: border-box; box-shadow: 0 12px 32px rgba(16, 24, 40, .08); }
.eyebrow { margin: 0 0 10px; color: #0f766e; font-size: 14px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }
h1 { margin: 0; font-size: 34px; line-height: 1.08; letter-spacing: 0; }
.sub { margin: 14px 0 24px; color: #596273; font-size: 17px; line-height: 1.5; }
.form { display: grid; gap: 16px; }
label { display: grid; gap: 8px; color: #3b4351; font-weight: 650; }
input { border: 1px solid #cfd6df; border-radius: 8px; padding: 13px 14px; font: inherit; background: #fff; box-sizing: border-box; max-width: 100%; }
button { justify-self: start; border: 0; border-radius: 8px; padding: 12px 18px; background: #0f766e; color: #fff; font: inherit; font-weight: 700; cursor: pointer; }
button:disabled { opacity: .55; cursor: default; }
.divider { color: #7b8494; font-size: 14px; }
.status { margin-top: 18px; color: #3f4857; min-height: 24px; }
.progress { height: 10px; background: #e8ecf1; border-radius: 999px; overflow: hidden; margin-top: 12px; }
.progress div { height: 100%; width: 0%; background: #0f766e; transition: width .18s ease; }
.job-link { margin: 16px 0 0; }
a { color: #0f766e; font-weight: 650; }
.links { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.links a { border: 1px solid #cfd6df; border-radius: 8px; padding: 9px 11px; text-decoration: none; }
.meta { display: grid; grid-template-columns: max-content 1fr; gap: 8px 16px; color: #4b5563; }
.meta dt { font-weight: 750; color: #2e3440; }
.meta dd { margin: 0; overflow-wrap: anywhere; }
"""


HOME_JS = """
const form = document.getElementById('job-form');
const button = document.getElementById('submit-button');
const fileInput = document.getElementById('media-file');
const urlInput = document.getElementById('media-url');
const statusEl = document.getElementById('status');
const bar = document.getElementById('bar');
const jobLink = document.getElementById('job-link');

function setStatus(text, percent) {
  statusEl.textContent = text;
  if (typeof percent === 'number') bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}
async function jsonFetch(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  button.disabled = true;
  jobLink.textContent = '';
  try {
    const mediaUrl = urlInput.value.trim();
    const file = fileInput.files[0];
    if (mediaUrl) {
      setStatus('Creating transcript job...', 5);
      const data = await jsonFetch('/jobs', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ mediaUrl })
      });
      setStatus('Job started. Redirecting...', 100);
      window.location.href = data.jobUrl;
      return;
    }
    if (!file) throw new Error('Choose a file or paste a media URL.');
    setStatus('Creating upload job...', 1);
    const created = await jsonFetch('/jobs', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ fileName: file.name, fileSize: file.size, contentType: file.type })
    });
    const job = created.job;
    jobLink.innerHTML = `Job created: <a href="${created.jobUrl}">${created.jobUrl}</a>`;
    const chunkSize = job.chunkSize;
    const totalChunks = job.totalChunks;
    for (let index = 0; index < totalChunks; index += 1) {
      const start = index * chunkSize;
      const end = Math.min(file.size, start + chunkSize);
      const chunk = file.slice(start, end);
      setStatus(`Uploading chunk ${index + 1} / ${totalChunks}...`, Math.round((index / totalChunks) * 85));
      await fetch(`/jobs/${job.jobId}/chunks/${index}`, {
        method: 'PUT',
        headers: { 'content-type': 'application/octet-stream' },
        body: chunk
      }).then(async response => {
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.error || 'Chunk upload failed');
        }
      });
    }
    setStatus('Starting transcript worker...', 92);
    const completed = await jsonFetch(`/jobs/${job.jobId}/complete`, { method: 'POST' });
    setStatus('Job started. Redirecting...', 100);
    window.location.href = completed.jobUrl;
  } catch (error) {
    setStatus(error.message, 0);
    button.disabled = false;
  }
});
"""


JOB_JS = """
const title = document.getElementById('title');
const message = document.getElementById('message');
const bar = document.getElementById('bar');
const meta = document.getElementById('meta');
const links = document.getElementById('links');

function pct(job) {
  if (job.status === 'completed') return 100;
  if (job.status === 'failed') return 100;
  if (job.status === 'processing') return 70;
  if (job.status === 'queued') return 45;
  if (job.status === 'uploading') return job.totalChunks ? Math.round((job.uploadedChunks.length / job.totalChunks) * 35) : 5;
  return 5;
}
function label(value) {
  if (!value) return '';
  return String(value).replace(/([A-Z])/g, ' $1').replace(/^./, s => s.toUpperCase());
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[char]));
}
function safeLink(url, text) {
  try {
    const parsed = new URL(String(url), window.location.href);
    if (!['http:', 'https:'].includes(parsed.protocol)) return '';
    return `<a href="${escapeHtml(parsed.href)}" target="_blank" rel="noreferrer">${escapeHtml(text)}</a>`;
  } catch {
    return '';
  }
}
function render(job) {
  title.textContent = label(job.status) || 'Job';
  message.textContent = job.message || '';
  bar.style.width = `${pct(job)}%`;
  const rows = [
    ['Job ID', escapeHtml(job.jobId)],
    ['Source', escapeHtml(job.fileName || job.mediaUrl)],
    ['Run', job.runUrl ? safeLink(job.runUrl, 'Open Apify run') : 'Not started yet'],
    ['Error', escapeHtml(job.error || '')]
  ].filter(([, value]) => value);
  meta.innerHTML = rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v}</dd>`).join('');
  const artifacts = job.artifactUrls || {};
  const names = [['mp3', 'MP3'], ['txt', 'Transcript'], ['json', 'JSON'], ['srt', 'SRT'], ['vtt', 'VTT'], ['quality.json', 'Quality'], ['zip', 'ZIP']];
  links.innerHTML = names.filter(([key]) => artifacts[key]).map(([key, text]) => safeLink(artifacts[key], text)).join('');
}
async function poll() {
  try {
    const response = await fetch(`/jobs/${JOB_ID}/status`, { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Job not found');
    render(data.job);
    if (!['completed', 'failed'].includes(data.job.status)) setTimeout(poll, 3000);
  } catch (error) {
    title.textContent = 'Could not load job';
    message.textContent = error.message;
  }
}
poll();
"""
