"""Optional private GitHub backup; never consult local Git/CLI credentials.

Only ``projects/`` on ``cloud-data`` is changed. Tokens live solely in the Git
child process environment, never arguments, repository config, or diagnostics.
"""
from __future__ import annotations

import base64
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

BRANCH = "cloud-data"
_LOCK = threading.Lock()
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_JOB_PROCESS_FILES = {"service.lock", "events.jsonl", "stderr.txt", "__pycache__", ".pytest_cache", ".uv-cache", "node_modules", ".DS_Store"}


class BackupError(RuntimeError):
    pass


def _settings() -> tuple[str, str]:
    repo = os.environ.get("WORKBENCH_BACKUP_REPO", "").strip()
    token = os.environ.get("WORKBENCH_GITHUB_TOKEN", "").strip()
    if not repo or not token:
        raise BackupError("云备份需要配置 WORKBENCH_BACKUP_REPO 和 WORKBENCH_GITHUB_TOKEN。")
    if not _REPO.fullmatch(repo) or repo.endswith(".git") or ".." in repo:
        raise BackupError("备份仓库必须是 GitHub 的 owner/repository，不能是 URL 或路径。")
    if any(char in token for char in "\r\n\0"):
        raise BackupError("云备份凭据格式无效。")
    return repo, token


def configured() -> bool:
    if not os.environ.get("WORKBENCH_BACKUP_REPO") and not os.environ.get("WORKBENCH_GITHUB_TOKEN"):
        return False
    _settings()  # An incomplete/malformed setup must not quietly disable durability.
    return True


def _environment(token: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    values = {
        "http.https://github.com/.extraheader": header,
        "credential.helper": "",
        "core.hooksPath": os.devnull,
        "core.fsmonitor": "false",
        "commit.gpgsign": "false",
        "core.autocrlf": "false",
        "user.name": "Flowdesk cloud backup",
        "user.email": "flowdesk-backup@users.noreply.github.com",
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false",
               GIT_ALLOW_PROTOCOL="https", GIT_CONFIG_COUNT=str(len(values)))
    for index, (key, value) in enumerate(values.items()):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _git(args: list[str], *, env: dict[str, str], cwd: Path | None = None,
         allow_missing: bool = False) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=240, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise BackupError("GitHub 云备份连接失败或超时；已有项目保留。") from None
    if allow_missing and result.returncode == 2:
        return None  # ls-remote --exit-code: no matching ref, not auth/network failure.
    if result.returncode:
        # Git stderr can echo server-controlled text or credentials. Never relay it.
        raise BackupError(f"GitHub 云备份操作失败（{args[0]}）；请检查权限、网络或远端分支冲突。")
    return result.stdout.strip()


def _no_symlinks(path: Path) -> None:
    for ancestor in [path, *path.parents]:
        if ancestor.is_symlink():
            raise BackupError("云备份不接受符号链接路径。")


def _paths(projects_path: Path | str) -> tuple[Path, Path, Path]:
    projects = Path(projects_path).absolute()
    _no_symlinks(projects)
    if projects.name != "projects":
        raise BackupError("云备份仅接受名为 projects 的项目目录。")
    storage = projects.parent / ".cloud-data"
    checkout = storage / "backup-checkout"
    _no_symlinks(checkout)
    _no_symlinks(checkout / ".git")
    storage.mkdir(parents=True, exist_ok=True)
    return projects, checkout, storage / "backup-state.json"


def _snapshot(source: Path, destination: Path) -> None:
    _no_symlinks(source)
    if not source.is_dir():
        raise BackupError("云备份中没有完整的 projects 目录。")
    active_jobs = set()
    jobs = source / ".jobs"
    if jobs.is_symlink():
        raise BackupError("项目备份不能包含符号链接。")
    if jobs.is_dir():
        for job in jobs.iterdir():
            metadata = job / "job.json"
            if job.is_symlink() or metadata.is_symlink():
                raise BackupError("任务备份不能包含符号链接。")
            if job.is_dir() and metadata.is_file():
                try:
                    if json.loads(metadata.read_text(encoding="utf-8")).get("status") in ("queued", "running"):
                        active_jobs.add(job)
                except (OSError, ValueError, AttributeError):
                    raise BackupError("任务记录无法读取；本次备份未覆盖远端。") from None
    for path in source.rglob("*"):
        if path in active_jobs or any(parent in active_jobs for parent in path.parents):
            continue
        if path.is_symlink() or path.name == ".git":
            raise BackupError("项目备份不能包含符号链接或嵌套 Git 目录。")
        if not path.is_file() and not path.is_dir():
            raise BackupError("项目备份包含不支持的文件类型。")
    def ignored(directory, names):
        folder = Path(directory)
        in_jobs = folder == jobs or jobs in folder.parents
        # Only the workbench-owned .jobs tree contains disposable process files.
        # A source snapshot may legitimately contain any of these same names.
        return [name for name in names
                if folder / name in active_jobs or
                (in_jobs and (name in _JOB_PROCESS_FILES or name.endswith(".pyc")))]
    shutil.copytree(source, destination, ignore=ignored)


def _validate_restored(projects: Path) -> None:
    from .importer import read_source_bytes, snapshot_directory
    from .store import Store

    store = Store(projects)
    try:
        for directory in projects.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            project = store.load(directory.name)
            manifests = {}
            for path in directory.glob("sources/*/manifest.json"):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                sid = manifest["id"]
                if snapshot_directory(directory, sid) != path.parent:
                    raise ValueError("来源快照编号不一致")
                for item in manifest["files"]:
                    data = read_source_bytes(directory, sid, item["path"])
                    if len(data) != item["size"]:
                        raise ValueError("来源文件大小不一致")
                manifests[sid] = manifest
            for source in project.get("sources", []):
                actual = manifests[source["id"]]
                contracts = lambda files: sorted((f["path"], f["size"], f["sha256"]) for f in files)
                if contracts(source["files"]) != contracts(actual["files"]):
                    raise ValueError("项目来源清单与快照不一致")
    except (KeyError, ValueError, OSError, TypeError):
        raise BackupError("云备份项目或来源快照校验失败；未恢复目录，也不会回退到旧资料。") from None


def _remote(repo: str, env: dict[str, str]) -> str | None:
    value = _git(["ls-remote", "--exit-code", "--heads", f"https://github.com/{repo}.git",
                  f"refs/heads/{BRANCH}"], env=env, allow_missing=True)
    if value is None:
        return None
    sha = value.split()[0] if value else ""
    if not _SHA.fullmatch(sha):
        raise BackupError("GitHub 返回的备份版本无效。")
    return sha


def _checkout(checkout: Path, repo: str, branch: str, env: dict[str, str]) -> None:
    checkout.mkdir(parents=True, exist_ok=True)
    if not (checkout / ".git").exists():
        _git(["init", "--quiet"], cwd=checkout, env=env)
    # Explicit URL prevents local config from redirecting a remote named origin.
    _git(["fetch", "--depth=1", "--no-tags", f"https://github.com/{repo}.git",
          f"refs/heads/{branch}"], cwd=checkout, env=env)
    _git(["checkout", "--quiet", "--force", "-B", BRANCH, "FETCH_HEAD"], cwd=checkout, env=env)


def _save_state(path: Path, repo: str, sha: str | None) -> None:
    temporary = path.with_suffix(".tmp")
    _no_symlinks(path)
    _no_symlinks(temporary)
    temporary.write_text(json.dumps({"repo": repo, "sha": sha}), encoding="utf-8")
    temporary.replace(path)


def restore(projects_path: Path | str) -> bool:
    """Restore only absent projects. No remote branch means seed may be used."""
    with _LOCK:
        repo, token = _settings()
        projects, checkout, state = _paths(projects_path)
        if projects.exists():
            return False
        env = _environment(token)
        sha = _remote(repo, env)
        if sha is None:
            _save_state(state, repo, None)
            return False
        _checkout(checkout, repo, BRANCH, env)
        # Same branch can change between ls-remote and fetch; record actual fetched version.
        sha = _git(["rev-parse", "HEAD"], cwd=checkout, env=env)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=projects.parent) as temporary:
            restored = Path(temporary) / "projects"
            _snapshot(checkout / "projects", restored)
            _validate_restored(restored)
            if projects.exists():
                raise BackupError("项目目录已出现，恢复操作未覆盖它。")
            restored.replace(projects)
        _save_state(state, repo, sha)
        return True


