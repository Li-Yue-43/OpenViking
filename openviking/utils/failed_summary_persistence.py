# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Persist failed summary requests in the shared knowledge MySQL database."""

from typing import Any, Dict, Optional

from openviking.storage.mysql_knowledge import (
    escape_like_prefix,
    failed_task_write_lock,
    knowledge_mysql_connection,
    normalize_viking_uri,
)
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

_SUMMARY_FAILED = "SUMMARY_FAILED"


def persist_failed_summary_for_directory(
    dir_uri: str,
    error: str,
    need_recursive: bool = False,
) -> None:
    """Insert a directory failure using the existing parent/child rules.

    Only SUMMARY_FAILED rows participate. There is no UPSERT: a non-recursive
    child replaces its non-recursive ancestors, while an existing descendant
    suppresses its parent. Retry removes the old row before a fresh failure.
    """
    normalized_dir_uri = normalize_viking_uri(dir_uri)
    try:
        with knowledge_mysql_connection() as conn:
            with failed_task_write_lock(conn), conn.cursor() as cursor:
                if not need_recursive:
                    cursor.execute(
                        """SELECT id, dir_uri, need_recursive
                           FROM knowledge_ingestion_failures
                           WHERE failure_type = %s""",
                        (_SUMMARY_FAILED,),
                    )
                    existing = cursor.fetchall()
                    prefix = normalized_dir_uri + "/"
                    if any(
                        row["dir_uri"] == normalized_dir_uri
                        or (row["dir_uri"] or "").startswith(prefix)
                        for row in existing
                    ):
                        return
                    ancestor_ids = [
                        row["id"]
                        for row in existing
                        if not row["need_recursive"]
                        and normalized_dir_uri.startswith((row["dir_uri"] or "") + "/")
                    ]
                    if ancestor_ids:
                        placeholders = ", ".join(["%s"] * len(ancestor_ids))
                        cursor.execute(
                            f"""DELETE FROM knowledge_ingestion_failures
                                WHERE failure_type = %s AND id IN ({placeholders})""",
                            (_SUMMARY_FAILED, *ancestor_ids),
                        )
                else:
                    cursor.execute(
                        """DELETE FROM knowledge_ingestion_failures
                           WHERE failure_type = %s AND dir_uri = %s""",
                        (_SUMMARY_FAILED, normalized_dir_uri),
                    )
                cursor.execute(
                    """INSERT INTO knowledge_ingestion_failures
                       (failure_type, stage, dir_uri, need_recursive, error)
                       VALUES (%s, 'SUMMARIZE', %s, %s, %s)""",
                    (_SUMMARY_FAILED, normalized_dir_uri, need_recursive, error),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to persist: {e}")


def get_failed_summary_record(dir_uri: str) -> Optional[Dict[str, Any]]:
    try:
        with knowledge_mysql_connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """SELECT id, failure_type, stage, dir_uri, parent_dir_uri,
                          need_recursive, elink_token, wiki_node_token, obj_type,
                          document_url, document_title, owner, error, created_at
                   FROM knowledge_ingestion_failures
                   WHERE failure_type = %s AND dir_uri = %s
                   ORDER BY id LIMIT 1""",
                (_SUMMARY_FAILED, normalize_viking_uri(dir_uri)),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to read: {e}")
        return None


def delete_failed_summary_record(dir_uri: str, need_recursive: Optional[bool] = None) -> bool:
    sql = """DELETE FROM knowledge_ingestion_failures
             WHERE failure_type = %s AND dir_uri = %s"""
    params: list = [_SUMMARY_FAILED, normalize_viking_uri(dir_uri)]
    if need_recursive is not None:
        sql += " AND need_recursive = %s"
        params.append(need_recursive)
    try:
        with knowledge_mysql_connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to delete: {e}")
        return False


def delete_failed_summary_under(dir_uri: str) -> int:
    normalized_dir_uri = normalize_viking_uri(dir_uri)
    try:
        with knowledge_mysql_connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """DELETE FROM knowledge_ingestion_failures
                   WHERE failure_type = %s
                     AND (dir_uri = %s OR dir_uri LIKE %s)""",
                (_SUMMARY_FAILED, normalized_dir_uri, escape_like_prefix(normalized_dir_uri) + "/%"),
            )
            deleted = cursor.rowcount
            conn.commit()
            return deleted
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to delete subtree: {e}")
        return 0


def move_failed_summary_record(from_dir_uri: str, to_dir_uri: str) -> bool:
    """Move all summary failures in the subtree in one transaction."""
    source = normalize_viking_uri(from_dir_uri)
    target = normalize_viking_uri(to_dir_uri)
    if source == target:
        return False
    try:
        with knowledge_mysql_connection() as conn:
            with failed_task_write_lock(conn), conn.cursor() as cursor:
                # Temporary prefix prevents an ancestor move from matching rows twice.
                temp_prefix = f"__moving__/{target}"
                cursor.execute(
                    """UPDATE knowledge_ingestion_failures
                       SET dir_uri = CONCAT(%s, SUBSTRING(dir_uri, %s))
                       WHERE failure_type = %s
                         AND (dir_uri = %s OR dir_uri LIKE %s)""",
                    (
                        temp_prefix,
                        len(source) + 1,
                        _SUMMARY_FAILED,
                        source,
                        escape_like_prefix(source) + "/%",
                    ),
                )
                moved = cursor.rowcount
                cursor.execute(
                    """UPDATE knowledge_ingestion_failures
                       SET dir_uri = SUBSTRING(dir_uri, %s)
                       WHERE failure_type = %s AND dir_uri LIKE %s""",
                    (len("__moving__/") + 1, _SUMMARY_FAILED, escape_like_prefix(temp_prefix) + "%"),
                )
            conn.commit()
            return moved > 0
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to move: {e}")
        return False


# Backwards-compatible wrapper: record a file failure against its parent directory.
def persist_failed_summary_request(
    file_uri: str,
    original_filename: str,
    prompt: str,
    error: str,
) -> None:
    try:
        from openviking_cli.utils.uri import VikingURI

        parent = VikingURI(file_uri).parent
        dir_uri = parent.uri if parent is not None else file_uri
    except Exception:
        stripped = file_uri.rstrip("/")
        last_slash = stripped.rfind("/")
        dir_uri = stripped[:last_slash] if last_slash > -1 else file_uri
    persist_failed_summary_for_directory(dir_uri=dir_uri, error=error)
