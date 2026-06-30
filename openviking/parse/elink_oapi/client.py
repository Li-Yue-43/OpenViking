# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Lightweight HTTP client for Elink (Honor's private Feishu deployment).

This client wraps the Elink HTTP gateway with custom authentication headers
and reuses lark-oapi SDK models for response deserialization.
"""

from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .board_service import ElinkBoardService
from .docx_service import ElinkDocxService
from .drive_service import ElinkDriveService
from .wiki_service import ElinkWikiService


class ElinkClient:
    """HTTP client for Elink OpenAPI."""

    def __init__(self, base_url: str, headers: Dict[str, str]):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            **headers,
        }
        self.wiki = ElinkWikiService(self)
        self.docx = ElinkDocxService(self)
        self.drive = ElinkDriveService(self)
        self.board = ElinkBoardService(self)

    async def request(
        self, method: str, path: str, params: Optional[dict] = None
    ) -> dict:
        """Send an HTTP request and return JSON response as a dict."""
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.request(
                method,
                url,
                params=params,
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def download_binary(self, path: str, save_path: Path) -> Path:
        """Download binary content and save to file."""
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.request(
                "GET",
                url,
                headers=self.headers,
            )
            resp.raise_for_status()
            save_path.write_bytes(resp.content)
            return save_path
