from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("poskuli_bot")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения {name}")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть целым числом") from exc


BOT_TOKEN = required_env("BOT_TOKEN")
DATABASE_URL = required_env("DATABASE_URL")
ARCHITECT_ID = env_int("ARCHITECT_ID", 6421600902)
COOLDOWN_MINUTES = env_int("COOLDOWN_MINUTES", 4)
DB_POOL_MAX_SIZE = max(1, env_int("DB_POOL_MAX_SIZE", 5))

DUEL_PENDING_MINUTES = 10
DUEL_FIGHT_MINUTES = 30


RANKS: dict[str, dict[str, Any]] = {
    "olympian": {
        "thresh": 1_000_000_000,
        "price": 0,
        "label": "Олимпиец 🏛️",
        "chance": 0.38,
        "all_in": 0.65,
        "cb": 0.10,
        "multiplier": 3.0,
    },
    "omnipotent": {
        "thresh": 1_500_000,
        "price": 1500,
        "label": "Всемогущий 🌌",
        "chance": 0.45,
        "all_in": 0.76,
        "cb": 0.18,
        "multiplier": 2.5,
    },
    "diamond": {
        "thresh": 500_000,
        "price": 1000,
        "label": "Бог 💎",
        "chance": 0.44,
        "all_in": 0.75,
        "cb": 0.15,
        "multiplier": 2.0,
    },
    "gold": {
        "thresh": 100_000,
        "price": 500,
        "label": "Ангел 👑",
        "chance": 0.43,
        "all_in": 0.74,
        "cb": 0.12,
        "multiplier": 1.5,
    },
    "silver": {
        "thresh": 30_000,
        "price": 150,
        "label": "МС 🌠",
        "chance": 0.42,
        "all_in": 0.73,
        "cb": 0.08,
        "multiplier": 1.2,
    },
    "bronze": {
        "thresh": 10_000,
        "price": 50,
        "label": "КМС 🚀",
        "chance": 0.41,
        "all_in": 0.72,
        "cb": 0.04,
        "multiplier": 1.1,
    },
    "user": {
        "thresh": 0,
        "price": 0,
        "label": "Новичок 👤",
        "chance": 0.40,
        "all_in": 0.70,
        "cb": 0.00,
        "multiplier": 1.0,
    },
}


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
db_pool: asyncpg.Pool | None = None


def get_pool() -> asyncpg.Pool:
    if db_pool is None:
        raise RuntimeError("Пул PostgreSQL ещё не запущен")
    return db_pool


async def open_database() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=DB_POOL_MAX_SIZE,
        command_timeout=30,
    )


async def close_database() -> None:
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None


async def init_db() -> None:
    """Создаёт и безопасно дополняет схему PostgreSQL."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    total_whine BIGINT NOT NULL DEFAULT 0,
                    last_whine BIGINT NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'user',
                    is_premium BOOLEAN NOT NULL DEFAULT FALSE,
                    vip_expire TIMESTAMPTZ,
                    duel_wins INTEGER NOT NULL DEFAULT 0,
                    duel_losses INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Эти ALTER нужны, если база уже была создана более старой версией бота.
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS duel_wins INTEGER DEFAULT 0")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS duel_losses INTEGER DEFAULT 0")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_expire TIMESTAMPTZ")
            await conn.execute("UPDATE users SET duel_wins = 0 WHERE duel_wins IS NULL")
            await conn.execute("UPDATE users SET duel_losses = 0 WHERE duel_losses IS NULL")
            await conn.execute("UPDATE users SET total_whine = 0 WHERE total_whine IS NULL OR total_whine < 0")
            await conn.execute("UPDATE users SET last_whine = 0 WHERE last_whine IS NULL")
            await conn.execute("UPDATE users SET status = 'olympian' WHERE status = 'olymp'")

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value BIGINT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO settings (key, value)
                VALUES ('vault', 1000000000)
                ON CONFLICT (key) DO NOTHING
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_members (
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    chat_id BIGINT NOT NULL,
                    PRIMARY KEY (user_id, chat_id)
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS chat_members_chat_id_idx
                ON chat_members (chat_id)
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_status (
                    chat_id BIGINT PRIMARY KEY,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS duels (
                    duel_id TEXT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT,
                    p1_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    p1_name TEXT NOT NULL,
                    p2_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    p2_name TEXT NOT NULL,
                    p1_stake BIGINT NOT NULL DEFAULT 0,
                    p2_stake BIGINT NOT NULL DEFAULT 0,
                    bank BIGINT NOT NULL DEFAULT 0,
                    turn_id BIGINT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'fighting')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS duels_active_idx
                ON duels (chat_id, status, expires_at)
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    telegram_payment_charge_id TEXT PRIMARY KEY,
                    provider_payment_charge_id TEXT,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    payload TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # На рестарте не обнуляем накопления и казну: только создаём Архитектора,
            # если его ещё нет, и гарантируем его специальный статус.
            await conn.execute(
                """
                INSERT INTO users (user_id, name, total_whine, status)
                VALUES ($1, 'Архитектор', 200, 'architect')
                ON CONFLICT (user_id) DO UPDATE SET status = 'architect'
                """,
                ARCHITECT_ID,
            )


async def set_chat_active(chat_id: int, is_active: bool) -> None:
    await get_pool().execute(
        """
        INSERT INTO chat_status (chat_id, is_active)
        VALUES ($1, $2)
        ON CONFLICT (chat_id)
        DO UPDATE SET is_active = EXCLUDED.is_active
        """,
        chat_id,
        is_active,
    )


async def is_chat_on(chat_id: int) -> bool:
    value = await get_pool().fetchval(
        "SELECT is_active FROM chat_status WHERE chat_id = $1",
        chat_id,
    )
    return True if value is None else bool(value)


async def _register_in_chat(conn: asyncpg.Connection, user_id: int, chat_id: int) -> None:
    await conn.execute(
        """
        INSERT INTO chat_members (user_id, chat_id)
        VALUES ($1, $2)
        ON CONFLICT (user_id, chat_id) DO NOTHING
        """,
        user_id,
        chat_id,
    )


async def register_in_chat(user_id: int, chat_id: int) -> None:
    async with get_pool().acquire() as conn:
        await _register_in_chat(conn, user_id, chat_id)


async def _ensure_user(conn: asyncpg.Connection, user: types.User, chat_id: int) -> None:
    status = "architect" if user.id == ARCHITECT_ID else "user"
    name = (user.first_name or user.username or str(user.id)).strip()[:100]
    await conn.execute(
        """
        INSERT INTO users (user_id, name, status)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE
        SET status = CASE
            WHEN users.user_id = $4 THEN 'architect'
            ELSE users.status
        END
        """,
        user.id,
        name,
        status,
        ARCHITECT_ID,
    )
    await _register_in_chat(conn, user.id, chat_id)


async def ensure_user(user: types.User, chat_id: int) -> None:
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user(conn, user, chat_id)


async def get_u(user_id: int) -> dict[str, Any] | None:
    row = await get_pool().fetchrow(
        """
        SELECT name, total_whine, last_whine, status, is_premium,
               vip_expire, duel_wins, duel_losses
        FROM users
        WHERE user_id = $1
        """,
        user_id,
    )
    if row is None:
        return None
    return {
        "name": row["name"],
        "total": row["total_whine"],
        "last": row["last_whine"],
        "status": row["status"],
        "is_p": row["is_premium"],
        "exp": row["vip_expire"],
        "wins": row["duel_wins"],
        "losses": row["duel_losses"],
    }


def rank_for_total(total: int) -> str:
    for rank_key, config in RANKS.items():
        if total >= int(config["thresh"]):
            return rank_key
    return "user"


async def _refresh_user_rank(conn: asyncpg.Connection, user_id: int) -> None:
    row = await conn.fetchrow(
        """
        SELECT total_whine, status, is_premium, vip_expire
        FROM users
        WHERE user_id = $1
        FOR UPDATE
        """,
        user_id,
    )
    if row is None or user_id == ARCHITECT_ID:
        return

    vip_expire = row["vip_expire"]
    premium_active = bool(
        row["is_premium"]
        and vip_expire is not None
        and vip_expire > datetime.now(timezone.utc)
    )
    if premium_active:
        return

    new_status = rank_for_total(row["total_whine"])
    await conn.execute(
        """
        UPDATE users
        SET status = $2, is_premium = FALSE, vip_expire = NULL
        WHERE user_id = $1
        """,
        user_id,
        new_status,
    )


async def update_score(user_id: int, amount: int, update_time: bool = False) -> dict[str, Any] | None:
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users
                SET total_whine = GREATEST(0, total_whine + $2),
                    last_whine = CASE WHEN $3 THEN $4 ELSE last_whine END
                WHERE user_id = $1
                RETURNING user_id
                """,
                user_id,
                amount,
                update_time,
                int(datetime.now(timezone.utc).timestamp()),
            )
            if row is None:
                return None
            await _refresh_user_rank(conn, user_id)
    return await get_u(user_id)


