"""case_index：M3 本地 case 库索引（契约 §14，ARCH §4.7，DEVELOPMENT_PLAN R42）。

**派生层定位**（[P§5.5]）：NDJSON/artifact（run 目录）是事实源，本 SQLite 索引可随时
经 `POST /api/cases/rebuild`（或 `python -m services.backend.rebuild_index`）从事实源
全量重建；索引与事实源短暂不一致属设计内（rebuild 修复）。

- 索引文件：`services/backend/data/cases.db`（Settings.cases_db_path 可覆盖，测试隔离）；
  连接即 `PRAGMA journal_mode=WAL`（读写并发：UI 浏览列表时后台登记/重建不阻塞读）；
  目录不存在自动创建。
- 表（ARCH §4.7 草案落定）：`cases(run_id PRIMARY KEY, case_name, device, package_name,
  created_at, summary_available, stats_json, path, updated_at)`；`stats_json` 存 §14
  统计快照，与 statistics_service 同源计算（build_case_stats_snapshot）。
- 登记时机：save 成功自动登记（session_manager 调 register_run，失败不阻断 save，
  rebuild 兜底）+ rebuild 全量重建（含 [R§2.12] 兼容导入的旧 run 目录）。
- GET 展示过滤：事实源已消失（run 目录被手动删除）的索引行不展示（契约 §14），
  rebuild 时物理清除。
- 并发：SQLite 单写者，写操作经进程内锁串行化（ARCH §4.7，M1 单进程假设内够用）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..errors import ApiError
from .statistics_service import (
    CASE_STATS_KEYS,
    build_case_stats_snapshot,
)

logger = logging.getLogger("pd-backend.case-index")

#: §14 sort 白名单：createdAt + 六个统计快照键（契约 §14 排序参数）
SORT_KEYS: tuple[str, ...] = ("createdAt", *CASE_STATS_KEYS)

MAX_LIMIT = 200  # 契约 §14 分页上限（limit 1–200，缺省 50）
DEFAULT_LIMIT = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cases (
    run_id TEXT PRIMARY KEY,
    case_name TEXT NOT NULL,
    device TEXT,
    package_name TEXT,
    created_at INTEGER,
    summary_available INTEGER NOT NULL DEFAULT 0,
    stats_json TEXT,
    path TEXT,
    updated_at INTEGER NOT NULL
);
"""

_COLUMNS = (
    "run_id",
    "case_name",
    "device",
    "package_name",
    "created_at",
    "summary_available",
    "stats_json",
    "path",
    "updated_at",
)


