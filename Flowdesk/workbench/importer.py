"""Read imported packages as evidence. Never import or execute their code."""
from __future__ import annotations

from collections import deque
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import unicodedata
from urllib.parse import unquote, urlsplit
import uuid
import stat
import zipfile


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILES = 2000
CONTEXT_CHARS = 20_000
SNAPSHOT_ID = re.compile(r"^src_[0-9a-f]{32}$")


def safe_relative_path(value: str) -> str:
    """Reject ambiguous paths before filesystem access, including macOS collisions."""
    if not isinstance(value, str) or not value or len(value) > 2000:
        raise ValueError("文件路径为空或过长")
    value = unicodedata.normalize("NFC", value)
    if (value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError(f"不安全的文件路径：{value!r}")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"文件路径不能越界或包含空层级：{value}")
    return value


def decode_text(data: bytes) -> str | None:
    """Keep original bytes; decode an inspection copy only when encoding is known."""
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            return None
        return text
    except UnicodeError:
        return None


def snapshot_directory(project_dir: Path, snapshot_id: str) -> Path:
    if not SNAPSHOT_ID.fullmatch(str(snapshot_id)):
        raise ValueError("无效来源快照编号")
    base = Path(project_dir).resolve() / "sources"
    if base.is_symlink():
        raise ValueError("来源目录不能为符号链接")
    target = base / snapshot_id
    if target.is_symlink():
        raise ValueError("来源快照不能为符号链接")
    return target


def read_source_bytes(project_dir: Path, snapshot_id: str, path: str) -> bytes:
    path = safe_relative_path(path)
    folder = snapshot_directory(project_dir, snapshot_id)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    declared = next((f for f in manifest["files"] if f["path"] == path), None)
    if declared is None:
        raise FileNotFoundError(path)
    root = (folder / "files").resolve()
    candidate = root / path
    if candidate.is_symlink() or root not in candidate.resolve().parents:
        raise ValueError("来源文件越界")
    data = candidate.read_bytes()
    if hashlib.sha256(data).hexdigest() != declared["sha256"]:
        raise ValueError(f"来源快照内容已改变：{path}")
    return data


def _unquote_label(label: str) -> str:
    label = label.strip().strip('"').strip("'")
    return re.sub(r"<br\s*/?>", "\n", label, flags=re.I)


