"""Stable private curation input identities."""

from pathlib import Path


def corpus_location(path: Path) -> tuple[Path, str]:
    for root in path.parents:
        relative = path.relative_to(root)
        if relative.parts[0] == "content" and (root / "content").is_dir():
            return root, relative.as_posix()
    raise ValueError("API intermediate lacks a corpus article identity")
