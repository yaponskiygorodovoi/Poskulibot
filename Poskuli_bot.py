import random, sqlite3, asyncio, time, os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, InlineKeyboardButton, InlineKeyboardMarkup

# --- НАСТРОЙКИ ---
DB_NAME = "/data/whine_bot.db"
if not os.path.exists("/data"):
    DB_NAME = "whine_bot.db"

# --- НАСТРОЙКИ БОТА ---
TOKEN = os.getenv('BOT_TOKEN')


bot = Bot(token=TOKEN)
dp = Dispatcher()

ARCHITECT_ID = 6421600902  # !!! ПОСТАВЬ СВОЙ ID !!!
COOLDOWN_MINUTES = 5

# Конфиг уровней
RANKS = {
    "omnipotent": {"thresh": 1500000, "price": 1500, "label": "Всемогущий 🌌", "chance": 0.50, "all_in": 0.85, "cb": 0.25, "multiplier": 2.5},
    "diamond": {"thresh": 500000, "price": 1000, "label": "Бог 💎", "chance": 0.49, "all_in": 0.83, "cb": 0.20, "multiplier": 2.0},
    "gold": {"thresh": 100000, "price": 500, "label": "Ангел 👑", "chance": 0.48, "all_in": 0.81, "cb": 0.15, "multiplier": 1.5},
    "silver": {"thresh": 30000, "price": 150, "label": "МС 🌠", "chance": 0.46, "all_in": 0.79, "cb": 0.10, "multiplier": 1.2},
    "bronze": {"thresh": 10000, "price": 50, "label": "КМС 🚀", "chance": 0.44, "all_in": 0.77, "cb": 0.05, "multiplier": 1.1},
    "user": {"thresh": 0, "price": 0, "label": "Новичок 👤", "chance": 0.42, "all_in": 0.75, "cb": 0.00, "multiplier": 1.0}
}


# --- БД (ИНТЕГРАЦИЯ НОВЫХ ПОЛЕЙ) ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    # Добавляем новые колонки в старую таблицу (если их нет)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'user'")
    except:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_premium BOOLEAN DEFAULT 0")
    except:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN vip_expire TEXT")
    except:
        pass
    # Таблица для Казны Архитектора
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)')
    conn.execute('INSERT OR IGNORE INTO settings VALUES ("vault", 100000000)')
    conn.commit()
    conn.close()
def cleanup_duplicates():
    conn = sqlite3.connect(DB_NAME)
    # Оставляем только одну запись для каждого пользователя с максимальным балансом
    conn.execute('''
        DELETE FROM users 
        WHERE rowid NOT IN (
            SELECT rowid FROM users GROUP BY user_id HAVING max(total_whine)
        )
    ''')
    conn.commit()
    conn.close()


def get_u(uid):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Убрали chat_id и добавили запятую после uid
    cur.execute(
        'SELECT name, total_whine, last_whine, status, is_premium, vip_expire FROM users WHERE user_id = ?',
        (uid,)
    )
    r = cur.fetchone()
    conn.close()
    if not r: return None
    return {
        "name": r[0], 
        "total": r[1], 
        "last": r[2], 
        "status": r[3], 
        "is_p": r[4], 
        "exp": r[5]
    }



async def update_score(uid, amt, upd_t=False):
    conn = sqlite3.connect(DB_NAME)
    # УБРАЛИ AND chat_id = ?, чтобы баланс обновлялся везде сразу
    conn.execute('UPDATE users SET total_whine = total_whine + ? WHERE user_id = ?', (amt, uid))
    
    if upd_t: 
        conn.execute('UPDATE users SET last_whine = ? WHERE user_id = ?',
                       (int(time.time()), uid))
    conn.commit()
    conn.close()

    # Авто-ранг: вызываем get_u тоже только с одним аргументом uid
    u = get_u(uid)
    if not u: return
    
    # Защита для Архитектора или купленного VIP
    if uid == ARCHITECT_ID or (u['is_p'] and u['exp'] and datetime.fromisoformat(u['exp']) > datetime.now()): 
        return

    # Логика определения нового ранга
    new_s = "user"
    for r_k, r_v in RANKS.items():
        if u['total'] >= r_v['thresh']:
            new_s = r_k
            break
            
    if u['status'] != new_s:
        conn = sqlite3.connect(DB_NAME)
        # Обновляем статус глобально для юзера
        conn.execute('UPDATE users SET status = ? WHERE user_id = ?', (new_s, uid))
        conn.commit()
        conn.close()


