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
