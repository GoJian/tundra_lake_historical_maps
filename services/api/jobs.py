"""Minimal in-process async job store for long-running segmentation runs.

Fine for a small self-hosted team. For multi-worker/production, swap for Redis +
a task queue (RQ/Celery). A single GPU lock serialises SAM inference.
"""

from __future__ import annotations

import uuid
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

_GPU_LOCK = threading.Lock()


@dataclass
class Job:
    id: str
    status: str = "queued"          # queued | running | done | error
    result: Optional[Any] = None
    error: Optional[str] = None
    progress: str = ""


class JobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self) -> Job:
        job = Job(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, jid: str) -> Optional[Job]:
        return self._jobs.get(jid)

    def run(self, job: Job, fn: Callable[[Job], Any], serialize_gpu: bool = True) -> None:
        """Execute ``fn(job)`` in a background thread, updating job state."""
        def _target():
            job.status = "running"
            try:
                if serialize_gpu:
                    with _GPU_LOCK:
                        job.result = fn(job)
                else:
                    job.result = fn(job)
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                job.status = "error"
                job.error = f"{e}\n{traceback.format_exc()[-800:]}"

        threading.Thread(target=_target, daemon=True).start()


STORE = JobStore()
