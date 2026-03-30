import random
import sqlite3
import asyncio
import time
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
import os
TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)

# --- НАСТРОЙКИ ---

DB_NAME = 'whine_bot.db'
COOLDOWN_MINUTES = 5  # Твой новый кулдаун

bot = Bot(token=TOKEN)
dp = Dispatcher()


# --- БЛОК БАЗЫ ДАННЫХ ---

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''
                CREATE TABLE IF NOT EXISTS users
                (
                    user_id
                    INTEGER
                    PRIMARY
                    KEY,
                    name
                    TEXT,
                    total_whine
                    INTEGER
                    DEFAULT
                    0,
                    last_whine
                    INTEGER
                    DEFAULT
                    0
                )
                ''')
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT name, total_whine, last_whine FROM users WHERE user_id = ?', (user_id,))
    user = cur.fetchone()
    conn.close()
    return user if user else ("Аноним", 0, 0)


def update_user(user_id, name=None, whine_score=None, update_time=False):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO users (user_id, name, total_whine, last_whine) VALUES (?, ?, 0, 0)',
                (user_id, str(name) if name else "Аноним"))
    if name:
        cur.execute('UPDATE users SET name = ? WHERE user_id = ?', (str(name), user_id))
    if whine_score is not None:
        cur.execute('UPDATE users SET total_whine = COALESCE(total_whine, 0) + ? WHERE user_id = ?',
                    (whine_score, user_id))
    if update_time:
        cur.execute('UPDATE users SET last_whine = ? WHERE user_id = ?', (int(time.time()), user_id))
    conn.commit()
    conn.close()


def get_leaderboard():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        'SELECT name, total_whine, user_id FROM users WHERE total_whine != 0 ORDER BY total_whine DESC LIMIT 10')
    rows = cur.fetchall()
    conn.close()
    return rows


# --- ХЕНДЛЕРЫ КОМАНД ---

@dp.message(Command("skulistart"))
async def start(message: Message):
    update_user(message.from_user.id, name=message.from_user.first_name)
    await message.answer("✅ Регистрация прошла успешно! Команда: /poskuli")


@dp.message(Command("poskuli"))
async def measure_whine(message: Message):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    name, current_total, last_time = user_data

    current_time = int(time.time())
    wait_time = (last_time + COOLDOWN_MINUTES * 60) - current_time
    user_tag = f"[{name}](tg://user?id={user_id})"

    if wait_time > 0:
        minutes, seconds = divmod(wait_time, 60)
        return await message.answer(
            f"🚫 {user_tag}, связки не восстановились!\nЖди еще **{minutes}м {seconds}с**",
            parse_mode="Markdown"
        )

    # Логика штрафов (20% шанс)
    is_penalty = random.random() < 0.20

    if is_penalty:
        # ТВОЙ БЛОК ШТРАФОВ
        fails = [
            "Прибор определил это как полная хуета. К сожалению, штраф!",
            "Поскулил как фанат чмотраса, а фанаты чмотраса хуесосы, минус вайб",
            "Это был не скулёж, а зевок. Учись скулить у чмани и пхуесоса.",
            "Ты начал ныть, но подавился слюной. Но хоть не малафьей! Нахуй с пляжа",
            "Паскудный скулеж, как будто ты не поскулить решил а пососать, штраф!"
        ]

        db_loss = random.randint(1, 5)
        update_user(user_id, whine_score=-db_loss, update_time=True)
        new_total = current_total - db_loss

        await message.answer(
            f"📉 {user_tag}, **-{db_loss} дБ**!\n❌ {random.choice(fails)}\nИтог: **{new_total} дБ**",
            parse_mode="Markdown"
        )
    else:
        # ПРИБАВКА
        db_gain = random.randint(10, 50)
        update_user(user_id, whine_score=db_gain, update_time=True)
        new_total = current_total + db_gain

        mood = "🤫 Тихое поскуливание" if db_gain < 40 else "📢 Скулишь пиздец!" if db_gain > 40 else "🫨 Средний вой"

        await message.answer(
            f"📈 {user_tag}, замер: **{db_gain} дБ**\nℹ️ Статус: {mood}\nВсего накоплено: **{new_total} дБ**",
            parse_mode="Markdown"
        )


@dp.message(Command("skuliname"))
async def change_name(message: Message):
    new_name = message.text.replace("/skuliname", "").strip()
    if not new_name:
        return await message.answer("⚠️ Введи имя после команды.")

    update_user(message.from_user.id, name=new_name)
    await message.answer(f"🤝 К сожалению теперь ты: **{new_name}**", parse_mode="Markdown")


@dp.message(Command("top_skuli"))
async def leaderboard(message: Message):
    top = get_leaderboard()
    if not top:
        return await message.answer("📭 Пока никто не скулил.")

    text = "🏆 **ЗАЛ СЛАВЫ НЫТИКОВ :**\n\n"
    for i, (name, total, u_id) in enumerate(top, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        u_tag = f"[{name}](tg://user?id={u_id})"
        text += f"{medal} {u_tag} — `{total} дБ` накоплено\n"

    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("skuli_bet"))
async def casino_bet(message: Message):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    name, current_total, _ = user_data
    user_tag = f"[{name}](tg://user?id={user_id})"

    # Разбираем команду: /skuli_bet 100
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.answer(f"⚠️ {user_tag}, пиши сумму ставки числом!\nПример: `/skuli_bet 50`",
                                    parse_mode="Markdown")

    bet = int(parts[1])

    if bet <= 0:
        return await message.answer(f"⚠️ {user_tag}, ставка должна быть больше нуля!")

    if bet > current_total:
        return await message.answer(f"🚫 {user_tag}, у тебя всего **{current_total} дБ**. Ты бичара из кантеры!",
                                    parse_mode="Markdown")

    roll = random.random()

    if roll < 0.60:  # 60% шанс проигрыша (казино всегда в плюсе)
        update_user(user_id, whine_score=-bet)
        await message.answer(f"🎰 {user_tag}, лудоманское горе, к сожалению! Ставка **{bet} дБ** сгорела. 📉",
                             parse_mode="Markdown")

    elif roll < 0.90:  # 30% шанс на x2
        update_user(user_id, whine_score=bet)
        await message.answer(f"🎰 {user_tag}, КУШ! К счастью, Ты удвоил свои сопли: **+{bet} дБ**! 💰", parse_mode="Markdown")

    else:  # 10% шанс на x5 (Джекпот)
        win = bet * 4
        update_user(user_id, whine_score=win)
        await message.answer(f"🎰 {user_tag}, ДЖЕКПОТ! Твой скулёж услышали боги, ТЫ ЛАМИН ЯМАЛЬ: **+{win + bet} дБ**! 🔥🔥🔥",
                             parse_mode="Markdown")


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
