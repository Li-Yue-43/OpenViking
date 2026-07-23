# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared utilities for persisting failed summary requests to disk."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

_RESOURCES_PREFIX = "viking://resources/"
_SCHEME_PREFIX = "viking://"
_FAILED_FILENAME = "failed.json"


def get_failed_summary_log_dir() -> Path:
    """Return the configured directory for persisting failed summary requests."""
    try:
        config = get_openviking_config()
        return Path(config.semantic.failed_summary_log_dir)
    except Exception:
        return Path("/data/openviking_log/failed_summaries")


def _dir_uri_to_relative_path(dir_uri: str) -> str:
    """Convert a directory URI to the relative path under the log directory."""
    if dir_uri.startswith(_RESOURCES_PREFIX):
        return dir_uri[len(_RESOURCES_PREFIX):].strip("/")
    if dir_uri.startswith(_SCHEME_PREFIX):
        return dir_uri[len(_SCHEME_PREFIX):].strip("/")
    return dir_uri.strip("/")


def _failed_json_path(dir_uri: str) -> Path:
    """Return the path to the failed.json record for a directory URI."""
    base_dir = get_failed_summary_log_dir()
    rel = _dir_uri_to_relative_path(dir_uri)
    return base_dir / rel / _FAILED_FILENAME


def _cleanup_empty_dirs(path: Path, stop_at: Path) -> None:
    """Remove empty parent directories up to (but not including) stop_at."""
    try:
        current = path.parent
        while current != stop_at and current.is_relative_to(stop_at):
            try:
                # Only remove empty directories; rmdir raises if not empty.
                current.rmdir()
            except OSError:
                break
            current = current.parent
    except Exception:
        pass


def persist_failed_summary_for_directory(
    dir_uri: str,
    error: str,
) -> None:
    """Persist a failed summary request for a directory to disk.

    The record is stored at ``<log_dir>/<relative_path>/failed.json``.
    The relative path is derived from ``dir_uri`` by stripping the
    ``viking://resources/`` prefix, or the ``viking://`` prefix if the URI
    does not point into the resources scope.
    """
    try:
        failed_json = _failed_json_path(dir_uri)
        failed_json.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "dir_uri": dir_uri,
            "error": error,
            "failed_at": datetime.now().isoformat(),
        }
        failed_json.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to persist failed summary request: {e}")


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


def delete_failed_summary_record(dir_uri: str) -> bool:
    """Delete the persisted failed summary record for the given directory URI.

    After deleting the record, empty parent directories are cleaned up up to
    the log root.

    Returns True if a record was deleted.
    """
    try:
        failed_json = _failed_json_path(dir_uri)
        if not failed_json.exists():
            return False
        failed_json.unlink()
        _cleanup_empty_dirs(failed_json, get_failed_summary_log_dir())
        return True
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to delete failed summary record: {e}")
        return False


def move_failed_summary_record(from_dir_uri: str, to_dir_uri: str) -> bool:
    """Move a failed summary record from one directory URI to another.

    Returns True if a record was moved.
    """
    try:
        src = _failed_json_path(from_dir_uri)
        if not src.exists():
            return False
        dst = _failed_json_path(to_dir_uri)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Update the stored dir_uri to reflect the new location.
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data["dir_uri"] = to_dir_uri
        dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        src.unlink()
        _cleanup_empty_dirs(src, get_failed_summary_log_dir())
        return True
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to move failed summary record: {e}")
        return False


def delete_failed_summary_under(dir_uri: str) -> int:
    """Delete all failed.json records under ``dir_uri`` (recursively).

    Returns the number of records deleted.
    """
    try:
        base_dir = get_failed_summary_log_dir()
        rel = _dir_uri_to_relative_path(dir_uri)
        root = base_dir / rel
        if not root.exists():
            return 0
        count = 0
        for failed_json in root.rglob(_FAILED_FILENAME):
            try:
                failed_json.unlink()
                count += 1
            except Exception:
                continue
        # Clean up empty directories that were left behind.
        for subdir in sorted(root.rglob("*"), reverse=True):
            if subdir.is_dir():
                try:
                    subdir.rmdir()
                except OSError:
                    pass
        try:
            root.rmdir()
        except OSError:
            pass
        return count
    except Exception as e:
        logger.error(f"[FailedSummaryPersistence] Failed to delete failed summary records under directory: {e}")
        return 0


def get_failed_summary_record(dir_uri: str) -> Optional[Dict[str, Any]]:
    """Read the failed summary record for ``dir_uri``.

    Returns None if the record does not exist or cannot be parsed.
    """
    try:
        failed_json = _failed_json_path(dir_uri)
        if not failed_json.exists():
            return None
        return json.loads(failed_json.read_text(encoding="utf-8"))
    except Exception:
        return None