async def set_user_name(user_id: int, new_name: str) -> bool:
    result = await get_pool().execute(
        "UPDATE users SET name = $2 WHERE user_id = $1",
        user_id,
        new_name,
    )
    return result == "UPDATE 1"


async def get_global_leaderboard(limit: int = 20) -> list[asyncpg.Record]:
    return await get_pool().fetch(
        """
        SELECT name, total_whine, status, user_id, duel_wins, duel_losses
        FROM users
        WHERE total_whine > 0
        ORDER BY total_whine DESC, user_id
        LIMIT $1
        """,
        limit,
    )


def get_duel_rank(wins: int, losses: int) -> str:
    total = wins + losses
    if total < 3:
        return "Новичок 🐣"
    win_rate = wins / total * 100
    if win_rate >= 80:
        return "БОГ ДУЭЛЕЙ 🌌⚡️"
    if win_rate >= 75:
        return "Серийный убийца 💀"
    if win_rate >= 60:
        return "Стрелок 🔫"
    if win_rate < 50:
        return "Салага 🐥"
    return "Боец 🥊"


def html_tag(user: types.User) -> str:
    name = escape(user.first_name or user.username or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def stored_user_tag(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(name)}</a>'


def parse_plus_args(message: Message, command_name: str) -> str:
    text = message.text or ""
    return text[len(command_name):].strip()


def parse_positive_int(raw: str | None) -> int | None:
    token = (raw or "").strip().split(maxsplit=1)[0] if (raw or "").strip() else ""
    return int(token) if token.isdigit() and int(token) > 0 else None


MUTE_RE = re.compile(r"^(\d+)\s*(с|сек|s|м|мин|m|ч|час|h|д|дн|d)?$", re.IGNORECASE)


def parse_mute_seconds(raw: str | None) -> int | None:
    token = (raw or "").strip().split(maxsplit=1)[0] if (raw or "").strip() else ""
    match = MUTE_RE.fullmatch(token)
    if not match:
        return None
    amount = int(match.group(1))
    suffix = (match.group(2) or "м").lower()
    multiplier = {
        "с": 1,
        "сек": 1,
        "s": 1,
        "м": 60,
        "мин": 60,
        "m": 60,
        "ч": 3600,
        "час": 3600,
        "h": 3600,
        "д": 86400,
        "дн": 86400,
        "d": 86400,
    }[suffix]
    seconds = amount * multiplier
    return seconds if seconds > 0 else None


def is_group(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
    except Exception:
        logger.exception("Не удалось проверить права пользователя %s в чате %s", user_id, chat_id)
        return False


async def can_architect_or_olympian_mute(message: Message, seconds: int) -> tuple[bool, str]:
    if message.from_user is None:
        return False, "🚫 Не удалось определить отправителя."
    if message.from_user.id == ARCHITECT_ID:
        return True, "architect"
    user = await get_u(message.from_user.id)
    if user and user["status"] == "olympian":
        if seconds > 10 * 60:
            return False, "🏛️ Олимпиец может мутить максимум на 10 минут."
        return True, "olympian"
    return False, "🚫 Команда доступна только Архитектору или Олимпийцу."


MUTED_CHAT_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)


async def play_casino(message: Message, arg_text: str, usage: str) -> None:
    if message.from_user is None or not await is_chat_on(message.chat.id):
        return
    user_id = message.from_user.id
    await ensure_user(message.from_user, message.chat.id)
    value = parse_positive_int(arg_text)
    if value is None:
        await message.answer(f"⚠️ Пиши сумму: <code>{escape(usage)}</code>")
        return

    # Баланс блокируется до конца ставки: два быстрых нажатия не могут
    # одновременно потратить одни и те же дБ.
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                """
                SELECT name, total_whine, status
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            if user is None:
                return
            if value > user["total_whine"]:
                outcome = "insufficient"
            else:
                config = RANKS.get(user["status"], RANKS["user"])
                if user_id == ARCHITECT_ID:
                    config = RANKS["bronze"]
                is_all_in = value == user["total_whine"]
                chance = config["all_in"] if is_all_in else config["chance"]
                if random.random() < chance:
                    win = int(value * (1.2 if is_all_in else 2.0))
                    jackpot = not is_all_in and random.random() > 0.93
                    if jackpot:
                        win = value * 4
                    delta = win - value
                    outcome = "win"
                    cashback = 0
                else:
                    cashback = int(value * config.get("cb", 0))
                    delta = -value + cashback
                    outcome = "loss"
                    win = 0
                    jackpot = False
                await conn.execute(
                    """
                    UPDATE users
                    SET total_whine = GREATEST(0, total_whine + $2)
                    WHERE user_id = $1
                    """,
                    user_id,
                    delta,
                )
                await _refresh_user_rank(conn, user_id)

    user_tag = stored_user_tag(user_id, user["name"])
    if outcome == "insufficient":
        await message.answer(
            f"🚫 {user_tag}, у тебя только <b>{user['total_whine']} дБ</b>! "
            "Ты нищееб никчемный, к сожалению!"
        )
        return
    if outcome == "win":
        if jackpot:
            text = (
                f"🎰 {user_tag}, ДЖЕКПОТ! БОГИ СЛЫШАТ ТВОЙ СКУЛЁЖ! "
                f"ТЫ УСМАН ДЕМБЕЛЕ: <b>+{win} дБ</b>!"
            )
        else:
            text = f"🎰 {user_tag}, КУШ! Как же ты ебешь, Боже: <b>+{win} дБ</b>!"
        await message.answer(text)
    else:
        await message.answer(
            f"🎰 {user_tag}, ставка <b>{value} дБ</b> сгорела, иди скули, пёс! "
            f"Кэшбек: {cashback} 📉"
        )


@dp.message(Command("skulistart"))
async def start(message: Message) -> None:
    if message.from_user is None:
        return
    await ensure_user(message.from_user, message.chat.id)
    await message.answer("✅ Регистрация успешна! Твой баланс теперь един во всех чатах. Юзай /poskuli")


@dp.message(Command("poskuli"))
async def measure_whine(message: Message) -> None:
    if message.from_user is None or not await is_chat_on(message.chat.id):
        return

    user_id = message.from_user.id
    user = await get_u(user_id)
    if user is None:
        await message.answer("⚠️ Сначала нажми /skulistart, чтобы прибор тебя запомнил!")
        return
    await register_in_chat(user_id, message.chat.id)

    user_tag = stored_user_tag(user_id, user["name"])

    # Проверка кулдауна и начисление идут под одной блокировкой строки.
    # Поэтому параллельные апдейты Telegram не дают двойной замер.
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            locked_user = await conn.fetchrow(
                """
                SELECT total_whine, last_whine, status
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            now_ts = int(datetime.now(timezone.utc).timestamp())
            wait_time = locked_user["last_whine"] + COOLDOWN_MINUTES * 60 - now_ts
            if wait_time <= 0:
                multiplier = RANKS.get(locked_user["status"], RANKS["user"])["multiplier"]
                is_penalty = random.random() < 0.20
                if is_penalty:
                    amount = -random.randint(1, 5)
                else:
                    amount = int(random.randint(10, 200) * multiplier)
                new_total = await conn.fetchval(
                    """
                    UPDATE users
                    SET total_whine = GREATEST(0, total_whine + $2),
                        last_whine = $3
                    WHERE user_id = $1
                    RETURNING total_whine
                    """,
                    user_id,
                    amount,
                    now_ts,
                )
                await _refresh_user_rank(conn, user_id)

    if wait_time > 0:
        minutes, seconds = divmod(wait_time, 60)
        await message.answer(
            f"🚫⛔ {user_tag}, связки не восстановились!\n"
            f"Обожди ещё <b>{minutes}м {seconds}с</b>, маленький"
        )
        return

    if is_penalty:
        fails = [
            "Прибор определил это как полную хуету. 🥺 К сожалению, штраф! 🫵🤡",
            "Поскулил как уебок — минус вайб 👺👎",
            "Это был не скулёж, а зевок. Учись скулить у первых скулюнов! 💩☠️👀",
            "Ты начал ныть, но, к сожалению, подавился слюной! Нахуй иди 👺",
            "Паскудный скулёж — штраф дебилу! 🫵🤡",
            "Ты фанат Реала? 🤡 Что за пронзительный скулёж на судей? Не одобрено!",
            "Хави смеётся над тем, как ты слабо скулишь! Пробуй снова! 👀",
            "Какая же хуетень, угараем всей командой разработчиков! 💩👀",
            "Как же ты срёшь на ляшки, чел 🤡",
            "К сожалению, ты обосрался 🤡",
            "Доволен собой? Но это хуйня, к сожалению! 🫵🤡",
            "Что это за дрисня? 🤡 Иди поплачь!",
            "Так скулят только хуесосы! ШТРАФ! 🫵🤡",
            "ПОСКУЛИ, БЛЯДОТА! 🫵🤡 ШТРАФ!",
        ]
        loss = -amount
        await message.answer(
            f"📉 {user_tag}, <b>-{loss} дБ</b>!\n"
            f"❌ {random.choice(fails)}\nИтог: <b>{new_total} дБ</b>"
        )
        return

    gain = amount
    if gain < 40:
        moods = [
            "🤫 Тихое поскуливание",
            "🦴 Тихушник...",
            "😶 Почти не слышно, это шёпот?!",
            "🧕 Будешь так своей крале на ушко шептать",
        ]
    elif gain > 100:
        moods = [
            "📢 Скулишь пиздец! Ты Винисиус??",
            "🚨 Уши закладывает! Аккуратнее немного...",
            "🐺 Воешь как Флик после поражения Барсы!",
            "🦜 Скулёж дичайший! Как от фанатов Никогдарсенала",
            "🤡 Заскулил как Чмани!",
            "🤡 Ебать! Вой как от некого Зураба!",
        ]
    else:
        moods = [
            "🫨 Средний вой",
            "😐 Умеренный скулёж, ничего особенного",
            "🕯️ Звучит стабильно, как скучная игра Арсенала",
            "🐔 Не говно, но и не топ — ты Тоттенхэм!",
        ]
    mood = random.choice(moods)
    bonus = f" (Бонус ранга x{multiplier})" if multiplier > 1.0 else ""
    replies = [
        f"📈 {user_tag}, замер: <b>{gain} дБ</b>{bonus}\nℹ️ Статус: {mood}\nВсего накоплено: <b>{new_total} дБ</b>",
        f"🧭 {user_tag}, твоё новое значение — <b>{gain} дБ</b>{bonus}\n{mood}\n🔊 Текущий итог: <b>{new_total} дБ</b>",
        f"🎚️ {user_tag}, измерение показало <b>{gain} дБ</b>{bonus}\n🎧 {mood}\nСуммарно: <b>{new_total} дБ</b>",
    ]
    await message.answer(random.choice(replies))


@dp.message(Command("skulibet"))
async def bet(message: Message, command: CommandObject) -> None:
    await play_casino(message, command.args or "", "/skulibet 50")


@dp.message(F.text.regexp(r"(?i)^\+казик(?:\s|$)"))
async def plus_casino(message: Message) -> None:
    await play_casino(message, parse_plus_args(message, "+казик"), "+казик 50")


@dp.message(F.text.lower() == "+поскулить")
async def plus_measure_whine(message: Message) -> None:
    await measure_whine(message)


@dp.message(F.text.regexp(r"(?i)^\+перевод(?:\s|$)"))
async def transfer_db(message: Message) -> None:
    if message.from_user is None or not await is_chat_on(message.chat.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Перевод делается ответом на сообщение юзера: <code>+перевод 100</code>")
        return

    sender = message.from_user
    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.answer("🤖 Ботам дБ не переводи, дебил сука ебаный.")
        return
    if sender.id == target.id:
        await message.answer("🤡 Сам себе перевод? Ты внатуре пизданутая скотина")
        return
    amount = parse_positive_int(parse_plus_args(message, "+перевод"))
    if amount is None:
        await message.answer("⚠️ Формат: <code>+перевод 100</code> в ответ на сообщение получателя.")
        return

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user(conn, sender, message.chat.id)
            await _ensure_user(conn, target, message.chat.id)
            locked_users = await conn.fetch(
                """
                SELECT user_id, total_whine
                FROM users
                WHERE user_id = ANY($1::BIGINT[])
                ORDER BY user_id
                FOR UPDATE
                """,
                [sender.id, target.id],
            )
            balances = {row["user_id"]: row["total_whine"] for row in locked_users}
            sender_balance = balances[sender.id]
            if sender_balance < amount:
                enough = False
            else:
                enough = True
                await conn.execute(
                    "UPDATE users SET total_whine = total_whine - $2 WHERE user_id = $1",
                    sender.id,
                    amount,
                )
                await conn.execute(
                    "UPDATE users SET total_whine = total_whine + $2 WHERE user_id = $1",
                    target.id,
                    amount,
                )
                await _refresh_user_rank(conn, sender.id)
                await _refresh_user_rank(conn, target.id)

    if not enough:
        await message.answer(
            f"🚫 У тебя только <b>{sender_balance} дБ</b>. Не вывез перевод, скули дальше."
        )
        return
    await message.answer(
        f"💸 {html_tag(target)}, тебе перевод от {html_tag(sender)}!\n"
        f"<b>{amount} дБ</b> прилетело в карман. Скули на здоровье! 🐺🔊🥂"
    )


@dp.message(F.text.regexp(r"(?i)^\+списать(?:\s|$)"))
async def architect_take_db(message: Message) -> None:
    if message.from_user is None or message.from_user.id != ARCHITECT_ID:
        await message.answer("🚫 Казначейский нож только у Архитектора.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Формат: <code>+списать 100</code> ответом на сообщение жертвы.")
        return
    amount = parse_positive_int(parse_plus_args(message, "+списать"))
    if amount is None:
        await message.answer("⚠️ Формат: <code>+списать 100</code>")
        return

    target = message.reply_to_message.from_user
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user(conn, target, message.chat.id)
            balance = await conn.fetchval(
                "SELECT total_whine FROM users WHERE user_id = $1 FOR UPDATE",
                target.id,
            )
            taken = min(amount, balance)
            await conn.execute(
                "UPDATE users SET total_whine = total_whine - $2 WHERE user_id = $1",
                target.id,
                taken,
            )
            await conn.execute(
                "UPDATE settings SET value = value + $1 WHERE key = 'vault'",
                taken,
            )
            await _refresh_user_rank(conn, target.id)

    await message.answer(
        "🧾 <b>Аудит Асгарда</b>\n"
        f"С {html_tag(target)} списано <b>{taken} дБ</b>. Казна довольно урчит. 🏦"
    )


@dp.message(F.text.regexp(r"(?i)^\+мут(?:\s|$)"))
async def mute_user(message: Message) -> None:
    if message.from_user is None:
        return
    if not is_group(message):
        await message.answer("⚠️ Мут работает только в группе или супергруппе.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Мут делается ответом на сообщение: <code>+мут 10м</code> или <code>+мут 1ч</code>")
        return
    seconds = parse_mute_seconds(parse_plus_args(message, "+мут"))
    if seconds is None:
        await message.answer("⚠️ Формат: <code>+мут 10м</code>, <code>+мут 1ч</code>, <code>+мут 2д</code>. Без суффикса — минуты.")
        return
    allowed, role_or_message = await can_architect_or_olympian_mute(message, seconds)
    if not allowed:
        await message.answer(role_or_message)
        return

    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        await message.answer("🤡 Самомут? Ты ебанат что ли блядь?!")
        return
    if await is_chat_admin(message.chat.id, target.id):
        await message.answer("🛡️ Админов/создателя чата мутить нельзя. Ебанулся что ли?!")
        return
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=MUTED_CHAT_PERMISSIONS,
            until_date=datetime.now(timezone.utc) + timedelta(seconds=seconds),
            use_independent_chat_permissions=True,
        )
    except Exception as exc:
        logger.exception("Ошибка мута")
        await message.answer(
            "⚠️ Не смог замутить. Проверь, что бот — администратор с правом ограничивать участников. "
            f"Ошибка: <code>{escape(str(exc))}</code>"
        )
        return

    await message.answer(
        f"🔇 {html_tag(target)} хуесос отправлен под шконарь на <b>{seconds} сек.</b>\n"
        f"Исполнитель: {html_tag(message.from_user)}"
    )


FALLBACK_UNMUTED_CHAT_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


@dp.message(F.text.lower() == "-мут")
async def unmute_user(message: Message) -> None:
    if message.from_user is None or not is_group(message):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Ответь командой <code>-мут</code> на сообщение юзера.")
        return

    moderator = message.from_user
    target = message.reply_to_message.from_user
    if moderator.id == target.id:
        await message.answer("🤡 Сам себя размутить решил? Ебантяй нахуй!")
        return
    moderator_user = await get_u(moderator.id)
    if moderator_user is None:
        await message.answer("⚠️ Ты не зарегистрирован. /skulistart")
        return

    is_architect = moderator.id == ARCHITECT_ID
    is_olympian = moderator_user["status"] == "olympian"
    if not is_architect and not is_olympian:
        await message.answer("🚫 Ты куда полез, скотина припизднутая?!")
        return
    if not is_architect and await is_chat_admin(message.chat.id, target.id):
        await message.answer("⚡️ Олимпийцы не могут размутить админов.")
        return

    try:
        chat_info = await bot.get_chat(message.chat.id)
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=chat_info.permissions or FALLBACK_UNMUTED_CHAT_PERMISSIONS,
            use_independent_chat_permissions=True,
        )
    except Exception as exc:
        logger.exception("Ошибка размута")
        await message.answer(f"❌ Не удалось размутить.\nПричина: <code>{escape(str(exc))}</code>")
        return

    await message.answer(
        f"🔓 {html_tag(target)} вышел из-под шконки!\n"
        f"⚖️ Амнистия от пахана {html_tag(moderator)}"
    )