def set_user_name(uid, new_name):
    conn = sqlite3.connect(DB_NAME)
    # Обновляем имя по user_id, чтобы оно изменилось везде сразу
    conn.execute('UPDATE users SET name = ? WHERE user_id = ?', (str(new_name), uid))
    conn.commit()
    conn.close()




def get_global_leaderboard(limit=15):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Берем топ игроков по всей таблице, независимо от чата
    cur.execute('''
        SELECT name, total_whine, status, user_id 
        FROM users 
        WHERE total_whine > 0 
        ORDER BY total_whine DESC 
        LIMIT ?
    ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- КОМАНДЫ ---
@dp.message(Command("skulistart"))
async def start(message: Message):
    conn = sqlite3.connect(DB_NAME)
    st = "architect" if message.from_user.id == ARCHITECT_ID else "user"
    conn.execute('INSERT OR IGNORE INTO users (user_id, chat_id, name, status) VALUES (?, ?, ?, ?)',
                 (message.from_user.id, message.chat.id, message.from_user.first_name, st))
    conn.commit();
    conn.close()
    await message.answer("✅ Регистрация успешна! Юзай /poskuli")




@dp.message(Command("poskuli"))
async def measure_whine(message: Message):
    user_id = message.from_user.id
    # ГЛОБАЛЬНО: получаем данные только по user_id
    u = get_u(user_id)

    if not u:
        return await message.answer("⚠️ Сначала нажми /skulistart, чтобы прибор тебя запомнил!")

    name, current_total, last_time = u['name'], u['total'], u['last']
    
    # Расчет Кулдауна (5 минут)
    current_time = int(time.time())
    wait_time = (last_time + COOLDOWN_MINUTES * 60) - current_time
    
    # Экранируем имя для Markdown
    safe_name = name.replace("_", "\\_").replace("*", "\\*")
    user_tag = f"[{safe_name}](tg://user?id={user_id})"

    if wait_time > 0:
        minutes, seconds = divmod(wait_time, 60)
        return await message.answer(
            f"🚫⛔ {user_tag}, связки не восстановились!\nЖди еще **{minutes}м {seconds}с**",
            parse_mode="Markdown"
        )

    # Берем конфиг ранга (множитель)
    cfg = RANKS.get(u['status'], RANKS['user'])
    multiplier = cfg.get('multiplier', 1.0)

    # Шанс штрафа (20%)
    if random.random() < 0.20:
        fails = [
            "Прибор определил это как полная хуета. 🥺 К сожалению, штраф!🫵🤡",
            "Поскулил как фанат Атлетико, а фанаты Атлетико скулят мерзко, минус вайб👺👎",
            "Это был не скулёж, а зевок. Учись скулить у первых скулюнов!💩☠️👀",
            "Ты начал ныть, но, к сожалению, подавился слюной! Нахуй с пляжа👺",
            "Паскудный скулеж, как будто ты не поскулить решил а пососать, штраф!🫵🤡",
            "Ты фанат Реала?🤡 Что за пронзительный скулёж на судей? Не одобрено!🤡",
            "Хави смеется над тем как ты слабо скулишь! Пробуй снова!👀",
            "Какая же хуетень,чувак, угараем всей командой разработчиков, больше так не позорься!💩👀",
            "Как же ты срешь на ляшки,чел🤡",
            "К сожалению ты обосрался🤡",
            "Доволен собой? Но это хуйня, к сожалению!🫵🤡",
            "Что это за дрисня?🤡 Иди поплачь!🤡"
        ]
        db_loss = random.randint(1, 5)
        # ВАЖНО: передаем только user_id и amt
        await update_score(user_id, -db_loss, upd_t=True)

        await message.answer(
            f"📉 {user_tag}, **-{db_loss} дБ**!\n❌ {random.choice(fails)}\nИтог: **{current_total - db_loss} дБ**",
            parse_mode="Markdown"
        )
    else:
        # Прибавка (10-200) * множитель статуса
        base_gain = random.randint(10, 200)
        db_gain = int(base_gain * multiplier)
        
        await update_score(user_id, db_gain, upd_t=True)

        mood = "🤫 Тихое поскуливание" if db_gain < 40 else "📢 Скулишь пиздец!" if db_gain > 100 else "🫨 Средний вой"
        bonus_text = f" (Бонус ранга x{multiplier})" if multiplier > 1.0 else ""

        await message.answer(
            f"📈 {user_tag}, замер: **{db_gain} дБ**{bonus_text}\nℹ️ Статус: {mood}\nВсего накоплено: **{current_total + db_gain} дБ**",
            parse_mode="Markdown"
        )


    # После замера ранг проверяется автоматически внутри update_score


@dp.message(Command("skulibet"))
async def bet(message: Message, command: CommandObject):
    u = get_u(message.from_user.id, message.chat.id)
    if not command.args or not command.args.isdigit(): return
    val = int(command.args)
    if val > u['total'] or val <= 0: return await message.answer("Бичара из Кантеры Реала, дБ не хватает! Пришло время уходишь в Хетафе!")

    cfg = RANKS.get(u['status'], RANKS['user'])
    if u['status'] == "architect": cfg = RANKS['bronze']

    is_all = val >= u['total']
    chance = cfg['all_in'] if is_all else cfg['chance']

    if random.random() < chance:
        win = int(val * (1.2 if is_all else 2.0))
        if not is_all and random.random() > 0.90:  # Джекпот
            win = val * 4
            msg = f"🎰 ДЖЕКПОТ! БОГИ СЛЫШАТ ТВОЙ СКУЛЁЖ! ТЫ ЛАМИН ЯМАЛЬ : **+{win} дБ**!"
        else:
            msg = f"🎰 КУШ! : **+{win} дБ**!"
        await update_score(message.from_user.id, message.chat.id, win - val)
        await message.answer(msg, parse_mode="Markdown")
    else:
        cb = int(val * cfg['cb'])
        await update_score(message.from_user.id, message.chat.id, -val + cb)
        await message.answer(f"🎰 Ставка **{val} дБ** сгорела, иди скули! Кэшбек: {cb}", parse_mode="Markdown")


@dp.message(Command("skuliname"))
async def change_name(message: Message, command: CommandObject):
    # Берем текст после команды
    new_name = command.args

    if not new_name:
        return await message.answer("⚠️ Введи новое имя после команды: `/skuliname Сын Коке`",
                                    parse_mode="Markdown")

    # Ограничим длину ника, чтобы не ломать верстку топа
    if len(new_name) > 20:
        return await message.answer("🚫 Слишком длинное погоняло! Максимум 20 символов.")

    # Сохраняем в базу
    set_user_name(message.from_user.id, new_name)

    # Экранируем для Markdown
    safe_name = new_name.replace("_", "\\_").replace("*", "\\*")

    await message.answer(f"🤝 К сожалению, теперь ты: **{safe_name}**", parse_mode="Markdown")


@dp.message(Command("grant"), F.from_user.id == ARCHITECT_ID)
async def grant(message: Message, command: CommandObject):
    if not message.reply_to_message or not command.args.isdigit(): return
    amt = int(command.args);
    tid = message.reply_to_message.from_user.id
    conn = sqlite3.connect(DB_NAME);
    conn.execute('UPDATE settings SET value = value - ? WHERE key = "vault"', (amt,));
    conn.commit();
    conn.close()
    await update_score(tid, message.chat.id, amt)
    await message.delete()
    await message.answer(
        f"⚡️ **Глас Асгарда**\n\nТы скулил так, что тебя услышали в Асгарде, тебе послали бонус **{amt} дБ**!")


@dp.message(Command("topskuli"))
async def top_chat(message: Message):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        'SELECT name, total_whine, status, user_id FROM users WHERE chat_id = ? ORDER BY total_whine DESC LIMIT 10',
        (message.chat.id,))
    rows = cur.fetchall();
    conn.close()
    res = "🏆 **ТОП НЫТИКОВ ЧАТА:**\n\n"
    for i, (n, t, s, uid) in enumerate(rows, 1):
        pref = "🌚🔧" if uid == ARCHITECT_ID else RANKS.get(s, RANKS['user'])['label'].split()[-1]
        res += f"{i}. {pref} {n} — `{t} дБ` \n"
    await message.answer(res, parse_mode="Markdown")


@dp.message(Command("topglobal"))
async def global_top_handler(message: Message):
    top_users = get_global_leaderboard(15)

    if not top_users:
        return await message.answer("🌌 Во вселенной скулеж еще не зафиксирован.")

    text = "🌌 **ГЛОБАЛЬНЫЙ РЕЙТИНГ ВСЕЛЕННОЙ**\n"
    text += "________________________________\n\n"

    for i, (name, total, status, uid) in enumerate(top_users, 1):
        # Экранируем спецсимволы в именах, чтобы Markdown не ломался
        safe_name = name.replace("_", "\\_").replace("*", "\\*")

        # Определяем иконку и подпись статуса из нашего конфига RANKS
        rank_info = RANKS.get(status, RANKS['user'])
        prefix = rank_info['label'].split()[-1]  # Берем только эмодзи из лейбла

        # Особая подсветка для тебя
        if uid == ARCHITECT_ID:
            text += f"🌚🔧 **Архитектор** — `{total} дБ`\n"
        else:
            # Выводим: 1. 🥈 Иван — 50000 дБ
            text += f"{i}. {prefix} {safe_name} — `{total} дБ`\n"

    text += "\n________________________________\n"
    text += "ℹ️ *Рейтинг един для всех чатов*"

    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("shop"))
async def shop(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['label']} ({v['price']} ⭐️)", callback_data=f"buy_{k}")]
        for k, v in RANKS.items() if v['price'] > 0
    ])
    await message.answer("🏪 **Магазин Скулежа**\nВыбери статус на 30 дней (защита от слива ранга):", reply_markup=kb)


@dp.callback_query(F.data.startswith("buy_"))
async def buy_call(call: types.CallbackQuery):
    rank_k = call.data.replace("buy_", "")
    await bot.send_invoice(call.message.chat.id, title=f"Статус {rank_k}", description="Привилегии на 30 дней",
                           payload=f"pay_{rank_k}", currency="XTR",
                           prices=[LabeledPrice(label="⭐️", amount=RANKS[rank_k]['price'])])


@dp.pre_checkout_query()
async def pre(q: PreCheckoutQuery): await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def success(m: Message):
    rk = m.successful_payment.invoice_payload.replace("pay_", "")
    exp = (datetime.now() + timedelta(days=30)).isoformat()
    conn = sqlite3.connect(DB_NAME);
    conn.execute('UPDATE users SET status=?, is_premium=1, vip_expire=? WHERE user_id=?', (rk, exp, m.from_user.id));
    conn.commit();
    conn.close()
    await m.answer(f"✨ ТЫ СРЕДИ АССОВ АСГАРДА! Ты теперь **{RANKS[rk]['label']}**! Скули с привелегиями!")



async def main(): init_db(); await dp.start_polling(bot)


if __name__ == "__main__": asyncio.run(main())