class CaseIndex:
    """SQLite WAL case 索引（契约 §14）。线程安全：写经进程内锁串行化。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._write_lock = threading.Lock()
        self._ensure_schema()

    # ---- 连接与 schema ----

    def _connect(self) -> sqlite3.Connection:
        """打开连接（每次连接都自愈损坏文件：契约 §14「索引文件损坏时以新建空库
        恢复」——恢复不能只在 __init__ 做，长驻进程里 db 文件可能中途被写坏）。"""
        try:
            return self._open()
        except sqlite3.DatabaseError as exc:
            if not self._is_corruption(exc):
                raise
            logger.warning("case 索引损坏（%s），重建空库：%s", exc, self.db_path)
            self._wipe_db_files()
            return self._open()

    def _open(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # ARCH §4.7：读写并发
            conn.execute("PRAGMA busy_timeout=5000")
            # IF NOT EXISTS：每个连接自足——自愈出的空库与首次建库同路径，杜绝
            # 「表已被 wipe、后续写无表」的中间态
            conn.executescript(_SCHEMA_SQL)
            return conn
        except BaseException:
            conn.close()
            raise

    @contextmanager
    def _write_conn(self) -> Iterator[sqlite3.Connection]:
        """写连接：进程内锁串行化 + 单事务（提交/回滚）+ 用毕关闭。"""
        with self._write_lock:
            conn = self._connect()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    @contextmanager
    def _read_conn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """__init__ 即建库建表（文件与 schema 立即可见）；_open 已含 IF NOT EXISTS。"""
        conn = self._connect()
        conn.close()

    @staticmethod
    def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
        text = str(exc).lower()
        return "not a database" in text or "malformed" in text or "encrypted" in text

    def _wipe_db_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.db_path) + suffix)
            candidate.unlink(missing_ok=True)

    # ---- 写 ----

    def upsert(self, row: dict[str, Any]) -> None:
        """登记/更新一行（save 自动登记与 rebuild 共用）。"""
        with self._write_conn() as conn:
            self._upsert_locked(conn, row)

    def delete(self, run_id: str) -> bool:
        """删索引行；行不存在返回 False（调用方转 404 report_not_found）。"""
        with self._write_conn() as conn:
            cursor = conn.execute("DELETE FROM cases WHERE run_id = ?", (run_id,))
            return cursor.rowcount > 0

    def rename(self, run_id: str, case_name: str) -> None:
        with self._write_conn() as conn:
            conn.execute(
                "UPDATE cases SET case_name = ?, updated_at = ? WHERE run_id = ?",
                (case_name, int(datetime.now().timestamp() * 1000), run_id),
            )

    def replace_all(self, rows: list[dict[str, Any]]) -> None:
        """rebuild 专用：全量登记 + 物理清除事实源已消失的旧行（契约 §14）。"""
        keep_ids = [row["runId"] for row in rows]
        with self._write_conn() as conn:
            for row in rows:
                self._upsert_locked(conn, row)
            if keep_ids:
                placeholders = ", ".join("?" for _ in keep_ids)
                conn.execute(
                    f"DELETE FROM cases WHERE run_id NOT IN ({placeholders})",
                    tuple(keep_ids),
                )
            else:
                conn.execute("DELETE FROM cases")

    def _upsert_locked(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        now_ms = int(datetime.now().timestamp() * 1000)
        conn.execute(
            f"""
            INSERT INTO cases ({", ".join(_COLUMNS)})
            VALUES ({", ".join("?" for _ in _COLUMNS)})
            ON CONFLICT(run_id) DO UPDATE SET
                case_name=excluded.case_name,
                device=excluded.device,
                package_name=excluded.package_name,
                created_at=excluded.created_at,
                summary_available=excluded.summary_available,
                stats_json=excluded.stats_json,
                path=excluded.path,
                updated_at=excluded.updated_at
            """,
            (
                row["runId"],
                row["caseName"],
                row.get("device"),
                row.get("packageName"),
                row.get("createdAt"),
                1 if row.get("summaryAvailable") else 0,
                json.dumps(row.get("stats") or {}, ensure_ascii=False, sort_keys=True),
                row.get("path"),
                now_ms,
            ),
        )

    # ---- 读 ----

    def get(self, run_id: str) -> dict[str, Any] | None:
        columns = ", ".join(_COLUMNS)
        with self._read_conn() as conn:
            row = conn.execute(
                f"SELECT {columns} FROM cases WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row_to_case(row) if row is not None else None

    def all_rows(self) -> list[dict[str, Any]]:
        """全量行（GET /api/cases 的筛选/排序在此之上做，见 list_cases）。"""
        columns = ", ".join(_COLUMNS)
        with self._read_conn() as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM cases ORDER BY created_at DESC, run_id ASC"
            ).fetchall()
        return [_row_to_case(row) for row in rows]

    def count(self) -> int:
        with self._read_conn() as conn:
            (total,) = conn.execute("SELECT COUNT(*) FROM cases").fetchone()
        return int(total)


# ---- 行装配（事实源 → 索引行；save 自动登记与 rebuild 共用）----


def run_id_of(run_path: Path) -> str:
    """run 目录名 → runId（`run-<caseName>`，契约 §1/§14）。"""
    return f"run-{run_path.name}"


def build_case_row(runs_dir: Path, run_path: Path) -> dict[str, Any]:
    """run 目录（事实源）→ §14 索引行：manifest 字段 + statistics_service 同源快照。

    [R§2.12] 兼容导入：旧 run 目录（perfeval 落盘）manifest 形状不同——device 为
    Mapping（deviceId/serialOrUdid）、包名在 app.packageName、时间用 createdAtUtc
    ISO——此处归一为标量（设备标识/包名/epoch 毫秒）；新后端落盘的 manifest 直接命中。
    """
    manifest = _read_json_dict(run_path / "manifest.json")
    dir_name = run_path.name
    case_name = manifest.get("caseName")
    if not isinstance(case_name, str) or not case_name:
        case_name = dir_name
    device = _scalar_from(manifest.get("device"), ("deviceId", "serialOrUdid", "serial"))
    package_name = _scalar_from(manifest.get("packageName"), ())
    if package_name is None:
        package_name = _scalar_from(
            manifest.get("app"), ("packageName", "appId", "bundleId")
        )
    created_at = manifest.get("createdAt")
    if not isinstance(created_at, int) or isinstance(created_at, bool) or created_at <= 0:
        created_at = _created_at_from_legacy(manifest) or int(run_path.stat().st_mtime * 1000)
    run_id = run_id_of(run_path)
    return {
        "runId": run_id,
        "caseName": case_name,
        "device": device,
        "packageName": package_name,
        "createdAt": created_at,
        "summaryAvailable": (run_path / "summary.json").is_file(),
        # 与 §12 统计层同源（ARCH §4.7 单一事实源约束）；统计失败不阻塞登记，
        # 快照留空由 rebuild 重算兜底
        "stats": build_case_stats_snapshot(runs_dir, run_id),
        "path": str(run_path.resolve()),
    }


def _scalar_from(value: Any, keys: tuple[str, ...]) -> str | None:
    """manifest 字段归一：字符串原样；Mapping 取第一个非空键；其余 None。"""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in keys:
            nested = value.get(key)
            if isinstance(nested, str) and nested:
                return nested
    return None


def _created_at_from_legacy(manifest: dict[str, Any]) -> int | None:
    """旧 manifest 的 createdAtUtc（ISO 8601）→ epoch 毫秒；解析失败 None。"""
    raw = manifest.get("createdAtUtc")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return document if isinstance(document, dict) else {}


# ---- GET /api/cases 装配（筛选/排序/分页；契约 §14）----


def list_cases(
    index: CaseIndex,
    *,
    query: str | None = None,
    device: str | None = None,
    sort: str = "createdAt",
    order: str = "desc",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """索引行 → 用例表响应：筛选 + 排序（null 恒排末尾）+ 分页 + total。

    事实源已消失（run 目录被手动删除）的行不展示、不计入 total（契约 §14）。
    """
    if sort not in SORT_KEYS:
        raise ApiError(
            "validation_error",
            f"sort 必须是 {'|'.join(SORT_KEYS)} 之一，收到 {sort!r}",
            details={"field": "sort"},
        )
    if order not in ("asc", "desc"):
        raise ApiError(
            "validation_error",
            f"order 必须是 asc|desc，收到 {order!r}",
            details={"field": "order"},
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ApiError(
            "validation_error",
            f"limit 必须是 1–{MAX_LIMIT} 的整数",
            details={"field": "limit"},
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ApiError(
            "validation_error",
            "offset 必须是非负整数",
            details={"field": "offset"},
        )

    rows = [row for row in index.all_rows() if row["path"] and Path(row["path"]).is_dir()]
    if query:
        needle = query.lower()
        rows = [row for row in rows if needle in str(row["caseName"]).lower()]
    if device is not None:
        rows = [row for row in rows if row["device"] == device]

    # 排序：createdAt 直取行值；统计键取 stats 快照（登记/重建时刻冻结值）。
    # null 值恒排末尾（asc/desc 一致，契约 §14）；次序稳定基线为 SQL 的
    # created_at DESC, run_id ASC（稳定排序保持平手时序）。
    reverse = order == "desc"

    def order_key(row: dict[str, Any]) -> tuple[int, float]:
        if sort == "createdAt":
            value = row["createdAt"]
        else:
            value = (row.get("stats") or {}).get(sort)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return (1, 0.0)
        return (0, -float(value) if reverse else float(value))

    rows.sort(key=order_key)
    page = rows[offset : offset + limit]
    return {"cases": page, "total": len(rows)}


# ---- rebuild（契约 §14 POST /api/cases/rebuild + CLI 入口共用）----


def rebuild_case_index(runs_dir: Path, index: CaseIndex) -> int:
    """扫描 runs 根目录全部 run 目录，按事实源重建索引；返回重建后的行数。

    - 含 [R§2.12] 兼容导入的旧 run 目录（无 manifest/形状不同亦登记）；
    - 只认**run 目录**：含任一事实源文件（manifest/samples/timeline/summary）的
      一级子目录；runs 根下的 run-group 容器目录（perfeval 多轮布局，只含子目录）
      不是 run，不登记（实测 runs/synthetic-validation 即此形态）；
    - 事实源已消失的旧行物理清除（replace_all）；
    - 幂等：同事实源重复执行结果一致；
    - 单个目录装配失败（如统计层异常）记日志跳过，不中断整体重建。
    """
    rows: list[dict[str, Any]] = []
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith(".") or not _is_run_dir(entry):
                continue
            try:
                rows.append(build_case_row(runs_dir, entry))
            except Exception as exc:  # noqa: BLE001 —— 单目录失败不中断重建
                logger.warning("rebuild 跳过 run 目录 %s：%s", entry.name, exc)
    index.replace_all(rows)
    logger.info("case 索引重建完成：%d 条（%s）", len(rows), index.db_path)
    return len(rows)


_RUN_FACT_FILES: tuple[str, ...] = (
    "manifest.json",
    "samples.ndjson",
    "timeline.ndjson",
    "summary.json",
)


def _is_run_dir(entry: Path) -> bool:
    return any((entry / name).is_file() for name in _RUN_FACT_FILES)


# ---- §9 runs 轻索引（GET /api/runs 与 GET /api/runs/{runId}；M8+ MCP 报告获取落点）----
#
# 契约 §14 注：「GET /api/runs（§9）是落盘目录的实时轻索引；/api/cases 是带统计
# 快照、筛选排序、分页的用例表」——因此本节不走 SQLite 索引，每次调用直接枚举
# runs 目录（事实源），不触碰统计层（轻）。


def iter_run_dirs(runs_dir: Path) -> list[Path]:
    """runs 根目录下的 run 目录清单（事实源实时枚举；run-group 容器目录不算）。"""
    if not runs_dir.is_dir():
        return []
    return sorted(
        (
            entry for entry in runs_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith(".") and _is_run_dir(entry)
        ),
        key=lambda path: path.name,
    )


def build_run_row(run_path: Path) -> dict[str, Any]:
    """§9 轻索引行：manifest 字段归一（[R§2.12] 兼容导入同 build_case_row 口径），
    不含统计快照。``device`` 为 manifest 的设备标识（device_id 字符串，§14 行同源）。"""
    manifest = _read_json_dict(run_path / "manifest.json")
    case_name = manifest.get("caseName")
    if not isinstance(case_name, str) or not case_name:
        case_name = run_path.name
    device = _scalar_from(manifest.get("device"), ("deviceId", "serialOrUdid", "serial"))
    package_name = manifest.get("packageName")
    if not isinstance(package_name, str) or not package_name:
        package_name = _scalar_from(manifest.get("app"), ("packageName", "appId", "bundleId"))
    created_at = manifest.get("createdAt")
    if not isinstance(created_at, int) or isinstance(created_at, bool) or created_at <= 0:
        created_at = _created_at_from_legacy(manifest) or int(run_path.stat().st_mtime * 1000)
    return {
        "runId": run_id_of(run_path),
        "caseName": case_name,
        "createdAt": created_at,
        "device": device,
        "packageName": package_name,
        "summaryAvailable": (run_path / "summary.json").is_file(),
        "path": str(run_path.resolve()),
    }


def list_runs(runs_dir: Path) -> dict[str, Any]:
    """契约 §9 `GET /api/runs`：{"runs": [轻索引行]}（按目录名稳定排序）。"""
    return {"runs": [build_run_row(path) for path in iter_run_dirs(runs_dir)]}


def resolve_run_path(runs_dir: Path, run_id: str) -> Path:
    """runId → run 目录（不存在或非法 → 404 report_not_found，契约 §9）。

    名称防护复用 statistics_service.resolve_run_dir（§12/§13.1 同一口径），
    追加 run 目录判据（无事实源文件的目录不是 run，如实 404）。
    """
    from .statistics_service import resolve_run_dir

    run_path = resolve_run_dir(runs_dir, run_id)
    if not _is_run_dir(run_path):
        raise ApiError("report_not_found", f"run {run_id} 不存在")
    return run_path


def run_detail(runs_dir: Path, run_id: str) -> dict[str, Any]:
    """契约 §9 `GET /api/runs/{runId}`：轻索引行 + manifest 原样 + summary.json 原样
    （无 → null，不伪造空 summary）+ artifacts 索引（§17 同源）。"""
    run_path = resolve_run_path(runs_dir, run_id)
    from .artifact_service import build_artifact_index

    detail = build_run_row(run_path)
    detail["manifest"] = _read_json_dict(run_path / "manifest.json")
    detail["summary"] = _read_json_dict(run_path / "summary.json") or None
    detail["artifacts"] = build_artifact_index(run_path)
    return detail


def register_run(runs_dir: Path, index: CaseIndex, run_id: str) -> dict[str, Any] | None:
    """save 成功后自动登记（契约 §14：登记失败由调用方兜底，不使 save 失败）。

    run 目录不存在或装配异常返回 None（调用方记日志），rebuild 兜底重登记。
    """
    name = run_id.removeprefix("run-")
    if not name or "/" in name or "\\" in name or "\x00" in name or name in (".", ".."):
        return None
    run_path = runs_dir / name
    if not run_path.is_dir():
        return None
    try:
        row = build_case_row(runs_dir, run_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("case 索引登记失败（run %s）：%s", run_id, exc)
        return None
    index.upsert(row)
    return row


def _row_to_case(row: sqlite3.Row | tuple) -> dict[str, Any]:
    values = dict(zip(_COLUMNS, tuple(row)))
    try:
        stats: dict[str, Any] = json.loads(values["stats_json"] or "{}")
    except json.JSONDecodeError:
        stats = {}
    stats = {key: stats.get(key) for key in CASE_STATS_KEYS}
    return {
        "runId": values["run_id"],
        "caseName": values["case_name"],
        "device": values["device"],
        "packageName": values["package_name"],
        "createdAt": values["created_at"],
        "summaryAvailable": bool(values["summary_available"]),
        "stats": stats,
        "path": values["path"],
    }
