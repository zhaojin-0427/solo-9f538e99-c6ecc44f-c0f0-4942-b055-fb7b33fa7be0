"""端到端测试：最少观测日计划的预览、确认保存、过期拒绝（即使改回原样）、
无解不保存、写入失败不动旧计划、20 候选上限、重置清理与大周期端点扫描。"""

import sqlite3

import pytest

from app import create_app
from app.core import day_to_epoch, day_phase_set

T0, P = 0, 1000


@pytest.fixture()
def env(tmp_path):
    db_path = str(tmp_path / "t.db")
    app = create_app(db_path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, db_path


def set_config(client):
    return client.post("/config", data={"t0": "0", "period": str(P)})


def upload(client, day, csv):
    return client.post("/day", data={"day": day, "csv": csv})


def preview(client, lo, hi, need):
    return client.post(
        "/plan/preview",
        data={"ph_start": str(lo), "ph_end": str(hi), "need_days": str(need)},
    )


def confirm(client):
    return client.post("/plan/confirm")


def raw(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def phases_of(db_path, day):
    """直接从库中某日曝光折相，得到当日相位集。"""
    conn = sqlite3.connect(db_path)
    cfg = conn.execute("SELECT t0, period FROM config WHERE id=1").fetchone()
    rows = conn.execute(
        "SELECT start_sec, end_sec FROM exposures WHERE day=?", (day,)
    ).fetchall()
    conn.close()
    return day_phase_set(list(rows), day_to_epoch(day), cfg[0], cfg[1])


def test_preview_and_confirm_save_plan(env):
    client, db_path = env
    set_config(client)
    # 两日都覆盖相位 [100, 200)
    upload(client, "1970-01-01", "100,200\n")
    upload(client, "1970-01-02", "100,200\n")
    text = preview(client, 100, 200, 1).get_data(as_text=True)
    assert "预览完成" in text and "最少选择 1 日" in text
    assert "候选日 2 个" in text and "本次预览读取曝光记录 2 条" in text
    assert "[100, 200)" in text  # 贡献区间列出
    # 确认保存
    text = confirm(client).get_data(as_text=True)
    assert "计划已保存" in text
    plans = raw(db_path, "SELECT ph_start, ph_end, need_days, chosen_days FROM plans")
    assert len(plans) == 1
    assert plans[0][:3] == (100, 200, 1)
    assert plans[0][3] in ('["1970-01-01"]', '["1970-01-02"]')
    # 页面回显已保存计划
    home = client.get("/").get_data(as_text=True)
    assert "当前已保存的计划" in home and "保存于" in home


def test_need_two_days_requires_two_distinct_days(env):
    client, db_path = env
    set_config(client)
    # 用绝对秒曝光，让两个 UTC 日折相后都落在 [100,200)
    # 1970-01-01 当日秒 100 -> 相位 100；1970-01-02 起点相位 400，当日秒 700 -> 100
    upload(client, "1970-01-01", "100,200\n")
    upload(client, "1970-01-02", "700,800\n")
    assert phases_of(db_path, "1970-01-01") == [(100, 200)]
    assert phases_of(db_path, "1970-01-02") == [(100, 200)]
    # k=2：两日都要
    text = preview(client, 100, 200, 2).get_data(as_text=True)
    assert "最少选择 2 日" in text
    confirm(client)
    chosen = raw(db_path, "SELECT chosen_days FROM plans")[0][0]
    assert "1970-01-01" in chosen and "1970-01-02" in chosen
    # 只有一日时 k=2 无解
    client.post("/reset")
    set_config(client)
    upload(client, "1970-01-01", "100,200\n")
    text = preview(client, 100, 200, 2).get_data(as_text=True)
    assert "无解" in text
    text = confirm(client).get_data(as_text=True)
    assert "拒绝保存" in text
    assert raw(db_path, "SELECT COUNT(*) FROM plans")[0][0] == 0


def test_stale_rejected_even_when_changed_back(env):
    """预览后候选日被更正，即使改回原样（版本已增）也拒绝确认。"""
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "100,200\n")
    upload(client, "1970-01-02", "100,200\n")
    preview(client, 100, 200, 1)
    # 先存一份计划，之后过期确认不应改动它
    confirm(client)
    before = raw(db_path, "SELECT * FROM plans")
    # 重新预览，再把 01-01 改成别的、又改回原样
    preview(client, 100, 200, 1)
    upload(client, "1970-01-01", "300,400\n")
    upload(client, "1970-01-01", "100,200\n")  # 内容改回原样
    assert phases_of(db_path, "1970-01-01") == [(100, 200)]  # 数据确实回到原样
    text = confirm(client).get_data(as_text=True)
    assert "拒绝保存" in text and "1970-01-01" in text and "重新预览" in text
    assert raw(db_path, "SELECT * FROM plans") == before  # 旧计划未改动
    # 重新预览后可以正常确认
    preview(client, 100, 200, 1)
    text = confirm(client).get_data(as_text=True)
    assert "计划已保存" in text


def test_stale_rejected_after_correction_of_any_candidate(env):
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "100,200\n")
    upload(client, "1970-01-02", "100,200\n")
    preview(client, 100, 200, 1)
    # 更正的是未选中的候选日，也必须拒绝
    upload(client, "1970-01-02", "100,200\n500,600\n")
    text = confirm(client).get_data(as_text=True)
    assert "拒绝保存" in text and "1970-01-02" in text
    assert raw(db_path, "SELECT COUNT(*) FROM plans")[0][0] == 0


