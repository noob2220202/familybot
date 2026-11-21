import asyncio
import logging
import time
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from . import config, db, sports

log = logging.getLogger("familybot")

PAGE = 10
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
    "포인트지급": ("points", 1, "💰", "포인트 지급", " P"),
    "포인트회수": ("points", -1, "💸", "포인트 회수", " P"),
    "이용권지급": ("tickets", 1, "🎟", "이용권 지급", "장"),
    "이용권회수": ("tickets", -1, "🎟", "이용권 회수", "장"),
}

group = Router()
group.message.filter(F.chat.id == config.GROUP_ID)
group.callback_query.filter(F.message.chat.id == config.GROUP_ID)
group.chat_member.filter(F.chat.id == config.GROUP_ID)
private = Router()
private.message.filter(F.chat.type == ChatType.PRIVATE)

_admins: dict = {"at": 0.0, "ids": set()}
_last_counted: dict[int, float] = {}
_bot_username = ""


def num(n: int) -> str:
    return f"{n:,}"


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(name)}</a>'


def quote(*lines: str) -> str:
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def app_button() -> InlineKeyboardMarkup | None:
    if not config.MINIAPP_URL or not _bot_username:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📊 미니앱 열기", url=f"https://t.me/{_bot_username}?startapp")]]
    )


async def admin_ids(bot: Bot) -> set[int]:
    if time.monotonic() - _admins["at"] > 60:
        members = await bot.get_chat_administrators(config.GROUP_ID)
        _admins.update(at=time.monotonic(), ids={m.user.id for m in members})
    return _admins["ids"]


async def is_admin(bot: Bot, msg: Message) -> bool:
    if msg.sender_chat and msg.sender_chat.id == msg.chat.id:
        return True
    return bool(msg.from_user) and msg.from_user.id in await admin_ids(bot)


def actor_name(msg: Message) -> str:
    if msg.sender_chat:
        return "익명 관리자"
    return msg.from_user.full_name


