"""Flowdesk's step contract, checked in code rather than requested in a prompt.

One step = one performer (ai / program / human) that reads artifacts whose origin
is declared in the skeleton, produces exactly one named primary artifact, and is
accepted on its own. The AI drafts the skeleton; check_skeleton decides whether it
is a Flowdesk workflow, and derive_wiring/build_composite build what the AI is not
allowed to author: data edges, composite interfaces, entry points and delivery.

Everything here is pure and deterministic. The editor runs a TypeScript port of
check_skeleton (ui/src/skeletonContract.ts) against the same fixture cases.
"""
from __future__ import annotations

import hashlib
import json

REQUEST = "request"
KINDS = ("ai", "program", "human")
MISSING_REQUEST = "启动请求待定义"
KIND_LABELS = {"ai": "AI", "program": "程序", "human": "用户"}


# Python's strip() and JavaScript's trim() disagree on a few characters (U+001C–U+001F,
# U+0085, U+FEFF); both sides trim the union so the editor and server agree on names.
# Names are compared with lower(), which matches JavaScript's toLowerCase() as long as
# both run the same Unicode version; the server's check is the one that decides.
def _edge_space(char):
    return char.isspace() or char == "\ufeff"


def clean(text):
    """Trim a name exactly as the editor does (skeletonContract.ts clean), in linear time."""
    start, end = 0, len(text)
    while start < end and _edge_space(text[start]):
        start += 1
    while end > start and _edge_space(text[end - 1]):
        end -= 1
    return text[start:end]


def step_title(item):
    # The id lets a repair prompt (which shows messages only) point at an unnamed step.
    return clean(item["name"]) or f"未命名步骤（{item['id']}）"


def issue(code, message, step_id=None, artifact=None):
    return {"code": code, "step_id": step_id, "artifact": artifact, "message": message}


LIMIT = 400


def per_item_problem(port):
    """Why a port cannot be worked through one item at a time, or None.

    JSON must be an array (each element is one item); a file port stands for a set of
    files, one per item. Text has no items to count.
    """
    if port.get("format") == "file":
        return None
    if port.get("format") != "json":
        return "format 用 json（数组）或 file（一组文件）"
    schema = port.get("schema")
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError:
            return None  # reported as invalid JSON elsewhere
    if not isinstance(schema, dict) or schema.get("type") != "array":
        return "schema 要写成数组（type=array，items 写清每一项是什么）"
    return None


class DesignErrors(ValueError):
    """Every contract violation found in one response, so one repair can fix them all.

    The text goes into the single repair prompt, so it lists every issue up to a cap far
    above what a 200-step skeleton realistically produces.
    """

    def __init__(self, issues):
        self.issues = list(issues)
        super().__init__(self.text())

    def text(self):
        lines = [f"{index + 1}. {item['message']}" for index, item in enumerate(self.issues[:LIMIT])]
        if len(self.issues) > LIMIT:
            lines.append(f"…另有 {len(self.issues) - LIMIT} 条")
        return "设计不符合 Flowdesk 步骤规则，请一次修正以下全部问题：\n" + "\n".join(lines)

    def __str__(self):
        return self.text()


def raise_if(issues):
    if issues:
        raise DesignErrors(issues)


def _children(modules):
    children = {}
    for item in modules:
        children.setdefault(item.get("parent_id"), []).append(item)
    return children


def display_order(modules):
    """Depth-first, siblings in array order: the order the skeleton editor shows."""
    children, rows, seen = _children(modules), [], set()

    def visit(parent):
        for item in children.get(parent, []):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            rows.append(item)
            visit(item["id"])

    visit(None)
    return rows


def leaf_ids(modules):
    parents = {item.get("parent_id") for item in modules}
    return {item["id"] for item in modules if item["id"] not in parents}


