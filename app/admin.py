import base64
import hashlib
import hmac
import ipaddress
import time
from html import escape

import httpx
import uvicorn
from fastapi import Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, db, sports, web

COOKIE = "fb_admin"
SESSION_TTL = 7 * 86400
ACTOR = "관리자 페이지"
SORTS = {"points": "points DESC", "tickets": "tickets DESC", "messages": "msg_total DESC", "recent": "last_seen DESC"}
HEADERS = {"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}
HIDDEN = {"session_epoch"}

app = web.create_app(HEADERS)
_fails: dict[str, list[float]] = {}


@app.middleware("http")
async def guard(request: Request, call_next):
    site, origin = request.headers.get("sec-fetch-site"), request.headers.get("origin")
    if site:
        same = site in ("same-origin", "none")
    else:
        same = not origin or origin.split("://")[-1] == request.headers.get("host")
    if request.method not in ("GET", "HEAD") and not same:
        return JSONResponse({"detail": "허용되지 않은 요청입니다."}, 403, headers=HEADERS)
    response = await call_next(request)
    response.headers.update(HEADERS)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def sign(expires: int) -> str:
    payload = f"{expires}:{db.setting('session_epoch')}".encode()
    mac = hmac.new(config.SESSION_SECRET, payload, hashlib.sha256).digest()
    return f"{expires}.{base64.urlsafe_b64encode(mac).decode()}"


def authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE, "")
    try:
        expires = int(token.split(".")[0])
    except ValueError:
        return False
    return expires > time.time() and hmac.compare_digest(sign(expires).encode(), token.encode())


def admin(request: Request):
    if not authed(request):
        raise HTTPException(401, "로그인이 필요합니다.")


def client_key(request: Request) -> str:
    host = request.client.host if request.client else "?"
    try:
        return str(ipaddress.ip_network(f"{host}/64", strict=False)) if ":" in host else host
    except ValueError:
        return host


def like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def visible_settings() -> dict:
    return {k: v for k, v in db.settings().items() if k not in HIDDEN}


