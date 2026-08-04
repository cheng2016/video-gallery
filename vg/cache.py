# -*- coding: utf-8 -*-
"""Encrypted thumb vault and index persistence."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from vg.config import (
    INDEX_NAME,
    KEY_FILE,
    LEGACY_DISK_CACHE_NAMES,
    THUMB_EXT,
    THUMB_JPEG_CACHE_MAX,
    VGDATA_DIR,
)
from vg.state import STATE, _thumb_jpeg_cache, _thumb_jpeg_lock
from vg.util import _clear_path_attrs_windows, log

def thumb_cache_get(vid: str) -> bytes | None:
    with _thumb_jpeg_lock:
        raw = _thumb_jpeg_cache.get(vid)
        if raw is not None:
            _thumb_jpeg_cache.move_to_end(vid)
        return raw


def thumb_cache_put(vid: str, raw: bytes) -> None:
    with _thumb_jpeg_lock:
        _thumb_jpeg_cache[vid] = raw
        _thumb_jpeg_cache.move_to_end(vid)
        while len(_thumb_jpeg_cache) > THUMB_JPEG_CACHE_MAX:
            _thumb_jpeg_cache.popitem(last=False)


def thumb_cache_invalidate(vid: str | None = None) -> None:
    with _thumb_jpeg_lock:
        if vid:
            _thumb_jpeg_cache.pop(vid, None)
        else:
            _thumb_jpeg_cache.clear()


def _ensure_vault_key() -> bytes:
    VGDATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        _clear_path_attrs_windows(KEY_FILE)
        try:
            key = KEY_FILE.read_bytes()
            if len(key) >= 32:
                return key[:32]
        except OSError as e:
            log(f"[预览图] 读取密钥失败: {e}，将重新生成（旧预览图会失效）")
    key = os.urandom(32)
    try:
        _clear_path_attrs_windows(KEY_FILE)
        KEY_FILE.write_bytes(key)
    except OSError as e:
        log(f"[预览图] 写入密钥失败: {e}")
        raise
    return key


def _xor_stream(key: bytes, nonce: bytes, length: int) -> bytes:
    seed = hashlib.sha256(key + nonce).digest()
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_blob_with_key(data: bytes, key: bytes) -> bytes:
    """用指定密钥加密（VG1 + nonce + ciphertext）。"""
    nonce = os.urandom(16)
    stream = _xor_stream(key[:32], nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    return b"VG1\0" + nonce + cipher


def encrypt_blob(data: bytes) -> bytes:
    """本地预览图加密：VG1 + nonce + ciphertext（无密钥无法当图片打开）。"""
    return encrypt_blob_with_key(data, _ensure_vault_key())


def decrypt_blob_with_key(blob: bytes, key: bytes) -> bytes | None:
    if not blob.startswith(b"VG1\0") or len(blob) < 20:
        return None
    nonce = blob[4:20]
    cipher = blob[20:]
    stream = _xor_stream(key[:32], nonce, len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, stream))


def decrypt_blob(blob: bytes) -> bytes | None:
    if not blob.startswith(b"VG1\0") or len(blob) < 20:
        return None
    return decrypt_blob_with_key(blob, _ensure_vault_key())


def thumb_path(cache: Path, vid: str) -> Path:
    return cache / f"{vid}{THUMB_EXT}"


def thumb_version(cache: Path | None, vid: str) -> int:
    """用于前端缓存破坏；有有效文件则返回 mtime。"""
    if not cache:
        return 0
    p = thumb_path(cache, vid)
    try:
        if p.exists() and p.stat().st_size > 24:
            return int(p.stat().st_mtime)
    except OSError:
        pass
    return 0


def thumb_file_ready(cache: Path | None, vid: str) -> bool:
    """只检查文件是否存在且非空，不解密（扫描/列表用，更快）。"""
    if not cache or not vid:
        return False
    p = thumb_path(cache, vid)
    try:
        return p.exists() and p.stat().st_size > 24
    except OSError:
        return False


def read_thumb_jpeg(cache: Path, vid: str) -> bytes | None:
    """读取并解密预览图；带内存 LRU。"""
    cached = thumb_cache_get(vid)
    if cached is not None:
        return cached
    p = thumb_path(cache, vid)
    try:
        if not p.exists() or p.stat().st_size <= 24:
            return None
        _clear_path_attrs_windows(p)
        raw = decrypt_blob(p.read_bytes())
        if raw and len(raw) > 100 and raw[:2] == b"\xff\xd8":
            thumb_cache_put(vid, raw)
            return raw
    except OSError as e:
        log(f"[预览图] 读取失败 {vid}: {e}")
    return None


def has_encrypted_thumb(cache: Path, vid: str) -> bool:
    """服务端校验：优先内存缓存，否则快速文件探测，必要时再解密。"""
    if thumb_cache_get(vid) is not None:
        return True
    if not thumb_file_ready(cache, vid):
        return False
    return read_thumb_jpeg(cache, vid) is not None


def ensure_cache_dir(root: Path) -> Path:
    """缓存固定：程序根目录/preview_cache/<盘符标识>/（绝不写到视频盘根目录）。"""
    VGDATA_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_vault_key()
    cleanup_legacy_disk_cache(root)
    # 用盘符字母作子目录名，方便辨认（如 E、D）；整路径再哈希兜底
    try:
        drive = root.resolve().drive.rstrip(":\\/") or "disk"
    except OSError:
        drive = "disk"
    safe = re.sub(r"[^\w\-]+", "_", drive)[:16] or "disk"
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:8]
    cache = VGDATA_DIR / f"{safe}_{digest}"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def cleanup_legacy_disk_cache(root: Path) -> None:
    """删除早期版本误写在视频盘根目录的缓存文件夹（可安全删）。"""
    if not root:
        return
    for name in LEGACY_DISK_CACHE_NAMES:
        p = root / name
        try:
            if not p.is_dir():
                continue
        except OSError:
            continue
        try:
            _clear_path_attrs_windows(p)
            shutil.rmtree(p)
            log(f"[清理] 已删除旧版盘根缓存（现已改到程序目录 preview_cache）: {p}")
        except OSError as e:
            log(f"[清理] 删不掉旧缓存 {p}: {e}（可在资源管理器里手动删除）")


def save_index(cache: Path, root: Path, videos: list[dict]) -> None:
    path = cache / INDEX_NAME
    tmp = cache / (INDEX_NAME + ".tmp")
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _clear_path_attrs_windows(path)
        _clear_path_attrs_windows(tmp)
        # 去掉运行期字段，减小索引体积
        clean = []
        for v in videos:
            if "_q" in v:
                clean.append({k: val for k, val in v.items() if k != "_q"})
            else:
                clean.append(v)
        payload = json.dumps(
            {"root": str(root), "videos": clean, "updated": datetime.now().isoformat()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"提示: 保存索引失败: {e}")
        print(f"       路径: {path}")
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except OSError:
            pass

def attach_thumb_meta(v: dict) -> dict:
    """给列表项补 has_thumb / thumb_v（只看文件是否存在，避免列表接口解密过慢）。"""
    cache = STATE.get("cache_dir")
    vid = v.get("id") or ""
    if cache and vid and (thumb_cache_get(vid) is not None or thumb_file_ready(cache, vid)):
        v["has_thumb"] = True
        v["thumb_v"] = thumb_version(cache, vid) or 1
        return v
    v["has_thumb"] = False
    v["thumb_v"] = 0
    return v

