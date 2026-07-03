# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Elink Accessor.

Fetches Elink (Honor's private Feishu deployment) cloud documents via HTTP API.
Reuses lark-oapi SDK models for response deserialization so that parsing logic
closely mirrors FeishuAccessor.
"""

import asyncio
import inspect
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

from openviking_cli.utils.logger import get_logger

from ..elink_oapi import ElinkClient
from .base import DataAccessor, LocalResource, SourceType

logger = get_logger(__name__)


@dataclass
class ElinkDocument:
    doc_type: str
    token: str
    markdown_content: str
    title: str
    meta: Dict[str, Any]


class ElinkAccessor(DataAccessor):
    """
    Accessor for Elink (Honor) cloud documents.

    Supports:
    - Documents: https://*.elink.e.hihonor.com/docx/{document_id}
    - Wiki pages: https://*.elink.e.hihonor.com/wiki/{token}

    Uses a lightweight HTTP client (elink_oapi) that sends requests to the
    Elink gateway with custom authentication headers, then deserializes
    responses into lark-oapi SDK objects for parsing.
    """

    PRIORITY = 99

    # Wiki obj_type normalization (API returns short names)
    _WIKI_TYPE_MAP = {"doc": "docx", "sheet": "sheets", "bitable": "base"}

    # Attributes that skip processing (structural containers or metadata)
    _SKIP_ATTRS = {"page", "table_cell", "quote_container", "grid", "grid_column"}

    # Attribute -> special handler method (non-text blocks)
    _SPECIAL_BLOCK_HANDLERS = {
        "divider": "_handle_divider",
        "image": "_handle_image",
        "board": "_handle_board",
        "table": "_table_block_to_markdown",
    }

    # Attribute -> markdown prefix template for text-bearing blocks.
    _TEXT_FORMAT = {
        "bullet": "- {text}",
        "quote": "> {text}",
    }

    # Known block_type integer -> SDK attribute name mapping.
    _BLOCK_TYPE_TO_ATTR = {
        1: "page",
        2: "text",
        3: "heading1",
        4: "heading2",
        5: "heading3",
        6: "heading4",
        7: "heading5",
        8: "heading6",
        9: "heading7",
        10: "heading8",
        11: "heading9",
        12: "bullet",
        13: "ordered",
        14: "code",
        15: "quote",
        17: "todo",
        19: "callout",
        22: "divider",
        27: "image",
        28: "board",
        31: "table",
        32: "table_cell",
        34: "quote_container",
    }

    # All known content attribute names on SDK Block objects (for fallback detection).
    _KNOWN_CONTENT_ATTRS = frozenset(
        {
            "page",
            "text",
            "heading1",
            "heading2",
            "heading3",
            "heading4",
            "heading5",
            "heading6",
            "heading7",
            "heading8",
            "heading9",
            "bullet",
            "ordered",
            "code",
            "quote",
            "todo",
            "callout",
            "divider",
            "image",
            "table",
            "table_cell",
            "quote_container",
            "equation",
            "task",
            "grid",
            "grid_column",
            "board",
        }
    )

    def __init__(self):
        self._client = None

    @property
    def priority(self) -> int:
        return self.PRIORITY

    def can_handle(self, source: Union[str, Path]) -> bool:
        source_str = str(source)
        if not source_str.startswith(("http://", "https://")):
            return False
        return self._is_elink_url(source_str)

    @staticmethod
    def _is_elink_url(url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path
        is_elink_domain = "hihonor.com" in host
        has_doc_path = any(
            path == f"/{t}" or path.startswith(f"/{t}/")
            for t in ("docx", "wiki", "sheets", "base")
        )
        return is_elink_domain and has_doc_path

    async def access(self, source: Union[str, Path], **kwargs) -> LocalResource:
        source_str = str(source)
        progress_callback = kwargs.pop("progress_callback", None)

        async def _report_progress(current: int, total: int, title: str = "") -> None:
            if progress_callback is None:
                return
            try:
                result = progress_callback({"current": current, "total": total, "title": title})
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("[ElinkAccessor] progress_callback failed", exc_info=True)

        try:
            doc_type, token = self._parse_elink_url(source_str)

            skipped_docs: List[Dict[str, Any]] = []
            if doc_type == "wiki":
                # Treat wiki URL as a knowledge-base space: traverse all child docx nodes.
                docs, root_title, skipped_docs = await self._fetch_wiki_space(
                    token, source_str
                )
                meta_doc_type = "wiki"
            else:
                doc = await self._fetch_document(source_str)
                docs = [(doc, [])]
                root_title = doc.title
                meta_doc_type = doc.doc_type

            total_docs = len(docs)
            await _report_progress(0, total_docs, root_title or "")

            # Create directory named after the root title
            safe_root_title = self._sanitize_filename(root_title)
            base_name = (
                f"ov_elink_{safe_root_title}" if safe_root_title else "ov_elink_untitled"
            )
            temp_dir = Path(tempfile.gettempdir()) / base_name

            counter = 1
            while temp_dir.exists():
                temp_dir = Path(tempfile.gettempdir()) / f"{base_name}_{counter}"
                counter += 1

            temp_dir.mkdir(parents=True, exist_ok=True)

            doc_metas: List[Dict[str, Any]] = []
            for idx, (doc, dir_parts) in enumerate(docs, start=1):
                await _report_progress(idx, total_docs, doc.title)

                parent_dir = temp_dir
                for part in dir_parts:
                    parent_dir = parent_dir / part

                # Each document gets its own folder so images stay with the doc
                safe_title = self._sanitize_filename(doc.title) or "document"
                doc_folder = self._unique_path(parent_dir / safe_title)
                doc_folder.mkdir(parents=True, exist_ok=True)

                # Download media (images and boards) and replace references
                images_dir = doc_folder / "images"
                markdown = await self._download_media_and_replace(
                    doc.markdown_content, images_dir
                )

                md_path = doc_folder / f"{doc_folder.name}.md"
                md_path = self._unique_path(md_path)
                md_path.write_text(markdown, encoding="utf-8")

                doc_metas.append(
                    {
                        "title": doc.title,
                        "token": doc.token,
                        "path": str(md_path.relative_to(temp_dir)),
                        **doc.meta,
                    }
                )

            meta = {
                "elink_doc_type": meta_doc_type,
                "elink_token": token,
                "elink_title": root_title,
                "documents": doc_metas,
                "skipped_documents": skipped_docs,
            }

            return LocalResource(
                path=temp_dir,
                source_type=SourceType.FEISHU,
                original_source=source_str,
                meta=meta,
                is_temporary=True,
            )

        except Exception as e:
            logger.error(f"[ElinkAccessor] Failed to access {source}: {e}", exc_info=True)
            raise

    # ========== Document Fetching ==========

    async def _fetch_document(self, url: str) -> ElinkDocument:
        doc_type, token = self._parse_elink_url(url)
        title = None
        meta = {}

        if doc_type == "wiki":
            real_type, real_token, title = await self._resolve_wiki_node(token)
            doc_type, token = real_type, real_token
            meta["wiki_resolved"] = True

        if doc_type != "docx":
            raise ValueError(
                f"Unsupported Elink document type: {doc_type}. "
                f"Only docx is supported in this version."
            )

        markdown, doc_title = await self._parse_docx(token)

        if title:
            doc_title = title

        meta["original_url"] = url

        return ElinkDocument(
            doc_type=doc_type,
            token=token,
            markdown_content=markdown,
            title=doc_title,
            meta=meta,
        )

    async def _download_media_and_replace(self, markdown: str, images_dir: Path) -> str:
        """Download images and boards, replace feishu:// references with local paths."""
        if not markdown:
            return markdown

        client = self._get_client()
        images_dir.mkdir(parents=True, exist_ok=True)

        # --- Download images ---
        image_matches = list(re.finditer(r'!\[(.*?)\]\(feishu://image/([^)]+)\)', markdown))
        image_tokens = {m.group(2) for m in image_matches}
        image_token_to_path: Dict[str, str] = {}
        for i, token in enumerate(image_tokens):
            save_path = images_dir / f"image_{i + 1}.png"
            try:
                await client.drive.v1.medias.download(token, save_path)
                image_token_to_path[token] = f"./images/image_{i + 1}.png"
            except Exception as e:
                logger.warning(f"[ElinkAccessor] Failed to download image {token}: {e}")

        def image_replacer(match):
            alt = match.group(1)
            token = match.group(2)
            path = image_token_to_path.get(token)
            if path:
                return f"![{alt}]({path})"
            return match.group(0)

        markdown = re.sub(r'!\[(.*?)\]\(feishu://image/([^)]+)\)', image_replacer, markdown)

        # --- Download boards as images and save raw node data ---
        board_matches = list(re.finditer(r'!\[(.*?)\]\(feishu://board/([^)]+)\)', markdown))
        board_token_to_paths: Dict[str, Tuple[str, str]] = {}
        for i, m in enumerate(board_matches):
            token = m.group(2)
            image_save_path = images_dir / f"board_{i + 1}.png"
            json_save_path = images_dir / f"board_{i + 1}.json"
            try:
                await client.board.v1.whiteboards.download_as_image(token, image_save_path)
                data = await client.board.v1.whiteboards.get_nodes(token)
                json_save_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                board_token_to_paths[token] = (
                    f"./images/board_{i + 1}.png",
                    f"./images/board_{i + 1}.json",
                )
            except Exception as e:
                logger.warning(f"[ElinkAccessor] Failed to process board {token}: {e}")

        def board_replacer(match):
            alt = match.group(1)
            token = match.group(2)
            paths = board_token_to_paths.get(token)
            if paths:
                image_path, json_path = paths
                return f"![{alt}]({image_path})\n\n[board source data]({json_path})"
            return f"[board diagram: {token}]"

        markdown = re.sub(r'!\[(.*?)\]\(feishu://board/([^)]+)\)', board_replacer, markdown)

        return markdown

    @staticmethod
    def _parse_elink_url(url: str) -> Tuple[str, str]:
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            raise ValueError(f"Cannot parse Elink URL: {url}")
        return path_parts[0], path_parts[1]

    # ========== Client ==========

    def _get_client(self):
        if self._client is None:
            self._client = ElinkClient(
                base_url="https://apigw-beta-new.test.hihonor.com/api/open-apis",
                headers={
                    "elink-appid": "cli_aab672c1eab8d368",
                    "X-RY-ID": "4f0a5538634f4f948ba451987710aa52",
                    "X-RY-APPKEY": "l9PlpgjkmdlKsG8na6NQs5pD",
                },
            )
        return self._client

    @staticmethod
    def _sanitize_filename(title: str) -> str:
        if not title:
            return ""
        safe = re.sub(r'[<>:"/\\|?*]', "_", title)
        safe = re.sub(r'\s+', "_", safe)
        safe = safe[:100]
        safe = safe.strip(" .")
        return safe

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """Return a unique path by appending (_1, _2, ...) if the file already exists."""
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 1
        while True:
            candidate = parent / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    # ========== Wiki Space Traversal ==========

    async def _fetch_wiki_space(
        self, token: str, original_url: str
    ) -> Tuple[List[Tuple[ElinkDocument, List[str]]], str, List[Dict[str, Any]]]:
        """Fetch all docx documents under a wiki node, preserving directory structure.

        Returns:
            (list of (document, relative_dir_parts), root_title, skipped_nodes)
        """
        root_node = await self._get_wiki_node(token)
        root_title = root_node.title or "Untitled"
        docs, skipped = await self._collect_wiki_docs(root_node, original_url)
        return docs, root_title, skipped

    async def _get_wiki_node(self, token: str) -> Any:
        """Get a single wiki node metadata."""
        client = self._get_client()
        response = await client.wiki.v2.space.get_node(token)
        if not response.success():
            raise RuntimeError(
                f"Failed to resolve wiki node {token}: "
                f"code={response.code}, msg={response.msg}"
            )
        return response.data.node

    async def _list_wiki_nodes(
        self, space_id: Union[int, str], parent_node_token: Optional[str] = None
    ) -> List[Any]:
        """List direct child nodes of a wiki space or parent node (with pagination)."""
        client = self._get_client()
        all_nodes: List[Any] = []
        page_token: Optional[str] = None

        while True:
            response = await client.wiki.v2.space.list_nodes(
                space_id=str(space_id),
                parent_node_token=parent_node_token,
                page_size=50,
                page_token=page_token,
            )
            if not response.success():
                raise RuntimeError(
                    f"Failed to list wiki nodes for space {space_id} "
                    f"parent={parent_node_token}: code={response.code}, msg={response.msg}"
                )
            items = response.data.items or []
            all_nodes.extend(items)
            if not response.data.has_more:
                break
            page_token = response.data.page_token

        return all_nodes

    async def _collect_wiki_docs(
        self, root_node: Any, original_url: str
    ) -> Tuple[List[Tuple[ElinkDocument, List[str]]], List[Dict[str, Any]]]:
        """Recursively collect all docx documents under a wiki node."""
        results: List[Tuple[ElinkDocument, List[str]]] = []
        skipped: List[Dict[str, Any]] = []

        async def collect(node: Any, dir_parts: List[str], is_root: bool):
            doc_type = self._WIKI_TYPE_MAP.get(node.obj_type, node.obj_type)

            if doc_type == "docx":
                try:
                    doc = await self._docx_node_to_document(node, original_url)
                    results.append((doc, dir_parts))
                except Exception as e:
                    skipped.append(
                        {
                            "node_token": getattr(node, "node_token", ""),
                            "obj_token": getattr(node, "obj_token", ""),
                            "title": getattr(node, "title", ""),
                            "error": str(e),
                        }
                    )
                    logger.warning(
                        f"[ElinkAccessor] Skip wiki node {getattr(node, 'node_token', '')} "
                        f"(obj_token={getattr(node, 'obj_token', '')}, "
                        f"title={getattr(node, 'title', '')}): {e}",
                        exc_info=True,
                    )
            else:
                logger.debug(
                    f"[ElinkAccessor] Skipping wiki node {node.node_token} "
                    f"with unsupported obj_type={node.obj_type}"
                )

            # Recurse into children if present. Root's children stay at the top level;
            # non-root nodes with children get their own subdirectory.
            if node.has_child:
                try:
                    children = await self._list_wiki_nodes(
                        node.space_id, node.node_token
                    )
                except Exception as e:
                    logger.warning(
                        f"[ElinkAccessor] Failed to list children of wiki node "
                        f"{node.node_token}: {e}",
                        exc_info=True,
                    )
                    return

                child_dir_parts = dir_parts
                if not is_root:
                    child_dir_parts = dir_parts + [
                        self._sanitize_filename(node.title or "untitled")
                    ]
                for child in children:
                    await collect(child, child_dir_parts, is_root=False)

        await collect(root_node, [], is_root=True)
        return results, skipped

    async def _docx_node_to_document(
        self, node: Any, original_url: str
    ) -> ElinkDocument:
        """Fetch a single docx wiki node and wrap it as ElinkDocument."""
        markdown, doc_title = await self._parse_docx(node.obj_token)
        title = node.title or doc_title
        meta = {
            "wiki_node_token": node.node_token,
            "original_url": original_url,
        }
        return ElinkDocument(
            doc_type="docx",
            token=node.obj_token,
            markdown_content=markdown,
            title=title,
            meta=meta,
        )

    # ========== Wiki Resolution ==========

    async def _resolve_wiki_node(self, token: str) -> Tuple[str, str, Optional[str]]:
        client = self._get_client()
        response = await client.wiki.v2.space.get_node(token)
        if not response.success():
            raise RuntimeError(
                f"Failed to resolve wiki node {token}: "
                f"code={response.code}, msg={response.msg}"
            )
        node = response.data.node
        obj_type = node.obj_type or ""
        obj_token = node.obj_token or ""
        title = node.title

        doc_type = self._WIKI_TYPE_MAP.get(obj_type, obj_type)
        return doc_type, obj_token, title

    # ========== Docx Parsing ==========

    async def _parse_docx(self, document_id: str) -> Tuple[str, str]:
        blocks = await self._fetch_all_blocks(document_id)
        if not blocks:
            return "", "Untitled"

        block_map = {b.block_id: b for b in blocks}

        doc_title = "Untitled"
        for b in blocks:
            if b.page is not None:
                if b.page.elements:
                    doc_title = self._extract_text_from_elements(b.page.elements)
                break

        markdown_lines = []
        ordered_counter: Dict[str, int] = {}

        for block in blocks:
            if block.page is not None:
                continue

            line = self._block_to_markdown(
                block, block_map, ordered_counter, document_id=document_id
            )
            if line is not None:
                markdown_lines.append(line)

        markdown = "\n\n".join(markdown_lines)

        if doc_title and doc_title != "Untitled":
            markdown = f"# {doc_title}\n\n{markdown}"

        return markdown, doc_title

    async def _fetch_all_blocks(self, document_id: str) -> list:
        client = self._get_client()
        all_blocks = []
        page_token = None

        while True:
            response = await client.docx.v1.document_block.list(
                document_id=document_id,
                page_size=500,
                page_token=page_token,
                document_revision_id=-1,
            )

            if not response.success():
                raise RuntimeError(
                    f"Failed to fetch blocks for {document_id}: "
                    f"code={response.code}, msg={response.msg}"
                )

            items = response.data.items or []
            all_blocks.extend(items)

            if not response.data.has_more:
                break
            page_token = response.data.page_token

        logger.info(f"[ElinkAccessor] Fetched {len(all_blocks)} blocks for {document_id}")
        return all_blocks

    # ========== Block -> Markdown Conversion ==========

    def _detect_block_attr(self, block) -> Optional[str]:
        block_type = getattr(block, "block_type", None)
        if block_type is not None:
            attr = self._BLOCK_TYPE_TO_ATTR.get(block_type)
            if attr:
                return attr

        for attr in self._KNOWN_CONTENT_ATTRS:
            if getattr(block, attr, None) is not None:
                return attr
        return None

    def _block_to_markdown(
        self, block, block_map: Dict, ordered_counter: Dict[str, int], document_id: str = ""
    ) -> Optional[str]:
        attr = self._detect_block_attr(block)

        if attr is None:
            return None

        if attr in self._SKIP_ATTRS:
            return None

        if attr != "ordered":
            parent_id = block.parent_id or ""
            if parent_id in ordered_counter:
                del ordered_counter[parent_id]

        special_handler = self._SPECIAL_BLOCK_HANDLERS.get(attr)
        if special_handler:
            return getattr(self, special_handler)(block, block_map, document_id=document_id)

        content_obj = getattr(block, attr, None)
        if not content_obj or not hasattr(content_obj, "elements") or not content_obj.elements:
            return None

        text = self._extract_text_from_elements(content_obj.elements)
        if not text:
            return None

        if attr.startswith("heading"):
            level = int(attr.replace("heading", "") or "1")
            return f"{'#' * level} {text}"

        if attr == "ordered":
            parent_id = block.parent_id or ""
            counter = ordered_counter.get(parent_id, 0) + 1
            ordered_counter[parent_id] = counter
            return f"{counter}. {text}"

        if attr == "code":
            lang = ""
            if hasattr(content_obj, "style") and content_obj.style:
                lang = str(getattr(content_obj.style, "language", "") or "")
            return f"```{lang}\n{text}\n```"

        if attr == "todo":
            done = False
            if hasattr(content_obj, "style") and content_obj.style:
                done = getattr(content_obj.style, "done", False)
            checkbox = "[x]" if done else "[ ]"
            return f"- {checkbox} {text}"

        fmt = self._TEXT_FORMAT.get(attr)
        if fmt:
            return fmt.format(text=text)

        return text

    @staticmethod
    def _handle_divider(block, block_map: Dict = None, **_) -> str:
        return "---"

    @staticmethod
    def _handle_image(block, block_map: Dict = None, **_) -> Optional[str]:
        image = block.image
        if not image:
            return None
        file_token = image.token or ""
        alt_text = getattr(image, "alt", "") or "image"
        return f"![{alt_text}](feishu://image/{file_token})"

    def _handle_board(self, block, block_map: Dict = None, **_) -> Optional[str]:
        board = block.board
        if not board:
            return None
        token = board.token or ""
        return f"![board diagram](feishu://board/{token})"

    def _extract_block_text(self, block, attr_name: str) -> str:
        content_obj = getattr(block, attr_name, None)
        if content_obj and hasattr(content_obj, "elements") and content_obj.elements:
            return self._extract_text_from_elements(content_obj.elements)
        return ""

    def _extract_text_from_elements(self, elements) -> str:
        if not elements:
            return ""
        parts = []
        for element in elements:
            text_run = element.text_run
            if text_run:
                content = text_run.content or ""
                style = text_run.text_element_style
                content = self._apply_text_style(content, style)
                parts.append(content)
                continue

            mention_user = element.mention_user
            if mention_user:
                user_id = getattr(mention_user, "user_id", "user")
                parts.append(f"@{user_id}")
                continue

            mention_doc = element.mention_doc
            if mention_doc:
                title = getattr(mention_doc, "title", "document")
                url = getattr(mention_doc, "url", "")
                parts.append(f"[{title}]({url})" if url else str(title))
                continue

            equation = element.equation
            if equation:
                parts.append(f"${getattr(equation, 'content', '')}$")
                continue

        return "".join(parts)

    @staticmethod
    def _apply_text_style(text: str, style) -> str:
        if not text or not style:
            return text
        if getattr(style, "inline_code", False):
            text = f"`{text}`"
        link = getattr(style, "link", None)
        if link:
            url = getattr(link, "url", "")
            if url:
                text = f"[{text}]({url})"
        if getattr(style, "bold", False):
            text = f"**{text}**"
        if getattr(style, "italic", False):
            text = f"*{text}*"
        if getattr(style, "strikethrough", False):
            text = f"~~{text}~~"
        return text

    def _table_block_to_markdown(self, block, block_map: Dict, **_) -> Optional[str]:
        table = block.table
        children = block.children
        if not table or not children:
            return None

        prop = table.property
        if not prop:
            return None
        row_size = prop.row_size or 0
        col_size = prop.column_size or 0
        if not row_size or not col_size:
            return None

        rows = []
        for row_idx in range(row_size):
            row = []
            for col_idx in range(col_size):
                cell_idx = row_idx * col_size + col_idx
                if cell_idx < len(children):
                    cell_block_id = children[cell_idx]
                    cell_block = block_map.get(cell_block_id)
                    cell_text = self._extract_cell_text(cell_block, block_map)
                    row.append(cell_text)
                else:
                    row.append("")
            rows.append(row)

        from openviking.parse.base import format_table_to_markdown

        return format_table_to_markdown(rows, has_header=True) if rows else None

    def _extract_cell_text(self, cell_block, block_map: Dict) -> str:
        if not cell_block or not cell_block.children:
            return ""
        texts = []
        for child_id in cell_block.children:
            child = block_map.get(child_id)
            if not child:
                continue
            attr = self._detect_block_attr(child)
            if attr:
                text = self._extract_block_text(child, attr)
                if text:
                    texts.append(text)
        return " ".join(texts)
