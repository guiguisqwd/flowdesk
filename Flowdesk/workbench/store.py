"""File-backed projects with optimistic writes and immutable revision snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .readme import readme_head, split_head


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:12]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", value):
        raise ValueError("无效对象标识")
    return value


class ConflictError(ValueError):
    pass


class ProjectBroken(ValueError):
    pass


def module_hash(module):
    fields = {k: module.get(k) for k in ("id", "purpose", "change", "inputs", "outputs", "plan", "role")}
    # The README head ("# title" + "> one-liner") is for people, not for the code; a plan
    # that does not start with "# title" hashes exactly as before.
    if isinstance(fields["plan"], str):
        fields["plan"] = split_head(fields["plan"])[1]
    # Classifying a port (origin/role) does not change what the code must do.
    for side in ("inputs", "outputs"):
        if isinstance(fields[side], list):
            fields[side] = [port_interface(port) if isinstance(port, dict) else port for port in fields[side]]
    for key in ("boundary_bindings", "execution_kind", "acceptance", "for_each"):
        if key in module:
            fields[key] = module[key]
    return digest(fields)


def design_hash(module):
    """module_hash of a step as gui would apply it: a plan echoed with or without its last newline
    is the same plan. The chat's redo guards and the app's same-design check compare with it."""
    if isinstance(module, dict) and isinstance(module.get("plan"), str):
        module = {**module, "plan": module["plan"].strip()}
    return module_hash(module)


PORT_ORIGINS = ("upstream", "request", "resource", "user")
PORT_ROLES = ("primary", "record")
CHECKERS = ("program", "ai", "human")


def port_interface(port):
    """What a downstream step depends on; classifying a port is not an interface change."""
    return {key: value for key, value in port.items() if key not in ("role", "origin")}


def inherit_port_types(proposed, original):
    """Keep origin/role the AI left unset on ports that already existed."""
    for side, key in (("inputs", "origin"), ("outputs", "role")):
        before = {port["id"]: port for port in original.get(side, [])}
        for port in proposed.get(side, []):
            if key not in port and key in before.get(port["id"], {}):
                port[key] = before[port["id"]][key]


def inherit_typed_fields(proposed, original):
    inherit_port_types(proposed, original)
    for key in ("acceptance", "for_each"):
        if key not in proposed and key in original:
            proposed[key] = copy.deepcopy(original[key])


def normalize_boundary_bindings(bindings):
    """Validate authored shape; missing endpoints remain editable design gaps."""
    if not isinstance(bindings, list):
        raise ValueError("父子端口映射必须是列表")
    fields = {"id", "direction", "parent_port", "child_module", "child_port", "selection", "usage"}
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != fields:
            raise ValueError("父子端口映射需要 id、direction、parent_port、child_module、child_port、selection、usage")
        valid_id(binding["id"])
        if binding["id"] in seen:
            raise ValueError("同一模块的父子映射标识不能重复")
        seen.add(binding["id"])
        for key in ("parent_port", "child_module", "child_port"):
            if not isinstance(binding[key], str):
                raise ValueError("父子映射端点必须是标识文字")
            if binding[key]:
                valid_id(binding[key])
        if binding["direction"] not in ("input", "output") or binding["usage"] not in ("handoff", "lookup"):
            raise ValueError("父子映射方向或用途无效")
        if not isinstance(binding["selection"], str):
            raise ValueError("父子映射取用内容必须是文字")
    return bindings


def boundary_endpoints(parent, binding, modules):
    """Return real port addresses only; hierarchy alone never creates a mapping."""
    child = modules.get(binding["child_module"])
    side = "inputs" if binding["direction"] == "input" else "outputs"
    parent_port = next((p for p in parent[side] if p["id"] == binding["parent_port"]), None)
    child_port = next((p for p in (child or {}).get(side, []) if p["id"] == binding["child_port"]), None)
    if not child or child.get("parent_id") != parent["id"] or not parent_port or not child_port:
        return None
    upstream = (parent["id"], side, parent_port["id"], parent_port)
    downstream = (child["id"], side, child_port["id"], child_port)
    return (upstream, downstream) if binding["direction"] == "input" else (downstream, upstream)


ACCEPTANCE_LIMIT = 6


def normalize_module(module):
    m = copy.deepcopy(module)
    valid_id(m["id"])
    for k in ("name", "purpose", "change", "role", "plan"):
        m.setdefault(k, "")
        if not isinstance(m[k], str):
            raise ValueError(f"模块 {k} 必须是文字")
    if not m["name"].strip():
        raise ValueError("模块名称不能为空")
    m.setdefault("parent_id", None)
    m.setdefault("provenance", "user")
    m.setdefault("source_refs", [])
    m.setdefault("generation", None)
    m.setdefault("needs_review", False)
    if "execution_kind" in m and m["execution_kind"] not in ("program", "ai", "human", "composite"):
        raise ValueError("执行方式必须为 program、ai、human 或 composite；未确定时省略 execution_kind")
    if "boundary_bindings" in m:
        normalize_boundary_bindings(m["boundary_bindings"])
    # Contract fields are optional and never defaulted, so legacy designs keep their hash;
    # an empty value means "not decided" and is stored as absent, which hashes the same.
    if m.get("acceptance") in (None, []):
        m.pop("acceptance", None)
    if m.get("for_each") in (None, ""):
        m.pop("for_each", None)
    if "acceptance" in m:
        if not isinstance(m["acceptance"], list) or any(
                not isinstance(item, dict) or set(item) != {"check", "by"} or not isinstance(item["check"], str)
                or not item["check"].strip() or item["by"] not in CHECKERS for item in m["acceptance"]):
            raise ValueError("验收项需要非空的 check，以及由谁判断 by（program、ai 或 human）")
    if "for_each" in m and not isinstance(m["for_each"], str):
        raise ValueError("逐个处理的对象必须是一个输入标识")
    for side in ("inputs", "outputs"):
        m.setdefault(side, [])
        seen = set()
        for p in m[side]:
            valid_id(p["id"])
            if p["id"] in seen:
                raise ValueError("同一模块的输入或输出标识不能重复")
            seen.add(p["id"])
            for k, default in {"name": "", "description": "", "format": "any", "schema": {}, "sample": "", "required": True, "provenance": "user"}.items():
                p.setdefault(k, default)
            if not isinstance(p["name"], str) or not isinstance(p["description"], str):
                raise ValueError("输入输出名称和说明必须是文字；未知内容请留空保存为设计草稿")
            if p["format"] not in ("text", "json", "file", "any"):
                raise ValueError("未知输入输出格式")
            if not isinstance(p["schema"], dict):
                raise ValueError("内容结构必须是 JSON Schema 对象")
            try:
                Draft202012Validator.check_schema(p["schema"])
            except SchemaError as exc:
                raise ValueError(f"输入输出结构不是有效的 JSON Schema：{exc.message}") from exc
            if not isinstance(p["sample"], str):
                p["sample"] = json.dumps(p["sample"], ensure_ascii=False, indent=2)
            if "origin" in p and (side != "inputs" or p["origin"] not in PORT_ORIGINS):
                raise ValueError("输入来处 origin 必须为 upstream、request、resource 或 user")
            if "role" in p and (side != "outputs" or p["role"] not in PORT_ROLES):
                raise ValueError("输出角色 role 必须为 primary 或 record")
    g = m.get("generation")
    if g:
        valid_id(g["id"])
        if g.get("plan_hash") != module_hash(m):
            g["status"] = "stale"
    return m


def normalize_workflow(workflow):
    """An authored user request and final delivery, never inferred from graph loose ends."""
    if not isinstance(workflow, dict) or not isinstance(workflow.get("request"), dict):
        raise ValueError("工作流需要一个明确的启动请求")
    w = copy.deepcopy(workflow)
    w["request"] = normalize_module({"id": "request_contract", "name": "启动请求", "inputs": [w["request"]]})["inputs"][0]
    delivery = w.get("delivery")
    if not isinstance(delivery, dict):
        raise ValueError("工作流需要最终交付约定")
    for key in ("name", "description"):
        if not isinstance(delivery.get(key), str):
            raise ValueError("最终交付名称和说明必须是文字")
    for field, bindings in (("启动", w.get("entry_points")), ("交付", delivery.get("ports"))):
        if not isinstance(bindings, list):
            raise ValueError(f"{field}映射必须是列表")
        seen = set()
        for item in bindings:
            if not isinstance(item, dict) or not isinstance(item.get("module_id"), str) or not isinstance(item.get("port_id"), str):
                raise ValueError(f"{field}映射需要模块与端口标识")
            for key in ("module_id", "port_id"):
                if item[key]:
                    valid_id(item[key])
            identity = (item["module_id"], item["port_id"])
            if identity in seen:
                raise ValueError(f"{field}映射重复")
            seen.add(identity)
            if field == "启动":
                if not isinstance(item.get("selection", ""), str):
                    raise ValueError("启动请求取用规则必须是文字")
                item.setdefault("selection", "")
    return w


def normalize_edge_usage(edge):
    """Usage describes a data dependency's purpose, never whether it is enforced."""
    edge.setdefault("usage", "handoff")
    if edge["usage"] not in ("handoff", "lookup"):
        raise ValueError("连接用途必须为 handoff 或 lookup")
    if edge.get("kind") != "data" and edge["usage"] != "handoff":
        raise ValueError("lookup 仅适用于 data 连接；其他关系的 usage 使用 handoff 默认值")
    return edge


