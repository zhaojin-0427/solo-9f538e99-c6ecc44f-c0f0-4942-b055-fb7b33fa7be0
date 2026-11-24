"""贪心对拍：随机小实例上用暴力枚举（全部子集）验证 find_min_days 的
最优性（个数最少）与可行性（每点覆盖数 >= k），k=1、k=2 均覆盖。"""

import random

from app.planning import find_min_days


def _brute_force(candidates, lo, hi, k):
    """枚举全部子集，返回最少可行解的大小；无解返回 None。"""
    sets = [iv for _, iv in candidates]
    n = len(sets)
    best = None
    for mask in range(1, 1 << n):
        picked = [sets[i] for i in range(n) if mask & (1 << i)]
        # 端点扫描核对可行性：只检查所有端点切出的常值段
        pts = {lo, hi}
        for iv in picked:
            for s, e in iv:
                if lo < s < hi:
                    pts.add(s)
                if lo < e < hi:
                    pts.add(e)
        pts = sorted(pts)
        ok = True
        for a, b in zip(pts, pts[1:]):
            mid = a  # 半开段 [a, b) 内计数恒定，取左端点即可
            cnt = sum(1 for iv in picked if any(s <= mid < e for s, e in iv))
            if cnt < k:
                ok = False
                break
        if ok:
            size = mask.bit_count()
            if best is None or size < best:
                best = size
    return best


def _feasible(chosen_sets, lo, hi, k):
    """独立复核贪心解在每个常值段上都满足 k 重覆盖。"""
    pts = {lo, hi}
    for iv in chosen_sets:
        for s, e in iv:
            if lo < s < hi:
                pts.add(s)
            if lo < e < hi:
                pts.add(e)
    for a, b in zip(sorted(pts), sorted(pts)[1:]):
        cnt = sum(1 for iv in chosen_sets if any(s <= a < e for s, e in iv))
        if cnt < k:
            return False
    return True


def test_greedy_matches_brute_force_random():
    rng = random.Random(20261124)
    trials = 300
    for _ in range(trials):
        lo, hi = 0, rng.choice([20, 50, 100])
        k = rng.choice([1, 2])
        n = rng.randint(0, 6)
        candidates = []
        for i in range(n):
            iv = []
            for _ in range(rng.randint(1, 2)):
                a = rng.randint(lo, hi - 1)
                b = rng.randint(a + 1, hi)
                iv.append((a, b))
            iv.sort()
            merged = []
            for s, e in iv:
                if merged and s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
            candidates.append((f"D{i}", merged))
        found = find_min_days(candidates, lo, hi, k)
        expect = _brute_force(candidates, lo, hi, k)
        if expect is None:
            assert found is None, (candidates, k, found)
        else:
            assert found is not None, (candidates, k)
            chosen, contrib = found
            assert len(chosen) == expect, (candidates, k, chosen, expect)
            assert _feasible(
                [contrib[d] for d in chosen], lo, hi, k
            ), (candidates, k, chosen)


def test_greedy_with_clipped_target_subrange():
    # 目标区间是候选区间的子范围：裁剪后再求解，与暴力结果一致
    rng = random.Random(77)
    for _ in range(100):
        lo, hi = 10, 90
        k = rng.choice([1, 2])
        candidates = []
        for i in range(rng.randint(1, 5)):
            a = rng.randint(0, 60)
            b = rng.randint(a + 1, 100)
            candidates.append((f"D{i}", [(a, b)]))
        from app.planning import clip_intervals

        clipped = [(d, clip_intervals(iv, lo, hi)) for d, iv in candidates]
        found = find_min_days(clipped, lo, hi, k)
        expect = _brute_force(clipped, lo, hi, k)
        if expect is None:
            assert found is None
        else:
            assert len(found[0]) == expect
