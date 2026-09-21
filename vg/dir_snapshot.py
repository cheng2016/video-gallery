# -*- coding: utf-8 -*-
"""Per-directory mtime snapshot so an unchanged tree is not scandir'd again.

NTFS updates a directory's mtime when a child is created, deleted, or renamed
in that directory — not when a nested directory changes, and not when an
existing file's bytes change. So a matching mtime lets us skip ``scandir``
and reuse the previous child list; file size/mtime is still checked by the
caller for content edits. A volume USN match (only reliable when the catalog
cache is not on the same volume, or nothing else has written) skips the walk.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from vg.config import PLAYLIST_EXTS, VIDEO_EXTS

_SNAPSHOT_NAME = "dir_snapshot.json"
_SCHEMA = 1
_MEDIA_EXTS = VIDEO_EXTS | PLAYLIST_EXTS


def _mtime_ns(st: os.stat_result) -> int:
    ns = getattr(st, "st_mtime_ns", None)
    if isinstance(ns, int) and ns > 0:
        return ns
    return int(float(st.st_mtime) * 1_000_000_000)


def _rel_key(root: Path, path: Path) -> str:
    try:
        rel = Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        rel = Path(path)
    text = rel.as_posix()
    return "" if text in ("", ".") else text


def _media_names(filenames: list[str]) -> list[str]:
    out = [name for name in filenames if Path(name).suffix.lower() in _MEDIA_EXTS]
    out.sort(key=str.casefold)
    return out


def query_volume_usn(root: Path) -> tuple[int, int] | None:
    """Return ``(journal_id, next_usn)`` or None when the journal isn't readable."""
    if os.name != "nt":
        return None
    try:
        drive = Path(root).resolve().drive
    except OSError:
        return None
    if not drive:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateFileW(
            "\\\\.\\" + drive,
            0x80000000,  # GENERIC_READ
            0x7,  # read | write | delete
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle is None or int(handle) in (0, invalid or -1):
            return None

        class _UsnJournalData(ctypes.Structure):
            _fields_ = [
                ("UsnJournalID", ctypes.c_uint64),
                ("FirstUsn", ctypes.c_int64),
                ("NextUsn", ctypes.c_int64),
                ("LowestValidUsn", ctypes.c_int64),
                ("MaxUsn", ctypes.c_int64),
                ("MaximumSize", ctypes.c_uint64),
                ("AllocationDelta", ctypes.c_uint64),
            ]

        out = _UsnJournalData()
        returned = wintypes.DWORD()
        ok = kernel32.DeviceIoControl(
            handle,
            0x000900F4,  # FSCTL_QUERY_USN_JOURNAL
            None,
            0,
            ctypes.byref(out),
            ctypes.sizeof(out),
            ctypes.byref(returned),
            None,
        )
        kernel32.CloseHandle(handle)
        if not ok:
            return None
        return int(out.UsnJournalID), int(out.NextUsn)
    except Exception:
        return None


class DirSnapshot:
    def __init__(self) -> None:
        self.dirs: dict[str, dict] = {}
        self.usn_journal: int | None = None
        self.usn_next: int | None = None
        self.hits = 0
        self.dirty = False
        self._lock = threading.Lock()

    @classmethod
    def load(cls, cache: Path) -> DirSnapshot:
        snap = cls()
        path = Path(cache) / _SNAPSHOT_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return snap
        if not isinstance(raw, dict) or int(raw.get("schema") or 0) != _SCHEMA:
            return snap
        dirs = raw.get("dirs")
        if isinstance(dirs, dict):
            clean: dict[str, dict] = {}
            for key, entry in dirs.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    mtime_ns = int(entry.get("m") or 0)
                except (TypeError, ValueError):
                    continue
                if mtime_ns <= 0:
                    continue
                kids = entry.get("kids") if isinstance(entry.get("kids"), list) else []
                files = entry.get("files") if isinstance(entry.get("files"), list) else []
                clean[str(key)] = {
                    "m": mtime_ns,
                    "kids": [str(k) for k in kids],
                    "files": [str(f) for f in files],
                }
            snap.dirs = clean
        try:
            journal = raw.get("usn_journal")
            nxt = raw.get("usn_next")
            snap.usn_journal = int(journal) if journal is not None else None
            snap.usn_next = int(nxt) if nxt is not None else None
        except (TypeError, ValueError):
            snap.usn_journal = None
            snap.usn_next = None
        return snap

    def volume_usn_unchanged(self, root: Path) -> bool:
        """True when the volume journal has not moved since the snapshot."""
        if not self.dirs or self.usn_journal is None or self.usn_next is None:
            return False
        current = query_volume_usn(root)
        if current is None:
            return False
        return current == (self.usn_journal, self.usn_next)

    def try_reuse(self, root: Path, path: Path) -> tuple[list[str], list[str]] | None:
        """Return ``(media_filenames, child_dir_names)`` when this dir is unchanged."""
        key = _rel_key(root, path)
        with self._lock:
            entry = self.dirs.get(key)
        if not entry:
            return None
        try:
            st = Path(path).stat()
        except OSError:
            return None
        if _mtime_ns(st) != int(entry["m"]):
            return None
        with self._lock:
            self.hits += 1
        return list(entry["files"]), list(entry["kids"])

    def observe(self, root: Path, path: Path, filenames: list[str], child_dirs: list[Path]) -> None:
        try:
            st = Path(path).stat()
        except OSError:
            return
        kids = [p.name for p in child_dirs]
        kids.sort(key=str.casefold)
        entry = {"m": _mtime_ns(st), "kids": kids, "files": _media_names(filenames)}
        with self._lock:
            self.dirs[_rel_key(root, path)] = entry
            self.dirty = True

    def save(self, cache: Path, root: Path) -> None:
        usn = query_volume_usn(root)
        with self._lock:
            payload = {
                "schema": _SCHEMA,
                "root": str(root),
                "usn_journal": usn[0] if usn else self.usn_journal,
                "usn_next": usn[1] if usn else self.usn_next,
                "dirs": self.dirs,
            }
        path = Path(cache) / _SNAPSHOT_NAME
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        # The snapshot write can itself advance the journal when cache and
        # library share a volume. Re-read and stamp the post-write cursor so
        # the next launch matches if nothing else changed.
        after = query_volume_usn(root)
        if after and usn and after != usn:
            payload["usn_journal"], payload["usn_next"] = after
            try:
                tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                tmp.replace(path)
                final = query_volume_usn(root)
                if final and final != after:
                    payload["usn_journal"], payload["usn_next"] = final
                    tmp.write_text(
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    tmp.replace(path)
            except OSError:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
