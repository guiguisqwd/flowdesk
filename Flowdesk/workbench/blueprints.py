"""New projects from a ready-made step skeleton (模板登记), checked by the same contract as AI drafts.

A template is a skeleton plus, for each step, its plan, purpose and acceptance. Data hand-offs,
entry and delivery are derived from the skeleton by contract.derive_wiring, never written by hand.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import contract
from .contract import REQUEST
from .store import valid_id

FOLDER = Path(__file__).resolve().parent / "templates"
FORMATS = {"text", "json", "file"}


def listing():
    found = []
    for path in sorted(FOLDER.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        found.append({"id": data["id"], "name": data["name"], "description": data["description"],
                      "steps": [m["name"] for m in data["modules"]]})
    return found


def load(template_id):
    valid_id(template_id)
    path = FOLDER / f"{template_id}.json"
    if not path.is_file():
        raise FileNotFoundError("没有这个模板")
    return json.loads(path.read_text(encoding="utf-8"))


def _port(pid, name, description, fmt, **extra):
    return {"id": pid, "name": name, "description": description, "format": fmt,
            "schema": {"type": "string"} if fmt == "text" else ({"type": "object"} if fmt == "json" else {}),
            "sample": "", "required": True, "provenance": "user", **extra}


def build(template):
    """Return (modules, edges, workflow, run settings) for a template, or raise DesignErrors."""
    outline = {"request": {"name": template["request"]["name"], "provenance": "user"},
               "resources": [{"id": r["id"], "name": r["name"], "note": r.get("note", "")} for r in template["resources"]],
               "delivery": list(template["delivery"]),
               "modules": [{"id": m["id"], "name": m["name"], "parent_id": m.get("parent_id"), "notes": m.get("purpose", ""),
                            "finding_ids": [], "kind": m.get("kind"), "reads": m.get("reads", []),
                            "produces": {"id": m["produces"]["id"], "name": m["produces"]["name"]} if m.get("produces") else None,
                            "each": None} for m in template["modules"]],
               "warnings": []}
    checked = contract.check_skeleton(outline, from_request=True)
    contract.raise_if(checked["errors"])
    resources = {r["id"]: r for r in template["resources"]}
    leaves = contract.leaf_ids(outline["modules"])
    produced = {m["produces"]["id"]: {**m["produces"], "step": m["name"]} for m in template["modules"] if m.get("produces")}
    modules = []
    for item in template["modules"]:
        module = {"id": item["id"], "name": item["name"], "purpose": item.get("purpose", ""),
                  "change": item.get("change", ""), "role": "", "parent_id": item.get("parent_id"),
                  "plan": item.get("plan", ""), "provenance": "user", "source_refs": [], "generation": None,
                  "needs_review": False, "inputs": [], "outputs": [], "boundary_bindings": []}
        if item["id"] in leaves:
            fmt = item["produces"].get("format", "text")
            if fmt not in FORMATS:
                raise ValueError(f"「{item['name']}」的产出格式只能是 text、json 或 file")
            module["execution_kind"] = item["kind"]
            module["outputs"] = [_port(item["produces"]["id"], item["produces"]["name"], item.get("purpose", ""), fmt,
                                       role="primary")]
            for read in item.get("reads", []):
                if read["artifact"] == REQUEST:
                    module["inputs"].append(_port(REQUEST, template["request"]["name"],
                                                  template["request"].get("description", ""), "text", origin="request"))
                elif read["artifact"] in produced:
                    source = produced[read["artifact"]]
                    module["inputs"].append(_port(read["artifact"], source["name"], f"由「{source['step']}」产出",
                                                  source.get("format", "text"), origin="upstream"))
                elif read["artifact"] in resources:
                    resource = resources[read["artifact"]]
                    module["inputs"].append(_port(resource["id"], resource["name"], resource.get("note", ""), "file",
                                                  origin="resource"))
            if not module["change"]:
                names = [p["name"] for p in module["inputs"]]
                module["change"] = f"把{'、'.join(names) or '启动请求'}变成「{item['produces']['name']}」"
            if item.get("acceptance"):
                module["acceptance"] = copy.deepcopy(item["acceptance"])
        else:
            module["execution_kind"] = "composite"
        modules.append(module)
    request_port = _port(REQUEST, template["request"]["name"], template["request"].get("description", ""), "text")
    wired = contract.derive_wiring(outline, modules, request_port=request_port)
    workflow = {"request": request_port, "entry_points": wired["entry_points"],
                "delivery": {"name": template.get("delivery_name", "最终交付"),
                             "description": template.get("description", ""), "ports": wired["delivery_ports"]}}
    return wired["modules"], wired["edges"], workflow, copy.deepcopy(template.get("settings", {}))


def create(store, runs, template_id, name=None):
    template = load(template_id)
    modules, edges, workflow, settings = build(template)
    project = store.create(name or template["name"])
    project.update(modules=modules, edges=edges, workflow=workflow)
    saved = store.save(project["id"], project, project["fingerprint"])
    if settings:
        runs.save_settings(saved["id"], settings)
    return saved


def duplicate(store, runs, project_id, name=None):
    """Start a new project from an existing one: same steps, plans and run settings; no runs."""
    import shutil

    source = store.load(project_id)
    project = store.create(name or f"{source['name']}（副本）")
    target = store.directory(project["id"])
    if (store.directory(project_id) / "sources").exists():
        shutil.copytree(store.directory(project_id) / "sources", target / "sources", dirs_exist_ok=True)
    copied = copy.deepcopy(source)
    for key in ("fingerprint", "issues"):
        copied.pop(key, None)
    copied["id"] = project["id"]
    copied["name"] = project["name"]
    saved = store.save(project["id"], copied, project["fingerprint"], preserve_sources=False, propagate=False)
    settings = store.directory(project_id) / "run-settings.json"
    if settings.exists():
        runs.save_settings(saved["id"], json.loads(settings.read_text(encoding="utf-8")))
    return saved
