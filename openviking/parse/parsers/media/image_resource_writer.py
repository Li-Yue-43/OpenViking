# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Unified image-resource persistence for document-embedded images.

Document parsers (Word/PDF/PowerPoint) and accessors (Elink) all extract
embedded images as bytes and need to persist them into a target directory
(``images/``) so they become part of the knowledge base.

Historically each path wrote the raw bytes verbatim (``viking_fs.write_file``
or ``save_path.write_bytes``), so oversized images (e.g. >4096px) were stored
full-resolution and later failed when fed to a VLM that caps total pixels
(e.g. 8192x8192).

This module provides the single, shared write path. It reuses the exact
large-image pipeline from :mod:`openviking.parse.parsers.media.large_image_processor`
(``process_large_image``) so document-embedded images behave identically to
standalone images parsed by ``ImageParser``:

- Large image (>10MB or any dimension >4096px):
    - ``{stem}_preview.jpg`` (low-res preview, long edge <= preview dimension)
    - ``{stem}_grid.jpg`` (grid overlay with tile labels)
    - ``tiles/{stem}_{rows}_{cols}_{r}_{c}`` (<=2048px each, with overlap)
    - The original bytes are NOT persisted.
- Small image: written verbatim.

The returned mapping tells the caller how to rewrite Markdown references: the
original referenced filename maps to the file that should now be referenced
(the preview filename for large images, or the original filename unchanged for
small images).
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from openviking.parse.parsers.media.large_image_processor import (
    process_large_image,
    save_image_to_bytes,
)
from openviking_cli.utils.config.parser_config import ImageConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class WrittenImage:
    """Result of persisting a single embedded image."""

    original_filename: str
    """The filename referenced by the source document (e.g. ``image_1.png``)."""

    is_large: bool
    """True if the image was split into preview/grid/tiles."""

    reference_filename: Optional[str]
    """Filename to use when rewriting Markdown references.

    For large images this is the preview filename; for small images it is
    ``original_filename`` unchanged.
    """


