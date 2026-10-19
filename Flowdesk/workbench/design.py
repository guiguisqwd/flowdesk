"""Deterministic completeness checks for the artifact-oriented design template.

These diagnostics do not claim to determine semantic granularity or execute a workflow.
They check the structural proxies of one step instead: one primary artifact, inputs
with a declared origin, outputs that some step really reads or the user receives, and
acceptance items. Most contract codes apply only to modules whose ports are typed, so
legacy designs keep exactly their previous issue list; the two that check a contract
field itself (for_each, acceptance) run wherever that field is set, and legacy designs
never carried either.
"""

from .contract import per_item_problem

TEMPLATE_VERSION = "artifact-step-v2"


def has_text(value):
    return isinstance(value, str) and bool(value.strip())


def typed(module):
    return any("origin" in port for port in module.get("inputs", [])) or \
        any("role" in port for port in module.get("outputs", []))


def reach(project):
    """Which ports really connect to something, following composite boundaries.

    A port counts only if a concrete step sits at the other end. A composite input is
    a reader only if a binding hands it to a child that reads it; a composite output
    is a producer only if a binding fills it from a child that produces it.

    consumed: outputs a reader takes through a data edge, or that are delivered.
    fed: inputs a producer feeds through a data edge, or that are entry points.
    edge_fed: the subset of fed that comes from an edge rather than an entry point.
    A child port bound to a composite port inherits that port's status.
    """
    _, consumed, fed, edge_fed = _flows(project)
    return consumed, fed, edge_fed


def read_later(project):
    """Outputs a later step really reads through a data edge, also through the outputs of the
    groups they sit in (being delivered alone does not count)."""
    return _flows(project)[0]


def _flows(project):
    """reach's sets, plus the outputs read through a data edge alone (first)."""
    modules = project.get("modules", [])
    parents = {m.get("parent_id") for m in modules}
    edges = [edge for edge in project.get("edges", []) if edge.get("kind") == "data"]
    workflow = project.get("workflow") or {}
    entries = {(item["module_id"], item["port_id"]) for item in workflow.get("entry_points", [])}
    delivered = {(item["module_id"], item["port_id"]) for item in (workflow.get("delivery") or {}).get("ports", [])}
    bindings = [(binding["direction"], (parent["id"], binding["parent_port"]), (binding["child_module"], binding["child_port"]))
                for parent in modules for binding in parent.get("boundary_bindings", [])]

    def spread(reached, direction, outward):
        changed = True
        while changed:
            changed = False
            for kind, outer, inner in bindings:
                start, end = (inner, outer) if outward else (outer, inner)
                if kind == direction and start in reached and end not in reached:
                    reached.add(end)
                    changed = True
        return reached

    leaves = [m for m in modules if m["id"] not in parents]
    readers = spread({(m["id"], p["id"]) for m in leaves for p in m.get("inputs", [])}, "input", True)
    producers = spread({(m["id"], p["id"]) for m in leaves for p in m.get("outputs", [])}, "output", True)
    read = {(e["source"], e.get("source_port")) for e in edges if (e["target"], e.get("target_port")) in readers}
    edge_fed = {(e["target"], e.get("target_port")) for e in edges if (e["source"], e.get("source_port")) in producers}
    consumed = spread(read | delivered, "output", False)
    fed = spread(edge_fed | entries, "input", False)
    return spread(read, "output", False), consumed, fed, spread(set(edge_fed), "input", False)


