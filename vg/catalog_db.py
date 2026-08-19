# -*- coding: utf-8 -*-
"""Per-cache SQLite catalog: video list + probe fields, row-level UPSERT."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from vg.config import VGDATA_DIR
from vg.duplicates import duplicate_name_key
from vg.schema import INDEX_SCHEMA_VERSION, serialize_video_item
from vg.util import _clear_path_attrs_windows, log

CATALOG_DB_NAME = "catalog.sqlite"
CATALOG_DB_SCHEMA = 1

_db_locks_guard = threading.Lock()
_db_locks: dict[str, threading.RLock] = {}


def catalog_db_path(cache: Path | None) -> Path | None:
    if not cache:
        return None
    return Path(cache) / CATALOG_DB_NAME


def _lock_for(cache: Path) -> threading.RLock:
    try:
        key = str(Path(cache).resolve()).casefold()
    except OSError:
        key = str(cache).casefold()
    with _db_locks_guard:
        lock = _db_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _db_locks[key] = lock
        return lock


def _norm_rel(rel: str | None) -> str:
    return (rel or "").replace("\\", "/").strip("/")


def _name_key_for(item: dict) -> str:
    return duplicate_name_key(item)


def _size_of(item: dict) -> int:
    try:
        return int(item.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def catalog_mtime(cache: Path | None) -> float:
    path = catalog_db_path(cache)
    if not path:
        return 0.0
    try:
        return path.stat().st_mtime if path.is_file() else 0.0
    except OSError:
        return 0.0


def catalog_exists(cache: Path | None) -> bool:
    path = catalog_db_path(cache)
    try:
        return bool(path and path.is_file() and path.stat().st_size > 0)
    except OSError:
        return False


def _connect(cache: Path) -> sqlite3.Connection:
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    path = catalog_db_path(cache)
    assert path is not None
    _clear_path_attrs_windows(path)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            rel TEXT NOT NULL COLLATE NOCASE,
            file_sig TEXT,
            name_key TEXT,
            size INTEGER,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_rel ON videos(rel COLLATE NOCASE)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_sig ON videos(file_sig)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_name_size ON videos(name_key, size)"
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key='catalog_schema'"
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('catalog_schema', ?)",
            (str(CATALOG_DB_SCHEMA),),
        )


def _meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (key, value),
    )


def _decode_row(row: sqlite3.Row) -> dict | None:
    try:
        data = json.loads(row["data"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def read_catalog_counts(cache: Path) -> tuple[int | None, dict[str, int] | None]:
    """Return (file_count, folder_counts). Both None when catalog has no counts."""
    if not catalog_exists(cache):
        return None, None
    with _lock_for(cache):
        try:
            conn = _connect(cache)
            try:
                if _meta_get(conn, "folder_counts") is None:
                    return None, None
                folders_raw = _meta_get(conn, "folder_counts", "{}") or "{}"
                try:
                    folders = json.loads(folders_raw)
                except json.JSONDecodeError:
                    folders = {}
                if not isinstance(folders, dict):
                    folders = {}
                out: dict[str, int] = {}
                for key, value in folders.items():
                    try:
                        folder = str(key or "").replace("\\", "/").strip("/")
                        count = int(value)
                    except (TypeError, ValueError):
                        continue
                    if count > 0:
                        out[folder] = count
                try:
                    file_count = int(_meta_get(conn, "file_count", "0") or 0)
                except (TypeError, ValueError):
                    file_count = sum(out.values())
                return file_count, out
            finally:
                conn.close()
        except sqlite3.Error as e:
            log(f"[目录库] 读取计数失败 {cache}: {e}")
            return None, None


def load_catalog_videos(cache: Path, root: Path | str | None = None) -> list[dict]:
    """Load all video rows from one cache catalog."""
    if not catalog_exists(cache):
        return []
    with _lock_for(cache):
        try:
            conn = _connect(cache)
            try:
                if root is not None:
                    stored = (_meta_get(conn, "root") or "").strip()
                    if stored:
                        try:
                            want = str(Path(root).expanduser().resolve())
                        except OSError:
                            want = str(root).strip()
                        if stored.casefold() != want.casefold():
                            # Still return rows; caller may filter. Log once lightly.
                            pass
                rows = conn.execute("SELECT data FROM videos").fetchall()
                out: list[dict] = []
                for row in rows:
                    item = _decode_row(row)
                    if item and item.get("id"):
                        out.append(item)
                return out
            finally:
                conn.close()
        except sqlite3.Error as e:
            log(f"[目录库] 读取失败 {cache}: {e}")
            return []


def load_catalog_by_rel(cache: Path) -> dict[str, dict]:
    """rel → row for incremental scan reuse (skip ts_set)."""
    out: dict[str, dict] = {}
    for item in load_catalog_videos(cache):
        if item.get("kind") == "ts_set":
            continue
        rel = _norm_rel(item.get("rel"))
        if rel:
            out[rel] = item
    return out


def save_catalog(
    cache: Path,
    root: Path | str,
    videos: list[dict],
    *,
    file_count: int | None = None,
    folder_counts: dict[str, int] | None = None,
) -> bool:
    """Replace the whole catalog for one cache (scan completion)."""
    try:
        root_s = str(Path(root).expanduser().resolve())
    except OSError:
        root_s = str(root).strip()

    with _lock_for(cache):
        try:
            conn = _connect(cache)
            try:
                preserved_count = _meta_get(conn, "file_count")
                preserved_folders = _meta_get(conn, "folder_counts")
                if folder_counts is not None:
                    use_folders = {
                        str(k or "").replace("\\", "/").strip("/"): int(v)
                        for k, v in folder_counts.items()
                        if int(v) > 0
                    }
                elif preserved_folders:
                    try:
                        raw = json.loads(preserved_folders)
                        use_folders = {
                            str(k or "").replace("\\", "/").strip("/"): int(v)
                            for k, v in (raw or {}).items()
                            if int(v) > 0
                        } if isinstance(raw, dict) else {}
                    except (TypeError, ValueError, json.JSONDecodeError):
                        use_folders = {}
                else:
                    use_folders = {}

                if file_count is not None:
                    use_count = int(file_count)
                elif preserved_count is not None:
                    try:
                        use_count = int(preserved_count)
                    except (TypeError, ValueError):
                        use_count = sum(use_folders.values())
                else:
                    use_count = sum(use_folders.values())

                now = time.time()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM videos")
                for raw in videos:
                    if not isinstance(raw, dict) or not raw.get("id"):
                        continue
                    item = serialize_video_item(raw, root=root_s, cache=cache)
                    vid = (item.get("id") or "").strip()
                    if not vid:
                        continue
                    rel = _norm_rel(item.get("rel"))
                    conn.execute(
                        """
                        INSERT INTO videos(id, rel, file_sig, name_key, size, data, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            vid,
                            rel,
                            str(item.get("file_sig") or "") or None,
                            _name_key_for(item) or None,
                            _size_of(item),
                            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                            now,
                        ),
                    )
                _meta_set(conn, "schema_ver", str(INDEX_SCHEMA_VERSION))
                _meta_set(conn, "catalog_schema", str(CATALOG_DB_SCHEMA))
                _meta_set(conn, "root", root_s)
                _meta_set(conn, "file_count", str(int(use_count)))
                _meta_set(
                    conn,
                    "folder_counts",
                    json.dumps(use_folders, ensure_ascii=False, separators=(",", ":")),
                )
                _meta_set(conn, "updated", datetime.now().isoformat())
                conn.execute("COMMIT")
                return True
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as e:
            log(f"[目录库] 保存失败 {cache}: {e}")
            return False


