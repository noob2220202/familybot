import asyncio
import logging
import re
import time
from contextlib import suppress
from html import escape, unescape
from unicodedata import east_asian_width, normalize

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from . import config, db, sports

log = logging.getLogger("familybot")

PAGE = 10
RESET_TTL = 300
MEDALS = ["🥇", "🥈", "🥉"]
WEEKDAYS = "월화수목금토일"
COUNTABLE = {
    ContentType.TEXT,
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.ANIMATION,
    ContentType.STICKER,
    ContentType.VOICE,
    ContentType.VIDEO_NOTE,
    ContentType.AUDIO,
    ContentType.DOCUMENT,
    ContentType.POLL,
    ContentType.LOCATION,
    ContentType.CONTACT,
}
ADJUST = {
    "포인트지급": ("points", 1, "💰", "포인트", " P"),
    "포인트회수": ("points", -1, "💰", "포인트", " P"),
    "이용권지급": ("tickets", 1, "🎟️", "이용권", "장"),
    "이용권회수": ("tickets", -1, "🎟️", "이용권", "장"),
}
STATES = ["live", "upcoming", "finished", "off"]
GLYPH = re.compile("[\U0001f1e6-\U0001f1ff]{2}|.(?:[️⃣\U0001f3fb-\U0001f3ff\U000e0020-\U000e007f]|‍.)*", re.S)
DENIED = "🚫 <b>관리자 전용</b> 명령어입니다."
ANON = "🚫 <b>익명</b>으로는 쓸 수 없습니다."

group = Router()
group.message.filter(F.chat.id == config.GROUP_ID)
group.callback_query.filter(F.message.chat.id == config.GROUP_ID)
group.chat_member.filter(F.chat.id == config.GROUP_ID)
private = Router()
private.message.filter(F.chat.type == ChatType.PRIVATE)

_admins: dict = {"at": float("-inf"), "ids": set()}
_resets: dict[int, float] = {}
_last_counted: dict[int, float] = {}
_bot_username = ""


def num(n: int) -> str:
    return f"{n:,}"


def clip(name: str, width: float = 14) -> str:
    name = normalize("NFC", name).replace("@", "＠")
    used, out = 0.0, ""
    for g in GLYPH.findall(name):
        used += 2.75 if len(g) > 1 or g >= "\U0001f000" else 2 if east_asian_width(g) in "WF" else 1.25
        if used > width:
            return out.rstrip() + "…"
        out += g
    return name


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(clip(name, 12))}</a>'


def size(text: str) -> int:
    return len(unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


def quote(*lines: str, fold: bool = False) -> str:
    return f"<blockquote{' expandable' if fold else ''}>" + "\n".join(lines) + "</blockquote>"


def keyboard(*rows: list[tuple]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d, style=s[0] if s else None) for t, d, *s in row]
            for row in rows
        ]
    )


def app_button() -> InlineKeyboardMarkup | None:
    if not config.MINIAPP_URL or not _bot_username:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📱 미니앱 열기", url=f"https://t.me/{_bot_username}?startapp")]]
    )


def sender(msg: Message | None):
    u = msg and msg.from_user
    return u if u and not u.is_bot and not msg.sender_chat else None


def anonymous(msg: Message) -> bool:
    return bool(msg.sender_chat and msg.sender_chat.id == msg.chat.id)


async def admin_ids(bot: Bot) -> set[int]:
    if time.monotonic() - _admins["at"] > 60:
        members = await bot.get_chat_administrators(config.GROUP_ID)
        _admins.update(at=time.monotonic(), ids={m.user.id for m in members})
    return _admins["ids"]


async def is_admin(bot: Bot, msg: Message) -> bool:
    if anonymous(msg):
        return True
    return bool(msg.from_user) and msg.from_user.id in await admin_ids(bot)


def actor_name(msg: Message) -> str:
    return "익명 관리자" if msg.sender_chat else msg.from_user.full_name