def contract_issues(project, module_id=None):
    modules = {m["id"]: m for m in project.get("modules", [])}
    parents = {m.get("parent_id") for m in modules.values()}
    edges = [edge for edge in project.get("edges", []) if edge.get("kind") == "data"]
    consumed, fed, edge_fed = reach(project)
    issues = []
    for m in modules.values():
        if module_id is not None and m["id"] != module_id:
            continue

        def report(code, message, port_id=None):
            item = {"module_id": m["id"], "code": code, "severity": "design", "message": message}
            if port_id is not None:
                item["port_id"] = port_id
            issues.append(item)

        # Per-item work is checked wherever it is marked, typed step or not; legacy
        # designs never carried the key, so their issue lists do not change.
        for_each = m.get("for_each")
        if for_each is not None:
            target = next((port for port in m.get("inputs", []) if port["id"] == for_each), None)
            if m["id"] in parents:
                report("for_each_invalid", "组合步骤只负责分组，不逐个处理：把「逐个处理」标在承接它的子步骤上，这里取消")
            elif target is None:
                report("for_each_invalid", "逐个处理的对象必须是本步一个 JSON 或文件格式的输入")
            elif problem := per_item_problem(target):
                report("for_each_invalid", f"逐个处理的「{target.get('name') or target['id']}」：{problem}", target["id"])
        if len(m.get("acceptance") or []) > 6:
            report("acceptance_too_many", f"验收项有 {len(m['acceptance'])} 条，最多 6 条：只留当场能判断对错的几条")
        if not typed(m):
            continue
        leaf = m["id"] not in parents
        for port in m.get("outputs", []):
            address = (m["id"], port["id"])
            if leaf and port.get("role") == "primary" and address not in consumed:
                report("unconsumed_product", f"「{port.get('name') or port['id']}」产出后无人取用：连到下游，或设为最终交付", port["id"])
        for edge in edges:
            if edge["source"] == m["id"]:
                source = next((p for p in m.get("outputs", []) if p["id"] == edge.get("source_port")), None)
                if source and source.get("role") == "record":
                    report("record_consumed", f"「{source.get('name') or source['id']}」是记录，不交给其他步骤；需要它就并入主产物", source["id"])
        if leaf:
            primaries = [port for port in m.get("outputs", []) if port.get("role") == "primary"]
            if len(primaries) != 1:
                report("primary_count", f"这一步应恰有一个主产物（现在 {len(primaries)} 个）")
            if not m.get("acceptance"):
                report("missing_acceptance", "还没有验收项：写下当场能判断对错的内容、数量、格式或禁止项")
            # A typed step has no unclassified ports: one primary, the rest records;
            # every input says where it comes from.
            for port in m.get("outputs", []):
                if port.get("role") not in ("primary", "record"):
                    report("port_unclassified", f"输出「{port.get('name') or port['id']}」没标是主产物还是记录：除主产物外只能是记录", port["id"])
            for port in m.get("inputs", []):
                if port.get("origin") not in ("upstream", "request", "resource", "user"):
                    report("port_unclassified", f"输入「{port.get('name') or port['id']}」没标来处：前面步骤、启动请求、资料或用户提供", port["id"])
        for port in m.get("inputs", []):
            address, origin, label = (m["id"], port["id"]), port.get("origin"), port.get("name") or port["id"]
            if origin == "upstream" and address not in fed:
                report("upstream_unfed", f"「{label}」应来自上一步，但还没连上", port["id"])
            elif origin == "request" and address not in fed:
                report("request_unbound", f"「{label}」应来自启动请求，但还没有接上", port["id"])
            elif origin in ("resource", "user") and address in edge_fed:
                report("external_with_edge", f"「{label}」标为外部提供，却有上游连线；二选一", port["id"])
    return issues


