"""Authenticated Streamlit host for the existing local application.

The component is a transport only. Requests run through FastAPI in-process, with
the same source validation, conflict checks and persistent job queue as localhost.
This module can be tested without importing Streamlit.
"""
from __future__ import annotations

import atexit
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from fastapi.testclient import TestClient

from .app import ROOT, create_app
from .cloud_usage import archive_known_usage

MAX_BODY_BYTES = 100 * 1024 * 1024
MAX_BATCH = 24
MAX_READ_CACHE_BYTES = 8 * 1024 * 1024
ID = r"[A-Za-z0-9_-]{1,160}"
RPC_ID = r"[A-Za-z0-9_:-]{1,160}"
CONFIG_KEYS = ("OPENAI_API_KEY", "WORKBENCH_OPENAI_MODEL", "WORKBENCH_AI_PROVIDER", "WORKBENCH_OPENAI_TIMEOUT",
               "WORKBENCH_GITHUB_TOKEN", "WORKBENCH_BACKUP_REPO")
_ENV_ORIGINAL = {}
# POSTs that only read: not recorded as writes and no backup.
_READ_POSTS = re.compile(rf"/api/projects/{ID}/insert/spots")
_ROUTES = (
    ("GET", r"/api/health", set()),
    ("GET", r"/api/ai/check", set()),
    ("GET", r"/api/projects", set()),
    ("POST", r"/api/projects", set()),
    ("GET", rf"/api/projects/{ID}", set()),
    ("PUT", rf"/api/projects/{ID}", set()),
    ("POST", rf"/api/projects/{ID}/(?:undo|imports|analyze|apply)", set()),
    ("POST", rf"/api/projects/{ID}/modules/{ID}/(?:chat|decompose|generate)", set()),
    ("POST", rf"/api/projects/{ID}/insert(?:/spots)?", set()),
    ("GET", rf"/api/projects/{ID}/sources/{ID}", {"path", "raw"}),
    ("GET", rf"/api/projects/{ID}/messages", {"module_id"}),
    ("GET", rf"/api/projects/{ID}/export", {"module_id"}),
    ("GET", r"/api/jobs", {"project_id"}),
    ("GET", rf"/api/jobs/{ID}", set()),
    ("POST", rf"/api/jobs/{ID}/cancel", set()),
)


def password_matches(candidate: str, configured: str) -> bool:
    """Do not authenticate an empty configuration, even with an empty candidate."""
    return bool(configured) and hmac.compare_digest(candidate.encode(), configured.encode())


def access_mode(password: str, configured_mode: str, *, local_development=False) -> str:
    if password:
        return "password"
    if local_development:
        return "local_development"
    if configured_mode == "streamlit_private":
        return "streamlit_private"
    return "closed"


def configure_environment(secrets: dict, *, local_development=False):
    """Only pass approved server configuration; removed secrets do not linger."""
    for key in CONFIG_KEYS:
        if key not in _ENV_ORIGINAL:
            _ENV_ORIGINAL[key] = os.environ.get(key)
        value = secrets.get(key, _ENV_ORIGINAL[key])
        if key == "WORKBENCH_AI_PROVIDER" and not value:
            value = "codex" if local_development else "disabled"
        if value:
            os.environ[key] = str(value)
        else:
            os.environ.pop(key, None)


def response_message(request_id: str, status: int, detail: str) -> dict:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
    return {"id": request_id, "status": status, "headers": {"content-type": "application/json"},
            "body_base64": base64.b64encode(body).decode()}


