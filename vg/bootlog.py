# -*- coding: utf-8 -*-
"""Persistent startup / runtime log for diagnosing flash-exit on frozen builds.

每次启动在专用 ``logs/`` 目录下创建新文件，文件名使用**单调序号**作为前缀，
无前导零，按文件名前缀的数字大小排序即为历史顺序，序号越大代表越新：

    0_startup-20260823-103812-12345.log
    1_startup-20260823-110207-12923.log
    ...
    42_startup-20260824-143055-8720.log

序号是一次运行中**确定一次**的，不会因时间戳差异生成两个文件（这是旧
``startup-{time}-{pid}.log`` 命名法的已知缺陷：`log_path()` 在 ``init()``
之前被调用一次（例如 fail() 里错误弹窗展示路径），几秒后 ``init()`` 再
调用一次，两次时间戳差了几秒就出现"一次启动两个日志文件"）。

为兼容仍然按固定路径读取最新日志的脚本/工具，``init()`` 还会写一个
``logs/latest.txt``，第一行是当前会话日志文件的绝对路径；``latest.txt``
同时保存当前已使用的最高序号 ``index=N``，下次启动时 ``N+1`` 开始，
实现跨重启的单调递增。
"""
from __future__ import annotations

import os
import re
import sys
import atexit
import threading
import traceback
from datetime import datetime
from pathlib import Path

_LOG_PATH: Path | None = None
_LOG_DIR: Path | None = None
# _PATH_STAMP: init() 未跑之前，log_path() 计算好的唯一占位路径；一旦被
# 确定下来，后续 log_path() 调用必须返回同一个值，和 init() 最终写入的
# 路径完全一致。  这样 fail() 弹窗里显示路径、bootlog.write 自动 init、
# 以及正式 init() 这三条路径就只会落同一个文件。
_PATH_STAMP: Path | None = None
_INIT = False
_WRITE_LOCK = threading.RLock()
_PENDING: list[str] = []
# 启动早期在 write()/step() 可用之前（_ensure_flush_thread 未启动时）暂存
# bootlog 自诊断信息（_read_last_index / _prune_old_logs 等），一旦正式日志
# 链路就绪就把这批 DIAG 写到当前会话日志里，便于只看 startup.log 就能
# 复核"序号为什么选了 N / 为什么 latest.txt 没被覆盖"等问题。
_PENDING_DIAG: list[str] = []
_FLUSH_EVENT = threading.Event()
_FLUSH_THREAD: threading.Thread | None = None
_STOP = False
# 保留最近 N 个历史日志文件，超出按序号淘汰最旧。
_KEEP_LOG_FILES = 20
# 单文件大小上限（超出后尾部截断保留，避免无限涨）。
_MAX_LOG_BYTES = 4 * 1024 * 1024
_KEEP_LOG_BYTES = 2 * 1024 * 1024
# latest.txt 指针提交状态。只有确认本次启动会真正跑服务（mode!="reuse" 且未 fail）
# 时才 commit，否则会把 latest.txt 覆盖到"启动一秒就死掉"的空壳日志文件上，
# 造成"明明有日志却查不到 / latest 指向不存在的文件"的常见诊断死路。
_LATEST_COMMITTED = False

_INDEX_RE = re.compile(r"^(\d+)_")
_LATEST_INI_RE = re.compile(r"^index\s*=\s*(\d+)\s*$", re.MULTILINE)


def log_dir() -> Path:
    """返回日志目录（exe 旁或项目根下的 ``logs/``）。"""
    global _LOG_DIR
    if _LOG_DIR is not None:
        return _LOG_DIR
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    _LOG_DIR = base / "logs"
    return _LOG_DIR


