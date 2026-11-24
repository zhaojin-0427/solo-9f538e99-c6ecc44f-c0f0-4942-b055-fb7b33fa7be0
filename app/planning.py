"""最少观测日计划：在相位区间 [lo, hi) 上求最少的候选日，使每个相位
至少被 k 个不同 UTC 日覆盖（k ∈ {1, 2}）。

只做区间事件扫描：所有判断都发生在候选日相位区间的端点上，
代价只取决于候选日数与区间条数，与区间长度（秒数）无关，不逐秒展开。

两种贪心（均为最优，规则因 k 不同而不同）：

k=1（经典最少区间覆盖）：
  1. 找已选区间未覆盖的最左位置 pos（端点扫描）；
  2. 在覆盖 pos 的未选候选区间中选右端点最远者；
  3. 重复。
  最优性（交换论证）：任一最优解在 pos 处必有区间 O 覆盖 pos，
  贪心所选 G 右端点不小于 O，用 G 替换 O 不会缩短右侧覆盖，解不增大。

k=2（最少双重区间覆盖）：
  1. 找覆盖数首次 < 2 的位置 pos，以及 pos 之前覆盖数曾达到 2 的
     连续前缀最远终点 reach；
  2. 在覆盖 pos、且起点不超过 reach（能接上已满足前缀）的未选候选区间中
     选右端点最远者；
  3. 重复。
  最优性（交换论证）：最优解在 pos 处需要两个可接续前缀的区间，
  贪心选其中右端点最远的 G，替换任一不优于 G 的区间不使右侧覆盖变差。
"""


def clip_intervals(intervals, lo, hi):
    """把规范化半开区间列表裁剪到 [lo, hi) 内；输入不相交，输出仍不相交。"""
    out = []
    for s, e in intervals:
        a, b = max(s, lo), min(e, hi)
        if a < b:
            out.append((a, b))
    return out


def _event_deltas(active_sets):
    """汇总各区间端点事件：{位置: 覆盖数增量}，同点事件合并。"""
    deltas = {}
    for iv in active_sets:
        for s, e in iv:
            deltas[s] = deltas.get(s, 0) + 1
            deltas[e] = deltas.get(e, 0) - 1
    return deltas


def _first_uncovered(active_sets, lo, hi):
    """k=1：返回已选区间在 [lo, hi) 上未覆盖的最左位置；全覆盖返回 None。"""
    deltas = _event_deltas(active_sets)
    cur = 0
    prev = lo
    for p in sorted(deltas):
        if p >= hi:
            break
        if p > prev and cur == 0:
            return prev
        cur += deltas[p]
        if p > prev:
            prev = p
    if prev < hi and cur == 0:
        return prev
    return None


def _first_undercovered_k2(active_sets, lo, hi):
    """k=2：返回 (pos, reach)；处处双重覆盖时返回 None。

    pos:   覆盖数首次不足 2 的位置（已满足前缀的右边界）；
    reach: pos 之前覆盖数 >= 2 的区域曾到达的最远终点，新选区间起点须 <= reach。
    """
    deltas = _event_deltas(active_sets)
    cur = 0
    prev = lo
    reach = lo
    for p in sorted(deltas):
        if p >= hi:
            break
        if p > prev:
            if cur < 2:
                return prev, reach
            if p > reach:
                reach = p
        cur += deltas[p]
        if p > prev:
            prev = p
    if prev < hi:
        if cur < 2:
            return prev, reach
        reach = hi
    return None


def find_min_days(candidates, lo, hi, k):
    """求覆盖 [lo, hi) 每个相位至少 k 日的最少候选日集合。

    candidates: [(day, 裁剪到 [lo, hi) 后的规范化相位区间), ...]，按候选顺序
    返回 (chosen, contributions)：
      chosen:        选中的日名列表（贪心选择顺序，即最少个数）
      contributions: {day: 该日落在 [lo, hi) 内的相位区间列表}
    无解返回 None。
    """
    if k not in (1, 2):
        raise ValueError("所需覆盖日数只支持 1 或 2")
    if not (0 <= lo < hi):
        raise ValueError("目标区间需满足 0 <= 起 < 止")
    usable = [(d, iv) for d, iv in candidates if iv]
    sets = [iv for _, iv in usable]
    chosen_idx = []
    chosen_set = set()
    active = []
    while True:
        if k == 1:
            pos = _first_uncovered(active, lo, hi)
            if pos is None:
                break
            reach = None
        else:
            found = _first_undercovered_k2(active, lo, hi)
            if found is None:
                break
            pos, reach = found
        best, best_end = -1, -1
        for idx, iv in enumerate(sets):
            if idx in chosen_set:
                continue
            for s, e in iv:  # 半开：覆盖点 pos 当且仅当 s <= pos < e
                if not (s <= pos < e):
                    continue
                if k == 2 and s > reach:  # k=2 须能接上已满足的双覆盖前缀
                    continue
                if e > best_end:  # 右端点最远；并列自然保留候选顺序靠前者
                    best_end, best = e, idx
                break  # 同一候选日的片段互不相交，至多一个覆盖 pos
        if best < 0:
            return None  # pos 处无可选候选日 -> 无解
        chosen_idx.append(best)
        chosen_set.add(best)
        active.append(sets[best])
    chosen = [usable[i][0] for i in chosen_idx]
    contributions = {usable[i][0]: sets[i] for i in chosen_idx}
    return chosen, contributions
