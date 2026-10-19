"""Insert one step between two cards on the same level.

Code picks the link, builds the new step's fixed input and the product the next step
will read, rewires the two edges and checks the whole project; the AI only writes the
step's wording (see ai.insert_step). Everything here is pure: nothing is saved.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import re

from . import contract
from .design import design_issues
from .readme import BOM, H1
from .runs import effective_leaves
from .store import WHOLE_SELECTIONS, integrity

PORT_KEYS = ("name", "description", "format", "schema", "sample")
PLACEHOLDER_TEXT = "待设计"


class InsertRefused(ValueError):
    """Plain Chinese for gui; the app turns it into a 422."""


# ---------------------------------------------------------------- step codes (stepDesk.ts)

# The separator must not be followed by a digit, so "2.5 合并结果" and "3:00 开会" stay whole.
# [0-9] and [a-zA-Z]: JavaScript's \d and /i match ASCII only.
CODE = re.compile(r"\s*([0-9]{1,3}[a-zA-Z]?(?:-[a-zA-Z0-9]+)*)\s*[·・.、:：](?![0-9])\s*(\S[^\n\r  ]*)")


def split_code(name: str) -> tuple[str | None, str]:
    """"09a · 文学诊断" → ("09a", "文学诊断"); a name without a code is kept whole."""
    match = CODE.fullmatch(name)
    if not match:
        return None, contract.clean(name) or "未命名步骤"
    return match[1], contract.clean(match[2])


def display_code(code: str) -> str:
    """Pad the number to two digits and drop any "-suffix": "5b" → "05b", "12a-primary" → "12a"."""
    head = code.split("-")[0]
    match = re.fullmatch(r"([0-9]+)(.*)", head, re.S)
    return match[1].rjust(2, "0") + match[2].lower() if match else head


def _code_labels(codes: list[str | None]) -> list[str | None]:
    """Steps that shorten to the same code get a running number: "12a·1", "12a·2"."""
    short = [display_code(code) if code else None for code in codes]
    total, seen, labels = Counter(s for s in short if s), Counter(), []
    for s in short:
        if not s or total[s] < 2:
            labels.append(s)
            continue
        seen[s] += 1
        labels.append(f"{s}·{seen[s]}")
    return labels


def _level(module: dict) -> str | None:
    return module.get("parent_id") or None


def _rank(code: str | None) -> tuple[int, str] | None:
    """Where a code sorts on its level: "05b" → (5, "b"), "5" → (5, ""); "05" < "05a" < "05b" < "06"."""
    match = re.fullmatch(r"([0-9]+)([a-z]?)", display_code(code)) if code else None
    return (int(match[1]), match[2]) if match else None


def level_code(project: dict, after_id: str, before_id: str) -> str | None:
    """The code for a step between `after_id` and `before_id`: "02"/"03" → "02a", "05b"/"06" → "05c".

    It always sorts strictly between the two, so the cards keep reading in order: the next free
    letter after `after_id`'s code, while it stays below `before_id`'s. When `before_id` has no
    code (its card shows "—"), the next code on this level bounds the new one instead.
    None when the desk numbers this level by position (fewer than half the steps carry a code),
    when `after_id` has no code, or when no letter fits ("09a"/"09b", "02"/"02a"): the new step
    then keeps its name without a code and its card shows "—".

    Except when one more uncoded step would leave fewer than half the level coded: the desk would
    then number every card by position, and gui's 09a and 09b would read 01 and 03. The new step
    then takes the last code before it with a "-n" suffix ("09a" → "09a-2"), which the desk tells
    apart in reading order: "09a·1", "09a·2", then "09b".
    """
    modules = _modules(project)
    after, before = modules.get(after_id), modules.get(before_id)
    if after is None or before is None:
        return None
    siblings = [m for m in project["modules"] if _level(m) == _level(after)]
    codes = [split_code(m["name"])[0] for m in siblings]
    coded = [label for label in _code_labels(codes) if label]
    if not coded or len(coded) * 2 < len(siblings) or len({label.lower() for label in coded}) != len(coded):
        return None
    tips = len(coded) * 2 < len(siblings) + 1
    used = {_rank(code) for code in codes} - {None}
    low, high = _rank(split_code(after["name"])[0]), _rank(split_code(before["name"])[0])
    if low is None:
        if not tips or high is None:
            return None
        # The step before shows "—": the new one follows the last code below the step after.
        low = max((rank for rank in used if rank < high), default=None)
        if low is None:
            return None
    if high is None:
        high = min((rank for rank in used if rank > low), default=None)
    number = f"{low[0]:02d}"
    for letter in map(chr, range(ord(low[1]) + 1 if low[1] else ord("a"), ord("z") + 1)):
        if high is not None and (low[0], letter) >= high:
            break
        if (low[0], letter) not in used:
            return number + letter
    if not tips:
        return None
    taken = {code.lower() for code in codes if code}
    suffix = 2
    while f"{number}{low[1]}-{suffix}" in taken:
        suffix += 1
    return f"{number}{low[1]}-{suffix}"


def step_call(module: dict) -> str:
    """How plain text for gui names a step: 「第 02 步」, or its name when it has no code."""
    code, title = split_code(module["name"])
    return f"第 {display_code(code)} 步" if code else f"「{title}」"


# ---------------------------------------------------------------- where a step fits

def _port(module: dict | None, side: str, port_id: object) -> dict | None:
    return next((p for p in (module or {}).get(side, []) if p["id"] == port_id), None)


def _modules(project: dict) -> dict:
    return {m["id"]: m for m in project["modules"]}


def _whole(edge: dict) -> bool:
    return str(edge.get("selection", "")).strip().lower() in WHOLE_SELECTIONS


def candidates(project: dict, after_id: str, before_id: str) -> list[dict]:
    """Handoff data edges after → before whose ports exist; those from a primary port first and only."""
    modules = _modules(project)
    after, before = modules.get(after_id), modules.get(before_id)
    if after is None or before is None:
        return []
    found = [e for e in project["edges"]
             if e.get("kind") == "data" and e["source"] == after_id and e["target"] == before_id
             and e.get("usage", "handoff") == "handoff"
             and _port(after, "outputs", e.get("source_port")) and _port(before, "inputs", e.get("target_port"))]
    primary = [e for e in found if _port(after, "outputs", e["source_port"]).get("role") == "primary"]
    return primary or found


def _free_id(taken: set, base: str) -> str:
    if base not in taken:
        return base
    base = base[:77]
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _named(port: dict) -> dict:
    return {"id": port["id"], "name": contract.clean(port.get("name") or "") or port["id"]}


def spot(project: dict, after_id: str, before_id: str, *, edge_id: str | None = None,
         units=(), ignored: dict | None = None) -> dict:
    """Whether a step can go between two cards, and on which handoff; InsertRefused says why not.

    units: composites run as one step (Runs.units); ignored: {step id: input ids it does without}.
    With several possible handoffs and no edge_id, `edge_id` is None and `choices` lists them.
    """
    modules = _modules(project)
    after, before = modules.get(after_id), modules.get(before_id)
    if after is None or before is None:
        raise InsertRefused("找不到这一步，可能刚改过工作流，请刷新后再试")
    if after_id == before_id or _level(after) != _level(before):
        raise InsertRefused("这两步不在同一层")
    found = candidates(project, after_id, before_id)
    if not found:
        raise InsertRefused(f"{step_call(after)}和{step_call(before)}之间没有直接交接的东西，这里没法插一步")
    if edge_id is not None and edge_id not in {e["id"] for e in found}:
        raise InsertRefused("选的这样东西已经不在了，请重新选")
    _, owner, _ = effective_leaves(project, units)
    if after_id in owner or before_id in owner:
        raise InsertRefused("这一组是一起跑的，里面不能单独加一步")
    skipped = set((ignored or {}).get(before_id) or ())
    base = _issue_keys(project)

    def refusal(edge):
        if edge["target_port"] in skipped:
            return InsertRefused(f"{step_call(before)}设成不读这样东西，加了也用不上")
        # A stand-in step catches hollow groups and other gaps before any AI call.
        module = placeholder(project, edge)
        new_edge_id = _free_id({e["id"] for e in project["edges"]}, "insert_edge")
        trial = place(project, module, edge, new_edge_id)
        issues = _diff(base, trial, module, edge, new_edge_id)
        if not issues:
            return None
        name = _named(_port(after, "outputs", edge["source_port"]))["name"]
        codes = {issue.get("code") for issue in issues if issue.get("module_id") == module["id"]}
        # Hollow groups: the producer group hands nothing out, or the reader group passes nothing in.
        if "upstream_unfed" in codes:
            return InsertRefused(f"{step_call(after)}这一组里没有哪一步交出《{name}》，这里没法插一步")
        if "unconsumed_product" in codes:
            return InsertRefused(f"{step_call(before)}这一组里没有哪一步读《{name}》，这里没法插一步")
        return InsertRefused(f"这里加一步会让工作流多出问题，没法插：{issues[0]['message']}")

    checked = [(edge, refusal(edge)) for edge in found]
    viable = [edge for edge, problem in checked if problem is None]
    if edge_id is not None:
        edge, problem = next(item for item in checked if item[0]["id"] == edge_id)
        if problem:
            raise problem
    elif not viable:
        raise checked[0][1]
    else:
        edge = viable[0] if len(viable) == 1 else None
    names = [_named(_port(after, "outputs", e["source_port"]))["name"] for e in viable]
    choices = [] if len(viable) < 2 else [
        {"edge_id": e["id"], "name": name if names.count(name) == 1
         else f"{name}（交给「{_named(_port(before, 'inputs', e['target_port']))['name']}」）"}
        for e, name in zip(viable, names)]
    away = {edge["id"]} if edge else {e["id"] for e in found}
    kept = [_named(_port(after, "outputs", e.get("source_port")))["name"]
            for e in project["edges"] if e.get("kind") == "data" and e["source"] == after_id
            and e["target"] == before_id and e["id"] not in away and _port(after, "outputs", e.get("source_port"))]
    return {"after": after_id, "before": before_id, "edge_id": edge["id"] if edge else None, "choices": choices,
            "handed": _named(_port(after, "outputs", edge["source_port"])) if edge else None,
            "reads": _named(_port(before, "inputs", edge["target_port"])) if edge else None,
            "code": level_code(project, after_id, before_id), "kept": kept}


# ---------------------------------------------------------------- the new step

def fixed_ports(project: dict, edge: dict) -> tuple[dict, dict | None]:
    """(the new step's input, what the next step accepts or None when it never said).

    A whole handoff copies the producer's port; a field path copies the reader's port,
    because the new step reads what the old edge selected.
    """
    modules = _modules(project)
    after, before = modules[edge["source"]], modules[edge["target"]]
    source, target = _port(after, "outputs", edge["source_port"]), _port(before, "inputs", edge["target_port"])
    whole = _whole(edge)
    base = source if whole else target
    defaults = {"name": "", "description": "", "format": "any", "schema": {}, "sample": ""}
    port = {key: deepcopy(base.get(key, defaults[key])) for key in PORT_KEYS}
    port["id"] = edge["source_port"] if whole else edge["target_port"]
    if not contract.clean(port["name"]):
        port["name"] = target.get("name", "")
    if not contract.clean(port["description"]):
        port["description"] = contract.clean(target.get("description", "")) or \
            f"{step_call(after)}交出的《{contract.clean(port['name']) or port['id']}》"
    port.update(required=True, provenance=base.get("provenance", "inferred"), origin="upstream")
    product = None if target.get("format", "any") == "any" else \
        {key: deepcopy(target.get(key, defaults[key])) for key in ("format", "schema", "sample")}
    return port, product


def _product_id(project: dict, edge: dict, wanted: str, own: list[dict]) -> str:
    """The product's id, suffixed when N's own ports or the two neighbours already use it."""
    modules = _modules(project)
    taken = {p["id"] for p in own}
    for mid in (edge["source"], edge["target"]):
        taken |= {p["id"] for side in ("inputs", "outputs") for p in modules[mid].get(side, [])}
    return _free_id(taken, wanted)


def _free_module_id(project: dict, base: str) -> str:
    return _free_id({m["id"] for m in project["modules"]}, base)


def placeholder(project: dict, edge: dict) -> dict:
    """A typed stand-in for the new step, used to check a spot and as the AI's context."""
    port, product_contract = fixed_ports(project, edge)
    code = level_code(project, edge["source"], edge["target"])
    title = "新的一步"
    product = {"id": _product_id(project, edge, "new_product", [port]), "name": "新的一步的产物",
               "description": PLACEHOLDER_TEXT, "format": "text", "schema": {"type": "string"},
               "sample": PLACEHOLDER_TEXT, "required": True, "provenance": "inferred", "role": "primary"}
    if product_contract:
        product.update(deepcopy(product_contract))
    plan = (f"# {title}\n\n> {PLACEHOLDER_TEXT}\n\n### 输入检查\n- {PLACEHOLDER_TEXT}\n\n"
            f"### 处理步骤\n1. {PLACEHOLDER_TEXT}\n\n### 边界与缺口\n- {PLACEHOLDER_TEXT}")
    return {"id": _free_module_id(project, "new_step"), "name": f"{code} · {title}" if code else title,
            "purpose": PLACEHOLDER_TEXT, "change": PLACEHOLDER_TEXT, "role": PLACEHOLDER_TEXT, "plan": plan,
            "execution_kind": "ai", "parent_id": _modules(project)[edge["target"]].get("parent_id"),
            "inputs": [port], "outputs": [product], "acceptance": [{"check": PLACEHOLDER_TEXT, "by": "human"}],
            "provenance": "inferred", "source_refs": [], "generation": None, "needs_review": False}


def _reject_constant(value: str):
    raise ValueError(f"JSON 不允许非有限数值：{value}")


def _schema(value: object) -> object:
    """A JSON-string schema as an object; anything that is not one is left for ai._normalise_port to report."""
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value, parse_constant=_reject_constant)
    except ValueError:
        return value
    return parsed if isinstance(parsed, dict) else value


