# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Word document (.docx) parser with image support for OpenViking.

Extends the original WordParser by extracting embedded images from docx
runs, saving them to the VikingFS temp directory alongside the markdown,
so that image content is preserved in the knowledge base.

The image persistence helpers (``_copy_images_to_vikingfs`` /
``_discover_md_target_dir`` / ``_rewrite_md_resource_paths`` /
``_guess_extension``) are shared with the PowerPoint parser via
``ImageResourceMixin`` (openviking.parse.parsers.image_mixin).

Usage: This parser is designed to REPLACE WordParser in the registry.
To revert, simply restore the original registry entry pointing to WordParser.

Original: openviking.parse.parsers.word.WordParser
Enhanced: openviking.parse.parsers.word_v2.WordParserWithImages
"""

from pathlib import Path
from typing import List, Optional, Tuple, Union

from docx.oxml.ns import qn

from openviking.parse.base import ParseResult
from openviking.parse.parsers.image_mixin import ImageResourceMixin
from openviking.parse.parsers.word import WordParser
from openviking_cli.utils.config.parser_config import ParserConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class WordParserWithImages(WordParser, ImageResourceMixin):
    """
    Enhanced Word document parser that preserves embedded images.

    The original WordParser only extracts text and tables, discarding all
    <w:drawing> / <w:pict> image elements. This parser:
    1. Extracts images from paragraph runs during markdown conversion
    2. Collects image bytes with sequential numbering
    3. After MarkdownParser creates the temp VikingFS directory, copies
       all images there so they become part of the knowledge base
    4. Injects ![Image N](filename) markdown references into the output

    Compatible with the existing pipeline: MarkdownParser, TreeBuilder,
    SemanticQueue, and embedding all work unchanged.
    """

    def __init__(self, config: Optional[ParserConfig] = None):
        super().__init__(config=config)

    @property
    def supported_extensions(self) -> List[str]:
        return [".docx"]

    async def parse(self, source: Union[str, Path], instruction: str = "", **kwargs) -> ParseResult:
        """Parse Word document from file path, preserving images."""
        path = Path(source)

        if path.exists():
            import docx

            # Step 1: Convert to markdown while extracting images
            markdown_content, extracted_images = self._convert_to_markdown_with_images(path, docx)

            # Step 2: Parse through MarkdownParser to create temp VikingFS directory
            result = await self._md_parser.parse_content(
                markdown_content,
                source_path=str(path),
                instruction=instruction,
                **kwargs,
            )

            # Step 3: Copy extracted images into the temp VikingFS directory and
            # rewrite resource references in all Markdown files.
            temp_dir_path = result.temp_dir_path
            if temp_dir_path and extracted_images:
                await self._copy_images_to_vikingfs(temp_dir_path, extracted_images)
                md_target_dir = await self._discover_md_target_dir(temp_dir_path)
                resource_vikinguri = md_target_dir
                await self._rewrite_md_resource_paths(temp_dir_path, resource_vikinguri)
                result.resource_vikinguri = resource_vikinguri

            result.source_format = "docx"
            result.parser_name = "WordParserWithImages"
            return result
        else:
            result = await self._md_parser.parse_content(
                str(source), instruction=instruction, **kwargs
            )
            result.source_format = "docx"
            result.parser_name = "WordParserWithImages"
            return result

    def _convert_to_markdown_with_images(self, path: Path, docx) -> Tuple[str, List[Tuple[str, bytes]]]:
        """Convert Word document to Markdown, extracting all embedded images.

        Returns:
            Tuple of (markdown_string, list of (filename, image_bytes))
        """
        doc = docx.Document(path)
        markdown_parts = []
        extracted_images = []  # list of (filename, bytes)
        image_counter = [0]  # use list for mutability in nested function

        # Map XML table elements to python-docx Table objects
        table_by_element = {table._tbl: table for table in doc.tables}

        def _extract_images_from_paragraph(paragraph):
            """Extract all images from a paragraph's runs."""
            nonlocal image_counter
            images_in_paragraph = []

            for run in paragraph.runs:
                # Check for <w:drawing> elements (inline and anchored images)
                drawings = run._r.findall(qn("w:drawing"))
                for drawing in drawings:
                    # Try inline images: <wp:inline>
                    inlines = drawing.findall(".//" + qn("wp:inline"))
                    # Try anchored images: <wp:anchor>
                    anchors = drawing.findall(".//" + qn("wp:anchor"))
                    shapes = drawing.findall(".//" + qn("w:pict"))

                    # Collect all potential image containers
                    all_nodes = inlines + anchors + shapes
                    if not all_nodes:
                        # Fallback: look for any blip element
                        blips = drawing.findall(".//" + qn("a:blip"))
                        for blip in blips:
                            embed_id = blip.get(qn("r:embed"))
                            if embed_id and embed_id in doc.part.rels:
                                image_counter[0] += 1
                                image_part = doc.part.rels[embed_id].target_part
                                ext = self._guess_extension(image_part.content_type, image_counter[0])
                                filename = f"image{image_counter[0]}.{ext}"
                                images_in_paragraph.append((filename, image_part.blob))
                        continue

                    for node in all_nodes:
                        # Find the blip element which contains the image relationship
                        blips = node.findall(".//" + qn("a:blip"))
                        for blip in blips:
                            embed_id = blip.get(qn("r:embed"))
                            if embed_id and embed_id in doc.part.rels:
                                image_counter[0] += 1
                                image_part = doc.part.rels[embed_id].target_part
                                ext = self._guess_extension(image_part.content_type, image_counter[0])
                                filename = f"image{image_counter[0]}.{ext}"
                                images_in_paragraph.append((filename, image_part.blob))

            return images_in_paragraph

        # Walk the document body
        for child in doc.element.body:
            if child.tag == qn("w:p"):
                from docx.text.paragraph import Paragraph

                paragraph = Paragraph(child, doc)
                if not paragraph.text.strip():
                    # Still check for images in empty paragraphs
                    imgs = _extract_images_from_paragraph(paragraph)
                    extracted_images.extend(imgs)
                    if imgs:
                        # Add image refs for images in otherwise empty paragraphs
                        for filename, _ in imgs:
                            markdown_parts.append(f"![{filename}](./images/{filename})")
                    continue

                # Extract images first
                imgs = _extract_images_from_paragraph(paragraph)
                extracted_images.extend(imgs)

                style_name = paragraph.style.name if paragraph.style else "Normal"

                if style_name.startswith("Heading"):
                    level = self._extract_heading_level(style_name)
                    markdown_parts.append(f"{'#' * level} {paragraph.text}")
                else:
                    text = self._convert_formatted_text(paragraph)
                    markdown_parts.append(text)

                # Append image references after text
                for filename, _ in imgs:
                    markdown_parts.append(f"![{filename}](./images/{filename})")

            elif child.tag == qn("w:tbl"):
                if child in table_by_element:
                    markdown_parts.append(self._convert_table(table_by_element[child]))

        markdown_content = "\n\n".join(markdown_parts)
        logger.info(
            f"[WordParserWithImages] Extracted {len(extracted_images)} images "
            f"from {path.name}"
        )
        return markdown_content, extracted_images
