# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
PowerPoint (.pptx) parser with image support for OpenViking.

Extends the original PowerPointParser by extracting embedded images from
slides (top-level pictures, pictures inside grouped shapes, and fallback
``a:blip`` elements), saving them to the VikingFS temp directory alongside
the markdown, so that image content is preserved in the knowledge base.

The image persistence helpers (``_copy_images_to_vikingfs`` /
``_discover_md_target_dir`` / ``_rewrite_md_resource_paths`` /
``_guess_extension``) are shared with the Word parser via
``ImageResourceMixin`` (openviking.parse.parsers.image_mixin), so the pptx
and docx paths use the exact same VikingFS resource layout, ``images/``
subdirectory, and relative-path rewriting — identical to the docx path
(WordParserWithImages).

Usage: This parser is designed to REPLACE PowerPointParser in the registry.
To revert, simply restore the original registry entry pointing to
PowerPointParser.

Original: openviking.parse.parsers.powerpoint.PowerPointParser
Enhanced: openviking.parse.parsers.powerpoint_v2.PowerPointParserWithImages
"""

from pathlib import Path
from typing import List, Optional, Tuple, Union

from openviking.parse.base import ParseResult
from openviking.parse.parsers.image_mixin import ImageResourceMixin
from openviking.parse.parsers.powerpoint import PowerPointParser
from openviking_cli.utils.config.parser_config import ParserConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class PowerPointParserWithImages(PowerPointParser, ImageResourceMixin):
    """
    Enhanced PowerPoint parser that preserves embedded images.

    The original PowerPointParser only extracts text and tables from slide
    shapes, discarding all picture shapes. This parser:
    1. Walks each slide's shape tree (including grouped shapes) and collects
       every ``<p:pic>`` / ``a:blip`` image with sequential numbering
    2. Inserts ``![imageN](./images/imageN.ext)`` references into the
       markdown at the position of the picture
    3. After MarkdownParser creates the temp VikingFS directory, copies all
       images there so they become part of the knowledge base
    4. Rewrites resource references in all Markdown files

    Compatible with the existing pipeline: MarkdownParser, TreeBuilder,
    SemanticQueue, and embedding all work unchanged.
    """

    def __init__(self, config: Optional[ParserConfig] = None, extract_notes: bool = False):
        super().__init__(config=config, extract_notes=extract_notes)

    @property
    def supported_extensions(self) -> List[str]:
        return [".pptx"]

    async def parse(self, source: Union[str, Path], instruction: str = "", **kwargs) -> ParseResult:
        """Parse PowerPoint presentation from file path, preserving images."""
        path = Path(source)

        if path.exists():
            import pptx

            # Step 1: Convert to markdown while extracting images
            markdown_content, extracted_images = self._convert_to_markdown_with_images(path, pptx)

            # Step 2: Parse through MarkdownParser to create temp VikingFS directory
            result = await self._md_parser.parse_content(
                markdown_content,
                source_path=str(path),
                instruction=instruction,
                **kwargs,
            )

            # Step 3: Copy extracted images into the temp VikingFS directory and
            # rewrite resource references in all Markdown files.
            # (inherited from ImageResourceMixin — document-type agnostic)
            temp_dir_path = result.temp_dir_path
            if temp_dir_path and extracted_images:
                await self._copy_images_to_vikingfs(temp_dir_path, extracted_images)
                md_target_dir = await self._discover_md_target_dir(temp_dir_path)
                resource_vikinguri = md_target_dir
                await self._rewrite_md_resource_paths(temp_dir_path, resource_vikinguri)
                result.resource_vikinguri = resource_vikinguri

            result.source_format = "pptx"
            result.parser_name = "PowerPointParserWithImages"
            return result
        else:
            result = await self._md_parser.parse_content(
                str(source), instruction=instruction, **kwargs
            )
            result.source_format = "pptx"
            result.parser_name = "PowerPointParserWithImages"
            return result

    def _convert_to_markdown_with_images(
        self, path: Path, pptx
    ) -> Tuple[str, List[Tuple[str, bytes]]]:
        """Convert PowerPoint presentation to Markdown, extracting all embedded images.

        Returns:
            Tuple of (markdown_string, list of (filename, image_bytes))
        """
        from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

        prs = pptx.Presentation(path)
        markdown_parts = []
        extracted_images = []  # list of (filename, bytes)
        image_counter = [0]  # use list for mutability in nested function
        slide_count = len(prs.slides)

        def _extract_images_from_shape(shape, images_for_shape):
            """Recursively collect images from a shape (incl. grouped shapes)."""
            # Grouped shapes: recurse into children, keeping visual order
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                for sub_shape in shape.shapes:
                    _extract_images_from_shape(sub_shape, images_for_shape)
                return

            # Top-level picture shape
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    image = shape.image
                    image_counter[0] += 1
                    ext = self._guess_extension(image.content_type, image_counter[0])
                    filename = f"image{image_counter[0]}.{ext}"
                    images_for_shape.append((filename, image.blob))
                except Exception as e:
                    logger.warning(
                        f"[PowerPointParserWithImages] Failed to extract image "
                        f"from picture shape: {e}"
                    )
                return

            # Other shapes (incl. placeholders, tables): fallback — find any
            # a:blip element referencing an embedded image relationship
            try:
                from pptx.oxml.ns import qn

                blips = shape._element.findall(".//" + qn("a:blip"))
                for blip in blips:
                    embed_id = blip.get(qn("r:embed"))
                    if embed_id and embed_id in shape.part.rels:
                        rel = shape.part.rels[embed_id]
                        if rel.is_external:
                            continue
                        image_part = rel.target_part
                        image_counter[0] += 1
                        ext = self._guess_extension(
                            image_part.content_type, image_counter[0]
                        )
                        filename = f"image{image_counter[0]}.{ext}"
                        images_for_shape.append((filename, image_part.blob))
            except Exception as e:
                logger.warning(
                    f"[PowerPointParserWithImages] Failed to extract blip image "
                    f"from shape: {e}"
                )

        # Track previous slide titles to detect duplicates (e.g. slides that
        # share the same section title). When a duplicate is found, append a
        # page marker so each section name stays unique.
        title_counts = {}

        # Resolve the original filename. Files uploaded via /temp_upload are
        # renamed to upload_<uuid>.pptx; the original name is stored in a
        # sibling .ov_upload.meta JSON file.
        original_stem = path.stem
        meta_path = path.with_name(path.name + ".ov_upload.meta")
        if meta_path.exists():
            try:
                import json as _json
                original_stem = Path(
                    _json.loads(meta_path.read_text(encoding="utf-8")).get(
                        "original_filename", path.name
                    )
                ).stem or path.stem
            except Exception:
                pass

        # Prefix every slide section with the original filename (truncated) so
        # merged filenames become meaningful, e.g.
        # "exit时报unknown稳定性问题_Slide_113_12more_60e4ea9c.md" instead of
        # the bare "Slide_113_12more_60e4ea9c.md". Use MarkdownParser's own
        # _sanitize_for_path so CJK characters are preserved (its regex
        # explicitly includes CJK Unicode ranges, unlike \w alone).
        stem_bytes = original_stem.encode("utf-8")[:20]
        prefix = self._md_parser._sanitize_for_path(
            stem_bytes.decode("utf-8", errors="ignore")
        )

        for idx, slide in enumerate(prs.slides, 1):
            slide_parts = []

            title = self._extract_slide_title(slide)

            if not title:
                # Fallback: title placeholder not found (some decks use plain
                # TEXT_BOX for titles). Use the topmost text-bearing shape
                # with at least 3 chars as the slide title.
                candidates = [
                    s for s in slide.shapes
                    if s.has_text_frame and s.text_frame.text.strip()
                ]
                if candidates:
                    top = min(candidates, key=lambda s: (s.top, s.left))
                    t = top.text_frame.text.strip()
                    if len(t) >= 3:
                        title = t

            # Use the pptx filename prefix + slide title as the section name,
            # keeping slide title as the primary key so filenames stay close
            # to the original "Slide X/Y" naming convention but with a
            # meaningful prefix.
            section_name = f"{prefix}_{title}" if title else f"{prefix}_Slide_{idx}/{slide_count}"

            if title:
                title_counts[title] = title_counts.get(title, 0) + 1
                if title_counts[title] > 1:
                    section_name = f"{section_name}(第{idx}页)"
                slide_parts.append(f"## {section_name}")
            else:
                slide_parts.append(f"## {section_name}")

            # Walk shapes in z-order; extract images alongside text/tables so
            # the markdown reflects the slide layout
            for shape in slide.shapes:
                images_for_shape = []
                _extract_images_from_shape(shape, images_for_shape)
                extracted_images.extend(images_for_shape)

                # Shape text / table (same logic as the original parser)
                if shape.is_placeholder:
                    ph_type = shape.placeholder_format.type
                    if ph_type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
                        # Image references still appended below if a title
                        # shape also carries a picture
                        for filename, _ in images_for_shape:
                            slide_parts.append(f"![{filename}](./images/{filename})")
                        continue

                if getattr(shape, "has_table", False) and shape.has_table:
                    table_md = self._convert_table(shape.table)
                    if table_md:
                        slide_parts.append(table_md)
                elif hasattr(shape, "text") and shape.text.strip():
                    slide_parts.append(shape.text.strip())

                for filename, _ in images_for_shape:
                    slide_parts.append(f"![{filename}](./images/{filename})")

            if self.extract_notes and slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    slide_parts.append(f"**Notes:** {notes}")

            markdown_parts.append("\n\n".join(slide_parts))

        markdown_content = "\n\n---\n\n".join(markdown_parts)
        logger.info(
            f"[PowerPointParserWithImages] Extracted {len(extracted_images)} images "
            f"from {path.name} ({slide_count} slides)"
        )
        return markdown_content, extracted_images
