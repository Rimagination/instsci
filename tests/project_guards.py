from pathlib import Path


IGNORED_PROJECT_SCAN_DIRS = {
    ".git",
    ".tmp",
    ".venv",
    ".pytest_cache",
    "_browsers",
    "build",
    "dist",
    "downloads",
    "runs",
    "__pycache__",
}

PROJECT_TEXT_SUFFIXES = {".py", ".md", ".toml", ".json", ".yml", ".yaml", ".txt"}


def find_project_reference_offenders(
    root: Path,
    names: tuple[str, ...],
    *,
    include_paths: bool = False,
) -> list[str]:
    normalized_names = tuple(name.lower() for name in names)
    offenders: list[str] = []

    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in IGNORED_PROJECT_SCAN_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if include_paths and any(name in str(rel).lower() for name in normalized_names):
            offenders.append(str(rel))
            continue
        if not path.is_file() or path.suffix.lower() not in PROJECT_TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if any(name in text for name in normalized_names):
            offenders.append(str(rel))

    return offenders