def test_unrelated_correction_does_not_invalidate(env):
    """更正非候选日（预览之后新上传的日）不影响确认。"""
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "100,200\n")
    preview(client, 100, 200, 1)
    upload(client, "1970-01-05", "100,200\n")  # 预览时不在候选集
    text = confirm(client).get_data(as_text=True)
    assert "计划已保存" in text
    assert raw(db_path, "SELECT COUNT(*) FROM plans")[0][0] == 1


def test_no_solution_preview_blocks_confirm_and_keeps_old_plan(env):
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "0,50\n")  # 覆盖不到 [100,200)
    # 先存一份合法旧计划
    upload(client, "1970-01-02", "0,400\n")
    preview(client, 0, 400, 1)
    confirm(client)
    before = raw(db_path, "SELECT * FROM plans")
    # 无解预览
    text = preview(client, 100, 200, 2).get_data(as_text=True)
    assert "无解" in text
    text = confirm(client).get_data(as_text=True)
    assert "拒绝保存" in text
    assert raw(db_path, "SELECT * FROM plans") == before


def test_write_failure_keeps_old_plan(env):
    """写入阶段故障：plans 表上的触发器抛错，事务回滚，旧计划不动。"""
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "100,200\n")
    upload(client, "1970-01-02", "100,200\n")
    preview(client, 100, 200, 1)
    confirm(client)
    before = raw(db_path, "SELECT * FROM plans")
    # 制造写入失败：plans 表禁止插入
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TRIGGER plans_block BEFORE INSERT ON plans "
        "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
    )
    conn.commit()
    conn.close()
    preview(client, 100, 200, 1)
    text = confirm(client).get_data(as_text=True)
    assert "确认失败" in text or "拒绝保存" in text
    assert raw(db_path, "SELECT * FROM plans") == before  # 旧计划原样保留
    # 预览快照因回滚仍在；移除触发器后重新确认可成功
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER plans_block")
    conn.commit()
    conn.close()
    text = confirm(client).get_data(as_text=True)
    assert "计划已保存" in text


def test_candidate_limit_twenty(env):
    """候选日不超过 20 个：上传 22 日，预览只取最早 20 日。"""
    client, db_path = env
    set_config(client)
    for i in range(1, 23):
        day = f"1970-01-{i:02d}"
        upload(client, day, "100,200\n")
    text = preview(client, 100, 200, 1).get_data(as_text=True)
    assert "候选日 20 个" in text
    cand = raw(db_path, "SELECT payload FROM plan_preview")[0][0]
    import json

    snapshot = json.loads(cand)
    assert len(snapshot["candidates"]) == 20
    assert snapshot["candidates"][0] == "1970-01-01"
    assert snapshot["candidates"][-1] == "1970-01-20"  # 按 UTC 日升序截断


def test_preview_validation_errors(env):
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "0,100\n")
    # 起点 >= 终点
    assert "半开区间需满足" in preview(client, 500, 500, 1).get_data(as_text=True)
    # 超出周期
    assert "相位端点须在" in preview(client, 0, P + 1, 1).get_data(as_text=True)
    # 非法覆盖日数
    assert "1 或 2" in preview(client, 0, 100, 3).get_data(as_text=True)
    assert "1 或 2" in preview(client, 0, 100, 0).get_data(as_text=True)
    # 非整数端点
    assert "非负整数秒" in preview(client, "x", 100, 1).get_data(as_text=True)
    # 未设参数
    client.post("/reset")
    assert "请先设置" in preview(client, 0, 100, 1).get_data(as_text=True)


