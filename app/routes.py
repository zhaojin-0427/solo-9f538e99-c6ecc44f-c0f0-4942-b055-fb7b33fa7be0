from flask import Blueprint, g, render_template, request, session

from . import core

bp = Blueprint("main", __name__)


def _render(**extra):
    cfg = core.get_config(g.db)
    ctx = {
        "config": cfg,
        "config_iso": core.epoch_to_iso(cfg[0]) if cfg else None,
        "days": core.list_days(g.db),
        "last_correction": session.get("last_correction"),
        "last_query": session.get("last_query"),
        "plan_preview": session.get("plan_preview"),
        "saved_plan": core.get_saved_plan(g.db) if cfg else None,
        "message": None,
        "kind": "ok",
        "correction": None,
        "result": None,
        "qstats": None,
        "excl": [],
    }
    ctx.update(extra)
    return render_template("index.html", **ctx)


@bp.get("/")
def index():
    return _render()


@bp.post("/config")
def set_config():
    try:
        t0 = core.parse_epoch(request.form.get("t0", ""))
        period_text = (request.form.get("period") or "").strip()
        period = int(period_text) if period_text.lstrip("-").isdigit() else -1
        core.set_config(g.db, t0, period)
        return _render(message="参数已保存。", kind="ok")
    except ValueError as e:
        return _render(message=f"保存参数失败：{e}", kind="err")


@bp.post("/reset")
def reset():
    core.reset_all(g.db)
    session.pop("last_correction", None)
    session.pop("last_query", None)
    session.pop("plan_preview", None)
    session.pop("plan_preview_id", None)
    return _render(message="已清空全部数据，可重新设置参数。", kind="ok")


@bp.post("/day")
def replace_day():
    day = (request.form.get("day") or "").strip()
    csv_text = request.form.get("csv") or ""
    try:
        stats = core.replace_day(g.db, day, csv_text)
        session["last_correction"] = {
            "day": stats["day"],
            "cov_written": stats["cov_written"],
            "cov_deleted": stats["cov_deleted"],
            "cov_inserted": stats["cov_inserted"],
            "day_version": stats["day_version"],
        }
        if stats["exposures"] == 0:
            msg = f"{day}：空表，已撤下当日曝光。"
        else:
            msg = f"{day}：已整日替换为 {stats['exposures']} 条曝光。"
        return _render(message=msg, kind="ok", correction=stats)
    except ValueError as e:
        return _render(message=f"更正失败，已沿用原结果：{e}", kind="err")


@bp.post("/query")
def query():
    excl = [
        d
        for d in (
            (request.form.get("excl1") or "").strip(),
            (request.form.get("excl2") or "").strip(),
        )
        if d
    ]
    try:
        result, stats = core.query_insufficient(g.db, excl)
        session["last_query"] = {"cov_read": stats["cov_read"], "excl": excl}
        return _render(
            message="查询完成。", kind="ok", result=result, qstats=stats, excl=excl
        )
    except ValueError as e:
        return _render(message=f"查询失败：{e}", kind="err")


def _parse_need_days(text):
    tok = (text or "").strip()
    if not tok.lstrip("-").isdigit():
        raise ValueError("所需覆盖日数应为 1 或 2")
    n = int(tok)
    if n not in (1, core.MAX_NEED_DAYS):
        raise ValueError("所需覆盖日数只支持 1 或 2")
    return n


@bp.post("/plan/preview")
def plan_preview():
    try:
        need_days = _parse_need_days(request.form.get("need_days"))
        preview_id, snapshot = core.preview_plan(
            g.db,
            request.form.get("ph_start", ""),
            request.form.get("ph_end", ""),
            need_days,
        )
        session["plan_preview_id"] = preview_id
        session["plan_preview"] = snapshot  # 用于页面回显；核对以服务端快照为准
        if snapshot["solvable"]:
            msg = (
                f"预览完成：候选 {len(snapshot['candidates'])} 日，"
                f"最少选择 {len(snapshot['chosen'])} 日即可满足"
                f" [{snapshot['lo']}, {snapshot['hi']}) 每相位 {need_days} 日覆盖。"
            )
        else:
            msg = (
                "预览完成但无解：现有候选日无法让目标区间每个相位都被 "
                f"{need_days} 个不同 UTC 日覆盖，请补充曝光或调整区间。"
            )
        return _render(message=msg, kind="ok" if snapshot["solvable"] else "err")
    except ValueError as e:
        return _render(message=f"预览失败：{e}", kind="err")


@bp.post("/plan/confirm")
def plan_confirm():
    preview_id = session.get("plan_preview_id")
    try:
        stats = core.confirm_plan(g.db, preview_id)
        session.pop("plan_preview_id", None)
        session.pop("plan_preview", None)
        return _render(
            message="计划已保存（所见版本）：" + "、".join(stats["chosen"]),
            kind="ok",
        )
    except core.StalePlanError as e:
        # 过期/无解：旧计划不动，要求重算
        return _render(message=f"拒绝保存，请重新预览：{e}", kind="err")
    except ValueError as e:
        return _render(message=f"确认失败，旧计划未改动：{e}", kind="err")
    except Exception as e:  # 写入失败等数据库异常：事务已回滚，旧计划不动
        return _render(message=f"确认失败，旧计划未改动：{type(e).__name__}: {e}", kind="err")