class Track(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        user = sender(event)
        if event.chat.id == config.GROUP_ID and user:
            count = event.content_type in COUNTABLE and not (event.text or "").startswith("/")
            if count:
                t = event.date.timestamp()
                count = abs(t - _last_counted.get(user.id, 0)) >= 1
                if count:
                    _last_counted[user.id] = t
            db.touch_user(user.id, user.username, user.full_name, count, event.date)
        return await handler(event, data)


class Retry(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        for _ in range(2):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
        return await make_request(bot, method)


async def resolve_target(bot: Bot, msg: Message, tokens: list[str]):
    reply = msg.reply_to_message
    u = sender(reply)
    if len(tokens) == 1 and u and not reply.forum_topic_created:
        db.touch_user(u.id, u.username, u.full_name)
        return db.get_user(u.id)
    if len(tokens) < 2:
        return None
    for e in msg.entities or []:
        if e.type == "text_mention" and e.user and not e.user.is_bot:
            db.touch_user(e.user.id, e.user.username, e.user.full_name)
            return db.get_user(e.user.id)
    token = tokens[0]
    found = db.find_user(token)
    if not found and token.isdecimal() and len(token) < 19:
        try:
            member = await bot.get_chat_member(config.GROUP_ID, int(token))
        except TelegramBadRequest:
            return None
        if member.user.is_bot or member.status in {"left", "kicked"}:
            return None
        db.touch_user(member.user.id, member.user.username, member.user.full_name)
        found = db.get_user(member.user.id)
    return found


@group.message(Command(*ADJUST))
async def adjust(msg: Message, command: CommandObject, bot: Bot):
    kind, sign, icon, noun, unit = ADJUST[command.command]
    if not await is_admin(bot, msg):
        return await msg.reply(DENIED)
    tokens = (command.args or "").split()
    amount = tokens[-1].replace(",", "") if tokens else ""
    reply = msg.reply_to_message
    aimed = len(tokens) > 1 or bool(reply and not reply.forum_topic_created)
    if not aimed or not amount.isdecimal() or not 0 < int(amount) <= 10**12:
        cmd = f"<code>/{command.command}</code>"
        return await msg.reply("ℹ️ <b>사용법</b>\n" + quote(f"{cmd} @유저네임 수량", f"{cmd} ID 수량", f"답장으로 {cmd} 수량"))
    target = await resolve_target(bot, msg, tokens)
    if not target:
        return await msg.reply(
            "❓ <b>대상을 찾을 수 없습니다</b>\n" + quote("채팅한 회원만 가능합니다.", "ID나 답장으로 지정해 주세요.")
        )
    reason = "grant" if sign > 0 else "revoke"
    applied, balance = db.change(target["user_id"], kind, sign * int(amount), reason, actor_name(msg))
    head = f"{icon} <b>{noun} {'지급' if sign > 0 else '회수'}</b> · "
    have = f"보유 <b>{num(balance)}{unit}</b>"
    if not applied:
        return await msg.reply(head + escape(clip(target["name"], 12)) + "\n" + quote(f"회수 없음 · {have}"))
    lines = [f"<b>{'+' if sign > 0 else '−'}{num(abs(applied))}{unit}</b> · {have}"]
    if abs(applied) < int(amount):
        lines.append(f"요청 {num(int(amount))}{unit} · 보유분만 회수")
    await msg.reply(head + mention(target["user_id"], target["name"]) + "\n" + quote(*lines))


@group.message(Command("포인트초기화"))
async def reset_ask(msg: Message, bot: Bot):
    if not await is_admin(bot, msg):
        return await msg.reply(DENIED)
    sent = await msg.reply(
        "⚠️ <b>포인트 초기화</b>\n" + quote("전원의 포인트가 <b>0 P</b>가 됩니다.", "되돌릴 수 없습니다."),
        reply_markup=keyboard([("취소", "reset:no"), ("초기화", "reset:yes", "danger")]),
    )
    _resets[sent.message_id] = time.monotonic()


@group.callback_query(F.data.startswith("reset:"))
async def reset_confirm(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in await admin_ids(bot):
        return await cb.answer("관리자 전용 버튼입니다.", show_alert=True)
    if time.monotonic() - _resets.pop(cb.message.message_id, float("-inf")) > RESET_TTL:
        return await cb.answer("만료된 요청입니다. /포인트초기화 를 다시 입력하세요.", show_alert=True)
    if cb.data == "reset:no":
        return await edit(cb, "↩️ <b>포인트 초기화 취소</b>", None)
    n = db.reset_all("points", cb.from_user.full_name)
    await edit(cb, "♻️ <b>포인트 초기화 완료</b>\n" + quote(f"대상 <b>{num(n)}명</b> · 전원 <b>0 P</b>"), None)


def ranking_view(page: int):
    period = db.setting("ranking_period")
    rows, total = db.ranking(period, page * PAGE, PAGE)
    pages = max(1, -(-total // PAGE))
    if page >= pages:
        page = pages - 1
        rows, total = db.ranking(period, page * PAGE, PAGE)
    head = f"🏆 <b>채팅 랭킹</b> · {db.PERIODS[period]}"
    if not rows:
        return head + "\n" + quote("아직 집계된 채팅이 없습니다."), None
    lines = []
    for i, r in enumerate(rows, start=page * PAGE):
        name = escape(clip(r["name"]))
        mark = f"{MEDALS[i]} <b>{name}</b>" if i < 3 else f"<b>{i + 1}.</b> {name}"
        lines.append(f"{mark} · {num(r['c'])}회")
    nav = [("‹ 이전", f"rank:{page - 1}")] * (page > 0) + [("다음 ›", f"rank:{page + 1}")] * (page < pages - 1)
    return f"{head} · {num(total)}명\n" + quote(*lines), keyboard(nav) if nav else None


@group.message(Command("랭킹"))
async def ranking_cmd(msg: Message):
    text, markup = ranking_view(0)
    await msg.answer(text, reply_markup=markup)


@group.callback_query(F.data.regexp(r"^rank:\d{1,6}$"))
async def ranking_page(cb: CallbackQuery):
    await edit(cb, *ranking_view(int(cb.data.split(":")[1])))


async def edit(cb: CallbackQuery, text: str, markup):
    try:
        await cb.bot.edit_message_text(
            text, chat_id=cb.message.chat.id, message_id=cb.message.message_id, reply_markup=markup
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
        return await cb.answer("변경된 내용이 없습니다.")
    await cb.answer()


def date_label() -> str:
    d = db.now()
    return f"{d.month}.{d.day} ({WEEKDAYS[d.weekday()]})"


def sport_menu():
    leagues = db.setting("leagues")
    items = [(f"{v[0]} {v[1]}", f"game:{k}") for k, v in sports.SPORTS.items() if leagues.get(k)]
    head = f"🏟️ <b>오늘의 경기</b> · {date_label()}"
    if not items:
        return head + "\n" + quote("설정된 리그가 없습니다."), None
    return head, keyboard(*[items[i : i + 2] for i in range(0, len(items), 2)])


def game_line(g: dict) -> str:
    home, away = escape(g["home"]), escape(g["away"])
    score = f"<b>{g['home_score'] or 0}:{g['away_score'] or 0}</b>"
    if g["state"] == "upcoming":
        return f"🕒 <b>{g['time']}</b> {home} vs {away}"
    if g["state"] == "live":
        return f"🔴 <b>{escape(g['label'])}</b> {home} {score} {away}"
    if g["state"] == "finished":
        return f"🏁 {home} {score} {away}" + (f" · {g['label']}" if g["label"] else "")
    return f"⏸️ <b>{g['label']}</b> {home} vs {away}"


async def games_view(sport: str):
    icon, name = sports.SPORTS[sport][:2]
    head = f"{icon} <b>{name}</b> · {date_label()}"
    markup = keyboard([("‹ 종목", "game:menu"), ("🔄 새로고침", f"game:{sport}")])
    try:
        leagues, at = await sports.today_games(sport)
    except sports.SportsError as e:
        return head + "\n" + quote(f"⚠️ {escape(str(e))}"), markup
    if not leagues:
        return head + "\n" + quote("오늘은 주요 경기가 없습니다."), markup
    fold = sum(len(games) for _, games in leagues) > 8
    text, hidden = head, 0
    for league, games in leagues:
        rows = map(game_line, sorted(games, key=lambda g: STATES.index(g["state"])))
        block = f"\n\n<b>{escape(league)}</b> · {len(games)}경기\n" + quote(*rows, fold=fold and len(games) > 2)
        if size(text + block) > 4000 or (text + block).count("</") > 96:
            hidden += len(games)
        else:
            text += block
    return f"{text}\n\n<i>{at:%H:%M} 기준" + (f" · 외 {hidden}경기" if hidden else "") + "</i>", markup


@group.message(Command("경기"))
async def games_cmd(msg: Message):
    text, markup = sport_menu()
    await msg.answer(text, reply_markup=markup)


@group.callback_query(F.data.startswith("game:"))
async def games_pick(cb: CallbackQuery):
    sport = cb.data.split(":")[1]
    if sport not in sports.SPORTS:
        return await edit(cb, *sport_menu())
    await edit(cb, *await games_view(sport))


@group.message(Command("내정보"))
async def my_info(msg: Message):
    user = sender(msg)
    if not user:
        return await msg.reply(ANON) if anonymous(msg) else None
    u = db.get_user(user.id)
    s = db.settings()
    period = s["ranking_period"]
    rank, count, _ = db.rank_of(user.id, period)
    lines = [f"💰 포인트 <b>{num(u['points'])} P</b>"]
    if s["roulette_enabled"]:
        lines.append(f"🎟️ 룰렛 이용권 <b>{num(u['tickets'])}장</b>")
    lines.append(f"💬 채팅 <b>{num(u['msg_total'])}회</b>")
    if rank:
        lines.append(f"🏆 {db.PERIODS[period]} <b>{num(rank)}위</b>" + (f" · {num(count)}회" if period != "all" else ""))
    lines.append(f"📅 출석 <b>{num(u['attend_total'])}일</b> · 연속 <b>{num(db.streak(u))}일</b>")
    await msg.reply(f"👤 <b>{escape(clip(u['name'], 22))}</b>\n" + quote(*lines), reply_markup=app_button())


@group.message(Command("출석"))
async def attend(msg: Message):
    user = sender(msg)
    if not user:
        return await msg.reply(ANON) if anonymous(msg) else None
    result = db.attend(user.id)
    if not result:
        u = db.get_user(user.id)
        return await msg.reply(
            "📅 <b>오늘은 이미 출석했습니다</b>\n"
            + quote(f"🔥 연속 <b>{num(u['attend_streak'])}일</b> · 누적 <b>{num(u['attend_total'])}일</b>")
        )
    lines = [f"🔥 연속 <b>{num(result['streak'])}일</b> · 누적 <b>{num(result['total'])}일</b>"]
    if result["reward"]:
        lines.insert(0, f"💰 <b>+{num(result['reward'])} P</b> · 보유 <b>{num(result['balance'])} P</b>")
    await msg.reply("✅ <b>출석 완료</b>\n" + quote(*lines))


@group.message(F.migrate_to_chat_id)
async def migrated(msg: Message):
    log.error("그룹이 슈퍼그룹으로 전환되었습니다. .env 의 GROUP_ID 를 %s 로 바꾸고 재시작하세요.", msg.migrate_to_chat_id)


@group.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def joined(event: ChatMemberUpdated):
    u = event.new_chat_member.user
    if not u.is_bot:
        db.touch_user(u.id, u.username, u.full_name)
        db.member_event(u.id, "join")


@group.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def left(event: ChatMemberUpdated):
    u = event.new_chat_member.user
    if not u.is_bot:
        db.member_event(u.id, "leave")


@group.chat_member()
async def member_changed(event: ChatMemberUpdated):
    _admins["at"] = float("-inf")


@group.callback_query()
async def stale(cb: CallbackQuery):
    await cb.answer()


@private.message()
async def start(msg: Message):
    if not config.MINIAPP_URL:
        return await msg.answer("🤖 <b>그룹방 전용</b> 봇입니다.")
    s = db.settings()
    tabs = ["내 정보", "통계", "랭킹"] + ["룰렛"] * bool(s["roulette_enabled"])
    await msg.answer(
        f"📱 <b>{escape(s['app_name'])} 미니앱</b>\n" + quote(" · ".join(tabs)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📱 미니앱 열기", web_app=WebAppInfo(url=config.MINIAPP_URL))]]
        ),
    )


async def chat_id(msg: Message):
    await msg.reply(f"🆔 <b>채팅방 ID</b> · <code>{msg.chat.id}</code>")


async def on_error(event: ErrorEvent):
    log.error("update %s 처리 실패: %r", event.update.update_id, event.exception, exc_info=event.exception)
    if cb := event.update.callback_query:
        with suppress(TelegramAPIError):
            await cb.answer("잠시 후 다시 시도해 주세요.")


async def check_setup(bot: Bot) -> None:
    global _bot_username
    me = await bot.get_me()
    _bot_username = me.username
    if not config.GROUP_ID:
        log.warning("GROUP_ID가 비어 있습니다. 그룹방에서 /id 로 확인한 값을 .env 에 넣고 재시작하세요.")
    else:
        try:
            status = (await bot.get_chat_member(config.GROUP_ID, me.id)).status
        except TelegramAPIError:
            status = "left"
        if status != "administrator":
            log.warning("봇이 그룹방 관리자가 아닙니다. 한글 명령어 · 채팅 집계 · 입퇴장 통계가 동작하지 않습니다.")
    if config.MINIAPP_URL:
        if not me.has_main_web_app:
            log.warning("Main Mini App 이 등록되지 않았습니다. @BotFather → Bot Settings → Configure Mini App")
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="미니앱", web_app=WebAppInfo(url=config.MINIAPP_URL))
            )
        except TelegramBadRequest as e:
            log.warning("미니앱 메뉴 버튼을 설정하지 못했습니다: %s", e)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN이 설정되지 않았습니다.")
    bot = Bot(
        config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True, allow_sending_without_reply=True),
    )
    bot.session.middleware(Retry())
    dp = Dispatcher()
    dp.errors.register(on_error)
    dp.message.outer_middleware(Track())
    dp.message.register(chat_id, Command("id"))
    dp.include_routers(group, private)
    try:
        await check_setup(bot)
        await bot.delete_webhook()
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