def structure_issues(modules):
    """Problems that leave no readable step tree; no other rule can be checked past them."""
    issues = []
    if not modules:
        return [issue("S0", "骨架至少需要一步")]
    ids = [item["id"] for item in modules]
    lookup = {item["id"]: item for item in modules}
    for mid in sorted({mid for mid in ids if ids.count(mid) > 1}):
        issues.append(issue("S0", f"步骤标识「{mid}」重复", mid))
    for item in modules:
        seen, parent = {item["id"]}, item.get("parent_id")
        while parent is not None:
            if parent not in lookup:
                issues.append(issue("S0", f"「{step_title(item)}」的上级步骤不存在", item["id"]))
                break
            if parent in seen:
                issues.append(issue("S0", f"「{step_title(item)}」的包含关系成环", item["id"]))
                break
            seen.add(parent)
            parent = lookup[parent].get("parent_id")
    return issues


def artifact_names(outline):
    names = {}
    for resource in outline.get("resources", []):
        names[resource["id"]] = clean(resource["name"])
    for item in outline["modules"]:
        if item.get("produces"):
            names[item["produces"]["id"]] = clean(item["produces"]["name"])
    names[REQUEST] = clean(outline["request"]["name"]) or MISSING_REQUEST
    return names


def check_skeleton(outline, *, from_request=False):
    """Return {errors, warnings, handoffs, readers}; errors block, warnings inform."""
    modules = outline["modules"]
    errors = structure_issues(modules)
    warnings = []
    if errors:
        return {"errors": errors, "warnings": warnings, "handoffs": [], "readers": {}}
    order = display_order(modules)
    position = {item["id"]: index for index, item in enumerate(order)}
    leaves = leaf_ids(modules)
    request = outline["request"]
    missing = request["provenance"] == "missing"
    resources = outline.get("resources", [])
    resource_ids = [resource["id"] for resource in resources]
    producers, product_names = {}, {}

    for item in order:
        name = step_title(item)
        if not clean(item["name"]):
            errors.append(issue("S0", f"步骤 {item['id']} 需要名称", item["id"]))
        if item["id"] in leaves:
            if item.get("kind") not in KINDS or not item.get("produces"):
                errors.append(issue("S1", f"「{name}」是具体步骤：选定执行者（AI／程序／用户），并只命名一个产出", item["id"]))
        elif item.get("kind") or item.get("produces") or item.get("reads") or item.get("each") or item.get("finding_ids"):
            errors.append(issue("S2", f"「{name}」含子步骤，只负责分组：读取、产出和原文要点请放到子步骤上", item["id"]))
        product = item.get("produces") if item["id"] in leaves else None
        if product:
            pid, pname = product["id"], clean(product["name"])
            if not pname:
                errors.append(issue("S3", f"「{name}」的产出需要名称", item["id"], pid))
            if pid in producers:
                errors.append(issue("S3", f"「{product_names[pid]}」被「{step_title(producers[pid])}」和「{name}」同时产出：修订后的版本请另起名，或合成一步", item["id"], pid))
                continue
            if pid == REQUEST or pid in resource_ids:
                errors.append(issue("S3", f"「{name}」的产出标识「{pid}」已用于启动请求或资料", item["id"], pid))
            producers[pid], product_names[pid] = item, pname

    names_seen = {}
    for aid, label in [(REQUEST, clean(request["name"]))] + [(r["id"], clean(r["name"])) for r in resources] + \
            [(pid, product_names[pid]) for pid in producers]:
        key = label.lower()
        if not key:
            continue
        if key in names_seen and names_seen[key] != aid:
            errors.append(issue("S3", f"「{label}」这个名称被两样东西同时使用：请改成不同的名称", None, aid))
        names_seen.setdefault(key, aid)
    for rid in sorted({rid for rid in resource_ids if resource_ids.count(rid) > 1}):
        errors.append(issue("S3", f"资料标识「{rid}」重复", None, rid))
    if REQUEST in resource_ids:
        errors.append(issue("S3", f"资料标识不能是「{REQUEST}」：它留给启动请求，请换一个标识", None, REQUEST))
    for resource in resources:
        if not clean(resource["name"]):
            errors.append(issue("S3", "每份资料都需要名称", None, resource["id"]))

    names = {**{r["id"]: clean(r["name"]) for r in resources}, **product_names,
             REQUEST: clean(request["name"]) or MISSING_REQUEST}
    readers, handoffs, read_request = {}, [], False
    for item in order:
        if item["id"] not in leaves:
            continue
        name, reads = step_title(item), item.get("reads") or []
        artifacts = [read["artifact"] for read in reads]
        if not reads and item.get("produces"):
            errors.append(issue("S12", f"「{name}」没有读取任何东西：至少读取启动请求、前面步骤的产出或一份资料", item["id"]))
        for duplicate in sorted({a for a in artifacts if artifacts.count(a) > 1}):
            errors.append(issue("S11", f"「{name}」重复读取了「{names.get(duplicate, duplicate)}」", item["id"], duplicate))
        product = (item.get("produces") or {}).get("id")
        for read in reads:
            aid = read["artifact"]
            readers.setdefault(aid, [])
            if item["id"] not in readers[aid]:
                readers[aid].append(item["id"])
            if aid == REQUEST:
                read_request = True
                handoffs.append({"from_step": None, "to_step": item["id"], "artifact": aid, "usage": read["usage"]})
                continue
            if aid in resource_ids:
                continue
            if aid not in producers:
                errors.append(issue("S4", f"「{name}」读取的「{aid}」既不是前面步骤的产出，也不是启动请求或已声明资料", item["id"], aid))
                continue
            producer = producers[aid]
            if aid == product or producer["id"] == item["id"]:
                errors.append(issue("S5", f"「{name}」不能读取自己的产出「{names[aid]}」", item["id"], aid))
            elif position[producer["id"]] > position[item["id"]]:
                errors.append(issue("S5", f"「{name}」要读的「{names[aid]}」由后面的「{step_title(producer)}」产出：调整顺序；需要回头修改请在连接阶段画成反馈", item["id"], aid))
            else:
                handoffs.append({"from_step": producer["id"], "to_step": item["id"], "artifact": aid, "usage": read["usage"]})
        if item.get("each") and item["each"] not in artifacts:
            errors.append(issue("S6", f"「{name}」逐个处理的「{names.get(item['each'], item['each'])}」不在它的读取里", item["id"], item["each"]))
        # The main line is what a step pushes forward; everything else it only consults.
        main = [read for read in reads if read["usage"] == "handoff"]
        if len(main) > 1:
            warnings.append(issue("W1", f"「{name}」有 {len(main)} 项主线输入：这一步推进的核心是哪一样？其余可标为调用资料", item["id"]))
        if item.get("produces") and not main:
            warnings.append(issue("W2", f"「{name}」没有主线输入：它推进的是哪一样东西？把那一项标为主线", item["id"]))
        if len(reads) > 6:
            warnings.append(issue("W3", f"「{name}」读取 {len(reads)} 样东西：考虑先合成一份材料，或拆开这一步", item["id"]))
        if not clean(item.get("notes", "")):
            warnings.append(issue("W6", f"「{name}」还没有一句话说明", item["id"]))

    delivery = outline.get("delivery", [])
    for aid in delivery:
        if aid not in producers:
            errors.append(issue("S7", f"最终交付「{names.get(aid, aid)}」不是任何步骤的产出", None, aid))
    for aid in sorted({aid for aid in delivery if delivery.count(aid) > 1}):
        errors.append(issue("S7", f"最终交付「{names.get(aid, aid)}」重复列出", None, aid))
    if missing:
        if clean(request["name"]) != MISSING_REQUEST:
            errors.append(issue("S7", f"启动请求标为待定义时，名称必须是「{MISSING_REQUEST}」"))
        if read_request:
            errors.append(issue("S7", "启动请求待定义时，不能有步骤读取它"))
        if delivery:
            errors.append(issue("S7", "启动请求待定义时，不能指定最终交付"))
    else:
        if clean(request["name"]) in (MISSING_REQUEST, ""):
            errors.append(issue("S7", "已有启动请求时，请写明它的名称；整体目的不明确时把来处标为待定义"))
        if not read_request:
            errors.append(issue("S7", f"启动请求「{names[REQUEST]}」没有交给任何步骤：让第一步读取它"))
        if not delivery:
            errors.append(issue("S8", "已有启动请求，请指定最终交付的产物"))
    if from_request and missing:
        errors.append(issue("S10", "新需求必须有启动请求和最终交付"))
    for pid, producer in producers.items():
        if pid in readers or pid in delivery:
            continue
        message = f"「{product_names[pid]}」产出后没人读取，也不是最终交付：交给下游、设为最终交付，或并入别的产出"
        (warnings if missing else errors).append(issue("S9", message, producer["id"], pid))
    for resource in resources:
        if resource["id"] not in readers:
            warnings.append(issue("W4", f"资料「{clean(resource['name'])}」没有步骤读取", None, resource["id"]))
    owners = {}
    for item in order:
        for fid in item.get("finding_ids") or []:
            owners.setdefault(fid, {})[item["id"]] = step_title(item)
    for fid, steps in owners.items():
        if len(steps) > 1:
            warnings.append(issue("W5", "同一条原文要点归入了多个步骤：" + "、".join(steps.values())))
    if len(set(delivery)) > 3:
        warnings.append(issue("W7", f"最终交付有 {len(set(delivery))} 项：不是交付的请交给下游"))
    return {"errors": errors, "warnings": warnings, "handoffs": handoffs, "readers": readers}


