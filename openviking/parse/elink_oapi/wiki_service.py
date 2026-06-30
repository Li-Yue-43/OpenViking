# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Elink Wiki service -- mirrors lark-oapi wiki.v2.space API."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import ElinkClient


class ElinkWikiService:
    def __init__(self, client: "ElinkClient"):
        self.client = client
        self.v2 = ElinkWikiV2(client)


class ElinkWikiV2:
    def __init__(self, client: "ElinkClient"):
        self.client = client
        self.space = ElinkWikiSpace(client)


class ElinkWikiSpace:
    def __init__(self, client: "ElinkClient"):
        self.client = client

    async def get_node(self, token: str):
        """Resolve a wiki node to its actual document type and token."""
        from lark_oapi.api.wiki.v2.model.get_node_space_response import (
            GetNodeSpaceResponse,
        )

        data = await self.client.request(
            "GET", "/wiki/v2/spaces/get_node", params={"token": token}
        )
        return GetNodeSpaceResponse(data)
