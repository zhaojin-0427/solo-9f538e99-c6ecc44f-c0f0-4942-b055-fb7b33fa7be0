"""SQLite 连接与模式。单星：零相位 t0（UTC 整秒）+ 固定周期 period（正整数秒）。"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    t0      INTEGER NOT NULL,                 -- 零相位 UTC 整秒时刻
    period  INTEGER NOT NULL CHECK (period > 0)
);
CREATE TABLE IF NOT EXISTS exposures (
    day       TEXT NOT NULL,                  -- UTC 日 'YYYY-MM-DD'
    start_sec INTEGER NOT NULL CHECK (start_sec >= 0),
    end_sec   INTEGER NOT NULL CHECK (end_sec <= 86400),
    PRIMARY KEY (day, start_sec, end_sec),
    CHECK (start_sec < end_sec)               -- 半开区间 [start_sec, end_sec)
);
CREATE TABLE IF NOT EXISTS coverage (          -- 全局覆盖：相位圈 [0, period) 上的不相交区间
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ph_start INTEGER NOT NULL CHECK (ph_start >= 0),
    ph_end   INTEGER NOT NULL,
    days     INTEGER NOT NULL CHECK (days > 0), -- 覆盖该区间的不同 UTC 日数
    CHECK (ph_start < ph_end)
);
CREATE TABLE IF NOT EXISTS day_versions (           -- 每个 UTC 日的数据版本（单调递增）
    day     TEXT PRIMARY KEY,                        -- 'YYYY-MM-DD'
    version INTEGER NOT NULL CHECK (version > 0)
);
CREATE TABLE IF NOT EXISTS plans (                  -- 最少观测日计划，单行（id = 1）
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    ph_start    INTEGER NOT NULL,                   -- 目标半开相位区间 [ph_start, ph_end)
    ph_end      INTEGER NOT NULL,
    need_days   INTEGER NOT NULL CHECK (need_days IN (1, 2)),
    chosen_days TEXT NOT NULL,                      -- JSON：选中的 UTC 日（有序）
    candidates  TEXT NOT NULL,                      -- JSON：候选日及其预览时版本
    contributions TEXT NOT NULL,                   -- JSON：各选中日在目标区间内的贡献区间
    saved_at    INTEGER NOT NULL                    -- 保存时刻（UTC 整秒）
);
CREATE TABLE IF NOT EXISTS plan_preview (          -- 最近一次计划预览的快照（单行，scratch）
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    preview_id  TEXT NOT NULL,                      -- 与会话中的 id 核对
    payload     TEXT NOT NULL,                      -- JSON：预览快照
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coverage_range ON coverage (ph_start, ph_end);
"""


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None)  # 自动提交，事务显式 BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
