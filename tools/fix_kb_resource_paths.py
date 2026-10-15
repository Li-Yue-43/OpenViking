#!/usr/bin/env python3
# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
One-off script to fix resource references in an already-ingested knowledge base.

The script walks a local directory tree (a Linux absolute path), finds all
Markdown files, detects the nearest ``images/`` directory as the resource root,
and rewrites ``./images/...`` references so they are valid from each Markdown
file's actual location.

Usage:
    python tools/fix_kb_resource_paths.py /path/to/knowledge/base
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

from openviking.parse.resource_path import ResourcePathResolver


def find_resource_dir(md_path: Path) -> Optional[Path]:
    """
    Find the nearest ancestor directory that contains an ``images/`` subdirectory.

    Args:
        md_path: Path to a Markdown file.

    Returns:
        The resource root directory, or ``None`` if no ``images/`` dir is found.
    """
    for parent in [md_path.parent, *md_path.parents]:
        images_dir = parent / "images"
        if images_dir.exists() and images_dir.is_dir():
            return parent
    return None


def fix_md_file(md_path: Path, resource_dir: Path) -> bool:
    """
    Rewrite resource references in a single Markdown file.

    Args:
        md_path: Path to the Markdown file.
        resource_dir: Resource root directory for this document.

    Returns:
        ``True`` if the file was modified, ``False`` otherwise.
    """
    content = md_path.read_text(encoding="utf-8")
    resolver = ResourcePathResolver(str(resource_dir))
    fixed = resolver.rewrite(content, str(md_path.parent))
    if fixed != content:
        md_path.write_text(fixed, encoding="utf-8")
        return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Fix resource paths in knowledge base Markdown files"
    )
    parser.add_argument("root", help="Linux absolute path to knowledge base root")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists() or not root.is_dir():
        print(f"Error: {root} is not a valid directory", file=sys.stderr)
        sys.exit(1)

    fixed_count = 0
    skipped_no_resource_dir: list[str] = []

    for md_path in root.rglob("*.md"):
        resource_dir = find_resource_dir(md_path)
        if resource_dir is None:
            skipped_no_resource_dir.append(str(md_path))
            continue
        if fix_md_file(md_path, resource_dir):
            fixed_count += 1
            print(f"Fixed: {md_path}")

    print(f"\nFixed {fixed_count} files")
    if skipped_no_resource_dir:
        print(
            f"Skipped {len(skipped_no_resource_dir)} files (no images dir found):"
        )
        for p in skipped_no_resource_dir[:10]:
            print(f"  - {p}")


if __name__ == "__main__":
    main()