def upgrade_v1(items):
    """Open a pre-contract skeleton (a bare list) for editing; it cannot pass as-is."""
    return {"request": {"name": MISSING_REQUEST, "provenance": "missing"}, "resources": [], "delivery": [],
            "modules": [{**item, "kind": None, "reads": [], "produces": None, "each": None} for item in items],
            "warnings": []}


def _hash(prefix, *parts):
    return prefix + hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def build_composite(item, children):
    """A composite only groups; Python writes it so its interface cannot be invented."""
    lines = []
    for child in children:
        product = (child.get("produces") or {}).get("name")
        lines.append(f"{child['name']}：{'产出「' + product + '」' if product else '组织其子步骤'}")
    return {"id": item["id"], "name": item["name"], "parent_id": item.get("parent_id"),
            "purpose": clean(item.get("notes", "")) or "组织子步骤：" + "、".join(child["name"] for child in children),
            "change": "；".join(lines) or "组织子步骤",
            "role": "【组合】只负责分组，不处理产物", "execution_kind": "composite",
            "plan": "### 组成\n" + "\n".join(f"- {line}" for line in lines) +
                    "\n\n### 边界与缺口\n- 输入输出由子步骤的读取与产出推出；内部交接见子步骤之间的连线。",
            "inputs": [], "outputs": [], "provenance": "inferred", "source_refs": [],
            "boundary_bindings": [], "generation": None, "needs_review": False}