def _answer_port(port: dict, **extra) -> dict:
    result = {key: deepcopy(port.get(key, default)) for key, default in
              (("id", ""), ("name", ""), ("description", ""), ("format", "any"), ("schema", {}), ("sample", ""),
               ("required", True), ("provenance", "inferred"))}
    result["schema"] = _schema(result["schema"])
    return {**result, **extra}


def _titled(plan: str, title: str) -> str:
    """The plan with `# title` as its first line: the H1 rewritten when there is one, else added."""
    text = plan.removeprefix(BOM)
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip(" \t\r")), None)
    if first is not None and H1.match(lines[first]):
        lines[first] = f"# {title}"
        return "\n".join(lines[first:])
    return f"# {title}\n\n{text.lstrip(chr(10) + chr(13))}"


def assemble(project: dict, edge: dict, answer: dict, module_id: str, code: str | None) -> dict:
    """The new step from the AI's wording; ports, parent and links are code's.

    answer: the AI's fields (title, purpose, change, role, plan, execution_kind, product,
    per_item, extra_inputs, records, acceptance); port schemas arrive as JSON strings.
    """
    port, product_contract = fixed_ports(project, edge)
    title = " ".join(str(answer.get("title") or "").split())
    stripped_code, rest = split_code(title)
    if stripped_code:
        title = rest
    extra = [_answer_port(p, origin=p.get("origin")) for p in answer.get("extra_inputs") or []]
    records = [_answer_port(p, role="record") for p in answer.get("records") or []]
    asked = answer.get("product") or {}
    product = {**_answer_port({**asked, "required": True, "provenance": "inferred"}), "role": "primary"}
    if product_contract:
        product.update(deepcopy(product_contract))
    product["id"] = _product_id(project, edge, product["id"] or "product", [port, *extra, *records])
    module = {"id": module_id, "name": f"{code} · {title}" if code and title else title,
              "purpose": answer.get("purpose", ""), "change": answer.get("change", ""), "role": answer.get("role", ""),
              "plan": _titled(answer.get("plan") or "", title), "execution_kind": answer.get("execution_kind"),
              "parent_id": _modules(project)[edge["target"]].get("parent_id"),
              "inputs": [port, *extra], "outputs": [product, *records],
              "acceptance": deepcopy(answer.get("acceptance") or []),
              "provenance": "inferred", "source_refs": [], "generation": None, "needs_review": False}
    if answer.get("per_item"):
        module["for_each"] = port["id"]
    return module


