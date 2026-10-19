"""One release migration for known acceptance records, never new cloud work."""
from __future__ import annotations

import json
from pathlib import Path

from scripts import archive_usage as archive

MIGRATION = "20261010-ui-acceptance-v1"
# From the verified local usage archive, deduplicated across its two data roots.
KNOWN_JOB_IDS = frozenset({
    "job_088c2b274250", "job_11fef8966f68", "job_2a51bb193317", "job_32778d244b90",
    "job_45e89e75d7ce", "job_50a324d9009c", "job_57fff31d0bbd", "job_652669f2a2f8",
    "job_65e2da7042be", "job_704be29995f5", "job_756847a1087a", "job_837bfd615f73",
    "job_900fe776423c", "job_a05966cb8403", "job_f5530b2b2400", "job_fd0db874866c",
})


def archive_known_usage(projects: Path) -> Path | None:
    """Archive this release's known records once, before a runtime opens its lease.

    Cloud projects remain authoritative. Completed migration markers make later
    restarts a no-op, including after a user deliberately restores an old record.
    An active service or damaged record aborts before modifying project contents.
    """
    projects = Path(projects).absolute()
    if projects.name != "projects":
        # Custom cloud data paths remain supported; this release migration only
        # applies to the conventional directory used by the approved cleanup.
        return None
    root = projects.parent
    archive.checked_path(root, "projects")
    jobs = archive.checked_path(root, "projects/.jobs")
    jobs.mkdir(parents=True, exist_ok=True)
    with archive.stopped_services(root, ("projects",)):
        marker = archive.checked_path(root, f".state/cloud-usage/{MIGRATION}.json")
        if marker.exists():
            state = json.loads(marker.read_text())
            if state.get("migration") != MIGRATION or state.get("state") != "completed":
                raise ValueError("Invalid cloud usage migration marker")
            return None

        manifest = archive.plan(root, ("projects",))
        active_projects = set()
        for record_path in jobs.glob("*/job.json"):
            record = json.loads(record_path.read_text())
            if record.get("status") not in archive.TERMINAL:
                active_projects.add(record.get("project_id"))

        selected = []
        for operation in manifest["operations"]:
            parts = Path(operation["path"]).parts
            if len(parts) == 3 and parts[1] == ".jobs":
                record = json.loads((root / operation["path"] / "job.json").read_text())
                keep = (parts[2] in KNOWN_JOB_IDS
                        and record.get("id") == parts[2]
                        and record.get("status") in archive.TERMINAL
                        and record.get("project_id") not in active_projects)
            else:
                # plan() only adds this exact fixture or fingerprinted messages;
                # it already excludes projects with active work for both cases.
                keep = (parts == ("projects", archive.FIXTURE_ID)
                        or (len(parts) == 3 and parts[2] == "conversations.jsonl"
                            and operation["kind"] == "rewrite"))
            if keep:
                selected.append(operation)
            else:
                manifest["preserved"].append({"path": operation["path"], "reason": "outside this release's known inactive records"})
        manifest["operations"] = selected
        manifest["migration"] = MIGRATION
        saved = archive.apply_manifest(root, manifest)
        if saved:
            (saved / "RESTORE.md").write_text(
                "# Restore cloud acceptance records\n\n"
                "Stop the Streamlit/workbench service using this data directory first.\n"
                "From the repository Python environment, preview the restoration:\n\n"
                "```python\nfrom pathlib import Path\nfrom scripts.archive_usage import restore\n"
                f"restore(Path({str(root)!r}), {saved.name!r}, apply_changes=False)\n```\n\n"
                "Change `apply_changes` to `True` to restore. Newer files are never overwritten.\n"
                "The completion marker remains, so a restart does not remove restored records.\n"
            )
        marker.parent.mkdir(parents=True, exist_ok=True)
        archive.write_json(marker, {
            "migration": MIGRATION, "state": "completed",
            "archive": saved.relative_to(root).as_posix() if saved else None,
            "operations": len(selected),
        })
        return saved