@dp.message(F.text.lower() == "+бан")
async def ban_user(message: Message) -> None:
    if message.from_user is None or message.from_user.id != ARCHITECT_ID:
        await message.answer("🚫 Банхаммер хранится только у Архитектора.")
        return
    if not is_group(message):
        await message.answer("⚠️ Бан работает только в группе или супергруппе.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Бан делается ответом на сообщение юзера: <code>+бан</code>")
        return
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        await message.answer("🤡 Самобан — сильно, почти как самодрочь, но нет.")
        return
    if await is_chat_admin(message.chat.id, target.id):
        await message.answer("🛡️ Админов/создателя чата банить нельзя, ты охуел?!")
        return
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
    except Exception as exc:
        logger.exception("Ошибка бана")
        await message.answer(
            f"⚠️ Не смог забанить. Проверь права бота. Ошибка: <code>{escape(str(exc))}</code>"
        )
        return
    await message.answer(f"🔨 {html_tag(target)} улетел из чата. Архитектор стукнул по ебалу хуебеса. 🌚")


@dp.message(Command("skuliname"))
async def change_name(message: Message, command: CommandObject) -> None:
    if message.from_user is None or not await is_chat_on(message.chat.id):
        return
    new_name = (command.args or "").strip()
    if not new_name:
        await message.answer("⚠️ Введи новое имя после команды: <code>/skuliname Сын Коке</code>")
        return
    if len(new_name) > 20:
        await message.answer("🚫 Слишком длинное погоняло! Максимум 20 символов.")
        return
    await ensure_user(message.from_user, message.chat.id)
    await set_user_name(message.from_user.id, new_name)
    await message.answer(f"🤝 К сожалению, теперь ты: <b>{escape(new_name)}</b>")


