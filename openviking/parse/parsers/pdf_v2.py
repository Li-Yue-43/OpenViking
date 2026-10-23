# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
PDF parser with image support for OpenViking.

Extends the original PDFParser by extracting embedded images during
pdfplumber conversion and saving them to the VikingFS temp directory
alongside the markdown, so that image content is preserved in the
knowledge base and can be summarised by SemanticQueue via VLM.

The image persistence helpers (``_copy_images_to_vikingfs`` /
``_discover_md_target_dir`` / ``_rewrite_md_resource_paths`` /
``_guess_extension``) are shared with the Word and PowerPoint parsers via
``ImageResourceMixin`` (openviking.parse.parsers.image_mixin).

Usage: This parser is designed to REPLACE PDFParser in the registry.
To revert, simply restore the original registry entry pointing to PDFParser.

Original: openviking.parse.parsers.pdf.PDFParser
Enhanced: openviking.parse.parsers.pdf_v2.PDFParserWithImages
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from openviking.parse.base import ParseResult, lazy_import
from openviking.parse.parsers.image_mixin import ImageResourceMixin
from openviking.parse.parsers.pdf import PDFParser
from openviking_cli.utils.config.parser_config import PDFConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class PDFParserWithImages(PDFParser, ImageResourceMixin):
    """
    Enhanced PDF parser that preserves embedded images.

    The original PDFParser extracts images via pdfplumber but saves them
    to a local ``.openviking/media/`` directory on disk (outside VikingFS),
    making them invisible to SemanticQueue.  This parser instead:

    1. Collects image bytes as ``(filename, bytes)`` tuples during
       pdfplumber conversion, inserting ``./images/`` references into the
       intermediate markdown.
    2. After MarkdownParser creates the temp VikingFS directory, copies all
       images into ``images/`` via ``_copy_images_to_vikingfs``.
    3. Rewrites relative resource references in every Markdown file via
       ``_rewrite_md_resource_paths``.

    Compatible with the existing pipeline: MarkdownParser, TreeBuilder,
    SemanticQueue, and embedding all work unchanged.
    """

    def __init__(self, config: Optional[PDFConfig] = None):
        super().__init__(config=config)

    @property
    def supported_extensions(self) -> List[str]:
        return [".pdf"]

    async def parse(
        self, source: Union[str, Path], instruction: str = "", **kwargs
    ) -> ParseResult:
        """
        Parse PDF file, preserving embedded images in VikingFS.

        Args:
            source: Path to PDF file
            **kwargs: Additional options (resource_name/source_name)

        Returns:
            ParseResult with document tree
        """
        start_time = __import__("time").time()
        pdf_path = Path(source)
        resource_name = kwargs.get("resource_name") or kwargs.get("source_name")

        if not pdf_path.exists():
            return await super().parse(source, instruction=instruction, **kwargs)

        try:
            # Step 1: Convert PDF to Markdown while extracting images
            markdown_content, extracted_images, conversion_meta = (
                await self._convert_to_markdown_with_images(
                    pdf_path, resource_name=resource_name
                )
            )

            # Step 2: Parse through MarkdownParser to create temp VikingFS directory
            md_parser = self._get_markdown_parser()
            result = await md_parser.parse_content(
                markdown_content,
                source_path=str(pdf_path),
                resource_name=resource_name,
                source_name=resource_name,
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

            # Step 4: Update metadata (same as original PDFParser)
            result.source_format = "pdf"
            result.parser_name = "PDFParserWithImages"
            result.parser_version = "2.0"
            result.parse_time = __import__("time").time() - start_time
            result.meta.update(conversion_meta)
            result.meta["pdf_strategy"] = self.config.strategy
            result.meta["intermediate_markdown_length"] = len(markdown_content)
            result.meta["intermediate_markdown_preview"] = markdown_content[:500]

            logger.info(
                f"[PDFParserWithImages] Parsed {pdf_path.name}: "
                f"{len(markdown_content)} chars markdown, "
                f"{len(extracted_images)} images preserved, "
                f"{result.parse_time:.2f}s"
            )

            return result

        except Exception as e:
            logger.error(f"[PDFParserWithImages] Failed to parse {pdf_path}: {e}")
            # Fallback to original PDFParser behaviour on any error
            return await super().parse(source, instruction=instruction, **kwargs)

    async def _convert_to_markdown_with_images(
        self,
        pdf_path: Path,
        resource_name: Optional[str] = None,
    ) -> Tuple[str, List[Tuple[str, bytes]], Dict[str, Any]]:
        """
        Convert PDF to Markdown, extracting all embedded images.

        Unlike the original ``_convert_local_sync`` which saves images to
        ``.openviking/media/`` on local disk, this method collects image
        bytes in-memory and returns them together with the markdown so
        they can later be copied into VikingFS.

        Args:
            pdf_path: Path to PDF file
            resource_name: Optional resource name for metadata

        Returns:
            Tuple of (markdown_content, extracted_images, meta_dict)
            where extracted_images is a list of (filename, bytes).
        """
        return await asyncio.to_thread(
            self._convert_to_markdown_with_images_sync, pdf_path, resource_name
        )

    def _convert_to_markdown_with_images_sync(
        self,
        pdf_path: Path,
        resource_name: Optional[str] = None,
    ) -> Tuple[str, List[Tuple[str, bytes]], Dict[str, Any]]:
        """Synchronous implementation of _convert_to_markdown_with_images."""
        pdfplumber = lazy_import("pdfplumber")

        if resource_name is None:
            resource_name = pdf_path.stem

        parts: List[str] = []
        extracted_images: List[Tuple[str, bytes]] = []
        image_counter = [0]  # use list for mutability in nested function

        meta: Dict[str, Any] = {
            "strategy": "local",
            "library": "pdfplumber",
            "pages_processed": 0,
            "images_extracted": 0,
            "tables_extracted": 0,
            "bookmarks_found": 0,
            "bookmarks_resolved": 0,
            "bookmarks_unresolved": 0,
            "headings_found": 0,
            "heading_source": "none",
        }

        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                meta["total_pages"] = len(pdf.pages)

                # ---- Heading detection (same logic as original) ----
                detection_mode = self.config.heading_detection
                bookmarks = []
                raw_bookmarks = []
                heading_source = "none"

                if detection_mode in ("bookmarks", "auto"):
                    raw_bookmarks = self._extract_bookmarks(pdf)
                    meta["bookmarks_found"] = len(raw_bookmarks)
                    bookmarks = [bm for bm in raw_bookmarks if bm["page_num"] is not None]
                    meta["bookmarks_resolved"] = len(bookmarks)
                    meta["bookmarks_unresolved"] = len(raw_bookmarks) - len(bookmarks)
                    if bookmarks:
                        heading_source = "bookmarks"
                    elif raw_bookmarks:
                        logger.info(
                            "Bookmark detection found %d entries but none resolved "
                            "to pages; ignoring bookmark headings",
                            len(raw_bookmarks),
                        )

                if not bookmarks and detection_mode in ("font", "auto"):
                    bookmarks = self._detect_headings_by_font(pdf)
                    if bookmarks:
                        heading_source = "font_analysis"

                meta["headings_found"] = len(bookmarks)
                meta["heading_source"] = heading_source
                logger.info(
                    "Heading detection: source=%s, headings=%d, bookmarks=%d, "
                    "resolved=%d, unresolved=%d",
                    heading_source,
                    len(bookmarks),
                    meta["bookmarks_found"],
                    meta["bookmarks_resolved"],
                    meta["bookmarks_unresolved"],
                )

                # Group bookmarks by page_num
                from collections import defaultdict

                bookmarks_by_page = defaultdict(list)
                for bm in bookmarks:
                    page = bm["page_num"]
                    if page is not None:
                        bookmarks_by_page[page].append(bm)

                # ---- Per-page extraction ----
                for page_num, page in enumerate(pdf.pages, 1):
                    try:
                        # Inject headings before page text
                        page_bookmarks = bookmarks_by_page.get(page_num, [])
                        for bm in page_bookmarks:
                            heading_prefix = "#" * bm["level"]
                            parts.append(f"\n{heading_prefix} {bm['title']}\n")

                        # Extract text
                        text = page.extract_text()
                        if text and text.strip():
                            parts.append(f"<!-- Page {page_num} -->\n{text.strip()}")
                            meta["pages_processed"] += 1

                        # Extract tables
                        tables = page.extract_tables()
                        for table_idx, table in enumerate(tables or []):
                            if table and len(table) > 0:
                                md_table = self._format_table_markdown(table)
                                if md_table:
                                    parts.append(
                                        f"<!-- Page {page_num} Table {table_idx + 1} -->\n"
                                        f"{md_table}"
                                    )
                                    meta["tables_extracted"] += 1

                        # Extract images — skip decorative/watermark images
                        images = page.images
                        for img_idx, img in enumerate(images or []):
                            try:
                                # Skip decorative/watermark images that are
                                # not part of the document content.  PDFs
                                # converted from Word/WPS mark header/footer
                                # and watermark images as Artifact; real
                                # content images carry a paragraph tag (P)
                                # and an mcid.  This is the most reliable
                                # encoding-level distinction.
                                tag = img.get("tag")
                                if tag == "Artifact":
                                    logger.debug(
                                        f"[PDFParserWithImages] Skipping "
                                        f"decorative image {img.get('name')} "
                                        f"on page {page_num} (tag=Artifact)"
                                    )
                                    meta["images_skipped"] = (
                                        meta.get("images_skipped", 0) + 1
                                    )
                                    continue

                                # Second filter: images whose SMask renders
                                # them almost fully transparent.  Word-saved
                                # PDFs embed decorative shapes as images with
                                # an SMask that sets alpha near zero for all
                                # pixels.  Real content images either have no
                                # SMask or have meaningful alpha values.
                                if self._is_nearly_transparent(img):
                                    logger.debug(
                                        f"[PDFParserWithImages] Skipping "
                                        f"nearly-transparent image "
                                        f"{img.get('name')} on page {page_num}"
                                    )
                                    meta["images_skipped"] = (
                                        meta.get("images_skipped", 0) + 1
                                    )
                                    continue

                                image_bytes = self._extract_image_bytes(page, img)
                                if image_bytes:
                                    image_counter[0] += 1
                                    ext = self._guess_image_extension(img, image_counter[0])
                                    filename = f"image{image_counter[0]}.{ext}"
                                    extracted_images.append((filename, image_bytes))
                                    parts.append(
                                        f"<!-- Page {page_num} Image {img_idx + 1} -->\n"
                                        f"![{filename}](./images/{filename})"
                                    )
                                    meta["images_extracted"] += 1
                                else:
                                    logger.debug(
                                        f"[PDFParserWithImages] Could not extract "
                                        f"image {img_idx + 1} on page {page_num}"
                                    )
                            except Exception as img_err:
                                logger.warning(
                                    f"[PDFParserWithImages] Failed to extract image "
                                    f"{img_idx + 1} on page {page_num}: {img_err}"
                                )
                    finally:
                        self._release_page_cache(page)

            if not parts:
                logger.warning(f"No content extracted from {pdf_path}")
                return "", extracted_images, meta

            markdown_content = "\n\n".join(parts)
            logger.info(
                f"[PDFParserWithImages] Local conversion: "
                f"{meta['pages_processed']}/{meta['total_pages']} pages, "
                f"{meta['headings_found']} headings ({meta['heading_source']}), "
                f"{meta['images_extracted']} images, "
                f"{meta['tables_extracted']} tables → "
                f"{len(markdown_content)} chars"
            )

            return markdown_content, extracted_images, meta

        except Exception as e:
            logger.error(f"[PDFParserWithImages] pdfplumber conversion failed: {e}")
            raise

    def _is_nearly_transparent(self, img_info: dict) -> bool:
        """
        Check if an image's SMask renders it partially or fully transparent.

        Word-saved PDFs embed decorative shapes (colored bars, borders,
        watermarks) as images with an SMask that hides most or all pixels.
        Real content images are fully opaque (no SMask, or SMask with
        alpha = 255 everywhere).

        Rule: SMask present AND visible_ratio < 1.0  →  skip.
        This is a deliberate one-size-fits-all filter:
        - Decorative images:  visible_ratio ≈ 0.0
        - Icons w/ shadow:    visible_ratio ≈ 0.13  (also not useful for KB)
        - Real content photos/screenshots:  no SMask, or visible_ratio = 1.0

        Args:
            img_info: Image metadata dict from page.images

        Returns:
            True if the image has any transparency (not fully opaque)
        """
        try:
            stream = img_info.get("stream")
            if stream is None:
                return False

            attrs = stream.attrs if hasattr(stream, "attrs") else {}
            smask_ref = attrs.get("SMask")
            if smask_ref is None:
                return False

            smask_stream = (
                smask_ref.resolve() if hasattr(smask_ref, "resolve") else smask_ref
            )
            smask_raw = smask_stream.get_data()
            smask_attrs = getattr(smask_stream, "attrs", {})
            sw = int(smask_attrs.get("Width", 0))
            sh = int(smask_attrs.get("Height", 0))

            if not sw or not sh or len(smask_raw) < sw * sh:
                return False

            # Count pixels that are NOT fully opaque
            opaque_count = 0
            for i in range(min(len(smask_raw), sw * sh)):
                if smask_raw[i] == 255:
                    opaque_count += 1

            visible_ratio = opaque_count / (sw * sh)
            return visible_ratio < 1.0

        except Exception as e:
            logger.debug(f"[PDFParserWithImages] Transparency check error: {e}")
            return False

    def _extract_image_bytes(self, page, img_info: dict) -> Optional[bytes]:
        """
        Extract image bytes from a PDF page and convert to a valid image file.

        PDF stores images as raw pixel streams (DeviceRGB/DeviceCMYK/etc.)
        without any file-format encapsulation.  The original
        ``_extract_image_from_page`` returns these raw bytes which are NOT
        valid image files.  This method reconstructs a proper image using
        PIL/Pillow.

        Handles:
        - DeviceRGB (24-bit RGB)          → PNG
        - DeviceGray (8-bit grayscale)    → PNG
        - DeviceCMYK (32-bit CMYK)        → PNG (converted to RGB)
        - DCTDecode (JPEG encoded)        → JPEG passthrough
        - SMask (soft mask / alpha)       → merged as PNG with transparency

        Args:
            page: pdfplumber page object
            img_info: Image metadata dict from page.images

        Returns:
            Valid image file bytes (PNG or JPEG) or None on failure
        """
        try:
            # Access the raw stream directly from img_info — the stream dict
            # contains all metadata needed for decoding.
            stream = img_info.get("stream")
            if stream is None:
                return None

            raw = stream.get_data()
            if not raw:
                return None

            from PIL import Image
            import io

            attrs = stream.attrs if hasattr(stream, "attrs") else {}
            filter_name = str(attrs.get("Filter", "")).upper()
            colorspace = str(attrs.get("ColorSpace", ""))
            bits = attrs.get("BitsPerComponent", 8)

            # --- JPEG passthrough ---
            if "DCT" in filter_name or "JPEG" in filter_name:
                return raw

            # --- JPX (JPEG 2000) passthrough ---
            if "JPX" in filter_name:
                return raw

            # --- Raw pixel formats: use srcsize for real pixel dimensions ---
            w, h = img_info.get("srcsize", (0, 0))
            if not w or not h:
                logger.debug(
                    f"[PDFParserWithImages] No srcsize for image, skipping"
                )
                return None

            w, h = int(w), int(h)

            # Determine PIL mode from colorspace
            if "DeviceRGB" in colorspace or "CalRGB" in colorspace:
                mode = "RGB"
                expected = w * h * 3
            elif "DeviceGray" in colorspace or "CalGray" in colorspace:
                mode = "L"
                expected = w * h
            elif "DeviceCMYK" in colorspace or "CalCMYK" in colorspace:
                mode = "CMYK"
                expected = w * h * 4
            else:
                logger.debug(
                    f"[PDFParserWithImages] Unsupported colorspace: {colorspace}"
                )
                return None

            if len(raw) < expected:
                logger.debug(
                    f"[PDFParserWithImages] Raw data too short: "
                    f"{len(raw)} < {expected} for {w}x{h} {mode}"
                )
                return None

            # Build image from raw bytes
            pil_img = Image.frombytes(mode, (w, h), raw[:expected])

            # CMYK → RGB for display compatibility
            if mode == "CMYK":
                pil_img = pil_img.convert("RGB")

            # Merge SMask (alpha channel) if present
            smask_ref = attrs.get("SMask")
            if smask_ref:
                try:
                    smask_stream = smask_ref.resolve() if hasattr(smask_ref, "resolve") else smask_ref
                    smask_raw = smask_stream.get_data()
                    smask_attrs = getattr(smask_stream, "attrs", {})
                    smask_w = int(smask_attrs.get("Width", w))
                    smask_h = int(smask_attrs.get("Height", h))

                    if len(smask_raw) >= smask_w * smask_h:
                        alpha = Image.frombytes("L", (smask_w, smask_h), smask_raw[: smask_w * smask_h])
                        if alpha.size != pil_img.size:
                            alpha = alpha.resize(pil_img.size)
                        if pil_img.mode != "RGBA":
                            pil_img = pil_img.convert("RGBA")
                        pil_img.putalpha(alpha)
                except Exception as smask_err:
                    logger.debug(
                        f"[PDFParserWithImages] SMask merge failed: {smask_err}"
                    )

            # Save as PNG
            buf = io.BytesIO()
            if pil_img.mode == "CMYK":
                pil_img = pil_img.convert("RGB")
            pil_img.save(buf, format="PNG")
            return buf.getvalue()

        except Exception as e:
            logger.debug(f"[PDFParserWithImages] Image extraction error: {e}")
            return None

    def _guess_image_extension(self, img_info: dict, index: int) -> str:
        """
        Guess file extension from PDF image metadata.

        Falls back to the shared ``_guess_extension`` from ImageResourceMixin
        when metadata is insufficient.

        Args:
            img_info: Image metadata dict from page.images
            index: Image index for fallback

        Returns:
            File extension string without leading dot (e.g. "png", "jpg")
        """
        # Try to get extension from the PDF image metadata
        for key in ("ext", "extension", "format"):
            val = img_info.get(key)
            if val:
                val_str = str(val).lower().lstrip(".")
                if val_str in ("png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp"):
                    return "jpg" if val_str == "jpeg" else val_str

        # Try to infer from colorspace / filter
        colorspace = img_info.get("colorspace", "")
        filter_name = str(img_info.get("filter", "")).upper()

        if "JPX" in filter_name:
            return "jp2"
        if "DCT" in filter_name or "JPEG" in filter_name:
            return "jpg"
        if "CCITT" in filter_name:
            return "tiff"

        # Default to PNG (safest — our _extract_image_bytes converts to PNG)
        return "png"

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, instruction: str = "", **kwargs
    ) -> ParseResult:
        """PDFParserWithImages requires a file path; delegate to parent."""
        raise NotImplementedError(
            "PDFParserWithImages does not support parsing content strings. "
            "Use parse() with a file path instead."
        )
