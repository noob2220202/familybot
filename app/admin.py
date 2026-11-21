import base64
import hashlib
import hmac
import time

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, db, sports

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

COOKIE = "fb_admin"
SESSION_TTL = 7 * 86400
ACTOR = "관리자 페이지"
SORTS = {"points": "points DESC", "tickets": "tickets DESC", "messages": "msg_total DESC", "recent": "last_seen DESC"}

_fails: dict[str, list[float]] = {}


def sign(expires: int) -> str:
    mac = hmac.new(config.SESSION_SECRET, str(expires).encode(), hashlib.sha256).digest()
    return f"{expires}.{base64.urlsafe_b64encode(mac).decode()}"


def authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE, "")
    expires = token.split(".")[0]
    return expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(sign(int(expires)), token)


def admin(request: Request):
    if not authed(request):
        raise HTTPException(401, "로그인이 필요합니다.")


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "로그인 시도가 너무 많습니다. 10분 뒤 다시 시도하세요.")
    ok = hmac.compare_digest(body.username.encode(), config.ADMIN_USERNAME.encode()) & hmac.compare_digest(
        body.password.encode(), config.ADMIN_PASSWORD.encode()
    )
    if not ok:
        _fails[ip] = recent + [time.time()]
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
    _fails.pop(ip, None)
    response.set_cookie(
        COOKIE,
        sign(int(time.time()) + SESSION_TTL),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/session")
def session(request: Request):
    return {"authed": authed(request)}


@app.get("/api/dashboard", dependencies=[Depends(admin)])
def dashboard():
    data = db.stats(30)
    period = db.setting("ranking_period")
    rows, _ = db.ranking(period, 0, 10)
    data["period"] = period
    data["ranking"] = [{"id": r["user_id"], "name": r["name"], "count": r["c"]} for r in rows]
    return data


@app.get("/api/users", dependencies=[Depends(admin)])
def users(q: str = "", sort: str = "points", page: int = 0):
    where, args = "", []
    q = q.strip().lstrip("@")
    if q:
        where = "WHERE name LIKE ? OR username LIKE ? OR CAST(user_id AS TEXT)=?"
        args = [f"%{q}%", f"%{q}%", q]
    c = db.conn()
    total = c.execute(f"SELECT COUNT(*) FROM users {where}", args).fetchone()[0]
    rows = c.execute(
        f"SELECT user_id, username, name, points, tickets, msg_total, attend_total, attend_streak, last_seen "
        f"FROM users {where} ORDER BY {SORTS.get(sort, SORTS['points'])}, user_id LIMIT 30 OFFSET ?",
        (*args, max(0, page) * 30),
    ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page_size": 30}


class Adjust(BaseModel):
    kind: str
    delta: int = Field(ge=-(10**12), le=10**12)


@app.post("/api/users/{user_id}/adjust", dependencies=[Depends(admin)])
def adjust(user_id: int, body: Adjust):
    if body.kind not in db.KINDS or not body.delta:
        raise HTTPException(400, "잘못된 요청입니다.")
    if not db.get_user(user_id):
        raise HTTPException(404, "회원을 찾을 수 없습니다.")
    applied, balance = db.change(user_id, body.kind, body.delta, "admin", ACTOR)
    return {"applied": applied, "balance": balance}


@app.get("/api/logs", dependencies=[Depends(admin)])
def logs(q: str = "", page: int = 0):
    where, args = "", []
    q = q.strip().lstrip("@")
    if q:
        where = "WHERE u.name LIKE ? OR u.username LIKE ? OR CAST(l.user_id AS TEXT)=?"
        args = [f"%{q}%", f"%{q}%", q]
    c = db.conn()
    base = f"FROM logs l LEFT JOIN users u USING(user_id) {where}"
    total = c.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = c.execute(
        f"SELECT l.id, l.ts, l.user_id, u.name, l.kind, l.delta, l.balance, l.reason, l.actor {base} "
        "ORDER BY l.id DESC LIMIT 50 OFFSET ?",
        (*args, max(0, page) * 50),
    ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page_size": 50}


class Slot(BaseModel):
    points: int = Field(ge=0, le=10**9)
    weight: int = Field(ge=0, le=10**6)


class League(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=40)


class Settings(BaseModel):
    ranking_period: str
    attend_points: int = Field(ge=0, le=10**9)
    roulette_enabled: bool
    roulette_slots: list[Slot] = Field(min_length=2, max_length=12)
    sports_cache_sec: int = Field(ge=30, le=3600)
    leagues: dict[str, list[League]]


@app.get("/api/settings", dependencies=[Depends(admin)])
def get_settings():
    return db.settings()


@app.put("/api/settings", dependencies=[Depends(admin)])
def put_settings(body: Settings):
    if body.ranking_period not in db.PERIODS:
        raise HTTPException(400, "잘못된 랭킹 기간입니다.")
    if not sum(s.weight for s in body.roulette_slots):
        raise HTTPException(400, "룰렛 확률 가중치의 합이 0입니다.")
    data = body.model_dump()
    data["leagues"] = {k: data["leagues"].get(k, []) for k in sports.SPORTS}
    db.save_settings(data)
    return db.settings()


@app.post("/api/reset/{kind}", dependencies=[Depends(admin)])
def reset(kind: str):
    if kind not in {"points", "tickets", "chat"}:
        raise HTTPException(400, "잘못된 요청입니다.")
    return {"affected": db.reset_all(kind, ACTOR)}


class Notice(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    pin: bool = False


@app.post("/api/notice", dependencies=[Depends(admin)])
async def notice(body: Notice):
    if not config.BOT_TOKEN or not config.GROUP_ID:
        raise HTTPException(400, "BOT_TOKEN 또는 GROUP_ID가 설정되지 않았습니다.")
    api = f"https://api.telegram.org/bot{config.BOT_TOKEN}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = (
            await client.post(
                f"{api}/sendMessage", json={"chat_id": config.GROUP_ID, "text": body.text, "parse_mode": "HTML"}
            )
        ).json()
        if not r.get("ok"):
            raise HTTPException(400, f"전송 실패: {r.get('description')}")
        if body.pin:
            await client.post(
                f"{api}/pinChatMessage", json={"chat_id": config.GROUP_ID, "message_id": r["result"]["message_id"]}
            )
    return {"ok": True}


@app.get("/api/leagues/search", dependencies=[Depends(admin)])
async def league_search(sport: str, q: str):
    if sport not in sports.SPORTS or len(q.strip()) < 3:
        raise HTTPException(400, "검색어는 3글자 이상(영문) 입력하세요.")
    try:
        return await sports.search_leagues(sport, q.strip())
    except sports.SportsError as e:
        raise HTTPException(502, str(e))


@app.get("/")
def index():
    return FileResponse(config.STATIC / "admin.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    if not config.ADMIN_PASSWORD:
        raise SystemExit("ADMIN_PASSWORD가 설정되지 않았습니다.")
    uvicorn.run(app, host=config.HOST, port=config.ADMIN_PORT, proxy_headers=True, forwarded_allow_ips="*")