@dp.message(Command("grant"))
async def god_grant(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    if message.from_user.id != ARCHITECT_ID:
        user = await get_u(message.from_user.id)
        if user and user["is_p"]:
            await message.answer("Прости, премиальный нытик, но для тебя это скрытая функция 🥷")
        else:
            await message.answer("Олимп разгневан, иди скули, чушкан недоебаный! 🐷")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Ответь на сообщение: <code>/grant 100</code>")
        return
    amount = parse_positive_int(command.args)
    if amount is None:
        await message.answer("⚠️ Формат: <code>/grant 100</code>")
        return

    target = message.reply_to_message.from_user
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user(conn, target, message.chat.id)
            vault = await conn.fetchval("SELECT value FROM settings WHERE key = 'vault' FOR UPDATE")
            if vault < amount:
                granted = False
            else:
                granted = True
                await conn.execute("UPDATE settings SET value = value - $1 WHERE key = 'vault'", amount)
                await conn.execute(
                    "UPDATE users SET total_whine = total_whine + $2 WHERE user_id = $1",
                    target.id,
                    amount,
                )
                await _refresh_user_rank(conn, target.id)

    if not granted:
        await message.answer("🏦 Казна Олимпа пуста! Бартомеу блядь!")
        return
    with contextlib.suppress(Exception):
        await message.delete()
    await message.answer(
        "⚡️ <b>Глас Олимпа</b>\n\n"
        f"{html_tag(target)}, ты скулил так, что тебя услышали на Олимпе! "
        f"Тебе послали бонус и леща <b>{amount} дБ</b>!"
    )


@dp.message(Command("topskuli"))
async def top_chat(message: Message) -> None:
    if message.from_user is None or not await is_chat_on(message.chat.id):
        return
    await ensure_user(message.from_user, message.chat.id)
    rows = await get_pool().fetch(
        """
        SELECT u.name, u.total_whine, u.status, u.user_id, u.duel_wins, u.duel_losses
        FROM users AS u
        JOIN chat_members AS cm ON cm.user_id = u.user_id
        WHERE cm.chat_id = $1
        ORDER BY u.total_whine DESC, u.user_id
        LIMIT 10
        """,
        message.chat.id,
    )
    if not rows:
        await message.answer("📭 В этом чате ещё никто не скулил.")
        return

    lines = ["🏆 <b>ТОП НЫТИКОВ ЧАТА:</b>", ""]
    for index, row in enumerate(rows, 1):
        duel_rank = get_duel_rank(row["duel_wins"], row["duel_losses"])
        if row["user_id"] == ARCHITECT_ID:
            prefix = "🌚🔧"
        else:
            prefix = RANKS.get(row["status"], RANKS["user"])["label"].split()[-1]
        lines.append(
            f"{index}. {prefix} {escape(row['name'])} — <code>{row['total_whine']} дБ</code> | "
            f"{duel_rank} ({row['duel_wins']}W/{row['duel_losses']}L)"
        )

    local_rank = await get_pool().fetchval(
        """
        SELECT COUNT(*) + 1
        FROM users AS u
        JOIN chat_members AS cm ON cm.user_id = u.user_id
        WHERE cm.chat_id = $1
          AND u.total_whine > COALESCE(
              (SELECT total_whine FROM users WHERE user_id = $2), 0
          )
        """,
        message.chat.id,
        message.from_user.id,
    )
    lines.extend(["", "________________________________", f"📍 Твоё место в этом чате: <b>#{local_rank}</b>"])
    await message.answer("\n".join(lines))


@dp.message(Command("topglobal"))
async def global_top_handler(message: Message) -> None:
    if message.from_user is None or not await is_chat_on(message.chat.id):
        return
    top_users = await get_global_leaderboard(20)
    if not top_users:
        await message.answer("🌌 Во вселенной скулёж ещё не зафиксирован.")
        return

    lines = ["🌌 <b>ГЛОБАЛЬНЫЙ РЕЙТИНГ ВСЕЛЕННОЙ</b>", "________________________________", ""]
    for index, row in enumerate(top_users, 1):
        duel_rank = get_duel_rank(row["duel_wins"], row["duel_losses"])
        prefix = (
            "🌚🔧"
            if row["user_id"] == ARCHITECT_ID
            else RANKS.get(row["status"], RANKS["user"])["label"].split()[-1]
        )
        lines.append(
            f"{index}. {prefix} {escape(row['name'])} — <code>{row['total_whine']} дБ</code> | "
            f"{duel_rank} ({row['duel_wins']}W/{row['duel_losses']}L)"
        )

    balance = await get_pool().fetchval(
        "SELECT total_whine FROM users WHERE user_id = $1",
        message.from_user.id,
    )
    if balance is None:
        rank_text = "❔ (не в базе)"
    else:
        user_rank = await get_pool().fetchval(
            "SELECT COUNT(*) + 1 FROM users WHERE total_whine > $1",
            balance,
        )
        rank_text = f"<b>#{user_rank}</b>"
    lines.extend(
        [
            "",
            "________________________________",
            f"👤 Твоё место в мировом рейтинге: {rank_text}",
            "ℹ️ <i>Рейтинг един для всех чатов</i>",
        ]
    )
    await message.answer("\n".join(lines))


@dp.message(F.text.lower() == "+скули")
async def bot_on(message: Message) -> None:
    if message.from_user is None or not is_group(message):
        return
    if await is_chat_admin(message.chat.id, message.from_user.id):
        await set_chat_active(message.chat.id, True)
        await message.answer("🔊 <b>Прибор замера прогрет!</b> Бот включён. Скулите на здоровье!")
    else:
        await message.answer(f"{html_tag(message.from_user)}, админом себя почухал?! Чеши в стойло! 🐽")


@dp.message(F.text.lower() == "-скули")
async def bot_off(message: Message) -> None:
    if message.from_user is None or not is_group(message):
        return
    if await is_chat_admin(message.chat.id, message.from_user.id):
        await set_chat_active(message.chat.id, False)
        await message.answer("💤 <b>Бот ушёл в спячку.</b> Скулёж в этом чате больше не фиксируется.")
    else:
        await message.answer(f"{html_tag(message.from_user)}, админом себя почухал?! Чеши в стойло! 🐽")


@dp.message(Command("vault"), F.from_user.id == ARCHITECT_ID)
async def check_vault(message: Message) -> None:
    value = await get_pool().fetchval("SELECT value FROM settings WHERE key = 'vault'")
    await message.answer(f"💰 <b>Запасы Асгарда:</b>\n<code>{value:,} дБ</code>")


async def cleanup_expired_duels() -> int:
    """Удаляет просроченные вызовы и возвращает заблокированные ставки."""
    cleaned = 0
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT duel_id, status, p1_id, p2_id, p1_stake, p2_stake
                FROM duels
                WHERE expires_at <= NOW()
                FOR UPDATE SKIP LOCKED
                """
            )
            for row in rows:
                if row["status"] == "fighting":
                    await conn.execute(
                        "UPDATE users SET total_whine = total_whine + $2 WHERE user_id = $1",
                        row["p1_id"],
                        row["p1_stake"],
                    )
                    await conn.execute(
                        "UPDATE users SET total_whine = total_whine + $2 WHERE user_id = $1",
                        row["p2_id"],
                        row["p2_stake"],
                    )
                    await _refresh_user_rank(conn, row["p1_id"])
                    await _refresh_user_rank(conn, row["p2_id"])
                await conn.execute("DELETE FROM duels WHERE duel_id = $1", row["duel_id"])
                cleaned += 1
    return cleaned


async def duel_cleanup_loop() -> None:
    while True:
        try:
            cleaned = await cleanup_expired_duels()
            if cleaned:
                logger.info("Очищено просроченных дуэлей: %s", cleaned)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Не удалось очистить просроченные дуэли")
        await asyncio.sleep(60)


@dp.message(F.text.lower() == "+дуэль")
async def duel_request(message: Message) -> None:
    if message.from_user is None or not await is_chat_on(message.chat.id):
        return
    if not is_group(message):
        await message.answer("⚠️ Дуэли работают только в группе или супергруппе.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Чтобы вызвать на дуэль, ответь на сообщение противника текстом <code>+дуэль</code>!")
        return

    player1 = message.from_user
    player2 = message.reply_to_message.from_user
    if player2.is_bot:
        await message.answer("🤖 С ботами дуэлей нет.")
        return
    if player1.id == player2.id:
        await message.answer("Самострел? Не в мою смену. 🤡")
        return

    await cleanup_expired_duels()
    duel_id = uuid.uuid4().hex[:16]
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            users = await conn.fetch(
                """
                SELECT user_id, name, total_whine
                FROM users
                WHERE user_id = ANY($1::BIGINT[])
                ORDER BY user_id
                FOR UPDATE
                """,
                [player1.id, player2.id],
            )
            by_id = {row["user_id"]: row for row in users}
            if player1.id not in by_id or player2.id not in by_id:
                error = "Оба нытика должны быть в базе (/skulistart)"
            elif by_id[player1.id]["total_whine"] <= 0 or by_id[player2.id]["total_whine"] <= 0:
                error = "У нищих дуэлей не бывает. Наскулите хоть что-то. 💸"
            else:
                busy = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM duels
                        WHERE chat_id = $1
                          AND expires_at > NOW()
                          AND status IN ('pending', 'fighting')
                          AND (
                              p1_id = ANY($2::BIGINT[])
                              OR p2_id = ANY($2::BIGINT[])
                          )
                    )
                    """,
                    message.chat.id,
                    [player1.id, player2.id],
                )
                if busy:
                    error = "⏳ Один из участников уже занят другой дуэлью в этом чате."
                else:
                    error = None
                    bank = by_id[player1.id]["total_whine"] + by_id[player2.id]["total_whine"]
                    await conn.execute(
                        """
                        INSERT INTO duels (
                            duel_id, chat_id, p1_id, p1_name, p2_id, p2_name,
                            bank, turn_id, status, expires_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $3, 'pending', $8)
                        """,
                        duel_id,
                        message.chat.id,
                        player1.id,
                        by_id[player1.id]["name"],
                        player2.id,
                        by_id[player2.id]["name"],
                        bank,
                        datetime.now(timezone.utc) + timedelta(minutes=DUEL_PENDING_MINUTES),
                    )

    if error:
        await message.answer(error)
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ ПРИНЯТЬ (ВА-БАНК)", callback_data=f"d_acc_{duel_id}"),
                InlineKeyboardButton(text="🏳️ СЛИТЬСЯ КАК ЧУШКАН", callback_data=f"d_dec_{duel_id}"),
            ]
        ]
    )
    sent = await message.answer(
        "⚔️ <b>ДУЭЛЬ НА ВЫЖИВАНИЕ!</b>\n\n"
        f"👤 {escape(by_id[player1.id]['name'])} сгорел и вызывает {escape(by_id[player2.id]['name'])}!\n"
        f"💰 <b>Предварительный банк:</b> {bank} дБ\n\n"
        f"<i>Вызов действует {DUEL_PENDING_MINUTES} минут. Проигравший обнуляется.</i>",
        reply_markup=keyboard,
    )
    await get_pool().execute(
        "UPDATE duels SET message_id = $2 WHERE duel_id = $1",
        duel_id,
        sent.message_id,
    )