class Login(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=200)


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    now = time.time()
    for k, v in list(_fails.items()):
        if now - v[-1] >= 600:
            del _fails[k]
    key = client_key(request)
    recent = [t for t in _fails.get(key, []) if now - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "로그인 시도가 너무 많습니다. 10분 뒤 다시 시도하세요.")
    ok = (
        bool(config.ADMIN_PASSWORD)
        & hmac.compare_digest(body.username.encode(errors="surrogatepass"), config.ADMIN_USERNAME.encode())
        & hmac.compare_digest(body.password.encode(errors="surrogatepass"), config.ADMIN_PASSWORD.encode())
    )
    if not ok:
        _fails[key] = recent + [now]
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
    _fails.pop(key, None)
    response.set_cookie(
        COOKIE,
        sign(int(now) + SESSION_TTL),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    if authed(request):
        db.save_settings({"session_epoch": time.time_ns()})
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
def users(q: str = Query("", max_length=64), sort: str = "points", page: int = Query(0, ge=0, le=100000)):
    where, args = "", []
    q = q.strip().lstrip("@")
    if q:
        where = "WHERE name LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\' OR CAST(user_id AS TEXT)=?"
        args = [like(q), like(q), q]
    c = db.conn()
    total = c.execute(f"SELECT COUNT(*) FROM users {where}", args).fetchone()[0]
    rows = c.execute(
        f"SELECT user_id, username, name, points, tickets, msg_total, attend_total, attend_streak, last_seen "
        f"FROM users {where} ORDER BY {SORTS.get(sort, SORTS['points'])}, user_id LIMIT 30 OFFSET ?",
        (*args, page * 30),
    ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page_size": 30}


class Adjust(BaseModel):
    kind: str
    delta: int = Field(ge=-(10**12), le=10**12)


@app.post("/api/users/{user_id}/adjust", dependencies=[Depends(admin)])
def adjust(body: Adjust, user_id: int = Path(ge=-(2**63), lt=2**63)):
    if body.kind not in db.KINDS or not body.delta:
        raise HTTPException(400, "잘못된 요청입니다.")
    if not db.get_user(user_id):
        raise HTTPException(404, "회원을 찾을 수 없습니다.")
    applied, balance = db.change(user_id, body.kind, body.delta, "admin", ACTOR)
    return {"applied": applied, "balance": balance}


@app.get("/api/logs", dependencies=[Depends(admin)])
def logs(q: str = Query("", max_length=64), page: int = Query(0, ge=0, le=100000)):
    where, args = "", []
    q = q.strip().lstrip("@")
    if q:
        where = "WHERE u.name LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\' OR CAST(l.user_id AS TEXT)=?"
        args = [like(q), like(q), q]
    c = db.conn()
    base = f"FROM logs l LEFT JOIN users u USING(user_id) {where}"
    total = c.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = c.execute(
        f"SELECT l.id, l.ts, l.user_id, u.name, l.kind, l.delta, l.balance, l.reason, l.actor {base} "
        "ORDER BY l.id DESC LIMIT 50 OFFSET ?",
        (*args, page * 50),
    ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page_size": 50}


class Slot(BaseModel):
    points: int = Field(ge=0, le=10**9)
    weight: int = Field(ge=0, le=10**6)


class League(BaseModel):
    id: int = Field(ge=0, lt=2**31)
    name: str = Field(min_length=1, max_length=40)


class Settings(BaseModel):
    app_name: str = Field(default=db.DEFAULTS["app_name"], min_length=1, max_length=24)
    ranking_period: str
    attend_points: int = Field(ge=0, le=10**9)
    roulette_enabled: bool
    roulette_slots: list[Slot] = Field(min_length=2, max_length=12)
    sports_cache_sec: int = Field(ge=30, le=3600)
    leagues: dict[str, list[League]]


@app.get("/api/settings", dependencies=[Depends(admin)])
def get_settings():
    return visible_settings()


@app.put("/api/settings", dependencies=[Depends(admin)])
def put_settings(body: Settings):
    if body.ranking_period not in db.PERIODS:
        raise HTTPException(400, "잘못된 랭킹 기간입니다.")
    if not sum(s.weight for s in body.roulette_slots):
        raise HTTPException(400, "룰렛 확률 가중치의 합이 0입니다.")
    data = body.model_dump()
    data["app_name"] = data["app_name"].strip()
    if not data["app_name"]:
        raise HTTPException(400, "브랜드 이름을 입력하세요.")
    data["leagues"] = {k: data["leagues"].get(k, []) for k in sports.SPORTS}
    db.save_settings(data)
    return visible_settings()


@app.post("/api/reset/{kind}", dependencies=[Depends(admin)])
def reset(kind: str):
    if kind not in {"points", "tickets", "chat"}:
        raise HTTPException(400, "잘못된 요청입니다.")
    return {"affected": db.reset_all(kind, ACTOR)}


class Notice(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    pin: bool = False


async def telegram(client: httpx.AsyncClient, method: str, **params) -> dict:
    try:
        return (await client.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}", json=params)).json()
    except (httpx.HTTPError, ValueError):
        return {"description": "텔레그램 서버가 응답하지 않습니다. 그룹방에 전송됐는지 확인한 뒤 다시 시도하세요."}


@app.post("/api/notice", dependencies=[Depends(admin)])
async def notice(body: Notice):
    if not config.BOT_TOKEN or not config.GROUP_ID:
        raise HTTPException(400, "BOT_TOKEN 또는 GROUP_ID가 설정되지 않았습니다.")
    async with httpx.AsyncClient(timeout=10) as client:
        for text in (body.text, escape(body.text, quote=False)):
            r = await telegram(
                client, "sendMessage", chat_id=config.GROUP_ID, text=f"📢 <b>공지</b>\n\n{text}", parse_mode="HTML"
            )
            if r.get("ok") or "parse entities" not in str(r.get("description")):
                break
        if not r.get("ok"):
            raise HTTPException(400, f"전송 실패: {r.get('description')}")
        if body.pin:
            p = await telegram(client, "pinChatMessage", chat_id=config.GROUP_ID, message_id=r["result"]["message_id"])
            if not p.get("ok"):
                raise HTTPException(502, f"공지는 보냈지만 상단 고정에 실패했습니다: {p.get('description')}")
    return {"ok": True}


@app.get("/api/leagues/search", dependencies=[Depends(admin)])
async def league_search(sport: str, q: str = Query(max_length=40)):
    if sport not in sports.SPORTS or len(q.strip()) < 3:
        raise HTTPException(400, "검색어는 3글자 이상(영문) 입력하세요.")
    try:
        return await sports.search_leagues(sport, q.strip())
    except sports.SportsError as e:
        raise HTTPException(502, e.detail)


@app.get("/")
def index():
    return FileResponse(config.STATIC / "admin.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    if not config.ADMIN_PASSWORD:
        raise SystemExit("ADMIN_PASSWORD가 설정되지 않았습니다.")
    uvicorn.run(app, host=config.HOST, port=config.ADMIN_PORT)