class Track(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        user = event.from_user
        if event.chat.id == config.GROUP_ID and user and not user.is_bot:
            count = event.content_type in COUNTABLE and not (event.text or "").startswith("/")
            if count:
                t = time.monotonic()
                count = t - _last_counted.get(user.id, 0) >= 1
                if count:
                    _last_counted[user.id] = t
            db.touch_user(user.id, user.username, user.full_name, count)
        return await handler(event, data)


async def resolve_target(bot: Bot, msg: Message, tokens: list[str]):
    reply = msg.reply_to_message
    if len(tokens) == 1 and reply and not reply.forum_topic_created and reply.from_user and not reply.from_user.is_bot:
        u = reply.from_user
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
    if not found and token.isdigit():
        try:
            member = await bot.get_chat_member(config.GROUP_ID, int(token))
        except TelegramBadRequest:
            return None
        if member.user.is_bot:
            return None
        db.touch_user(member.user.id, member.user.username, member.user.full_name)
        found = db.get_user(member.user.id)
    return found


@group.message(Command(*ADJUST))
async def adjust(msg: Message, command: CommandObject, bot: Bot):
    kind, sign, icon, title, unit = ADJUST[command.command]
    if not await is_admin(bot, msg):
        return await msg.reply("🚫 <b>관리자 전용</b> 명령어입니다.")
    tokens = (command.args or "").split()
    amount = tokens[-1].replace(",", "") if tokens else ""
    if not amount.isdigit() or not 0 < int(amount) <= 10**12:
        cmd = command.command
        return await msg.reply(
            "ℹ️ <b>사용법</b>\n" + quote(f"/{cmd} @유저 숫자", f"/{cmd} 유저ID 숫자", f"(답장으로) /{cmd} 숫자")
        )
    target = await resolve_target(bot, msg, tokens)
    if not target:
        return await msg.reply("❓ <b>대상을 찾을 수 없습니다.</b>\n" + quote("그룹방에서 채팅한 적이 있는 유저만 지정할 수 있습니다."))
    reason = "grant" if sign > 0 else "revoke"
    applied, balance = db.change(target["user_id"], kind, sign * int(amount), reason, actor_name(msg))
    await msg.reply(
        f"{icon} <b>{title}</b>\n"
        + quote(
            f"👤 {mention(target['user_id'], target['name'])}",
            f"{'➕' if sign > 0 else '➖'} <b>{num(abs(applied))}{unit}</b>",
            f"💳 보유 <b>{num(balance)}{unit}</b>",
        )
    )


@group.message(Command("포인트초기화"))
async def reset_ask(msg: Message, bot: Bot):
    if not await is_admin(bot, msg):
        return await msg.reply("🚫 <b>관리자 전용</b> 명령어입니다.")
    await msg.reply(
        "⚠️ <b>포인트 초기화</b>\n" + quote("전체 회원의 보유 포인트가 <b>0 P</b>가 됩니다.", "되돌릴 수 없습니다."),
        reply_markup=keyboard([("✅ 초기화", "reset:yes"), ("취소", "reset:no")]),
    )


@group.callback_query(F.data.startswith("reset:"))
async def reset_confirm(cb: CallbackQuery, bot: Bot):
    answer = cb.data.split(":")[1]
    if cb.from_user.id not in await admin_ids(bot):
        return await cb.answer("관리자 전용입니다.", show_alert=True)
    if answer == "no":
        await cb.message.edit_text("↩️ 포인트 초기화를 <b>취소</b>했습니다.")
    else:
        n = db.reset_all("points", cb.from_user.full_name)
        await cb.message.edit_text("♻️ <b>포인트 초기화 완료</b>\n" + quote(f"👥 대상 <b>{num(n)}명</b>", "💳 전원 <b>0 P</b>"))
    await cb.answer()


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
        name = escape(r["name"])
        if i < 3:
            lines.append(f"{MEDALS[i]} <b>{name}</b> · {num(r['c'])}")
        else:
            lines.append(f"<b>{i + 1}.</b> {name} · {num(r['c'])}")
    markup = None
    if pages > 1:
        markup = keyboard(
            [
                ("◀", f"rank:{page - 1}" if page > 0 else "noop"),
                (f"{page + 1} / {pages}", "noop"),
                ("▶", f"rank:{page + 1}" if page < pages - 1 else "noop"),
            ]
        )
    return head + "\n" + quote(*lines), markup


@group.message(Command("랭킹"))
async def ranking_cmd(msg: Message):
    text, markup = ranking_view(0)
    await msg.answer(text, reply_markup=markup)


@group.callback_query(F.data.startswith("rank:"))
async def ranking_page(cb: CallbackQuery):
    text, markup = ranking_view(max(0, int(cb.data.split(":")[1])))
    await edit(cb, text, markup)


@group.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


async def edit(cb: CallbackQuery, text: str, markup):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await cb.answer()


def date_label() -> str:
    d = db.now()
    return f"{d.month}.{d.day} ({WEEKDAYS[d.weekday()]})"


def sport_menu():
    items = [(f"{v[0]} {v[1]}", f"game:{k}") for k, v in sports.SPORTS.items()]
    return (
        f"📅 <b>오늘의 경기</b> · {date_label()}\n종목을 선택하세요.",
        keyboard(items[:2], items[2:]),
    )


def game_line(g: dict) -> str:
    home, away = escape(g["home"]), escape(g["away"])
    score = f"<b>{g['home_score'] if g['home_score'] is not None else 0} : {g['away_score'] if g['away_score'] is not None else 0}</b>"
    if g["state"] == "upcoming":
        return f"🕘 <b>{g['time']}</b>  {home} vs {away}"
    if g["state"] == "live":
        return f"🔴 <b>{escape(g['label'])}</b>  {home} {score} {away}"
    if g["state"] == "finished":
        return f"✅ {home} {score} {away}"
    return f"⚠️ <b>{g['label']}</b>  {home} vs {away}"


async def games_view(sport: str):
    icon, name = sports.SPORTS[sport][:2]
    head = f"{icon} <b>{name}</b> · {date_label()}"
    markup = keyboard([("🔄 새로고침", f"game:{sport}"), ("◀ 종목", "game:menu")])
    try:
        leagues = await sports.today_games(sport)
    except sports.SportsError as e:
        return head + "\n" + quote(f"⚠️ {escape(str(e))}"), markup
    if not leagues:
        return head + "\n" + quote("오늘 예정된 주요 경기가 없습니다."), markup
    text, hidden = head, 0
    for league, games in leagues:
        block = f"\n\n<b>{escape(league)}</b>\n" + quote(*map(game_line, games))
        if len(text) + len(block) > 3900:
            hidden += len(games)
        else:
            text += block
    if hidden:
        text += f"\n\n외 <b>{hidden}경기</b>"
    return text + f"\n\n<i>{db.now():%H:%M} 기준</i>", markup


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
    if not msg.from_user or msg.from_user.is_bot:
        return
    uid = msg.from_user.id
    u = db.get_user(uid)
    s = db.settings()
    period = s["ranking_period"]
    rank, count = db.rank_of(uid, period)
    chat = f"💬 채팅 <b>{num(u['msg_total'])}회</b>"
    if rank:
        chat += f" · {db.PERIODS[period]} <b>{rank}위</b> ({num(count)}회)"
    lines = [f"💰 포인트 <b>{num(u['points'])} P</b>"]
    if s["roulette_enabled"]:
        lines.append(f"🎟 룰렛 이용권 <b>{num(u['tickets'])}장</b>")
    lines += [chat, f"📆 출석 <b>{num(u['attend_total'])}일</b> · 연속 <b>{attend_streak(u)}일</b>"]
    await msg.reply(f"👤 <b>{escape(u['name'])}</b>\n" + quote(*lines), reply_markup=app_button())


def attend_streak(u) -> int:
    if not u["attend_last"]:
        return 0
    gap = (db.now().date() - db.date.fromisoformat(u["attend_last"])).days
    return u["attend_streak"] if gap <= 1 else 0


@group.message(Command("출석"))
async def attend(msg: Message):
    if not msg.from_user or msg.from_user.is_bot:
        return
    uid = msg.from_user.id
    result = db.attend(uid)
    if not result:
        u = db.get_user(uid)
        return await msg.reply(
            "📆 <b>오늘은 이미 출석했습니다.</b>\n"
            + quote(f"🔥 연속 <b>{u['attend_streak']}일</b> · 누적 <b>{num(u['attend_total'])}일</b>")
        )
    await msg.reply(
        "✅ <b>출석 완료</b>\n"
        + quote(
            f"➕ <b>{num(result['reward'])} P</b>",
            f"🔥 연속 <b>{result['streak']}일</b> · 누적 <b>{num(result['total'])}일</b>",
            f"💳 보유 <b>{num(result['balance'])} P</b>",
        )
    )


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


@private.message(Command("start"))
async def start(msg: Message):
    if not config.MINIAPP_URL:
        return await msg.answer("🤖 그룹방 전용 봇입니다.")
    await msg.answer(
        "📊 <b>미니앱</b>\n" + quote("통계 · 랭킹 · 룰렛"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="열기", web_app=WebAppInfo(url=config.MINIAPP_URL))]]
        ),
    )


async def chat_id(msg: Message):
    await msg.reply(f"🆔 <code>{msg.chat.id}</code>")


async def main():
    global _bot_username
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN이 설정되지 않았습니다.")
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.message.outer_middleware(Track())
    dp.message.register(chat_id, Command("id"))
    dp.include_routers(group, private)
    _bot_username = (await bot.get_me()).username
    if not config.GROUP_ID:
        log.warning("GROUP_ID가 비어 있습니다. 그룹방에서 /id 로 확인한 값을 .env에 넣으세요.")
    if config.MINIAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="미니앱", web_app=WebAppInfo(url=config.MINIAPP_URL))
        )
    await bot.delete_webhook()
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
