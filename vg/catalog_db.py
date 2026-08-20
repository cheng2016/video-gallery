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
CATALOG_DB_SCHEMA = 2

_db_locks_guard = threading.Lock()
_db_locks: dict[str, threading.RLock] = {}
_schema_ready: set[str] = set()
_schema_ready_lock = threading.Lock()


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


def _db_guard(cache: Path, operation: str = "catalog_db"):
    from vg.diagnostics import timed_lock

    return timed_lock(_lock_for(cache), operation, cache=cache)


def _schema_repair_reason(conn: sqlite3.Connection) -> str:
    """Return why a catalog needs bootstrap, or an empty string when ready.

    ``_schema_ready`` is only an in-process hint.  The database can be removed
    or replaced while the app is running (cache cleanup, another process, or a
    failed previous write), so trusting that hint alone can leave a fresh
    zero-byte database without ``meta``/``videos`` tables.
    """
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing_tables = {"meta", "videos"} - tables
        if missing_tables:
            return "missing_tables:" + ",".join(sorted(missing_tables))
        required_columns = {
            "id", "rel", "file_sig", "name_key", "size", "data", "updated_at",
            "root", "category", "folder", "ext", "mtime", "duration", "kind",
            "name", "search_text", "genres_text", "themes_text", "backgrounds_text",
        }
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(videos)").fetchall()
        }
        missing_columns = required_columns - columns
        if missing_columns:
            return "missing_columns:" + ",".join(sorted(missing_columns))
        schema_row = conn.execute(
            "SELECT 1 FROM meta WHERE key='catalog_schema' LIMIT 1"
        ).fetchone()
        if schema_row is None:
            return "missing_catalog_schema"
    except sqlite3.Error as exc:
        return f"schema_check_failed:{type(exc).__name__}"
    return ""


def _norm_rel(rel: str | None) -> str:
    return (rel or "").replace("\\", "/").strip("/")


def _name_key_for(item: dict) -> str:
    return duplicate_name_key(item)


