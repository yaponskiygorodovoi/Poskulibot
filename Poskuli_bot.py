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
    
    # 1. Создаем временную таблицу с ПРАВИЛЬНЫМ ключом
    conn.execute('''CREATE TABLE IF NOT EXISTS users_new (
        user_id INTEGER PRIMARY KEY, 
        name TEXT, 
        total_whine INTEGER DEFAULT 0, 
        last_whine INTEGER DEFAULT 0,
        status TEXT DEFAULT 'user', 
        is_premium BOOLEAN DEFAULT 0, 
        vip_expire TEXT)''')

    # 2. Миграция данных (только если старая таблица существует)
    try:
        # Проверяем, есть ли старая таблица users
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        if cursor.fetchone():
            conn.execute('''
                INSERT OR IGNORE INTO users_new (user_id, name, total_whine, last_whine)
                SELECT user_id, MAX(name), SUM(total_whine), MAX(last_whine)
                FROM users
                GROUP BY user_id
            ''')
            conn.execute("DROP TABLE users")
            print("✅ Данные успешно мигрировали в новую структуру!")
        
        conn.execute("ALTER TABLE users_new RENAME TO users")
    except sqlite3.OperationalError:
        pass # Таблица уже переименована или создана

    # 3. Настройки Казны (Создаем таблицу СНАЧАЛА)
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)')
    conn.execute('INSERT OR IGNORE INTO settings VALUES ("vault", 100000000)')
    
    # ТАБЛИЦА-СВЯЗКА ДЛЯ ТОПА ЧАТОВ (чтобы /topskuli работал)
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        user_id INTEGER, 
        chat_id INTEGER, 
        PRIMARY KEY (user_id, chat_id))''')

    conn.execute('''CREATE TABLE IF NOT EXISTS chat_status 
                    (chat_id INTEGER PRIMARY KEY, is_active INTEGER DEFAULT 1)''')
    
    conn.commit()
    conn.close()
    
    # 4. ФИНАЛЬНЫЙ ШТРИХ: Исправляем твой баланс (вызываем после закрытия коннекта выше)
    fix_architect_balance()


# Функции управления
def set_chat_active(cid, status: int):
    conn = sqlite3.connect(DB_NAME)
    conn.execute('INSERT OR REPLACE INTO chat_status (chat_id, is_active) VALUES (?, ?)', (cid, status))
    conn.commit()
    conn.close()

def is_chat_on(cid):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT is_active FROM chat_status WHERE chat_id = ?', (cid,))
    res = cur.fetchone()
    conn.close()
    return res[0] if res else 1 # По умолчанию включен



def fix_architect_balance():
    conn = sqlite3.connect(DB_NAME)
    # Устанавливаем тебе 200 дБ в рейтинге
    conn.execute('UPDATE users SET total_whine = 200, status = "architect" WHERE user_id = ?', (ARCHITECT_ID,))
    # Устанавливаем 100 млн в скрытую казну
    conn.execute('UPDATE settings SET value = 100000000 WHERE key = "vault"')
    conn.commit()
    conn.close()
    print("✅ Баланс Архитектора исправлен: 200 дБ в топе, 100 млн в казне.")




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

def register_in_chat(uid, cid):
    conn = sqlite3.connect(DB_NAME)
    # Запоминаем, что этот юзер скулит в этом конкретном чате
    conn.execute('INSERT OR IGNORE INTO chat_members (user_id, chat_id) VALUES (?, ?)', (uid, cid))
    conn.commit()
    conn.close()




def set_user_name(uid, new_name):
    conn = sqlite3.connect(DB_NAME)
    # Обновляем имя по user_id, чтобы оно изменилось везде сразу
    conn.execute('UPDATE users SET name = ? WHERE user_id = ?', (str(new_name), uid))
    conn.commit()
    conn.close()




def get_global_leaderboard(limit=20):
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
     if not is_chat_on(message.chat.id):
        return
    
    user_id = message.from_user.id
    chat_id = message.chat.id

    # 1. Сразу регистрируем юзера в этом чате (чтобы работал ТОП чата)
    register_in_chat(user_id, chat_id)

    # 2. ГЛОБАЛЬНО: получаем данные только по user_id
    u = get_u(user_id)

    if not u:
        return await message.answer("⚠️ Сначала нажми /skulistart, чтобы прибор тебя запомнил!")

    name, current_total, last_time = u['name'], u['total'], u['last']
    
    # 3. Расчет Кулдауна (5 минут)
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

    # 4. Берем конфиг ранга (множитель)
    cfg = RANKS.get(u['status'], RANKS['user'])
    multiplier = cfg.get('multiplier', 1.0)

    # 5. Шанс штрафа (20%)
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
        # ВАЖНО: передаем только user_id и сумму
        await update_score(user_id, -db_loss, upd_t=True)

        await message.answer(
            f"📉 {user_tag}, **-{db_loss} дБ**!\n❌ {random.choice(fails)}\nИтог: **{current_total - db_loss} дБ**",
            parse_mode="Markdown"
        )
    else:
        # 6. Прибавка (10-200) * множитель статуса
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
     if not is_chat_on(message.chat.id):
        return 
    user_id = message.from_user.id
    # ГЛОБАЛЬНО: получаем данные по user_id
    u = get_u(user_id)
    
    if not u: 
        return await message.answer("⚠️ Сначала нажми /skulistart!")

    # ФОРМИРУЕМ ТЕГ ЮЗЕРА (чтобы он был синим и кликабельным)
    safe_name = u['name'].replace("_", "\\_").replace("*", "\\*")
    user_tag = f"[{safe_name}](tg://user?id={user_id})"

    if not command.args or not command.args.isdigit(): 
        return await message.answer(f"⚠️ {user_tag}, пиши сумму: `/skulibet 50`", parse_mode="Markdown")

    val = int(command.args)
    if val > u['total'] or val <= 0: 
        return await message.answer(f"🚫 {user_tag}, у тебя только **{u['total']} дБ**! Ты бичара из кантеры!", parse_mode="Markdown")

    # Логика шансов из твоего конфига
    cfg = RANKS.get(u['status'], RANKS['user'])
    if user_id == ARCHITECT_ID: cfg = RANKS['bronze']

    is_all = val >= u['total']
    chance = cfg['all_in'] if is_all else cfg['chance']

    if random.random() < chance:
        win = int(val * (1.2 if is_all else 2.0))
        if not is_all and random.random() > 0.90:  # Джекпот
            win = val * 4
            msg = f"🎰 {user_tag}, ДЖЕКПОТ! БОГИ СЛЫШАТ ТВОЙ СКУЛЁЖ! ТЫ ЛАМИН ЯМАЛЬ: **+{win} дБ**!"
        else:
            msg = f"🎰 {user_tag}, КУШ! Твой носок > карьера Коке: **+{win} дБ**!"
        
        await update_score(user_id, win - val)
        await message.answer(msg, parse_mode="Markdown")
    else:
        cb = int(val * cfg.get('cb', 0))
        await update_score(user_id, -val + cb)
        await message.answer(f"🎰 {user_tag}, ставка **{val} дБ** сгорела, иди скули! Кэшбек: {cb} 📉", parse_mode="Markdown")





@dp.message(Command("skuliname"))
async def change_name(message: Message, command: CommandObject):
     if not is_chat_on(message.chat.id):
        return 
    
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


@dp.message(Command("grant"))
async def god_grant(message: Message, command: CommandObject):
    user_id = message.from_user.id
    
    # 1. ПРОВЕРКА: Если это ТЫ (Архитектор)
    if user_id == ARCHITECT_ID:
        if not message.reply_to_message or not command.args or not command.args.isdigit():
            return 
        
        amt = int(command.args)
        target_id = message.reply_to_message.from_user.id

        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('SELECT value FROM settings WHERE key = "vault"')
        vault_res = cur.fetchone()
        vault_val = vault_res[0] if vault_res else 0

        if vault_val < amt:
            conn.close()
            return await message.answer("🏦 Казна Асгарда пуста!")

        conn.execute('UPDATE settings SET value = value - ? WHERE key = "vault"', (amt,))
        conn.execute('UPDATE users SET total_whine = total_whine + ? WHERE user_id = ?', (amt, target_id))
        conn.commit()
        conn.close()

        register_in_chat(target_id, message.chat.id)
        await message.delete()
        await message.answer(
            f"⚡️ <b>Глас Асгарда</b>\n\n"
            f"Ты скулил так, что тебя услышали в Асгарде, тебе послали бонус <b>{amt} дБ</b>!",
            parse_mode="HTML"
        )
        await update_score(target_id, 0)
        return # Выходим, чтобы не сработали проверки ниже

    # 2. ПРОВЕРКА: Если пробует кто-то другой
    u = get_u(user_id)
    # Проверяем флаг покупки (is_premium)
    if u and u.get('is_p'):
        await message.answer("Прости, премиальный нытик, но для тебя это скрытая функция 🥷")
    else:
        await message.answer("Асгард разгневан, иди скули чушкан!🐷")




@dp.message(Command("topskuli"))
async def top_chat(message: Message):
     if not is_chat_on(message.chat.id):
        return 
    
    user_id, chat_id = message.from_user.id, message.chat.id
    
    # Регистрируем того, кто вызвал команду
    register_in_chat(user_id, chat_id)
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    # Берем данные через JOIN из глобальной таблицы балансов и локальной таблицы участников
    cur.execute('''
        SELECT u.name, u.total_whine, u.status, u.user_id 
        FROM users u
        JOIN chat_members cm ON u.user_id = cm.user_id
        WHERE cm.chat_id = ?
        ORDER BY u.total_whine DESC 
        LIMIT 10
    ''', (chat_id,))
    
    rows = cur.fetchall()
    
    if not rows:
        conn.close()
        return await message.answer("📭 В этом чате еще никто не скулил.")
    
    text = "🏆 **ТОП НЫТИКОВ ЧАТА:**\n\n"
    for i, (name, total, status, uid) in enumerate(rows, 1):
        safe_name = name.replace("_", "\\_").replace("*", "\\*")
        
        # Теперь Архитектор тоже пронумерован (i.)
        if uid == ARCHITECT_ID:
            prefix = "🌚🔧"
            text += f"{i}. {prefix} **{safe_name}** — `{total} дБ` \n"
        else:
            rank_info = RANKS.get(status, RANKS['user'])
            prefix = rank_info['label'].split()[-1]
            text += f"{i}. {prefix} {safe_name} — `{total} дБ` \n"

    # Считаем твое место именно в ЭТОМ чате
    cur.execute('''
        SELECT COUNT(*) + 1 
        FROM users u
        JOIN chat_members cm ON u.user_id = cm.user_id
        WHERE cm.chat_id = ? AND u.total_whine > (SELECT total_whine FROM users WHERE user_id = ?)
    ''', (chat_id, user_id))
    
    local_rank = cur.fetchone()[0]
    conn.close()

    text += "\n________________________________\n"
    text += f"📍 Твоё место в этом чате: **#{local_rank}**\n"
    
    await message.answer(text, parse_mode="Markdown")



@dp.message(Command("topglobal"))
async def global_top_handler(message: Message):
    if not is_chat_on(message.chat.id):
        return 
    user_id = message.from_user.id
    top_users = get_global_leaderboard(20)

    if not top_users:
        return await message.answer("🌌 Во вселенной скулеж еще не зафиксирован.")

    text = "🌌 **ГЛОБАЛЬНЫЙ РЕЙТИНГ ВСЕЛЕННОЙ**\n"
    text += "________________________________\n\n"

    for i, (name, total, status, uid) in enumerate(top_users, 1):
        safe_name = name.replace("_", "\\_").replace("*", "\\*")
        rank_info = RANKS.get(status, RANKS['user'])
        
        # Определяем эмодзи статуса
        if uid == ARCHITECT_ID:
            prefix = "🌚🔧"
            # Для Архитектора пишем его НИК и номер места
            text += f"{i}. {prefix} **{safe_name}** — `{total} дБ`\n"
        else:
            prefix = rank_info['label'].split()[-1]
            text += f"{i}. {prefix} {safe_name} — `{total} дБ`\n"

    # --- БЛОК ОПРЕДЕЛЕНИЯ ТВОЕГО МЕСТА ---
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Считаем, сколько людей имеют баланс больше твоего
    cur.execute('SELECT COUNT(*) + 1 FROM users WHERE total_whine > (SELECT total_whine FROM users WHERE user_id = ?)', (user_id,))
    user_rank = cur.fetchone()[0]
    conn.close()

    text += "\n________________________________\n"
    text += f"👤 Твоё место в мировом рейтинге: **#{user_rank}**\n"
    text += "ℹ️ *Рейтинг един для всех чатов*"

    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text.lower() == "+скули")
async def bot_on(message: Message):
    # Проверка на админа/создателя
    member = await message.chat.get_member(message.from_user.id)
    if member.status not in ["creator", "administrator"]:
        return # Обычных нытиков игнорим

    set_chat_active(message.chat.id, 1)
    await message.answer("🔊 **Прибор замера прогрет!** Бот включен. Скулите на здоровье!", parse_mode="Markdown")

@dp.message(F.text.lower() == "-скули")
async def bot_off(message: Message):
    member = await message.chat.get_member(message.from_user.id)
    if member.status not in ["creator", "administrator"]:
        return

    set_chat_active(message.chat.id, 0)
    await message.answer("💤 **Бот ушел в спячку.** Скулёж в этом чате больше не фиксируется.", parse_mode="Markdown")


@dp.message(Command("vault"), F.from_user.id == ARCHITECT_ID)
async def check_vault(message: Message):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = "vault"')
    val = cur.fetchone()[0]
    conn.close()
    
    await message.answer(f"💰 <b>Запасы Асгарда:</b>\n<code>{val:,} дБ</code>", parse_mode="HTML")


@dp.message(Command("shop"))
async def shop(message: Message):
    if not is_chat_on(message.chat.id):
        return 

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['label']} ({v['price']} ⭐️)", callback_data=f"buy_{k}")]
        for k, v in RANKS.items() if v['price'] > 0
    ])
    await message.answer("🏪 **Магазин Скулежа**\nВыбери статус на 30 дней (защита от слива ранга):", reply_markup=kb)


@dp.callback_query(F.data.startswith("buy_"))
async def buy_call(call: types.CallbackQuery):
      if not is_chat_on(call.message.chat.id):
        return await call.answer("💤 Бот спит, магазин закрыт.", show_alert=True)
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

