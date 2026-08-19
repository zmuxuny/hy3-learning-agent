#!/usr/bin/env python3
"""Validate local file targets in repository Markdown documents."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


IGNORED_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "dist",
    "htmlcov",
    "node_modules",
}
LINK_RE = re.compile(r"!?\[[^\]\n]*\]\((?P<target>[^)\n]+)\)")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


@dataclass(frozen=True)
class BrokenLink:
    source: Path
    line: int
    target: str
    reason: str


def markdown_files(root: Path) -> list[Path]:
    git_marker = root / ".git"
    if git_marker.exists():
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return [
            root / relative_path.decode("utf-8")
            for relative_path in result.stdout.split(b"\0")
            if relative_path
        ]
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part in IGNORED_PARTS for part in path.relative_to(root).parts)
    )


def _target_path(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing < 0:
            return target
        target = target[1:closing]
    else:
        # Markdown permits an optional quoted title after the destination.
        target = target.split(maxsplit=1)[0]

    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return unquote(parsed.path)


def check_markdown_links(root: Path) -> list[BrokenLink]:
    root = root.resolve()
    broken: list[BrokenLink] = []
    for source in markdown_files(root):
        in_fence = False
        for line_number, original_line in enumerate(
            source.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if FENCE_RE.match(original_line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            line = INLINE_CODE_RE.sub("", original_line)
            for match in LINK_RE.finditer(line):
                raw_target = match.group("target")
                relative_target = _target_path(raw_target)
                if relative_target is None:
                    continue
                candidate = (source.parent / relative_target).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    broken.append(
                        BrokenLink(source, line_number, raw_target, "escapes repository")
                    )
                    continue
                if not candidate.exists():
                    broken.append(
                        BrokenLink(source, line_number, raw_target, "target does not exist")
                    )
    return broken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of scripts/)",
    )
    args = parser.parse_args()
    broken = check_markdown_links(args.root)
    if not broken:
        print("Markdown relative links: OK")
        return 0
    for issue in broken:
        source = issue.source.relative_to(args.root.resolve())
        print(f"{source}:{issue.line}: {issue.reason}: {issue.target}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