def _read_last_index(d: Path) -> int:
    """Return the highest session index already used on disk, or -1 if none.

    Two sources are consulted, in order of trust:
      (1) ``logs/latest.txt`` which we write with a ``index=<N>`` line on
          every successful ``init()`` since this format was introduced.
      (2) A scan of all ``startup-*.log``/``*_startup-*.log`` files on disk,
          picking the maximum ``index`` embedded in the 5-digit prefix.
          Existing files that still use the legacy ``startup-time-pid.log``
          form (no prefix) are treated as index -1 so they sort to the very
          beginning and never compete with new indexed names.

    This two-source approach tolerates ``logs/latest.txt`` being manually
    deleted by the user without accidentally restarting the counter from 0.
    """
    best = -1
    latest_idx: int | None = None
    scanned_matches = 0
    latest = d / "latest.txt"
    if latest.is_file():
        try:
            txt = latest.read_text(encoding="utf-8", errors="replace")
            m = _LATEST_INI_RE.search(txt)
            if m:
                latest_idx = int(m.group(1))
                best = max(best, latest_idx)
        except OSError:
            pass
    scanned_files: list[tuple[int, str]] = []
    if d.is_dir():
        for p in d.glob("*.log"):
            m = _INDEX_RE.match(p.name)
            if m:
                scanned_matches += 1
                try:
                    parsed = int(m.group(1))
                    best = max(best, parsed)
                    scanned_files.append((parsed, p.name))
                except ValueError:
                    continue
    # 启动早期 stdout 会被 app.py 重定向到 _stdout_startup_*.log，
    # 所以用 print 留下索引扫描的审计痕迹，方便反查"为什么这次序号跳回 1"。
    # 同时也在 bootlog runtime 缓冲区里留一份（pending → 真正日志文件），
    # 这样诊断时只看 logs/*.log 就能看到完整索引扫描过程。
    scan_preview = ";".join(
        f"{i}:{n}" for i, n in sorted(scanned_files, reverse=True)[:10]
    )
    _PENDING_DIAG.append(
        f"[bootlog:index_scan] log_dir={d} latest_index={latest_idx} "
        f"disk_indexed_matches={scanned_matches} best={best} next={best + 1} "
        f"top_files(10)={scan_preview!r}"
    )
    print(
        f"[bootlog:index_scan] log_dir={d} latest_index={latest_idx} "
        f"disk_indexed_matches={scanned_matches} best={best} next={best + 1}",
        flush=True,
    )
    return best


def _index_prefix(idx: int) -> str:
    """Plain index prefix without zero-padding."""
    return str(idx)


def _log_stem_for_index(idx: int) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{_index_prefix(idx)}_startup-{stamp}-{os.getpid()}"


def _determine_session_path(d: Path) -> Path:
    """Pick the exact path the current session will use.

    The result is memoised into ``_PATH_STAMP`` / ``_LOG_PATH`` so every
    caller during the lifetime of the process (``log_path()`` lookup,
    error-popups, ``init()``, ``write()`` auto-init) agrees on the exact
    same filename.  Collision with an existing file is resolved by bumping
    the index until a free slot is found.
    """
    global _PATH_STAMP
    if _PATH_STAMP is not None:
        return _PATH_STAMP
    idx0 = _read_last_index(d) + 1
    idx = idx0
    # Loop just in case two independent processes raced + generated the same
    # index via latest.txt window (shouldn't happen for this local GUI app,
    # but defensive).
    while True:
        path = d / f"{_log_stem_for_index(idx)}.log"
        if not path.exists():
            _PATH_STAMP = path
            return path
        idx += 1
        if idx - idx0 > 1000:
            # Extremely unlikely fallback: do not loop forever if index
            # tracking is broken.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = d / f"{_log_stem_for_index(idx0)}-dup-{stamp}.log"
            _PATH_STAMP = path
            return path


def log_path() -> Path:
    """当前会话日志文件路径。未 ``init`` 前返回与未来 ``init()`` 相同的路径。

    该方法返回值在进程生命周期内唯一确定一次（见 ``_determine_session_path``
    + ``_PATH_STAMP``），因此在启动早期（``init()`` 之前）被多次调用（例如
    错误弹窗展示路径、``util.py`` 调 ``bootlog.write`` 触发自动 init）、以及
    随后的正式 ``init()`` 之间，**不会出现一次启动落两个文件**的问题。
    """
    global _PATH_STAMP
    if _LOG_PATH is not None:
        return _LOG_PATH
    if _PATH_STAMP is not None:
        return _PATH_STAMP
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path(os.getcwd()) / "logs"
        d.mkdir(parents=True, exist_ok=True)
    return _determine_session_path(d)


