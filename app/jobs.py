"""任务存储与处理流水线（线程安全，后台定期清理过期任务）。"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from . import anonymizer
from .config import settings


@dataclass
class Job:
    id: str
    source_name: str
    secret_fingerprint: str
    shift_dates: bool
    created_at: float
    ttl: int
    upload_path: str
    extract_dir: str
    output_dir: str
    zip_path: str
    date_shift_min: int = -365
    date_shift_max: int = 365
    status: str = "ready"
    result: Optional[anonymizer.JobResult] = None
    error: Optional[str] = None
    _zip_lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._zip_lock = threading.Lock()

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl


class JobRegistry:
    def __init__(self, work_dir: str, ttl_seconds: int) -> None:
        self.work_dir = work_dir
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def setup(self) -> None:
        if os.path.isdir(self.work_dir):
            shutil.rmtree(self.work_dir, ignore_errors=True)
        os.makedirs(self.work_dir, exist_ok=True)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def create_and_process(
        self,
        upload_path: str,
        source_name: str,
        secret: str,
        shift_dates: bool,
        date_shift_min: int = -365,
        date_shift_max: int = 365,
    ) -> Job:
        job_id = uuid.uuid4().hex
        base = os.path.join(self.work_dir, job_id)
        job = Job(
            id=job_id,
            source_name=os.path.basename(source_name),
            secret_fingerprint=hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12],
            shift_dates=shift_dates,
            date_shift_min=date_shift_min,
            date_shift_max=date_shift_max,
            created_at=time.time(),
            ttl=self.ttl_seconds,
            upload_path=os.path.join(base, "input.zip"),
            extract_dir=os.path.join(base, "extracted"),
            output_dir=os.path.join(base, "anonymized"),
            zip_path=os.path.join(base, "anonymized.zip"),
        )
        os.makedirs(base, exist_ok=True)
        shutil.move(upload_path, job.upload_path)
        try:
            os.makedirs(job.extract_dir, exist_ok=True)
            members = anonymizer.safe_extract_zip(
                job.upload_path,
                job.extract_dir,
                max_total_bytes=settings.max_extracted_bytes,
                max_files=settings.max_extracted_files,
            )
            engine = anonymizer.Anonymizer(
                secret=secret,
                shift_dates=shift_dates,
                date_shift_min=date_shift_min,
                date_shift_max=date_shift_max,
            )
            job.result = engine.run(job.extract_dir, members, job.output_dir)
        except ValueError as exc:
            job.status = "failed"
            job.error = str(exc)
        except Exception as exc:  # pragma: no cover - 兜底
            job.status = "failed"
            job.error = f"处理失败：{exc}"
        with self._lock:
            self._jobs[job_id] = job
        return job

    def ensure_zip(self, job: Job) -> Optional[str]:
        if job.status != "ready" or job.result is None:
            return None
        with job._zip_lock:
            if not os.path.exists(job.zip_path):
                anonymizer.build_zip(job.result, job.zip_path)
        return job.zip_path

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            expired = [jid for jid, job in self._jobs.items() if now > job.expires_at]
            for jid in expired:
                self._jobs.pop(jid)
                shutil.rmtree(
                    os.path.join(self.work_dir, jid), ignore_errors=True
                )

    async def purge_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                self.purge_expired()
            except Exception:
                pass


registry = JobRegistry(settings.work_dir, settings.job_ttl_seconds)
