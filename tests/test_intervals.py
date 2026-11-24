"""区间算术单元测试：折相（含跨周期）、同日去重、区域重算、不足两日查询。"""

from app.intervals import (
    clip_outside,
    day_phase_set,
    fold_interval,
    insufficient_intervals,
    normalize,
    recompute_region,
)


def test_normalize_merges_overlap_and_touch():
    assert normalize([(5, 8), (0, 3), (3, 5), (10, 12), (7, 9)]) == [(0, 9), (10, 12)]
    assert normalize([]) == []
    assert normalize([(4, 4), (1, 2)]) == [(1, 2)]  # 空区间丢弃


def test_fold_no_wrap():
    assert fold_interval(100, 250, 0, 1000) == [(100, 250)]


def test_fold_wrap_crosses_period_boundary():
    # 跨周期：绝对 [900, 1200) 折成相位 [900,1000) ∪ [0,200)
    assert fold_interval(900, 1200, 0, 1000) == [(900, 1000), (0, 200)]


def test_fold_exact_period_and_longer_cover_full_circle():
    assert fold_interval(7, 7 + 1000, 0, 1000) == [(0, 1000)]
    assert fold_interval(7, 7 + 2500, 0, 1000) == [(0, 1000)]


def test_fold_exact_boundary_no_wrap_piece():
    # 终点恰好在周期边界：不应产生 [0,0) 碎片
    assert fold_interval(900, 1000, 0, 1000) == [(900, 1000)]


def test_fold_with_t0_offset():
    # t0=100：绝对 [100,200) 是相位 [0,100)；绝对 [50,150) 跨过 t0+1000
    assert fold_interval(100, 200, 100, 1000) == [(0, 100)]
    assert fold_interval(50, 150, 100, 1000) == [(950, 1000), (0, 50)]


def test_fold_huge_numbers_no_per_second_expansion():
    p = 10**15
    assert fold_interval(10**15 - 5, 10**15 + 5, 0, p) == [(p - 5, p), (0, 5)]
    assert fold_interval(0, 3 * p, 0, p) == [(0, p)]


def test_day_set_counts_phase_once_per_day():
    # 同一日两段曝光（相隔一个周期）折到同一相位 -> 并集只算一次
    assert day_phase_set([(0, 100), (3600, 3700)], 0, 0, 3600) == [(0, 100)]
    # 同一日内重叠曝光同样只算一次
    assert day_phase_set([(0, 100), (50, 150)], 0, 0, 3600) == [(0, 150)]


def test_recompute_region_replace():
    # 全局：day1 [0,300)，day2 [100,400) -> (0,100,1)(100,300,2)(300,400,1)
    cover = [(0, 100, 1), (100, 300, 2), (300, 400, 1)]
    old_set = [(0, 300)]
    new_set = [(150, 250)]
    region = normalize(old_set + new_set)
    assert recompute_region(cover, old_set, new_set, region) == [
        (100, 150, 1),
        (150, 250, 2),
        (250, 300, 1),
    ]


def test_recompute_region_remove_day():
    cover = [(0, 100, 1), (100, 300, 2), (300, 400, 1)]
    region = [(0, 300)]
    # 撤下 day1（[0,300)）：[0,100) 归零消失，[100,300) 降为 1
    assert recompute_region(cover, [(0, 300)], [], region) == [(100, 300, 1)]


def test_recompute_region_add_day():
    region = [(0, 300)]
    assert recompute_region([], [], [(0, 300)], region) == [(0, 300, 1)]


def test_clip_outside():
    region = [(10, 20), (30, 40)]
    assert clip_outside(0, 50, region) == [(0, 10), (20, 30), (40, 50)]
    assert clip_outside(10, 40, region) == [(20, 30)]  # 区域之间的缝隙在区域之外
    assert clip_outside(10, 20, region) == []  # 完全落在区域内
    assert clip_outside(5, 15, region) == [(5, 10)]
    assert clip_outside(35, 45, region) == [(40, 45)]
    assert clip_outside(0, 100, []) == [(0, 100)]


def test_insufficient_without_exclusion():
    cover = [(0, 100, 1), (100, 300, 2), (300, 400, 1)]
    # [0,100) 一天、[100,300) 两天、[300,400) 一天、其余零天
    assert insufficient_intervals(cover, [], 1000) == [(0, 100), (300, 1000)]


def test_insufficient_with_exclusion():
    cover = [(0, 100, 1), (100, 300, 2), (300, 400, 1)]
    # 排除贡献了 [100,400) 的一日后，全周期都不足两日
    assert insufficient_intervals(cover, [[(100, 400)]], 1000) == [(0, 1000)]
    # 排除不覆盖任何已覆盖相位的一日：结果不变
    assert insufficient_intervals(cover, [[(500, 600)]], 1000) == [(0, 100), (300, 1000)]


def test_insufficient_huge_coordinates_no_per_second():
    p = 10**15
    cover = [(10**14, 2 * 10**14, 3)]
    assert insufficient_intervals(cover, [], p) == [(0, 10**14), (2 * 10**14, p)]


def test_insufficient_two_exclusions_and_empty_coverage():
    assert insufficient_intervals([], [], 500) == [(0, 500)]
    cover = [(0, 500, 2)]
    # 两日都被临时排除 -> 全周期不足
    assert insufficient_intervals(cover, [[(0, 500)], [(0, 500)]], 500) == [(0, 500)]
    # 只排除其一 -> 仍剩一天，依然不足两日（阈值是 <2）
    assert insufficient_intervals(cover, [[(0, 500)]], 500) == [(0, 500)]
    cover3 = [(0, 500, 3)]
    assert insufficient_intervals(cover3, [[(0, 500)]], 500) == []
