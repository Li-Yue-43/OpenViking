# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Elink Wiki service -- mirrors lark-oapi wiki.v2.space API."""

from typing import Any, Dict, Optional, TYPE_CHECKING

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

    async def list_nodes(
        self,
        space_id: str,
        parent_node_token: Optional[str] = None,
        page_size: int = 50,
        page_token: Optional[str] = None,
    ):
        """List child nodes of a wiki space (optionally filtered by parent)."""
        from lark_oapi.api.wiki.v2.model.list_space_node_response import (
            ListSpaceNodeResponse,
        )

        params: Dict[str, Any] = {"page_size": page_size}
        if parent_node_token:
            params["parent_node_token"] = parent_node_token
        if page_token:
            params["page_token"] = page_token

        data = await self.client.request(
            "GET",
            f"/wiki/v2/spaces/{space_id}/nodes",
            params=params,
        )
        return ListSpaceNodeResponse(data)