async def callback_message(call: types.CallbackQuery) -> Message | None:
    if not isinstance(call.message, Message):
        await call.answer("Сообщение больше недоступно.", show_alert=True)
        return None
    return call.message


async def show_shoot_round(message: Message, duel: asyncpg.Record | dict[str, Any]) -> None:
    turn_name = duel["p1_name"] if duel["turn_id"] == duel["p1_id"] else duel["p2_name"]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💥 ВЫСТРЕЛ!", callback_data=f"d_shot_{duel['duel_id']}")]
        ]
    )
    await message.edit_text(
        f"🔫 <b>ОЧЕРЕДЬ СТРЕЛЯТЬ:</b> {escape(turn_name)}\n"
        f"💰 Банк: {duel['bank']} дБ\n\n"
        "<i>Кто же отправится бомжевать первым?...</i>",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("d_acc_"))
async def d_accept(call: types.CallbackQuery) -> None:
    message = await callback_message(call)
    if message is None or call.data is None:
        return
    duel_id = call.data.removeprefix("d_acc_")
    await cleanup_expired_duels()

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            duel = await conn.fetchrow("SELECT * FROM duels WHERE duel_id = $1 FOR UPDATE", duel_id)
            if duel is None:
                outcome = "missing"
            elif call.from_user.id != duel["p2_id"]:
                outcome = "wrong_user"
            elif duel["status"] == "fighting":
                outcome = "already_fighting"
            else:
                balances = await conn.fetch(
                    """
                    SELECT user_id, total_whine
                    FROM users
                    WHERE user_id = ANY($1::BIGINT[])
                    ORDER BY user_id
                    FOR UPDATE
                    """,
                    [duel["p1_id"], duel["p2_id"]],
                )
                balance_by_id = {row["user_id"]: row["total_whine"] for row in balances}
                p1_stake = balance_by_id.get(duel["p1_id"], 0)
                p2_stake = balance_by_id.get(duel["p2_id"], 0)
                if p1_stake <= 0 or p2_stake <= 0:
                    await conn.execute("DELETE FROM duels WHERE duel_id = $1", duel_id)
                    outcome = "no_money"
                else:
                    await conn.execute(
                        "UPDATE users SET total_whine = 0 WHERE user_id = ANY($1::BIGINT[])",
                        [duel["p1_id"], duel["p2_id"]],
                    )
                    duel = await conn.fetchrow(
                        """
                        UPDATE duels
                        SET p1_stake = $2,
                            p2_stake = $3,
                            bank = $2 + $3,
                            status = 'fighting',
                            expires_at = $4
                        WHERE duel_id = $1
                        RETURNING *
                        """,
                        duel_id,
                        p1_stake,
                        p2_stake,
                        datetime.now(timezone.utc) + timedelta(minutes=DUEL_FIGHT_MINUTES),
                    )
                    await _refresh_user_rank(conn, duel["p1_id"])
                    await _refresh_user_rank(conn, duel["p2_id"])
                    outcome = "accepted"

    if outcome == "missing":
        await call.answer("Вызов истёк или уже завершён.", show_alert=True)
    elif outcome == "wrong_user":
        await call.answer("Это не твой вызов! 👺", show_alert=True)
    elif outcome == "no_money":
        await call.answer("У одного из дуэлянтов уже нет дБ. Вызов отменён.", show_alert=True)
        await message.edit_text("💸 Дуэль отменена: один из участников успел обнищать.")
    else:
        await call.answer("Дуэль принята!" if outcome == "accepted" else "Дуэль уже идёт.")
        await show_shoot_round(message, duel)


