import json
import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    tickets INTEGER NOT NULL DEFAULT 0,
    msg_total INTEGER NOT NULL DEFAULT 0,
    attend_last TEXT,
    attend_streak INTEGER NOT NULL DEFAULT 0,
    attend_total INTEGER NOT NULL DEFAULT 0,
    joined_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS activity (
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    cnt INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id, hour)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    kind TEXT NOT NULL,
    delta INTEGER NOT NULL,
    balance INTEGER NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id, id);
CREATE TABLE IF NOT EXISTS attendance (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS member_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

PERIODS = {"day": "일간", "week": "주간", "month": "월간", "all": "전체"}
KINDS = {"points", "tickets"}

DEFAULTS = {
    "app_name": "FAMILY",
    "ranking_period": "week",
    "attend_points": 100,
    "roulette_enabled": True,
    "roulette_slots": [
        {"points": 0, "weight": 40},
        {"points": 100, "weight": 30},
        {"points": 300, "weight": 15},
        {"points": 500, "weight": 10},
        {"points": 1000, "weight": 4},
        {"points": 5000, "weight": 1},
    ],
    "sports_cache_sec": 180,
    "leagues": {
        "football": [
            {"id": 39, "name": "프리미어리그"},
            {"id": 140, "name": "라리가"},
            {"id": 78, "name": "분데스리가"},
            {"id": 135, "name": "세리에 A"},
            {"id": 61, "name": "리그 1"},
            {"id": 2, "name": "챔피언스리그"},
            {"id": 292, "name": "K리그1"},
        ],
        "basketball": [{"id": 12, "name": "NBA"}, {"id": 91, "name": "KBL"}],
        "baseball": [{"id": 1, "name": "MLB"}, {"id": 5, "name": "KBO"}, {"id": 2, "name": "NPB"}],
        "volleyball": [],
    },
}

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(SCHEMA)
        _local.c = c
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def now() -> datetime:
    return datetime.now(config.TZ)


def today() -> str:
    return now().date().isoformat()


def stamp() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def period_start(period: str) -> str | None:
    d = now().date()
    if period == "day":
        return d.isoformat()
    if period == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if period == "month":
        return d.replace(day=1).isoformat()
    return None


def settings() -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    for r in conn().execute("SELECT key, value FROM settings"):
        if r["key"] in out:
            out[r["key"]] = json.loads(r["value"])
    return out


def setting(key: str):
    r = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else DEFAULTS[key]


def save_settings(values: dict) -> None:
    with tx() as c:
        for k, v in values.items():
            if k in DEFAULTS:
                c.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v, ensure_ascii=False)),
                )


def touch_user(user_id: int, username: str | None, name: str, count: bool = False) -> None:
    n = now()
    ts = n.strftime("%Y-%m-%d %H:%M:%S")
    with tx() as c:
        c.execute(
            "INSERT INTO users(user_id, username, name, joined_at, last_seen) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name, last_seen=excluded.last_seen",
            (user_id, username, name, ts, ts),
        )
        if count:
            c.execute("UPDATE users SET msg_total=msg_total+1 WHERE user_id=?", (user_id,))
            c.execute(
                "INSERT INTO activity(day, user_id, hour, cnt) VALUES(?, ?, ?, 1) "
                "ON CONFLICT(day, user_id, hour) DO UPDATE SET cnt=cnt+1",
                (n.date().isoformat(), user_id, n.hour),
            )


def get_user(user_id: int):
    return conn().execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def find_user(token: str):
    token = token.strip()
    if token.startswith("@"):
        return conn().execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (token[1:],)).fetchone()
    if token.isdigit():
        return get_user(int(token))
    return None


