# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Elink Drive service -- mirrors lark-oapi drive.v1.media API."""

from pathlib import Path
from typing import TYPE_CHECKING

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


class ElinkDriveMedia:
    def __init__(self, client: "ElinkClient"):
        self.client = client

    async def download(self, file_token: str, save_path: Path) -> Path:
        """Download a media file by token and save to path."""
        path = f"/drive/v1/medias/{file_token}/download"
        return await self.client.download_binary(path, save_path)