# Selections that take a port whole; anything else is a field path (compared lower-cased).
WHOLE_SELECTIONS = frozenset({"", "$", "全部", "整体", "全文", "完整", "完整材料", "完整输出", "全部输出", "all", "full", "whole"})


def integrity(project):
    """Drafts may have holes. Surface them without pretending they are runnable."""
    modules = {m["id"]: m for m in project["modules"]}
    issues = []
    bound = set()

    def schema_types(schema):
        if not isinstance(schema, dict):
            return set()
        declared = schema.get("type", [])
        return {declared} if isinstance(declared, str) else set(declared)

    def inspect_schema(upstream, downstream, report, location="内容"):
        # This is a conservative declared-shape diagnostic, not a JSON Schema
        # implication solver. In particular, absent types and $refs are unknown.
        if not isinstance(upstream, dict) or not isinstance(downstream, dict):
            return
        before, after = schema_types(upstream), schema_types(downstream)
        if before and after:
            produced, accepted = set(before), set(after)
            if "number" in produced:
                produced.add("integer")
            if "number" in accepted:
                accepted.add("integer")
            if not produced <= accepted:
                report(f"{location}结构类型不保证兼容：{' / '.join(sorted(before))} → {' / '.join(sorted(after))}")
        properties = upstream.get("properties", {})
        expected = downstream.get("properties", {})
        if ("object" in before or properties) and ("object" in after or expected or downstream.get("required")):
            missing = set(downstream.get("required", [])) - set(properties)
            if missing:
                report(f"{location}的上游结构未声明必需字段：" + "、".join(sorted(missing)))
            for name in sorted(set(properties) & set(expected)):
                inspect_schema(properties[name], expected[name], report, f"{location}.{name}")
        if "array" in before and "array" in after:
            inspect_schema(upstream.get("items"), downstream.get("items"), report, location + "[]")

    def selected_schema(schema, selection):
        """Resolve only a simple property path; never guess a natural-language transform."""
        if not isinstance(schema, dict):
            return None
        if selection.startswith("/"):
            parts = [p.replace("~1", "/").replace("~0", "~") for p in selection[1:].split("/")]
        else:
            path = selection[2:] if selection.startswith("$.") else selection
            if not re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", path):
                return None
            parts = path.split(".")
        current = schema
        for part in parts:
            if not isinstance(current, dict) or part not in current.get("properties", {}):
                return None
            current = current["properties"][part]
        return current if isinstance(current, dict) else None

    for edge in project["edges"]:
        if edge["source"] not in modules or edge["target"] not in modules:
            issues.append({"module_id": edge["target"], "edge_id": edge["id"], "message": "连接引用的模块已不存在"})
            continue
        if edge["kind"] != "data":
            continue
        upstream = next((p for p in modules[edge["source"]]["outputs"] if p["id"] == edge.get("source_port")), None)
        downstream = next((p for p in modules[edge["target"]]["inputs"] if p["id"] == edge.get("target_port")), None)
        if not upstream or not downstream:
            issues.append({"module_id": edge["target"], "edge_id": edge["id"], "message": "输入或输出端口缺失，请重新绑定"})
            continue
        bound.add((edge["target"], downstream["id"]))
        report = lambda message: issues.append({"module_id": edge["target"], "edge_id": edge["id"], "message": message})
        selection = str(edge.get("selection", "")).strip()
        if not selection:
            report("待明确取用内容：请填写全文或具体字段路径，空白不代表已确认取用全部")
        whole = selection.lower() in WHOLE_SELECTIONS
        schema = upstream.get("schema", {})
        if not whole:
            schema = selected_schema(schema, selection)
            if schema is None:
                report("输入选择尚未解析为明确字段路径，所选内容的格式和结构需要复核")
        if whole and "any" not in (upstream["format"], downstream["format"]) and upstream["format"] != downstream["format"]:
            issues.append({"module_id": edge["target"], "edge_id": edge["id"], "message": f"格式不匹配：{upstream['format']} → {downstream['format']}"})
        if schema is not None:
            expected_schema = downstream.get("schema") or ({"type": "string"} if downstream["format"] in ("text", "file") else {})
            inspect_schema(schema, expected_schema, report)
        if edge.get("needs_review"):
            issues.append({"module_id": edge["target"], "edge_id": edge["id"], "code": "edge_needs_review",
                           "message": "上游输出已变化，此连接需要复核"})

    for parent in modules.values():
        seen_bindings = set()
        for binding in parent.get("boundary_bindings", []):
            def report(message):
                issues.append({"module_id": parent["id"], "binding_id": binding["id"],
                               "port_id": binding["parent_port"], "message": message})
            endpoints = boundary_endpoints(parent, binding, modules)
            if not endpoints:
                report("父子端口映射的端口缺失，或目标不是本模块的直接子模块")
                continue
            upstream_address, downstream_address = endpoints
            upstream, downstream = upstream_address[3], downstream_address[3]
            if binding["direction"] == "input":
                bound.add((binding["child_module"], binding["child_port"]))
            selection = binding["selection"].strip()
            identity = tuple(binding[key] for key in ("direction", "parent_port", "child_module", "child_port")) + (selection,)
            if identity in seen_bindings:
                report("父子端口映射重复；同一取用关系只需保存一次")
            seen_bindings.add(identity)
            if not selection:
                report("父子映射的取用内容待明确；空白不代表全文")
            whole = selection.lower() in WHOLE_SELECTIONS
            schema = upstream.get("schema", {}) if whole else selected_schema(upstream.get("schema", {}), selection)
            if schema is None:
                report("父子映射的字段路径待复核；映射不能代替处理或格式转换")
            else:
                expected = downstream.get("schema") or ({"type": "string"} if downstream["format"] in ("text", "file") else {})
                inspect_schema(schema, expected, report, "父子映射")
            if whole and "any" not in (upstream["format"], downstream["format"]) and upstream["format"] != downstream["format"]:
                report(f"父子映射格式不匹配：{upstream['format']} → {downstream['format']}")

    workflow = project.get("workflow")
    if workflow:
        request = workflow["request"]
        if not request.get("name", "").strip() or request.get("provenance") == "missing":
            issues.append({"message": "工作流启动请求尚待定义；未将资料依赖合并为用户请求"})
        if not request.get("description", "").strip():
            issues.append({"message": "启动请求需要说明用户提出什么，以及需要带上哪些信息"})
        if request.get("format") == "any":
            issues.append({"message": "启动请求的内容格式尚待定义"})
        if not workflow["entry_points"]:
            issues.append({"message": "用户请求尚未交给具体起始步骤"})
        for binding in workflow["entry_points"]:
            mid, pid = binding["module_id"], binding["port_id"]
            downstream = next((p for p in modules.get(mid, {}).get("inputs", []) if p["id"] == pid), None)
            report = lambda message: issues.append({"module_id": mid, "port_id": pid, "message": message})
            if not downstream:
                report("工作流启动映射的步骤或输入已不存在")
                continue
            bound.add((mid, pid))
            selection = binding.get("selection", "").strip()
            if not selection:
                report("启动请求的取用内容待明确")
            whole = selection.lower() in WHOLE_SELECTIONS
            schema = request.get("schema", {}) if whole else selected_schema(request.get("schema", {}), selection)
            if schema is None:
                report("启动请求的字段路径待复核")
            else:
                inspect_schema(schema, downstream.get("schema", {}), report, "启动请求")
            if whole and "any" not in (request["format"], downstream["format"]) and request["format"] != downstream["format"]:
                report("启动请求与起始步骤输入格式不匹配")
        if not workflow["delivery"]["ports"]:
            issues.append({"message": "最终交付尚未指定具体产物"})
        if not workflow["delivery"]["name"].strip() or not workflow["delivery"]["description"].strip():
            issues.append({"message": "最终交付的名称与完成说明尚待定义"})
        for binding in workflow["delivery"]["ports"]:
            mid, pid = binding["module_id"], binding["port_id"]
            if not any(p["id"] == pid for p in modules.get(mid, {}).get("outputs", [])):
                issues.append({"module_id": mid, "port_id": pid, "message": "最终交付的步骤或输出已不存在"})

    # Both handoff and lookup are same-run data dependencies. Usage controls
    # presentation, not validation or ordering. Feedback,
    # references and conditions retain their own semantics and may form loops.
    graph = {mid: [] for mid in modules}
    reverse = {mid: [] for mid in modules}
    data_edges = [edge for edge in project["edges"] if edge["kind"] == "data"
                  and edge["source"] in modules and edge["target"] in modules]
    for edge in data_edges:
        graph[edge["source"]].append(edge["target"])
        reverse[edge["target"]].append(edge["source"])
    visited, order = set(), []
    for mid in graph:
        if mid in visited:
            continue
        visited.add(mid)
        stack = [(mid, iter(graph[mid]))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                order.append(node)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(graph[child])))
    visited.clear()
    for mid in reversed(order):
        if mid in visited:
            continue
        component, pending = set(), [mid]
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            component.add(node)
            pending.extend(reverse[node])
        if len(component) == 1 and mid not in graph[mid]:
            continue
        names = "、".join(modules[node]["name"] for node in sorted(component))
        for edge in data_edges:
            if edge["source"] in component and edge["target"] in component:
                issues.append({"module_id": edge["target"], "edge_id": edge["id"],
                               "message": f"同次数据依赖形成循环（{names}）；返回或补作应明确为反馈或新尝试"})
    for m in modules.values():
        for p in m["inputs"]:
            # Typed inputs are diagnosed by their declared origin in design.contract_issues.
            if p.get("required") and "origin" not in p and (m["id"], p["id"]) not in bound:
                issues.append({"module_id": m["id"], "port_id": p["id"], "code": "input_unbound",
                               "message": f"「{p['name']}」需要上游绑定或由外部提供"})
    # Input and output boundaries are different addresses. Treating a parent as
    # one dependency node would invent a cycle for every valid nested pipeline.
    arcs = dependency_arcs(project)
    adjacency = {}
    for before, after, _, _ in arcs:
        adjacency.setdefault(before, set()).add(after)
    for source, target, binding_owner, binding_id in arcs:
        if not binding_id:
            continue
        pending, seen = [target], set()
        while pending:
            node = pending.pop()
            if node == source:
                issues.append({"module_id": binding_owner, "binding_id": binding_id,
                               "message": "父子映射与数据传递形成同次依赖循环；返回或补作须使用反馈或新尝试"})
                break
            if node not in seen:
                seen.add(node)
                pending.extend(adjacency.get(node, ()))
    return issues


