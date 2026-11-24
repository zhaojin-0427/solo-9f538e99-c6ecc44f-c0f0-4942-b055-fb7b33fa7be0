"""端到端测试：跨周期曝光、整日替换前后的缺口、空表撤下、临时排除、
失败沿用原结果、记录数统计、大周期（不逐秒展开）。"""

import sqlite3

import pytest

from app import create_app

T0, P = 0, 1000
# t0=0（1970-01-01T00:00:00Z），P=1000 秒。
# 1970-01-02 起点的相位 = 86400 % 1000 = 400；1970-01-03 -> 800；1970-01-04 -> 200。
DAY1, DAY2, DAY3 = "1970-01-02", "1970-01-03", "1970-01-04"
# DAY1: 绝对 [86900,87200) -> 相位 [900,1000)∪[0,200)（跨周期）；[87210,87220) -> [210,220)
CSV1 = "500,800\n810,820\n"
# DAY2: 绝对 [172950,173050) -> 相位 [950,1000)∪[0,50)（跨周期）
CSV2 = "150,250\n"
# DAY3: 绝对 [259600,259700) -> 相位 [600,700)
CSV3 = "400,500\n"
# 整日替换 DAY1：绝对 [87000,87150) -> 相位 [0,150)
CSV1_NEW = "600,750\n"


@pytest.fixture()
def env(tmp_path):
    db_path = str(tmp_path / "t.db")
    app = create_app(db_path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, db_path


def dump(db_path):
    conn = sqlite3.connect(db_path)
    cov = conn.execute(
        "SELECT ph_start, ph_end, days FROM coverage ORDER BY ph_start"
    ).fetchall()
    exp = conn.execute(
        "SELECT day, start_sec, end_sec FROM exposures ORDER BY day, start_sec"
    ).fetchall()
    ids = dict(
        conn.execute("SELECT ph_start, id FROM coverage").fetchall()
    )  # ph_start 唯一（区间不相交）
    conn.close()
    return [tuple(r) for r in cov], [tuple(r) for r in exp], ids


def set_config(client, t0=str(T0), period=str(P)):
    return client.post("/config", data={"t0": t0, "period": period})


def upload(client, day, csv):
    return client.post("/day", data={"day": day, "csv": csv})


def query(client, excl1="", excl2=""):
    return client.post("/query", data={"excl1": excl1, "excl2": excl2})


def setup_three_days(client):
    set_config(client)
    upload(client, DAY1, CSV1)
    upload(client, DAY2, CSV2)
    upload(client, DAY3, CSV3)


# 三日上传后的全局覆盖（手工推导）：
COV3 = [
    (0, 50, 2),
    (50, 200, 1),
    (210, 220, 1),
    (600, 700, 1),
    (900, 950, 1),
    (950, 1000, 2),
]


def test_cross_period_exposures_build_coverage(env):
    client, db_path = env
    setup_three_days(client)
    cov, exp, _ = dump(db_path)
    assert cov == COV3
    assert len(exp) == 4  # 2 + 1 + 1 条曝光


def test_query_gaps_before_replacement(env):
    client, db_path = env
    setup_three_days(client)
    resp = query(client)
    text = resp.get_data(as_text=True)
    assert "[50, 950)" in text  # 唯一不足两日的相位段
    assert "[0, 50)" not in text and "[950, 1,000)" not in text  # 这两段已满两日
    assert "本次查询读取全局覆盖记录 6 条" in text  # 单次查询读取的记录数


def test_day_replacement_gaps_and_affected_only(env):
    """整日替换：缺口从 [50,950) 变为 [50,1000)，受影响区域外的记录不动。"""
    client, db_path = env
    setup_three_days(client)
    _, _, ids_before = dump(db_path)
    id_600 = ids_before[600]  # (600,700,1) 与受影响区域不相交

    resp = upload(client, DAY1, CSV1_NEW)
    text = resp.get_data(as_text=True)
    assert "已整日替换为 1 条曝光" in text
    # 受影响区域 = 旧集 ∪ 新集
    for frag in ("[0, 200)", "[210, 220)", "[900, 1,000)"):
        assert frag in text
    # 单次更正写入的记录数：删除 5 + 插入 3 = 8
    assert "写入全局覆盖记录：8 条" in text
    assert "删除 5 + 插入 3" in text

    cov, _, ids_after = dump(db_path)
    assert cov == [(0, 50, 2), (50, 150, 1), (600, 700, 1), (950, 1000, 1)]
    assert ids_after[600] == id_600  # 未受影响的记录原样保留（id 不变）

    # 替换后的缺口
    text = query(client).get_data(as_text=True)
    assert "[50, 1,000)" in text
    assert "本次查询读取全局覆盖记录 4 条" in text


def test_empty_csv_withdraws_day(env):
    client, db_path = env
    setup_three_days(client)
    resp = upload(client, DAY2, "")  # 空表撤下当日
    text = resp.get_data(as_text=True)
    assert "空表，已撤下当日曝光" in text
    assert "写入全局覆盖记录：4 条" in text  # 删 2 + 插 2
    cov, exp, _ = dump(db_path)
    assert all(d != DAY2 for d, _, _ in exp)
    assert cov == [
        (0, 50, 1),
        (50, 200, 1),
        (210, 220, 1),
        (600, 700, 1),
        (900, 950, 1),
        (950, 1000, 1),
    ]


def test_query_with_excluded_days(env):
    client, db_path = env
    setup_three_days(client)
    upload(client, DAY1, CSV1_NEW)  # 覆盖：(0,50,2)(50,150,1)(600,700,1)(950,1000,1)

    # 排除 DAY2（[0,50)∪[950,1000)）：两段满两日的都失守 -> 全周期不足
    text = query(client, excl1=DAY2).get_data(as_text=True)
    assert "[0, 1,000)" in text and "已临时排除" in text
    # 排除 DAY1（[0,150)）：同样全周期不足
    text = query(client, excl1=DAY1).get_data(as_text=True)
    assert "[0, 1,000)" in text
    # 排除 DAY3（[600,700)，本就不足两日）：结果不变
    text = query(client, excl1=DAY3).get_data(as_text=True)
    assert "[50, 1,000)" in text and "[0, 1,000)" not in text
    # 排除两日
    text = query(client, excl1=DAY1, excl2=DAY2).get_data(as_text=True)
    assert "[0, 1,000)" in text
    # 排除是临时的：再查不带排除，缺口依旧
    text = query(client).get_data(as_text=True)
    assert "[50, 1,000)" in text and "[0, 1,000)" not in text


def test_exclusion_discriminates_sufficient_phase(env):
    """两日覆盖同一相位 -> 满两日；临时排除任一日 -> 立即不足。"""
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "0,100\n")  # 相位 [0,100)
    upload(client, DAY1, "600,700\n")  # 绝对 [87000,87100) -> 相位 [0,100)
    cov, _, _ = dump(db_path)
    assert cov == [(0, 100, 2)]
    text = query(client).get_data(as_text=True)
    assert "[100, 1,000)" in text and "[0, 100)" not in text
    text = query(client, excl1="1970-01-01").get_data(as_text=True)
    assert "[0, 1,000)" in text


