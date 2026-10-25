"""论文阅读进度：把 Flowdesk 里已通过的「阅读进度条目」汇总成本地记录，Codex 能直接读，也能放进「提醒事项」。

用法（一般双击同文件夹里的 .command 就行）：
    python3 reading_progress.py update            汇总，写到 ~/Documents/论文阅读进度/
    python3 reading_progress.py reminders         把每篇的「下一步」放进「提醒事项」的「论文阅读」列表
    python3 reading_progress.py reminders --dry   只列出会放进去的提醒，不真的放
    python3 reading_progress.py pdf2txt a.pdf     把 PDF 转成带页码的文字版（a.txt，放在 PDF 旁边）

只用 Python 自带的东西。读 Flowdesk 的项目文件夹，但从不改它。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

KIND = "论文阅读进度"
HOME = Path.home()
OUT = Path(os.environ.get("READING_PROGRESS_DIR") or HOME / "Documents" / "论文阅读进度")
LIST_NAME = os.environ.get("READING_REMINDERS_LIST") or "论文阅读"


# ---------- 找到 Flowdesk 的项目 ----------

def project_roots() -> list[Path]:
    """Flowdesk 存项目的文件夹：环境变量指定的、试用版 ~/Flowdesk/projects、Dock 图标指向的那份。"""
    found: list[Path] = []
    for raw in filter(None, os.environ.get("FLOWDESK_PROJECTS", "").split(os.pathsep)):
        found.append(Path(raw).expanduser())
    found.append(HOME / "Flowdesk" / "projects")
    for apps in (Path("/Applications"), HOME / "Applications"):
        launcher = apps / "Flowdesk.app" / "Contents" / "Resources" / "launcher.zsh"
        try:
            match = re.search(r'^REPO="(.*)"$', launcher.read_text(encoding="utf-8"), re.M)
        except OSError:
            continue
        if match:
            found.append(Path(match.group(1)) / "projects")
    unique: list[Path] = []
    for path in found:
        try:
            real = path.resolve()
        except OSError:
            continue
        if real.is_dir() and real not in unique:
            unique.append(real)
    return unique


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def entry_from_text(text: str):
    """一份产物里的阅读进度条目：整份是 JSON，或 Markdown 里的一个 ```json 代码块。"""
    candidates = [text]
    candidates += re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    for chunk in candidates:
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("记录类型") == KIND:
            return data
    return None


def flowdesk_entries(roots: list[Path]) -> list[dict]:
    """每条运行里，已通过的那一版阅读进度条目（只收你点过「通过」的）。"""
    entries = []
    for root in roots:
        for run_file in sorted(root.glob("*/runs/*/run.json")):
            run = _read_json(run_file)
            if not isinstance(run, dict):
                continue
            project_dir = run_file.parent.parent.parent
            project = _read_json(project_dir / "project.json") or {}
            best = None
            for step_id, rec in (run.get("steps") or {}).items():
                version = (rec or {}).get("approved")
                if not version:
                    continue
                folder = run_file.parent / step_id / f"v{int(version)}"
                meta = _read_json(folder / "meta.json") or {}
                for path in sorted(folder.glob("*")):
                    if path.name == "meta.json" or not path.is_file() or path.stat().st_size > 2_000_000:
                        continue
                    try:
                        text = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    data = entry_from_text(text)
                    if data:
                        when = str(meta.get("created_at") or "")
                        if best is None or when > best[0]:
                            best = (when, data)
            if best:
                entries.append({**best[1], "_来自": {
                    "Flowdesk项目": project.get("name") or project_dir.name,
                    "这一条": run.get("title") or run_file.parent.name,
                    "通过时间": best[0],
                }})
    return entries


def manual_entries(folder: Path) -> list[dict]:
    """「手动添加」文件夹里的条目（你或 Codex 直接写的 .json）。"""
    entries = []
    for path in sorted(folder.glob("*.json")):
        data = _read_json(path)
        if isinstance(data, dict) and data.get("记录类型") == KIND:
            entries.append({**data, "_来自": {"手动添加": path.name}})
    return entries


# ---------- 汇总 ----------

def paper_key(entry: dict) -> str:
    paper = entry.get("论文") or {}
    doi = str(paper.get("DOI") or "").strip().lower()
    if doi:
        return "doi:" + re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    title = re.sub(r"\W+", "", str(paper.get("标题") or "").lower())
    return "title:" + title


def merge(entries: list[dict]) -> list[dict]:
    """同一篇论文只留最新的一条（按日期，其次按通过时间），按日期从新到旧排。"""
    newest: dict[str, dict] = {}
    for entry in entries:
        if not str((entry.get("论文") or {}).get("标题") or "").strip():
            continue
        key = paper_key(entry)
        rank = (str(entry.get("日期") or ""), str((entry.get("_来自") or {}).get("通过时间") or ""))
        old = newest.get(key)
        if old is None or rank >= (str(old.get("日期") or ""), str((old.get("_来自") or {}).get("通过时间") or "")):
            newest[key] = entry
    return sorted(newest.values(), key=lambda e: str(e.get("日期") or ""), reverse=True)


def _cell(value) -> str:
    return str(value or "").replace("|", "／").replace("\n", " ").strip()


def _items(value) -> list:
    return [v for v in value if v] if isinstance(value, list) else []


def render_markdown(papers: list[dict]) -> str:
    lines = ["# 论文阅读进度", "",
             f"更新于 {datetime.now().strftime('%Y-%m-%d %H:%M')}，共 {len(papers)} 篇。"
             "这个文件由「更新阅读进度」自动生成，手改会被覆盖；要补一篇，往「手动添加」文件夹放一个 .json。", "",
             "| 日期 | 论文 | 状态 | 采用 | 一句话结论 | 待办 |", "|---|---|---|---|---|---|"]
    for p in papers:
        paper = p.get("论文") or {}
        todo = len(_items(p.get("下一步")))
        lines.append(f"| {_cell(p.get('日期'))} | {_cell(paper.get('标题'))} | {_cell(p.get('状态'))} | "
                     f"{_cell(p.get('采用'))} | {_cell(p.get('一句话结论'))} | {todo or ''} |")
    for p in papers:
        paper = p.get("论文") or {}
        lines += ["", f"## {_cell(paper.get('标题'))}", ""]
        who = "，".join(_cell(paper.get(k)) for k in ("作者", "年份", "期刊") if paper.get(k))
        if who:
            lines.append(f"- 文献：{who}")
        if paper.get("DOI"):
            lines.append(f"- DOI：{_cell(paper.get('DOI'))}")
        lines.append(f"- 日期：{_cell(p.get('日期'))}　状态：{_cell(p.get('状态'))}　采用：{_cell(p.get('采用'))}")
        if p.get("一句话结论"):
            lines.append(f"- 一句话结论：{_cell(p.get('一句话结论'))}")
        if p.get("我的评价"):
            lines.append(f"- 我的评价：{_cell(p.get('我的评价'))}")
        if _items(p.get("标签")):
            lines.append("- 标签：" + "、".join(_cell(t) for t in _items(p.get("标签"))))
        claims = [c for c in _items(p.get("主要结论")) if isinstance(c, dict)]
        if claims:
            lines += ["", "**主要结论**", ""]
            for c in claims:
                lines.append(f"- {_cell(c.get('编号'))} {_cell(c.get('结论'))}（支持范围：{_cell(c.get('支持范围')) or '未写'}；"
                             f"证据强度：{_cell(c.get('证据强度')) or '未写'}）")
        gaps = [g for g in _items(p.get("仍有缺口")) if isinstance(g, dict)]
        if gaps:
            lines += ["", "**仍有缺口**", ""]
            for g in gaps:
                lines.append(f"- {_cell(g.get('缺口'))}（影响：{_cell(g.get('影响哪条结论')) or '未写'}；怎么补：{_cell(g.get('怎么补')) or '未写'}）")
        look = _items(p.get("要回原文看的"))
        if look:
            lines += ["", "**要回原文看的**", ""] + [f"- {_cell(x)}" for x in look]
        steps = [s for s in _items(p.get("下一步")) if isinstance(s, dict)]
        if steps:
            lines += ["", "**下一步**", ""]
            for s in steps:
                due = f"（{_cell(s.get('截止日期'))} 前）" if s.get("截止日期") else ""
                lines.append(f"- [ ] {_cell(s.get('要做的事'))}{due}")
        source = p.get("_来自") or {}
        if source:
            lines += ["", "来自：" + "，".join(f"{k} {_cell(v)}" for k, v in source.items() if v)]
    return "\n".join(lines) + "\n"


AGENTS = """# 这个文件夹是什么（给 Codex 和其他 AI 看）

这里是我读过的学术论文的进度记录，由 Flowdesk 的「学术论文阅读」流程产出，再由「更新阅读进度」汇总。

- `阅读进度.json`：全部论文，一个数组，每篇一个对象，字段见下。读数据时优先读它。
- `阅读进度.md`：同样内容的可读版，开头一张总表，后面每篇一节。
- `手动添加/`：手动补的条目，每篇一个 .json，字段和下面一样，必须有 `"记录类型": "论文阅读进度"`。
  你（Codex）要替我记一篇时，就在这里新建一个文件，文件名用论文短标题；不要直接改 `阅读进度.json` 和 `阅读进度.md`，它们每次更新都会重新生成。

每篇的字段：
- `论文`：`标题`、`作者`、`年份`、`期刊`、`DOI`
- `日期`（YYYY-MM-DD）、`状态`（已读完 / 读了一部分 / 暂停）、`采用`（采用 / 暂不采用 / 部分采用）
- `一句话结论`、`我的评价`、`标签`（数组）
- `主要结论`：数组，每项 `编号`、`结论`、`支持范围`、`证据强度`
- `仍有缺口`：数组，每项 `缺口`、`影响哪条结论`、`怎么补`
- `要回原文看的`：数组
- `下一步`：数组，每项 `要做的事`、`截止日期`（可空）
- `_来自`：这条记录来自哪个 Flowdesk 项目、哪一条运行，或哪个手动文件

回答我关于“读过什么、读到哪、还欠什么”的问题时，以这里的记录为准；记录里没有的，就说没有，不要猜。
"""


def update(quiet=False) -> list[dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    manual = OUT / "手动添加"
    manual.mkdir(exist_ok=True)
    roots = project_roots()
    papers = merge(flowdesk_entries(roots) + manual_entries(manual))
    (OUT / "阅读进度.json").write_text(json.dumps(papers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "阅读进度.md").write_text(render_markdown(papers), encoding="utf-8")
    (OUT / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
    if not quiet:
        print(f"找了这些 Flowdesk 项目文件夹：{', '.join(map(str, roots)) or '（没找到）'}")
        print(f"汇总了 {len(papers)} 篇论文，写在：{OUT}")
    return papers


# ---------- 提醒事项 ----------

REMINDERS_JXA = r"""
function run(argv) {
  var items = JSON.parse(argv[0]); var listName = argv[1];
  var app = Application('Reminders');
  var lists = app.lists.whose({name: listName});
  var list = lists.length ? lists[0] : null;
  if (!list) { list = app.List({name: listName}); app.lists.push(list); list = app.lists.whose({name: listName})[0]; }
  var made = 0;
  items.forEach(function (it) {
    var props = {name: it.name, body: it.body};
    if (it.due) { var d = new Date(it.due + 'T09:00:00'); if (!isNaN(d)) props.dueDate = d; }
    list.reminders.push(app.Reminder(props)); made++;
  });
  return String(made);
}
"""


def reminder_items(papers: list[dict]) -> list[dict]:
    items = []
    for p in papers:
        title = str((p.get("论文") or {}).get("标题") or "").strip()
        for step in _items(p.get("下一步")):
            if not isinstance(step, dict) or not str(step.get("要做的事") or "").strip():
                continue
            todo = str(step["要做的事"]).strip()
            due = str(step.get("截止日期") or "").strip()
            due = due if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else ""
            key = hashlib.sha1(f"{paper_key(p)}|{todo}".encode("utf-8")).hexdigest()[:16]
            items.append({"key": key, "name": f"{todo}（{title[:40]}）", "due": due,
                          "body": f"论文：{title}\n来自：{OUT / '阅读进度.md'}"})
    return items


def reminders(dry=False):
    papers = update(quiet=True)
    sent_file = OUT / ".已放进提醒事项.json"
    sent = set(_read_json(sent_file) or [])
    todo = [i for i in reminder_items(papers) if i["key"] not in sent]
    if not todo:
        print("没有新的待办要放进提醒事项。")
        return
    for item in todo:
        print(f"- {item['name']}" + (f"，截止 {item['due']}" if item["due"] else ""))
    if dry:
        print(f"（只是预览）会放进「提醒事项」的「{LIST_NAME}」列表，共 {len(todo)} 条。")
        return
    if sys.platform != "darwin":
        sys.exit("提醒事项只在 Mac 上能用。")
    payload = [{k: i[k] for k in ("name", "body", "due")} for i in todo]
    result = subprocess.run(["osascript", "-l", "JavaScript", "-e", REMINDERS_JXA, json.dumps(payload, ensure_ascii=False), LIST_NAME],
                            capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("没放进去：" + (result.stderr.strip() or "提醒事项没有回应。")
                 + "\n如果 Mac 问要不要允许控制「提醒事项」，请点「好」，再双击一次。")
    sent.update(i["key"] for i in todo)
    sent_file.write_text(json.dumps(sorted(sent)), encoding="utf-8")
    print(f"放好了：{result.stdout.strip()} 条，在「提醒事项」的「{LIST_NAME}」列表里。同一件事不会重复放。")


# ---------- PDF 转文字 ----------

PDF_JXA = r"""
ObjC.import('Foundation'); ObjC.import('PDFKit');
function run(argv) {
  var doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  if (!doc || doc.isNil()) { throw new Error('打不开这个 PDF'); }
  var n = doc.pageCount, pages = [], empty = [];
  for (var i = 0; i < n; i++) {
    var s = doc.pageAtIndex(i).string;
    var text = (s && !s.isNil()) ? s.js : '';
    if (!text.replace(/\s/g, '')) empty.push(i + 1);
    pages.push('=== 第 ' + (i + 1) + ' 页 ===\n' + text.trim());
  }
  var head = '来源文件：' + argv[0].split('/').pop() + '\n总页数：' + n + '\n没转出文字的页：' + (empty.length ? empty.join('、') : '无') + '\n\n';
  return head + pages.join('\n\n') + '\n';
}
"""


def pdf2txt(paths: list[str]):
    if sys.platform != "darwin":
        sys.exit("PDF 转文字用的是 Mac 自带的功能，只能在 Mac 上用。")
    for raw in paths:
        pdf = Path(raw).expanduser()
        if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
            print(f"跳过（不是 PDF）：{pdf}")
            continue
        result = subprocess.run(["osascript", "-l", "JavaScript", "-e", PDF_JXA, str(pdf)], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"没转成：{pdf.name}：{result.stderr.strip()}")
            continue
        target = pdf.with_suffix(".txt")
        target.write_text(result.stdout, encoding="utf-8")
        first = result.stdout.splitlines()[:3]
        print(f"转好了：{target}\n  " + "\n  ".join(first))


def main(argv=None):
    parser = argparse.ArgumentParser(description="论文阅读进度")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update")
    r = sub.add_parser("reminders")
    r.add_argument("--dry", action="store_true")
    p = sub.add_parser("pdf2txt")
    p.add_argument("pdf", nargs="+")
    args = parser.parse_args(argv)
    if args.cmd == "update":
        update()
    elif args.cmd == "reminders":
        reminders(dry=args.dry)
    else:
        pdf2txt(args.pdf)


if __name__ == "__main__":
    main()
