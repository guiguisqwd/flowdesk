"""Audit a saved project against Flowdesk's step rules, without changing it.

    python -m workbench.check <project-id or folder> [--data-dir DIR] [--json] [--strict]

Exit codes: 0 audited, 1 findings with --strict, 2 the project could not be read.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

from .design import AUDIT_LABELS, audit
from .store import Store

# The same default as the server (workbench.app), whatever directory the command runs in.
DATA_DIR = os.environ.get("WORKBENCH_PROJECTS", str(Path(__file__).resolve().parent.parent / "projects"))


def report(project):
    modules = project["modules"]
    parents = {m.get("parent_id") for m in modules}
    findings = audit(project)
    return {"id": project["id"], "name": project["name"], "modules": len(modules),
            "steps": sum(1 for m in modules if m["id"] not in parents),
            "counts": dict(Counter(item["code"] for item in findings)), "findings": findings,
            "existing_issues": len(project.get("issues", [])), "names": {m["id"]: m["name"] for m in modules}}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m workbench.check", description="按 Flowdesk 步骤规则体检一个已保存的项目（只读）")
    parser.add_argument("project", help="项目标识（projects/ 下的文件夹名）或项目文件夹路径")
    parser.add_argument("--data-dir", default=DATA_DIR, help="项目所在目录，默认仓库里的 projects/")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--strict", action="store_true", help="发现任何问题时以 1 退出")
    args = parser.parse_args(argv)
    path = Path(args.project)
    root, project_id = (path.resolve().parent, path.resolve().name) if path.is_dir() else (Path(args.data_dir), args.project)
    # Look before constructing Store, which would create a missing data directory.
    if not (root / project_id / "project.json").is_file():
        print(f"找不到项目：{root / project_id}（需要其中有 project.json）", file=sys.stderr)
        return 2
    try:
        result = report(Store(root).load(project_id))
    except ValueError as error:  # an invalid id or a broken project file
        print(f"无法读取项目 {project_id}：{error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        names = {item["module_id"]: item["module_id"] for item in result["findings"]} | result["names"]
        print(f"{result['name']}（{result['id']}）：{result['modules']} 个模块，其中 {result['steps']} 个具体步骤")
        print("按步骤规则体检：" + ("没有发现问题" if not result["findings"] else ""))
        for code, label in AUDIT_LABELS.items():
            if code in result["counts"]:
                steps = len({item["module_id"] for item in result["findings"] if item["code"] == code})
                print(f"  {label}：{result['counts'][code]} 处（{steps} 步）")
        print(f"现有完整性与模板检查：{result['existing_issues']} 条（见工作台「待检查」）")
        for module_id in dict.fromkeys(item["module_id"] for item in result["findings"]):
            codes = Counter(item["code"] for item in result["findings"] if item["module_id"] == module_id)
            print(f"- {names.get(module_id, module_id)}：" + "；".join(
                AUDIT_LABELS[code] + (f" ×{n}" if n > 1 else "") for code, n in codes.items()))
    return 1 if args.strict and result["findings"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # piped into head
        sys.exit(0)