def test_same_day_same_phase_counts_once(env):
    client, db_path = env
    set_config(client)
    # 两段相隔整一周期的曝光折到同一相位；同日只计一次
    upload(client, "1970-01-01", "0,100\n1000,1100\n")
    cov, _, _ = dump(db_path)
    assert cov == [(0, 100, 1)]
    # 同日内重叠曝光亦然
    upload(client, "1970-01-01", "0,100\n50,150\n")
    cov, _, _ = dump(db_path)
    assert cov == [(0, 150, 1)]


def test_half_open_adjacent_exposures_merge(env):
    client, db_path = env
    set_config(client)
    upload(client, "1970-01-01", "0,100\n100,200\n")  # 半开相接 -> 连续覆盖
    cov, _, _ = dump(db_path)
    assert cov == [(0, 200, 1)]


def test_failed_correction_keeps_original(env):
    client, db_path = env
    setup_three_days(client)
    before = dump(db_path)
    resp = upload(client, DAY1, "这不是CSV\n")
    text = resp.get_data(as_text=True)
    assert "更正失败，已沿用原结果" in text
    assert dump(db_path) == before
    # start >= end 同样失败且不动数据
    resp = upload(client, DAY1, "500,100\n")
    assert "更正失败" in resp.get_data(as_text=True)
    assert dump(db_path) == before


def test_upload_before_config_fails_cleanly(env):
    client, db_path = env
    resp = upload(client, DAY1, CSV1)
    assert "请先设置零相位时刻和周期" in resp.get_data(as_text=True)
    cov, exp, _ = dump(db_path)
    assert cov == [] and exp == []


def test_config_locked_while_data_exists(env):
    client, db_path = env
    setup_three_days(client)
    resp = set_config(client, t0="5", period="2000")
    assert "请先清空" in resp.get_data(as_text=True)
    # 清空后可重设
    client.post("/reset")
    resp = set_config(client, t0="5", period="2000")
    assert "参数已保存" in resp.get_data(as_text=True)


def test_last_op_stats_shown_on_home(env):
    client, db_path = env
    setup_three_days(client)
    upload(client, DAY1, CSV1_NEW)
    query(client, excl1=DAY2)
    text = client.get("/").get_data(as_text=True)
    assert "最近一次查询：读取全局覆盖记录 4 条" in text
    assert f"（排除 {DAY2}）" in text
    assert "最近一次更正" in text and "写入全局覆盖记录 8 条" in text


def test_huge_period_no_per_second_expansion(env):
    """P=10^12：若逐秒展开将挂起；区间算法应瞬间完成。"""
    client, db_path = env
    set_config(client, t0="0", period=str(10**12))
    upload(client, "2026-11-23", "0,3600\n")
    cov, _, _ = dump(db_path)
    assert len(cov) == 1 and cov[0][2] == 1
    text = query(client).get_data(as_text=True)
    assert "本次查询读取全局覆盖记录 1 条" in text
    # 只上传了一天，所有相位覆盖都不足两日 -> 全周期 [0, 10^12)
    assert "[0, 1,000,000,000,000)" in text
