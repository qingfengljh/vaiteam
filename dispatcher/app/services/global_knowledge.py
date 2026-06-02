import hashlib
import re
from pathlib import Path

from app.core.config import settings

GLOBAL_KNOWLEDGE_ENTRY = "docs/00-GLOBAL_KNOWLEDGE_INDEX.md"
SYSTEM_GLOBAL_KNOWLEDGE_ENTRY = Path(__file__).resolve().parents[3] / "docs" / "00-GLOBAL_KNOWLEDGE_INDEX.md"


def project_repo_dir(project_id: str) -> Path:
    return Path(settings.PROJECTS_DIR) / project_id


def resolve_entry_path(project_id: str) -> Path | None:
    project_entry = project_repo_dir(project_id) / GLOBAL_KNOWLEDGE_ENTRY
    if project_entry.exists():
        return project_entry
    if SYSTEM_GLOBAL_KNOWLEDGE_ENTRY.exists():
        return SYSTEM_GLOBAL_KNOWLEDGE_ENTRY
    return None


def read_entry_text(project_id: str) -> str:
    entry = resolve_entry_path(project_id)
    if not entry:
        return ""
    return entry.read_text(encoding="utf-8")


def calc_version(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def current_version(project_id: str) -> str:
    return calc_version(read_entry_text(project_id))


def to_revision(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def format_revision(revision: int) -> str:
    return f"v{max(0, revision):06d}"


def extract_local_refs(markdown_text: str) -> list[str]:
    refs = []
    for m in re.findall(r"\[[^\]]+\]\(([^)]+)\)", markdown_text):
        ref = m.strip()
        if not ref or ref.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if ref not in refs:
            refs.append(ref)
    return refs