@dp.callback_query(F.data.startswith("d_shot_"))
async def d_shoot(call: types.CallbackQuery) -> None:
    message = await callback_message(call)
    if message is None or call.data is None:
        return
    duel_id = call.data.removeprefix("d_shot_")
    await cleanup_expired_duels()

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            duel = await conn.fetchrow("SELECT * FROM duels WHERE duel_id = $1 FOR UPDATE", duel_id)
            if duel is None:
                outcome = "missing"
            elif duel["status"] != "fighting":
                outcome = "not_started"
            elif call.from_user.id != duel["turn_id"]:
                outcome = "wrong_turn"
            elif random.random() < 0.35:
                outcome = "hit"
                winner_id = duel["turn_id"]
                loser_id = duel["p2_id"] if winner_id == duel["p1_id"] else duel["p1_id"]
                winner_name = duel["p1_name"] if winner_id == duel["p1_id"] else duel["p2_name"]
                await conn.execute(
                    """
                    UPDATE users
                    SET total_whine = total_whine + $2,
                        duel_wins = duel_wins + 1
                    WHERE user_id = $1
                    """,
                    winner_id,
                    duel["bank"],
                )
                await conn.execute(
                    """
                    UPDATE users
                    SET total_whine = 0,
                        duel_losses = duel_losses + 1
                    WHERE user_id = $1
                    """,
                    loser_id,
                )
                await _refresh_user_rank(conn, winner_id)
                await _refresh_user_rank(conn, loser_id)
                await conn.execute("DELETE FROM duels WHERE duel_id = $1", duel_id)
            else:
                outcome = "miss"
                next_turn = duel["p2_id"] if duel["turn_id"] == duel["p1_id"] else duel["p1_id"]
                duel = await conn.fetchrow(
                    "UPDATE duels SET turn_id = $2 WHERE duel_id = $1 RETURNING *",
                    duel_id,
                    next_turn,
                )

    if outcome == "missing":
        await call.answer("Дуэль истекла или уже завершена.", show_alert=True)
    elif outcome == "not_started":
        await call.answer("Сначала противник должен принять вызов.", show_alert=True)
    elif outcome == "wrong_turn":
        await call.answer("Сейчас не твой ход! ⏳", show_alert=True)
    elif outcome == "hit":
        await call.answer("Попадание!")
        death_phrase = random.choice(["ПОТРАЧЕНО! ⚰️", "В КАНАВУ! 🕳", "ОТКИС! 🧊", "ЗЕМЛЯ ПУХОМ! 🪦"])
        await message.edit_text(
            f"💀 <b>{death_phrase}</b>\n\n"
            f"🎯 Победитель забрал <b>{duel['bank']} дБ</b>!\n"
            f"🏆 Чемпион: <b>{escape(winner_name)}</b>\n"
            "📉 Проигравший обнулён. Иди скули с нуля! 🐷"
        )
    else:
        miss_phrase = random.choice(
            [
                "МИМО! Пуля просвистела мимо уха... 💨",
                "КОСОЙ! Даже Дарвин Нуньес бы попал... 💩",
                "РИКОШЕТ! Пуля улетела в Мадрид! ✈️",
                "ОСЕЧКА! Твой ствол заклинило! 🔫🤡",
                "МАЗИЛА! Иди тренируйся на чушканах! 🐽",
                "ПЕРЕЛЁТ! Ты куда стреляешь, чучело? 👺",
            ]
        )
        await call.answer(miss_phrase)
        await show_shoot_round(message, duel)


