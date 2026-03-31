import random
import sqlite3
import asyncio
import time
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message

# --- НАСТРОЙКИ ПУТЕЙ (Для Render Disk) ---
DB_NAME = "/data/whine_bot.db"
if not os.path.exists("/data"):
    DB_NAME = "whine_bot.db"

# --- НАСТРОЙКИ БОТА ---
TOKEN = os.getenv('BOT_TOKEN')
COOLDOWN_MINUTES = 5

bot = Bot(token=TOKEN)
dp = Dispatcher()


# --- БЛОК БАЗЫ ДАННЫХ ---

def get_db_connection():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
                CREATE TABLE IF NOT EXISTS users
                (
                    user_id
                    INTEGER,
                    chat_id
                    INTEGER,
                    name
                    TEXT,
                    total_whine
                    INTEGER
                    DEFAULT
                    0,
                    last_whine
                    INTEGER
                    DEFAULT
                    0,
                    PRIMARY
                    KEY
                (
                    user_id,
                    chat_id
                )
                    )
                ''')
    conn.commit()
    conn.close()


def get_user(user_id, chat_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT name, total_whine, last_whine FROM users WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    user = cur.fetchone()
    conn.close()
    return user if user else (None, 0, 0)


def update_user(user_id, chat_id, name=None, whine_score=None, update_time=False):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
                INSERT
                OR IGNORE INTO users (user_id, chat_id, name, total_whine, last_whine) 
        VALUES (?, ?, ?, 0, 0)
                ''', (user_id, chat_id, str(name) if name else "Аноним"))
    if name:
        cur.execute('UPDATE users SET name = ? WHERE user_id = ? AND chat_id = ?', (str(name), user_id, chat_id))
    if whine_score is not None:
        cur.execute('UPDATE users SET total_whine = COALESCE(total_whine, 0) + ? WHERE user_id = ? AND chat_id = ?',
                    (whine_score, user_id, chat_id))
    if update_time:
        cur.execute('UPDATE users SET last_whine = ? WHERE user_id = ? AND chat_id = ?',
                    (int(time.time()), user_id, chat_id))
    conn.commit()
    conn.close()


def get_leaderboard(chat_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        'SELECT name, total_whine, user_id FROM users WHERE chat_id = ? AND total_whine != 0 ORDER BY total_whine DESC LIMIT 10',
        (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


# --- ХЕНДЛЕРЫ КОМАНД ---

@dp.message(Command("skulistart"))
async def start(message: Message):
    update_user(message.from_user.id, message.chat.id, name=message.from_user.first_name)
    await message.answer("✅ Регистрация прошла успешно! Команда: /poskuli")


@dp.message(Command("poskuli"))
async def measure_whine(message: Message):
    user_id, chat_id = message.from_user.id, message.chat.id
    user_data = get_user(user_id, chat_id)
    name, current_total, last_time = user_data
    if name is None:
        name = message.from_user.first_name
        update_user(user_id, chat_id, name=name)

    wait_time = (last_time + COOLDOWN_MINUTES * 60) - int(time.time())
    user_tag = f"[{name}](tg://user?id={user_id})"

    if wait_time > 0:
        minutes, seconds = divmod(wait_time, 60)
        return await message.answer(f"🚫 {user_tag}, связки не восстановились!\nЖди еще **{minutes}м {seconds}с**",
                                    parse_mode="Markdown")

    if random.random() < 0.20:
        fails = [
            "Прибор определил это как полная хуета. К сожалению, штраф!",
            "Поскулил как фанат чмотраса, а фанаты чмотраса хуесосы, минус вайб",
            "Это был не скулёж, а зевок. Учись скулить у чмани и пхуесоса.",
            "Ты начал ныть, но подавился слюной. Но хоть не малафьей! Нахуй с пляжа",
            "Паскудный скулеж, как будто ты не поскулить решил а пососать, штраф!"
        ]
        db_loss = random.randint(1, 5)
        update_user(user_id, chat_id, whine_score=-db_loss, update_time=True)
        await message.answer(
            f"📉 {user_tag}, **-{db_loss} дБ**!\n❌ {random.choice(fails)}\nИтог: **{current_total - db_loss} дБ**",
            parse_mode="Markdown")
    else:
        db_gain = random.randint(10, 50)
        update_user(user_id, chat_id, whine_score=db_gain, update_time=True)
        mood = "🤫 Тихое поскуливание" if db_gain < 30 else "📢 Скулишь пиздец!" if db_gain > 40 else "🫨 Средний вой"
        await message.answer(
            f"📈 {user_tag}, замер: **{db_gain} дБ**\nℹ️ Статус: {mood}\nВсего: **{current_total + db_gain} дБ**",
            parse_mode="Markdown")


@dp.message(Command("skulibet"))
async def casino_bet(message: Message):
    user_id, chat_id = message.from_user.id, message.chat.id
    user_data = get_user(user_id, chat_id)
    name, current_total, _ = user_data
    user_tag = f"[{name}](tg://user?id={user_id})"

    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.answer(f"⚠️ {user_tag}, пиши сумму: `/skulibet 50`", parse_mode="Markdown")

    bet = int(parts[1])
    if bet <= 0: return await message.answer("⚠️ Ставка должна быть > 0")
    if bet > current_total: return await message.answer(f"🚫 {user_tag}, у тебя только **{current_total} дБ**! Ты бичара из кантеры!")

    roll = random.random()
    if roll < 0.60:
        update_user(user_id, chat_id, whine_score=-bet)
        await message.answer(f"🎰 {user_tag}, ставка **{bet} дБ** сгорела, иди скули! 📉", parse_mode="Markdown")
    elif roll < 0.90:
        update_user(user_id, chat_id, whine_score=bet)
        await message.answer(f"🎰 {user_tag}, КУШ! Твой носок > карьера Коке: **+{bet} дБ**! 💰", parse_mode="Markdown")
    else:
        win = bet * 4
        update_user(user_id, chat_id, whine_score=win)
        await message.answer(f"🎰 {user_tag}, ДЖЕКПОТ! Скулёж услышан Богами! ТЫ ЛАМИН ЯМАЛЬ: **+{win + bet} дБ**! 🔥", parse_mode="Markdown")


@dp.message(Command("skuliname"))
async def change_name(message: Message):
    new_name = message.text.replace("/skuliname", "").strip()
    if not new_name: return await message.answer("⚠️ Введи имя после команды.")
    update_user(message.from_user.id, message.chat.id, name=new_name)
    await message.answer(f"🤝 К сожалению теперь ты: **{new_name}**", parse_mode="Markdown")


@dp.message(Command("top_skuli"))
async def leaderboard(message: Message):
    top = get_leaderboard(message.chat.id)
    if not top: return await message.answer("📭 В чате пусто.")
    text = "🏆 **ТОП НЫТИКОВ ЧАТА:**\n\n"
    for i, (n, t, u_id) in enumerate(top, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        text += f"{medal} [{n}](tg://user?id={u_id}) — `{t} дБ` накоплено\n"
    await message.answer(text, parse_mode="Markdown")


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

