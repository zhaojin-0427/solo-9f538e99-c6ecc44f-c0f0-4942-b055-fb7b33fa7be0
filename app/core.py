"""更正与查询、最少观测日计划。

更正（replace_day）：只读全局配置、当日曝光、与受影响区域相交的覆盖记录；
只更新受影响的覆盖；曝光、覆盖、当日数据版本在同一事务生效；失败回滚、沿用原结果。

查询（query_insufficient）：只读全局覆盖与排除日的当日数据；
全部读取在同一事务内完成，整次结果取自同一已提交状态。

计划预览（preview_plan）：只读取一个已提交状态（单一只读事务），
从已有曝光日中选 <=20 个候选日，端点扫描求最少观测日。
计划确认（confirm_plan）：BEGIN IMMEDIATE 内核对候选日版本与参数，
任一候选日在预览后被更正过（版本变化，即使改回原样）即拒绝；
无解/过期/写入失败都不改旧计划。

两者都基于区间算术，不逐秒展开。
"""

import json
import re
import uuid
from datetime import datetime, timezone

from .intervals import (
    clip_outside,
    day_phase_set,
    insufficient_intervals,
    normalize,
    recompute_region,
)
from .planning import clip_intervals, find_min_days

MAX_CANDIDATES = 20
MAX_NEED_DAYS = 2

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
        conn.execute("DELETE FROM day_versions")
        conn.execute("DELETE FROM plans")
        conn.execute("DELETE FROM plan_preview")
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
        "day_version": None,
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
        # 当日数据版本单调 +1（与曝光、覆盖同事务）：预览后只要成功更正过，
        # 即使内容改回原样，版本也不再相同，确认据此判定过期。
        conn.execute(
            "INSERT INTO day_versions (day, version) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET version = version + 1",
            (day,),
        )
        ver = conn.execute(
            "SELECT version FROM day_versions WHERE day = ?", (day,)
        ).fetchone()["version"]
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")  # 失败沿用原结果
        raise
    stats["cov_written"] = stats["cov_deleted"] + stats["cov_inserted"]
    stats["day_version"] = ver
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


def _parse_phase(text, name):
    """解析相位端点：非负整数秒。"""
    tok = (text or "").strip()
    if not re.fullmatch(r"\d+", tok):
        raise ValueError(f"{name}应为非负整数秒：{tok!r}")
    return int(tok)


def _day_versions_map(conn, days):
    if not days:
        return {}
    rows = conn.execute(
        f"SELECT day, version FROM day_versions WHERE day IN "
        f"({','.join('?' for _ in days)})",
        days,
    ).fetchall()
    return {r["day"]: r["version"] for r in rows}