def design_issues(project, module_id=None):
    modules = project.get("modules", [])
    parents = {m.get("parent_id") for m in modules}
    issues = []
    for m in modules:
        if module_id is not None and m["id"] != module_id:
            continue
        def report(code, message, port_id=None):
            item = {"module_id": m["id"], "code": code, "severity": "design", "message": message}
            if port_id is not None:
                item["port_id"] = port_id
            issues.append(item)
        for field, title in (("purpose", "单一步骤目的"), ("change", "输入到产物的变化"), ("plan", "处理与验收 plan")):
            if not has_text(m.get(field)):
                report("missing_" + field, f"统一模板待补：{title}")
        for side, title in (("inputs", "输入产物"), ("outputs", "输出产物")):
            # A composite's interface is whatever its children read and produce.
            if not m.get(side) and not (m["id"] in parents and typed_children(modules, m["id"])):
                report("missing_" + side, f"统一模板待补：{title}（外部触发也应明确命名）")
            for port in m.get(side, []):
                if not has_text(port.get("name")) or not has_text(port.get("description")):
                    report("unnamed_artifact", f"{title}需要名称和内容说明", port["id"])
                if port.get("provenance") == "missing" or port.get("format", "any") == "any":
                    report("unknown_artifact", f"「{port.get('name') or port['id']}」的产物约定尚待定义", port["id"])
        if m["id"] in parents:
            from .store import boundary_endpoints
            by_id = {module["id"]: module for module in modules}
            mapped = {(b["direction"], b["parent_port"]) for b in m.get("boundary_bindings", [])
                      if boundary_endpoints(m, b, by_id)}
            for direction, side, label in (("input", "inputs", "输入"), ("output", "outputs", "输出")):
                for port in m.get(side, []):
                    if (direction, port["id"]) not in mapped:
                        report("unmapped_boundary", f"本模块{label}「{port.get('name') or port['id']}」尚未映射到直接子步骤；没有自动猜测接线。", port["id"])
    return issues + contract_issues(project, module_id)


def typed_children(modules, parent_id):
    return any(typed(m) for m in modules if m.get("parent_id") == parent_id)


AUDIT_LABELS = {
    "no_performer": "没定执行者（AI／程序／用户）",
    "outputs_unranked": "有多个输出，没标哪一个是主产物",
    "no_output": "没有输出",
    "unconsumed_output": "输出无人取用，也不是最终交付",
    "input_unsourced": "必需输入没有来处（没接上游，也没标为资料或用户提供）",
    "missing_acceptance": "没有验收项",
    "primary_count": "主产物不是恰好一个",
    "unconsumed_product": "主产物无人取用，也不是最终交付",
    "record_consumed": "记录被交给了别的步骤",
    "upstream_unfed": "应来自前面步骤却没接上",
    "request_unbound": "应来自启动请求却没接上",
    "external_with_edge": "标为外部提供却有上游连线",
    "for_each_invalid": "逐个处理标得不对（对象不是本步的 JSON 数组或文件输入，或标在了组合步骤上）",
    "acceptance_too_many": "验收项超过 6 条",
    "port_unclassified": "端口没标来处，或没标是主产物还是记录",
}


def audit(project):
    """The step rules applied to every concrete step, typed or not (read-only).

    Typed steps get exactly their contract_issues. A step designed before the
    contract is asked the questions the contract would answer: which output is the
    primary artifact, where each required input comes from, who performs it, how it
    is accepted. These are questions for a reviewer, never part of design_issues.
    """
    modules = project.get("modules", [])
    parents = {m.get("parent_id") for m in modules}
    consumed, fed, _ = reach(project)
    issues = contract_issues(project)
    for m in modules:
        if m["id"] in parents:
            continue

        def report(code, port_id=None):
            item = {"module_id": m["id"], "code": code, "severity": "audit", "message": AUDIT_LABELS[code]}
            if port_id is not None:
                item["port_id"] = port_id
            issues.append(item)

        if m.get("execution_kind") not in ("ai", "program", "human"):
            report("no_performer")
        if typed(m):
            continue
        outputs = m.get("outputs", [])
        if not outputs:
            report("no_output")
        elif len(outputs) > 1:
            report("outputs_unranked")
        for port in outputs:
            if (m["id"], port["id"]) not in consumed:
                report("unconsumed_output", port["id"])
        for port in m.get("inputs", []):
            if port.get("required") and (m["id"], port["id"]) not in fed:
                report("input_unsourced", port["id"])
        if not m.get("acceptance"):
            report("missing_acceptance")
    return issues