def validate_request(request: dict) -> tuple[str, str, dict]:
    if not isinstance(request, dict) or set(request) - {"id", "method", "path", "body", "form"}:
        raise ValueError("请求格式不合法")
    if not isinstance(request.get("id"), str) or not re.fullmatch(RPC_ID, request["id"]):
        raise ValueError("请求标识不合法")
    method, path = request.get("method"), request.get("path")
    if not isinstance(path, str) or len(path) > 16384 or "\\" in path or any(ord(c) < 32 for c in path):
        raise ValueError("接口路径不合法")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith("/api/"):
        raise ValueError("只允许访问工作台 API")
    route = next((query for verb, pattern, query in _ROUTES
                  if method == verb and re.fullmatch(pattern, parsed.path)), None)
    if route is None or any(key not in route for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("接口或参数不在允许范围内")
    if "body" in request and "form" in request:
        raise ValueError("JSON 与文件表单不能同时提交")
    if method == "GET" and ("body" in request or "form" in request):
        raise ValueError("读取请求不能包含正文")
    if len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode()) > MAX_BODY_BYTES * 4 // 3 + 65536:
        raise ValueError("上传请求超过 100MB")
    kwargs = {}
    if "body" in request:
        if not isinstance(request["body"], dict):
            raise ValueError("JSON 正文必须是对象")
        if len(json.dumps(request["body"], ensure_ascii=False, allow_nan=False).encode()) > MAX_BODY_BYTES:
            raise ValueError("JSON 正文超过 100MB")
        kwargs["json"] = request["body"]
    if "form" in request:
        if method != "POST" or not parsed.path.endswith("/imports"):
            raise ValueError("只有资料导入允许文件表单")
        form = request["form"]
        if not isinstance(form, list) or len(form) > 2002:
            raise ValueError("文件表单数量不合法")
        data, files, size = {}, [], 0
        for field in form:
            if not isinstance(field, dict):
                raise ValueError("文件表单不合法")
            if "filename" in field:
                if set(field) - {"name", "filename", "content_type", "data_base64"} or field.get("name") != "files":
                    raise ValueError("上传文件字段不合法")
                if not isinstance(field["filename"], str) or not isinstance(field.get("data_base64"), str):
                    raise ValueError("文件名与内容必须是字符串")
                content_type = field.get("content_type", "application/octet-stream")
                if not isinstance(content_type, str) or "\r" in content_type or "\n" in content_type:
                    raise ValueError("文件类型不合法")
                try:
                    content = base64.b64decode(field["data_base64"], validate=True)
                except (ValueError, binascii.Error):
                    raise ValueError("文件内容不是合法 Base64") from None
                files.append(("files", (field["filename"], content, content_type)))
                size += len(content)
            else:
                if set(field) != {"name", "value"} or field.get("name") not in ("paths", "entry") or not isinstance(field["value"], str):
                    raise ValueError("文件表单文本字段不合法")
                if field["name"] in data:
                    raise ValueError("文件表单文本字段重复")
                data[field["name"]] = field["value"]
                size += len(field["value"].encode())
            if size > MAX_BODY_BYTES:
                raise ValueError("上传请求超过 100MB")
        kwargs.update(data=data, files=files)
    return method, path, kwargs


def prepare_cloud_data(destination: Path, source: Path | None = None) -> Path:
    """Seed an independent directory once; preserve job evidence, never its lease.

    An existing destination is authoritative. In particular a restart does not
    overwrite cloud edits with the Git version. Symbolic links are not followed.
    """
    destination = Path(destination).resolve()
    source = Path(source or ROOT / "projects").resolve()
    if destination == source or source in destination.parents:
        raise ValueError("线上资料目录必须独立于仓库原始项目")
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".seed-" + uuid.uuid4().hex)
    try:
        def ignore(directory, names):
            return [name for name in names if name in {"service.lock", "__pycache__", ".DS_Store"}
                    or (Path(directory) / name).is_symlink()]
        if source.exists():
            shutil.copytree(source, staging, ignore=ignore)
        else:
            staging.mkdir()
        try:
            staging.rename(destination)
        except FileExistsError:
            pass
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