def _mermaid(raw: str, path: str, start: int, end: int) -> dict:
    """Extract the common flowchart subset without treating a graph as executable."""
    graph_id = "graph_" + hashlib.sha256(f"{path}:{start}".encode()).hexdigest()[:16]
    result = {"id": graph_id, "start_line": start, "end_line": end, "source": raw,
              "nodes": [], "edges": [], "groups": [], "warnings": []}
    nodes: dict[str, dict] = {}
    groups: list[str] = []
    edge_re = re.compile(r"\s*(<-->|<\.->|-->|-\.->|==>|---|--|\.->)(?:\|([^|]*)\|)?\s*")

    def endpoint(line: str, at: int, number: int):
        match = re.match(r"\s*([A-Za-z_][\w.-]*)", line[at:])
        if not match:
            return None
        local = match.group(1)
        pos = at + match.end()
        label, shape = local, "implicit"
        while pos < len(line) and line[pos].isspace():
            pos += 1
        if pos < len(line) and line[pos] in "[({":
            opener = line[pos]
            closer = {"[": "]", "(": ")", "{": "}"}[opener]
            begin = pos
            depth, quote, escaped = 0, None, False
            while pos < len(line):
                char = line[pos]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif quote:
                    if char == quote:
                        quote = None
                elif char in "\"'":
                    quote = char
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        break
                pos += 1
            if pos >= len(line):
                result["warnings"].append(f"第 {number} 行节点形状未完整解析")
                return None
            label = _unquote_label(line[begin + 1:pos].strip("[({])}"))
            shape = {"[": "rectangle", "(": "rounded", "{": "decision"}[opener]
            pos += 1
        entry = nodes.setdefault(local, {"id": f"{graph_id}::{local}", "local_id": local,
                                        "label": label, "shape": shape, "line": number,
                                        "parent_id": groups[-1] if groups else None})
        if shape != "implicit":
            entry.update(label=label, shape=shape, line=number)
        return entry, pos

    for offset, original in enumerate(raw.splitlines()):
        number = start + 1 + offset
        line = original.strip()
        if not line or line.startswith(("%%", "flowchart ", "graph ", "direction ")):
            continue
        if line.startswith("subgraph "):
            declaration = line[9:].strip()
            m = re.match(r'([A-Za-z_][\w.-]*)\s*(?:\[(.*)\]|\s+(.*))?$', declaration)
            if m:
                gid = f"{graph_id}::group::{m.group(1)}"
                result["groups"].append({"id": gid, "local_id": m.group(1),
                                         "label": _unquote_label(m.group(2) or m.group(3) or m.group(1)),
                                         "parent_id": groups[-1] if groups else None, "line": number})
                groups.append(gid)
            else:
                result["warnings"].append(f"第 {number} 行分组未解析")
            continue
        if line == "end":
            if groups:
                groups.pop()
            continue
        if line.startswith(("style ", "class ", "classDef ", "linkStyle ", "click ")):
            continue
        current = endpoint(line, 0, number)
        if not current:
            result["warnings"].append(f"第 {number} 行保留原文，未提取结构")
            continue
        source, pos = current
        while pos < len(line):
            m = edge_re.match(line, pos)
            if not m:
                if line[pos:].strip(" ;"):
                    result["warnings"].append(f"第 {number} 行含未提取的 Mermaid 语法")
                break
            target = endpoint(line, m.end(), number)
            if not target:
                result["warnings"].append(f"第 {number} 行连接目标未解析")
                break
            destination, pos = target
            arrow = m.group(1)
            result["edges"].append({"id": f"{graph_id}::edge::{len(result['edges']) + 1}",
                                    "source": source["id"], "target": destination["id"],
                                    "arrow": arrow, "label": m.group(2) or "", "line": number,
                                    "bidirectional": arrow.startswith("<"),
                                    "line_style": "dotted" if "." in arrow else "solid"})
            source = destination
    result["nodes"] = list(nodes.values())
    return result