def _size_of(item: dict) -> int:
    try:
        return int(item.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _query_columns(item: dict, root_s: str = "") -> tuple:
    folder = _norm_rel(item.get("folder"))
    category = folder.split("/", 1)[0] if folder else ""
    try:
        mtime = float(item.get("mtime") or 0)
    except (TypeError, ValueError):
        mtime = 0.0
    try:
        duration = float(item.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    name = str(item.get("name") or Path(item.get("filename") or "").stem or "")
    search_parts = [
        name,
        str(item.get("filename") or ""),
        str(item.get("rel") or ""),
        " ".join(str(x) for x in (item.get("genres") or [])),
        " ".join(str(x) for x in (item.get("themes") or [])),
        " ".join(str(x) for x in (item.get("backgrounds") or [])),
        " ".join(str(x) for x in (item.get("actors") or [])),
    ]
    def tokens(values) -> str:
        return "|" + "|".join(str(x).strip() for x in (values or []) if str(x).strip()) + "|"

    return (
        root_s or str(item.get("_lib_root") or item.get("root") or ""),
        category,
        folder,
        str(item.get("ext") or "").lower(),
        mtime,
        duration,
        str(item.get("kind") or ""),
        name,
        " ".join(search_parts).casefold(),
        tokens(item.get("genres")),
        tokens(item.get("themes")),
        tokens(item.get("backgrounds")),
    )


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
    except OSError as exc:
        from vg.diagnostics import error

        error("catalog_stat_failed", exc, cache=cache, path=path)
        return False


def _connect(cache: Path) -> sqlite3.Connection:
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    path = catalog_db_path(cache)
    assert path is not None
    try:
        cache_key = str(path.resolve()).casefold()
    except OSError:
        cache_key = str(path).casefold()
    with _schema_ready_lock:
        first_open = cache_key not in _schema_ready
    if first_open:
        _clear_path_attrs_windows(path)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if first_open:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
    else:
        conn.execute("PRAGMA temp_store=MEMORY")
    repair_reason = "" if first_open else _schema_repair_reason(conn)
    if first_open or repair_reason:
        if not first_open:
            from vg.diagnostics import emit

            emit(
                "WARN",
                "sqlite_schema_repair",
                force=True,
                cache=cache,
                reason=repair_reason,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(conn)
        with _schema_ready_lock:
            _schema_ready.add(cache_key)
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
            root TEXT,
            category TEXT,
            folder TEXT,
            ext TEXT,
            mtime REAL,
            duration REAL,
            kind TEXT,
            name TEXT,
            search_text TEXT,
            genres_text TEXT,
            themes_text TEXT,
            backgrounds_text TEXT,
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
    existing = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(videos)").fetchall()
    }
    for name, sql_type in (
        ("root", "TEXT"),
        ("category", "TEXT"),
        ("folder", "TEXT"),
        ("ext", "TEXT"),
        ("mtime", "REAL"),
        ("duration", "REAL"),
        ("kind", "TEXT"),
        ("name", "TEXT"),
        ("search_text", "TEXT"),
        ("genres_text", "TEXT"),
        ("themes_text", "TEXT"),
        ("backgrounds_text", "TEXT"),
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {sql_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_scope_mtime "
        "ON videos(category, folder, mtime DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_category_mtime "
        "ON videos(category, mtime DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_category_size "
        "ON videos(category, size DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_category_duration "
        "ON videos(category, duration DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_scope_name "
        "ON videos(category, folder, name COLLATE NOCASE)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_ext ON videos(ext)")
    row = conn.execute(
        "SELECT value FROM meta WHERE key='catalog_schema'"
    ).fetchone()
    old_schema = int(row["value"]) if row and str(row["value"]).isdigit() else 0
    if old_schema < CATALOG_DB_SCHEMA:
        migration_started = time.perf_counter()
        root_row = conn.execute(
            "SELECT value FROM meta WHERE key='root'"
        ).fetchone()
        root_s = str(root_row["value"]) if root_row else ""
        rows = conn.execute("SELECT id, data FROM videos").fetchall()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for video_row in rows:
                item = _decode_row(video_row)
                if not item:
                    continue
                conn.execute(
                    """
                    UPDATE videos
                    SET root=?, category=?, folder=?, ext=?, mtime=?, duration=?,
                        kind=?, name=?, search_text=?, genres_text=?, themes_text=?,
                        backgrounds_text=?
                    WHERE id=?
                    """,
                    (*_query_columns(item, root_s), video_row["id"]),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        from vg.diagnostics import emit

        emit(
            "INFO",
            "catalog_schema_migrated",
            force=True,
            old_schema=old_schema,
            new_schema=CATALOG_DB_SCHEMA,
            rows=len(rows),
            elapsed_ms=f"{(time.perf_counter() - migration_started) * 1000.0:.1f}",
        )
    if not row or old_schema < CATALOG_DB_SCHEMA:
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
    with _db_guard(cache, "catalog_read_counts"):
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
    started = time.perf_counter()
    from vg.diagnostics import emit, perf, timed_lock

    with timed_lock(_lock_for(cache), "catalog_load_all", cache=cache):
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
                            emit(
                                "WARN",
                                "catalog_root_mismatch",
                                force=True,
                                cache=cache,
                                stored_root=stored,
                                requested_root=want,
                                action="return_rows_for_caller_filter",
                            )
                rows = conn.execute("SELECT data FROM videos").fetchall()
                out: list[dict] = []
                for row in rows:
                    item = _decode_row(row)
                    if item and item.get("id"):
                        out.append(item)
                perf(
                    "catalog_load_all",
                    (time.perf_counter() - started) * 1000.0,
                    force=True,
                    cache=cache,
                    rows=len(out),
                    requested_root=root,
                )
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


_SORT_SQL = {
    "mtime_desc": "mtime DESC, id",
    "mtime_asc": "mtime ASC, id",
    "name": "name COLLATE NOCASE ASC, id",
    "size_desc": "size DESC, id",
    "size_asc": "size ASC, id",
    "duration_desc": "duration DESC, id",
    "duration_asc": "duration ASC, id",
}


def query_catalog_page(
    cache: Path,
    *,
    category: str = "",
    folder: str = "",
    include_descendants: bool = True,
    ext: str = "",
    search: str = "",
    genre: str = "",
    theme: str = "",
    background: str = "",
    sort: str = "mtime_desc",
    offset: int = 0,
    limit: int = 60,
) -> tuple[list[dict], int]:
    """SQL-filtered page for flat-list browsing; avoids full JSON decode."""
    if not catalog_exists(cache):
        return [], 0
    clauses: list[str] = []
    params: list[object] = []
    category_n = _norm_rel(category)
    folder_n = _norm_rel(folder)
    if category_n == "__root__":
        clauses.append("category=''")
    elif category_n:
        clauses.append("category=?")
        params.append(category_n)
    if folder_n:
        if include_descendants:
            clauses.append("(folder=? OR folder LIKE ? ESCAPE '\\')")
            escaped = folder_n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend((folder_n, escaped + "/%"))
        else:
            clauses.append("folder=?")
            params.append(folder_n)
    if ext:
        ext_n = ext.lower()
        if not ext_n.startswith("."):
            ext_n = "." + ext_n
        clauses.append("ext=?")
        params.append(ext_n)
    if search:
        clauses.append("search_text LIKE ?")
        params.append("%" + search.casefold() + "%")
    for column, value in (
        ("genres_text", genre),
        ("themes_text", theme),
        ("backgrounds_text", background),
    ):
        if value:
            clauses.append(f"{column} LIKE ?")
            params.append(f"%|{value}|%")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order = _SORT_SQL.get(sort, _SORT_SQL["mtime_desc"])
    page_params = [*params, max(1, min(int(limit), 10_000)), max(0, int(offset))]
    started = time.perf_counter()
    from vg.diagnostics import timed_lock

    with timed_lock(_lock_for(cache), "sqlite_query_page", cache=cache):
        try:
            conn = _connect(cache)
            try:
                count_started = time.perf_counter()
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM videos{where}",
                        params,
                    ).fetchone()["n"]
                )
                count_ms = (time.perf_counter() - count_started) * 1000.0
                select_started = time.perf_counter()
                rows = conn.execute(
                    f"SELECT data FROM videos{where} ORDER BY {order} LIMIT ? OFFSET ?",
                    page_params,
                ).fetchall()
                select_ms = (time.perf_counter() - select_started) * 1000.0
                decode_started = time.perf_counter()
                root_s = (_meta_get(conn, "root") or "").strip()
                out = []
                for row in rows:
                    item = _decode_row(row)
                    if not item:
                        continue
                    if root_s and not item.get("root"):
                        item["root"] = root_s
                    out.append(item)
                decode_ms = (time.perf_counter() - decode_started) * 1000.0
                from vg.diagnostics import perf

                perf(
                    "sqlite_query_page",
                    (time.perf_counter() - started) * 1000.0,
                    rows=len(out),
                    total_rows=total,
                    offset=offset,
                    limit=limit,
                    cache=cache,
                    count_ms=f"{count_ms:.1f}",
                    select_ms=f"{select_ms:.1f}",
                    decode_ms=f"{decode_ms:.1f}",
                    category=category_n or "all",
                    folder=folder_n,
                    search=bool(search),
                    sort=sort,
                )
                return out, total
            finally:
                conn.close()
        except sqlite3.Error as exc:
            from vg.diagnostics import error

            error("sqlite_query_page_failed", exc, cache=cache)
            return [], 0


def query_catalogs_page(
    caches: list[Path],
    **kwargs,
) -> tuple[list[dict], int]:
    """Merge bounded SQL pages from multiple per-disk catalogs."""
    offset = max(0, int(kwargs.pop("offset", 0) or 0))
    limit = max(1, min(int(kwargs.pop("limit", 60) or 60), 200))
    sort = str(kwargs.get("sort") or "mtime_desc")
    candidates: list[dict] = []
    total = 0
    started = time.perf_counter()
    per_cache: list[str] = []
    for cache in caches:
        cache_started = time.perf_counter()
        rows, count = query_catalog_page(
            cache,
            offset=0,
            limit=offset + limit,
            **kwargs,
        )
        candidates.extend(rows)
        total += count
        per_cache.append(
            f"{cache.name}:rows={len(rows)},total={count},"
            f"ms={(time.perf_counter() - cache_started) * 1000.0:.1f}"
        )

    reverse = sort not in {"mtime_asc", "name", "size_asc", "duration_asc"}
    if sort == "name":
        key = lambda item: (str(item.get("name") or "").casefold(), item.get("id") or "")
    elif sort.startswith("size_"):
        key = lambda item: (int(item.get("size") or 0), item.get("id") or "")
    elif sort.startswith("duration_"):
        key = lambda item: (float(item.get("duration") or 0), item.get("id") or "")
    else:
        key = lambda item: (float(item.get("mtime") or 0), item.get("id") or "")
    candidates.sort(key=key, reverse=reverse)
    page = candidates[offset: offset + limit]
    from vg.diagnostics import perf

    perf(
        "sqlite_multi_page_merge",
        (time.perf_counter() - started) * 1000.0,
        caches=len(caches),
        candidates=len(candidates),
        rows=len(page),
        total_rows=total,
        offset=offset,
        limit=limit,
        sort=sort,
        per_cache=" | ".join(per_cache),
    )
    return page, total


def merge_catalog_facets(facets: list[dict]) -> dict:
    out = {"genres": [], "themes": [], "backgrounds": [], "subfolders": []}
    for key in out:
        counts: dict[str, dict] = {}
        for payload in facets:
            for row in payload.get(key) or []:
                identity = str(row.get("id") or row.get("name") or "")
                if not identity:
                    continue
                current = counts.get(identity)
                if current is None:
                    current = dict(row)
                    current["count"] = 0
                    counts[identity] = current
                current["count"] = int(current.get("count") or 0) + int(
                    row.get("count") or 0
                )
        out[key] = sorted(
            counts.values(),
            key=lambda row: (-int(row.get("count") or 0), str(row.get("name") or "")),
        )
    return out


def query_catalog_facets(
    cache: Path,
    *,
    category: str = "",
    folder: str = "",
    include_descendants: bool = True,
    ext: str = "",
    search: str = "",
) -> dict:
    """Read narrow indexed columns for first-page facets, never full JSON blobs."""
    if not catalog_exists(cache):
        return {"genres": [], "themes": [], "backgrounds": [], "subfolders": []}
    clauses: list[str] = []
    params: list[object] = []
    category_n = _norm_rel(category)
    folder_n = _norm_rel(folder)
    if category_n == "__root__":
        clauses.append("category=''")
    elif category_n:
        clauses.append("category=?")
        params.append(category_n)
    if folder_n:
        if include_descendants:
            clauses.append("(folder=? OR folder LIKE ?)")
            params.extend((folder_n, folder_n + "/%"))
        else:
            clauses.append("folder=?")
            params.append(folder_n)
    if ext:
        ext_n = ext if ext.startswith(".") else "." + ext
        clauses.append("ext=?")
        params.append(ext_n.lower())
    if search:
        clauses.append("search_text LIKE ?")
        params.append("%" + search.casefold() + "%")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    def count_tokens(rows, key: str) -> list[dict]:
        counts: dict[str, int] = {}
        for row in rows:
            for value in str(row[key] or "").strip("|").split("|"):
                if value:
                    counts[value] = counts.get(value, 0) + 1
        return [
            {"id": value, "name": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]

    started = time.perf_counter()
    from vg.diagnostics import timed_lock

    with timed_lock(_lock_for(cache), "sqlite_query_facets", cache=cache):
        try:
            conn = _connect(cache)
            try:
                rows = conn.execute(
                    f"SELECT folder, genres_text, themes_text, backgrounds_text "
                    f"FROM videos{where}",
                    params,
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            from vg.diagnostics import error

            error("sqlite_facets_failed", exc, cache=cache)
            return {"genres": [], "themes": [], "backgrounds": [], "subfolders": []}
    prefix = folder_n or category_n
    sub_counts: dict[str, int] = {}
    for row in rows:
        value = _norm_rel(row["folder"])
        if prefix:
            if value == prefix or not value.startswith(prefix + "/"):
                continue
            rest = value[len(prefix) + 1 :]
        else:
            rest = value
        child = rest.split("/", 1)[0] if rest else ""
        if child:
            full = f"{prefix}/{child}" if prefix else child
            sub_counts[full] = sub_counts.get(full, 0) + 1
    subfolders = [
        {"id": path, "name": path.rsplit("/", 1)[-1], "count": count}
        for path, count in sorted(sub_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    payload = {
        "genres": count_tokens(rows, "genres_text"),
        "themes": count_tokens(rows, "themes_text"),
        "backgrounds": count_tokens(rows, "backgrounds_text"),
        "subfolders": subfolders,
    }
    from vg.diagnostics import perf

    perf(
        "sqlite_query_facets",
        (time.perf_counter() - started) * 1000.0,
        cache=cache,
        source_rows=len(rows),
        genres=len(payload["genres"]),
        themes=len(payload["themes"]),
        backgrounds=len(payload["backgrounds"]),
        subfolders=len(payload["subfolders"]),
        category=category_n or "all",
        folder=folder_n,
        search=bool(search),
    )
    return payload


def checkpoint_catalog(cache: Path, *, truncate: bool = True) -> bool:
    """Checkpoint WAL after bulk work; never required on a request hot path."""
    if not catalog_exists(cache):
        return False
    with _db_guard(cache, "catalog_checkpoint"):
        try:
            conn = _connect(cache)
            try:
                conn.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                    if truncate
                    else "PRAGMA wal_checkpoint(PASSIVE)"
                )
                return True
            finally:
                conn.close()
        except sqlite3.Error as exc:
            from vg.diagnostics import error

            error("sqlite_checkpoint_failed", exc, cache=cache)
            return False


def save_catalog(
    cache: Path,
    root: Path | str,
    videos: list[dict],
    *,
    file_count: int | None = None,
    folder_counts: dict[str, int] | None = None,
) -> bool:
    """Replace the whole catalog for one cache (scan completion)."""
    started = time.perf_counter()
    try:
        root_s = str(Path(root).expanduser().resolve())
    except OSError:
        root_s = str(root).strip()

    with _db_guard(cache, "catalog_save"):
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
                    query_values = _query_columns(item, root_s)
                    conn.execute(
                        """
                        INSERT INTO videos(
                            id, rel, file_sig, name_key, size,
                            root, category, folder, ext, mtime, duration, kind, name, search_text,
                            genres_text, themes_text, backgrounds_text,
                            data, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            vid,
                            rel,
                            str(item.get("file_sig") or "") or None,
                            _name_key_for(item) or None,
                            _size_of(item),
                            *query_values,
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
                from vg.diagnostics import perf

                perf(
                    "sqlite_save_catalog",
                    (time.perf_counter() - started) * 1000.0,
                    force=True,
                    rows=len(videos),
                    cache=cache,
                )
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
            from vg.diagnostics import error

            error("sqlite_save_catalog_failed", e, cache=cache)
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
    started = time.perf_counter()
    try:
        root_s = str(Path(root).expanduser().resolve())
    except OSError:
        root_s = str(root).strip()

    with _db_guard(cache, "catalog_upsert"):
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
                    query_values = _query_columns(item, root_s)
                    if existing is not None:
                        old_id = existing["id"]
                        conn.execute(
                            """
                            UPDATE videos
                            SET id=?, rel=?, file_sig=?, name_key=?, size=?,
                                root=?, category=?, folder=?, ext=?, mtime=?, duration=?,
                                kind=?, name=?, search_text=?, genres_text=?, themes_text=?,
                                backgrounds_text=?, data=?, updated_at=?
                            WHERE id=?
                            """,
                            (
                                source_id,
                                rel,
                                file_sig,
                                name_key,
                                size,
                                *query_values,
                                payload,
                                now,
                                old_id,
                            ),
                        )
                        changed += 1
                    elif allow_insert:
                        conn.execute(
                            """
                            INSERT INTO videos(
                                id, rel, file_sig, name_key, size,
                                root, category, folder, ext, mtime, duration, kind, name, search_text,
                                genres_text, themes_text, backgrounds_text,
                                data, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source_id,
                                rel,
                                file_sig,
                                name_key,
                                size,
                                *query_values,
                                payload,
                                now,
                            ),
                        )
                        changed += 1
                if changed:
                    _meta_set(conn, "updated", datetime.now().isoformat())
                    _meta_set(conn, "root", root_s)
                conn.execute("COMMIT")
                from vg.diagnostics import perf

                perf(
                    "sqlite_upsert",
                    (time.perf_counter() - started) * 1000.0,
                    rows=changed,
                    cache=cache,
                )
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
            from vg.diagnostics import error

            error("sqlite_upsert_failed", e, cache=cache, rows=len(items))
            return 0


def lookup_probe_by_sig(cache: Path, file_sig: str) -> dict | None:
    sig = (file_sig or "").strip()
    if not sig or not catalog_exists(cache):
        return None
    with _db_guard(cache, "catalog_lookup_sig"):
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
    with _db_guard(cache, "catalog_lookup_name_size"):
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
    with _db_guard(cache, "catalog_read_root"):
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
