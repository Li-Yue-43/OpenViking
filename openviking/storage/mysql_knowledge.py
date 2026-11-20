# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""MySQL access shared by knowledge-ingestion persistence.

The Camera deployment uses the same MySQL settings as camera-knowledge-api.
Keep SQL for those shared tables here rather than in the semantic processors.
"""

import os
from contextlib import contextmanager
from typing import Iterator, Optional

import pymysql
from pymysql.cursors import DictCursor

from openviking_cli.utils.config import get_openviking_config


@contextmanager
def knowledge_mysql_connection() -> Iterator[pymysql.connections.Connection]:
    """Yield a short-lived connection and commit when the operation succeeds.

    Credentials come from ov.conf ``mysql``; MYSQL_* env vars override them.
    """
    mysql = get_openviking_config().mysql
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST") or mysql.host,
        port=int(os.getenv("MYSQL_PORT") or mysql.port),
        user=os.getenv("MYSQL_USER") or mysql.user,
        password=os.getenv("MYSQL_PASSWORD")
        if os.getenv("MYSQL_PASSWORD") is not None
        else mysql.password,
        database=os.getenv("MYSQL_DATABASE") or mysql.database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=5,
        read_timeout=30,
        write_timeout=30,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def normalize_viking_uri(uri: str) -> str:
    return (uri or "").strip().rstrip("/")


def resource_relative_path(uri: str) -> str:
    """Convert viking://resources/a/b to the file_owners path a/b."""
    normalized = normalize_viking_uri(uri)
    prefix = "viking://resources/"
    if normalized.startswith(prefix):
        return normalized[len(prefix) :].strip("/")
    return ""


def escape_like_prefix(uri: str) -> str:
    """Escape LIKE input; no explicit ESCAPE clause means MySQL's backslash default."""
    return uri.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_elink_file_owner(elink_token: str, obj_type: str) -> Optional[dict]:
    """An exact token/type match means the source document was already parsed."""
    with knowledge_mysql_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """SELECT file_path, owner, elink_token, wiki_node_token, obj_type
               FROM file_owners
               WHERE elink_token = %s AND obj_type = %s
               LIMIT 1""",
            (elink_token, obj_type),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


@contextmanager
def failed_task_write_lock(conn) -> Iterator[None]:
    """Serialize directory dedup/replacement across OpenViking processes.

    No URI hash or long unique index is required. The database named lock makes
    check-delete-insert and subtree moves atomic across all MySQL clients using
    this store.
    """
    with conn.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s, 10) AS acquired", ("camera_failed_tasks_write",))
        acquired = (cursor.fetchone() or {}).get("acquired")
    if acquired != 1:
        raise TimeoutError("Timed out waiting for failed-task database write lock")
    try:
        yield
    finally:
        with conn.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", ("camera_failed_tasks_write",))