async def write_embedded_image(
    viking_fs: Any,
    images_dir_uri: str,
    filename: str,
    image_bytes: bytes,
    config: Optional[ImageConfig] = None,
    filename_prefix: Optional[str] = None,
) -> WrittenImage:
    """Persist a single embedded image into ``images_dir_uri``.

    Large images are split into preview/grid/tiles (original not kept); small
    images are written verbatim. ``images_dir_uri`` must already exist (the
    caller owns creating the ``images/`` directory).

    Args:
        viking_fs: VikingFS instance.
        images_dir_uri: VikingFS URI of the target ``images/`` directory.
        filename: Original referenced filename (e.g. ``image_1.png``).
        image_bytes: Raw image bytes.
        config: Optional ``ImageConfig`` for large-image thresholds. If None,
            defaults are used.
        filename_prefix: Optional prefix for preview/grid/tile filenames. If
            None, derived from ``filename`` stem.

    Returns:
        ``WrittenImage`` describing what was persisted and how to rewrite
        references.
    """
    stem = Path(filename).stem
    prefix = filename_prefix or stem

    # Determine large-image handling without a filesystem path. process_large_image
    # needs a Path to read the file size, so use a temporary in-memory file for
    # small images only when needed is avoided: we inspect size/format directly.
    width, height, fmt = _probe_image(image_bytes)
    is_large = _is_large(image_bytes, width, height, config)

    if not is_large:
        file_uri = f"{images_dir_uri}/{filename}"
        await viking_fs.write_file_bytes(file_uri, image_bytes)
        logger.debug(
            "Wrote embedded image (small) filename=%s width=%d height=%d",
            filename,
            width,
            height,
        )
        return WrittenImage(
            original_filename=filename,
            is_large=False,
            reference_filename=filename,
        )

    # Large image: reuse the exact ImageParser pipeline. process_large_image
    # reads the source file path to get file size; write to a temp file to stay
    # consistent with its contract.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix or ".png", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = Path(tmp.name)

    try:
        with Image.open(tmp_path) as img:
            result = process_large_image(
                tmp_path,
                img,
                filename_prefix=prefix,
                config=config,
            )

        if not result.needs_processing:
            # Fallback: probe said large but process_large_image disagreed.
            file_uri = f"{images_dir_uri}/{filename}"
            await viking_fs.write_file_bytes(file_uri, image_bytes)
            return WrittenImage(
                original_filename=filename,
                is_large=False,
                reference_filename=filename,
            )

        # Write preview.
        preview_filename = result.preview_filename or f"{prefix}_preview.jpg"
        await viking_fs.write_file_bytes(
            f"{images_dir_uri}/{preview_filename}", result.preview_bytes
        )

        # Write grid overlay.
        if result.grid_overlay_bytes:
            grid_filename = result.grid_overlay_filename or f"{prefix}_grid.jpg"
            await viking_fs.write_file_bytes(
                f"{images_dir_uri}/{grid_filename}", result.grid_overlay_bytes
            )

        # Write tiles (plus the original large image for traceability).
        tiles_dir_uri = f"{images_dir_uri}/tiles"
        await viking_fs.mkdir(tiles_dir_uri, exist_ok=True)
        # Preserve the original bytes inside tiles/ so the full-resolution image
        # remains available without participating in semantic retrieval.
        await viking_fs.write_file_bytes(f"{tiles_dir_uri}/{filename}", image_bytes)
        for tile in result.tiles or []:
            if tile.bytes_data:
                await viking_fs.write_file_bytes(
                    f"{tiles_dir_uri}/{tile.filename}", tile.bytes_data
                )

        logger.info(
            "Wrote embedded image (large) filename=%s width=%d height=%d preview=%s tiles=%d",
            filename,
            width,
            height,
            preview_filename,
            len(result.tiles or []),
        )
        return WrittenImage(
            original_filename=filename,
            is_large=True,
            reference_filename=preview_filename,
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


async def write_embedded_images(
    viking_fs: Any,
    images_dir_uri: str,
    extracted_images: List[Tuple[str, bytes]],
    config: Optional[ImageConfig] = None,
) -> Dict[str, str]:
    """Persist multiple embedded images and return a reference rewrite map.

    Args:
        viking_fs: VikingFS instance.
        images_dir_uri: VikingFS URI of the target ``images/`` directory.
        extracted_images: List of ``(filename, bytes)``.
        config: Optional ``ImageConfig``.

    Returns:
        Mapping ``{original_filename: reference_filename}``. For large images
        the value is the preview filename; for small images it equals the key.
    """
    rewrite_map: Dict[str, str] = {}
    for filename, image_bytes in extracted_images:
        written = await write_embedded_image(
            viking_fs,
            images_dir_uri,
            filename,
            image_bytes,
            config=config,
        )
        if written.reference_filename:
            rewrite_map[filename] = written.reference_filename
    return rewrite_map


def _probe_image(image_bytes: bytes) -> Tuple[int, int, str]:
    """Return (width, height, format) for image bytes without persisting."""
    with Image.open(BytesIO(image_bytes)) as img:
        return img.size[0], img.size[1], img.format or ""


def _is_large(
    image_bytes: bytes,
    width: int,
    height: int,
    config: Optional[ImageConfig] = None,
) -> bool:
    """Return whether image bytes exceed large-image thresholds."""
    from openviking.utils.media_limits import is_large_image_by_size

    return is_large_image_by_size(
        file_size_bytes=len(image_bytes),
        width=width,
        height=height,
        config=config,
    )


def process_embedded_image_file(
    image_path: Path,
    config: Optional[ImageConfig] = None,
) -> Optional[str]:
    """Process a large image file on the local filesystem in place.

    For accessor-style pipelines (e.g. Elink) that download images to a local
    directory which is later synced into VikingFS by ``DirectoryParser``,
    large images are replaced in place with the preview/grid/tiles layout so
    the final synced resource matches ``ImageParser`` semantics.

    Args:
        image_path: Path to the downloaded image file.
        config: Optional ``ImageConfig`` for large-image thresholds.

    Returns:
        The filename that should now be referenced by Markdown:
        - the preview filename for a large image that was split;
        - ``None`` if the image was small and left unchanged.
    """
    raw = image_path.read_bytes()
    width, height, _fmt = _probe_image(raw)
    if not _is_large(raw, width, height, config):
        return None

    with Image.open(image_path) as img:
        result = process_large_image(
            image_path,
            img,
            filename_prefix=image_path.stem,
            config=config,
        )

    if not result.needs_processing:
        return None

    parent = image_path.parent
    preview_filename = result.preview_filename or f"{image_path.stem}_preview.jpg"

    # Write preview and grid next to the original.
    (parent / preview_filename).write_bytes(result.preview_bytes)
    if result.grid_overlay_bytes:
        grid_filename = result.grid_overlay_filename or f"{image_path.stem}_grid.jpg"
        (parent / grid_filename).write_bytes(result.grid_overlay_bytes)

    # Write tiles under tiles/ (plus the original large image for traceability).
    tiles_dir = parent / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    for tile in result.tiles or []:
        if tile.bytes_data:
            (tiles_dir / tile.filename).write_bytes(tile.bytes_data)

    # Preserve the original bytes inside tiles/ so the full-resolution image
    # remains available without participating in semantic retrieval.
    original_copy = tiles_dir / image_path.name
    original_copy.write_bytes(raw)

    # Remove the full-resolution original from its original location; the
    # preview is the canonical image referenced by Markdown.
    try:
        image_path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Failed to remove original large image %s: %s", image_path, e)

    logger.info(
        "Processed large embedded image (local) path=%s preview=%s tiles=%d",
        image_path.name,
        preview_filename,
        len(result.tiles or []),
    )
    return preview_filename
