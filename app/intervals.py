"""半开区间 [s, e) 的区间算术。

所有坐标均为整数秒。本模块任何函数都不按秒展开区间，
代价只取决于区间条数，与区间长度无关。
"""

from bisect import bisect_right


def normalize(intervals):
    """合并重叠或首尾相接的半开区间，返回升序不相交的 [(s, e), ...]。"""
    iv = sorted((int(s), int(e)) for s, e in intervals if e > s)
    out = []
    for s, e in iv:
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def fold_interval(start, end, t0, period):
    """把绝对秒区间 [start, end) 折进相位圈 [0, period)。

    返回 0~2 个相位区间；区间长度 >= period 时覆盖整个相位圈。
    """
    if end <= start:
        return []
    length = end - start
    if length >= period:
        return [(0, period)]
    a = (start - t0) % period
    b = a + length
    if b <= period:
        return [(a, b)]
    return [(a, period), (0, b - period)]


def day_phase_set(exposures, day_start, t0, period):
    """把某日全部曝光 [(start_sec, end_sec), ...]（当日秒）折相后取并集。

    同一日对同一相位只计一次：返回的是并集（集合），不是多重集。
    """
    iv = []
    for s, e in exposures:
        iv.extend(fold_interval(day_start + s, day_start + e, t0, period))
    return normalize(iv)


def clip_outside(s, e, region):
    """返回区间 [s, e) 落在规范化区域 region 之外的部分（半开区间列表）。"""
    parts = []
    cur = s
    for rs, re_ in region:
        if re_ <= cur:
            continue
        if rs >= e:
            break
        if rs > cur:
            parts.append((cur, rs))
        cur = max(cur, re_)
        if cur >= e:
            break
    if cur < e:
        parts.append((cur, e))
    return parts


def _finder(items):
    """对按起点升序、互不相交的 (s, e, ...) 列表，返回'覆盖点 x 的项'查询函数。"""
    starts = [it[0] for it in items]

    def at(x):
        i = bisect_right(starts, x) - 1
        if i >= 0 and items[i][1] > x:
            return items[i]
        return None

    return at


def recompute_region(cover, old_set, new_set, region):
    """在受影响区域 region 内重算覆盖计数（事件扫描，不逐秒展开）。

    cover:   与 region 相交的全局覆盖记录 [(s, e, days)]，可越出 region（内部裁剪）
    old_set: 旧当日相位集（normalize 后）
    new_set: 新当日相位集（normalize 后）
    region:  受影响区域 = normalize(old_set + new_set)
    返回 region 内新的覆盖记录 [(s, e, days)]，days > 0，相邻同计数已合并。
    """
    clipped = []
    for s, e, d in cover:
        for rs, re_ in region:
            lo = s if s > rs else rs
            hi = e if e < re_ else re_
            if lo < hi:
                clipped.append((lo, hi, d))
    clipped.sort()

    pts = set()
    for s, e, _ in clipped:
        pts.add(s)
        pts.add(e)
    for s, e in old_set:
        pts.add(s)
        pts.add(e)
    for s, e in new_set:
        pts.add(s)
        pts.add(e)
    for s, e in region:
        pts.add(s)
        pts.add(e)
    pts = sorted(pts)

    cov_at = _finder(clipped)
    old_at = _finder(old_set)
    new_at = _finder(new_set)

    out = []
    for a, b in zip(pts, pts[1:]):
        c = cov_at(a)
        base = c[2] if c else 0
        cnt = base - (1 if old_at(a) else 0) + (1 if new_at(a) else 0)
        if cnt < 0:
            raise ValueError("覆盖计数出现负值，数据不一致")
        if cnt > 0:
            if out and out[-1][1] == a and out[-1][2] == cnt:
                out[-1] = (out[-1][0], b, cnt)
            else:
                out.append((a, b, cnt))
    return out


def insufficient_intervals(cover, excluded_sets, period):
    """有效覆盖天数 < 2 的相位区间（半开、合并），不逐秒展开。

    cover:         全部全局覆盖记录 [(s, e, days)]
    excluded_sets: 临时排除日（<=2）各自的当日相位集
    有效天数 = 全局覆盖天数 - 覆盖该相位的排除日个数
    """
    pts = {0, period}
    for s, e, _ in cover:
        pts.add(s)
        pts.add(e)
    for es in excluded_sets:
        for s, e in es:
            pts.add(s)
            pts.add(e)
    pts = sorted(pts)

    cov_at = _finder(sorted(cover))
    excl_at = [_finder(es) for es in excluded_sets]

    out = []
    for a, b in zip(pts, pts[1:]):
        c = cov_at(a)
        eff = (c[2] if c else 0) - sum(1 for f in excl_at if f(a))
        if eff < 2:
            if out and out[-1][1] == a:
                out[-1] = (out[-1][0], b)
            else:
                out.append((a, b))
    return out