def backup(projects_path: Path | str, *, snapshot_guard=None) -> dict:
    """Snapshot and fast-forward cloud-data; concurrent remote changes are errors."""
    with _LOCK:
        repo, token = _settings()
        projects, checkout, state = _paths(projects_path)
        env = _environment(token)
        sha = _remote(repo, env)
        _no_symlinks(state)
        previous = json.loads(state.read_text(encoding="utf-8")) if state.exists() else None
        if sha is not None and (not previous or previous.get("repo") != repo or previous.get("sha") != sha):
            raise BackupError("远端云备份已有其他版本；为避免覆盖，请先恢复或人工合并。")
        if sha is None and previous and previous.get("sha"):
            raise BackupError("远端备份分支已消失；已停止写入以保留历史。")
        # Prepare/validate before modifying checkout. Never follow a project symlink.
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=projects.parent) as temporary:
            snapshot = Path(temporary) / "projects"
            # The host can protect only this local copy with store/job locks.
            # Network fetch, commit and push never hold application locks.
            with snapshot_guard() if snapshot_guard is not None else nullcontext():
                _snapshot(projects, snapshot)
            _checkout(checkout, repo, BRANCH if sha else "main", env)
            current = _git(["rev-parse", "HEAD"], cwd=checkout, env=env)
            if sha and current != sha:
                raise BackupError("备份过程中远端版本已改变；本次没有推送。")
            target = checkout / "projects"
            _no_symlinks(target)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(snapshot, target)
            # Snapshot filtering is authoritative; source-repo ignore rules must
            # not silently omit newly imported materials from the data branch.
            _git(["add", "--force", "--all", "--", "projects"], cwd=checkout, env=env)
            changed = bool(_git(["diff", "--cached", "--name-only", "--", "projects"], cwd=checkout, env=env))
            if changed:
                _git(["commit", "--quiet", "-m", "Back up Flowdesk project data", "--", "projects"], cwd=checkout, env=env)
            if changed or sha is None:
                _git(["push", f"https://github.com/{repo}.git", f"HEAD:refs/heads/{BRANCH}"], cwd=checkout, env=env)
            result = _git(["rev-parse", "HEAD"], cwd=checkout, env=env)
            _save_state(state, repo, result)
            return {"status": "saved" if changed or sha is None else "unchanged", "commit": result, "branch": BRANCH}
