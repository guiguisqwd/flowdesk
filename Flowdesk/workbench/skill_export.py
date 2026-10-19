"""Export each step as an Agent Skills folder (SKILL.md + assets), readable by Claude Code and Codex.

The step's purpose, plan and acceptance become the skill's instructions. What it reads and makes
go in frontmatter ``metadata`` so a host can wire steps the way Flowdesk does. Output contracts go
in ``assets/<output>.schema.json``. A top-level README lists the order and the review points.
"""
from __future__ import annotations

import io
import json
import re
import zipfile

from .runs import Runs, produced, steps_of

ORIGIN = {"upstream": "前一步的产出", "request": "启动请求", "resource": "资料", "user": "你在运行时提供"}


def skill_name(module_id):
    name = re.sub(r"[^a-z0-9-]+", "-", module_id.lower()).strip("-") or "step"
    return name[:64]


def _yaml_text(value):
    return json.dumps(value, ensure_ascii=False)


def _frontmatter(module, number, reads, outputs, setting):
    description = (module.get("purpose") or module["name"]).replace("\n", " ").strip()[:900]
    metadata = {"flowdesk-step": module["id"], "order": number, "executor": setting["kind"],
                "review-point": bool(setting["review"]),
                "reads": reads, "produces": [{"id": p["id"], "name": p.get("name", ""), "format": p.get("format", "any")}
                                             for p in outputs]}
    lines = ["---", f"name: {skill_name(module['id'])}", f"description: {_yaml_text(description)}", "metadata:"]
    for key, value in metadata.items():
        lines.append(f"  {key}: {_yaml_text(value)}")
    lines.append("---")
    return "\n".join(lines)


def _body(module, number, reads, outputs, names):
    parts = [f"# {number:02d} · {module['name']}", ""]
    if module.get("purpose"):
        parts += [module["purpose"].strip(), ""]
    if reads:
        parts += ["## 读取", ""]
        for read in reads:
            source = f"「{names.get(read['from_step'], read['from_step'])}」的产出" if read.get("from_step") else ORIGIN.get(read.get("origin"), "外部输入")
            parts.append(f"- **{read['name']}**：{source}")
        parts.append("")
    parts += ["## 产出", ""]
    for port in outputs:
        line = f"- **{port.get('name') or port['id']}**（{port.get('format', 'any')}）"
        if port.get("description"):
            line += f"：{port['description']}"
        if port.get("format") == "json" and port.get("schema"):
            line += f"。结构见 `assets/{port['id']}.schema.json`"
        parts.append(line)
    parts.append("")
    if module.get("plan", "").strip():
        parts += ["## 做法", "", module["plan"].strip(), ""]
    if module.get("acceptance"):
        parts += ["## 验收", ""]
        who = {"program": "程序", "ai": "AI", "human": "人"}
        parts += [f"- [{who.get(item['by'], item['by'])}] {item['check']}" for item in module["acceptance"]]
        parts.append("")
    return "\n".join(parts)


def export_skills(store, project_id) -> bytes:
    project = store.load(project_id)
    runs = Runs(store)
    settings = runs.settings(project_id)
    units = runs.units(project_id)
    order = steps_of(project, units)
    wired = runs._wired(project_id, project)
    names = {m["id"]: m["name"] for m in project["modules"]}
    buffer = io.BytesIO()
    folders = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for number, module in enumerate(order, 1):
            setting = runs.step_setting(settings, module)
            outputs = produced(module, setting["outputs"])
            reads = []
            for port in module["inputs"]:
                for source in wired.get(module["id"], {}).get(port["id"], []):
                    reads.append({"id": port["id"], "name": port.get("name") or port["id"],
                                  "from_step": source.get("step"), "from_output": source.get("port"),
                                  "origin": source.get("origin")})
            folder = f"{number:02d}-{skill_name(module['id'])}"
            folders.append((number, folder, module, setting))
            text = _frontmatter(module, number, reads, outputs, setting) + "\n\n" + _body(module, number, reads, outputs, names)
            archive.writestr(f"{folder}/SKILL.md", text)
            for port in outputs:
                if port.get("format") == "json" and port.get("schema"):
                    archive.writestr(f"{folder}/assets/{port['id']}.schema.json",
                                     json.dumps(port["schema"], ensure_ascii=False, indent=2) + "\n")
        readme = [f"# {project['name']}", "", "每个文件夹是一步，按编号顺序做。每步的 SKILL.md 写了读什么、产出什么、怎么做、怎么验收。", "",
                  "| 步 | 名称 | 执行者 | 要人审 |", "|---|---|---|---|"]
        who = {"ai": "AI", "program": "程序", "human": "人"}
        readme += [f"| {n:02d} | [{m['name']}]({f}/SKILL.md) | {who.get(s['kind'], s['kind'])} | {'是' if s['review'] else ''} |"
                   for n, f, m, s in folders]
        readme += ["", "由 Flowdesk 导出。在 Claude Code 或 Codex 里打开这个文件夹，就能按步调用。", ""]
        archive.writestr("README.md", "\n".join(readme))
        archive.writestr("workflow.json", json.dumps({
            "project": project["name"], "request": (project.get("workflow") or {}).get("request", {}).get("name"),
            "steps": [{"order": n, "folder": f, "id": m["id"], "name": m["name"], "executor": s["kind"],
                       "review_point": s["review"]} for n, f, m, s in folders]}, ensure_ascii=False, indent=2) + "\n")
    return buffer.getvalue()
