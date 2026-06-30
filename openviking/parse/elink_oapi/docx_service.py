# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Elink Docx service -- mirrors lark-oapi docx.v1 API."""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .client import ElinkClient


class ElinkDocxService:
    def __init__(self, client: "ElinkClient"):
        self.client = client
        self.v1 = ElinkDocxV1(client)


class ElinkDocxV1:
    def __init__(self, client: "ElinkClient"):
        self.client = client
        self.document = ElinkDocument(client)
        self.document_block = ElinkDocumentBlock(client)


class ElinkDocument:
    def __init__(self, client: "ElinkClient"):
        self.client = client

    async def raw_content(
        self,
        document_id: str,
    ):
        """Get raw text content of a document."""
        data = await self.client.request(
            "GET",
            f"/docx/v1/documents/{document_id}/raw_content",
        )
        return data


class ElinkDocumentBlock:
    def __init__(self, client: "ElinkClient"):
        self.client = client

    async def list(
        self,
        document_id: str,
        page_size: int = 500,
        page_token: Optional[str] = None,
        document_revision_id: int = -1,
    ):
        """List all blocks in a document with pagination."""
        from lark_oapi.api.docx.v1.model.list_document_block_response import (
            ListDocumentBlockResponse,
        )

        params = {
            "page_size": page_size,
            "document_revision_id": document_revision_id,
        }
        if page_token:
            params["page_token"] = page_token

        data = await self.client.request(
            "GET",
            f"/docx/v1/documents/{document_id}/blocks",
            params=params,
        )
        return ListDocumentBlockResponse(data)