def _structure(text: str, path: str) -> dict:
    lines = text.splitlines()
    headings, tables, mermaid = [], [], []
    fence = None
    code_start = 0
    table_start = None
    for index, line in enumerate(lines, 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            if fence is None:
                fence = (marker.group(1)[0], len(marker.group(1)), marker.group(2).strip().lower())
                code_start = index
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= fence[1]:
                if fence[2] == "mermaid":
                    mermaid.append(_mermaid("\n".join(lines[code_start:index - 1]), path, code_start, index))
                fence = None
            continue
        if fence:
            continue
        h = re.match(r"^(#{1,6})\s+(.+?)(?:\s+#+\s*)?$", line)
        if h:
            headings.append({"level": len(h.group(1)), "text": h.group(2), "line": index})
        if line.lstrip().startswith("|"):
            if table_start is None:
                table_start = index
        elif table_start is not None:
            block = lines[table_start - 1:index - 1]
            if len(block) > 1 and re.match(r"^\s*\|?\s*:?-{3,}", block[1]):
                tables.append({"start_line": table_start, "end_line": index - 1,
                               "rows": [[v.strip() for v in row.strip().strip("|").split("|")] for row in block],
                               "text": "\n".join(block)})
            table_start = None
    if table_start is not None:
        block = lines[table_start - 1:]
        if len(block) > 1 and re.match(r"^\s*\|?\s*:?-{3,}", block[1]):
            tables.append({"start_line": table_start, "end_line": len(lines),
                           "rows": [[v.strip() for v in row.strip().strip("|").split("|")] for row in block],
                           "text": "\n".join(block)})
    warnings = []
    if fence:
        warnings.append(f"第 {code_start} 行代码块未闭合；原文完整保留")
        if fence[2] == "mermaid":
            mermaid.append(_mermaid("\n".join(lines[code_start:]), path, code_start, len(lines)))
    return {"line_count": len(lines), "headings": headings, "tables": tables,
            "mermaid": mermaid, "warnings": warnings}


_PATH_TOKEN = re.compile(r"(?:\$[A-Za-z_][\w]*(?:/[^\s`<>\"'|,;)]+)+|(?:\.{0,2}/)?[\w\u0080-\uffff@+.-]+(?:/[\w\u0080-\uffff@+.$%:() -]+)*\.(?:md|markdown|txt|py|pyi|js|mjs|cjs|ts|tsx|jsx|json|ya?ml|toml|csv|tsv|png|jpe?g|webp|svg|pdf|docx|xlsx|sh|html|css)(?:#[\w-]+)?)", re.I)


def _join(base: tuple[str, ...], target: str) -> str | None:
    """Join a relative target onto a folder; None when it climbs above the package."""
    parts = list(base)
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return unicodedata.normalize("NFC", "/".join(parts))


def _bases(path: str) -> list[tuple[str, ...]]:
    """The file's own folder first, then each enclosing folder up to the package root.
    Guides often write paths from the workflow root ("命令均从根目录执行")."""
    parts = PurePosixPath(path).parent.parts
    return [tuple(parts[:end]) for end in range(len(parts), -1, -1)]


_FOLDER_TOKEN = re.compile(r"(?:\.{0,2}/)?[\w\u0080-\uffff@+.-]+(?:/[\w\u0080-\uffff@+.-]+)*/(?=[\s`|，。、；：）)\]\"'”]|$)")


def _folder_candidates(line: str) -> list[str]:
    """Folder names a guide points at: `code spans`, table cells, or tokens ending in '/'."""
    found = [m.group(1) for m in re.finditer(r"`([^`\n]+)`", line)]
    if line.lstrip().startswith("|"):
        found.extend(cell.strip() for cell in line.strip().strip("|").split("|"))
    found.extend(m.group(0) for m in _FOLDER_TOKEN.finditer(line))
    return [value.strip().rstrip("/") for value in found if value.strip().rstrip("/")]


def _references(text: str, path: str, available: set[str], folders: frozenset[str] = frozenset()) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple[int, str]] = set()
    suffix = PurePosixPath(path).suffix.lower()
    for number, line in enumerate(text.splitlines(), 1):
        for raw in _folder_candidates(line) if folders else []:
            if (number, raw + "/") in seen or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:|^[/~\\]", raw):
                continue
            folder = next((c for base in _bases(path) if (c := _join(base, raw)) in folders), None)
            if folder is not None:
                seen.add((number, raw + "/"))
                found.append({"from_path": path, "line": number, "target": raw + "/", "origin": "folder",
                              "resolved_path": folder, "fragment": "", "status": "folder"})
        candidates: list[tuple[str, str]] = []
        for m in re.finditer(r"!?\[[^\]]*\]\((<[^>]+>|[^)\s]+)(?:\s+[\"'][^\n]*?[\"'])?\)", line):
            candidates.append((m.group(1).strip("<>"), "markdown"))
        definition = re.match(r"^\s*\[[^\]]+\]:\s*(\S+)", line)
        if definition:
            candidates.append((definition.group(1).strip("<>"), "markdown"))
        for m in _PATH_TOKEN.finditer(line):
            candidates.append((m.group(0).strip(), "code_path" if suffix not in (".md", ".markdown", ".txt") else "mentioned_path"))
        for raw, origin in candidates:
            if (number, raw) in seen:
                continue
            seen.add((number, raw))
            record = {"from_path": path, "line": number, "target": raw, "origin": origin,
                      "resolved_path": None, "fragment": "", "status": "missing"}
            if raw.startswith("#"):
                record.update(status="internal_anchor", resolved_path=path, fragment=raw[1:])
            elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://|^(?:mailto|data|javascript):", raw):
                record["status"] = "external"
            elif "$" in raw or raw.startswith(("/mnt/data/", "/tmp/", "~/", "<")) or "<" in raw:
                record["status"] = "example"
            elif raw.startswith("/") or "\\" in raw or re.match(r"^[A-Za-z]:", raw):
                record["status"] = "outside"
            else:
                parsed = urlsplit(raw)
                target = unquote(parsed.path)
                record["fragment"] = unquote(parsed.fragment)
                if target.startswith("/") or "\\" in target or re.match(r"^[A-Za-z]:", target):
                    record["status"] = "outside"
                    found.append(record)
                    continue
                candidate = _join(PurePosixPath(path).parent.parts, target)
                if candidate is None:
                    record["status"] = "outside"
                else:
                    record["resolved_path"] = candidate
                    rooted = next((c for base in _bases(path)[1:] if (c := _join(base, target)) in available), None)
                    if candidate in available:
                        record["status"] = "resolved"
                    elif rooted is not None:
                        record.update(status="resolved", resolved_path=rooted)
                    elif origin != "markdown" and ("input." in target or "output." in target or
                            re.search(r"(?:example|sample|fixture|示例|例如)", line, re.I)):
                        record["status"] = "example"
            found.append(record)
    return found


