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

from typing import List, Tuple

from openviking.parse.resource_path import ResourcePathResolver
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


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
        """
        viking_fs = self._get_viking_fs()

        md_target_dir = await self._discover_md_target_dir(temp_uri)
        images_dir = f"{md_target_dir}/images"
        await viking_fs.mkdir(images_dir, exist_ok=True)

        logger.info(
            f"[ImageResourceMixin] Saving {len(extracted_images)} images to: {images_dir}"
        )

        for filename, image_bytes in extracted_images:
            try:
                file_uri = f"{images_dir}/{filename}"
                await viking_fs.write_file(file_uri, image_bytes)
                logger.info(
                    f"[ImageResourceMixin] Saved image to VikingFS: {file_uri}"
                )
            except Exception as e:
                logger.warning(
                    f"[ImageResourceMixin] Failed to save image {filename}: {e}"
                )

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
