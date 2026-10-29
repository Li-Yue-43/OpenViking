# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Elink OAPI -- lightweight HTTP client for Elink (Honor's private Feishu deployment)."""

from .client import ELINK_BASE_URL, ELINK_HEADERS, ElinkClient

__all__ = ["ElinkClient", "ELINK_BASE_URL", "ELINK_HEADERS"]
