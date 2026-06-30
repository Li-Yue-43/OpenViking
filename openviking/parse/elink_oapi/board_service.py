# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Elink Board service -- wraps board/v1/whiteboards API."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import ElinkClient


class ElinkBoardService:
    def __init__(self, client: "ElinkClient"):
        self.client = client
        self.v1 = ElinkBoardV1(client)


class ElinkBoardV1:
    def __init__(self, client: "ElinkClient"):
        self.client = client
        self.whiteboards = ElinkBoardWhiteboards(client)


class ElinkBoardWhiteboards:
    def __init__(self, client: "ElinkClient"):
        self.client = client

    async def get_nodes(self, whiteboard_id: str) -> dict:
        """Get all nodes in a whiteboard."""
        return await self.client.request(
            "GET", f"/board/v1/whiteboards/{whiteboard_id}/nodes"
        )

    async def download_as_image(self, whiteboard_id: str, save_path: Path) -> Path:
        """Download a whiteboard as an image."""
        return await self.client.download_binary(
            f"/board/v1/whiteboards/{whiteboard_id}/download_as_image",
            save_path,
        )


# ===== Structured text extraction (preferred for knowledge retrieval) =====

def board_nodes_to_text(data: dict) -> str:
    """Convert board nodes to structured Markdown text for knowledge retrieval."""
    nodes = data.get("data", {}).get("nodes", []) if isinstance(data, dict) else []
    if not nodes:
        return "[empty board diagram]"

    shapes = []
    connectors = []
    for node in nodes:
        node_type = node.get("type", "")
        if node_type in ("composite_shape", "text_shape"):
            shapes.append(node)
        elif node_type == "connector":
            connectors.append(node)

    if not shapes and not connectors:
        return "[empty board diagram]"

    lines = ["**Board Diagram:**"]

    # Extract nodes with type and text
    if shapes:
        lines.append("")
        lines.append("**Nodes:**")
        id_to_text = {}
        for node in shapes:
            node_id = node.get("id", "")
            text_info = node.get("text", {}) or {}
            text = text_info.get("text", "") if text_info else ""
            id_to_text[node_id] = text

            node_type = node.get("type", "")
            shape_type = ""
            if node_type == "composite_shape":
                shape_type = (node.get("composite_shape") or {}).get("type", "")

            type_label = shape_type or node_type
            display_text = text.replace("\n", " ") if text else "[empty]"
            lines.append(f"- [{type_label}] {display_text}")

    # Extract connections
    if connectors:
        lines.append("")
        lines.append("**Connections:**")
        for conn in connectors:
            conn_data = conn.get("connector", {})
            start_id = conn_data.get("start_object", {}).get("id", "")
            end_id = conn_data.get("end_object", {}).get("id", "")
            start_text = id_to_text.get(start_id, "[unknown]")
            end_text = id_to_text.get(end_id, "[unknown]")
            start_label = start_text.replace("\n", " ")[:40] or "[unknown]"
            end_label = end_text.replace("\n", " ")[:40] or "[unknown]"

            # Check for caption on the connector
            captions = conn_data.get("captions", {}).get("data", [])
            cap_text = ""
            if captions:
                cap_text = captions[0].get("text", "")

            if cap_text:
                lines.append(f"- {start_label} --({cap_text})--> {end_label}")
            else:
                lines.append(f"- {start_label} --> {end_label}")

    return "\n".join(lines)


# ===== Mermaid fallback =====


def board_nodes_to_mermaid(data: dict) -> str:
    """Convert board nodes data to Mermaid flowchart (fallback when image render fails)."""
    nodes = data.get("data", {}).get("nodes", []) if isinstance(data, dict) else []
    if not nodes:
        return "[empty board diagram]"

    id_to_text = {}
    for node in nodes:
        node_id = node.get("id", "")
        node_type = node.get("type", "")
        text = ""
        if node_type in ("composite_shape", "text_shape") and node.get("text"):
            text = node["text"].get("text", "")
        if text and node_id:
            id_to_text[node_id] = text

    mermaid_id_map = {}
    idx = 0
    for node_id in sorted(id_to_text.keys()):
        idx += 1
        mermaid_id_map[node_id] = f"N{idx}"

    connections = []
    for node in nodes:
        if node.get("type") == "connector":
            conn = node.get("connector", {})
            start_id = conn.get("start_object", {}).get("id", "")
            end_id = conn.get("end_object", {}).get("id", "")
            if start_id in mermaid_id_map and end_id in mermaid_id_map:
                connections.append((start_id, end_id))

    lines = ["```mermaid", "flowchart TD"]
    for node_id, m_id in mermaid_id_map.items():
        text = id_to_text[node_id]
        label = text.replace("\n", "<br/>")
        label = label.replace('"', '#quot;')
        if len(label) > 60:
            label = label[:57] + "..."
        lines.append(f'    {m_id}["{label}"]')

    for start_id, end_id in connections:
        lines.append(f"    {mermaid_id_map[start_id]} --> {mermaid_id_map[end_id]}")

    lines.append("```")
    return "\n".join(lines)
