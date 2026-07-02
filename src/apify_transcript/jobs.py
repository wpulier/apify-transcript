from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apify_client import ApifyClient

from .utils import slugify


JOB_STORE_NAME = "large-video-to-transcript-jobs"
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    return secrets.token_urlsafe(18)


def validate_job_id(job_id: str) -> str:
    if not JOB_ID_PATTERN.match(job_id):
        raise ValueError("invalid job id")
    return job_id


def job_key(job_id: str) -> str:
    return f"jobs/{validate_job_id(job_id)}.json"


def chunk_key(job_id: str, index: int) -> str:
    return f"uploads/{validate_job_id(job_id)}/chunks/{index:06d}"


@dataclass
class JobStore:
    client: Any
    store_id: str | None = None
    store_name: str = JOB_STORE_NAME

    @classmethod
    def from_token(cls, token: str, store_name: str = JOB_STORE_NAME) -> "JobStore":
        if not token:
            raise RuntimeError("APIFY_TOKEN is required for Standby jobs")
        apify_client = ApifyClient(token)
        store = apify_client.key_value_stores().get_or_create(name=store_name)
        return cls(apify_client.key_value_store(store["id"]), store_id=store["id"], store_name=store_name)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        record = self.client.get_record(job_key(job_id))
        if not record:
            return None
        value = record["value"]
        if isinstance(value, bytes):
            return json.loads(value.decode("utf-8"))
        if isinstance(value, str):
            return json.loads(value)
        return dict(value)

    def save_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job["updatedAt"] = utc_now()
        self.client.set_record(job_key(job["jobId"]), job, content_type="application/json; charset=utf-8")
        return job

    def patch_job(self, job_id: str, **updates: Any) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise KeyError(f"job not found: {job_id}")
        job.update(updates)
        return self.save_job(job)

    def set_chunk(self, job_id: str, index: int, content: bytes) -> None:
        self.client.set_record(chunk_key(job_id, index), content, content_type="application/octet-stream")

    def get_chunk(self, job_id: str, index: int) -> bytes:
        record = self.client.get_record_as_bytes(chunk_key(job_id, index))
        if not record:
            raise FileNotFoundError(f"missing chunk {index}")
        return record["value"]


def create_url_job(media_url: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "jobId": new_job_id(),
        "createdAt": now,
        "updatedAt": now,
        "sourceType": "url",
        "status": "created",
        "mediaUrl": media_url,
        "message": "Job created.",
        "runId": None,
        "runUrl": None,
        "result": None,
        "artifactUrls": {},
        "error": None,
    }


def create_upload_job(file_name: str, file_size: int, content_type: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> dict[str, Any]:
    if file_size <= 0:
        raise ValueError("file size must be greater than zero")
    if chunk_size <= 0:
        raise ValueError("chunk size must be greater than zero")
    total_chunks = (file_size + chunk_size - 1) // chunk_size
    now = utc_now()
    return {
        "jobId": new_job_id(),
        "createdAt": now,
        "updatedAt": now,
        "sourceType": "upload",
        "status": "uploading",
        "fileName": Path(file_name).name,
        "fileSize": file_size,
        "contentType": content_type or "application/octet-stream",
        "chunkSize": chunk_size,
        "totalChunks": total_chunks,
        "uploadedChunks": [],
        "uploadedBytes": 0,
        "message": "Upload started.",
        "runId": None,
        "runUrl": None,
        "result": None,
        "artifactUrls": {},
        "error": None,
    }


def record_uploaded_chunk(job: dict[str, Any], index: int, chunk_bytes: int) -> dict[str, Any]:
    total_chunks = int(job.get("totalChunks") or 0)
    if index < 0 or index >= total_chunks:
        raise ValueError("chunk index is out of range")
    uploaded = set(int(value) for value in job.get("uploadedChunks", []))
    if index not in uploaded:
        uploaded.add(index)
        job["uploadedBytes"] = int(job.get("uploadedBytes") or 0) + chunk_bytes
    job["uploadedChunks"] = sorted(uploaded)
    job["message"] = f"Uploaded {len(uploaded)} / {total_chunks} chunks."
    return job


def upload_is_complete(job: dict[str, Any]) -> bool:
    total_chunks = int(job.get("totalChunks") or 0)
    uploaded = set(int(value) for value in job.get("uploadedChunks", []))
    return total_chunks > 0 and uploaded == set(range(total_chunks))


def worker_input_for_job(job: dict[str, Any], store_name: str = JOB_STORE_NAME) -> dict[str, Any]:
    standby_job = {"jobId": job["jobId"], "storeName": store_name}
    if job["sourceType"] == "url":
        return {"media": [job["mediaUrl"]], "standbyJob": standby_job}
    return {
        "media": [f"ingested://{job['jobId']}/{job['fileName']}"],
        "ingestedMedia": {
            "jobId": job["jobId"],
            "storeName": store_name,
            "fileName": job["fileName"],
            "contentType": job.get("contentType") or "application/octet-stream",
            "totalChunks": job["totalChunks"],
        },
        "standbyJob": standby_job,
    }


def materialize_ingested_media(spec: dict[str, Any], target_dir: Path, apify_token: str) -> Path:
    store_name = str(spec.get("storeName") or JOB_STORE_NAME)
    job_id = validate_job_id(str(spec["jobId"]))
    file_name = Path(str(spec["fileName"])).name
    total_chunks = int(spec["totalChunks"])
    if total_chunks <= 0:
        raise ValueError("ingestedMedia.totalChunks must be greater than zero")
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file_name).suffix
    output_path = target_dir / f"{job_id}_{slugify(Path(file_name).stem, 'media')}{suffix}"
    store = JobStore.from_token(apify_token, store_name)
    with output_path.open("wb") as output:
        for index in range(total_chunks):
            output.write(store.get_chunk(job_id, index))
    return output_path