def _migrate_legacy_log() -> None:
    """把项目根 / exe 旁旧的 ``startup.log`` 搬进 ``logs/`` 目录。

    升级到分文件日志后，老的固定路径文件不再写入；如果它仍然存在，
    就改名归档到 ``logs/`` 下，避免用户去老路径找不到新日志。
    """
    if getattr(sys, "frozen", False):
        legacy = Path(sys.executable).resolve().parent / "startup.log"
    else:
        legacy = Path(__file__).resolve().parent.parent / "startup.log"
    try:
        if not legacy.exists() or legacy.stat().st_size == 0:
            return
    except OSError:
        return
    target_dir = log_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Legacy 文件直接按 0 前缀，后续正式 init() 会从它扫出 index=0
    # 然后从 1 开始。这样新格式命名风格一开始就统一。
    target = target_dir / f"{_index_prefix(0)}_startup-legacy-{stamp}.log"
    i = 0
    while target.exists():
        i += 1
        target = target_dir / f"{_index_prefix(i)}_startup-legacy-{stamp}.log"
    try:
        legacy.replace(target)
    except OSError:
        # 跨卷或权限问题：尝试读后写
        try:
            target.write_bytes(legacy.read_bytes())
            legacy.unlink()
        except Exception:
            pass


def _prune_old_logs() -> None:
    """保留最近 ``_KEEP_LOG_FILES`` 个会话日志，按序号淘汰最旧。

    新格式按文件名前缀序号排序（稳定、不依赖文件系统 mtime 精度）；
    旧无序号文件全部视为 index=-1 排序在最前面，优先被淘汰。
    """
    try:
        d = log_dir()
        if not d.is_dir():
            return
        files = [p for p in d.glob("*.log") if p.is_file()]
        # Keep only startup logs (skip explicit legacy files too but they
        # still follow index order so are fine too).
        if len(files) <= _KEEP_LOG_FILES:
            return
        def _sort_key(p: Path):
            m = _INDEX_RE.match(p.name)
            return int(m.group(1)) if m else -1, p.name
        files.sort(key=_sort_key, reverse=True)
        removed = files[_KEEP_LOG_FILES:]
        removed_names: list[str] = []
        for p in removed:
            try:
                removed_names.append(f"{p.name}({p.stat().st_size}B)")
                p.unlink()
            except OSError:
                pass
        if removed_names:
            _PENDING_DIAG.append(
                f"[bootlog:old_logs_cleaned] kept={_KEEP_LOG_FILES} "
                f"removed_count={len(removed_names)} removed={';'.join(removed_names[:30])}"
            )
    except Exception:
        pass


def _write_latest_pointer(current_index: int) -> None:
    """在 ``logs/latest.txt`` 写当前会话路径 + 已用最高序号。

    第一行是日志路径（保持和旧约定一致），后续以 ``key=value`` 形式存
    元信息，目前只写 ``index=<最大序号>``。
    """
    try:
        ptr = log_dir() / "latest.txt"
        lines = [f"{_LOG_PATH}\n", f"index={current_index}\n"]
        ptr.write_text("".join(lines), encoding="utf-8")
    except Exception:
        pass


def _extract_index_from_name(name: str) -> int:
    m = _INDEX_RE.match(name)
    return int(m.group(1)) if m else -1