def _primary(module: dict) -> dict:
    return next((p for p in module["outputs"] if p.get("role") == "primary"), module["outputs"][0])


def place(project: dict, module: dict, edge: dict, new_edge_id: str) -> dict:
    """A copy of the project with the step before the reader and the handoff routed through it.

    The old edge keeps its id, usage, label and review flag; it now runs new step → reader.
    Every other edge, and every other reader of the producer, stays as it is.
    """
    trial = deepcopy(project)
    at = next(i for i, m in enumerate(trial["modules"]) if m["id"] == edge["target"])
    trial["modules"].insert(at, deepcopy(module))
    old = next(e for e in trial["edges"] if e["id"] == edge["id"])
    trial["edges"].append({"id": new_edge_id, "source": edge["source"], "source_port": edge["source_port"],
                           "target": module["id"], "target_port": module["inputs"][0]["id"], "kind": "data",
                           "usage": "handoff", "selection": edge.get("selection") or "全文", "label": "",
                           "needs_review": False})
    old.update(source=module["id"], source_port=_primary(module)["id"], selection="全文")
    return trial


# ---------------------------------------------------------------- no new issue

def _key(issue: dict) -> tuple:
    what = issue.get("code") or issue["message"].split("（")[0]
    return (issue.get("module_id"), what, issue.get("port_id"), issue.get("edge_id"), issue.get("binding_id"))


