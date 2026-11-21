import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from . import config, db, web

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
web.mount_fonts(app)


def verify_init_data(init_data: str) -> dict | None:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(pairs.get("auth_date") or 0) > 86400:
        return None
    try:
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None


def member(x_init_data: str = Header("")):
    tg = verify_init_data(x_init_data)
    if not tg:
        raise HTTPException(401, "텔레그램에서 열어 주세요.")
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(403, "그룹방에서 채팅한 회원만 이용할 수 있습니다.")
    return user


def roulette_info(s: dict) -> dict:
    enabled = bool(s["roulette_enabled"])
    return {
        "enabled": enabled,
        "slots": [int(x["points"]) for x in s["roulette_slots"]],
        "recent": db.recent_wins(10) if enabled else [],
    }


@app.get("/api/me")
def me(user=Depends(member)):
    s = db.settings()
    period = s["ranking_period"]
    rank, count, total = db.rank_of(user["user_id"], period)
    return {
        "app_name": s["app_name"],
        "today": db.today(),
        "period": period,
        "user": {
            "id": user["user_id"],
            "name": user["name"],
            "username": user["username"],
            "points": user["points"],
            "tickets": user["tickets"],
            "messages": user["msg_total"],
            "attend_total": user["attend_total"],
            "attend_streak": db.streak(user),
            "attended_today": user["attend_last"] == db.today(),
            "rank": rank,
            "rank_count": count,
            "rank_total": total,
            "joined_at": user["joined_at"],
        },
        "activity": db.user_activity(user["user_id"], 14),
        "attend_days": db.attend_days(user["user_id"]),
        "roulette": roulette_info(s),
        "logs": [dict(r) for r in db.user_logs(user["user_id"])],
    }


@app.get("/api/stats")
def stats(user=Depends(member)):
    return db.stats(30)


@app.get("/api/ranking")
def ranking(period: str = "week", user=Depends(member)):
    if period not in db.PERIODS:
        raise HTTPException(400, "잘못된 기간입니다.")
    rows, total = db.ranking(period, 0, 50)
    rank, count, _ = db.rank_of(user["user_id"], period)
    return {
        "rows": [{"id": r["user_id"], "name": r["name"], "count": r["c"]} for r in rows],
        "total": total,
        "me": {"rank": rank, "count": count},
    }


@app.post("/api/spin")
def spin(user=Depends(member)):
    if not db.setting("roulette_enabled"):
        raise HTTPException(403, "룰렛이 비활성화되어 있습니다.")
    result = db.spin(user["user_id"])
    if not result:
        raise HTTPException(409, "룰렛 이용권이 없습니다.")
    return result


@app.get("/")
def index():
    return FileResponse(config.STATIC / "miniapp.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.MINIAPP_PORT, proxy_headers=True, forwarded_allow_ips="*")
