"""最少观测日贪心算法的单元测试：k=1/k=2 的最优性、裁剪、无解、
边界相接与超大坐标（只扫描端点，不随秒数增长）。"""

import time

import pytest

from app.planning import clip_intervals, find_min_days


def test_clip_intervals():
    assert clip_intervals([(0, 100), (150, 300)], 50, 200) == [(50, 100), (150, 200)]
    assert clip_intervals([(0, 100)], 200, 300) == []  # 完全不相交
    assert clip_intervals([(10, 20), (20, 30)], 0, 100) == [(10, 20), (20, 30)]
    assert clip_intervals([], 0, 10) == []


def test_k1_picks_fewest_longest_reaching():
    # [0,100)：A 到 60，B 到 100 -> 只选 B；贪心不会先选 A
    candidates = [("A", [(0, 60)]), ("B", [(0, 100)])]
    chosen, contrib = find_min_days(candidates, 0, 100, 1)
    assert chosen == ["B"]
    assert contrib == {"B": [(0, 100)]}


def test_k1_greedy_chain_is_optimal():
    # A[0,40) B[30,80) C[70,100)：B 不覆盖点 0，任何覆盖 [0,100) 的解都至少 3 日，
    # 贪心先选覆盖 0 且右端点最远的 A，再 B、C -> 3 日，达到下界。
    candidates = [("A", [(0, 40)]), ("B", [(30, 80)]), ("C", [(70, 100)])]
    chosen, _ = find_min_days(candidates, 0, 100, 1)
    assert chosen == ["A", "B", "C"]
    # 存在一日从 35 直达 100 时，A+E 两日即可（贪心识别最远右端点，不会多选 B、C）
    chosen2, _ = find_min_days(
        [("A", [(0, 40)]), ("B", [(30, 80)]), ("C", [(70, 100)]),
         ("D", [(0, 30)]), ("E", [(35, 100)])], 0, 100, 1
    )
    assert chosen2 == ["A", "E"]
    # 单个全覆盖区间 -> 只选它
    assert find_min_days(
        [("A", [(0, 40)]), ("Z", [(0, 100)])], 0, 100, 1
    )[0] == ["Z"]


def test_k2_requires_two_distinct_days_everywhere():
    # [0,100) 需两日覆盖；只有一日 -> 无解
    assert find_min_days([("A", [(0, 100)])], 0, 100, 2) is None
    # 两日全覆盖 -> 解为两日
    candidates = [("A", [(0, 100)]), ("B", [(0, 100)])]
    chosen, contrib = find_min_days(candidates, 0, 100, 2)
    assert sorted(chosen) == ["A", "B"]
    assert contrib["A"] == [(0, 100)] and contrib["B"] == [(0, 100)]


def test_k2_greedy_minimum_over_piecewise_sets():
    # A:[0,60) B:[40,100) C:[0,100)：A+B 在 [40,60) 有双覆盖、两端只有单覆盖，
    # 缺口需 C 补 -> 最少 3 日
    candidates = [("A", [(0, 60)]), ("B", [(40, 100)]), ("C", [(0, 100)])]
    chosen, _ = find_min_days(candidates, 0, 100, 2)
    assert sorted(chosen) == ["A", "B", "C"]


def test_k2_half_open_boundary_adjacency_counts():
    # A:[0,50) B:[50,100) C:[0,100)：点 50 处 A 已结束（半开），
    # 需 B+C 覆盖 [50,100)，[0,50) 需 A+C -> 最少 3 日
    candidates = [("A", [(0, 50)]), ("B", [(50, 100)]), ("C", [(0, 100)])]
    chosen, _ = find_min_days(candidates, 0, 100, 2)
    assert sorted(chosen) == ["A", "B", "C"]
    # 半开相接：[0,50)+[50,100) 一日即可单覆盖
    chosen1, _ = find_min_days(
        [("A", [(0, 50)]), ("B", [(50, 100)])], 0, 100, 1
    )
    assert sorted(chosen1) == ["A", "B"]


def test_no_solution_when_gap_exists():
    # [0,100) 中 (60,70) 无任何候选日覆盖 -> 无解（k=1 与 k=2 皆然）
    candidates = [("A", [(0, 60)]), ("B", [(70, 100)])]
    assert find_min_days(candidates, 0, 100, 1) is None
    assert find_min_days(candidates, 0, 100, 2) is None


def test_empty_and_invalid_inputs():
    assert find_min_days([], 0, 100, 1) is None
    # 与目标不相交的候选日等于不存在
    assert find_min_days([("A", [])], 0, 100, 1) is None
    with pytest.raises(ValueError):
        find_min_days([], 0, 100, 3)
    with pytest.raises(ValueError):
        find_min_days([], 100, 100, 1)


def test_huge_coordinates_cost_independent_of_seconds():
    # 区间长度 10^15 秒：逐秒展开会挂起；端点扫描应瞬时完成且结果正确
    p = 10**15
    candidates = [
        ("A", [(0, p // 2)]),
        ("B", [(p // 4, 3 * p // 4)]),
        ("C", [(p // 2, p)]),
    ]
    t0 = time.monotonic()
    chosen1, _ = find_min_days(candidates, 0, p, 1)
    k2 = find_min_days(candidates, 0, p, 2)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0
    # k=1：B 接不到 0，需 A 起步；A(->p/2) 后 C 补到 p -> A、C 两日
    assert sorted(chosen1) == ["A", "C"]
    # k=2：[0, p/4) 只有 A，(3p/4, p) 只有 C，而 [3p/4, p) 无第二日可补 -> 无解
    assert k2 is None
    # 加上全覆盖的 D 后 k=2 可解：D+A 覆盖 [0,p/2)，D+C 覆盖 [p/2,p) -> 3 日
    candidates2 = candidates + [("D", [(0, p)])]
    chosen2, _ = find_min_days(candidates2, 0, p, 2)
    assert sorted(chosen2) == ["A", "C", "D"]


def test_multi_interval_day_set():
    # 同一候选日多个不相交片段，扫描计数与选择均正确
    candidates = [
        ("A", [(0, 30), (70, 100)]),
        ("B", [(20, 80)]),
        ("C", [(0, 100)]),
    ]
    # k=1：C 一日全覆盖 -> 只选 C
    chosen, contrib = find_min_days(candidates, 0, 100, 1)
    assert chosen == ["C"]
    assert contrib["C"] == [(0, 100)]
    # k=2：C 之外，[0,20) 与 (80,100) 只有 A 能补 -> 必选 A；
    # [30,70) 选 B -> A、B、C 三日
    chosen2, contrib2 = find_min_days(candidates, 0, 100, 2)
    assert sorted(chosen2) == ["A", "B", "C"]
    assert contrib2["A"] == [(0, 30), (70, 100)]