@dp.callback_query(F.data.startswith("d_dec_"))
async def d_decline(call: types.CallbackQuery) -> None:
    message = await callback_message(call)
    if message is None or call.data is None:
        return
    duel_id = call.data.removeprefix("d_dec_")
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            duel = await conn.fetchrow("SELECT * FROM duels WHERE duel_id = $1 FOR UPDATE", duel_id)
            if duel is None:
                outcome = "missing"
            elif call.from_user.id not in {duel["p1_id"], duel["p2_id"]}:
                outcome = "wrong_user"
            elif duel["status"] != "pending":
                outcome = "fighting"
            else:
                await conn.execute("DELETE FROM duels WHERE duel_id = $1", duel_id)
                outcome = "declined"

    if outcome == "missing":
        await call.answer("Вызов уже исчез.", show_alert=True)
    elif outcome == "wrong_user":
        await call.answer("Это не твоя дуэль.", show_alert=True)
    elif outcome == "fighting":
        await call.answer("Поздно сливаться — дуэль уже началась.", show_alert=True)
    else:
        await call.answer("Дуэль отменена.")
        await message.edit_text("🏳️ Дуэль отменена. Один из нытиков поджал хвост и убежал. 🐕‍🦺")


@dp.message(Command("shop"))
async def shop(message: Message) -> None:
    if not await is_chat_on(message.chat.id):
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{config['label']} ({config['price']} ⭐️)",
                    callback_data=f"buy_{rank_key}",
                )
            ]
            for rank_key, config in RANKS.items()
            if config["price"] > 0
        ]
    )
    await message.answer(
        "🏪 <b>Магазин Скулежа</b>\nВыбери статус на 30 дней (защита от слива ранга):",
        reply_markup=keyboard,
    )


