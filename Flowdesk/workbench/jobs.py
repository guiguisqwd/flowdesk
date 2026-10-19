"""Small persistent local job queue; only design-time AI tasks are scheduled."""
from __future__ import annotations

import copy
import fcntl
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .store import atomic_json, now, uid, valid_id


class Jobs:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lease = (self.root / "service.lock").open("a+")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lease.close()
            raise RuntimeError("该资料目录已有工作台服务运行。请打开原服务，或用 --data-dir 指定独立目录。") from exc
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workbench-design")
        self.lock = threading.RLock()
        self.cancels = {}
        self.records = {}
        for path in self.root.glob("*/job.json"):
            try:
                record = json.loads(path.read_text())
                if record["status"] in ("queued", "running"):
                    record.update(status="interrupted", error="服务重启，任务已中断；已保存的设计和诊断仍在。", updated_at=now())
                    atomic_json(path, record)
                self.records[record["id"]] = record
            except (ValueError, KeyError):
                continue

    def get(self, job_id):
        with self.lock:
            valid_id(job_id)
            if job_id not in self.records:
                raise FileNotFoundError("任务不存在")
            return copy.deepcopy(self.records[job_id])

    def list(self, project_id=None):
        with self.lock:
            return sorted([copy.deepcopy(j) for j in self.records.values() if not project_id or j["project_id"] == project_id], key=lambda j: j["created_at"], reverse=True)

    def update(self, job_id, **updates):
        with self.lock:
            self.records[job_id].update(updates, updated_at=now())
            atomic_json(self.root / job_id / "job.json", self.records[job_id])

    def start(self, project_id, module_id, kind, fingerprint, callback, job_id=None):
        job_id = valid_id(job_id) if job_id else uid("job")
        record = {"id": job_id, "project_id": project_id, "module_id": module_id, "kind": kind, "base_fingerprint": fingerprint, "status": "queued", "progress": "等待开始", "result": None, "error": None, "created_at": now(), "updated_at": now()}
        cancel = threading.Event()
        with self.lock:
            self.records[job_id] = record
            self.cancels[job_id] = cancel
            atomic_json(self.root / job_id / "job.json", record)

        def work():
            if cancel.is_set():
                self.update(job_id, status="cancelled", progress="已取消")
                with self.lock:
                    self.cancels.pop(job_id, None)
                return
            self.update(job_id, status="running", progress="准备上下文")
            try:
                result = callback(self.root / job_id, cancel, lambda message: self.update(job_id, progress=str(message)), job_id)
                cancelled = cancel.is_set() and not result.get("applied_revision")
                final = (result or {}).get("final_progress") or "已完成"
                self.update(job_id, result=result, status="cancelled" if cancelled else "completed", progress="已取消" if cancelled else final)
            except Exception as exc:
                self.update(job_id, status="cancelled" if cancel.is_set() else "failed", error=str(exc), progress="已取消" if cancel.is_set() else "处理失败，设计未被覆盖")
            finally:
                with self.lock:
                    self.cancels.pop(job_id, None)

        self.executor.submit(work)
        return self.get(job_id)

    def cancel(self, job_id):
        with self.lock:
            self.get(job_id)
            if job_id in self.cancels:
                self.cancels[job_id].set()
                self.update(job_id, progress="正在取消…")
        return self.get(job_id)

    def close(self):
        with self.lock:
            for event in self.cancels.values():
                event.set()
        # Keep the directory lease until cancelled callbacks stop writing.
        self.executor.shutdown(wait=True, cancel_futures=True)
        if not self._lease.closed:
            fcntl.flock(self._lease, fcntl.LOCK_UN)
            self._lease.close()