def unpack_archive(data: bytes) -> list[tuple[str, bytes]]:
    """Read one ZIP layer into validated bytes; nested archives remain opaque assets."""
    if len(data) > MAX_TOTAL_BYTES:
        raise ValueError("ZIP 上传总量超过 100 MB")
    result = []
    total = 0
    seen = set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > MAX_FILES * 2:
                raise ValueError("ZIP 文件条目过多")
            for info in archive.infolist():
                path = safe_relative_path(info.filename.rstrip("/") if info.is_dir() else info.filename)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ValueError(f"ZIP 不接受链接或特殊文件：{path}")
                if info.is_dir():
                    continue
                if path.casefold() in seen:
                    raise ValueError(f"ZIP 文件路径重复：{path}")
                seen.add(path.casefold())
                total += info.file_size
                if info.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(seen) > MAX_FILES:
                    raise ValueError("ZIP 展开后单文件最多 64 MB、总量 100 MB、最多 2000 文件")
                if info.flag_bits & 1:
                    raise ValueError("请提供未加密的 ZIP")
                with archive.open(info) as stream:
                    contents = stream.read(MAX_FILE_BYTES + 1)
                if len(contents) != info.file_size or len(contents) > MAX_FILE_BYTES:
                    raise ValueError("ZIP 文件大小与清单不一致")
                result.append((path, contents))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError(f"无法读取 ZIP：{exc}") from exc
    if not result:
        raise ValueError("ZIP 中没有文件")
    return result


def _choose_entry(paths: list[str], requested: str | None, texts: dict[str, str]) -> str:
    if requested is not None:
        chosen = safe_relative_path(requested)
        if chosen not in texts:
            raise ValueError("入口文件不存在或不是可解析文本")
        return chosen
    if len(paths) == 1 and paths[0] in texts:
        return paths[0]
    candidates = [p for p in texts if PurePosixPath(p).suffix.lower() in (".md", ".markdown", ".txt")]
    if not candidates:
        candidates = list(texts)
    if not candidates:
        raise ValueError("导入需要至少一份文本入口，二进制文件仍可作为附带资源")
    # An embedded tool's SKILL.md must not eclipse the enclosing workflow guide.
    priority = {"skill.md": 0, "readme.md": 1}
    candidates.sort(key=lambda p: (len(PurePosixPath(p).parts), priority.get(PurePosixPath(p).name.lower(), 2), p.casefold()))
    return candidates[0]


def _folder_documents(folder: str, texts: dict[str, str]) -> list[str]:
    """A folder a guide points at stands for its top-level documents: the Markdown
    files nearest to the folder (the step prompts, the 说明), not every nested resource."""
    prefix = folder + "/"
    docs = [p for p in texts if p.startswith(prefix) and PurePosixPath(p).suffix.lower() in (".md", ".markdown")]
    if not docs:
        return []
    depth = min(p.count("/") for p in docs)
    return sorted((p for p in docs if p.count("/") == depth), key=str.casefold)


