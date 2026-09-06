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
from typing import Any, Iterable

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("poskuli_bot")


# ============================================================
# ENV
# ============================================================

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения {name}"
        )
    return value


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)

    if raw is None:
        return default

    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Переменная {name} должна быть целым числом"
        ) from exc


BOT_TOKEN = required_env("BOT_TOKEN")
DATABASE_URL = required_env("DATABASE_URL")

ARCHITECT_ID = env_int("ARCHITECT_ID", 6421600902)

COOLDOWN_MINUTES = env_int("COOLDOWN_MINUTES", 4)

DB_POOL_MAX_SIZE = max(
    1,
    env_int("DB_POOL_MAX_SIZE", 5),
)

RUN_MODE = os.getenv(
    "RUN_MODE",
    "polling",
).strip().lower()

WEBHOOK_BASE_URL = os.getenv(
    "WEBHOOK_BASE_URL",
    "",
).strip().rstrip("/")

WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH",
    "/telegram-webhook",
).strip()

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "",
).strip()

if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = f"/{WEBHOOK_PATH}"


# ============================================================
# CONSTANTS
# ============================================================

DUEL_PENDING_MINUTES = 10
DUEL_FIGHT_MINUTES = 30

DUEL_CALLBACK_TIMEOUT_SECONDS = 12
DUEL_HIT_CHANCE = 0.35

MUTE_MIN_SECONDS = 30
MUTE_MAX_SECONDS = 365 * 24 * 60 * 60


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


# ============================================================
# BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

dp = Dispatcher()

db_pool: asyncpg.Pool | None = None
cleanup_task: asyncio.Task[None] | None = None


# ============================================================
# DATABASE
# ============================================================

def get_pool() -> asyncpg.Pool:
    if db_pool is None:
        raise RuntimeError(
            "Пул PostgreSQL ещё не запущен"
        )

    return db_pool


async def open_database() -> None:
    global db_pool

    if db_pool is not None:
        return

    db_pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=DB_POOL_MAX_SIZE,
        command_timeout=30,
        max_inactive_connection_lifetime=300,
        server_settings={
            "application_name": "poskulibot",
        },
    )


async def close_database() -> None:
    global db_pool

    if db_pool is not None:
        await db_pool.close()
        db_pool = None


async def init_db() -> None:
    """
    Создаёт схему и мигрирует старую версию базы.
    """

    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():

            # ------------------------------------------------
            # USERS
            # ------------------------------------------------

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    telegram_name TEXT NOT NULL DEFAULT '',
                    name_is_custom BOOLEAN NOT NULL DEFAULT FALSE,

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

            # Миграции со старой схемы.
            await conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS telegram_name TEXT
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS name_is_custom BOOLEAN
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS duel_wins INTEGER
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS duel_losses INTEGER
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS is_premium BOOLEAN
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS vip_expire TIMESTAMPTZ
                """
            )

            await conn.execute(
                """
                UPDATE users
                SET telegram_name = name
                WHERE telegram_name IS NULL
                   OR telegram_name = ''
                """
            )

            await conn.execute(
                """
                UPDATE users
                SET name_is_custom = FALSE
                WHERE name_is_custom IS NULL
                """
            )

            await conn.execute(
                """
                UPDATE users
                SET duel_wins = 0
                WHERE duel_wins IS NULL
                """
            )

            await conn.execute(
                """
                UPDATE users
                SET duel_losses = 0
                WHERE duel_losses IS NULL
                """
            )

            await conn.execute(
                """
                UPDATE users
                SET is_premium = FALSE
                WHERE is_premium IS NULL
                """
            )

            await conn.execute(
                """
                UPDATE users
                SET total_whine = 0
                WHERE total_whine IS NULL
                   OR total_whine < 0
                """
            )

            await conn.execute(
                """
                UPDATE users
                SET last_whine = 0
                WHERE last_whine IS NULL
                """
            )

            await conn.execute(
                """
                UPDATE users
                SET status = 'olympian'
                WHERE status = 'olymp'
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN telegram_name SET DEFAULT ''
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN telegram_name SET NOT NULL
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN name_is_custom SET DEFAULT FALSE
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN name_is_custom SET NOT NULL
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN duel_wins SET DEFAULT 0
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN duel_losses SET DEFAULT 0
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN duel_wins SET NOT NULL
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN duel_losses SET NOT NULL
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN is_premium SET DEFAULT FALSE
                """
            )

            await conn.execute(
                """
                ALTER TABLE users
                ALTER COLUMN is_premium SET NOT NULL
                """
            )

            # ------------------------------------------------
            # SETTINGS
            # ------------------------------------------------

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

            # ------------------------------------------------
            # CHAT MEMBERS
            # ------------------------------------------------

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_members (
                    user_id BIGINT NOT NULL
                        REFERENCES users(user_id)
                        ON DELETE CASCADE,

                    chat_id BIGINT NOT NULL,

                    PRIMARY KEY (user_id, chat_id)
                )
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                chat_members_chat_id_idx
                ON chat_members (chat_id)
                """
            )

            # ------------------------------------------------
            # CHAT STATUS
            # ------------------------------------------------

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_status (
                    chat_id BIGINT PRIMARY KEY,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )

            # ------------------------------------------------
            # DUELS
            # ------------------------------------------------

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS duels (
                    duel_id TEXT PRIMARY KEY,

                    chat_id BIGINT NOT NULL,
                    message_id BIGINT,

                    p1_id BIGINT NOT NULL
                        REFERENCES users(user_id)
                        ON DELETE CASCADE,

                    p1_name TEXT NOT NULL,

                    p2_id BIGINT NOT NULL
                        REFERENCES users(user_id)
                        ON DELETE CASCADE,

                    p2_name TEXT NOT NULL,

                    p1_stake BIGINT NOT NULL DEFAULT 0,
                    p2_stake BIGINT NOT NULL DEFAULT 0,

                    bank BIGINT NOT NULL DEFAULT 0,

                    turn_id BIGINT NOT NULL,

                    round_no INTEGER NOT NULL DEFAULT 0,

                    status TEXT NOT NULL
                        CHECK (
                            status IN (
                                'pending',
                                'fighting'
                            )
                        ),

                    created_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW(),

                    expires_at TIMESTAMPTZ NOT NULL
                )
                """
            )

            await conn.execute(
                """
                ALTER TABLE duels
                ADD COLUMN IF NOT EXISTS
                round_no INTEGER NOT NULL DEFAULT 0
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                duels_expires_at_idx
                ON duels (expires_at)
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                duels_p1_active_idx
                ON duels (p1_id, expires_at)
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                duels_p2_active_idx
                ON duels (p2_id, expires_at)
                """
            )

            # ------------------------------------------------
            # PAYMENTS
            # ------------------------------------------------

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    telegram_payment_charge_id TEXT PRIMARY KEY,

                    provider_payment_charge_id TEXT,

                    user_id BIGINT NOT NULL
                        REFERENCES users(user_id)
                        ON DELETE CASCADE,

                    payload TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount INTEGER NOT NULL,

                    created_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW()
                )
                """
            )

            # ------------------------------------------------
            # ARCHITECT
            # ------------------------------------------------

            await conn.execute(
                """
                INSERT INTO users (
                    user_id,
                    name,
                    telegram_name,
                    name_is_custom,
                    total_whine,
                    status
                )
                VALUES (
                    $1,
                    'Архитектор',
                    'Архитектор',
                    TRUE,
                    200,
                    'architect'
                )

                ON CONFLICT (user_id)
                DO UPDATE SET
                    status = 'architect',
                    name = 'Архитектор',
                    name_is_custom = TRUE
                """,
                ARCHITECT_ID,
            )


# ============================================================
# CHAT STATUS
# ============================================================

async def set_chat_active(
    chat_id: int,
    is_active: bool,
) -> None:

    await get_pool().execute(
        """
        INSERT INTO chat_status (
            chat_id,
            is_active
        )
        VALUES ($1, $2)

        ON CONFLICT (chat_id)
        DO UPDATE
        SET is_active = EXCLUDED.is_active
        """,
        chat_id,
        is_active,
    )


async def is_chat_on(chat_id: int) -> bool:

    value = await get_pool().fetchval(
        """
        SELECT is_active
        FROM chat_status
        WHERE chat_id = $1
        """,
        chat_id,
    )

    return True if value is None else bool(value)


async def require_chat_active(
    message: Message,
) -> bool:

    # В личке бот всегда доступен.
    if message.chat.type == ChatType.PRIVATE:
        return True

    if await is_chat_on(message.chat.id):
        return True

    await message.answer(
        "💤 Бот выключен в этом чате.\n"
        "Администратор может включить его командой "
        "<code>+скули</code> или <code>/skulion</code>."
    )

    return False


# ============================================================
# USERS
# ============================================================

def telegram_display_name(
    user: types.User,
) -> str:

    raw_name = (
        user.first_name
        or user.username
        or str(user.id)
    )

    return raw_name.strip()[:100]


async def _register_in_chat(
    conn: asyncpg.Connection,
    user_id: int,
    chat_id: int,
) -> None:

    await conn.execute(
        """
        INSERT INTO chat_members (
            user_id,
            chat_id
        )
        VALUES ($1, $2)

        ON CONFLICT (
            user_id,
            chat_id
        )
        DO NOTHING
        """,
        user_id,
        chat_id,
    )


async def register_in_chat(
    user_id: int,
    chat_id: int,
) -> None:

    async with get_pool().acquire() as conn:
        await _register_in_chat(
            conn,
            user_id,
            chat_id,
        )