class StreamlitRuntime:
    """One cached application per cloud data directory; sessions share its jobs."""

    def __init__(self, projects: Path, *, ai_backend=None, on_mutation: Callable | None = None, backup_debounce: float = 10):
        self.projects = Path(projects).resolve()
        self.app = create_app(self.projects, ai_backend=ai_backend)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.lock = threading.RLock()
        self.on_mutation = on_mutation
        self.last_backup_error: str | None = None
        self.last_backup_at: str | None = None
        self._terminal_seen = {(j["id"], j.get("updated_at")) for j in self.app.state.jobs.list()
                               if j["status"] not in ("queued", "running")}
        self._reads = OrderedDict()
        self._read_bytes = 0
        self._closed = False
        self._ledger = sqlite3.connect(self.projects.parent / "streamlit-rpc.sqlite3", check_same_thread=False)
        self._ledger.execute("CREATE TABLE IF NOT EXISTS requests (session TEXT, id TEXT, digest TEXT, response TEXT, PRIMARY KEY(session,id))")
        self._ledger.commit()
        self._backup_condition = threading.Condition()
        self._backup_pending = None
        self._backup_running = False
        self._backup_stopping = False
        self._backup_due = 0.0
        self._backup_debounce = backup_debounce
        self._backup_thread = threading.Thread(target=self._backup_worker, name="workbench-cloud-backup", daemon=True)
        self._backup_thread.start()
        atexit.register(self.close)

    def close(self):
        with self._backup_condition:
            self._backup_stopping = True
            self._backup_pending = None
            self._backup_condition.notify_all()
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self.client.__exit__(None, None, None)
            self._ledger.close()

    @contextmanager
    def snapshot_guard(self):
        """Hold local writes only while copying a coherent snapshot, never Git I/O."""
        with self.lock, ExitStack() as stack:
            for project in sorted(self.app.state.store.list(), key=lambda p: p["id"]):
                stack.enter_context(self.app.state.store.lock(project["id"]))
            with self.app.state.jobs.lock:
                yield

    @property
    def backup_state(self):
        with self._backup_condition:
            if self._backup_running:
                return "saving"
            if self._backup_pending:
                return "pending"
            if self.last_backup_error:
                return "failed"
            return "saved" if self.last_backup_at else "idle"

    def _backup_worker(self):
        while True:
            with self._backup_condition:
                while not self._backup_stopping:
                    if self._backup_pending:
                        delay = self._backup_due - time.monotonic()
                        if delay <= 0:
                            break
                        self._backup_condition.wait(timeout=delay)
                    else:
                        self._backup_condition.wait()
                if self._backup_stopping:
                    return
                callback, request, response = self._backup_pending
                self._backup_pending = None
                self._backup_running = True
            try:
                callback(self, request, response)
                error = None
            except Exception:
                error = "本次改动已写入工作台，云端备份未完成。请重试保存快照。"
            with self._backup_condition:
                self.last_backup_error = error
                if error is None:
                    self.last_backup_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                self._backup_running = False
                self._backup_condition.notify_all()

    def _queue_backup(self, callback, request, response, *, immediate=False):
        with self._backup_condition:
            if self._backup_stopping:
                return
            self._backup_pending = (callback, request, response)
            self._backup_due = time.monotonic() + (0 if immediate else self._backup_debounce)
            self._backup_condition.notify_all()

    def request_snapshot(self, callback):
        self._queue_backup(lambda runtime, *_: callback(runtime), None, None, immediate=True)

    def wait_for_backup(self, timeout=5):
        """For shutdown/verification callers; the component request path never waits."""
        deadline = time.monotonic() + timeout
        with self._backup_condition:
            while self._backup_pending or self._backup_running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._backup_condition.wait(timeout=remaining)
            return True

    def _notify_mutation(self, request, response):
        if self.on_mutation:
            self._queue_backup(self.on_mutation, request, response)

    def _observe_jobs(self, request, response) -> bool:
        """Notify once when polling discovers a newly persisted terminal job."""
        if request["method"] != "GET" or not request["path"].startswith("/api/jobs") or response["status"] != 200:
            return False
        try:
            payload = json.loads(base64.b64decode(response["body_base64"]))
        except (ValueError, TypeError):
            return False
        changed = False
        for job in payload if isinstance(payload, list) else [payload]:
            if not isinstance(job, dict) or "id" not in job or job.get("status") in ("queued", "running", None):
                continue
            key = (job["id"], job.get("updated_at"))
            if key not in self._terminal_seen:
                changed = True
                self._terminal_seen.add(key)
        return changed

    def dispatch(self, session: str, request: dict) -> dict:
        request_id = request.get("id", "invalid") if isinstance(request, dict) else "invalid"
        try:
            method, path, kwargs = validate_request(request)
            if not re.fullmatch(RPC_ID, session):
                raise ValueError("会话标识不合法")
        except (ValueError, TypeError, OverflowError):
            return response_message(str(request_id)[:160], 400, "请求不符合工作台接口约定")
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        key = (session, request_id)
        with self.lock:
            if self._closed:
                return response_message(request_id, 503, "工作台正在重启，请刷新后重试")
            stored = self._ledger.execute("SELECT digest,response FROM requests WHERE session=? AND id=?", key).fetchone()
            if stored is None:
                stored = self._reads.get(key)
            if stored is not None:
                old_digest, response = stored
                if digest != old_digest:
                    return response_message(request_id, 409, "相同请求标识对应的内容已变化，请重新发起操作")
                if response is None:
                    return response_message(request_id, 409, "先前请求执行结果尚未确认；请先刷新检查当前内容，再决定是否重新操作")
                return json.loads(response)
            mutation = method != "GET" and not _READ_POSTS.fullmatch(urlsplit(path).path)
            if mutation:
                # Commit before dispatch. A crash during the operation leaves an
                # uncertain request, which may be inspected but never replayed.
                self._ledger.execute("INSERT INTO requests VALUES (?,?,?,NULL)", (*key, digest))
                self._ledger.commit()
            try:
                result = self.client.request(method, path, **kwargs)
                response = {"id": request_id, "status": result.status_code,
                            "headers": {name: value for name, value in result.headers.items()
                                        if name.lower() in {"content-type", "content-disposition"}},
                            "body_base64": base64.b64encode(result.content).decode()}
            except Exception:
                # Do not expose server paths, environment or provider secrets.
                response = response_message(request_id, 500, "请求处理失败，请检查当前内容后重试；已有设计仍保留")
            serialized = json.dumps(response, ensure_ascii=False)
            if mutation:
                self._ledger.execute("UPDATE requests SET response=? WHERE session=? AND id=?", (serialized, *key))
                self._ledger.commit()
                if 200 <= response["status"] < 300:
                    self._notify_mutation(request, response)
            else:
                size = len(serialized.encode())
                if size <= MAX_READ_CACHE_BYTES:
                    self._reads[key] = (digest, serialized)
                    self._read_bytes += size
                    while len(self._reads) > 128 or self._read_bytes > MAX_READ_CACHE_BYTES:
                        _, (_, expired) = self._reads.popitem(last=False)
                        self._read_bytes -= len(expired.encode())
                if self._observe_jobs(request, response):
                    self._notify_mutation(request, response)
            return response

    def process(self, session: str, value) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, dict) or set(value) != {"requests"} or not isinstance(value["requests"], list):
            return [response_message("invalid", 400, "组件请求格式不合法")]
        if len(value["requests"]) > MAX_BATCH:
            return [response_message(str(r.get("id", "invalid"))[:160], 429, "并发请求过多，请稍后重试")
                    for r in value["requests"][:MAX_BATCH] if isinstance(r, dict)]
        wire_bytes, decoded_bytes = 0, 0
        try:
            for request in value["requests"]:
                size = len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode())
                wire_bytes += size
                decoded_bytes += size
                for field in request.get("form", []) if isinstance(request, dict) else []:
                    data = field.get("data_base64") if isinstance(field, dict) else None
                    if isinstance(data, str):
                        padding = 2 if data.endswith("==") else 1 if data.endswith("=") else 0
                        decoded_bytes += len(data) * 3 // 4 - padding - len(data.encode())
                if wire_bytes > MAX_BODY_BYTES * 4 // 3 + 65536 or decoded_bytes > MAX_BODY_BYTES:
                    return [response_message(str(r.get("id", "invalid"))[:160], 413, "整批请求超过 100MB，请减少同时上传的内容")
                            for r in value["requests"] if isinstance(r, dict)]
        except (ValueError, TypeError):
            return [response_message("invalid", 400, "组件请求格式不合法")]
        return [self.dispatch(session, request) for request in value["requests"]]


