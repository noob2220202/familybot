import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx

from . import config, db

log = logging.getLogger("familybot.sports")

SPORTS = {
    "football": ("⚽", "축구", "https://v3.football.api-sports.io", "fixtures"),
    "basketball": ("🏀", "농구", "https://v1.basketball.api-sports.io", "games"),
    "baseball": ("⚾", "야구", "https://v1.baseball.api-sports.io", "games"),
    "volleyball": ("🏐", "배구", "https://v1.volleyball.api-sports.io", "games"),
}

UPCOMING = {"NS", "TBD"}
FINISHED = {"FT": "", "AET": "연장", "PEN": "승부차기", "AOT": "연장", "AP": "승부차기", "AW": ""}
OFF = {"PST": "연기", "POST": "연기", "CANC": "취소", "ABD": "중단", "SUSP": "중단", "INTR": "중단", "AWD": "몰수", "WO": "부전승"}
LIVE_LABELS = {
    "1H": "전반",
    "2H": "후반",
    "HT": "하프타임",
    "BT": "휴식",
    "OT": "연장",
    "ET": "연장",
    "P": "승부차기",
    "INT": "중단",
    "LIVE": "진행 중",
}
ERRORS = {
    "token": "경기 API 키가 올바르지 않습니다.",
    "requests": "오늘의 경기 API 요청 한도를 모두 사용했습니다.",
    "rateLimit": "요청이 많습니다. 잠시 후 다시 시도해 주세요.",
    "plan": "현재 API 요금제로는 조회할 수 없습니다.",
}

_cache: dict[tuple[str, str], tuple[float, list | Exception, datetime]] = {}
_locks: dict[str, asyncio.Lock] = {}
_leagues: dict[str, list] = {}


class SportsError(Exception):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


async def _get(sport: str, path: str, params: dict) -> list:
    if not config.SPORTS_API_KEY:
        raise SportsError("경기 API 키가 설정되지 않았습니다.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SPORTS[sport][2]}/{path}", params=params, headers={"x-apisports-key": config.SPORTS_API_KEY}
            )
            r.raise_for_status()
            body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("%s/%s: %r", sport, path, e)
        raise SportsError("경기 정보를 불러오지 못했습니다.") from e
    errors = body.get("errors")
    if errors:
        detail = "; ".join(map(str, errors.values() if isinstance(errors, dict) else errors))
        log.warning("%s/%s: %s", sport, path, detail)
        key = next(iter(errors), "") if isinstance(errors, dict) else ""
        raise SportsError(ERRORS.get(key, "경기 정보를 불러오지 못했습니다."), f"경기 API 오류: {detail}")
    return body.get("response", [])


def _score(v):
    return v.get("total") if isinstance(v, dict) else v


def _live_label(sport: str, short: str, elapsed) -> str:
    if sport == "football" and elapsed and short in {"1H", "2H", "ET"}:
        return f"{elapsed}'"
    if short in LIVE_LABELS:
        return LIVE_LABELS[short]
    digits = "".join(ch for ch in short if ch.isdigit())
    if digits:
        unit = {"Q": "쿼터", "I": "회", "S": "세트"}.get(short[0])
        if unit:
            return f"{digits}{unit}"
    return short


def _normalize(sport: str, item: dict) -> dict:
    if sport == "football":
        head, scores = item["fixture"], item["goals"]
    else:
        head, scores = item, item["scores"]
    status = head["status"]
    short = status.get("short") or ""
    if short in UPCOMING:
        state, label = "upcoming", ""
    elif short in FINISHED:
        state, label = "finished", FINISHED[short]
    elif short in OFF:
        state, label = "off", OFF[short]
    else:
        state, label = "live", _live_label(sport, short, status.get("elapsed"))
    return {
        "league_id": item["league"]["id"],
        "ts": head["timestamp"],
        "time": datetime.fromtimestamp(head["timestamp"], config.TZ).strftime("%H:%M"),
        "state": state,
        "label": label,
        "home": item["teams"]["home"]["name"],
        "away": item["teams"]["away"]["name"],
        "home_score": _score(scores.get("home")),
        "away_score": _score(scores.get("away")),
    }


def _soon(g: dict) -> bool:
    return g["state"] == "live" or g["state"] == "upcoming" and -10800 < g["ts"] - time.time() < 900


async def _day(sport: str, day: str) -> tuple[list[dict], datetime]:
    key = (sport, day)
    async with _locks.setdefault(sport, asyncio.Lock()):
        hit = _cache.get(key)
        if not hit or time.monotonic() >= hit[0]:
            try:
                raw = await _get(sport, SPORTS[sport][3], {"date": day, "timezone": "Asia/Seoul"})
            except SportsError as e:
                hit = (time.monotonic() + 30, e, db.now())
            else:
                games = sorted((_normalize(sport, g) for g in raw), key=lambda g: g["ts"])
                ids = {x["id"] for x in db.setting("leagues").get(sport, [])}
                ttl = max(30, int(db.setting("sports_cache_sec")))
                if not any(g["league_id"] in ids and _soon(g) for g in games):
                    ttl = max(ttl, 900)
                hit = (time.monotonic() + ttl, games, db.now())
            _cache[key] = hit
    if isinstance(hit[1], Exception):
        raise hit[1]
    return hit[1], hit[2]


async def today_games(sport: str) -> tuple[list[tuple[str, list[dict]]], datetime]:
    now = db.now()
    day, prev = now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()
    for k in [k for k in _cache if k[1] < prev]:
        del _cache[k]
    games, at = await _day(sport, day)
    if now.hour < 6:
        try:
            games = [g for g in (await _day(sport, prev))[0] if g["state"] == "live"] + games
        except SportsError:
            pass
    out = []
    for league in db.setting("leagues").get(sport, []):
        matched = [g for g in games if g["league_id"] == league["id"]]
        if matched:
            out.append((league["name"], matched))
    return out, at


async def search_leagues(sport: str, query: str) -> list[dict]:
    flat = sport != "football"
    if flat and sport not in _leagues:
        _leagues[sport] = await _get(sport, "leagues", {})
    rows = _leagues[sport] if flat else await _get(sport, "leagues", {"search": query})
    q = query.lower().replace("-", " ")
    out = []
    for r in rows:
        league = r.get("league", r)
        country = (r.get("country") or {}).get("name") or ""
        if not flat or q in f"{league['name']} {country}".lower().replace("-", " "):
            out.append({"id": int(league["id"]), "name": str(league["name"]), "country": str(country)})
    return out
