# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Mixin providing shared image-resource persistence for document parsers.

Document-type parsers (Word, PowerPoint, etc.) that extract embedded images
during markdown conversion all need the same post-processing:

1. Copy extracted image bytes into the temp VikingFS directory, under an
   ``images/`` subdirectory next to the Markdown files.
2. Discover the directory where MarkdownParser actually wrote the ``.md``
   files (the tree may be split into subdirectories).
3. Rewrite relative resource references (``./images/xxx.png``) in every
   Markdown file so they stay valid from that file's location.
4. Guess a file extension from an image content type.

These helpers were originally private methods on ``WordParserWithImages``
(``openviking.parse.parsers.word_v2``). They are document-type agnostic and
depend only on ``self._get_viking_fs()`` (provided by ``BaseParser``), so
they are extracted here as a mixin to be shared by any parser that preserves
images (docx, pptx, and future formats) without duplicating code.

A mixin (rather than a free-function utility class) is used because the
helpers need ``self._get_viking_fs()``; with a plain utility every call
site would have to pass the VikingFS instance explicitly.
"""

import re
from typing import List, Tuple

from openviking.parse.resource_path import ResourcePathResolver
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

# Match Markdown image/link references so we can rewrite the referenced
# filename (basename) when a large image is replaced by its preview.
_IMAGE_REF_RE = re.compile(r"(!?\[[^\]]*\]\()([^)]+)(\))")


class ImageResourceMixin:
    """
    Mixin for parsers that persist extracted images into the temp VikingFS
    directory and rewrite Markdown resource references.

    Expects the host class to provide ``self._get_viking_fs()`` (inherited
    from ``BaseParser``).
    """

    async def _discover_md_target_dir(self, temp_uri: str) -> str:
        """Discover the directory where MarkdownParser wrote .md files."""
        viking_fs = self._get_viking_fs()
        try:
            listing = await viking_fs.ls(temp_uri)
            for entry in listing:
                if entry.get("isDir", False):
                    subdir_uri = f"{temp_uri}/{entry['name']}"
                    sub_listing = await viking_fs.ls(subdir_uri)
                    has_md = any(
                        e.get("name", "").endswith(".md") for e in sub_listing
                    )
                    if has_md:
                        return subdir_uri
                elif entry.get("name", "").endswith(".md"):
                    return temp_uri
        except Exception as e:
            logger.warning(f"[ImageResourceMixin] Failed to discover md dir: {e}")
        return temp_uri

    async def _rewrite_md_resource_paths(self, temp_uri: str, resource_vikinguri: str) -> None:
        """Rewrite resource references in all Markdown files under temp_uri."""
        viking_fs = self._get_viking_fs()
        resolver = ResourcePathResolver(resource_vikinguri)

        async def walk(uri: str) -> None:
            try:
                listing = await viking_fs.ls(uri)
            except Exception:
                return
            for entry in listing:
                full_uri = f"{uri}/{entry['name']}"
                if entry.get("isDir", False):
                    await walk(full_uri)
                elif entry.get("name", "").endswith(".md"):
                    try:
                        content = await viking_fs.read_file(full_uri)
                        rewritten = resolver.rewrite(content, uri)
                        if rewritten != content:
                            await viking_fs.write_file(full_uri, rewritten)
                    except Exception as e:
                        logger.warning(
                            f"[ImageResourceMixin] Failed to rewrite {full_uri}: {e}"
                        )

        await walk(temp_uri)

    async def _copy_images_to_vikingfs(
        self, temp_uri: str, extracted_images: List[Tuple[str, bytes]]
    ) -> None:
        """Copy extracted images into the VikingFS temp directory.

        Images are placed under an ``images/`` subdirectory at the same level
        as the Markdown files (``resource_vikinguri``), and Markdown
        references use ``./images/{filename}`` so that MarkdownParser can
        rewrite them when splitting content into subdirectories.

        Oversized images are handled by the shared large-image pipeline so
        document-embedded images behave identically to standalone images
        parsed by ``ImageParser``: large images become preview/grid/tiles and
        their Markdown references are rewritten to point at the preview.
        """
        viking_fs = self._get_viking_fs()

        md_target_dir = await self._discover_md_target_dir(temp_uri)
        images_dir = f"{md_target_dir}/images"
        await viking_fs.mkdir(images_dir, exist_ok=True)

        logger.info(
            f"[ImageResourceMixin] Saving {len(extracted_images)} images to: {images_dir}"
        )

        from openviking.parse.parsers.media.image_resource_writer import (
            write_embedded_images,
        )

        rewrite_map = await write_embedded_images(
            viking_fs,
            images_dir,
            extracted_images,
            config=self._image_config(),
        )
        self._image_reference_rewrite_map = rewrite_map

    def _image_config(self):
        """Resolve the ImageConfig for large-image thresholds.

        Subclasses may provide ``self.config`` as an ImageConfig; otherwise the
        default ImageConfig is returned (which carries the same defaults used
        by ImageParser).
        """
        from openviking_cli.utils.config.parser_config import ImageConfig

        config = getattr(self, "config", None)
        return config if isinstance(config, ImageConfig) else ImageConfig()

    async def _rewrite_image_references(self, temp_uri: str) -> None:
        """Rewrite Markdown image references for images replaced by previews.

        Walks all ``.md`` files under ``temp_uri`` and replaces the referenced
        basename (e.g. ``image_1.png``) with the preview filename produced by
        the large-image pipeline. Only files whose basename appears in the
        rewrite map are touched; the relative path prefix is preserved.
        """
        rewrite_map = getattr(self, "_image_reference_rewrite_map", None) or {}
        if not rewrite_map:
            return

        viking_fs = self._get_viking_fs()

        async def walk(uri: str) -> None:
            try:
                listing = await viking_fs.ls(uri)
            except Exception:
                return
            for entry in listing:
                full_uri = f"{uri}/{entry['name']}"
                if entry.get("isDir", False):
                    await walk(full_uri)
                elif entry.get("name", "").endswith(".md"):
                    try:
                        content = await viking_fs.read_file(full_uri)
                        rewritten = _rewrite_image_filenames(content, rewrite_map)
                        if rewritten != content:
                            await viking_fs.write_file(full_uri, rewritten)
                    except Exception as e:
                        logger.warning(
                            f"[ImageResourceMixin] Failed to rewrite image refs in {full_uri}: {e}"
                        )

        await walk(temp_uri)

    def _guess_extension(self, content_type: str, fallback_index: int) -> str:
        """Guess file extension from content type."""
        type_map = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/gif": "gif",
            "image/bmp": "bmp",
            "image/tiff": "tiff",
            "image/x-emf": "emf",
            "image/x-wmf": "wmf",
            "image/x-tiff": "tiff",
        }
        return type_map.get(content_type, "png")


def _rewrite_image_filenames(content: str, rewrite_map: dict) -> str:
    """Rewrite the basename of Markdown image/link references.

    For each ``![alt](path)`` or ``[text](path)`` reference, if the final path
    segment matches a key in ``rewrite_map``, replace it with the mapped value
    (e.g. ``./images/image_1.png`` -> ``./images/image_1_preview.jpg``). The
    relative prefix is preserved.
    """

    def replacer(match):
        lead = match.group(1)
        ref = match.group(2)
        trail = match.group(3)

        if "://" in ref or ref.startswith("data:"):
            return match.group(0)

        # Split directory prefix and basename.
        if "/" in ref:
            prefix, _, basename = ref.rpartition("/")
        else:
            prefix, basename = "", ref

        if basename in rewrite_map:
            new_basename = rewrite_map[basename]
            new_ref = f"{prefix}/{new_basename}" if prefix else new_basename
            return f"{lead}{new_ref}{trail}"

        return match.group(0)

    return _IMAGE_REF_RE.sub(replacer, content)