def _apply(c, user_id: int, kind: str, delta: int, reason: str, actor: str) -> tuple[int, int]:
    cur = c.execute(f"SELECT {kind} FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
    new = max(0, cur + delta)
    applied = new - cur
    c.execute(f"UPDATE users SET {kind}=? WHERE user_id=?", (new, user_id))
    if applied:
        c.execute(
            "INSERT INTO logs(ts, user_id, kind, delta, balance, reason, actor) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (stamp(), user_id, kind, applied, new, reason, actor),
        )
    return applied, new


def change(user_id: int, kind: str, delta: int, reason: str, actor: str) -> tuple[int, int]:
    assert kind in KINDS
    with tx() as c:
        return _apply(c, user_id, kind, delta, reason, actor)


def reset_all(kind: str, actor: str) -> int:
    with tx() as c:
        if kind == "chat":
            n = c.execute("SELECT COUNT(*) FROM users WHERE msg_total>0").fetchone()[0]
            c.execute("UPDATE users SET msg_total=0")
            c.execute("DELETE FROM activity")
        else:
            assert kind in KINDS
            n = c.execute(f"SELECT COUNT(*) FROM users WHERE {kind}>0").fetchone()[0]
            c.execute(f"UPDATE users SET {kind}=0")
        c.execute(
            "INSERT INTO logs(ts, user_id, kind, delta, balance, reason, actor) VALUES(?, NULL, ?, 0, 0, 'reset', ?)",
            (stamp(), kind, actor),
        )
        return n


def attend(user_id: int) -> dict | None:
    d = now().date()
    with tx() as c:
        u = c.execute("SELECT attend_last, attend_streak FROM users WHERE user_id=?", (user_id,)).fetchone()
        if u["attend_last"] == d.isoformat():
            return None
        streak = u["attend_streak"] + 1 if u["attend_last"] == (d - timedelta(days=1)).isoformat() else 1
        c.execute(
            "UPDATE users SET attend_last=?, attend_streak=?, attend_total=attend_total+1 WHERE user_id=?",
            (d.isoformat(), streak, user_id),
        )
        c.execute("INSERT OR IGNORE INTO attendance(user_id, day) VALUES(?, ?)", (user_id, d.isoformat()))
        reward = int(setting("attend_points"))
        _, balance = _apply(c, user_id, "points", reward, "attend", "출석")
        total = c.execute("SELECT attend_total FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
        return {"reward": reward, "streak": streak, "total": total, "balance": balance}


def _counts(period: str) -> tuple[str, tuple]:
    start = period_start(period)
    if start is None:
        return "SELECT user_id, msg_total AS c FROM users WHERE msg_total>0", ()
    return "SELECT user_id, SUM(cnt) AS c FROM activity WHERE day>=? GROUP BY user_id", (start,)


def ranking(period: str, offset: int = 0, limit: int = 10) -> tuple[list, int]:
    sub, args = _counts(period)
    c = conn()
    total = c.execute(f"SELECT COUNT(*) FROM ({sub})", args).fetchone()[0]
    rows = c.execute(
        f"SELECT u.user_id, u.name, t.c FROM ({sub}) t JOIN users u USING(user_id) "
        "ORDER BY t.c DESC, u.user_id LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    return rows, total


def rank_of(user_id: int, period: str) -> tuple[int | None, int, int]:
    sub, args = _counts(period)
    c = conn()
    total = c.execute(f"SELECT COUNT(*) FROM ({sub})", args).fetchone()[0]
    mine = c.execute(f"SELECT c FROM ({sub}) WHERE user_id=?", (*args, user_id)).fetchone()
    if not mine:
        return None, 0, total
    above = c.execute(f"SELECT COUNT(*) FROM ({sub}) WHERE c>?", (*args, mine["c"])).fetchone()[0]
    return above + 1, mine["c"], total


def streak(user) -> int:
    if not user["attend_last"]:
        return 0
    gap = (now().date() - date.fromisoformat(user["attend_last"])).days
    return user["attend_streak"] if gap <= 1 else 0


def user_activity(user_id: int, days: int = 14) -> list[dict]:
    d = now().date()
    start = d - timedelta(days=days - 1)
    counts = {
        r["day"]: r["n"]
        for r in conn().execute(
            "SELECT day, SUM(cnt) AS n FROM activity WHERE user_id=? AND day>=? GROUP BY day",
            (user_id, start.isoformat()),
        )
    }
    out = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        out.append({"day": day, "count": counts.get(day, 0)})
    return out


def attend_days(user_id: int) -> list[str]:
    start = now().date().replace(day=1).isoformat()
    rows = conn().execute("SELECT day FROM attendance WHERE user_id=? AND day>=? ORDER BY day", (user_id, start))
    return [r["day"] for r in rows]


def recent_wins(limit: int = 10) -> list[dict]:
    rows = conn().execute(
        "SELECT u.name, l.delta AS points, l.ts FROM logs l JOIN users u USING(user_id) "
        "WHERE l.reason='roulette' AND l.kind='points' AND l.delta>0 ORDER BY l.id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


def spin(user_id: int) -> dict | None:
    slots = [s for s in setting("roulette_slots")]
    weights = [max(0, int(s["weight"])) for s in slots]
    if not slots or not sum(weights):
        return None
    with tx() as c:
        used = c.execute("UPDATE users SET tickets=tickets-1 WHERE user_id=? AND tickets>0", (user_id,)).rowcount
        if not used:
            return None
        index = random.SystemRandom().choices(range(len(slots)), weights)[0]
        prize = int(slots[index]["points"])
        tickets = c.execute("SELECT tickets FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
        c.execute(
            "INSERT INTO logs(ts, user_id, kind, delta, balance, reason, actor) VALUES(?, ?, 'tickets', -1, ?, 'roulette', '룰렛')",
            (stamp(), user_id, tickets),
        )
        _, balance = _apply(c, user_id, "points", prize, "roulette", "룰렛")
        return {"index": index, "points": prize, "tickets": tickets, "balance": balance}


def member_event(user_id: int, event: str) -> None:
    conn().execute("INSERT INTO member_events(day, user_id, event) VALUES(?, ?, ?)", (today(), user_id, event))


def user_logs(user_id: int, limit: int = 20) -> list:
    return conn().execute(
        "SELECT ts, kind, delta, balance, reason FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()


def stats(days: int = 30) -> dict:
    c = conn()
    d = now().date()
    start = (d - timedelta(days=days - 1)).isoformat()
    week = (d - timedelta(days=6)).isoformat()
    by_day = {
        r["day"]: r
        for r in c.execute(
            "SELECT day, SUM(cnt) AS messages, COUNT(DISTINCT user_id) AS users FROM activity WHERE day>=? GROUP BY day",
            (start,),
        )
    }
    events = {
        (r["day"], r["event"]): r["n"]
        for r in c.execute(
            "SELECT day, event, COUNT(*) AS n FROM member_events WHERE day>=? GROUP BY day, event", (start,)
        )
    }
    daily = []
    for i in range(days):
        day = (date.fromisoformat(start) + timedelta(days=i)).isoformat()
        r = by_day.get(day)
        daily.append(
            {
                "day": day,
                "messages": r["messages"] if r else 0,
                "users": r["users"] if r else 0,
                "joins": events.get((day, "join"), 0),
                "leaves": events.get((day, "leave"), 0),
            }
        )
    hours = [0] * 24
    for r in c.execute("SELECT hour, SUM(cnt) AS n FROM activity WHERE day>=? GROUP BY hour", (start,)):
        hours[r["hour"]] = r["n"]
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0
    return {
        "daily": daily,
        "hourly": hours,
        "totals": {
            "members": one("SELECT COUNT(*) FROM users"),
            "messages_today": daily[-1]["messages"],
            "active_today": daily[-1]["users"],
            "messages_week": one("SELECT SUM(cnt) FROM activity WHERE day>=?", week),
            "active_week": one("SELECT COUNT(DISTINCT user_id) FROM activity WHERE day>=?", week),
            "messages_all": one("SELECT SUM(msg_total) FROM users"),
            "points": one("SELECT SUM(points) FROM users"),
            "tickets": one("SELECT SUM(tickets) FROM users"),
        },
    }
