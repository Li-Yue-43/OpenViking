# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared utilities for persisting failed summary requests to disk."""

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

_RESOURCES_PREFIX = "viking://resources/"
_SCHEME_PREFIX = "viking://"
_FAILED_JSON_FILENAME = "failed_summaries.json"


def get_failed_summary_log_dir() -> Path:
    """Return the configured directory for persisting failed summary requests."""
    try:
        config = get_openviking_config()
        return Path(config.semantic.failed_summary_log_dir)
    except Exception:
        return Path("/data/openviking_log/failed_summaries")


def _get_failed_json_path() -> Path:
    """Return the path to the centralized failed summaries JSON file."""
    base_dir = get_failed_summary_log_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / _FAILED_JSON_FILENAME


def _acquire_lock(fd, exclusive=True):
    """Acquire file lock."""
    if exclusive:
        fcntl.flock(fd, fcntl.LOCK_EX)
    else:
        fcntl.flock(fd, fcntl.LOCK_SH)


def _release_lock(fd):
    """Release file lock."""
    fcntl.flock(fd, fcntl.LOCK_UN)


def _is_descendant_or_same(parent_uri: str, child_uri: str) -> bool:
    """Return True if ``child_uri`` is ``parent_uri`` or one of its descendants."""
    parent = parent_uri.rstrip("/")
    child = child_uri.rstrip("/")
    if child == parent:
        return True
    prefix = parent + "/"
    return child.startswith(prefix)


def _is_strict_ancestor(child_uri: str, parent_uri: str) -> bool:
    """Return True if ``parent_uri`` is a strict ancestor of ``child_uri``."""
    parent = parent_uri.rstrip("/")
    child = child_uri.rstrip("/")
    if child == parent:
        return False
    prefix = parent + "/"
    return child.startswith(prefix)


