# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Elink Drive service -- mirrors lark-oapi drive.v1.media API."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from .client import ElinkClient


class ElinkDriveService:
    def __init__(self, client: "ElinkClient"):
        self.client = client
        self.v1 = ElinkDriveV1(client)


class ElinkDriveV1:
    def __init__(self, client: "ElinkClient"):
        self.client = client
        self.medias = ElinkDriveMedia(client)
        self.metas = ElinkDriveMetas(client)


class ElinkDriveMetas:
    def __init__(self, client: "ElinkClient"):
        self.client = client

    async def batch_query(self, doc_tokens: List[str]) -> List[Dict[str, Any]]:
        """按 doc_token 查询工号责任人和文档元数据，不下载文档内容。"""
        response = await self.client.request(
            "POST",
            "/drive/v1/metas/batch_query",
            params={"user_id_type": "user_id"},
            json={
                "request_docs": [
                    {"doc_token": token, "doc_type": "docx"} for token in doc_tokens
                ],
                "with_url": False,
            },
        )
        if response.get("code") != 0:
            raise RuntimeError(
                f"Elink metadata query failed: code={response.get('code')}, "
                f"msg={response.get('msg')}"
            )
        return (response.get("data") or {}).get("metas") or []


class ElinkDriveMedia:
    def __init__(self, client: "ElinkClient"):
        self.client = client

    async def download(self, file_token: str, save_path: Path) -> Path:
        """Download a media file by token and save to path."""
        path = f"/drive/v1/medias/{file_token}/download"
        return await self.client.download_binary(path, save_path)
