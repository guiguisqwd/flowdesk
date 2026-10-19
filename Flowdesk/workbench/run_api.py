"""HTTP routes for runs: run one step, review it, ask for a redo, rerun what is stale."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from .runs import RunError, Runs, STATE_LABELS, steps_of
from .store import uid

ADVANCE_LIMIT = 60
# A step's output or attachment opened in its own tab (a web page, a picture with scripts) runs
# sandboxed: no scripts on the workbench's origin, so it cannot call the workbench. <img> still shows
# it. A PDF is left out: browsers refuse to show a PDF in a sandboxed page, and its viewer is not
# the workbench's origin anyway.


def sandboxed(media):
    headers = {"X-Content-Type-Options": "nosniff"}
    if not media.startswith("application/pdf"):
        headers["Content-Security-Policy"] = "sandbox"
    return headers


def register(app, store, jobs, backend):
    def ai_call(*args, **kwargs):
        call = getattr(backend(), "step_call", None)
        if call is None:
            from .runs import default_ai_call
            call = default_ai_call
        return call(*args, **kwargs)

    def job_status(job_id):
        try:
            return jobs.get(job_id)["status"]
        except (FileNotFoundError, ValueError):
            return None

    runs = Runs(store, ai_call=ai_call, job_status=job_status)
    app.state.runs = runs

    def start_step(project_id, run_id, step_id, request=None):
        # Running a step whose revision failed redoes that revision.
        request = request or runs.retry_request(project_id, run_id, step_id)
        job_id = uid("job")
        project, module = runs.begin(project_id, run_id, step_id, job_id, request=request)

        def work(evidence, cancel, progress, _job):
            result = runs.execute(project_id, run_id, step_id, evidence, cancel, progress, request=request,
                                  project=project, module=module)
            if not cancel.is_set() and (result.get("status") == "approved"
                                        or runs.carried(project_id, run_id, step_id)):
                # Approved by itself (no review point here): the run carries on from it, as after 通过, so
                # what it replaced below is redone too; and so it does when the step itself came out made from
                # an upstream version approved while it ran. The step itself is done whatever happens next.
                try:
                    result["carried_on"] = advance_run(project_id, run_id, carry_on=True, after=step_id)
                except Exception as error:
                    result["carried_on"] = {"job": None, "refused": {"step": None, "reason": str(error)},
                                            "waiting": None}
            return result
        return jobs.start(project_id, step_id, "revise" if request else "run_step", project["fingerprint"], work,
                          job_id=job_id)
    app.state.start_step = start_step

    @app.get("/review", include_in_schema=False)
    def review_page():
        """Old 审核台 links: reviewing now happens on the 产出链 itself (the key is kept as a cookie)."""
        return RedirectResponse("/", status_code=303)

    @app.get("/api/templates")
    def list_templates():
        from .blueprints import listing
        return listing()

    @app.post("/api/templates/{template_id}")
    def from_template(template_id: str, body: dict | None = None):
        from .blueprints import create
        return create(store, runs, template_id, (body or {}).get("name"))

    @app.post("/api/projects/{project_id}/duplicate")
    def duplicate_project(project_id: str, body: dict | None = None):
        from .blueprints import duplicate
        return duplicate(store, runs, project_id, (body or {}).get("name"))

    @app.get("/api/projects/{project_id}/export-skills")
    def export_skills(project_id: str):
        from fastapi.responses import Response
        from .skill_export import export_skills as build
        data = build(store, project_id)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(project_id)}-skills.zip"})

    @app.get("/api/run-states")
    def run_states():
        return STATE_LABELS

    @app.get("/api/projects/{project_id}/steps")
    def list_steps(project_id: str):
        project = store.load(project_id)
        settings = runs.settings(project_id)
        return [{"id": m["id"], "name": m["name"], "parent_id": m.get("parent_id"),
                 "setting": runs.step_setting(settings, m)} for m in steps_of(project, runs.units(project_id))]

    @app.get("/api/projects/{project_id}/run-settings")
    def get_settings(project_id: str):
        store.load(project_id)
        return runs.settings(project_id)

    @app.put("/api/projects/{project_id}/run-settings")
    def put_settings(project_id: str, body: dict):
        return runs.save_settings(project_id, body)

    @app.get("/api/projects/{project_id}/runs")
    def list_runs(project_id: str):
        return runs.list(project_id)

    @app.post("/api/projects/{project_id}/runs")
    def create_run(project_id: str, body: dict):
        return runs.create(project_id, body.get("title"), request=body.get("request"), inputs=body.get("inputs"),
                           due_at=body.get("due_at"))

    @app.get("/api/projects/{project_id}/runs/{run_id}")
    def get_run(project_id: str, run_id: str):
        return runs.view(project_id, run_id)

    @app.get("/api/projects/{project_id}/chain")
    def chain(project_id: str, run: str | None = None):
        """产出链: the latest run (or ``run``) summarised per step and per group, for the home cards."""
        return runs.chain(project_id, run)

    @app.get("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/detail")
    def step_detail(project_id: str, run_id: str, step_id: str):
        return runs.step_detail(project_id, run_id, step_id)

    @app.get("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/versions/{version}/diff/{port_id}")
    def diff(project_id: str, run_id: str, step_id: str, version: int, port_id: str, against: int | None = None):
        return runs.diff(project_id, run_id, step_id, version, port_id, against)

    @app.post("/api/projects/{project_id}/runs/{run_id}/request")
    def set_request(project_id: str, run_id: str, body: dict):
        if body.get("request") in (None, ""):
            raise RunError("请写下这次要做什么")
        runs.set_request(project_id, run_id, body["request"])
        return runs.view(project_id, run_id)

    @app.post("/api/projects/{project_id}/runs/{run_id}/inputs/{key}")
    async def provide_input(project_id: str, run_id: str, key: str, request: Request):
        kind = request.headers.get("content-type", "")
        if kind.startswith("multipart/"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise RunError("请选择一个文件")
            data = await upload.read()
            return runs.provide_input(project_id, run_id, key, data, filename=Path(upload.filename or "").name or None)
        body = await request.json()
        if body.get("snapshot_id"):
            return runs.provide_from_source(project_id, run_id, key, body["snapshot_id"],
                                            body.get("paths") or str(body.get("path", "")))
        return runs.provide_input(project_id, run_id, key, str(body.get("text", "")), filename=body.get("filename"))

    @app.get("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/versions")
    def versions(project_id: str, run_id: str, step_id: str):
        runs.view(project_id, run_id)
        return runs.versions(project_id, run_id, step_id)

    @app.get("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/versions/{version}/files/{port_id}")
    def artifact_file(project_id: str, run_id: str, step_id: str, version: int, port_id: str, download: int = 0):
        path, out = runs.content(project_id, run_id, step_id, version, port_id)
        media = mimetypes.guess_type(out["file"])[0] or "application/octet-stream"
        if media.startswith("text/") or out["file"].endswith((".md", ".json", ".txt")):
            media = (media if media.startswith("text/") else "text/plain") + "; charset=utf-8"
            if out["file"].endswith(".json"):
                media = "application/json; charset=utf-8"
        disposition = "attachment" if download else "inline"
        return FileResponse(path, media_type=media, headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(out['file'])}", **sandboxed(media)})

    @app.get("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/versions/{version}/attachments/{port_id}/{index}")
    def attachment_file(project_id: str, run_id: str, step_id: str, version: int, port_id: str, index: int):
        path, out = runs.content(project_id, run_id, step_id, version, port_id)
        names = out.get("attachments", [])
        if not 0 <= index < len(names):
            raise FileNotFoundError("没有这个附件")
        target = path.parent / names[index]
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(target, media_type=media, headers=sandboxed(media))

    @app.post("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/run")
    def run_step(project_id: str, run_id: str, step_id: str):
        return start_step(project_id, run_id, step_id)

    @app.post("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/revise")
    def revise(project_id: str, run_id: str, step_id: str, body: dict):
        text = str(body.get("request", "")).strip()
        if not text:
            raise RunError("请写下要怎么改")
        project = store.load(project_id)
        module = next((m for m in steps_of(project, runs.units(project_id)) if m["id"] == step_id), None)
        if module and runs.step_setting(runs.settings(project_id), module)["kind"] == "human":
            raise RunError("这一步由你来做：直接在产物里写新的一版")
        return start_step(project_id, run_id, step_id, request=text)

    @app.post("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/decision")
    def decide(project_id: str, run_id: str, step_id: str, body: dict):
        outcome = body.get("outcome")
        redo_step = body.get("redo_step") or None
        view = runs.decide(project_id, run_id, step_id, outcome, body.get("comment", ""), body.get("checked"),
                           body.get("version"), redo_step=redo_step)
        job = None
        row = next(r for r in view["steps"] if r["id"] == step_id)
        comment = str(body.get("comment", "")).strip()
        if outcome == "reject" and redo_step:
            # 打回到上游: the upstream step redoes with this reason; this step reruns once it is approved.
            try:
                job = start_step(project_id, run_id, redo_step, request=f"下游「{row['name']}」打回：{comment}")
            except RunError as error:
                runs.drop_rerun(project_id, run_id, step_id)
                return {"run": runs.view(project_id, run_id), "job": None, "redo_refused": str(error)}
            view = runs.view(project_id, run_id)
        elif outcome == "reject" and body.get("redo", True) and not row["needs_method_change"]:
            try:
                job = start_step(project_id, run_id, step_id, request="打回原因：" + comment)
            except RunError as error:
                return {"run": view, "job": None, "redo_refused": str(error)}
            view = runs.view(project_id, run_id)
        return {"run": view, "job": job}

    @app.put("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/edit")
    def edit(project_id: str, run_id: str, step_id: str, body: dict):
        return runs.edit(project_id, run_id, step_id, body.get("outputs"), body.get("comment", ""))

    @app.post("/api/projects/{project_id}/runs/{run_id}/steps/{step_id}/select")
    def select(project_id: str, run_id: str, step_id: str, body: dict):
        return runs.select(project_id, run_id, step_id, int(body.get("version", 0)))

    @app.post("/api/projects/{project_id}/runs/{run_id}/advance")
    def advance(project_id: str, run_id: str, body: dict | None = None):
        """跑到下一个审核点: run steps in order until one needs a person, fails, or ``until`` is done.

        ``carry_on`` (通过 on a step page, with ``after`` = the step just approved): start only steps that
        never ran, were sent back, or were made from versions an approval above them replaced, and answer
        {"job", "refused", "waiting"} instead of an error, since the approval itself has already been saved.
        ``waiting`` is the step the run waits at when nothing starts (null once every step is approved).
        """
        body = body or {}
        carry_on = bool(body.get("carry_on"))
        return advance_run(project_id, run_id, until=body.get("until"), only_stale=bool(body.get("only_stale")),
                           carry_on=carry_on, after=(body.get("after") or None) if carry_on else None)

    def advance_run(project_id, run_id, *, until=None, only_stale=False, carry_on=False, after=None):
        project = store.load(project_id)
        if after:
            runs.mark_carry(project_id, run_id, after, project)
        first = runs.next_step(project_id, run_id, only_stale=only_stale, fresh_only=carry_on, project=project)
        if first is None:
            if carry_on:
                return {"job": None, "refused": None, "waiting": runs.waiting_at(project_id, run_id, project)}
            raise RunError("没有可以往下跑的步骤：要么都在等你审，要么在等你提供材料")
        job_id = uid("job")
        try:
            retry = runs.retry_request(project_id, run_id, first)
            runs.begin(project_id, run_id, first, job_id, request=retry)
        except RunError as error:
            if carry_on:
                runs.drop_carry(project_id, run_id, first)
                return {"job": None, "refused": {"step": first, "reason": str(error)}, "waiting": first}
            raise

        def work(evidence, cancel, progress, _job):
            done, step, count, request, refused = [], first, 0, retry, None
            while step and not cancel.is_set() and count < ADVANCE_LIMIT:
                count += 1
                if count > 1:
                    request = runs.retry_request(project_id, run_id, step)
                    try:
                        runs.begin(project_id, run_id, step, job_id, request=request)
                    except RunError as error:
                        # e.g. a material could not be prepared: stop here and say why; the run now asks for it.
                        refused = {"step": step, "reason": str(error)}
                        if carry_on:
                            runs.drop_carry(project_id, run_id, step)
                        progress(f"停在这里：{error}")
                        break
                try:
                    result = runs.execute(project_id, run_id, step, Path(evidence) / step, cancel, progress,
                                          request=request)
                except Exception:
                    # Said on the job, so the page that follows it can go to the step that failed.
                    jobs.update(job_id, failed_step=step)
                    raise
                done.append(result)
                if step == until:
                    break
                if result["status"] == "approved":
                    if carry_on:
                        # Approved without a stop (no review point): what it replaced below is redone as well.
                        runs.mark_carry(project_id, run_id, step)
                elif not (carry_on and runs.carried(project_id, run_id, step)):
                    # It waits for the person, unless an upstream was approved again while it was being made:
                    # then it is redone from that first.
                    break
                step = runs.next_step(project_id, run_id, only_stale=only_stale, fresh_only=carry_on)
            outcome = {"steps": done, "stopped_at": done[-1]["step"] if done else None, "refused": refused}
            if carry_on:
                # Ended on a step approved without a stop: the page goes on to where the run waits for you.
                ended = bool(done) and not refused and done[-1]["status"] == "approved"
                outcome["waiting"] = runs.waiting_at(project_id, run_id) if ended else None
            if refused:
                outcome["final_progress"] = f"停在这里：{refused['reason']}"
            return outcome
        job = jobs.start(project_id, first, "advance", project["fingerprint"], work, job_id=job_id)
        return {"job": job, "refused": None, "waiting": None} if carry_on else job

    @app.post("/api/projects/{project_id}/runs/{run_id}/rerun")
    def rerun(project_id: str, run_id: str, body: dict | None = None):
        body = body or {}
        return runs.rerun_from(project_id, run_id, body.get("from_step"), body.get("title"))

    @app.get("/api/projects/{project_id}/inbox")
    def inbox(project_id: str):
        return runs.inbox(project_id)

    @app.get("/api/inbox")
    def inbox_all():
        result = []
        for project in store.list():
            if project.get("error"):
                continue
            boxes = runs.inbox(project["id"])
            if any(boxes.values()):
                result.append({"project": project["id"], "name": project["name"], **boxes})
        return result

    @app.get("/api/projects/{project_id}/stats")
    def stats(project_id: str):
        return runs.stats(project_id)

    return runs