def persist_failed_summary_for_directory(
    dir_uri: str,
    error: str,
    recursive: bool = False,
) -> None:
    """Persist a failed summary request for a directory to the centralized JSON file.

    The record is stored in a single JSON file at ``<log_dir>/failed_summaries.json``.
    Uses fcntl file locking for concurrency safety.

    Persistence rules:

    1. If ``recursive`` is True, the record is always persisted.
    2. If ``recursive`` is False, existing records are inspected:
       - If an existing record is a descendant of ``dir_uri``, do not persist
         ``dir_uri``; the descendant record already covers the failure and will
         re-trigger its parent refresh when retried.
       - If an existing record is an ancestor of ``dir_uri`` and was itself
         persisted with ``recursive=False``, remove that ancestor record so the
         more specific ``dir_uri`` failure is tracked instead.
    """
    try:
        json_path = _get_failed_json_path()
        json_path.parent.mkdir(parents=True, exist_ok=True)

        # 规范化 dir_uri：移除尾部斜杠，确保后续查询/删除时 key 一致
        normalized_dir_uri = dir_uri.rstrip("/")

        fd = os.open(str(json_path), os.O_RDWR | os.O_CREAT)
        try:
            _acquire_lock(fd, exclusive=True)

            with os.fdopen(fd, 'r+', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {"records": {}}

                records = data.get("records", {})

                if not recursive:
                    should_persist = True
                    uris_to_remove = []
                    for existing_uri, existing_record in records.items():
                        if _is_descendant_or_same(normalized_dir_uri, existing_uri):
                            # 已有相同目录或子目录失败记录，无需再落盘
                            should_persist = False
                            break
                        if _is_strict_ancestor(normalized_dir_uri, existing_uri):
                            # 已有父目录记录；若其 recursive=False，则替换为更具体的子目录记录
                            if not existing_record.get("recursive", False):
                                uris_to_remove.append(existing_uri)

                    if not should_persist:
                        logger.info(
                            "[FailedSummaryPersistence] Skip persisting %s: "
                            "a descendant record already exists",
                            normalized_dir_uri,
                        )
                        _release_lock(fd)
                        return

                    for uri in uris_to_remove:
                        del records[uri]
                        logger.info(
                            "[FailedSummaryPersistence] Replaced parent record %s "
                            "with more specific record %s",
                            uri,
                            normalized_dir_uri,
                        )

                records[normalized_dir_uri] = {
                    "dir_uri": normalized_dir_uri,
                    "error": error,
                    "failed_at": datetime.now().isoformat(),
                    "recursive": recursive,
                }
                data["records"] = records

                f.seek(0)
                f.truncate()
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(fd)

                # 在 fd 关闭前释放锁
                _release_lock(fd)
        except Exception:
            # 如果 with 块内抛异常，fd 会被 with 关闭，flock 锁会自动释放
            raise
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to persist: {e}")


def delete_failed_summary_record(dir_uri: str, recursive: Optional[bool] = None) -> bool:
    """Delete a failed summary record from the centralized JSON file.

    When ``recursive`` is provided, the record is only deleted if its stored
    ``recursive`` value matches. This prevents accidentally removing a record
    for the same directory that was created with a different recursive flag.

    Returns True if a record was deleted.
    """
    try:
        json_path = _get_failed_json_path()
        if not json_path.exists():
            return False

        normalized_dir_uri = dir_uri.rstrip("/")

        fd = os.open(str(json_path), os.O_RDWR)
        try:
            _acquire_lock(fd, exclusive=True)

            with os.fdopen(fd, 'r+', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    return False

                records = data.get("records", {})
                record = records.get(normalized_dir_uri)
                if record is None:
                    return False

                if recursive is not None:
                    record_recursive = record.get("recursive", False)
                    if record_recursive != recursive:
                        logger.info(
                            f"[FailedSummaryPersistence] Skip deleting {normalized_dir_uri}: "
                            f"recursive mismatch (request={recursive}, record={record_recursive})"
                        )
                        return False

                del records[normalized_dir_uri]

                f.seek(0)
                f.truncate()
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(fd)

                # 在 fd 关闭前释放锁
                _release_lock(fd)
        except Exception:
            # 如果 with 块内抛异常，fd 会被 with 关闭，flock 锁会自动释放
            raise

        logger.info(f"[FailedSummaryPersistence] Deleted failed summary record: {normalized_dir_uri}")
        return True
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to delete: {e}")
        return False


def get_failed_summary_record(dir_uri: str) -> Optional[Dict[str, Any]]:
    """Read a failed summary record from the centralized JSON file.

    Returns None if the record does not exist or cannot be parsed.
    """
    try:
        json_path = _get_failed_json_path()
        if not json_path.exists():
            return None

        normalized_dir_uri = dir_uri.rstrip("/")

        fd = os.open(str(json_path), os.O_RDONLY)
        try:
            _acquire_lock(fd, exclusive=False)

            with os.fdopen(fd, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    return None

                return data.get("records", {}).get(normalized_dir_uri)
        except Exception:
            # 如果 with 块内抛异常，fd 会被 with 关闭，flock 锁会自动释放
            raise
    except Exception:
        return None


# Backwards-compatible wrapper: older callers still pass a file URI plus
# ``original_filename``/``prompt`` arguments. We derive the parent directory
# from the file URI and record the failure against that directory.
def persist_failed_summary_request(
    file_uri: str,
    original_filename: str,
    prompt: str,
    error: str,
) -> None:
    """Backwards-compatible entry point that persists per directory.

    The parent directory of ``file_uri`` is resolved and used as the
    directory URI for the new per-directory persistence scheme.
    """
    try:
        from openviking_cli.utils.uri import VikingURI

        uri_obj = VikingURI(file_uri)
        parent = uri_obj.parent
        dir_uri = parent.uri if parent is not None else file_uri
    except Exception:
        # Fall back to string manipulation if URI parsing fails.
        stripped = file_uri.rstrip("/")
        last_slash = stripped.rfind("/")
        dir_uri = stripped[:last_slash] if last_slash > -1 else file_uri

    persist_failed_summary_for_directory(dir_uri=dir_uri, error=error)


def move_failed_summary_record(from_dir_uri: str, to_dir_uri: str) -> bool:
    """Move a failed summary record from one directory URI to another.

    Returns True if a record was moved.
    """
    try:
        record = get_failed_summary_record(from_dir_uri)
        if record is None:
            return False

        persist_failed_summary_for_directory(
            dir_uri=to_dir_uri,
            error=record.get("error", ""),
            recursive=record.get("recursive", False),
        )
        delete_failed_summary_record(from_dir_uri)
        return True
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to move: {e}")
        return False


def delete_failed_summary_under(dir_uri: str) -> int:
    """Delete all failed.json records under ``dir_uri`` (recursively).

    Returns the number of records deleted.
    """
    try:
        json_path = _get_failed_json_path()
        if not json_path.exists():
            return 0

        normalized_dir_uri = dir_uri.rstrip("/")

        fd = os.open(str(json_path), os.O_RDWR)
        try:
            _acquire_lock(fd, exclusive=True)

            with os.fdopen(fd, 'r+', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    return 0

                records = data.get("records", {})
                prefix = normalized_dir_uri + "/"
                to_delete = [
                    uri for uri in records.keys()
                    if uri == normalized_dir_uri or uri.startswith(prefix)
                ]

                count = 0
                for uri in to_delete:
                    del records[uri]
                    count += 1

                if count > 0:
                    f.seek(0)
                    f.truncate()
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(fd)

                # 在 fd 关闭前释放锁
                _release_lock(fd)
        except Exception:
            # 如果 with 块内抛异常，fd 会被 with 关闭，flock 锁会自动释放
            raise

        return count
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to delete under directory: {e}")
        return 0