@dp.message(Command("info"))
async def info_handler(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE and not await is_chat_on(message.chat.id):
        return
    text = (
        "ℹ️ <b>ИНФОРМАЦИЯ О ПОСКУЛИБОТЕ</b> ℹ️\n"
        "________________________________\n\n"
        "🎮 <b>ОСНОВНЫЕ КОМАНДЫ:</b>\n"
        "• <code>/skulistart</code> — регистрация в системе\n"
        "• <code>/poskuli</code> или <code>+поскулить</code> — замер скулежа\n"
        "• <code>/skulibet 50</code> или <code>+казик 50</code> — казино\n"
        "• <code>+перевод 100</code> ответом — перевести дБ 💸\n"
        "• <code>+дуэль</code> ответом — дуэль ва-банк ⚔️\n"
        "• <code>/skuliname Имя</code> — сменить погоняло\n"
        "• <code>/topskuli</code> — топ чата\n"
        "• <code>/topglobal</code> — мировой рейтинг\n"
        "• <code>/shop</code> — магазин статусов ⭐️\n\n"
        "📈 <b>РАНГИ ЗА дБ:</b>\n"
        "• 10к — КМС 🚀\n• 30к — МС 🌠\n• 100к — Ангел 👑\n"
        "• 500к — Бог 💎\n• 1.5м — Всемогущий 🌌\n"
        "• 1 млрд — Олимпиец 🏛️ (<code>+мут</code> до 10 минут)\n"
        "<i>Если дБ упадут ниже порога, бесплатный статус слетает.</i>\n\n"
        "💎 <b>ПРИВИЛЕГИИ:</b>\n"
        "1. Купленный статус защищён 30 дней.\n"
        "2. Множитель замера — до x3.0 у Олимпийца.\n"
        "3. На высоких рангах казино строже.\n"
        "4. Олимпиец может мутить максимум на 10 минут, кроме админов.\n"
        "5. Архитектор: <code>+списать</code>, <code>+мут</code>, <code>+бан</code>, "
        "<code>/grant</code>, <code>/vault</code>.\n\n"
        "⚙️ <i>Архитектор слышит твой скулёж...</i>"
    )
    await message.answer(text, link_preview_options=types.LinkPreviewOptions(is_disabled=True))


@dp.callback_query(F.data.startswith("buy_"))
async def buy_call(call: types.CallbackQuery) -> None:
    message = await callback_message(call)
    if message is None or call.data is None:
        return
    if not await is_chat_on(message.chat.id):
        await call.answer("💤 Бот спит, магазин закрыт.", show_alert=True)
        return
    rank_key = call.data.removeprefix("buy_")
    config = RANKS.get(rank_key)
    if config is None or config["price"] <= 0:
        await call.answer("Такого товара нет.", show_alert=True)
        return
    await bot.send_invoice(
        chat_id=message.chat.id,
        title=f"Статус {config['label']}",
        description="Привилегии на 30 дней",
        payload=f"pay_{rank_key}",
        currency="XTR",
        prices=[LabeledPrice(label="⭐️", amount=config["price"])],
    )
    await call.answer()


def paid_rank_from_payload(payload: str) -> str | None:
    if not payload.startswith("pay_"):
        return None
    rank_key = payload.removeprefix("pay_")
    config = RANKS.get(rank_key)
    if config is None or config["price"] <= 0:
        return None
    return rank_key


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    rank_key = paid_rank_from_payload(query.invoice_payload)
    valid = bool(
        rank_key
        and query.currency == "XTR"
        and query.total_amount == RANKS[rank_key]["price"]
    )
    await bot.answer_pre_checkout_query(
        query.id,
        ok=valid,
        error_message=None if valid else "Цена или товар устарели. Открой /shop заново.",
    )


@dp.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    if message.from_user is None or message.successful_payment is None:
        return
    payment = message.successful_payment
    rank_key = paid_rank_from_payload(payment.invoice_payload)
    if (
        rank_key is None
        or payment.currency != "XTR"
        or payment.total_amount != RANKS[rank_key]["price"]
    ):
        logger.error("Получен платёж с неверными параметрами: %r", payment)
        await message.answer("⚠️ Платёж получен, но товар не распознан. Обратись к Архитектору.")
        return

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user(conn, message.from_user, message.chat.id)
            inserted = await conn.fetchval(
                """
                INSERT INTO payments (
                    telegram_payment_charge_id, provider_payment_charge_id,
                    user_id, payload, currency, amount
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (telegram_payment_charge_id) DO NOTHING
                RETURNING 1
                """,
                payment.telegram_payment_charge_id,
                payment.provider_payment_charge_id,
                message.from_user.id,
                payment.invoice_payload,
                payment.currency,
                payment.total_amount,
            )
            if inserted:
                await conn.execute(
                    """
                    UPDATE users
                    SET status = $2,
                        is_premium = TRUE,
                        vip_expire = GREATEST(COALESCE(vip_expire, NOW()), NOW())
                                     + INTERVAL '30 days'
                    WHERE user_id = $1
                    """,
                    message.from_user.id,
                    rank_key,
                )

    if inserted:
        await message.answer(
            f"✨ ТЫ СРЕДИ БОГОВ ОЛИМПА! Ты теперь <b>{escape(RANKS[rank_key]['label'])}</b>! "
            "Скули с привилегиями!"
        )
    else:
        await message.answer("✅ Этот платёж уже был обработан.")


async def main() -> None:
    await open_database()
    cleanup_task: asyncio.Task[None] | None = None
    try:
        await init_db()
        await cleanup_expired_duels()
        cleanup_task = asyncio.create_task(duel_cleanup_loop(), name="duel-cleanup")
        logger.info("Бот запущен; PostgreSQL подключён")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        await close_database()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())