def open_cloud_runtime(directory: Path, *, on_restore=None, source=None):
    """A failed remote restore must not fall back to a stale repository seed."""
    if not Path(directory).exists() and on_restore:
        on_restore(Path(directory))
    projects = prepare_cloud_data(Path(directory), source=source)
    archive_known_usage(projects)
    return StreamlitRuntime(projects)


def render_app(*, on_mutation=None, on_snapshot=None, on_restore=None, backup_configured=None):
    """UI entry point. Hooks receive runtime (and request/response for mutations)."""
    import streamlit as st
    import streamlit.components.v1 as components

    st.set_page_config(page_title="Flowdesk · 功能模块工作台", page_icon="🌊", layout="wide", initial_sidebar_state="collapsed")
    st.markdown("<style>.stMainBlockContainer,.block-container{padding:3.75rem 0.5rem 0.5rem;max-width:100%;}</style>", unsafe_allow_html=True)

    def setting(name, default=""):
        try:
            value = st.secrets.get(name, os.environ.get(name, default))
        except Exception:
            value = os.environ.get(name, default)
        return str(value) if value is not None else default

    password = setting("WORKBENCH_PASSWORD")
    local_development = os.environ.get("WORKBENCH_STREAMLIT_LOCAL") == "1"
    mode = access_mode(password, setting("WORKBENCH_ACCESS_MODE"), local_development=local_development)
    if mode == "closed":
        st.title("Flowdesk · 线上工作台")
        st.info("请先在应用 Secrets 中配置 WORKBENCH_PASSWORD，再打开工作台。未配置访问密码前不会加载项目资料。")
        st.stop()
    stamp = hashlib.sha256(password.encode()).hexdigest()
    if password and st.session_state.get("_workbench_auth") != stamp:
        st.title("登录 Flowdesk")
        def authenticate():
            candidate = st.session_state.pop("_workbench_password", "")
            if password_matches(candidate, password):
                st.session_state["_workbench_auth"] = stamp
                st.session_state.pop("_workbench_login_error", None)
            else:
                st.session_state["_workbench_login_error"] = True
        with st.form("workbench_login"):
            st.text_input("访问密码", type="password", key="_workbench_password")
            st.form_submit_button("进入工作台", on_click=authenticate)
        if st.session_state.get("_workbench_login_error"):
            st.error("密码不正确。")
        st.stop()

    # Provider and backup configuration is never a component argument. Preserve
    # native environment defaults, but clear secrets removed in later reruns.
    try:
        server_secrets = {key: st.secrets[key] for key in CONFIG_KEYS if key in st.secrets}
    except Exception:
        server_secrets = {}
    configure_environment(server_secrets, local_development=local_development)
    try:
        backup_enabled = bool(backup_configured and backup_configured())
    except Exception:
        st.error("云端快照配置不完整或无效。请检查备份仓库与令牌配置后重试；没有用仓库旧快照覆盖云端资料。")
        st.stop()

    @st.cache_resource(show_spinner="正在准备工作台…")
    def runtime_for(directory):
        return open_cloud_runtime(Path(directory), on_restore=on_restore if backup_enabled else None)

    try:
        runtime = runtime_for(setting("WORKBENCH_CLOUD_PROJECTS", str(ROOT / ".cloud-data" / "projects")))
    except Exception:
        st.error("工作台资料加载失败。请检查云端快照权限或服务状态后重试；没有用仓库旧快照覆盖云端资料。")
        st.stop()
    runtime.on_mutation = on_mutation if backup_enabled else None
    if "_workbench_session" not in st.session_state:
        st.session_state["_workbench_session"] = uuid.uuid4().hex
    if local_development:
        st.caption("本地开发模式 · 请勿将 WORKBENCH_STREAMLIT_LOCAL=1 用于线上部署")

    def backup_status_text():
        return {"pending": "改动已保存，等待同步云端快照。", "saving": "正在后台保存云端快照…", "failed": "云端快照未完成，请打开侧栏重试。",
                "saved": f"最近保存：{runtime.last_backup_at}", "idle": "编辑后与 AI 任务完成后保存。"}[runtime.backup_state]

    if not backup_enabled:
        st.caption("尚未启用云端持久备份，修改后请导出项目保存。")
    else:
        @st.fragment(run_every=5)
        def show_backup_status():
            # Only this caption refreshes, preserving the component iframe and
            # its in-progress edits while a background snapshot is uploading.
            st.caption("云端快照 · " + backup_status_text())
        show_backup_status()
    with st.sidebar:
        st.title("Flowdesk")
        if mode == "streamlit_private":
            st.caption("使用 Streamlit 私有应用访问控制")
        st.caption("项目编辑与 AI 设计任务共用同一工作台。业务代码不会在导入或预览时执行。")
        if not backup_enabled:
            st.warning("当前修改只保存在运行中的云主机；请导出项目备份，重建可能恢复为仓库快照。")
        else:
            st.caption("已配置云端快照。" + backup_status_text())
        if backup_enabled and on_snapshot and st.button("保存云端快照", use_container_width=True):
            runtime.request_snapshot(on_snapshot)
            st.info("已加入云端快照保存队列。")
        if runtime.last_backup_error:
            st.warning(runtime.last_backup_error)
        if password and st.button("退出登录", use_container_width=True):
            for key in ("_workbench_auth", "_workbench_session", "_workbench_responses", "workbench_component"):
                st.session_state.pop(key, None)
            st.rerun()

    frontend = ROOT / "streamlit_frontend"
    if not (frontend / "index.html").exists():
        st.error("线上前端尚未构建，请先运行 pnpm build:streamlit。")
        st.stop()
    component = components.declare_component("flowdesk", path=str(frontend))
    value = component(responses=st.session_state.get("_workbench_responses", []), transport="streamlit",
                      protocol=1, key="workbench_component", default=None)
    responses = runtime.process(st.session_state["_workbench_session"], value)
    if responses != st.session_state.get("_workbench_responses", []):
        st.session_state["_workbench_responses"] = responses
        st.rerun()