def upsert_catalog_videos(
    cache: Path,
    root: Path | str,
    items: list[dict],
    *,
    allow_insert: bool = False,
) -> int:
    """UPSERT rows without rewriting the whole catalog. Returns changed count."""
    if not items:
        return 0
    try:
        root_s = str(Path(root).expanduser().resolve())
    except OSError:
        root_s = str(root).strip()

    with _lock_for(cache):
        try:
            conn = _connect(cache)
            try:
                now = time.time()
                changed = 0
                conn.execute("BEGIN IMMEDIATE")
                for raw in items:
                    if not isinstance(raw, dict) or not raw.get("id"):
                        continue
                    item = serialize_video_item(raw, root=root_s, cache=cache)
                    source_id = (
                        raw.get("_thumb_id") or item.get("id") or raw.get("id") or ""
                    ).strip()
                    rel = _norm_rel(item.get("rel") or raw.get("rel"))
                    if not source_id:
                        continue
                    existing = conn.execute(
                        "SELECT id FROM videos WHERE id=?",
                        (source_id,),
                    ).fetchone()
                    if existing is None and rel:
                        existing = conn.execute(
                            "SELECT id FROM videos WHERE rel=? COLLATE NOCASE",
                            (rel,),
                        ).fetchone()
                    payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    file_sig = str(item.get("file_sig") or "") or None
                    name_key = _name_key_for(item) or None
                    size = _size_of(item)
                    if existing is not None:
                        old_id = existing["id"]
                        conn.execute(
                            """
                            UPDATE videos
                            SET id=?, rel=?, file_sig=?, name_key=?, size=?, data=?, updated_at=?
                            WHERE id=?
                            """,
                            (
                                source_id,
                                rel,
                                file_sig,
                                name_key,
                                size,
                                payload,
                                now,
                                old_id,
                            ),
                        )
                        changed += 1
                    elif allow_insert:
                        conn.execute(
                            """
                            INSERT INTO videos(id, rel, file_sig, name_key, size, data, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (source_id, rel, file_sig, name_key, size, payload, now),
                        )
                        changed += 1
                if changed:
                    _meta_set(conn, "updated", datetime.now().isoformat())
                    _meta_set(conn, "root", root_s)
                conn.execute("COMMIT")
                return changed
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as e:
            log(f"[目录库] UPSERT 失败 {cache}: {e}")
            return 0


def lookup_probe_by_sig(cache: Path, file_sig: str) -> dict | None:
    sig = (file_sig or "").strip()
    if not sig or not catalog_exists(cache):
        return None
    with _lock_for(cache):
        try:
            conn = _connect(cache)
            try:
                row = conn.execute(
                    "SELECT data FROM videos WHERE file_sig=? LIMIT 1",
                    (sig,),
                ).fetchone()
                return _decode_row(row) if row else None
            finally:
                conn.close()
        except sqlite3.Error:
            return None


def lookup_probe_by_name_size(cache: Path, name_key: str, size: int) -> dict | None:
    key = (name_key or "").strip()
    if not key or size <= 0 or not catalog_exists(cache):
        return None
    with _lock_for(cache):
        try:
            conn = _connect(cache)
            try:
                row = conn.execute(
                    "SELECT data FROM videos WHERE name_key=? AND size=? LIMIT 1",
                    (key, int(size)),
                ).fetchone()
                return _decode_row(row) if row else None
            finally:
                conn.close()
        except sqlite3.Error:
            return None


def iter_catalog_cache_dirs() -> list[Path]:
    """Known cache directories that already have a catalog.sqlite."""
    found: list[Path] = []
    seen: set[str] = set()

    def add(cache: Path) -> None:
        try:
            key = str(cache.resolve()).casefold()
        except OSError:
            key = str(cache).casefold()
        if key in seen:
            return
        if catalog_exists(cache):
            seen.add(key)
            found.append(Path(cache))

    try:
        if VGDATA_DIR.is_dir():
            for path in VGDATA_DIR.glob(f"*/{CATALOG_DB_NAME}"):
                add(path.parent)
    except OSError:
        pass

    from vg.state import STATE

    for lib in (STATE.get("disk_libs") or {}).values():
        if not isinstance(lib, dict):
            continue
        cache_s = (lib.get("cache_dir") or "").strip()
        if cache_s:
            add(Path(cache_s))
    cache = STATE.get("cache_dir")
    if cache:
        add(Path(cache))
    return found


def read_catalog_root(cache: Path) -> str:
    """Return the stored scan-root path for one catalog, or empty string."""
    if not catalog_exists(cache):
        return ""
    with _lock_for(cache):
        try:
            conn = _connect(cache)
            try:
                return (_meta_get(conn, "root") or "").strip()
            finally:
                conn.close()
        except sqlite3.Error:
            return ""


def find_probe_donor(
    *,
    file_sig: str = "",
    name_key: str = "",
    size: int = 0,
    skip_bad: bool = True,
) -> dict | None:
    """Cross-cache lookup for reusable probe fields."""
    sig = (file_sig or "").strip()
    if sig:
        for cache in iter_catalog_cache_dirs():
            hit = lookup_probe_by_sig(cache, sig)
            if hit and (not skip_bad or not hit.get("bad")):
                return hit
    if name_key and size > 0:
        for cache in iter_catalog_cache_dirs():
            hit = lookup_probe_by_name_size(cache, name_key, size)
            if hit and (not skip_bad or not hit.get("bad")):
                return hit
    return None
