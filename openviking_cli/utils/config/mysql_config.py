# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Connection settings for the Camera knowledge MySQL database."""

from pydantic import BaseModel, Field


class MySQLConfig(BaseModel):
    """Connection settings for the Camera knowledge MySQL database.

    Configured directly in ov.conf, independently from camera-knowledge-api.
    """

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=3306)
    user: str = Field(default="root")
    password: str = Field(default="")
    database: str = Field(default="camera_knowledge")