def init(reset: bool = False) -> Path:
    """初始化日志：每次启动在 ``logs/`` 下创建新文件。

    ``reset=True`` 目前保留占位符语义（不在磁盘上重置历史）；新增的
    唯一公开效果是：如果之前因为调用链上有人早早 ``write()`` 触发了
    自动 init，这次显式调用仍会无条件地把 session header（========
    session ========）重新写入一遍，方便在日志里看到"这是启动阶段的头"。
    """
    global _INIT, _LOG_PATH, _PATH_STAMP, _LATEST_COMMITTED
    if _INIT and _LOG_PATH is not None and not reset:
        # 注意：多次 init(reset=False) 不再重写 latest.txt。
        # latest.txt 只能由显式的 commit_latest_pointer() 写入，
        # 这样"启动时探测到端口已被占 → reuse_instance sys.exit(0)"就不会
        # 把真正还在跑服务的旧日志路径覆盖掉。
        return _LOG_PATH
    try:
        d = log_dir()
        before = d.is_dir()
        d.mkdir(parents=True, exist_ok=True)
        after = d.is_dir()
        _PENDING_DIAG.append(
            f"[bootlog:logs_dir_created] path={d} existed_before={before} "
            f"ready_after={after}"
        )
    except Exception:
        d = Path(os.getcwd()) / "logs"
        d.mkdir(parents=True, exist_ok=True)
        _PENDING_DIAG.append(
            f"[bootlog:logs_dir_created] path={d} fallback=cwd"
        )
    # 迁移老 startup.log（如果存在）
    _migrate_legacy_log()
    # 为本会话确定路径 —— 注意这一步使用 _PATH_STAMP 缓存，和早于 init()
    # 的 log_path() / write() 调用链上的值完全一致，不会生成两文件。
    session_path = _determine_session_path(d)
    _LOG_PATH = session_path
    current_index = _extract_index_from_name(_LOG_PATH.name)
    _PENDING_DIAG.append(
        f"[bootlog:log_index_allocated] session_path={session_path} "
        f"session_index={current_index} commit_later=True"
    )
    # 启动早期 stdout 审计：记录本次选择的会话路径+序号，便于将来诊断
    # "为什么 latest.txt 指向的不是这条"（只有 commit 成功后才会真写 latest）。
    print(
        f"[bootlog:session_chosen] session_path={session_path} "
        f"session_index={current_index} commit_later=True",
        flush=True,
    )
    try:
        # Truncate the file if it already exists (should only happen if the
        # user ran two processes in a race and we resolved collision).
        with _LOG_PATH.open("w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
    _prune_old_logs()
    _INIT = True
    _ensure_flush_thread()
    write("", urgent=True)
    write("======== session ========", urgent=True)
    write(f"time={datetime.now().isoformat(timespec='seconds')}", urgent=True)
    write(f"frozen={bool(getattr(sys, 'frozen', False))}", urgent=True)
    write(f"exe={sys.executable!r}", urgent=True)
    write(f"argv={sys.argv!r}", urgent=True)
    write(f"cwd={os.getcwd()!r}", urgent=True)
    write(f"pid={os.getpid()}", urgent=True)
    write(f"python={sys.version.split()[0]} platform={sys.platform}", urgent=True)
    write(f"log_file={_LOG_PATH}", urgent=True)
    write(f"log_index={current_index}", urgent=True)
    # 启动标记：便于在日志里一眼确认"日志系统正式开始工作"。
    step("boot_begin", f"index={current_index} path={_LOG_PATH}")
    # 把启动早期暂存的 bootlog 自诊断（index_scan / logs_dir_created /
    # log_index_allocated / old_logs_cleaned 等）刷入当前会话日志，
    # 这样诊断时只看 startup.log 就足够了。
    pending_diag_snapshot: list[str] = []
    with _WRITE_LOCK:
        if _PENDING_DIAG:
            pending_diag_snapshot = list(_PENDING_DIAG)
            _PENDING_DIAG.clear()
    for diag_line in pending_diag_snapshot:
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            formatted = f"[{ts}] [DIAG] {diag_line}\n"
            with _WRITE_LOCK:
                with _LOG_PATH.open("a", encoding="utf-8", errors="replace") as f:
                    f.write(formatted)
                    f.flush()
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        write(f"_MEIPASS={meipass!r}", urgent=True)
        internal = Path(sys.executable).resolve().parent / "_internal"
        write(f"_internal_exists={internal.is_dir()}", urgent=True)
    return _LOG_PATH


def _append_lines(lines: list[str], *, sync: bool) -> None:
    if not lines:
        return
    if _LOG_PATH is None:
        init(reset=False)
    with _WRITE_LOCK:
        with _LOG_PATH.open("a", encoding="utf-8", errors="replace") as f:
            f.write("".join(lines))
            f.flush()
            if sync:
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass


def flush(*, sync: bool = False) -> None:
    with _WRITE_LOCK:
        if not _PENDING:
            return
        lines = list(_PENDING)
        _PENDING.clear()
    try:
        _append_lines(lines, sync=sync)
    except Exception:
        pass


def _flush_worker() -> None:
    while not _STOP:
        _FLUSH_EVENT.wait(0.5)
        _FLUSH_EVENT.clear()
        flush(sync=False)
    flush(sync=True)


def _ensure_flush_thread() -> None:
    global _FLUSH_THREAD
    if _FLUSH_THREAD and _FLUSH_THREAD.is_alive():
        return
    with _WRITE_LOCK:
        if _FLUSH_THREAD and _FLUSH_THREAD.is_alive():
            return
        _FLUSH_THREAD = threading.Thread(
            target=_flush_worker,
            daemon=True,
            name="runtime-log-writer",
        )
        _FLUSH_THREAD.start()


def write(msg: str, *, urgent: bool = False) -> None:
    """Queue normal lines; errors/startup markers are durably flushed now."""
    try:
        if not _INIT:
            init(reset=False)
        line = str(msg).rstrip("\n")
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{ts}] {line}\n"
        if urgent:
            flush(sync=False)
            _append_lines([formatted], sync=True)
            return
        with _WRITE_LOCK:
            _PENDING.append(formatted)
            should_flush = len(_PENDING) >= 64
        _ensure_flush_thread()
        if should_flush:
            _FLUSH_EVENT.set()
    except Exception:
        pass