async def _ensure_user(
    conn: asyncpg.Connection,
    user: types.User,
    chat_id: int,
) -> None:

    status = (
        "architect"
        if user.id == ARCHITECT_ID
        else "user"
    )

    tg_name = telegram_display_name(user)

    await conn.execute(
        """
        INSERT INTO users (
            user_id,
            name,
            telegram_name,
            status
        )
        VALUES (
            $1,
            $2,
            $2,
            $3
        )

        ON CONFLICT (user_id)
        DO UPDATE SET

            telegram_name = EXCLUDED.telegram_name,

            name = CASE
        WHEN users.name_is_custom
           THEN users.name
        ELSE EXCLUDED.telegram_name
        END,

            status = CASE

                WHEN users.user_id = $4
                    THEN 'architect'

                ELSE users.status

            END
        """,
        user.id,
        tg_name,
        status,
        ARCHITECT_ID,
    )

    await _register_in_chat(
        conn,
        user.id,
        chat_id,
    )


async def ensure_user(
    user: types.User,
    chat_id: int,
) -> None:

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user(
                conn,
                user,
                chat_id,
            )


async def sync_existing_user(
    user: types.User,
    chat_id: int,
) -> bool:
    """
    Обновляет Telegram-имя существующего пользователя,
    но НЕ регистрирует нового автоматически.
    """

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM users
                    WHERE user_id = $1
                )
                """,
                user.id,
            )

            if not exists:
                return False

            await _ensure_user(
                conn,
                user,
                chat_id,
            )

            return True


async def get_u(
    user_id: int,
) -> dict[str, Any] | None:

    row = await get_pool().fetchrow(
        """
        SELECT
            name,
            telegram_name,
            name_is_custom,
            total_whine,
            last_whine,
            status,
            is_premium,
            vip_expire,
            duel_wins,
            duel_losses

        FROM users

        WHERE user_id = $1
        """,
        user_id,
    )

    if row is None:
        return None

    # Если платный статус уже протух,
    # актуализируем ранг при чтении пользователя.
    vip_expire = row["vip_expire"]

    if (
        user_id != ARCHITECT_ID
        and row["is_premium"]
        and (
            vip_expire is None
            or vip_expire <= datetime.now(timezone.utc)
        )
    ):
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await _refresh_user_rank(
                    conn,
                    user_id,
                )

        row = await get_pool().fetchrow(
            """
            SELECT
                name,
                telegram_name,
                name_is_custom,
                total_whine,
                last_whine,
                status,
                is_premium,
                vip_expire,
                duel_wins,
                duel_losses

            FROM users

            WHERE user_id = $1
            """,
            user_id,
        )

        if row is None:
            return None

    return {
        "name": row["name"],
        "telegram_name": row["telegram_name"],
        "name_is_custom": row["name_is_custom"],

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


async def _refresh_user_rank(
    conn: asyncpg.Connection,
    user_id: int,
) -> None:

    row = await conn.fetchrow(
        """
        SELECT
            total_whine,
            status,
            is_premium,
            vip_expire

        FROM users

        WHERE user_id = $1

        FOR UPDATE
        """,
        user_id,
    )

    if row is None:
        return

    if user_id == ARCHITECT_ID:
        return

    vip_expire = row["vip_expire"]

    premium_active = bool(
        row["is_premium"]
        and vip_expire is not None
        and vip_expire > datetime.now(timezone.utc)
    )

    if premium_active:
        return

    new_status = rank_for_total(
        row["total_whine"]
    )

    await conn.execute(
        """
        UPDATE users

        SET
            status = $2,
            is_premium = FALSE,
            vip_expire = NULL

        WHERE user_id = $1
        """,
        user_id,
        new_status,
    )


async def update_score(
    user_id: int,
    amount: int,
    update_time: bool = False,
) -> dict[str, Any] | None:

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            row = await conn.fetchrow(
                """
                UPDATE users

                SET
                    total_whine =
                        GREATEST(
                            0,
                            total_whine + $2
                        ),

                    last_whine =
                        CASE
                            WHEN $3
                            THEN $4
                            ELSE last_whine
                        END

                WHERE user_id = $1

                RETURNING user_id
                """,
                user_id,
                amount,
                update_time,
                int(
                    datetime.now(
                        timezone.utc
                    ).timestamp()
                ),
            )

            if row is None:
                return None

            await _refresh_user_rank(
                conn,
                user_id,
            )

    return await get_u(user_id)


async def set_user_name(
    user_id: int,
    new_name: str,
) -> str | None:

    return await get_pool().fetchval(
        """
        UPDATE users

        SET
            name = $2,
            name_is_custom = TRUE

        WHERE user_id = $1

        RETURNING name
        """,
        user_id,
        new_name,
    )


async def reset_user_name(
    user_id: int,
) -> bool:

    result = await get_pool().execute(
        """
        UPDATE users

        SET
            name = telegram_name,
            name_is_custom = FALSE

        WHERE user_id = $1
        """,
        user_id,
    )

    return result == "UPDATE 1"


async def get_global_leaderboard(
    limit: int = 20,
) -> list[asyncpg.Record]:

    return await get_pool().fetch(
        """
        SELECT
            name,
            total_whine,
            status,
            user_id,
            duel_wins,
            duel_losses

        FROM users

        WHERE total_whine > 0

        ORDER BY
            total_whine DESC,
            user_id

        LIMIT $1
        """,
        limit,
    )


# ============================================================
# DUEL HELPERS / CLEANUP
# ============================================================

async def _lock_users(
    conn: asyncpg.Connection,
    user_ids: Iterable[int],
) -> list[asyncpg.Record]:

    ids = sorted(set(user_ids))

    if not ids:
        return []

    return await conn.fetch(
        """
        SELECT
            user_id,
            name,
            total_whine

        FROM users

        WHERE user_id = ANY(
            $1::BIGINT[]
        )

        ORDER BY user_id

        FOR UPDATE
        """,
        ids,
    )


async def _refund_expired_duel(
    conn: asyncpg.Connection,
    duel: asyncpg.Record,
) -> None:

    if duel["status"] == "fighting":

        await _lock_users(
            conn,
            [
                duel["p1_id"],
                duel["p2_id"],
            ],
        )

        if duel["p1_stake"] > 0:
            await conn.execute(
                """
                UPDATE users

                SET total_whine =
                    total_whine + $2

                WHERE user_id = $1
                """,
                duel["p1_id"],
                duel["p1_stake"],
            )

        if duel["p2_stake"] > 0:
            await conn.execute(
                """
                UPDATE users

                SET total_whine =
                    total_whine + $2

                WHERE user_id = $1
                """,
                duel["p2_id"],
                duel["p2_stake"],
            )

        await _refresh_user_rank(
            conn,
            duel["p1_id"],
        )

        await _refresh_user_rank(
            conn,
            duel["p2_id"],
        )

    await conn.execute(
        """
        DELETE FROM duels
        WHERE duel_id = $1
        """,
        duel["duel_id"],
    )


async def cleanup_expired_duels(
    user_ids: Iterable[int] | None = None,
    limit: int = 100,
) -> int:

    cleaned = 0

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            if user_ids:
                ids = sorted(set(user_ids))

                rows = await conn.fetch(
                    """
                    SELECT *

                    FROM duels

                    WHERE expires_at <= NOW()

                      AND (
                          p1_id = ANY(
                              $1::BIGINT[]
                          )

                          OR

                          p2_id = ANY(
                              $1::BIGINT[]
                          )
                      )

                    ORDER BY expires_at

                    FOR UPDATE SKIP LOCKED
                    """,
                    ids,
                )

            else:
                rows = await conn.fetch(
                    """
                    SELECT *

                    FROM duels

                    WHERE expires_at <= NOW()

                    ORDER BY expires_at

                    LIMIT $1

                    FOR UPDATE SKIP LOCKED
                    """,
                    limit,
                )

            for duel in rows:
                await _refund_expired_duel(
                    conn,
                    duel,
                )

                cleaned += 1

    return cleaned


async def duel_cleanup_loop() -> None:

    while True:

        try:
            cleaned = await cleanup_expired_duels()

            if cleaned:
                logger.info(
                    "Очищено просроченных дуэлей: %s",
                    cleaned,
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Не удалось очистить просроченные дуэли"
            )

        await asyncio.sleep(60)


# ============================================================
# COMMON HELPERS
# ============================================================

def get_duel_rank(
    wins: int,
    losses: int,
) -> str:

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

    name = escape(
        user.first_name
        or user.username
        or str(user.id)
    )

    return (
        f'<a href="tg://user?id={user.id}">'
        f"{name}"
        "</a>"
    )


def stored_user_tag(
    user_id: int,
    name: str,
) -> str:

    return (
        f'<a href="tg://user?id={user_id}">'
        f"{escape(name)}"
        "</a>"
    )


def extract_args(
    message: Message,
    plus_prefix: str | None = None,
) -> str:

    text = (
        message.text
        or ""
    ).strip()

    if not text:
        return ""

    if text.startswith("/"):
        parts = text.split(
            maxsplit=1
        )

        if len(parts) == 2:
            return parts[1].strip()

        return ""

    if plus_prefix:

        if text.lower().startswith(
            plus_prefix.lower()
        ):
            return text[
                len(plus_prefix):
            ].strip()

    return ""


def parse_positive_int(
    raw: str | None,
) -> int | None:

    text = (
        raw
        or ""
    ).strip()

    if not text:
        return None

    token = text.split(
        maxsplit=1
    )[0]

    if not token.isdigit():
        return None

    value = int(token)

    if value <= 0:
        return None

    return value


MUTE_RE = re.compile(
    r"^(\d+)\s*(с|сек|s|м|мин|m|ч|час|h|д|дн|d)?$",
    re.IGNORECASE,
)


def parse_mute_seconds(
    raw: str | None,
) -> int | None:

    text = (
        raw
        or ""
    ).strip()

    if not text:
        return None

    match = MUTE_RE.fullmatch(
        text
    )

    if not match:
        return None

    amount = int(
        match.group(1)
    )

    suffix = (
        match.group(2)
        or "м"
    ).lower()

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

    if seconds < MUTE_MIN_SECONDS:
        return None

    if seconds > MUTE_MAX_SECONDS:
        return None

    return seconds


def is_group(
    message: Message,
) -> bool:

    return message.chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }


async def is_chat_admin(
    chat_id: int,
    user_id: int,
) -> bool:

    try:
        member = await bot.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        }

    except Exception:
        logger.exception(
            "Не удалось проверить права пользователя %s в чате %s",
            user_id,
            chat_id,
        )

        return False


async def can_architect_or_olympian_mute(
    message: Message,
    seconds: int,
) -> tuple[bool, str]:

    if message.from_user is None:
        return (
            False,
            "🚫 Не удалось определить отправителя.",
        )

    if message.from_user.id == ARCHITECT_ID:
        return (
            True,
            "architect",
        )

    user = await get_u(
        message.from_user.id
    )

    if (
        user
        and user["status"] == "olympian"
    ):
        if seconds > 10 * 60:
            return (
                False,
                "🏛️ Олимпиец может мутить максимум на 10 минут.",
            )

        return (
            True,
            "olympian",
        )

    return (
        False,
        "🚫 Команда доступна только Архитектору или Олимпийцу.",
    )


# ============================================================
# PERMISSIONS
# ============================================================

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


# ============================================================
# START
# ============================================================

@dp.message(Command("skulistart"))
async def start(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    await ensure_user(
        message.from_user,
        message.chat.id,
    )

    await message.answer(
        "✅ Регистрация успешна! "
        "Твой баланс теперь един во всех чатах. "
        "Юзай /poskuli"
    )


# ============================================================
# POSKULI
# ============================================================

@dp.message(Command("poskuli"))
@dp.message(F.text.lower() == "+поскулить")
async def measure_whine(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    if not await require_chat_active(
        message
    ):
        return

    user_id = message.from_user.id

    registered = await sync_existing_user(
        message.from_user,
        message.chat.id,
    )

    if not registered:
        await message.answer(
            "⚠️ Сначала нажми /skulistart, "
            "чтобы прибор тебя запомнил!"
        )
        return

    await cleanup_expired_duels(
        [user_id]
    )

    user = await get_u(
        user_id
    )

    if user is None:
        await message.answer(
            "⚠️ Сначала нажми /skulistart."
        )
        return

    user_tag = stored_user_tag(
        user_id,
        user["name"],
    )

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            locked_user = await conn.fetchrow(
                """
                SELECT
                    total_whine,
                    last_whine,
                    status

                FROM users

                WHERE user_id = $1

                FOR UPDATE
                """,
                user_id,
            )

            if locked_user is None:
                return

            now_ts = int(
                datetime.now(
                    timezone.utc
                ).timestamp()
            )

            wait_time = (
                locked_user["last_whine"]
                + COOLDOWN_MINUTES * 60
                - now_ts
            )

            if wait_time > 0:
                amount = None
                new_total = None
                is_penalty = False
                multiplier = 1.0

            else:

                multiplier = RANKS.get(
                    locked_user["status"],
                    RANKS["user"],
                )["multiplier"]

                is_penalty = (
                    random.random()
                    < 0.20
                )

                if is_penalty:
                    amount = -random.randint(
                        1,
                        5,
                    )
                else:
                    amount = int(
                        random.randint(
                            10,
                            200,
                        )
                        * multiplier
                    )

                new_total = await conn.fetchval(
                    """
                    UPDATE users

                    SET
                        total_whine =
                            GREATEST(
                                0,
                                total_whine + $2
                            ),

                        last_whine = $3

                    WHERE user_id = $1

                    RETURNING total_whine
                    """,
                    user_id,
                    amount,
                    now_ts,
                )

                await _refresh_user_rank(
                    conn,
                    user_id,
                )

    if wait_time > 0:

        minutes, seconds = divmod(
            wait_time,
            60,
        )

        await message.answer(
            f"🚫⛔ {user_tag}, связки не восстановились!\n"
            f"Обожди ещё "
            f"<b>{minutes}м {seconds}с</b>, маленький"
        )

        return

    assert amount is not None
    assert new_total is not None

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
            f"📉 {user_tag}, "
            f"<b>-{loss} дБ</b>!\n"
            f"❌ {random.choice(fails)}\n"
            f"Итог: <b>{new_total} дБ</b>"
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

    mood = random.choice(
        moods
    )

    bonus = (
        f" (Бонус ранга x{multiplier})"
        if multiplier > 1.0
        else ""
    )

    replies = [
        (
            f"📈 {user_tag}, замер: "
            f"<b>{gain} дБ</b>{bonus}\n"
            f"ℹ️ Статус: {mood}\n"
            f"Всего накоплено: "
            f"<b>{new_total} дБ</b>"
        ),
        (
            f"🧭 {user_tag}, твоё новое значение — "
            f"<b>{gain} дБ</b>{bonus}\n"
            f"{mood}\n"
            f"🔊 Текущий итог: "
            f"<b>{new_total} дБ</b>"
        ),
        (
            f"🎚️ {user_tag}, измерение показало "
            f"<b>{gain} дБ</b>{bonus}\n"
            f"🎧 {mood}\n"
            f"Суммарно: "
            f"<b>{new_total} дБ</b>"
        ),
    ]

    await message.answer(
        random.choice(replies)
    )


# ============================================================
# CASINO
# ============================================================

async def play_casino(
    message: Message,
    arg_text: str,
    usage: str,
) -> None:

    if message.from_user is None:
        return

    if not await require_chat_active(
        message
    ):
        return

    user_id = message.from_user.id

    await ensure_user(
        message.from_user,
        message.chat.id,
    )

    await cleanup_expired_duels(
        [user_id]
    )

    value = parse_positive_int(
        arg_text
    )

    if value is None:
        await message.answer(
            f"⚠️ Пиши сумму: "
            f"<code>{escape(usage)}</code>"
        )
        return

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            user = await conn.fetchrow(
                """
                SELECT
                    name,
                    total_whine,
                    status

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

                config = RANKS.get(
                    user["status"],
                    RANKS["user"],
                )

                if user_id == ARCHITECT_ID:
                    config = RANKS["bronze"]

                is_all_in = (
                    value
                    == user["total_whine"]
                )

                chance = (
                    config["all_in"]
                    if is_all_in
                    else config["chance"]
                )

                if random.random() < chance:

                    payout = int(
                        value
                        * (
                            1.2
                            if is_all_in
                            else 2.0
                        )
                    )

                    jackpot = (
                        not is_all_in
                        and random.random() > 0.93
                    )

                    if jackpot:
                        payout = value * 4

                    profit = payout - value
                    delta = profit

                    outcome = "win"
                    cashback = 0

                else:

                    cashback = int(
                        value
                        * config.get(
                            "cb",
                            0,
                        )
                    )

                    delta = (
                        -value
                        + cashback
                    )

                    outcome = "loss"
                    payout = 0
                    profit = 0
                    jackpot = False

                await conn.execute(
                    """
                    UPDATE users

                    SET total_whine =
                        GREATEST(
                            0,
                            total_whine + $2
                        )

                    WHERE user_id = $1
                    """,
                    user_id,
                    delta,
                )

                await _refresh_user_rank(
                    conn,
                    user_id,
                )

    user_tag = stored_user_tag(
        user_id,
        user["name"],
    )

    if outcome == "insufficient":

        await message.answer(
            f"🚫 {user_tag}, у тебя только "
            f"<b>{user['total_whine']} дБ</b>! "
            "Ты нищееб никчемный, к сожалению!"
        )

        return

    if outcome == "win":

        if jackpot:

            text = (
                f"🎰 {user_tag}, ДЖЕКПОТ! "
                "БОГИ СЛЫШАТ ТВОЙ СКУЛЁЖ! "
                "ТЫ УСМАН ДЕМБЕЛЕ: "
                f"<b>+{profit} дБ</b> чистыми!"
            )

        else:

            text = (
                f"🎰 {user_tag}, КУШ! "
                "Как же ты ебешь, Боже: "
                f"<b>+{profit} дБ</b> чистыми!"
            )

        await message.answer(
            text
        )

    else:

        await message.answer(
            f"🎰 {user_tag}, ставка "
            f"<b>{value} дБ</b> сгорела, "
            "иди скули, пёс! "
            f"Кэшбек: {cashback} 📉"
        )