def test_confirm_without_preview_rejected(env):
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "0,100\n")
    text = confirm(client).get_data(as_text=True)
    assert "拒绝保存" in text and "重新预览" in text
    assert raw(db_path, "SELECT COUNT(*) FROM plans")[0][0] == 0


def test_new_preview_replaces_old_one(env):
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "0,100\n")
    upload(client, "1970-01-02", "0,100\n")
    preview(client, 0, 100, 1)
    first_id = raw(db_path, "SELECT preview_id FROM plan_preview")[0][0]
    preview(client, 0, 100, 2)
    second_id = raw(db_path, "SELECT preview_id FROM plan_preview")[0][0]
    assert first_id != second_id
    # 会话里的旧 id 与库中不匹配 -> 拒绝
    with client.session_transaction() as sess:
        sess["plan_preview_id"] = first_id
    text = confirm(client).get_data(as_text=True)
    assert "预览已失效" in text


def test_reset_clears_plans_and_previews(env):
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "0,100\n")
    preview(client, 0, 100, 1)
    confirm(client)
    assert raw(db_path, "SELECT COUNT(*) FROM plans")[0][0] == 1
    client.post("/reset")
    assert raw(db_path, "SELECT COUNT(*) FROM plans")[0][0] == 0
    assert raw(db_path, "SELECT COUNT(*) FROM plan_preview")[0][0] == 0
    assert raw(db_path, "SELECT COUNT(*) FROM day_versions")[0][0] == 0
    # 重置后旧会话确认无效
    text = confirm(client).get_data(as_text=True)
    assert "拒绝保存" in text


def test_huge_period_endpoint_scan_is_instant(env):
    """P=10^12：计划计算只扫端点，不随区间秒数增长。"""
    client, db_path = env
    client.post("/config", data={"t0": "0", "period": str(10**12)})
    upload(client, "2026-11-23", "0,3600\n")
    upload(client, "2026-11-24", "0,3600\n")
    ph1 = phases_of(db_path, "2026-11-23")
    ph2 = phases_of(db_path, "2026-11-24")
    # 大周期下两个 UTC 日折相互不重叠
    assert ph1 != ph2 and all(e1 <= s2 or e2 <= s1
                              for s1, e1 in ph1 for s2, e2 in ph2)
    import time

    t0 = time.monotonic()
    # k=2：整圈任何点都不可能有两日重叠 -> 无解；扫描跨度 10^12 秒仍瞬时
    text = preview(client, 0, 10**12, 2).get_data(as_text=True)
    assert "无解" in text
    # k=1：各自片段内一日即可
    s1, e1 = ph1[0]
    text = preview(client, s1, e1, 1).get_data(as_text=True)
    assert "最少选择 1 日" in text
    s2, e2 = ph2[0]
    text = preview(client, s2, e2, 1).get_data(as_text=True)
    assert "最少选择 1 日" in text
    # k=1 目标横跨两段（中间有缺口）-> 无解
    text = preview(client, min(s1, s2), max(e1, e2), 1).get_data(as_text=True)
    assert "无解" in text
    assert time.monotonic() - t0 < 2.0
    # 小周期 P=1000：两个 UTC 日可折到同一相位，k=2 可解
    client.post("/reset")
    set_config(client)
    upload(client, "1970-01-01", "100,200\n")
    upload(client, "1970-01-02", "700,800\n")  # 折相后同为 [100,200)
    text = preview(client, 100, 200, 2).get_data(as_text=True)
    assert "最少选择 2 日" in text


def test_day_versions_increment_on_every_correction(env):
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "0,100\n")
    assert raw(db_path, "SELECT version FROM day_versions WHERE day=?",
               ("1970-01-01",))[0][0] == 1
    upload(client, "1970-01-01", "0,100\n")  # 相同内容再次提交也算一次更正
    assert raw(db_path, "SELECT version FROM day_versions WHERE day=?",
               ("1970-01-01",))[0][0] == 2
    upload(client, "1970-01-01", "")  # 撤下
    assert raw(db_path, "SELECT version FROM day_versions WHERE day=?",
               ("1970-01-01",))[0][0] == 3