def _issue_keys(project: dict) -> set:
    # The same composition as Store.load.
    return {_key(issue) for issue in integrity(project) + design_issues(project)}


def _diff(before_keys: set, trial: dict, module: dict, edge: dict, new_edge_id: str) -> list[dict]:
    port_id = module["inputs"][0]["id"]
    # The new input copies a port (the producer's, or the reader's for a field path):
    # a gap that port already had is carried, not new.
    copied = [(edge["source"], edge["source_port"])] + ([] if _whole(edge) else [(edge["target"], edge["target_port"])])
    carried = {(module["id"], code, port_id, None, None) for code in ("unknown_artifact", "unnamed_artifact")
               for mid, pid in copied if (mid, code, pid, None, None) in before_keys}
    found, seen = [], set()
    for issue in integrity(trial) + design_issues(trial):
        key = _key(issue)
        # The new edge carries the old edge's selection: what it reports moved, it is not new.
        if issue.get("edge_id") == new_edge_id:
            key = (edge["target"], key[1], None, edge["id"], key[4])
        if key in before_keys or key in carried or key in seen:
            continue
        seen.add(key)
        found.append(issue)
    return found


def new_issues(before: dict, trial: dict, module: dict, edge: dict, new_edge_id: str) -> list[dict]:
    """Issues the trial project has that `before` did not, anywhere, including on the new step itself."""
    return _diff(_issue_keys(before), trial, module, edge, new_edge_id)