def dependency_arcs(project):
    """Port-level data flow for nested impact/cycle checks, never execution."""
    modules = {m["id"]: m for m in project["modules"]}
    parents = {m.get("parent_id") for m in modules.values()}
    arcs = []
    for module in modules.values():
        if module["id"] not in parents:
            arcs.extend(((module["id"], "inputs", inp["id"]), (module["id"], "outputs", out["id"]), None, None)
                        for inp in module["inputs"] for out in module["outputs"])
        for binding in module.get("boundary_bindings", []):
            endpoints = boundary_endpoints(module, binding, modules)
            if endpoints:
                arcs.append((endpoints[0][:3], endpoints[1][:3], module["id"], binding["id"]))
    for edge in project["edges"]:
        if edge["kind"] == "data" and edge["source"] in modules and edge["target"] in modules:
            arcs.append(((edge["source"], "outputs", edge["source_port"]),
                         (edge["target"], "inputs", edge["target_port"]), None, None))
    return arcs


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = {}
        self._guard = threading.Lock()

    def directory(self, project_id):
        return self.root / valid_id(project_id)

    def lock(self, project_id):
        with self._guard:
            return self._locks.setdefault(project_id, threading.RLock())

    def list(self):
        results = []
        for path in sorted(self.root.glob("*/project.json")):
            try:
                p = self.load(path.parent.name)
                results.append({k: p[k] for k in ("id", "name", "revision", "updated_at")})
            except (ValueError, OSError) as exc:
                results.append({"id": path.parent.name, "name": path.parent.name, "error": str(exc)})
        return sorted(results, key=lambda p: p.get("updated_at", ""), reverse=True)

    def create(self, name):
        project = {"id": uid("project"), "name": name.strip() or "未命名工作台", "revision": 1, "updated_at": now(), "modules": [], "edges": [], "sources": [], "warnings": []}
        self._write(project)
        return self.load(project["id"])

    def load(self, project_id):
        with self.lock(project_id):
            directory = self.directory(project_id)
            if not (directory / "project.json").exists():
                raise FileNotFoundError("项目不存在")
            try:
                data = json.loads((directory / "project.json").read_text(encoding="utf-8"))
                if data.get("id") != project_id:
                    raise ValueError("project.json 的项目标识与所在目录不一致")
                for m in data["modules"]:
                    valid_id(m["id"])
                    m["plan"] = (directory / "plans" / f"{m['id']}.md").read_text(encoding="utf-8")
                    if m.get("generation"):
                        g = m["generation"]
                        valid_id(g["id"])
                        g["code"] = (directory / "generated" / g["id"] / "module.py").read_text(encoding="utf-8")
                self._validate(data)
            except (KeyError, ValueError, OSError, TypeError) as exc:
                raise ProjectBroken(f"项目文件无法读取，请保留文件并修复：{exc}") from exc
            # Fingerprint includes externally edited canonical files. Staleness is derived.
            data["fingerprint"] = digest(data)
            for m in data["modules"]:
                if m.get("generation") and m["generation"].get("plan_hash") != module_hash(m):
                    m["generation"]["status"] = "stale"
                # Derived from the plan on every load (after the fingerprint); never stored.
                m["readme_head"] = readme_head(m["plan"])
            from .design import design_issues
            data["issues"] = integrity(data) + design_issues(data)
            return data

    def _validate(self, project):
        if not isinstance(project.get("name"), str) or not project["name"].strip():
            raise ValueError("项目名称不能为空")
        ids = [m["id"] for m in project["modules"]]
        if len(ids) != len(set(ids)):
            raise ValueError("模块标识重复")
        project["modules"] = [normalize_module(m) for m in project["modules"]]
        if project.get("workflow") is not None:
            project["workflow"] = normalize_workflow(project["workflow"])
        parents = {m["id"]: m["parent_id"] for m in project["modules"]}
        composite_ids = set(parents.values())
        for m in project["modules"]:
            if m["id"] in composite_ids and m.get("generation"):
                m["generation"]["status"] = "stale"
        for mid in ids:
            seen = {mid}
            cursor = parents[mid]
            while cursor:
                if cursor not in parents:
                    raise ValueError("父模块不存在")
                if cursor in seen:
                    raise ValueError("模块包含关系不能成环")
                seen.add(cursor)
                cursor = parents[cursor]
        edge_ids = set()
        for edge in project["edges"]:
            valid_id(edge["id"])
            if edge["id"] in edge_ids:
                raise ValueError("连接标识重复")
            edge_ids.add(edge["id"])
            if edge.get("kind") not in ("data", "condition", "feedback", "reference"):
                raise ValueError("未知连接关系")
            normalize_edge_usage(edge)
            for k in ("source_port", "target_port", "selection", "label"):
                edge.setdefault(k, "")
            edge.setdefault("needs_review", False)
        project.setdefault("warnings", [])
        project.setdefault("sources", [])

    def _write(self, project):
        directory = self.directory(project["id"])
        metadata = copy.deepcopy(project)
        metadata.pop("fingerprint", None)
        metadata.pop("issues", None)
        # Added to an apply response for the page; never part of the design.
        metadata.pop("redo_job", None)
        metadata.pop("redo_refused", None)
        for m in metadata["modules"]:
            m.pop("readme_head", None)
            atomic_text(directory / "plans" / f"{valid_id(m['id'])}.md", m.pop("plan", ""))
            if m.get("generation"):
                g = m["generation"]
                atomic_text(directory / "generated" / valid_id(g["id"]) / "module.py", g.pop("code", ""))
        atomic_json(directory / "project.json", metadata)

    def save(self, project_id, project, base_fingerprint, *, preserve_sources=True, propagate=True, record_undo=True, undo_stack_override=None,
             keep_review=()):
        """keep_review: modules whose own needs_review stays as given; their readers are still flagged."""
        with self.lock(project_id):
            old = self.load(project_id)
            if base_fingerprint != old["fingerprint"]:
                raise ConflictError("文件或项目已更新。请先重新载入；当前编辑仍可复制保留。")
            new = copy.deepcopy(project)
            new["id"] = project_id
            new["revision"] = old["revision"] + 1
            new["updated_at"] = now()
            if preserve_sources:
                new["sources"] = old["sources"]
            self._validate(new)
            previous = {m["id"]: m for m in old["modules"]}
            # Capped when written, not when read: a project saved elsewhere with more
            # items still opens, and the audit lists it as acceptance_too_many. Undo
            # restores a state that was already saved, so it is never refused here.
            for m in [] if undo_stack_override is not None else new["modules"]:
                before = previous.get(m["id"]) or {}
                if len(m.get("acceptance", [])) > ACCEPTANCE_LIMIT and m.get("acceptance") != before.get("acceptance"):
                    raise ValueError(f"「{m['name']}」的验收项最多 {ACCEPTANCE_LIMIT} 条：只留当场能判断对错的几条")
            interface = lambda ports: [port_interface(port) for port in ports]
            changed = {m["id"] for m in new["modules"] if m["id"] in previous
                       and interface(m["outputs"]) != interface(previous[m["id"]]["outputs"])}
            if propagate:
                by_id = {m["id"]: m for m in new["modules"]}
                # A changed boundary can affect a child without changing the
                # parent's external output schema. Seed exact port addresses;
                # follow only real data edges and authored boundary bindings.
                seeds = set()
                for mid, module in by_id.items():
                    before = previous.get(mid)
                    if before is None:
                        continue
                    for side in ("inputs", "outputs"):
                        old_ports = {p["id"]: port_interface(p) for p in before[side]}
                        new_ports = {p["id"]: port_interface(p) for p in module[side]}
                        seeds.update((mid, side, pid) for pid in old_ports.keys() | new_ports.keys()
                                     if old_ports.get(pid) != new_ports.get(pid))
                    old_bindings = {b["id"]: b for b in before.get("boundary_bindings", [])}
                    new_bindings = {b["id"]: b for b in module.get("boundary_bindings", [])}
                    for bid in old_bindings.keys() | new_bindings.keys():
                        if old_bindings.get(bid) == new_bindings.get(bid):
                            continue
                        for binding in (old_bindings.get(bid), new_bindings.get(bid)):
                            if binding:
                                seeds.add((binding["child_module"], "inputs", binding["child_port"]) if binding["direction"] == "input"
                                          else (mid, "outputs", binding["parent_port"]))
                adjacency = {}
                for source, target, _, _ in dependency_arcs(new):
                    adjacency.setdefault(source, set()).add(target)
                affected, pending = set(), list(seeds)
                while pending:
                    address = pending.pop()
                    if address in affected:
                        continue
                    affected.add(address)
                    pending.extend(adjacency.get(address, ()))
                for mid in {address[0] for address in affected}:
                    if mid in by_id:
                        if mid not in keep_review:
                            by_id[mid]["needs_review"] = True
                        if by_id[mid].get("generation"):
                            by_id[mid]["generation"]["status"] = "stale"
                for edge in new["edges"]:
                    if edge["kind"] == "data" and (edge["source"] in changed or (edge["source"], "outputs", edge["source_port"]) in affected):
                        edge["needs_review"] = True
                        if edge["target"] in by_id and edge["target"] not in keep_review:
                            by_id[edge["target"]]["needs_review"] = True
            history_name = f"{old['revision']:06d}-{uuid.uuid4().hex[:6]}.json"
            atomic_json(self.directory(project_id) / "history" / history_name, old)
            undo_path = self.directory(project_id) / "undo-stack.json"
            undo_stack = json.loads(undo_path.read_text()) if undo_path.exists() else []
            try:
                self._write(new)
                if undo_stack_override is not None:
                    atomic_json(undo_path, undo_stack_override)
                elif record_undo:
                    atomic_json(undo_path, [*undo_stack, history_name])
            except Exception:
                self._write(old)
                raise
            return self.load(project_id)

    def undo(self, project_id, base_fingerprint):
        with self.lock(project_id):
            directory = self.directory(project_id)
            stack_path = directory / "undo-stack.json"
            stack = json.loads(stack_path.read_text()) if stack_path.exists() else [p.name for p in sorted((directory / "history").glob("*.json"))]
            if not stack:
                raise ValueError("还没有可以撤销的版本")
            if Path(stack[-1]).name != stack[-1]:
                raise ValueError("撤销历史路径无效")
            previous = json.loads((directory / "history" / stack[-1]).read_text())
            return self.save(project_id, previous, base_fingerprint, preserve_sources=False, propagate=False, record_undo=False, undo_stack_override=stack[:-1])

    def messages(self, project_id, module_id=None):
        path = self.directory(project_id) / "conversations.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [e for e in entries if not module_id or e.get("module_id") == module_id]

    def message(self, project_id, entry):
        with self.lock(project_id):
            with (self.directory(project_id) / "conversations.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"created_at": now(), **entry}, ensure_ascii=False) + "\n")
