# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Resource path resolution utilities for OpenViking parsers.

Provides a unified way to manage resource references inside Markdown:
- All resources (images, videos, attachments, etc.) live under a single
  ``resource_dir`` associated with a parsed document.
- Markdown files may be split into subdirectories by the parser.
- Resource references in Markdown are written relative to ``resource_dir``
  (e.g. ``./images/figure.png``).
- When a Markdown file is written to a subdirectory, references are rewritten
  so that they remain valid from that file's location.
"""

import re
from typing import Optional


class ResourcePathResolver:
    """
    Rewrite resource references in Markdown content based on the directory
    depth of the Markdown file relative to the resource root directory/URI.
    """

    # Match Markdown image/link syntax: ![alt](path) or [text](path)
    MD_LINK_RE = re.compile(r"(!?\[([^\]]*)\]\()([^)]+)(\))")

    def __init__(self, resource_root: Optional[str] = None):
        """
        Initialize resolver with the resource root directory/URI.

        Args:
            resource_root: Path/URI of the directory containing all resources.
                           References in Markdown are expected to be relative
                           to this directory.
        """
        self.resource_root = resource_root

    @staticmethod
    def relative_prefix(from_dir: str, to_dir: str) -> str:
        """
        Compute the relative path prefix from ``from_dir`` to ``to_dir``.

        Both arguments should be POSIX-style absolute paths (Linux absolute
        paths or VikingFS URIs), separated by ``/``.

        Examples:
            >>> ResourcePathResolver.relative_prefix("/kb/doc/section", "/kb/doc/images")
            '../images/'
            >>> ResourcePathResolver.relative_prefix("/kb/doc", "/kb/doc/images")
            './images/'
            >>> ResourcePathResolver.relative_prefix("/kb/doc/a/b", "/kb/doc/images")
            '../../images/'
        """
        from_parts = [p for p in from_dir.split("/") if p and not p.startswith("viking:")]
        to_parts = [p for p in to_dir.split("/") if p and not p.startswith("viking:")]

        common = 0
        for a, b in zip(from_parts, to_parts):
            if a == b:
                common += 1
            else:
                break

        up = len(from_parts) - common
        down = to_parts[common:]
        prefix = "../" * up + "/".join(down)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return prefix

    def rewrite(self, content: str, md_dir: str) -> str:
        """
        Rewrite resource references in ``content`` so they are valid from
        ``md_dir``.

        Only references that start with ``./`` or ``/`` are treated as
        relative to ``resource_root`` and rewritten. Other references
        (absolute URLs, protocol URLs like ``feishu://...``) are left
        unchanged.

        Args:
            content: Markdown content.
            md_dir: Directory where the Markdown file will be written.

        Returns:
            Markdown content with rewritten resource references.
        """
        if not self.resource_root or self.resource_root == md_dir:
            return content

        prefix = self.relative_prefix(md_dir, self.resource_root)

        def replacer(match):
            lead = match.group(1)
            ref = match.group(3)
            trail = match.group(4)

            if ref.startswith("./"):
                new_ref = prefix + ref[2:]
            elif ref.startswith("/"):
                new_ref = prefix + ref[1:]
            else:
                return match.group(0)

            return f"{lead}{new_ref}{trail}"

        return self.MD_LINK_RE.sub(replacer, content)