def import_snapshot(project_dir: Path, files: list[tuple[str, bytes]], entry: str | None = None) -> dict:
    """Validate complete package, then atomically publish a new immutable snapshot."""
    if not files or len(files) > MAX_FILES:
        raise ValueError(f"每次导入须包含 1–{MAX_FILES} 个文件")
    contents: dict[str, bytes] = {}
    collision_keys: set[str] = set()
    total = 0
    for name, data in files:
        name = safe_relative_path(name)
        key = name.casefold()
        if key in collision_keys:
            raise ValueError(f"重复或大小写冲突的文件路径：{name}")
        if not isinstance(data, bytes):
            raise ValueError("文件内容必须为 bytes")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"单文件超过 64 MB：{name}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("导入总量超过 100 MB")
        collision_keys.add(key)
        contents[name] = data
    # A file cannot also be the parent directory of another file (even by case).
    for name in contents:
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and str(parent).casefold() in collision_keys:
                raise ValueError(f"文件与目录路径冲突：{name}")
    texts = {p: text for p, data in contents.items() if (text := decode_text(data)) is not None}
    selected = _choose_entry(list(contents), entry, texts)
    references = []
    structure = {}
    folders = frozenset(str(parent) for name in contents for parent in PurePosixPath(name).parents if str(parent) != ".")
    for path, text in texts.items():
        structure[path] = _structure(text, path)
        references.extend(_references(text, path, set(contents), folders))
    by_source: dict[str, list[dict]] = {}
    for ref in references:
        by_source.setdefault(ref["from_path"], []).append(ref)
    queue, visited, order = deque([selected]), set(), []
    while queue:
        path = queue.popleft()
        if path in visited:
            continue
        visited.add(path)
        order.append(path)
        for ref in by_source.get(path, []):
            target = ref["resolved_path"]
            if ref["status"] == "resolved" and target in texts and target not in visited:
                queue.append(target)
            elif ref["status"] == "folder" and path == selected:
                # Only the entry guide's folders open up; documents found there follow their
                # own file references, so a whole resource library is not read by default.
                queue.extend(p for p in _folder_documents(target, texts) if p not in visited)
    snapshot_id = "src_" + uuid.uuid4().hex
    descriptions = []
    for path, data in contents.items():
        text = path in texts
        descriptions.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                             "kind": "text" if text else "binary",
                             "status": ("parsed" if path in visited else "unreferenced") if text else "binary",
                             **structure.get(path, {"line_count": 0, "headings": [], "tables": [], "mermaid": [], "warnings": []})})
    coverage = {"total_files": len(contents), "total_bytes": total, "analysis_order": order,
                "parsed_files": order, "unreferenced_files": [p for p in texts if p not in visited],
                "binary_files": [p for p in contents if p not in texts], "truncated_files": [],
                "missing_references": [r for r in references if r["status"] == "missing"],
                "outside_references": [r for r in references if r["status"] in ("outside", "external")],
                "example_references": [r for r in references if r["status"] == "example"],
                "note": "完整文件已保存；语义上下文按入口及包内引用读取，未引用文件和二进制资源未伪称已读。"}
    manifest = {"id": snapshot_id, "entry": selected, "files": descriptions,
                "references": references, "coverage": coverage}
    destination = snapshot_directory(project_dir, snapshot_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / ("." + snapshot_id + ".importing")
    try:
        temporary.mkdir()
        for path, data in contents.items():
            target = temporary / "files" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def source_context(project_dir: Path, snapshot_id: str) -> list[dict]:
    """Return every line of reachable text, in bounded chunks without truncation."""
    directory = snapshot_directory(project_dir, snapshot_id)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    chunks = []
    for path in manifest["coverage"]["analysis_order"]:
        text = decode_text(read_source_bytes(project_dir, snapshot_id, path))
        if text is None:
            raise ValueError(f"已声明文本文件无法解析：{path}")
        lines = text.splitlines(keepends=True)
        if not lines:
            chunks.append({"path": path, "start_line": 1, "end_line": 1, "text": ""})
            continue
        start, current, size = 1, [], 0
        for number, line in enumerate(lines, 1):
            if current and size + len(line) > CONTEXT_CHARS:
                chunks.append({"path": path, "start_line": start, "end_line": number - 1, "text": "".join(current)})
                start, current, size = number, [], 0
            current.append(line)
            size += len(line)
        if current:
            chunks.append({"path": path, "start_line": start, "end_line": len(lines), "text": "".join(current)})
    return chunks
