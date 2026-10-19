"""Local HTTP service. Generated business code is never imported or executed."""
from __future__ import annotations

import copy
import hmac
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import insert
from .ai import _confirmed_outline as confirmed_outline, _redo_text as redo_text, chat_failure, insert_failure, redo_allowed
from .contract import DesignErrors
from .jobs import Jobs
from .store import (Store, ConflictError, ProjectBroken, atomic_json, inherit_typed_fields, module_hash, design_hash,
                    normalize_module, now, uid, valid_id)

ROOT = Path(__file__).resolve().parent.parent
# A note written while its step makes a new version is redone once that version is done;
# after this long the wait is given up (said in the chat).
REDO_WAIT_SECONDS = 3 * 60 * 60
REDO_WAIT_POLL = 1.0
# Spots asked about in one go: a desk level's neighbouring cards.
MAX_INSERT_PAIRS = 200
INSERT_STALE = "你在这之后改过工作流，这份设计用不上了；可以按原话再设计一次。"
# Why a note was not redone, said in the chat as 「没能重做这一版：…」. UNFINISHED: the step's new
# version was not made, so the change it was making (a 打回 reason, an earlier note) still waits
# for 再跑一次; a note redone now would take its place.
UNFINISHED = "新的一版没做完；先点「再跑一次」把它做好，再说一次"
RESTARTED = "工作台中途关了，没等到这一步做完；要改就再说一次"
PROMISE = "这一步正在做新的一版，做完后我再按你说的重做。"
DISMISSED = "这条建议我不要了。"


