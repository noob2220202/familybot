import time
from datetime import datetime

import httpx

from . import config, db

SPORTS = {
    "football": ("⚽", "축구", "https://v3.football.api-sports.io", "fixtures"),
    "basketball": ("🏀", "농구", "https://v1.basketball.api-sports.io", "games"),
    "baseball": ("⚾", "야구", "https://v1.baseball.api-sports.io", "games"),
    "volleyball": ("🏐", "배구", "https://v1.volleyball.api-sports.io", "games"),
}

UPCOMING = {"NS", "TBD"}
FINISHED = {"FT", "AET", "PEN", "AOT", "AP", "AW"}
OFF = {"PST": "연기", "CANC": "취소", "ABD": "중단", "SUSP": "중단", "INTR": "중단", "AWD": "몰수", "WO": "부전승"}
LIVE_LABELS = {"HT": "하프타임", "BT": "휴식", "OT": "연장", "ET": "연장", "P": "승부차기", "LIVE": "진행 중"}

_cache: dict[tuple[str, str], tuple[float, list]] = {}


class SportsError(Exception):
    pass


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
        raise SportsError("경기 정보를 불러오지 못했습니다.") from e
    if body.get("errors"):
        errors = body["errors"]
        detail = "; ".join(map(str, errors.values() if isinstance(errors, dict) else errors))
        raise SportsError(f"경기 API 오류: {detail}")
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
        state, label = "finished", "종료"
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


async def today_games(sport: str, force: bool = False) -> list[tuple[str, list[dict]]]:
    day = db.today()
    key = (sport, day)
    hit = _cache.get(key)
    ttl = max(30, int(db.setting("sports_cache_sec")))
    if hit and not force and time.monotonic() - hit[0] < ttl:
        games = hit[1]
    else:
        raw = await _get(sport, SPORTS[sport][3], {"date": day, "timezone": "Asia/Seoul"})
        games = sorted((_normalize(sport, g) for g in raw), key=lambda g: g["ts"])
        for k in [k for k in _cache if k[1] != day]:
            del _cache[k]
        _cache[key] = (time.monotonic(), games)
    out = []
    for league in db.setting("leagues").get(sport, []):
        matched = [g for g in games if g["league_id"] == league["id"]]
        if matched:
            out.append((league["name"], matched))
    return out


async def search_leagues(sport: str, query: str) -> list[dict]:
    rows = await _get(sport, "leagues", {"search": query})
    out = []
    for r in rows:
        league = r.get("league", r)
        country = r.get("country") or {}
        out.append({"id": league["id"], "name": league["name"], "country": country.get("name", "")})
    return out