def shutdown() -> None:
    global _STOP
    _STOP = True
    _FLUSH_EVENT.set()
    flush(sync=True)


atexit.register(shutdown)


def step(name: str, detail: str = "") -> None:
    if detail:
        write(f"STEP {name}: {detail}")
    else:
        write(f"STEP {name}")


def commit_latest_pointer(mark_index: int | None = None) -> None:
    """显式把 ``logs/latest.txt`` 指向当前会话。

    必须在真正启动服务（端口已选择成功、不再会走 ``reuse_instance``、
    ``fail()`` 等提前退出路径）之后才调用。若本次启动最终没有 serve 任何
    服务（例如端口被占用直接复用现有实例），则跳过本函数调用即可，
    latest.txt 会**保持指向旧的、真正在跑服务**的日志，避免诊断时踩坑。
    """
    global _LATEST_COMMITTED
    try:
        if _LOG_PATH is None:
            init(reset=False)
        if _LOG_PATH is None:
            return
        if mark_index is None:
            mark_index = _extract_index_from_name(_LOG_PATH.name)
        if mark_index < 0:
            mark_index = 0
        _write_latest_pointer(mark_index)
        _LATEST_COMMITTED = True
        # 运行时 DIAG（write 链路已就绪）：直接写入日志 + 同步刷盘。
        write(
            f"[bootlog:commit_latest_pointer] index={mark_index} path={_LOG_PATH}",
            urgent=True,
        )
        # 留在 startup 日志里：诊断时能直接对应到"写 latest.txt 的精确时刻"。
        step(
            "latest_pointer_committed",
            f"index={mark_index} path={_LOG_PATH}",
        )
    except Exception:
        try:
            exception("LATEST_POINTER_COMMIT_FAILED")
        except Exception:
            pass


def abort_skip_commit(reason: str) -> None:
    """声明：本次启动不会真正跑服务，latest.txt 保持旧值不变。

    只在当前启动日志里留一条 STEP，便于事后核对；不实际写任何文件，
    也不更改 latest.txt 指针。典型调用点：main.py 里
    ``choose_listen_port`` 返回 ``mode == "reuse"`` → 直接 sys.exit(0)
    之前。
    """
    try:
        if _LOG_PATH is None:
            return
        reason_text = (reason or "").strip() or "unknown_reason"
        # 运行时 DIAG：abort 一定发生在 write 链路 ready 之后（main.py choose_listen_port）。
        write(f"[bootlog:abort_skip_commit] reason={reason_text}", urgent=True)
        step(
            "latest_pointer_skipped",
            reason_text,
        )
    except Exception:
        pass


def fail(msg: str, detail: str = "") -> None:
    write(f"FAIL: {msg}", urgent=True)
    if detail:
        for line in str(detail).splitlines() or [detail]:
            write(f"  {line}", urgent=True)


def exception(prefix: str = "EXCEPTION") -> None:
    write(f"{prefix}:", urgent=True)
    for line in traceback.format_exc().splitlines():
        write(f"  {line}", urgent=True)
