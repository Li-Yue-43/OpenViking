# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Lightweight HTTP client for Elink (Honor's private Feishu deployment).

This client wraps the Elink HTTP gateway with custom authentication headers
and reuses lark-oapi SDK models for response deserialization.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .board_service import ElinkBoardService
from .docx_service import ElinkDocxService
from .drive_service import ElinkDriveService
from .wiki_service import ElinkWikiService

logger = logging.getLogger(__name__)


class ElinkClient:
    """HTTP client for Elink OpenAPI."""

    # 网关不稳定时重试次数（指数退避）
    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 1.0

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
        last_exc: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES):
            async with httpx.AsyncClient(timeout=120) as client:
                try:
                    resp = await client.request(
                        method,
                        url,
                        params=params,
                        headers=self.headers,
                    )
                    resp.raise_for_status()
                    return resp.json()
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    last_exc = e
                    status_code = getattr(e, "response", None) and getattr(
                        e.response, "status_code", None
                    )
                    should_retry = (
                        attempt < self.MAX_RETRIES - 1
                        and status_code in (None, 500, 502, 503, 504)
                    )
                    if should_retry:
                        delay = self.RETRY_DELAY_BASE * (2 ** attempt)
                        logger.warning(
                            f"[ElinkClient] {method} {url} failed "
                            f"(attempt {attempt + 1}/{self.MAX_RETRIES}, "
                            f"status={status_code}): {e}. Retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise

        # 防御：理论上不会到达这里，因为最后一次重试失败会 raise
        raise last_exc or RuntimeError(f"Failed to request {url}")

    async def download_binary(self, path: str, save_path: Path) -> Path:
        """Download binary content and save to file."""
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES):
            async with httpx.AsyncClient(timeout=120) as client:
                try:
                    resp = await client.request(
                        "GET",
                        url,
                        headers=self.headers,
                    )
                    resp.raise_for_status()
                    save_path.write_bytes(resp.content)
                    return save_path
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    last_exc = e
                    status_code = getattr(e, "response", None) and getattr(
                        e.response, "status_code", None
                    )
                    should_retry = (
                        attempt < self.MAX_RETRIES - 1
                        and status_code in (None, 500, 502, 503, 504)
                    )
                    if should_retry:
                        delay = self.RETRY_DELAY_BASE * (2 ** attempt)
                        logger.warning(
                            f"[ElinkClient] GET {url} failed "
                            f"(attempt {attempt + 1}/{self.MAX_RETRIES}, "
                            f"status={status_code}): {e}. Retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise

        raise last_exc or RuntimeError(f"Failed to download {url}")