@dp.message(Command("skulibet"))
@dp.message(
    F.text.regexp(
        r"(?i)^\+казик(?:\s|$)"
    )
)
async def bet(
    message: Message,
) -> None:

    await play_casino(
        message,
        extract_args(
            message,
            "+казик",
        ),
        "/skulibet 50",
    )


# ============================================================
# TRANSFER
# ============================================================

@dp.message(Command("transfer"))
@dp.message(
    F.text.regexp(
        r"(?i)^\+перевод(?:\s|$)"
    )
)
async def transfer_db(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    if not await require_chat_active(
        message
    ):
        return

    if (
        not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.answer(
            "⚠️ Перевод делается ответом на сообщение юзера:\n"
            "<code>+перевод 100</code>\n"
            "или\n"
            "<code>/transfer 100</code>"
        )
        return

    sender = message.from_user
    target = message.reply_to_message.from_user

    if target.is_bot:
        await message.answer(
            "🤖 Ботам дБ не переводи, дебил сука ебаный."
        )
        return

    if sender.id == target.id:
        await message.answer(
            "🤡 Сам себе перевод? "
            "Ты внатуре пизданутая скотина"
        )
        return

    amount = parse_positive_int(
        extract_args(
            message,
            "+перевод",
        )
    )

    if amount is None:
        await message.answer(
            "⚠️ Формат: "
            "<code>+перевод 100</code> "
            "или "
            "<code>/transfer 100</code>"
        )
        return

    await cleanup_expired_duels(
        [
            sender.id,
            target.id,
        ]
    )

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            await _ensure_user(
                conn,
                sender,
                message.chat.id,
            )

            await _ensure_user(
                conn,
                target,
                message.chat.id,
            )

            locked_users = await _lock_users(
                conn,
                [
                    sender.id,
                    target.id,
                ],
            )

            balances = {
                row["user_id"]:
                    row["total_whine"]
                for row in locked_users
            }

            sender_balance = balances.get(
                sender.id,
                0,
            )

            if sender_balance < amount:
                enough = False

            else:
                enough = True

                await conn.execute(
                    """
                    UPDATE users

                    SET total_whine =
                        total_whine - $2

                    WHERE user_id = $1
                    """,
                    sender.id,
                    amount,
                )

                await conn.execute(
                    """
                    UPDATE users

                    SET total_whine =
                        total_whine + $2

                    WHERE user_id = $1
                    """,
                    target.id,
                    amount,
                )

                await _refresh_user_rank(
                    conn,
                    sender.id,
                )

                await _refresh_user_rank(
                    conn,
                    target.id,
                )

    if not enough:

        await message.answer(
            f"🚫 У тебя только "
            f"<b>{sender_balance} дБ</b>. "
            "Не вывез перевод, скули дальше."
        )

        return

    await message.answer(
        f"💸 {html_tag(target)}, "
        f"тебе перевод от {html_tag(sender)}!\n"
        f"<b>{amount} дБ</b> прилетело в карман. "
        "Скули на здоровье! 🐺🔊🥂"
    )


# ============================================================
# ARCHITECT TAKE
# ============================================================

@dp.message(Command("take"))
@dp.message(
    F.text.regexp(
        r"(?i)^\+списать(?:\s|$)"
    )
)
async def architect_take_db(
    message: Message,
) -> None:

    if (
        message.from_user is None
        or message.from_user.id
        != ARCHITECT_ID
    ):
        await message.answer(
            "🚫 Казначейский нож только у Архитектора."
        )
        return

    if (
        not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.answer(
            "⚠️ Формат: "
            "<code>+списать 100</code> "
            "или "
            "<code>/take 100</code> "
            "ответом на сообщение жертвы."
        )
        return

    amount = parse_positive_int(
        extract_args(
            message,
            "+списать",
        )
    )

    if amount is None:
        await message.answer(
            "⚠️ Формат: "
            "<code>+списать 100</code>"
        )
        return

    target = (
        message
        .reply_to_message
        .from_user
    )

    await cleanup_expired_duels(
        [target.id]
    )

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            await _ensure_user(
                conn,
                target,
                message.chat.id,
            )

            balance = await conn.fetchval(
                """
                SELECT total_whine

                FROM users

                WHERE user_id = $1

                FOR UPDATE
                """,
                target.id,
            )

            balance = balance or 0

            taken = min(
                amount,
                balance,
            )

            await conn.execute(
                """
                UPDATE users

                SET total_whine =
                    total_whine - $2

                WHERE user_id = $1
                """,
                target.id,
                taken,
            )

            await conn.execute(
                """
                UPDATE settings

                SET value =
                    value + $1

                WHERE key = 'vault'
                """,
                taken,
            )

            await _refresh_user_rank(
                conn,
                target.id,
            )

    await message.answer(
        "🧾 <b>Аудит Асгарда</b>\n"
        f"С {html_tag(target)} списано "
        f"<b>{taken} дБ</b>. "
        "Казна довольно урчит. 🏦"
    )


# ============================================================
# MUTE
# ============================================================

@dp.message(Command("mute"))
@dp.message(
    F.text.regexp(
        r"(?i)^\+мут(?:\s|$)"
    )
)
async def mute_user(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    if not is_group(message):
        await message.answer(
            "⚠️ Мут работает только "
            "в группе или супергруппе."
        )
        return

    if (
        not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.answer(
            "⚠️ Мут делается ответом:\n"
            "<code>+мут 10м</code>\n"
            "или\n"
            "<code>/mute 10м</code>"
        )
        return

    seconds = parse_mute_seconds(
        extract_args(
            message,
            "+мут",
        )
    )

    if seconds is None:
        await message.answer(
            "⚠️ Формат: "
            "<code>+мут 10м</code>, "
            "<code>+мут 1ч</code>, "
            "<code>+мут 2д</code>.\n"
            "Минимум — 30 секунд."
        )
        return

    allowed, role_or_message = (
        await can_architect_or_olympian_mute(
            message,
            seconds,
        )
    )

    if not allowed:
        await message.answer(
            role_or_message
        )
        return

    target = (
        message
        .reply_to_message
        .from_user
    )

    if target.id == message.from_user.id:
        await message.answer(
            "🤡 Самомут? Ты ебанат что ли блядь?!"
        )
        return

    if await is_chat_admin(
        message.chat.id,
        target.id,
    ):
        await message.answer(
            "🛡️ Админов/создателя чата "
            "мутить нельзя. Ебанулся что ли?!"
        )
        return

    try:

        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=MUTED_CHAT_PERMISSIONS,
            until_date=(
                datetime.now(
                    timezone.utc
                )
                + timedelta(
                    seconds=seconds
                )
            ),
            use_independent_chat_permissions=True,
        )

    except Exception as exc:

        logger.exception(
            "Ошибка мута"
        )

        await message.answer(
            "⚠️ Не смог замутить. "
            "Проверь, что бот — администратор "
            "с правом ограничивать участников.\n"
            f"Ошибка: <code>{escape(str(exc))}</code>"
        )

        return

    await message.answer(
        f"🔇 {html_tag(target)} "
        f"хуесос отправлен под шконарь "
        f"на <b>{seconds} сек.</b>\n"
        f"Исполнитель: "
        f"{html_tag(message.from_user)}"
    )


# ============================================================
# UNMUTE
# ============================================================

@dp.message(Command("unmute"))
@dp.message(F.text.lower() == "-мут")
async def unmute_user(
    message: Message,
) -> None:

    if (
        message.from_user is None
        or not is_group(message)
    ):
        return

    if (
        not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.answer(
            "⚠️ Ответь командой "
            "<code>-мут</code> "
            "или <code>/unmute</code> "
            "на сообщение юзера."
        )
        return

    moderator = message.from_user
    target = (
        message
        .reply_to_message
        .from_user
    )

    if moderator.id == target.id:
        await message.answer(
            "🤡 Сам себя размутить решил? "
            "Ебантяй нахуй!"
        )
        return

    moderator_user = await get_u(
        moderator.id
    )

    if moderator_user is None:
        await message.answer(
            "⚠️ Ты не зарегистрирован. "
            "/skulistart"
        )
        return

    is_architect = (
        moderator.id
        == ARCHITECT_ID
    )

    is_olympian = (
        moderator_user["status"]
        == "olympian"
    )

    if (
        not is_architect
        and not is_olympian
    ):
        await message.answer(
            "🚫 Ты куда полез, "
            "скотина припизднутая?!"
        )
        return

    if (
        not is_architect
        and await is_chat_admin(
            message.chat.id,
            target.id,
        )
    ):
        await message.answer(
            "⚡️ Олимпийцы не могут "
            "размутить админов."
        )
        return

    try:

        chat_info = await bot.get_chat(
            message.chat.id
        )

        permissions = (
            chat_info.permissions
            or FALLBACK_UNMUTED_CHAT_PERMISSIONS
        )

        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=permissions,
            use_independent_chat_permissions=True,
        )

    except Exception as exc:

        logger.exception(
            "Ошибка размута"
        )

        await message.answer(
            "❌ Не удалось размутить.\n"
            f"Причина: "
            f"<code>{escape(str(exc))}</code>"
        )

        return

    await message.answer(
        f"🔓 {html_tag(target)} "
        "вышел из-под шконки!\n"
        f"⚖️ Амнистия от пахана "
        f"{html_tag(moderator)}"
    )


# ============================================================
# BAN
# ============================================================

@dp.message(Command("ban"))
@dp.message(F.text.lower() == "+бан")
async def ban_user(
    message: Message,
) -> None:

    if (
        message.from_user is None
        or message.from_user.id
        != ARCHITECT_ID
    ):
        await message.answer(
            "🚫 Банхаммер хранится "
            "только у Архитектора."
        )
        return

    if not is_group(message):
        await message.answer(
            "⚠️ Бан работает только "
            "в группе или супергруппе."
        )
        return

    if (
        not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.answer(
            "⚠️ Бан делается ответом "
            "на сообщение юзера: "
            "<code>+бан</code>"
        )
        return

    target = (
        message
        .reply_to_message
        .from_user
    )

    if target.id == message.from_user.id:
        await message.answer(
            "🤡 Самобан — сильно, "
            "почти как самодрочь, но нет."
        )
        return

    if await is_chat_admin(
        message.chat.id,
        target.id,
    ):
        await message.answer(
            "🛡️ Админов/создателя чата "
            "банить нельзя, ты охуел?!"
        )
        return

    try:

        await bot.ban_chat_member(
            message.chat.id,
            target.id,
        )

    except Exception as exc:

        logger.exception(
            "Ошибка бана"
        )

        await message.answer(
            "⚠️ Не смог забанить. "
            "Проверь права бота.\n"
            f"Ошибка: "
            f"<code>{escape(str(exc))}</code>"
        )

        return

    await message.answer(
        f"🔨 {html_tag(target)} "
        "улетел из чата. "
        "Архитектор стукнул по ебалу хуебеса. 🌚"
    )


# ============================================================
# NAME
# ============================================================

@dp.message(Command("skuliname"))
async def change_name(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    if not await require_chat_active(
        message
    ):
        return

    new_name = extract_args(
        message
    ).strip()

    if not new_name:
        await message.answer(
            "⚠️ Введи новое имя:\n"
            "<code>/skuliname Сын Коке</code>\n\n"
            "Вернуть Telegram-имя:\n"
            "<code>/skuliname reset</code>"
        )
        return

    await ensure_user(
        message.from_user,
        message.chat.id,
    )

    if new_name.lower() in {
        "reset",
        "сброс",
    }:
        reset_ok = await reset_user_name(
            message.from_user.id
        )

        if not reset_ok:
            await message.answer(
                "⚠️ Не удалось сбросить имя."
            )
            return

        current = await get_u(
            message.from_user.id
        )

        if current is None:
            await message.answer(
                "⚠️ Не удалось прочитать профиль после сброса имени."
            )
            return

        await message.answer(
            "♻️ Вернул имя из Telegram: "
            f"<b>{escape(current['name'])}</b>"
        )
        return

    if len(new_name) > 20:
        await message.answer(
            "🚫 Слишком длинное погоняло! "
            "Максимум 20 символов."
        )
        return

    saved_name = await set_user_name(
        message.from_user.id,
        new_name,
    )

    if saved_name is None:
        await message.answer(
            "⚠️ Не удалось сохранить новое имя."
        )
        return

    # Дополнительно перечитываем профиль из БД,
    # чтобы убедиться, что сохранилось именно нужное имя.
    current = await get_u(
        message.from_user.id
    )

    if current is None:
        await message.answer(
            "⚠️ Имя записалось, но профиль не удалось перечитать."
        )
        return

    await message.answer(
        "🤝 К сожалению, теперь ты: "
        f"<b>{escape(current['name'])}</b>"
    )


# ============================================================
# GRANT
# ============================================================

@dp.message(Command("grant"))
async def god_grant(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    if message.from_user.id != ARCHITECT_ID:

        user = await get_u(
            message.from_user.id
        )

        if user and user["is_p"]:
            await message.answer(
                "Прости, премиальный нытик, "
                "но для тебя это скрытая функция 🥷"
            )
        else:
            await message.answer(
                "Олимп разгневан, "
                "иди скули, чушкан недоебаный! 🐷"
            )

        return

    if (
        not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.answer(
            "⚠️ Ответь на сообщение:\n"
            "<code>/grant 100</code>"
        )
        return

    amount = parse_positive_int(
        extract_args(
            message
        )
    )

    if amount is None:
        await message.answer(
            "⚠️ Формат: "
            "<code>/grant 100</code>"
        )
        return

    target = (
        message
        .reply_to_message
        .from_user
    )

    await cleanup_expired_duels(
        [target.id]
    )

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            await _ensure_user(
                conn,
                target,
                message.chat.id,
            )

            vault = await conn.fetchval(
                """
                SELECT value

                FROM settings

                WHERE key = 'vault'

                FOR UPDATE
                """
            )

            if vault < amount:
                granted = False

            else:
                granted = True

                await conn.execute(
                    """
                    UPDATE settings

                    SET value =
                        value - $1

                    WHERE key = 'vault'
                    """,
                    amount,
                )

                await conn.execute(
                    """
                    UPDATE users

                    SET total_whine =
                        total_whine + $2

                    WHERE user_id = $1
                    """,
                    target.id,
                    amount,
                )

                await _refresh_user_rank(
                    conn,
                    target.id,
                )

    if not granted:
        await message.answer(
            "🏦 Казна Олимпа пуста! "
            "Бартомеу блядь!"
        )
        return

    with contextlib.suppress(
        Exception
    ):
        await message.delete()

    await message.answer(
        "⚡️ <b>Глас Олимпа</b>\n\n"
        f"{html_tag(target)}, "
        "ты скулил так, что тебя услышали на Олимпе! "
        "Тебе послали бонус и леща "
        f"<b>{amount} дБ</b>!"
    )


# ============================================================
# TOP CHAT
# ============================================================

@dp.message(Command("topskuli"))
async def top_chat(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    if not await require_chat_active(
        message
    ):
        return

    await ensure_user(
        message.from_user,
        message.chat.id,
    )

    rows = await get_pool().fetch(
        """
        SELECT
            u.name,
            u.total_whine,
            u.status,
            u.user_id,
            u.duel_wins,
            u.duel_losses

        FROM users AS u

        JOIN chat_members AS cm
          ON cm.user_id = u.user_id

        WHERE cm.chat_id = $1

        ORDER BY
            u.total_whine DESC,
            u.user_id

        LIMIT 10
        """,
        message.chat.id,
    )

    if not rows:
        await message.answer(
            "📭 В этом чате ещё никто не скулил."
        )
        return

    lines = [
        "🏆 <b>ТОП НЫТИКОВ ЧАТА:</b>",
        "",
    ]

    for index, row in enumerate(
        rows,
        1,
    ):

        duel_rank = get_duel_rank(
            row["duel_wins"],
            row["duel_losses"],
        )

        if row["user_id"] == ARCHITECT_ID:
            prefix = "🌚🔧"
        else:
            prefix = RANKS.get(
                row["status"],
                RANKS["user"],
            )["label"].split()[-1]

        lines.append(
            f"{index}. "
            f"{prefix} "
            f"{escape(row['name'])} — "
            f"<code>{row['total_whine']} дБ</code> | "
            f"{duel_rank} "
            f"({row['duel_wins']}W/"
            f"{row['duel_losses']}L)"
        )

    balance = await get_pool().fetchval(
        """
        SELECT total_whine
        FROM users
        WHERE user_id = $1
        """,
        message.from_user.id,
    )

    local_rank = await get_pool().fetchval(
        """
        SELECT COUNT(*) + 1

        FROM users AS u

        JOIN chat_members AS cm
          ON cm.user_id = u.user_id

        WHERE cm.chat_id = $1
          AND u.total_whine > $2
        """,
        message.chat.id,
        balance or 0,
    )

    lines.extend(
        [
            "",
            "______________________________",
            f"📍 Твоё место в этом чате: "
            f"<b>#{local_rank}</b>",
        ]
    )

    await message.answer(
        "\n".join(lines)
    )


# ============================================================
# GLOBAL TOP
# ============================================================

@dp.message(Command("topglobal"))
async def global_top_handler(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    if not await require_chat_active(
        message
    ):
        return

    top_users = await get_global_leaderboard(
        20
    )

    if not top_users:
        await message.answer(
            "🌌 Во вселенной скулёж "
            "ещё не зафиксирован."
        )
        return

    lines = [
        "🌌 <b>ГЛОБАЛЬНЫЙ РЕЙТИНГ ВСЕЛЕННОЙ</b>",
        "______________________________",
        "",
    ]

    for index, row in enumerate(
        top_users,
        1,
    ):

        duel_rank = get_duel_rank(
            row["duel_wins"],
            row["duel_losses"],
        )

        prefix = (
            "🌚🔧"
            if row["user_id"] == ARCHITECT_ID
            else RANKS.get(
                row["status"],
                RANKS["user"],
            )["label"].split()[-1]
        )

        lines.append(
            f"{index}. "
            f"{prefix} "
            f"{escape(row['name'])} — "
            f"<code>{row['total_whine']} дБ</code> | "
            f"{duel_rank} "
            f"({row['duel_wins']}W/"
            f"{row['duel_losses']}L)"
        )

    balance = await get_pool().fetchval(
        """
        SELECT total_whine

        FROM users

        WHERE user_id = $1
        """,
        message.from_user.id,
    )

    if balance is None:
        rank_text = "❔ (не в базе)"

    else:

        user_rank = await get_pool().fetchval(
            """
            SELECT COUNT(*) + 1

            FROM users

            WHERE total_whine > $1
            """,
            balance,
        )

        rank_text = (
            f"<b>#{user_rank}</b>"
        )

    lines.extend(
        [
            "",
            "______________________________",
            f"👤 Твоё место в мировом рейтинге: "
            f"{rank_text}",
            "ℹ️ <i>Рейтинг един для всех чатов</i>",
        ]
    )

    await message.answer(
        "\n".join(lines)
    )


# ============================================================
# BOT ON/OFF
# ============================================================

@dp.message(Command("skulion"))
@dp.message(F.text.lower() == "+скули")
async def bot_on(
    message: Message,
) -> None:

    if (
        message.from_user is None
        or not is_group(message)
    ):
        return

    if await is_chat_admin(
        message.chat.id,
        message.from_user.id,
    ):

        await set_chat_active(
            message.chat.id,
            True,
        )

        await message.answer(
            "🔊 <b>Прибор замера прогрет!</b> "
            "Бот включён. Скулите на здоровье!"
        )

    else:

        await message.answer(
            f"{html_tag(message.from_user)}, "
            "админом себя почухал?! "
            "Чеши в стойло! 🐽"
        )


@dp.message(Command("skulioff"))
@dp.message(F.text.lower() == "-скули")
async def bot_off(
    message: Message,
) -> None:

    if (
        message.from_user is None
        or not is_group(message)
    ):
        return

    if await is_chat_admin(
        message.chat.id,
        message.from_user.id,
    ):

        await set_chat_active(
            message.chat.id,
            False,
        )

        await message.answer(
            "💤 <b>Бот ушёл в спячку.</b> "
            "Скулёж в этом чате больше "
            "не фиксируется."
        )

    else:

        await message.answer(
            f"{html_tag(message.from_user)}, "
            "админом себя почухал?! "
            "Чеши в стойло! 🐽"
        )


# ============================================================
# VAULT
# ============================================================

@dp.message(
    Command("vault"),
    F.from_user.id == ARCHITECT_ID,
)
async def check_vault(
    message: Message,
) -> None:

    value = await get_pool().fetchval(
        """
        SELECT value

        FROM settings

        WHERE key = 'vault'
        """
    )

    await message.answer(
        "💰 <b>Запасы Асгарда:</b>\n"
        f"<code>{value:,} дБ</code>"
    )


# ============================================================
# DUEL
# ============================================================

@dp.message(Command("duel"))
@dp.message(F.text.lower() == "+дуэль")
async def duel_request(
    message: Message,
) -> None:

    if message.from_user is None:
        return

    if not await require_chat_active(
        message
    ):
        return

    if not is_group(message):
        await message.answer(
            "⚠️ Дуэли работают только "
            "в группе или супергруппе."
        )
        return

    if (
        not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.answer(
            "⚠️ Чтобы вызвать на дуэль, "
            "ответь на сообщение противника:\n"
            "<code>+дуэль</code>\n"
            "или\n"
            "<code>/duel</code>"
        )
        return

    player1 = message.from_user
    player2 = (
        message
        .reply_to_message
        .from_user
    )

    if player2.is_bot:
        await message.answer(
            "🤖 С ботами дуэлей нет."
        )
        return

    if player1.id == player2.id:
        await message.answer(
            "Самострел? "
            "Не в мою смену. 🤡"
        )
        return

    # Возвращаем ставки старых зависших дуэлей.
    await cleanup_expired_duels(
        [
            player1.id,
            player2.id,
        ]
    )

    duel_id = uuid.uuid4().hex[:16]

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            # Теперь дуэль сама синхронизирует имена игроков.
            await _ensure_user(
                conn,
                player1,
                message.chat.id,
            )

            await _ensure_user(
                conn,
                player2,
                message.chat.id,
            )

            users = await _lock_users(
                conn,
                [
                    player1.id,
                    player2.id,
                ],
            )

            by_id = {
                row["user_id"]: row
                for row in users
            }

            p1 = by_id.get(
                player1.id
            )

            p2 = by_id.get(
                player2.id
            )

            if (
                p1 is None
                or p2 is None
            ):
                error = (
                    "Не удалось загрузить игроков."
                )

            elif (
                p1["total_whine"] <= 0
                or p2["total_whine"] <= 0
            ):
                error = (
                    "У нищих дуэлей не бывает. "
                    "Наскулите хоть что-то. 💸"
                )

            else:

                # ВАЖНО:
                # баланс глобальный, поэтому пользователь
                # не может быть одновременно в дуэли
                # даже в другом Telegram-чате.
                busy = await conn.fetchval(
                    """
                    SELECT EXISTS (

                        SELECT 1

                        FROM duels

                        WHERE
                            expires_at > NOW()

                        AND status IN (
                            'pending',
                            'fighting'
                        )

                        AND (
                            p1_id = ANY(
                                $1::BIGINT[]
                            )

                            OR

                            p2_id = ANY(
                                $1::BIGINT[]
                            )
                        )
                    )
                    """,
                    [
                        player1.id,
                        player2.id,
                    ],
                )

                if busy:
                    error = (
                        "⏳ Один из участников "
                        "уже занят другой дуэлью."
                    )

                else:

                    error = None

                    bank = (
                        p1["total_whine"]
                        + p2["total_whine"]
                    )

                    await conn.execute(
                        """
                        INSERT INTO duels (
                            duel_id,
                            chat_id,

                            p1_id,
                            p1_name,

                            p2_id,
                            p2_name,

                            bank,
                            turn_id,

                            status,
                            expires_at
                        )

                        VALUES (
                            $1,
                            $2,

                            $3,
                            $4,

                            $5,
                            $6,

                            $7,
                            $3,

                            'pending',
                            $8
                        )
                        """,
                        duel_id,
                        message.chat.id,

                        player1.id,
                        p1["name"],

                        player2.id,
                        p2["name"],

                        bank,

                        (
                            datetime.now(
                                timezone.utc
                            )
                            + timedelta(
                                minutes=DUEL_PENDING_MINUTES
                            )
                        ),
                    )

    if error:
        await message.answer(
            error
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ ПРИНЯТЬ (ВА-БАНК)",
                    callback_data=(
                        f"d_acc_{duel_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="🏳️ СЛИТЬСЯ КАК ЧУШКАН",
                    callback_data=(
                        f"d_dec_{duel_id}"
                    ),
                ),
            ]
        ]
    )

    sent = await message.answer(
        "⚔️ <b>ДУЭЛЬ НА ВЫЖИВАНИЕ!</b>\n\n"
        f"👤 {escape(p1['name'])} "
        "сгорел и вызывает "
        f"{escape(p2['name'])}!\n"
        f"💰 <b>Предварительный банк:</b> "
        f"{bank} дБ\n\n"
        f"<i>Вызов действует "
        f"{DUEL_PENDING_MINUTES} минут. "
        "Проигравший обнуляется.</i>",
        reply_markup=keyboard,
    )

    await get_pool().execute(
        """
        UPDATE duels

        SET message_id = $2

        WHERE duel_id = $1
        """,
        duel_id,
        sent.message_id,
    )


async def callback_message(
    call: types.CallbackQuery,
) -> Message | None:

    if not isinstance(
        call.message,
        Message,
    ):

        with contextlib.suppress(
            Exception
        ):
            await call.answer(
                "Сообщение больше недоступно.",
                show_alert=True,
            )

        return None

    return call.message


async def show_shoot_round(
    message: Message,
    duel: asyncpg.Record | dict[str, Any],
    prefix: str | None = None,
) -> None:

    turn_name = (
        duel["p1_name"]
        if duel["turn_id"]
        == duel["p1_id"]
        else duel["p2_name"]
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💥 ВЫСТРЕЛ!",
                    callback_data=(
                        f"d_shot_{duel['duel_id']}"
                    ),
                )
            ]
        ]
    )

    parts: list[str] = []

    if prefix:
        parts.append(prefix)
        parts.append("")

    parts.extend(
        [
            (
                "🔫 <b>ОЧЕРЕДЬ СТРЕЛЯТЬ:</b> "
                f"{escape(turn_name)}"
            ),
            (
                f"💰 Банк: "
                f"{duel['bank']} дБ"
            ),
            (
                f"🔄 Раунд: "
                f"{duel['round_no'] + 1}"
            ),
            "",
            "<i>Кто же отправится бомжевать первым?...</i>",
        ]
    )

    text = "\n".join(
        parts
    )

    try:

        await message.edit_text(
            text,
            reply_markup=keyboard,
        )

    except TelegramBadRequest as exc:

        if (
            "message is not modified"
            in str(exc).lower()
        ):
            return

        logger.warning(
            "Не удалось обновить сообщение дуэли %s: %s",
            duel["duel_id"],
            exc,
        )

        await message.answer(
            text,
            reply_markup=keyboard,
        )

# ------------------------------------------------------------
# DUEL ACCEPT
# ------------------------------------------------------------

@dp.callback_query(
    F.data.startswith("d_acc_")
)
async def d_accept(
    call: types.CallbackQuery,
) -> None:

    message = await callback_message(call)

    if message is None or call.data is None:
        return

    duel_id = call.data.removeprefix("d_acc_")

    # Сразу гасим Telegram spinner.
    try:
        await call.answer("Принимаю вызов…")
    except Exception:
        logger.exception(
            "Не удалось ответить callback accept"
        )

    try:
        async with asyncio.timeout(
            DUEL_CALLBACK_TIMEOUT_SECONDS
        ):

            # Возвращаем зависшую ставку этого пользователя,
            # если старая дуэль уже истекла.
            await cleanup_expired_duels(
                [call.from_user.id]
            )

            async with get_pool().acquire() as conn:
                async with conn.transaction():

                    duel = await conn.fetchrow(
                        """
                        SELECT *
                        FROM duels
                        WHERE duel_id = $1
                        FOR UPDATE
                        """,
                        duel_id,
                    )

                    if duel is None:
                        outcome = "missing"

                    elif (
                        duel["expires_at"]
                        <= datetime.now(timezone.utc)
                    ):
                        await _refund_expired_duel(
                            conn,
                            duel,
                        )

                        outcome = "missing"

                    elif (
                        call.from_user.id
                        != duel["p2_id"]
                    ):
                        outcome = "wrong_user"

                    elif (
                        duel["status"]
                        == "fighting"
                    ):
                        outcome = "already_fighting"

                    else:
                        balances = await _lock_users(
                            conn,
                            [
                                duel["p1_id"],
                                duel["p2_id"],
                            ],
                        )

                        balance_by_id = {
                            row["user_id"]: row["total_whine"]
                            for row in balances
                        }

                        p1_stake = int(
                            balance_by_id.get(
                                duel["p1_id"],
                                0,
                            )
                        )

                        p2_stake = int(
                            balance_by_id.get(
                                duel["p2_id"],
                                0,
                            )
                        )

                        if (
                            p1_stake <= 0
                            or p2_stake <= 0
                        ):
                            await conn.execute(
                                """
                                DELETE FROM duels
                                WHERE duel_id = $1
                                """,
                                duel_id,
                            )

                            outcome = "no_money"

                        else:
                            # Рассчитываем банк в Python.
                            # Не используем "$2 + $3" внутри SQL,
                            # потому что asyncpg/PostgreSQL
                            # воспринимал оба параметра как unknown.
                            duel_bank = (
                                p1_stake
                                + p2_stake
                            )

                            # Забираем ставки обоих игроков.
                            await conn.execute(
                                """
                                UPDATE users
                                SET total_whine = 0
                                WHERE user_id = ANY(
                                    $1::BIGINT[]
                                )
                                """,
                                [
                                    duel["p1_id"],
                                    duel["p2_id"],
                                ],
                            )

                            # Фиксируем реальные ставки и банк.
                            duel = await conn.fetchrow(
                                """
                                UPDATE duels
                                SET
                                    p1_stake = $2,
                                    p2_stake = $3,
                                    bank = $4,
                                    status = 'fighting',
                                    round_no = 0,
                                    expires_at = $5
                                WHERE duel_id = $1
                                RETURNING *
                                """,
                                duel_id,
                                p1_stake,
                                p2_stake,
                                duel_bank,
                                (
                                    datetime.now(
                                        timezone.utc
                                    )
                                    + timedelta(
                                        minutes=DUEL_FIGHT_MINUTES
                                    )
                                ),
                            )

                            await _refresh_user_rank(
                                conn,
                                duel["p1_id"],
                            )

                            await _refresh_user_rank(
                                conn,
                                duel["p2_id"],
                            )

                            outcome = "accepted"

    except TimeoutError:
        logger.warning(
            "Тайм-аут принятия дуэли %s",
            duel_id,
        )

        await message.answer(
            "⚠️ База отвечает слишком долго. "
            "Попробуй нажать ещё раз."
        )

        return

    except Exception:
        logger.exception(
            "Ошибка принятия дуэли %s",
            duel_id,
        )

        await message.answer(
            "⚠️ Не удалось принять дуэль. "
            "Попробуй ещё раз."
        )

        return

    if outcome == "missing":
        await message.answer(
            "⌛ Вызов истёк или уже завершён."
        )

    elif outcome == "wrong_user":
        await message.answer(
            f"{html_tag(call.from_user)}, "
            "это не твой вызов! 👺"
        )

    elif outcome == "no_money":
        with contextlib.suppress(Exception):
            await message.edit_text(
                "💸 Дуэль отменена: "
                "один из участников успел обнищать."
            )

    elif outcome in {
        "accepted",
        "already_fighting",
    }:
        await show_shoot_round(
            message,
            duel,
        )

# ------------------------------------------------------------
# DUEL SHOOT
# ------------------------------------------------------------

@dp.callback_query(
    F.data.startswith(
        "d_shot_"
    )
)
async def d_shoot(
    call: types.CallbackQuery,
) -> None:

    message = await callback_message(
        call
    )

    if (
        message is None
        or call.data is None
    ):
        return

    duel_id = call.data.removeprefix(
        "d_shot_"
    )

    # Спиннер гасим ДО PostgreSQL.
    try:
        await call.answer(
            "💥 Выстрел…"
        )
    except Exception:
        logger.exception(
            "Не удалось ответить callback shoot"
        )

    try:

        async with asyncio.timeout(
            DUEL_CALLBACK_TIMEOUT_SECONDS
        ):

            await cleanup_expired_duels(
                [call.from_user.id]
            )

            async with get_pool().acquire() as conn:
                async with conn.transaction():

                    duel = await conn.fetchrow(
                        """
                        SELECT *

                        FROM duels

                        WHERE duel_id = $1

                        FOR UPDATE
                        """,
                        duel_id,
                    )

                    if duel is None:
                        outcome = "missing"

                    elif (
                        duel["expires_at"]
                        <= datetime.now(
                            timezone.utc
                        )
                    ):

                        await _refund_expired_duel(
                            conn,
                            duel,
                        )

                        outcome = "expired"

                    elif (
                        duel["status"]
                        != "fighting"
                    ):
                        outcome = "not_started"

                    elif (
                        call.from_user.id
                        != duel["turn_id"]
                    ):
                        outcome = "wrong_turn"

                    elif (
                        random.random()
                        < DUEL_HIT_CHANCE
                    ):

                        outcome = "hit"

                        winner_id = duel["turn_id"]

                        loser_id = (
                            duel["p2_id"]
                            if winner_id
                            == duel["p1_id"]
                            else duel["p1_id"]
                        )

                        winner_name = (
                            duel["p1_name"]
                            if winner_id
                            == duel["p1_id"]
                            else duel["p2_name"]
                        )

                        bank = duel["bank"]

                        await _lock_users(
                            conn,
                            [
                                winner_id,
                                loser_id,
                            ],
                        )

                        await conn.execute(
                            """
                            UPDATE users

                            SET
                                total_whine =
                                    total_whine + $2,

                                duel_wins =
                                    duel_wins + 1

                            WHERE user_id = $1
                            """,
                            winner_id,
                            bank,
                        )

                        await conn.execute(
                            """
                            UPDATE users

                            SET
                                total_whine = 0,

                                duel_losses =
                                    duel_losses + 1

                            WHERE user_id = $1
                            """,
                            loser_id,
                        )

                        await _refresh_user_rank(
                            conn,
                            winner_id,
                        )

                        await _refresh_user_rank(
                            conn,
                            loser_id,
                        )

                        await conn.execute(
                            """
                            DELETE FROM duels

                            WHERE duel_id = $1
                            """,
                            duel_id,
                        )

                    else:

                        outcome = "miss"

                        next_turn = (
                            duel["p2_id"]
                            if duel["turn_id"]
                            == duel["p1_id"]
                            else duel["p1_id"]
                        )

                        duel = await conn.fetchrow(
                            """
                            UPDATE duels

                            SET
                                turn_id = $2,

                                round_no =
                                    round_no + 1,

                                expires_at = $3

                            WHERE duel_id = $1

                            RETURNING *
                            """,
                            duel_id,
                            next_turn,
                            (
                                datetime.now(
                                    timezone.utc
                                )
                                + timedelta(
                                    minutes=DUEL_FIGHT_MINUTES
                                )
                            ),
                        )

    except TimeoutError:

        logger.warning(
            "Тайм-аут выстрела дуэли %s",
            duel_id,
        )

        await message.answer(
            "⚠️ Сервер задумался. "
            "Нажми выстрел ещё раз."
        )

        return

    except Exception:

        logger.exception(
            "Ошибка выстрела дуэли %s",
            duel_id,
        )

        await message.answer(
            "⚠️ Не удалось обработать выстрел."
        )

        return

    if outcome in {
        "missing",
        "expired",
    }:

        await message.answer(
            "⌛ Дуэль истекла "
            "или уже завершена."
        )

    elif outcome == "not_started":

        await message.answer(
            "⚠️ Дуэль ещё не началась."
        )

    elif outcome == "wrong_turn":

        await message.answer(
            f"{html_tag(call.from_user)}, "
            "сейчас не твой ход! ⏳"
        )

    elif outcome == "hit":

        death_phrase = random.choice(
            [
                "ПОТРАЧЕНО! ⚰️",
                "В КАНАВУ! 🕳",
                "ОТКИС! 🧊",
                "ЗЕМЛЯ ПУХОМ! 🪦",
            ]
        )

        try:

            await message.edit_text(
                f"💀 <b>{death_phrase}</b>\n\n"
                f"🎯 Победитель забрал "
                f"<b>{bank} дБ</b>!\n"
                f"🏆 Чемпион: "
                f"<b>{escape(winner_name)}</b>\n"
                "📉 Проигравший обнулён. "
                "Иди скули с нуля! 🐷"
            )

        except TelegramBadRequest:
            await message.answer(
                f"💀 <b>{death_phrase}</b>\n\n"
                f"🎯 Победитель забрал "
                f"<b>{bank} дБ</b>!\n"
                f"🏆 Чемпион: "
                f"<b>{escape(winner_name)}</b>"
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

        await show_shoot_round(
            message,
            duel,
            prefix=f"💨 {miss_phrase}",
        )


# ------------------------------------------------------------
# DUEL DECLINE
# ------------------------------------------------------------

@dp.callback_query(
    F.data.startswith(
        "d_dec_"
    )
)
async def d_decline(
    call: types.CallbackQuery,
) -> None:

    message = await callback_message(
        call
    )

    if (
        message is None
        or call.data is None
    ):
        return

    duel_id = call.data.removeprefix(
        "d_dec_"
    )

    # Сразу убираем spinner.
    try:
        await call.answer(
            "Отменяю дуэль…"
        )
    except Exception:
        logger.exception(
            "Не удалось ответить callback decline"
        )

    try:

        async with asyncio.timeout(
            DUEL_CALLBACK_TIMEOUT_SECONDS
        ):

            async with get_pool().acquire() as conn:
                async with conn.transaction():

                    duel = await conn.fetchrow(
                        """
                        SELECT *

                        FROM duels

                        WHERE duel_id = $1

                        FOR UPDATE
                        """,
                        duel_id,
                    )

                    if duel is None:
                        outcome = "missing"

                    elif (
                        duel["expires_at"]
                        <= datetime.now(
                            timezone.utc
                        )
                    ):

                        await _refund_expired_duel(
                            conn,
                            duel,
                        )

                        outcome = "missing"

                    elif (
                        call.from_user.id
                        not in {
                            duel["p1_id"],
                            duel["p2_id"],
                        }
                    ):
                        outcome = "wrong_user"

                    elif (
                        duel["status"]
                        != "pending"
                    ):
                        outcome = "fighting"

                    else:

                        await conn.execute(
                            """
                            DELETE FROM duels

                            WHERE duel_id = $1
                            """,
                            duel_id,
                        )

                        outcome = "declined"

    except TimeoutError:

        await message.answer(
            "⚠️ Сервер отвечает слишком долго."
        )

        return

    except Exception:

        logger.exception(
            "Ошибка отмены дуэли %s",
            duel_id,
        )

        await message.answer(
            "⚠️ Не удалось отменить дуэль."
        )

        return

    if outcome == "missing":

        await message.answer(
            "Вызов уже исчез."
        )

    elif outcome == "wrong_user":

        await message.answer(
            f"{html_tag(call.from_user)}, "
            "это не твоя дуэль."
        )

    elif outcome == "fighting":

        await message.answer(
            "Поздно сливаться — "
            "дуэль уже началась."
        )

    else:

        try:

            await message.edit_text(
                "🏳️ Дуэль отменена. "
                "Один из нытиков поджал хвост "
                "и убежал. 🐕‍🦺"
            )

        except TelegramBadRequest:
            await message.answer(
                "🏳️ Дуэль отменена."
            )


# ============================================================
# SHOP
# ============================================================

@dp.message(Command("shop"))
async def shop(
    message: Message,
) -> None:

    if not await require_chat_active(
        message
    ):
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"{config['label']} "
                        f"({config['price']} ⭐️)"
                    ),
                    callback_data=(
                        f"buy_{rank_key}"
                    ),
                )
            ]
            for rank_key, config
            in RANKS.items()
            if config["price"] > 0
        ]
    )

    await message.answer(
        "🏪 <b>Магазин Скулежа</b>\n"
        "Выбери статус на 30 дней "
        "(защита от слива ранга):",
        reply_markup=keyboard,
    )


# ============================================================
# INFO
# ============================================================

@dp.message(Command("info"))
async def info_handler(
    message: Message,
) -> None:

    text = (
        "ℹ️ <b>ИНФОРМАЦИЯ О ПОСКУЛИБОТЕ</b> ℹ️\n"
        "________________________________\n\n"

        "🎮 <b>ОСНОВНЫЕ КОМАНДЫ:</b>\n"

        "• <code>/skulistart</code> — регистрация\n"

        "• <code>/poskuli</code> "
        "или <code>+поскулить</code> — замер\n"

        "• <code>/skulibet 50</code> "
        "или <code>+казик 50</code> — казино\n"

        "• <code>/transfer 100</code> "
        "или <code>+перевод 100</code> ответом — перевод 💸\n"

        "• <code>/duel</code> "
        "или <code>+дуэль</code> ответом — дуэль ⚔️\n"

        "• <code>/skuliname Имя</code> — погоняло\n"

        "• <code>/skuliname reset</code> — вернуть Telegram-имя\n"

        "• <code>/topskuli</code> — топ чата\n"

        "• <code>/topglobal</code> — мировой рейтинг\n"

        "• <code>/shop</code> — магазин ⭐️\n\n"

        "📈 <b>РАНГИ ЗА дБ:</b>\n"

        "• 10к — КМС 🚀\n"
        "• 30к — МС 🌠\n"
        "• 100к — Ангел 👑\n"
        "• 500к — Бог 💎\n"
        "• 1.5м — Всемогущий 🌌\n"
        "• 1 млрд — Олимпиец 🏛️\n\n"

        "🏛️ Олимпиец может использовать "
        "<code>/mute</code> или <code>+мут</code> "
        "до 10 минут.\n\n"

        "💎 <b>ПРИВИЛЕГИИ:</b>\n"

        "1. Купленный статус защищён 30 дней.\n"

        "2. Множитель замера — до x3.0.\n"

        "3. Казино зависит от ранга.\n"

        "4. Олимпиец может мутить не-админов.\n"

        "5. Архитектор: "
        "<code>/take</code>, "
        "<code>/mute</code>, "
        "<code>/ban</code>, "
        "<code>/grant</code>, "
        "<code>/vault</code>.\n\n"

        "⚙️ <i>Архитектор слышит твой скулёж...</i>"
    )

    await message.answer(
        text,
        link_preview_options=types.LinkPreviewOptions(
            is_disabled=True
        ),
    )


# ============================================================
# BUY
# ============================================================

@dp.callback_query(
    F.data.startswith(
        "buy_"
    )
)
async def buy_call(
    call: types.CallbackQuery,
) -> None:

    message = await callback_message(
        call
    )

    if (
        message is None
        or call.data is None
    ):
        return

    if (
        message.chat.type != ChatType.PRIVATE
        and not await is_chat_on(
            message.chat.id
        )
    ):

        await call.answer(
            "💤 Бот спит, магазин закрыт.",
            show_alert=True,
        )

        return

    rank_key = call.data.removeprefix(
        "buy_"
    )

    config = RANKS.get(
        rank_key
    )

    if (
        config is None
        or config["price"] <= 0
    ):

        await call.answer(
            "Такого товара нет.",
            show_alert=True,
        )

        return

    await call.answer(
        "Открываю оплату…"
    )

    await bot.send_invoice(
        chat_id=message.chat.id,

        title=(
            f"Статус "
            f"{config['label']}"
        ),

        description=(
            "Привилегии на 30 дней"
        ),

        payload=(
            f"pay_{rank_key}"
        ),

        currency="XTR",

        prices=[
            LabeledPrice(
                label="⭐️",
                amount=config["price"],
            )
        ],
    )


def paid_rank_from_payload(
    payload: str,
) -> str | None:

    if not payload.startswith(
        "pay_"
    ):
        return None

    rank_key = payload.removeprefix(
        "pay_"
    )

    config = RANKS.get(
        rank_key
    )

    if (
        config is None
        or config["price"] <= 0
    ):
        return None

    return rank_key


# ============================================================
# PAYMENTS
# ============================================================

@dp.pre_checkout_query()
async def pre_checkout(
    query: PreCheckoutQuery,
) -> None:

    rank_key = paid_rank_from_payload(
        query.invoice_payload
    )

    valid = bool(
        rank_key
        and query.currency == "XTR"
        and query.total_amount
        == RANKS[rank_key]["price"]
    )

    await bot.answer_pre_checkout_query(
        query.id,
        ok=valid,
        error_message=(
            None
            if valid
            else (
                "Цена или товар устарели. "
                "Открой /shop заново."
            )
        ),
    )


@dp.message(
    F.successful_payment
)
async def successful_payment(
    message: Message,
) -> None:

    if (
        message.from_user is None
        or message.successful_payment is None
    ):
        return

    payment = (
        message.successful_payment
    )

    rank_key = paid_rank_from_payload(
        payment.invoice_payload
    )

    if (
        rank_key is None
        or payment.currency != "XTR"
        or payment.total_amount
        != RANKS[rank_key]["price"]
    ):

        logger.error(
            "Получен платёж "
            "с неверными параметрами: %r",
            payment,
        )

        await message.answer(
            "⚠️ Платёж получен, "
            "но товар не распознан. "
            "Обратись к Архитектору."
        )

        return

    async with get_pool().acquire() as conn:
        async with conn.transaction():

            await _ensure_user(
                conn,
                message.from_user,
                message.chat.id,
            )

            inserted = await conn.fetchval(
                """
                INSERT INTO payments (
                    telegram_payment_charge_id,
                    provider_payment_charge_id,

                    user_id,
                    payload,
                    currency,
                    amount
                )

                VALUES (
                    $1,
                    $2,
                    $3,
                    $4,
                    $5,
                    $6
                )

                ON CONFLICT (
                    telegram_payment_charge_id
                )
                DO NOTHING

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

                    SET
                        status = $2,

                        is_premium = TRUE,

                        vip_expire =
                            GREATEST(
                                COALESCE(
                                    vip_expire,
                                    NOW()
                                ),
                                NOW()
                            )
                            + INTERVAL '30 days'

                    WHERE user_id = $1
                    """,
                    message.from_user.id,
                    rank_key,
                )

    if inserted:

        await message.answer(
            "✨ ТЫ СРЕДИ БОГОВ ОЛИМПА! "
            "Ты теперь "
            f"<b>{escape(RANKS[rank_key]['label'])}</b>! "
            "Скули с привилегиями!"
        )

    else:

        await message.answer(
            "✅ Этот платёж уже был обработан."
        )


# ============================================================
# RUNTIME
# ============================================================

async def start_runtime() -> None:

    global cleanup_task

    if db_pool is None:
        await open_database()

    await init_db()

    cleaned = await cleanup_expired_duels()

    if cleaned:
        logger.info(
            "При старте очищено дуэлей: %s",
            cleaned,
        )

    if (
        cleanup_task is None
        or cleanup_task.done()
    ):

        cleanup_task = asyncio.create_task(
            duel_cleanup_loop(),
            name="duel-cleanup",
        )

    logger.info(
        "PostgreSQL подключён"
    )


async def stop_runtime() -> None:

    global cleanup_task

    if cleanup_task is not None:

        cleanup_task.cancel()

        with contextlib.suppress(
            asyncio.CancelledError
        ):
            await cleanup_task

        cleanup_task = None

    await close_database()


# ============================================================
# POLLING
# ============================================================

async def polling_main() -> None:

    await start_runtime()

    try:

        # Если раньше использовался webhook,
        # polling иначе может вообще не заработать.
        await bot.delete_webhook(
            drop_pending_updates=False
        )

        logger.info(
            "Бот запущен в режиме long polling"
        )

        await dp.start_polling(
            bot,
            allowed_updates=(
                dp.resolve_used_update_types()
            ),
            close_bot_session=False,
        )

    finally:

        await stop_runtime()

        await bot.session.close()


# ============================================================
# WEBHOOK
# ============================================================

async def webhook_startup(
    bot: Bot,
) -> None:

    if not WEBHOOK_BASE_URL:
        raise RuntimeError(
            "WEBHOOK_BASE_URL обязателен "
            "при RUN_MODE=webhook"
        )

    if not WEBHOOK_SECRET:
        raise RuntimeError(
            "WEBHOOK_SECRET обязателен "
            "при RUN_MODE=webhook"
        )

    await start_runtime()

    webhook_url = (
        f"{WEBHOOK_BASE_URL}"
        f"{WEBHOOK_PATH}"
    )

    await bot.set_webhook(
        webhook_url,

        secret_token=WEBHOOK_SECRET,

        allowed_updates=(
            dp.resolve_used_update_types()
        ),

        drop_pending_updates=False,
    )

    logger.info(
        "Telegram webhook настроен: %s",
        webhook_url,
    )


async def webhook_shutdown(
    bot: Bot,
) -> None:

    # Webhook специально НЕ удаляем.
    # Telegram должен продолжать обращаться
    # к Cloud Run после scale-to-zero.

    await stop_runtime()

    await bot.session.close()


async def healthcheck(
    _: web.Request,
) -> web.Response:

    return web.Response(
        text="ok"
    )


def webhook_main() -> None:

    dp.startup.register(
        webhook_startup
    )

    dp.shutdown.register(
        webhook_shutdown
    )

    app = web.Application()

    app.router.add_get(
        "/",
        healthcheck,
    )

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=False,
        secret_token=WEBHOOK_SECRET,
    ).register(
        app,
        path=WEBHOOK_PATH,
    )

    setup_application(
        app,
        dp,
        bot=bot,
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=env_int(
            "PORT",
            8080,
        ),
    )


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":

    if RUN_MODE == "webhook":

        webhook_main()

    elif RUN_MODE == "polling":

        asyncio.run(
            polling_main()
        )

    else:

        raise RuntimeError(
            "RUN_MODE должен быть "
            "'polling' или 'webhook'"
        )
