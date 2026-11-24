"""更正与查询。

更正（replace_day）：只读全局配置、当日曝光、与受影响区域相交的覆盖记录；
只更新受影响的覆盖；曝光与覆盖在同一事务生效；失败回滚、沿用原结果。

查询（query_insufficient）：只读全局覆盖与排除日的当日数据；
全部读取在同一事务内完成，整次结果取自同一已提交状态。

两者都基于区间算术，不逐秒展开。
"""

import re
from datetime import datetime, timezone

from .intervals import (
    clip_outside,
    day_phase_set,
    insufficient_intervals,
    normalize,
    recompute_region,
)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EPOCH_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def day_to_epoch(day):
    """'YYYY-MM-DD' -> 当日 00:00:00 UTC 的整数秒。"""
    if not DAY_RE.match(day or ""):
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{day!r}")
    try:
        dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"无效日期：{day!r}") from None
    return int(dt.timestamp())


def epoch_to_iso(ts):
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return "（超出可显示范围）"


def parse_epoch(text):
    """解析零相位时刻：整数秒，或 'YYYY-MM-DDTHH:MM:SSZ' 等 UTC 形式。"""
    tok = (text or "").strip()
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    for fmt in EPOCH_FORMATS:
        try:
            return int(datetime.strptime(tok, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise ValueError(f"无法解析 UTC 整秒时刻：{tok!r}（用整数秒或 YYYY-MM-DDTHH:MM:SSZ）")


def _parse_day_sec(tok, lineno):
    if re.fullmatch(r"\d+", tok):
        return int(tok)
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", tok)
    if m:
        h, mi, sec = (int(g) for g in m.groups())
        if mi < 60 and sec < 60 and h * 3600 + mi * 60 + sec <= 86400:
            return h * 3600 + mi * 60 + sec
    raise ValueError(f"第 {lineno} 行时间应为当日整数秒或 HH:MM:SS：{tok!r}")


def parse_csv(text):
    """解析当日曝光 CSV：每行 start,end（当日整数秒或 HH:MM:SS，含起点、不含终点）。

    '#' 开头为注释；可选表头 start,end；空表（无数据行）表示撤下当日曝光。
    """
    rows = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().replace(" ", "") == "start,end":
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            raise ValueError(f"第 {lineno} 行应为两列 start,end：{line!r}")
        s = _parse_day_sec(parts[0], lineno)
        e = _parse_day_sec(parts[1], lineno)
        if not (0 <= s < e <= 86400):
            raise ValueError(f"第 {lineno} 行需满足 0 <= start < end <= 86400：{line!r}")
        rows.append((s, e))
    return sorted(set(rows))


def get_config(conn):
    row = conn.execute("SELECT t0, period FROM config WHERE id = 1").fetchone()
    return (row["t0"], row["period"]) if row else None


def set_config(conn, t0, period):
    """设置零相位与周期；已有数据时拒绝（须先清空）。"""
    if not isinstance(period, int) or period <= 0:
        raise ValueError("周期必须是正整数秒")
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = conn.execute(
            "SELECT (SELECT COUNT(*) FROM exposures) + (SELECT COUNT(*) FROM coverage) AS c"
        ).fetchone()["c"]
        if n:
            raise ValueError("已有曝光/覆盖数据，请先清空再重设参数")
        conn.execute("DELETE FROM config")
        conn.execute("INSERT INTO config (id, t0, period) VALUES (1, ?, ?)", (t0, period))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reset_all(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM exposures")
        conn.execute("DELETE FROM coverage")
        conn.execute("DELETE FROM config")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def list_days(conn):
    return conn.execute(
        "SELECT day, COUNT(*) AS n, SUM(end_sec - start_sec) AS total "
        "FROM exposures GROUP BY day ORDER BY day"
    ).fetchall()


def replace_day(conn, day, csv_text):
    """整日上传/替换/撤下曝光，并同步受影响的全局覆盖（同一事务）。

    返回统计：受影响区域、读取/删除/插入/写入的覆盖记录数。
    """
    exposures = parse_csv(csv_text)  # 先解析校验，失败则不触碰数据库
    day_start = day_to_epoch(day)
    stats = {
        "day": day,
        "exposures": len(exposures),
        "affected": [],
        "cov_read": 0,
        "cov_deleted": 0,
        "cov_inserted": 0,
        "cov_written": 0,
    }
    conn.execute("BEGIN IMMEDIATE")
    try:
        cfg = get_config(conn)
        if cfg is None:
            raise ValueError("请先设置零相位时刻和周期")
        t0, period = cfg
        old_rows = conn.execute(
            "SELECT start_sec, end_sec FROM exposures WHERE day = ?", (day,)
        ).fetchall()
        old_set = day_phase_set(
            [(r["start_sec"], r["end_sec"]) for r in old_rows], day_start, t0, period
        )
        new_set = day_phase_set(exposures, day_start, t0, period)
        if old_set != new_set:
            affected = normalize(old_set + new_set)
            stats["affected"] = affected
            rows, seen = [], set()
            for rs, re_ in affected:  # 只读与受影响区域相交的覆盖记录
                for r in conn.execute(
                    "SELECT id, ph_start, ph_end, days FROM coverage "
                    "WHERE ph_start < ? AND ph_end > ?",
                    (re_, rs),
                ):
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        rows.append(r)
            stats["cov_read"] = len(rows)
            cover = sorted((r["ph_start"], r["ph_end"], r["days"]) for r in rows)
            new_rows = recompute_region(cover, old_set, new_set, affected)
            # 与区域部分相交的行被整行删除，区域外碎片原样补回
            fragments = []
            for r in rows:
                for fs, fe in clip_outside(r["ph_start"], r["ph_end"], affected):
                    fragments.append((fs, fe, r["days"]))
            inserted = new_rows + fragments
            conn.executemany("DELETE FROM coverage WHERE id = ?", [(r["id"],) for r in rows])
            conn.executemany(
                "INSERT INTO coverage (ph_start, ph_end, days) VALUES (?, ?, ?)", inserted
            )
            stats["cov_deleted"] = len(rows)
            stats["cov_inserted"] = len(inserted)
        # 曝光与覆盖在同一事务内一起生效
        conn.execute("DELETE FROM exposures WHERE day = ?", (day,))
        conn.executemany(
            "INSERT INTO exposures (day, start_sec, end_sec) VALUES (?, ?, ?)",
            [(day, s, e) for s, e in exposures],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")  # 失败沿用原结果
        raise
    stats["cov_written"] = stats["cov_deleted"] + stats["cov_inserted"]
    return stats


def query_insufficient(conn, exclude_days):
    """覆盖不足两日的相位区间（半开秒区间）。

    只读全局覆盖与排除日（<=2）的当日数据；全部读取在同一事务内完成，
    整次结果取自同一已提交状态。
    """
    if len(exclude_days) > 2:
        raise ValueError("最多临时排除两日")
    conn.execute("BEGIN")
    try:
        cfg = get_config(conn)
        if cfg is None:
            raise ValueError("请先设置零相位时刻和周期")
        t0, period = cfg
        rows = conn.execute(
            "SELECT ph_start, ph_end, days FROM coverage ORDER BY ph_start"
        ).fetchall()
        cover = [(r["ph_start"], r["ph_end"], r["days"]) for r in rows]
        excl_sets = []
        for d in exclude_days:
            ex = conn.execute(
                "SELECT start_sec, end_sec FROM exposures WHERE day = ?", (d,)
            ).fetchall()
            excl_sets.append(
                day_phase_set(
                    [(r["start_sec"], r["end_sec"]) for r in ex],
                    day_to_epoch(d),
                    t0,
                    period,
                )
            )
        result = insufficient_intervals(cover, excl_sets, period)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result, {"cov_read": len(rows), "period": period}