def derive_wiring(outline, modules, request_port=None):
    """Compute every data edge, composite port and binding, entry and delivery from the skeleton.

    Edges join siblings under the readers' lowest common ancestor; composites pass an
    artifact through their own port with a boundary binding, never a cross-layer edge.
    """
    items = {item["id"]: item for item in outline["modules"]}
    parent = {mid: item.get("parent_id") for mid, item in items.items()}
    by_id = {module["id"]: dict(module) for module in modules}
    for module in by_id.values():
        module["inputs"] = [dict(port) for port in module.get("inputs", [])]
        module["outputs"] = [dict(port) for port in module.get("outputs", [])]
        module["boundary_bindings"] = [dict(binding) for binding in module.get("boundary_bindings", [])]
    names = artifact_names(outline)
    leaves = leaf_ids(outline["modules"])
    producer = {item["produces"]["id"]: item["id"] for item in outline["modules"]
                if item["id"] in leaves and item.get("produces")}
    readers = {}
    for item in display_order(outline["modules"]):
        for read in item.get("reads") or []:
            readers.setdefault(read["artifact"], []).append(item["name"])

    def source_port(aid):
        if aid == REQUEST:
            return request_port
        return next((port for port in by_id[producer[aid]]["outputs"] if port["id"] == aid), None)

    def chain(mid):
        path = [mid]
        while parent[path[-1]] is not None:
            path.append(parent[path[-1]])
        return path

    def ensure_port(owner, side, aid):
        module = by_id[owner]
        if any(port["id"] == aid for port in module[side]):
            return
        source = source_port(aid) or {}
        origin = "request" if aid == REQUEST else "upstream"
        if aid == REQUEST:
            description = "用户发起的启动请求"
        else:
            description = f"由「{items[producer[aid]]['name']}」产出"
            if readers.get(aid):
                description += "，交给「" + "」「".join(dict.fromkeys(readers[aid])) + "」"
        port = {"id": aid, "name": names[aid], "description": description,
                "format": source.get("format", "any"), "schema": source.get("schema", {}),
                "sample": source.get("sample", "示例待填写"), "required": True,
                "provenance": source.get("provenance", "inferred")}
        port["origin" if side == "inputs" else "role"] = origin if side == "inputs" else "primary"
        module[side].append(port)

    bindings, edges, entry, delivery = {}, {}, {}, {}

    def ensure_binding(owner, direction, aid, child, usage):
        key = (owner, direction, aid, child)
        if key in bindings:
            if usage == "handoff":
                bindings[key]["usage"] = "handoff"
            return
        bindings[key] = {"id": _hash("b-", owner, direction, aid, child), "direction": direction,
                         "parent_port": aid, "child_module": child, "child_port": aid,
                         "selection": "全文", "usage": usage}

    def route_down(path, stop, aid, usage):
        for node in path:
            if node == stop:
                return
            owner = parent[node]
            ensure_port(owner, "inputs", aid)
            ensure_binding(owner, "input", aid, node, usage)

    def route_up(path, stop, aid, usage):
        for node in path:
            if node == stop or parent[node] is None:
                return
            owner = parent[node]
            ensure_port(owner, "outputs", aid)
            ensure_binding(owner, "output", aid, node, usage)

    for item in display_order(outline["modules"]):
        if item["id"] not in leaves:
            continue
        for read in item.get("reads") or []:
            aid, usage = read["artifact"], read["usage"]
            if aid == REQUEST:
                path = chain(item["id"])
                route_down(path, path[-1], aid, usage)
                entry[(path[-1], REQUEST)] = {"module_id": path[-1], "port_id": REQUEST, "selection": "全文"}
                continue
            if aid not in producer:
                continue
            up, down = chain(producer[aid]), chain(item["id"])
            ancestor = next((node for node in up[1:] if node in down[1:]), None)
            source = next(node for node in up if parent[node] == ancestor)
            target = next(node for node in down if parent[node] == ancestor)
            route_up(up, source, aid, usage)
            route_down(down, target, aid, usage)
            key = (source, aid, target)
            if key in edges:
                if usage == "handoff":
                    edges[key]["usage"] = "handoff"
                continue
            edges[key] = {"id": _hash("d-", source, aid, target), "source": source, "target": target,
                          "source_port": aid, "target_port": aid, "kind": "data", "usage": usage,
                          "selection": "全文", "label": "", "needs_review": False}

    for aid in outline.get("delivery", []):
        if aid not in producer:
            continue
        up = chain(producer[aid])
        route_up(up, None, aid, "handoff")
        delivery[(up[-1], aid)] = {"module_id": up[-1], "port_id": aid}

    # One artifact, one contract: a reader inherits the producer's format and schema.
    for mid in leaves:
        module = by_id.get(mid)
        if module is None:
            continue
        for port in module["inputs"]:
            if port.get("origin") not in ("upstream", "request"):
                continue
            source = source_port(port["id"]) if (port["id"] in producer or port["id"] == REQUEST) else None
            if source:
                port["format"], port["schema"] = source.get("format", port["format"]), source.get("schema", port["schema"])
    for (owner, _, _, _), binding in bindings.items():
        by_id[owner]["boundary_bindings"].append(binding)
    return {"modules": [by_id[module["id"]] for module in modules], "edges": list(edges.values()),
            "entry_points": list(entry.values()), "delivery_ports": list(delivery.values())}