def preview_plan(conn, start_text, end_text, need_days):
    """最少观测日计划预览：单一只读事务，整次结果取自同一已提交状态。

    从已有曝光日中选不超过 20 个候选日（按 UTC 日升序），
    在端点上扫描求让 [start, end) 每个相位至少被 need_days 个不同 UTC 日
    覆盖的最少日期；计算只扫描区间端点，与区间秒数无关。
    计算完成后把快照写入单行 scratch 表 plan_preview（新事务，只写派生数据），
    返回 (preview_id, snapshot)。
    """
    lo = _parse_phase(start_text, "相位起点")
    hi = _parse_phase(end_text, "相位终点")
    if not isinstance(need_days, int) or need_days not in (1, MAX_NEED_DAYS):
        raise ValueError("所需覆盖日数只支持 1 或 2")
    if lo >= hi:
        raise ValueError("半开区间需满足 起 < 止")
    conn.execute("BEGIN")  # 单一只读事务 -> 单一已提交状态
    try:
        cfg = get_config(conn)
        if cfg is None:
            raise ValueError("请先设置零相位时刻和周期")
        t0, period = cfg
        if not (0 <= lo < hi <= period):
            raise ValueError(f"相位端点须在 [0, {period}) 内且起 < 止")
        day_rows = conn.execute(
            "SELECT DISTINCT day FROM exposures ORDER BY day LIMIT ?",
            (MAX_CANDIDATES,),
        ).fetchall()
        days = [r["day"] for r in day_rows]
        exp_read = 0
        if len(days) < need_days:
            # 连候选日数都不足，必然无解（仍返回快照，由页面提示）
            solvable, chosen, contributions = False, [], {}
        else:
            candidates = []
            for d in days:
                rows = conn.execute(
                    "SELECT start_sec, end_sec FROM exposures WHERE day = ?", (d,)
                ).fetchall()
                exp_read += len(rows)
                phase_set = day_phase_set(
                    [(r["start_sec"], r["end_sec"]) for r in rows],
                    day_to_epoch(d),
                    t0,
                    period,
                )
                candidates.append((d, clip_intervals(phase_set, lo, hi)))
            found = find_min_days(candidates, lo, hi, need_days)
            if found is None:
                solvable, chosen, contributions = False, [], {}
            else:
                solvable, chosen, contributions = True, found[0], found[1]
        versions = _day_versions_map(conn, days)
        snapshot = {
            "t0": t0,
            "lo": lo,
            "hi": hi,
            "need_days": need_days,
            "period": period,
            "candidates": days,
            "versions": versions,
            "solvable": solvable,
            "chosen": chosen,
            "contributions": contributions,
            "exp_read": exp_read,
        }
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    # 派生快照单独事务落盘（scratch 表，不影响曝光/覆盖/已存计划）
    preview_id = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM plan_preview")
        conn.execute(
            "INSERT INTO plan_preview (id, preview_id, payload, created_at) "
            "VALUES (1, ?, ?, ?)",
            (preview_id, json.dumps(snapshot, ensure_ascii=False),
             int(datetime.now(tz=timezone.utc).timestamp())),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return preview_id, snapshot


class StalePlanError(ValueError):
    """预览后候选日数据被更正过（或参数变化、预览无解），须重新预览。"""


def confirm_plan(conn, preview_id):
    """按预览快照保存计划；不重新求解，只核对并写入所见版本。

    BEGIN IMMEDIATE 单事务内完成全部读取（快照、参数、版本、曝光）与写入：
    任一候选日版本与预览时不同（即使改回原样）即拒绝；
    无解、过期、写入失败都抛出异常，旧计划保持不变。
    """
    if not preview_id:
        raise StalePlanError("缺少预览结果，请先预览再确认保存")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT preview_id, payload FROM plan_preview WHERE id = 1"
        ).fetchone()
        if row is None or row["preview_id"] != preview_id:
            raise StalePlanError("预览已失效（被新的预览取代或已清空），请重新预览")
        preview = json.loads(row["payload"])
        if not preview.get("solvable"):
            raise StalePlanError("预览无解，无法保存；请调整区间或补充曝光后重新预览")
        days = list(preview["candidates"])
        old_versions = dict(preview["versions"])
        lo, hi = preview["lo"], preview["hi"]
        need_days = preview["need_days"]
        chosen = list(preview["chosen"])
        contributions = {d: list(iv) for d, iv in preview["contributions"].items()}
        cfg = get_config(conn)
        if cfg is None or cfg != (preview["t0"], preview["period"]):
            raise StalePlanError("零相位/周期已变化，请重新预览")
        t0, period = cfg
        if not (0 <= lo < hi <= period) or need_days not in (1, MAX_NEED_DAYS):
            raise StalePlanError("预览参数已失效，请重新预览")
        current = _day_versions_map(conn, days)
        changed = [d for d in days if current.get(d) != old_versions.get(d)]
        if changed:
            raise StalePlanError(
                "候选日在预览后被更正过（" + "、".join(sorted(changed)) + "），请重新预览"
            )
        # 版本一致前提下再确认选中日仍有曝光（防御性校验）
        n = conn.execute(
            f"SELECT COUNT(DISTINCT day) AS c FROM exposures "
            f"WHERE day IN ({','.join('?' for _ in chosen)})",
            chosen,
        ).fetchone()["c"]
        if n != len(chosen):
            raise StalePlanError("选中日数据已变化，请重新预览")
        saved_at = int(datetime.now(tz=timezone.utc).timestamp())
        conn.execute("DELETE FROM plans WHERE id = 1")
        conn.execute(
            "INSERT INTO plans (id, ph_start, ph_end, need_days, chosen_days, "
            "candidates, contributions, saved_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                lo,
                hi,
                need_days,
                json.dumps(chosen, ensure_ascii=False),
                json.dumps(
                    [{"day": d, "version": old_versions.get(d)} for d in days],
                    ensure_ascii=False,
                ),
                json.dumps(contributions, ensure_ascii=False),
                saved_at,
            ),
        )
        # 确认成功后使本次预览失效（所见版本已落盘）
        conn.execute("DELETE FROM plan_preview")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")  # 过期/写入失败都不改旧计划
        raise
    return {"chosen": chosen, "saved_at": saved_at, "n_candidates": len(days)}


def get_saved_plan(conn):
    """读取当前已保存的计划（单行，无则 None）。"""
    row = conn.execute(
        "SELECT ph_start, ph_end, need_days, chosen_days, candidates, "
        "contributions, saved_at FROM plans WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "lo": row["ph_start"],
        "hi": row["ph_end"],
        "need_days": row["need_days"],
        "chosen": json.loads(row["chosen_days"]),
        "candidates": json.loads(row["candidates"]),
        "contributions": json.loads(row["contributions"]),
        "saved_at": row["saved_at"],
    }