def create_app(data_dir=None, *, ai_backend=None, phone_token=None):
    store = Store(Path(data_dir or os.environ.get("WORKBENCH_PROJECTS", str(ROOT / "projects"))))
    jobs = Jobs(store.root / ".jobs")

    stopping = threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        yield
        stopping.set()
        jobs.close()

    app = FastAPI(title="功能模块工作台", lifespan=lifespan)
    app.state.store = store
    app.state.jobs = jobs
    app.state.redo_wait = (REDO_WAIT_SECONDS, REDO_WAIT_POLL)

    def phone_key(request):
        """Phone review (opt-in, `--phone`): another device on the same network needs the secret key."""
        if not phone_token:
            return None
        supplied = request.query_params.get("key") or request.cookies.get("flowdesk_key") or ""
        return supplied if hmac.compare_digest(supplied.encode(), phone_token.encode()) else None

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        allowed = {"127.0.0.1", "localhost", "::1", "testserver"}
        local = request.url.hostname in allowed
        remembered = None
        if not local:
            remembered = phone_key(request)
            if remembered is None:
                return JSONResponse({"detail": "此服务仅供本机使用"}, status_code=403)
        if request.url.path.startswith("/api"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "请求来源与本地工作台不一致"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if remembered and request.query_params.get("key"):
            response.set_cookie("flowdesk_key", remembered, httponly=True, samesite="strict", max_age=30 * 86400)
        return response

    @app.exception_handler(ConflictError)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ProjectBroken)
    async def broken_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        body = {"detail": str(exc)}
        if isinstance(exc, DesignErrors):
            body["issues"] = exc.issues
        return JSONResponse(body, status_code=422)

    @app.exception_handler(FileNotFoundError)
    async def missing_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    def backend():
        if ai_backend is not None:
            return ai_backend
        from . import ai
        return ai

    def checked(project_id, body):
        p = store.load(project_id)
        if body.get("base_fingerprint") != p["fingerprint"]:
            raise ConflictError("当前文件已经变化，请重新载入后再发起任务。")
        return p

    def selected(p, module_id):
        m = next((m for m in p["modules"] if m["id"] == module_id), None)
        if not m:
            raise FileNotFoundError("模块不存在")
        return m

    @app.get("/api/health")
    def health():
        from .codex import find_codex
        from .providers import provider_status
        executable = find_codex()
        return {"ok": True, "codex_available": bool(executable), "codex_path": executable,
                "business_execution": False, **provider_status(executable)}

    @app.get("/api/ai/check")
    def ai_check():
        """Is the AI the workbench calls reachable right now? Codex is asked whether it
        is logged in (its output is never passed on); OpenAI only checks its settings."""
        from .codex import codex_login_status, find_codex
        from .providers import provider_status
        executable = find_codex()
        status = provider_status(executable)
        provider = status["ai_provider"]
        if provider == "codex":
            if executable is None:
                return {"provider": provider, "installed": False, "logged_in": None, "ok": False,
                        "message": "没找到本机 Codex。"}
            logged_in = codex_login_status(executable, timeout=15)
            message = {True: "已连上：本机 Codex 已登录。",
                       False: "本机 Codex 还没登录：在终端运行 codex login。",
                       None: "检查没完成：Codex 没正常回应。请稍后再试，或在终端运行 codex login status 看看。"}[logged_in]
            return {"provider": provider, "installed": True, "logged_in": logged_in,
                    "ok": logged_in is True, "message": message}
        if provider == "openai":
            return {"provider": provider, "installed": True, "logged_in": None,
                    "ok": status["ai_available"], "message": status["ai_message"]}
        return {"provider": provider, "installed": False, "logged_in": None, "ok": False,
                "message": status["ai_message"]}

    @app.get("/api/projects")
    def list_projects():
        return store.list()

    @app.post("/api/projects")
    def new_project(body: dict):
        return store.create(str(body.get("name", "新的功能工作台")))

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return store.load(project_id)

    @app.put("/api/projects/{project_id}")
    def save_project(project_id: str, body: dict):
        if not isinstance(body.get("project"), dict):
            raise ValueError("缺少项目内容")
        return store.save(project_id, body["project"], body.get("base_fingerprint"))

    @app.post("/api/projects/{project_id}/undo")
    def undo(project_id: str, body: dict):
        return store.undo(project_id, body.get("base_fingerprint"))

    @app.post("/api/projects/{project_id}/imports")
    async def import_files(project_id: str, files: list[UploadFile] = File(...), paths: str = Form("[]"), entry: str | None = Form(None),
                           origin: str | None = Form(None)):
        from .importer import import_snapshot, unpack_archive, MAX_FILE_BYTES, MAX_TOTAL_BYTES
        names = json.loads(paths)
        if not names:
            names = [f.filename for f in files]
        if not isinstance(names, list) or len(names) != len(files) or len(files) > 2000:
            raise ValueError("文件路径清单与上传文件不匹配，或数量超过 2000")
        payload = []
        total = 0
        for name, file in zip(names, files):
            limit = MAX_TOTAL_BYTES if len(files) == 1 and str(name).lower().endswith(".zip") else MAX_FILE_BYTES
            content = await file.read(limit + 1)
            total += len(content)
            if len(content) > limit or total > MAX_TOTAL_BYTES:
                raise ValueError("单文件最多 64MB，目录或 ZIP 最多 100MB")
            payload.append((name, content))
        if len(payload) == 1 and str(payload[0][0]).lower().endswith(".zip"):
            payload = unpack_archive(payload[0][1])
        with store.lock(project_id):
            p = store.load(project_id)
            snapshot = import_snapshot(store.directory(project_id), payload, entry)
            if origin == "request":
                snapshot["origin"] = "request"
            p["sources"].append(snapshot)
            return store.save(project_id, p, p["fingerprint"], preserve_sources=False)

    @app.get("/api/projects/{project_id}/sources/{snapshot_id}")
    def source(project_id: str, snapshot_id: str, path: str, raw: int = 0):
        from .importer import decode_text, read_source_bytes
        p = store.load(project_id)
        valid_id(snapshot_id)
        snapshot = next((s for s in p["sources"] if s["id"] == snapshot_id), None)
        if not snapshot or path not in {f["path"] for f in snapshot["files"]}:
            raise FileNotFoundError("来源文件不存在")
        contents = read_source_bytes(store.directory(project_id), snapshot_id, path)
        if raw:
            disposition = "attachment; filename*=UTF-8''" + quote(Path(path).name, safe="")
            return Response(contents, media_type="application/octet-stream", headers={"Content-Disposition": disposition})
        text = decode_text(contents)
        if text is not None:
            return {"path": path, "kind": "text", "text": text}
        return {"path": path, "kind": "binary", "text": "此资源以原始文件保存，可下载查看。"}

    # Checking whether a skeleton is already being continued or adopted, and recording
    # that it now is, must not interleave: two clicks could otherwise both pass the check.
    skeleton_lock = threading.Lock()

    def adopted(paused, project):
        """A skeleton counts as adopted while what it produced is still in the project, so an
        undo lets the user continue the same edited skeleton again."""
        if not paused or not paused.get("adopted_by"):
            return False
        ids = paused.get("adopted_modules")
        if ids is None:  # adopted before the adopted ids were recorded
            return True
        return bool(set(ids) & {m["id"] for m in project["modules"]})

    def skeleton_job(job):
        """The paused skeleton a continuation (or a resumed continuation) came from."""
        seen = set()
        while job and job["id"] not in seen:
            seen.add(job["id"])
            if job["status"] == "completed" and (job.get("result") or {}).get("stage") == "outline":
                return job
            job = jobs.get(job["resume_from"]) if job.get("resume_from") else None
        return None

    @app.post("/api/projects/{project_id}/analyze")
    def analyze(project_id: str, body: dict):
        with skeleton_lock:
            return start_analysis(project_id, body)

    def start_analysis(project_id: str, body: dict):
        p = checked(project_id, body)
        snapshot_id = body.get("snapshot_id")
        resume_dir = None
        # The skeleton stop: a first run may pause after the outline; the user's
        # edited outline then continues the same job's cached source reading.
        options = {"pause_after_outline": body.get("pause_after_outline") is True,
                   "from_request": body.get("from_request") is True}
        confirmed = body.get("outline")
        if confirmed is not None and not body.get("resume_job_id"):
            raise ValueError("确认的步骤骨架必须接着它所属的解析任务继续")
        if body.get("resume_job_id"):
            previous = jobs.get(body["resume_job_id"])
            outline_stop = previous["status"] == "completed" and (previous.get("result") or {}).get("stage") == "outline"
            if previous["project_id"] != project_id or previous["kind"] != "analyze" or not (
                    outline_stop or previous["status"] in ("failed", "cancelled", "interrupted")):
                raise ValueError("只能继续本项目等待确认骨架、或已失败、取消或中断的解析任务")
            options["from_request"] = previous.get("from_request") is True
            if outline_stop:
                if confirmed is None:
                    raise ValueError("请先确认步骤骨架，再继续补全细节")
                if adopted(previous, p):
                    raise ConflictError("这份骨架已经采用过；要再做一份请先撤销那次采用，或重新解析。")
                running = previous.get("continued_by")
                if running and jobs.get(running)["status"] in ("queued", "running"):
                    raise ConflictError("这份骨架的上一次继续仍在进行，请等它完成或取消后再改。")
                # Edits elsewhere in the project do not invalidate the skeleton: the
                # confirmed outline is re-checked against the current module IDs, and
                # pinned to the source findings the user actually saw.
                # Check the skeleton contract now, so a broken skeleton is a form error
                # listing every problem, not a job that fails later.
                findings = {item["id"]: item for item in previous["result"].get("findings") or []}
                confirmed_outline(confirmed, findings, {m["id"] for m in p["modules"]}, options["from_request"])
                options["pause_after_outline"] = False
                options["confirmed_outline"] = confirmed
                options["expected_findings"] = previous["result"].get("findings")
            else:
                if confirmed is not None:
                    raise ValueError("只有等待确认骨架的任务可以提交修改后的骨架")
                # A continuation carries its own skeleton, so unrelated edits do not
                # strand the user's corrections.
                if previous.get("confirmed_outline") is None and previous["base_fingerprint"] != p["fingerprint"]:
                    raise ConflictError("解析任务之后项目已变化，请重新发起解析；之前的结果与诊断仍保留。")
                options["pause_after_outline"] = previous.get("pause_after_outline") is True
                if previous.get("confirmed_outline") is not None:
                    paused = skeleton_job(previous)
                    if adopted(paused, p):
                        raise ConflictError("这份骨架已经采用过；要再做一份请先撤销那次采用，或重新解析。")
                    running = (paused or {}).get("continued_by")
                    if running and running != previous["id"] and jobs.get(running)["status"] in ("queued", "running"):
                        raise ConflictError("这份骨架的另一次继续仍在进行，请等它完成或取消后再试。")
                    options["pause_after_outline"] = False
                    options["confirmed_outline"] = previous["confirmed_outline"]
                    options["expected_findings"] = previous.get("expected_findings")
            resume_dir = jobs.root / previous["id"]
            coverage_path = resume_dir / "coverage.json"
            previous_snapshot = previous.get("snapshot_id")
            if coverage_path.exists():
                previous_snapshot = json.loads(coverage_path.read_text())["snapshot_id"]
            if snapshot_id and snapshot_id != previous_snapshot:
                raise ValueError("续作来源必须与原任务一致")
            snapshot_id = previous_snapshot
        if snapshot_id not in {s["id"] for s in p["sources"]}:
            raise ValueError("请选择已经导入的来源快照")
        # A typed requirement stays one wherever its analysis is started from.
        if next(s for s in p["sources"] if s["id"] == snapshot_id).get("origin") == "request":
            options["from_request"] = True
        # Only pass options that are in use, so older/injected backends keep working.
        extra = {key: value for key, value in options.items() if value not in (False, None)}
        if resume_dir is not None:
            extra["resume_dir"] = resume_dir
        def work(evidence, cancel, progress, job_id):
            result = backend().analyze(store.directory(project_id), p, snapshot_id, evidence, cancel, progress, **extra)
            return result
        job = jobs.start(project_id, None, "analyze", p["fingerprint"], work)
        jobs.update(job["id"], snapshot_id=snapshot_id, resume_from=body.get("resume_job_id"),
                    pause_after_outline=options["pause_after_outline"], from_request=options["from_request"],
                    confirmed_outline=options.get("confirmed_outline"), expected_findings=options.get("expected_findings"))
        paused = skeleton_job(jobs.get(body["resume_job_id"])) if body.get("resume_job_id") else None
        if paused is not None:
            # The paused job tracks every continuation (also resumed ones) so it is never
            # continued twice at once, and remembers the latest skeleton for re-editing.
            updates = {"continued_by": job["id"], "continuations": [*paused.get("continuations", []), job["id"]]}
            if confirmed is not None:
                updates["latest_outline"] = confirmed
            jobs.update(paused["id"], **updates)
        return jobs.get(job["id"])

    def related(p, a, b):
        """a and b are one step, or one holds the other (a group page shows an inner step's output)."""
        parents = {m["id"]: m.get("parent_id") for m in p["modules"]}

        def holds(outer, inner):
            seen = set()
            while inner and inner not in seen:
                if inner == outer:
                    return True
                seen.add(inner)
                inner = parents.get(inner)
            return False
        return b in parents and (holds(a, b) or holds(b, a))

    def same_design(proposed, stored):
        """The proposal leaves everything gui would apply as it is (all that module_hash covers too)."""
        try:
            new, old = normalize_module(proposed), normalize_module(stored)
        except (KeyError, TypeError, ValueError):
            return False
        keys = ("id", "name", "description", "format", "schema", "sample", "required", "origin", "role")
        ports = lambda m, side: [{k: port.get(k) for k in keys} for port in m[side]]
        # A plan echoed with or without its last newline is the same plan (for the hash too).
        new["plan"], old["plan"] = new["plan"].strip(), old["plan"].strip()
        return (all(new.get(key) == old.get(key) for key in ("name", "purpose", "change", "role", "acceptance",
                                                             "for_each", "needs_review"))
                and new["plan"] == old["plan"]
                and all(ports(new, side) == ports(old, side) for side in ("inputs", "outputs"))
                and design_hash(new) == design_hash(old))

    def note(project_id, module_id, job_id, content):
        store.message(project_id, {"module_id": module_id, "role": "assistant", "content": content, "job_id": job_id})

    def add_result(job_id, found):
        with jobs.lock:
            jobs.update(job_id, result={**(jobs.get(job_id)["result"] or {}), **found})

    def remember(job_id, **found):
        """Add what became of the redo to the chat job's result, once that job has its result."""
        while not stopping.is_set():
            try:
                with jobs.lock:
                    if jobs.get(job_id)["status"] not in ("queued", "running"):
                        add_result(job_id, found)
                        return
            except (FileNotFoundError, ValueError):
                return
            stopping.wait(0.05)

    # The notes waiting for a step to finish the version it is making, per (project, run, step), in
    # the order gui wrote them. One waiter per step redoes them one after another, so the newest
    # note has the last word.
    waiting = {}
    waiting_lock = threading.Lock()

    def step_failed(project_id, run_id, step_id):
        """The step's last version was not made: its job failed, was cancelled or was lost."""
        return any(row["id"] == step_id and row["state"] == "failed"
                   for row in app.state.runs.view(project_id, run_id)["steps"])

    def not_redone(count, reason):
        """One chat line for the notes that were not redone, however many there were."""
        if count == 1:
            return f"没能重做这一版：{reason}"
        return f"没能重做这一版，你前面说的 {count} 条都没改：{reason}"

    def too_long(count, reason):
        what = "没按你说的重做" if count == 1 else f"你前面说的 {count} 条都没改"
        return f"这一步做新的一版做了太久，我没等到它做完，{what}。等它做完后再说一次。"

    def give_up(key, reason, line=not_redone):
        """None of the notes still waiting for this step is redone: say so once in each chat they came
        from (after its newest note)."""
        with waiting_lock:
            given_up = waiting.pop(key, [])
        chats = {}
        for entry in given_up:
            chats.setdefault(entry["module_id"], []).append(entry)
        for module_id, notes in chats.items():
            note(key[0], module_id, notes[-1]["job_id"], line(len(notes), reason))
        for entry in given_up:
            remember(entry["job_id"], redo_refused=reason)

    def watch(key):
        project_id, run_id, step_id = key
        limit, poll = app.state.redo_wait
        deadline = time.monotonic() + limit
        while not stopping.wait(poll):  # on a stop the next start says so (settle_lost_waits)
            with waiting_lock:
                entry = waiting[key][0]
            try:
                if app.state.runs.step_busy(project_id, run_id, step_id):
                    if time.monotonic() < deadline:
                        continue
                    give_up(key, "这一步做了太久，没等到它做完", too_long)
                    return
                # The version the notes waited for was not made. Redoing them on the older one would
                # wipe out that failure, and the change it was making (a 打回 reason) with it.
                reason = UNFINISHED if step_failed(*key) else app.state.runs.redo_blocked(*key)
                if reason is None:
                    job = app.state.start_step(project_id, run_id, step_id, request=entry["result"]["redo_request"])
            except (ValueError, FileNotFoundError) as error:
                reason = str(error)
            if reason is None:
                with waiting_lock:
                    waiting[key].remove(entry)
                    done = not waiting[key]
                    if done:
                        del waiting[key]
                remember(entry["job_id"], redo_job_id=job["id"])
                if done:
                    return
                deadline = time.monotonic() + limit  # the next note waits for this redo
                continue
            try:
                if app.state.runs.step_busy(project_id, run_id, step_id):
                    continue  # a new version started meanwhile: wait for it instead
            except (ValueError, FileNotFoundError):
                pass
            give_up(key, reason)
            return

    def redo_after_run(project_id, module_id, result, job_id, *, only_behind=False):
        """Redo once the step has finished the version it is making and the notes written before
        this one are redone (in the background); the chat says so first. With only_behind, this
        happens only while such notes wait (it returns False otherwise and does nothing)."""
        key = (project_id, result["run_id"], result["step_id"])
        entry = {"written": jobs.get(job_id)["created_at"], "module_id": module_id, "result": result,
                 "job_id": job_id}
        with waiting_lock:
            queue = waiting.get(key)
            if only_behind and not queue:
                return False
            # The promise comes first in the chat, before anything the waiter may say.
            note(project_id, module_id, job_id, PROMISE)
            if queue:
                queue.append(entry)
                queue.sort(key=lambda waits: waits["written"])
                return True  # this step's waiter is already there
            waiting[key] = [entry]
        threading.Thread(target=watch, args=(key,), name="workbench-redo-wait", daemon=True).start()
        return True

    def settle_lost_waits():
        """A note still waiting when the workbench stopped is not redone: its waiter stopped with the
        workbench (and so did the version it waited for, most likely). Say so in each chat, once."""
        lost = {}
        for job in sorted(jobs.list(), key=lambda j: j["created_at"]):
            found = job.get("result") or {}
            if job["kind"] == "chat" and found.get("redo_waiting") and "redo_job_id" not in found \
                    and "redo_refused" not in found:
                lost.setdefault((job["project_id"], job["module_id"]), []).append(job)
        for (project_id, module_id), notes in lost.items():
            try:
                note(project_id, module_id, notes[-1]["id"], not_redone(len(notes), RESTARTED))
            except (OSError, ValueError):
                pass  # the project is gone
            for job in notes:
                add_result(job["id"], {"redo_refused": RESTARTED})

    settle_lost_waits()

    def start_redo(project_id, module_id, result, job_id):
        """Redo the version on screen as the chat asked. Returns (job, None, False) when it started,
        (None, None, True) when it waits for the step to finish the version it is making, or for the
        notes written before it (said in the chat), or (None, the plain reason, False); a refusal is
        also said in the step's chat."""
        # Notes written earlier still wait for this step (it may be free for a moment before its
        # waiter looks again): this one goes after them, so the newest note has the last word.
        if redo_after_run(project_id, module_id, result, job_id, only_behind=True):
            return None, None, True
        try:
            reason, waits = app.state.runs.redo_status(project_id, result["run_id"], result["step_id"])
            if reason is None and app.state.runs.retry_request(project_id, result["run_id"], result["step_id"]):
                # The step's new version failed while making a change (a 打回 reason, an earlier note)
                # that 再跑一次 redoes. This redo would take its place and hide the failure.
                reason = UNFINISHED
            if reason is None:
                return app.state.start_step(project_id, result["run_id"], result["step_id"],
                                            request=result["redo_request"]), None, False
        except (ValueError, FileNotFoundError) as error:
            reason, waits = str(error), set()
            try:  # the step began a new version in between: follow it
                if app.state.runs.step_busy(project_id, result["run_id"], result["step_id"]):
                    waits = {"busy"}
            except (ValueError, FileNotFoundError):
                pass
        if waits == {"busy"}:
            redo_after_run(project_id, module_id, result, job_id)
            return None, None, True
        note(project_id, module_id, job_id, f"没能重做这一版：{reason}")
        return None, reason, False

    def redo_outcome(job, reason, waiting, *, job_key):
        if job:
            return {job_key: job if job_key == "redo_job" else job["id"]}
        return {"redo_waiting": True} if waiting else {"redo_refused": reason}

    @app.post("/api/projects/{project_id}/modules/{module_id}/chat")
    def chat(project_id: str, module_id: str, body: dict):
        p = checked(project_id, body)
        selected(p, module_id)
        message = str(body.get("message", "")).strip()
        if not message:
            raise ValueError("请写下你希望讨论或修改的内容")
        # The output on the step page, when there is one: the chat may ask to redo it.
        run_id, step_id = body.get("run_id") or None, body.get("step_id") or module_id
        version = body.get("version")
        run_output = None
        if run_id is not None:
            valid_id(run_id)
            valid_id(step_id)
            if version is not None:
                # The version on screen (an older tab may be open); a redo still starts from the newest.
                if isinstance(version, str) and version.isdigit():
                    version = int(version)
                if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                    raise ValueError("看的这一版的编号不对")
            if not related(p, module_id, step_id):
                raise ValueError("这份产物不是这一步的")
            try:
                run_output = app.state.runs.output_for_chat(project_id, run_id, step_id, version=version, project=p)
            except (FileNotFoundError, KeyError, OSError, ValueError):
                run_output = None  # an unreadable version: talk about the step without it
            if run_output and step_id != module_id:
                # A group's page shows an inner step's version: that step is made by its own 做法.
                run_output["made_in"] = selected(p, step_id).get("name") or step_id
        history = store.messages(project_id, module_id)
        store.message(project_id, {"module_id": module_id, "role": "user", "content": message})
        def work(evidence, cancel, progress, job_id):
            # Only pass what is in use, so older/injected backends keep working.
            extra = {"run_output": run_output} if run_output else {}
            try:
                result = backend().chat(store.directory(project_id), p, module_id, message, history, evidence, cancel,
                                        progress, **extra)
                # A reply without the key (an older or injected backend) asks for no redo.
                allowed = redo_allowed(run_output, result.get("module"), selected(p, module_id))
                result["redo_request"] = redo_text(result.get("redo_request")) if allowed else None
            except Exception as error:
                if cancel.is_set():
                    raise
                # gui sees one plain line, here and in the chat; the details stay on the job.
                plain = chat_failure(error)
                jobs.update(job_id, error_detail=str(error)[:4000])
                store.message(project_id, {"module_id": module_id, "role": "assistant", "content": plain,
                                           "job_id": job_id, "failed": True})
                raise RuntimeError(plain) from error
            if cancel.is_set():
                return result
            if run_output:
                result.update(run_id=run_id, step_id=step_id)
            store.message(project_id, {"module_id": module_id, "role": "assistant", "content": result["reply"], "job_id": job_id})
            # Nothing to apply first: redo now (or once the step is free). Otherwise the redo waits for the apply.
            if result["redo_request"] and not result.get("confirmed_links") and not result.get("connect") \
                    and same_design(result["module"], selected(p, module_id)):
                result.update(redo_outcome(*start_redo(project_id, module_id, result, job_id), job_key="redo_job_id"))
            return result
        return jobs.start(project_id, module_id, "chat", p["fingerprint"], work)

    def insert_limits(project_id):
        """What the run settings keep a new step out of: groups run as one step, inputs a step does without."""
        settings = app.state.runs.settings(project_id)
        ignored = {mid: item.get("ignore_inputs") or [] for mid, item in settings["steps"].items()}
        return app.state.runs.units(project_id), ignored

    @app.post("/api/projects/{project_id}/insert/spots")
    def insert_spots(project_id: str, body: dict):
        """Where a step can go between two neighbouring cards, and why not elsewhere. Reads only."""
        pairs = body.get("pairs")
        if not isinstance(pairs, list) or len(pairs) > MAX_INSERT_PAIRS or not all(
                isinstance(pair, list) and len(pair) == 2 and all(isinstance(mid, str) for mid in pair)
                for pair in pairs):
            raise ValueError(f"要查的位置不对：最多 {MAX_INSERT_PAIRS} 处，每处是前后两步")
        p = store.load(project_id)
        units, ignored = insert_limits(project_id)
        found = []
        for after, before in pairs:
            row = {"after": after, "before": before, "ok": True, "why": None}
            try:
                s = insert.spot(p, after, before, units=units, ignored=ignored)
            except insert.InsertRefused as refused:
                found.append({**row, "ok": False, "why": str(refused), "choices": [], "handed": None, "reads": None,
                              "code": None, "kept": []})
                continue
            found.append({**row, **{key: s[key] for key in ("choices", "handed", "reads", "code", "kept")}})
        return found

    @app.post("/api/projects/{project_id}/insert")
    def insert_step(project_id: str, body: dict):
        """Ask the AI to design one step between two cards; nothing changes until it is applied."""
        p = checked(project_id, body)
        request = body.get("request")
        request = request.strip() if isinstance(request, str) else ""
        if not request:
            raise ValueError("先写一句这一步做什么")
        if len(request) > 300:
            raise ValueError("这句话太长了，用一两句话说这一步做什么")
        after, before, edge_id = body.get("after"), body.get("before"), body.get("edge_id")
        if not isinstance(after, str) or not isinstance(before, str) or not isinstance(edge_id, (str, type(None))):
            raise ValueError("先选好在哪两步之间加")
        design = getattr(backend(), "insert_step", None)
        if design is None:  # an injected backend from before inserts
            raise ValueError("这里的 AI 还不会加一步")
        units, ignored = insert_limits(project_id)
        s = insert.spot(p, after, before, edge_id=edge_id, units=units, ignored=ignored)
        if s["edge_id"] is None:
            raise ValueError("先选好新的一步接在哪样东西上")

        def work(evidence, cancel, progress, job_id):
            try:
                result = design(store.directory(project_id), p, s, request, evidence, cancel, progress)
            except Exception as error:
                if cancel.is_set():
                    raise
                # gui sees one plain line; the details stay on the job.
                jobs.update(job_id, error_detail=str(error)[:4000])
                raise RuntimeError(insert_failure(error)) from error
            atomic_json(evidence / "insert-result.json", result)
            return result

        # The check, the start and the job's own record in one go: one insert per project at a time.
        with jobs.lock:
            if any(j["kind"] == "insert" and j["status"] in ("queued", "running") for j in jobs.list(project_id)):
                raise ConflictError("已经有一步在设计，等它好了或取消后再加")
            job = jobs.start(project_id, None, "insert", p["fingerprint"], work)
            # gui's own choice of handoff (null when there was only one), kept to design it again.
            jobs.update(job["id"], insert={"after": after, "before": before, "edge_id": edge_id, "request": request,
                                           "code": s["code"]})
            return jobs.get(job["id"])

    @app.post("/api/projects/{project_id}/modules/{module_id}/decompose")
    def decompose(project_id: str, module_id: str, body: dict):
        p = checked(project_id, body)
        selected(p, module_id)
        if any(m.get("parent_id") == module_id for m in p["modules"]):
            raise ValueError("已有子步骤，请进入后细化其中一步，避免覆盖现有设计。")
        def work(evidence, cancel, progress, job_id):
            result = backend().decompose_module(p, module_id, store.directory(project_id), evidence, progress, cancel)
            atomic_json(evidence / "decomposition-result.json", result)
            return result
        return jobs.start(project_id, module_id, "decompose", p["fingerprint"], work)

    @app.post("/api/projects/{project_id}/modules/{module_id}/generate")
    def generate(project_id: str, module_id: str, body: dict):
        from .exporter import check_code
        p = checked(project_id, body)
        module = selected(p, module_id)
        if any(m.get("parent_id") == module_id for m in p["modules"]):
            raise ValueError("此模块包含子步骤，请进入并选择具体步骤生成实现；组合关系仍是设计草稿。")
        if module.get("execution_kind") in ("human", "composite"):
            raise ValueError("用户决定或组合模块不能直接生成叶子实现；请选中承担具体处理的程序或 AI 步骤。")
        if not module["purpose"].strip() or not module["plan"].strip() or not module["inputs"] or not module["outputs"]:
            raise ValueError("生成前请补全模块目的、输入、输出和处理 plan")
        if any(not port["name"].strip() or not port["description"].strip() or port["format"] == "any" for port in module["inputs"] + module["outputs"]):
            raise ValueError("生成前请给每项输入输出填写名称、说明，并确定格式")

        def has_json_contract(schema):
            # Store validation already checks JSON Schema syntax. This gate asks
            # whether it actually declares a shape/value contract, rather than
            # accepting a description, example, or an empty object as a design.
            if not isinstance(schema, dict) or not schema:
                return False
            if schema.get("type") or schema.get("properties") or schema.get("enum") or "const" in schema:
                return True
            if has_json_contract(schema.get("items")):
                return True
            return any(has_json_contract(part) for keyword in ("oneOf", "anyOf", "allOf", "prefixItems")
                       for part in schema.get(keyword, []))

        unspecified = [f"{label}「{port['name']}」（{port['id']}）"
                       for side, label in (("inputs", "输入"), ("outputs", "输出"))
                       for port in module[side]
                       if port["format"] == "json" and not has_json_contract(port.get("schema"))]
        if unspecified:
            raise ValueError("生成前请明确 JSON 结构：" + "、".join(unspecified)
                             + "。请填写 type、properties、oneOf 等具体约定；仅有说明或样例不够。现有设计仍可保存。")
        def work(evidence, cancel, progress, job_id):
            result = backend().generate(store.directory(project_id), p, module_id, evidence, cancel, progress)
            atomic_json(evidence / "generation-result.json", result)
            errors = check_code(result["code"], execution_kind=selected(p, module_id).get("execution_kind"))
            if errors:
                raise ValueError("生成代码未通过静态检查：" + "；".join(errors))
            if cancel.is_set():
                return result
            with store.lock(project_id):
                current = store.load(project_id)
                if current["fingerprint"] != p["fingerprint"]:
                    raise ConflictError("生成期间设计已变化。代码已保留在任务诊断中，未覆盖当前实现；请按新版重新生成。")
                if cancel.is_set():
                    return result
                target = selected(current, module_id)
                target["generation"] = {"id": uid("generation"), "code": result["code"], "plan_hash": module_hash(target), "status": "generated", "created_at": now(), "notes": result.get("notes", ""), "step_map": result.get("step_map", [])}
                saved = store.save(project_id, current, current["fingerprint"])
                result["applied_revision"] = saved["revision"]
            return result
        return jobs.start(project_id, module_id, "generate", p["fingerprint"], work)

    @app.get("/api/projects/{project_id}/messages")
    def messages(project_id: str, module_id: str | None = None):
        store.load(project_id)
        return store.messages(project_id, module_id)

    @app.post("/api/projects/{project_id}/apply")
    def apply(project_id: str, body: dict):
        with skeleton_lock:
            return apply_result(project_id, body)

    def apply_result(project_id: str, body: dict):
        p = checked(project_id, body)
        j = jobs.get(body.get("job_id", ""))
        if j["project_id"] != project_id or j["status"] != "completed" or not j["result"]:
            raise ValueError("此任务没有可应用的结果")
        if j["result"].get("stage") == "outline":
            raise ValueError("这只是步骤骨架；请先确认骨架，补全细节后再应用")
        if j["base_fingerprint"] != p["fingerprint"]:
            raise ConflictError(INSERT_STALE if j["kind"] == "insert"
                                else "此建议基于旧版本。请重新讨论或解析，已有建议仍保留。")
        if j.get("dismissed_at"):
            raise ConflictError("这条建议你已经不要了；想改的话再跟 AI 说一次。")
        if j["kind"] == "chat":
            proposed = copy.deepcopy(j["result"]["module"])
            old = selected(p, j["module_id"])
            proposed.update(id=old["id"], parent_id=old["parent_id"], generation=old["generation"], provenance="user")
            if "boundary_bindings" in old:
                proposed["boundary_bindings"] = copy.deepcopy(old["boundary_bindings"])
            else:
                proposed.pop("boundary_bindings", None)
            if "execution_kind" in old:
                proposed["execution_kind"] = old["execution_kind"]
            else:
                proposed.pop("execution_kind", None)
            inherit_typed_fields(proposed, old)
            # The chat checked its own change: the step keeps the review mark it proposed.
            keep_review = {old["id"]}
            p["modules"] = [proposed if m["id"] == old["id"] else m for m in p["modules"]]
            # Links into this step the chat checked still fit. Only a link that still
            # comes into this step and still waits for review is cleared; others are ignored.
            confirmed = {link for link in j["result"].get("confirmed_links") or [] if isinstance(link, str)}
            for edge in p["edges"]:
                if edge["id"] in confirmed and edge["target"] == old["id"] and edge.get("needs_review"):
                    edge["needs_review"] = False
            # Lines the chat drew into this step (checked when it answered): each goes in only while
            # its input still exists and nothing feeds it, and its source output is still there.
            drawn = [link for link in j["result"].get("connect") or [] if isinstance(link, dict)]
            if drawn:
                outputs = {(m["id"], port["id"]) for m in p["modules"] for port in m.get("outputs", [])}
                inputs = {port["id"] for port in proposed.get("inputs", [])}
                lined = {(e["target"], e.get("target_port")) for e in p["edges"] if e.get("kind") == "data"}
                for link in drawn:
                    if (link.get("target_port") not in inputs or (link.get("source"), link.get("source_port")) not in outputs
                            or (old["id"], link["target_port"]) in lined):
                        raise ConflictError("这条建议要接的线已经接不上了（那一步或那样东西变了）；再跟 AI 说一次。")
                    p["edges"].append({"id": uid("edge"), "source": link["source"], "target": old["id"],
                                       "source_port": link["source_port"], "target_port": link["target_port"],
                                       "kind": "data", "usage": "handoff", "selection": "", "label": "",
                                       "needs_review": False})
                    lined.add((old["id"], link["target_port"]))
        elif j["kind"] == "decompose":
            old = selected(p, j["module_id"])
            if any(m.get("parent_id") == old["id"] for m in p["modules"]):
                raise ValueError("已有子步骤，不能覆盖现有设计")
            modules = copy.deepcopy(j["result"]["modules"])
            edges = copy.deepcopy(j["result"]["edges"])
            parent = next((m for m in modules if m["id"] == old["id"]), None)
            if not parent or parent["inputs"] != old["inputs"] or parent["outputs"] != old["outputs"]:
                raise ValueError("细化必须保留父模块的输入输出约定")
            children = [m for m in modules if m["id"] != old["id"]]
            if len(children) < 2 or any(m.get("parent_id") != old["id"] for m in children):
                raise ValueError("细化需要至少两个直接子步骤")
            ids = {m["id"] for m in children}
            if len(ids) != len(children) or any(e["source"] not in ids or e["target"] not in ids for e in edges):
                raise ValueError("细化的连接只能位于新子步骤之间")
            existing = {m["id"] for m in p["modules"]}
            mapping = {m["id"]: uid("module") if m["id"] in existing else m["id"] for m in children}
            for child in children:
                child.update(id=mapping[child["id"]], generation=None)
            for e in edges:
                e.update(id=uid("edge"), source=mapping[e["source"]], target=mapping[e["target"]])
            if "boundary_bindings" not in parent and "boundary_bindings" in old:
                parent["boundary_bindings"] = copy.deepcopy(old["boundary_bindings"])
            for binding in parent.get("boundary_bindings", []):
                binding["child_module"] = mapping.get(binding["child_module"], binding["child_module"])
            parent.update(parent_id=old["parent_id"], generation=old["generation"])
            # Adopting a decomposition explicitly makes this node a container;
            # legacy proposals cannot retain a leaf's program/AI label by accident.
            parent["execution_kind"] = "composite"
            if parent.get("generation"):
                parent["generation"]["status"] = "stale"
            p["modules"] = [parent if m["id"] == old["id"] else m for m in p["modules"]] + children
            p["edges"].extend(edges)
            p["warnings"].extend(j["result"].get("warnings", []))
        elif j["kind"] == "analyze":
            paused = skeleton_job(j)
            if adopted(paused, p):
                raise ConflictError("这份骨架已经采用过；同一份骨架不能同时采用两次。")
            modules = copy.deepcopy(j["result"]["modules"])
            edges = copy.deepcopy(j["result"]["edges"])
            existing = {m["id"] for m in p["modules"]}
            mapping = {m["id"]: uid("module") if m["id"] in existing else m["id"] for m in modules}
            for m in modules:
                m["id"] = mapping[m["id"]]
                if m.get("parent_id"):
                    m["parent_id"] = mapping[m["parent_id"]]
                m["generation"] = None
                for binding in m.get("boundary_bindings", []):
                    binding["child_module"] = mapping.get(binding["child_module"], binding["child_module"])
            for e in edges:
                e.update(id=uid("edge"), source=mapping[e["source"]], target=mapping[e["target"]])
            p["modules"].extend(modules)
            p["edges"].extend(edges)
            if j["result"].get("workflow"):
                if p.get("workflow"):
                    p["warnings"].append("新分析包含启动与交付建议，已有项目约定保持不变；可在任务结果中核对后编辑起点与交付。")
                else:
                    workflow = copy.deepcopy(j["result"]["workflow"])
                    for binding in workflow["entry_points"] + workflow["delivery"]["ports"]:
                        if binding["module_id"] not in mapping:
                            raise ValueError("工作流边界引用了分析结果之外的步骤")
                        binding["module_id"] = mapping[binding["module_id"]]
                    p["workflow"] = workflow
            p["warnings"].extend(j["result"].get("warnings", []))
        elif j["kind"] == "insert":
            designed = j["result"]
            # Run settings are not in the fingerprint: the spot is checked again.
            units, ignored = insert_limits(project_id)
            s = insert.spot(p, designed["after"], designed["before"], edge_id=designed["edge_id"], units=units,
                            ignored=ignored)
            edge = next(e for e in p["edges"] if e["id"] == s["edge_id"])
            # Always a fresh id: an undone insert leaves its chat lines and run records under the old one.
            added = copy.deepcopy(designed["module"])
            added["id"] = uid("module")
            new_edge = uid("edge")
            trial = insert.place(p, added, edge, new_edge)
            if insert.new_issues(p, trial, added, edge, new_edge):
                raise ValueError("这份设计加进去会让工作流多出问题，什么都没动；可以按原话再设计一次。")
            p = trial
        else:
            raise ValueError("此任务无需手动应用")
        result = store.save(project_id, p, p["fingerprint"], keep_review=keep_review if j["kind"] == "chat" else ())
        jobs.update(j["id"], applied_revision=result["revision"])
        if j["kind"] == "analyze" and paused is not None:
            jobs.update(paused["id"], adopted_by=j["id"], adopted_modules=sorted(mapping.values()))
        if j["kind"] == "insert":
            # The new step's page starts with gui's sentence and the AI's reply.
            asked_for = (j.get("insert") or {}).get("request")
            if asked_for:
                store.message(project_id, {"module_id": added["id"], "role": "user", "content": asked_for})
            note(project_id, added["id"], j["id"], j["result"]["reply"])
            result["inserted_module_id"] = added["id"]
        asked = j["result"] if j["kind"] == "chat" else {}
        tried = any(key in asked for key in ("redo_job_id", "redo_refused", "redo_waiting"))
        if asked.get("redo_request") and asked.get("run_id") and asked.get("step_id") and not tried:
            # The chat asked for a redo after this change: start it now, with the new design
            # (start_redo checks again: a new 做法 lifts the reject stop).
            outcome = start_redo(project_id, j["module_id"], asked, j["id"])
            add_result(j["id"], redo_outcome(*outcome, job_key="redo_job_id"))
            result.update(redo_outcome(*outcome, job_key="redo_job"))
        return result

    @app.post("/api/projects/{project_id}/jobs/{job_id}/dismiss")
    def dismiss_proposal(project_id: str, job_id: str):
        """gui does not want a step chat's proposal: it stops waiting, and the chat says so, so the
        AI knows next time."""
        j = jobs.get(job_id)
        if j["project_id"] != project_id or j["kind"] != "chat" or j["status"] != "completed" or not j["result"]:
            raise ValueError("这不是一条等你看的建议")
        if j.get("applied_revision"):
            raise ConflictError("这条建议已经用上了；要退回，点右上角项目名，选「撤销上一次修改」。")
        if j.get("dismissed_at"):
            return j
        jobs.update(job_id, dismissed_at=now())
        store.message(project_id, {"module_id": j["module_id"], "role": "user", "content": DISMISSED,
                                   "job_id": job_id, "dismissed": True})
        return jobs.get(job_id)

    @app.get("/api/jobs")
    def list_jobs(project_id: str | None = None):
        return jobs.list(project_id)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return jobs.get(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        return jobs.cancel(job_id)

    @app.get("/api/projects/{project_id}/export")
    def export(project_id: str, module_id: str | None = None):
        from .exporter import export_project
        p = store.load(project_id)
        if module_id:
            selected(p, module_id)
        archive = export_project(store.directory(project_id), p, module_id)
        return Response(archive, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{module_id or project_id}.zip"'})

    from .run_api import register as register_runs
    register_runs(app, store, jobs, backend)

    ui = ROOT / "ui" / "dist"
    if ui.exists():
        app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    else:
        @app.get("/")
        def setup_page():
            return {"message": "前端尚未构建，请按 README 执行启动脚本。", "api": "/docs"}
    return app
