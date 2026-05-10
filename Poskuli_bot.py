import random, sqlite3, asyncio, time, os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, InlineKeyboardButton, InlineKeyboardMarkup, \
    ChatPermissions

# --- НАСТРОЙКИ ---
active_duels = {}  # Храним текущие бои
DB_NAME = "/data/whine_bot.db"
if not os.path.exists("/data"):
    DB_NAME = "whine_bot.db"

# --- НАСТРОЙКИ БОТА ---
TOKEN = os.getenv('BOT_TOKEN')

bot = Bot(token=TOKEN)
dp = Dispatcher()

ARCHITECT_ID = 6421600902  # !!! ПОСТАВЬ СВОЙ ID !!!
COOLDOWN_MINUTES = 4

# Конфиг уровней
RANKS = {
    # Новый топ-ранг. Шансы намеренно ниже: на вершине Асгарда казино уже не кормит халявой.
    "olympian": {"thresh": 1000000000, "price": 0, "label": "Олимпиец 🏛️", "chance": 0.38, "all_in": 0.65,
                 "cb": 0.10, "multiplier": 3.0},
    "omnipotent": {"thresh": 1500000, "price": 1500, "label": "Всемогущий 🌌", "chance": 0.45, "all_in": 0.76,
                   "cb": 0.18, "multiplier": 2.5},
    "diamond": {"thresh": 500000, "price": 1000, "label": "Бог 💎", "chance": 0.44, "all_in": 0.75, "cb": 0.15,
                "multiplier": 2.0},
    "gold": {"thresh": 100000, "price": 500, "label": "Ангел 👑", "chance": 0.43, "all_in": 0.74, "cb": 0.12,
             "multiplier": 1.5},
    "silver": {"thresh": 30000, "price": 150, "label": "МС 🌠", "chance": 0.42, "all_in": 0.73, "cb": 0.08,
               "multiplier": 1.2},
    "bronze": {"thresh": 10000, "price": 50, "label": "КМС 🚀", "chance": 0.41, "all_in": 0.72, "cb": 0.04,
               "multiplier": 1.1},
    "user": {"thresh": 0, "price": 0, "label": "Новичок 👤", "chance": 0.40, "all_in": 0.70, "cb": 0.00,
             "multiplier": 1.0}
}


# --- БД (ИНТЕГРАЦИЯ НОВЫХ ПОЛЕЙ) ---
def init_db():
    conn = sqlite3.connect(DB_NAME)

    # 1. Создаем временную таблицу с глобальным ключом и дуэльной статой
    conn.execute('''CREATE TABLE IF NOT EXISTS users_new (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        total_whine INTEGER DEFAULT 0,
        last_whine INTEGER DEFAULT 0,
        status TEXT DEFAULT 'user',
        is_premium BOOLEAN DEFAULT 0,
        vip_expire TEXT,
        duel_wins INTEGER DEFAULT 0,
        duel_losses INTEGER DEFAULT 0
    )''')

    # 2. Миграция данных (если старая таблица существует)
    try:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        if cursor.fetchone():
            conn.execute('''
                INSERT OR IGNORE INTO users_new (user_id, name, total_whine, last_whine)
                SELECT user_id, MAX(name), SUM(total_whine), MAX(last_whine)
                FROM users
                GROUP BY user_id
            ''')
            conn.execute("DROP TABLE users")
            print("✅ Данные мигрировали!")

        conn.execute("ALTER TABLE users_new RENAME TO users")
    except sqlite3.OperationalError:
        pass

        # 3. Создание остальных таблиц (Исправлены отступы!)
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)')
    conn.execute('INSERT OR IGNORE INTO settings VALUES ("vault", 1000000000)')

    conn.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        user_id INTEGER, chat_id INTEGER, PRIMARY KEY (user_id, chat_id))''')

    conn.execute('''CREATE TABLE IF NOT EXISTS chat_status (
        chat_id INTEGER PRIMARY KEY, is_active INTEGER DEFAULT 1)''')

    # ИСПРАВЛЕНИЕ: Лечим базу от NULL, чтобы стата дуэлей начала считаться (+1)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN duel_wins INTEGER DEFAULT 0")
    except:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN duel_losses INTEGER DEFAULT 0")
    except:
        pass

    try:
        conn.execute("UPDATE users SET duel_wins = 0 WHERE duel_wins IS NULL")
        conn.execute("UPDATE users SET duel_losses = 0 WHERE duel_losses IS NULL")
    except:
        pass

    conn.commit()
    conn.close()

    # 4. ФИНАЛЬНЫЙ ШТРИХ: Твой баланс и казна
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
    return res[0] if res else 1  # По умолчанию включен


def fix_architect_balance():
    conn = sqlite3.connect(DB_NAME)
    # Устанавливаем тебе 200 дБ в рейтинге
    conn.execute('UPDATE users SET total_whine = 200, status = "architect" WHERE user_id = ?', (ARCHITECT_ID,))
    # Устанавливаем 100 млн в скрытую казну
    conn.execute('UPDATE settings SET value = 1000000000 WHERE key = "vault"')
    conn.commit()
    conn.close()
    print("✅ Баланс Архитектора исправлен: 200 дБ в топе, 1 млрд в казне.")


def get_u(uid):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Добавили в SELECT duel_wins и duel_losses
    cur.execute(
        'SELECT name, total_whine, last_whine, status, is_premium, vip_expire, duel_wins, duel_losses FROM users WHERE user_id = ?',
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
        "exp": r[5],
        "wins": r[6],  # Новое поле
        "losses": r[7]  # Новое поле
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
    # Добавили выборку побед и поражений (индексы 4 и 5)
    cur.execute('''
        SELECT name, total_whine, status, user_id, duel_wins, duel_losses
        FROM users
        WHERE total_whine > 0
        ORDER BY total_whine DESC LIMIT ?
    ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_duel_rank(wins, losses):
    total = wins + losses
    # Пока нет 3 боев, ранг не даем, чтобы не было "Богов" со счетом 1-0
    if total < 3:
        return "Новичок 🐣"

    win_rate = (wins / total) * 100

    if win_rate >= 80: return "БОГ ДУЭЛЕЙ 🌌⚡️"
    if win_rate >= 75: return "Серийный убийца 💀"
    if win_rate >= 60: return "Стрелок 🔫"
    if win_rate < 50:  return "Салага 🐥"

    return "Боец 🥊"


def html_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def html_tag(user: types.User) -> str:
    name = html_escape(user.first_name or user.username or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def md_escape(text: str) -> str:
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


def parse_plus_args(message: Message, command_name: str) -> str:
    text = message.text or ""
    return text[len(command_name):].strip()


def parse_positive_int(raw: str):
    raw = (raw or "").strip().split()[0] if raw and raw.strip() else ""
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def parse_mute_seconds(raw: str):
    raw = (raw or "").strip().lower().split()[0] if raw and raw.strip() else ""
    if not raw:
        return None

    multipliers = {
        "с": 1, "сек": 1, "s": 1,
        "м": 60, "мин": 60, "m": 60,
        "ч": 3600, "час": 3600, "h": 3600,
        "д": 86400, "дн": 86400, "d": 86400,
    }

    digits = ""
    suffix = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        else:
            suffix += ch

    if not digits:
        return None

    amount = int(digits)
    multiplier = multipliers.get(suffix, 60)  # просто `+мут 10` = 10 минут
    seconds = amount * multiplier
    return seconds if seconds > 0 else None


def ensure_user(user: types.User, chat_id: int):
    st = "architect" if user.id == ARCHITECT_ID else "user"
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        'INSERT OR IGNORE INTO users (user_id, name, status) VALUES (?, ?, ?)',
        (user.id, user.first_name, st)
    )
    conn.commit()
    conn.close()
    register_in_chat(user.id, chat_id)


def get_vault():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = "vault"')
    res = cur.fetchone()
    conn.close()
    return res[0] if res else 0


def change_vault(delta: int):
    conn = sqlite3.connect(DB_NAME)
    conn.execute('UPDATE settings SET value = value + ? WHERE key = "vault"', (delta,))
    conn.commit()
    conn.close()


async def is_chat_admin(chat: types.Chat, user_id: int) -> bool:
    try:
        member = await chat.get_member(user_id)
        return member.status in ["creator", "administrator"]
    except Exception:
        return False


async def can_architect_or_olympian_mute(message: Message, seconds: int):
    if message.from_user.id == ARCHITECT_ID:
        return True, "architect"

    u = get_u(message.from_user.id)
    if u and u.get("status") == "olympian":
        if seconds > 10 * 60:
            return False, "🏛️ Олимпиец может мутить максимум на 10 минут."
        return True, "olympian"

    return False, "🚫 Команда доступна только Архитектору или Олимпийцу."


async def play_casino(message: Message, arg_text: str, usage: str):
    if not is_chat_on(message.chat.id):
        return

    user_id = message.from_user.id
    ensure_user(message.from_user, message.chat.id)
    u = get_u(user_id)

    safe_name = md_escape(u['name'])
    user_tag = f"[{safe_name}](tg://user?id={user_id})"

    val = parse_positive_int(arg_text)
    if val is None:
        return await message.answer(f"⚠️ {user_tag}, пиши сумму: `{usage}`", parse_mode="Markdown")

    if val > u['total'] or val <= 0:
        return await message.answer(
            f"🚫 {user_tag}, у тебя только **{u['total']} дБ**! Ты нищееб никчемный, к сожалению!",
            parse_mode="Markdown")

    cfg = RANKS.get(u['status'], RANKS['user'])
    if user_id == ARCHITECT_ID:
        cfg = RANKS['bronze']

    is_all = val >= u['total']
    chance = cfg['all_in'] if is_all else cfg['chance']

    if random.random() < chance:
        win = int(val * (1.2 if is_all else 2.0))
        if not is_all and random.random() > 0.93:  # Джекпот стал реже
            win = val * 4
            msg = f"🎰 {user_tag}, ДЖЕКПОТ! БОГИ СЛЫШАТ ТВОЙ СКУЛЁЖ! ТЫ УСМАН ДЕМБЕЛЕ: **+{win} дБ**!"
        else:
            msg = f"🎰 {user_tag}, КУШ! Как же ты ебешь!, Боже: **+{win} дБ**!"

        await update_score(user_id, win - val)
        await message.answer(msg, parse_mode="Markdown")
    else:
        cb = int(val * cfg.get('cb', 0))
        await update_score(user_id, -val + cb)
        await message.answer(f"🎰 {user_tag}, ставка **{val} дБ** сгорела, иди скули, пёс! Кэшбек: {cb} 📉",
                             parse_mode="Markdown")


# --- КОМАНДЫ ---
@dp.message(Command("skulistart"))
async def start(message: Message):
    # Регистрация теперь глобальная (пользователь один во всех чатах)
    conn = sqlite3.connect(DB_NAME)
    st = "architect" if message.from_user.id == ARCHITECT_ID else "user"

    # ПРАВКА: Убрали chat_id из запроса к таблице users
    conn.execute('INSERT OR IGNORE INTO users (user_id, name, status) VALUES (?, ?, ?)',
                 (message.from_user.id, message.from_user.first_name, st))
    conn.commit()
    conn.close()

    # Сразу регистрируем его присутствие в ЭТОМ чате для локального топа
    register_in_chat(message.from_user.id, message.chat.id)

    await message.answer("✅ Регистрация успешна! Твой баланс теперь един во всех чатах. Юзай /poskuli")


@dp.message(Command("poskuli"))
async def measure_whine(message: Message):
    # ПРОВЕРКА: Включен ли бот
    if not is_chat_on(message.chat.id):
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    # 1. Регистрируем юзера в этом чате (чтобы работал ТОП чата)
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
            f"🚫⛔ {user_tag}, связки не восстановились!\nОбожди еще **{minutes}м {seconds}с**,маленький",
            parse_mode="Markdown"
        )

    # 4. Берем конфиг ранга (множитель)
    cfg = RANKS.get(u['status'], RANKS['user'])
    multiplier = cfg.get('multiplier', 1.0)

    # 5. Шанс штрафа (20%)
    if random.random() < 0.20:
        fails = [
            "Прибор определил это как полная хуета. 🥺 К сожалению, штраф!🫵🤡",
            "Поскулил как уебок минус вайб👺👎",
            "Это был не скулёж, а зевок. Учись скулить у первых скулюнов!💩☠️👀",
            "Ты начал ныть, но, к сожалению, подавился слюной! Нахуй иди👺",
            "Паскудный скулеж, как будто ты не поскулить решил, а пососать — штраф дебилу!🫵🤡",
            "Ты фанат Реала?🤡 Что за пронзительный скулёж на судей? Не одобрено!🤡",
            "Хави смеётся над тем, как ты слабо скулишь! Пробуй снова!👀",
            "Какая же хуетень, чувак, угараем всей командой разработчиков! 💩👀",
            "Как же ты срёшь на ляшки, чел 🤡",
            "К сожалению, ты обосрался 🤡",
            "Доволен собой? Но это хуйня, к сожалению!🫵🤡",
            "Что это за дрисня?🤡 Иди поплачь!🤡",
            "Так скулят только хуесосы! ШТРАФ!🫵🤡"
        ]
        db_loss = random.randint(1, 5)
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

        # Варианты реакций по уровням результата
        if db_gain < 40:
            mood_options = [
                "🤫 Тихое поскуливание",
                "🦴 Тихушник...",
                "😶 Почти не слышно, это шёпот?!",
                "🧕 Будешь так своей крале на ушко шептать"
            ]
        elif db_gain > 100:
            mood_options = [
                "📢 Скулишь пиздец! Ты Винисиус??",
                "🚨 Уши закладывает! Аккуратнее немного...",
                "🐺 Воешь как Флик после поражения Барсы!",
                "🦜 Скулёж дичайший! Как от фанатов Никогдарсенала",
                "🤡 Заскулил как Чмани! ",
                "🤡 Ебать! Вой как от некого Зураба!"
            ]
        else:
            mood_options = [
                "🫨 Средний вой",
                "😐 Умеренный скулёж, ничего особенного",
                "🕯️ Звучит стабильно, как скучная игра Арсенала",
                "🐔 Не говно, но и не топ — ты Тоттенхэм!",

            ]

        mood = random.choice(mood_options)
        bonus_text = f" (Бонус ранга x{multiplier})" if multiplier > 1.0 else ""

        reply_variants = [
            f"📈 {user_tag}, замер: **{db_gain} дБ**{bonus_text}\nℹ️ Статус: {mood}\nВсего накоплено: **{current_total + db_gain} дБ**",
            f"🧭 {user_tag}, твоё новое значение — **{db_gain} дБ**{bonus_text}\n{mood}\n🔊 Текущий итог: **{current_total + db_gain} дБ**",
            f"🎚️ {user_tag}, измерение показало **{db_gain} дБ**{bonus_text}\n🎧 {mood}\nСуммарно: **{current_total + db_gain} дБ**"
        ]

        await message.answer(random.choice(reply_variants), parse_mode="Markdown")


# После замера ранг проверяется автоматически внутри update_score

@dp.message(Command("skulibet"))
async def bet(message: Message, command: CommandObject):
    await play_casino(message, command.args or "", "/skulibet 50")


@dp.message(F.text.regexp(r"^\+казик(\s|$)"))
async def plus_casino(message: Message):
    await play_casino(message, parse_plus_args(message, "+казик"), "+казик 50")


@dp.message(F.text.lower() == "+поскулить")
async def plus_measure_whine(message: Message):
    await measure_whine(message)


@dp.message(F.text.regexp(r"^\+перевод(\s|$)"))
async def transfer_db(message: Message):
    if not is_chat_on(message.chat.id):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.answer("⚠️ Перевод делается ответом на сообщение юзера: `+перевод 100`",
                                    parse_mode="Markdown")

    sender = message.from_user
    target = message.reply_to_message.from_user

    if target.is_bot:
        return await message.answer("🤖 Ботам дБ не переводим, они и так бездушные.")

    if sender.id == target.id:
        return await message.answer("🤡 Сам себе перевод? Это уже финансовая шизофрения Асгарда.")

    amount = parse_positive_int(parse_plus_args(message, "+перевод"))
    if amount is None:
        return await message.answer("⚠️ Формат: `+перевод 100` в ответ на сообщение получателя.", parse_mode="Markdown")

    ensure_user(sender, message.chat.id)
    ensure_user(target, message.chat.id)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT total_whine FROM users WHERE user_id = ?', (sender.id,))
    sender_balance = cur.fetchone()[0]

    if sender_balance < amount:
        conn.close()
        return await message.answer(f"🚫 У тебя только <b>{sender_balance} дБ</b>. Не вывез перевод, скули дальше.",
                                    parse_mode="HTML")

    cur.execute('UPDATE users SET total_whine = total_whine - ? WHERE user_id = ?', (amount, sender.id))
    cur.execute('UPDATE users SET total_whine = total_whine + ? WHERE user_id = ?', (amount, target.id))
    conn.commit()
    conn.close()

    await update_score(sender.id, 0)
    await update_score(target.id, 0)

    await message.answer(
        f"💸 {html_tag(target)}, тебе перевод от {html_tag(sender)}!\n"
        f"<b>{amount} дБ</b> прилетело в карман. Скули на здоровье! 🐺🔊🥂",
        parse_mode="HTML"
    )


@dp.message(F.text.regexp(r"^\+списать(\s|$)"))
async def architect_take_db(message: Message):
    if message.from_user.id != ARCHITECT_ID:
        return await message.answer("🚫 Казначейский нож только у Архитектора.")

    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.answer("⚠️ Формат: `+списать 100` ответом на сообщение жертвы.", parse_mode="Markdown")

    amount = parse_positive_int(parse_plus_args(message, "+списать"))
    if amount is None:
        return await message.answer("⚠️ Формат: `+списать 100`", parse_mode="Markdown")

    target = message.reply_to_message.from_user
    ensure_user(target, message.chat.id)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT total_whine FROM users WHERE user_id = ?', (target.id,))
    balance = cur.fetchone()[0]
    take = min(amount, balance)
    cur.execute('UPDATE users SET total_whine = total_whine - ? WHERE user_id = ?', (take, target.id))
    cur.execute('UPDATE settings SET value = value + ? WHERE key = "vault"', (take,))
    conn.commit()
    conn.close()

    await update_score(target.id, 0)

    await message.answer(
        f"🧾 <b>Аудит Асгарда</b>\n"
        f"С {html_tag(target)} списано <b>{take} дБ</b>. Казна довольно урчит. 🏦",
        parse_mode="HTML"
    )


@dp.message(F.text.regexp(r"^\+мут(\s|$)"))
async def mute_user(message: Message):
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.answer("⚠️ Мут делается ответом на сообщение: `+мут 10м` или `+мут 1ч`",
                                    parse_mode="Markdown")

    seconds = parse_mute_seconds(parse_plus_args(message, "+мут"))
    if seconds is None:
        return await message.answer("⚠️ Формат: `+мут 10м`, `+мут 1ч`, `+мут 2д`. Без суффикса — минуты.",
                                    parse_mode="Markdown")

    ok, role_or_msg = await can_architect_or_olympian_mute(message, seconds)
    if not ok:
        return await message.answer(role_or_msg)

    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        return await message.answer("🤡 Самомут? Ты ебанат что ли блядь?!")

    if await is_chat_admin(message.chat, target.id):
        return await message.answer("🛡️ Админов/создателя чата мутить нельзя. Ебанулся что ли долбоеб?!")

    until_date = datetime.now() + timedelta(seconds=seconds)
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
    except Exception as e:
        return await message.answer(
            f"⚠️ Не смог замутить. Проверь, что бот админ с правом ограничивать участников. Ошибка: <code>{html_escape(e)}</code>",
            parse_mode="HTML")

    mins = max(1, seconds // 60)
    await message.answer(
        f"🔇 {html_tag(target)} хуесос отправлен под шконарь на <b>{mins} мин.</b>\n"
        f"Исполнитель: {html_tag(message.from_user)}",
        parse_mode="HTML"
    )


@dp.message(F.text.lower() == "+бан")
async def ban_user(message: Message):
    if message.from_user.id != ARCHITECT_ID:
        return await message.answer("🚫 Банхаммер хранится только у Архитектора.")

    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.answer("⚠️ Бан делается ответом на сообщение юзера: `+бан`", parse_mode="Markdown")

    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        return await message.answer("🤡 Самобан — сильно,почти как самодрочь, но нет.")

    if await is_chat_admin(message.chat, target.id):
        return await message.answer("🛡️ Админов/создателя чата банить нельзя, ты охуел?!")

    try:
        await bot.ban_chat_member(message.chat.id, target.id)
    except Exception as e:
        return await message.answer(f"⚠️ Не смог забанить. Проверь права бота. Ошибка: <code>{html_escape(e)}</code>",
                                    parse_mode="HTML")

    await message.answer(
        f"🔨 {html_tag(target)} улетел из чата. Архитектор стукнул по ебалу хуебеса. 🌚",
        parse_mode="HTML"
    )


@dp.message(Command("skuliname"))
async def change_name(message: Message, command: CommandObject):
    if not is_chat_on(message.chat.id):
        return

    # Берем текст после команды
    new_name = command.args

    if not new_name:
        return await message.answer("⚠️ Введи новое имя после команды: `/skuliname Сын Коке`",
                                    parse_mode="Markdown")

    # Ограничим длину ника
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
        target = message.reply_to_message.from_user
        target_id = target.id

        # ОПРЕДЕЛЯЕМ ТЕГ (чтобы не было NameError)
        t_name = target.first_name.replace("<", "&lt;").replace(">", "&gt;")
        target_tag = f'<a href="tg://user?id={target_id}">{t_name}</a>'

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

        # Сначала удаляем команду, потом шлем ответ
        await message.delete()

        # ИСПРАВЛЕНО: Один await и корректный текст
        await message.answer(
            f"⚡️ <b>Глас Олимпа</b>\n\n"
            f"{target_tag}, ты скулил так, что тебя услышали на Олимпе! "
            f"Тебе послали бонус и леща <b>{amt} дБ</b>!",
            parse_mode="HTML"
        )

        await update_score(target_id, 0)
        return

    # 2. ПРОВЕРКА: Если пробует кто-то другой (остается как было)
    u = get_u(user_id)
    if u and u.get('is_p'):
        await message.answer("Прости, премиальный нытик, но для тебя это скрытая функция 🥷")
    else:
        await message.answer("Олимп разгневан, иди скули чушкан недоебаный!🐷")


@dp.message(Command("topskuli"))
async def top_chat(message: Message):
    if not is_chat_on(message.chat.id):
        return

    user_id, chat_id = message.from_user.id, message.chat.id
    register_in_chat(user_id, chat_id)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # Добавили u.duel_wins и u.duel_losses в запрос
    cur.execute('''
        SELECT u.name, u.total_whine, u.status, u.user_id, u.duel_wins, u.duel_losses
        FROM users u
        JOIN chat_members cm ON u.user_id = cm.user_id
        WHERE cm.chat_id = ?
        ORDER BY u.total_whine DESC LIMIT 10
    ''', (chat_id,))

    rows = cur.fetchall()

    if not rows:
        conn.close()
        return await message.answer("📭 В этом чате еще никто не скулил.")

    text = "🏆 **ТОП НЫТИКОВ ЧАТА:**\n\n"
    for i, (name, total, status, uid, wins, losses) in enumerate(rows, 1):
        safe_name = name.replace("_", "\\_").replace("*", "\\*")

        # Считаем дуэльный ранг для каждого
        d_rank = get_duel_rank(wins, losses)

        # Определяем префикс ранга по дБ
        if uid == ARCHITECT_ID:
            prefix = "🌚🔧"
        else:
            rank_info = RANKS.get(status, RANKS['user'])
            prefix = rank_info['label'].split()[-1]

        # Итоговая строка: Номер. Иконка Имя — баланс | Дуэльный ранг (W/L)
        text += f"{i}. {prefix} {safe_name} — `{total} дБ` | {d_rank} ({wins}W/{losses}L)\n"

    # Считаем твое место
    cur.execute('''
        SELECT COUNT(*) + 1
        FROM users u
        JOIN chat_members cm ON u.user_id = cm.user_id
        WHERE cm.chat_id = ?
          AND u.total_whine > (SELECT total_whine FROM users WHERE user_id = ?)
    ''', (chat_id, user_id))

    res = cur.fetchone()
    local_rank = res[0] if res else "?"
    conn.close()

    text += "\n________________________________\n"
    text += f"📍 Твоё место в этом чате: **#{local_rank}**\n"

    await message.answer(text, parse_mode="Markdown")


# Считаем твое место именно в ЭТОМ чате
@dp.message(Command("topglobal"))
async def global_top_handler(message: Message):
    # Проверка включен ли бот
    if not is_chat_on(message.chat.id):
        return

    user_id = message.from_user.id
    # Функция get_global_leaderboard(20) теперь должна возвращать wins и losses (индексы 4 и 5)
    top_users = get_global_leaderboard(20)

    if not top_users:
        return await message.answer("🌌 Во вселенной скулеж еще не зафиксирован.")

    text = "🌌 **ГЛОБАЛЬНЫЙ РЕЙТИНГ ВСЕЛЕННОЙ**\n"
    text += "________________________________\n\n"

    for i, (name, total, status, uid, wins, losses) in enumerate(top_users, 1):
        safe_name = name.replace("_", "\\_").replace("*", "\\*")
        rank_info = RANKS.get(status, RANKS['user'])

        # Получаем ранг дуэлянта (Салага, Стрелок, Бог и т.д.)
        d_rank = get_duel_rank(wins, losses)

        # Префикс (Архитектор или ранг по дБ)
        prefix = "🌚🔧" if uid == ARCHITECT_ID else rank_info['label'].split()[-1]

        # Итоговая строка с балансом и боксерской статой
        text += f"{i}. {prefix} {safe_name} — `{total} дБ` | {d_rank} ({wins}W/{losses}L)\n"

    # --- БЛОК ОПРЕДЕЛЕНИЯ ТВОЕГО МЕСТА ---
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute('SELECT total_whine FROM users WHERE user_id = ?', (user_id,))
    u_exists = cur.fetchone()

    if u_exists:
        cur.execute('SELECT COUNT(*) + 1 FROM users WHERE total_whine > ?', (u_exists[0],))
        user_rank = cur.fetchone()[0]
        rank_text = f"**#{user_rank}**"
    else:
        rank_text = "❔ (не в базе)"

    conn.close()

    text += "\n________________________________\n"
    text += f"👤 Твоё место в мировом рейтинге: {rank_text}\n"
    text += "ℹ️ *Рейтинг един для всех чатов*"

    await message.answer(text, parse_mode="Markdown")


@dp.message(F.text.lower() == "+скули")
async def bot_on(message: Message):
    member = await message.chat.get_member(message.from_user.id)

    # ПРОВЕРКА: Если это Админ или Создатель
    if member.status in ["creator", "administrator"]:
        set_chat_active(message.chat.id, 1)
        await message.answer("🔊 **Прибор замера прогрет!** Бот включен. Скулите на здоровье!", parse_mode="Markdown")
    else:
        # Если пишет обычный смертный
        t_name = message.from_user.first_name.replace("<", "&lt;").replace(">", "&gt;")
        t_tag = f'<a href="tg://user?id={message.from_user.id}">{t_name}</a>'
        await message.answer(f"{t_tag}, админом себя почухал?! Чеши в стойло, чушканидзе! 🐽", parse_mode="HTML")


@dp.message(F.text.lower() == "-скули")
async def bot_off(message: Message):
    member = await message.chat.get_member(message.from_user.id)

    if member.status in ["creator", "administrator"]:
        set_chat_active(message.chat.id, 0)
        await message.answer("💤 **Бот ушел в спячку.** Скулёж в этом чате больше не фиксируется.",
                             parse_mode="Markdown")
    else:
        # Если пишет обычный смертный
        t_name = message.from_user.first_name.replace("<", "&lt;").replace(">", "&gt;")
        t_tag = f'<a href="tg://user?id={message.from_user.id}">{t_name}</a>'
        await message.answer(f"{t_tag}, админом себя почухал?! Чеши в стойло, чушканидзе! 🐽", parse_mode="HTML")


@dp.message(Command("vault"), F.from_user.id == ARCHITECT_ID)
async def check_vault(message: Message):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = "vault"')
    val = cur.fetchone()[0]
    conn.close()

    await message.answer(f"💰 <b>Запасы Асгарда:</b>\n<code>{val:,} дБ</code>", parse_mode="HTML")


@dp.message(F.text.lower() == "+дуэль")
async def duel_request(message: Message):
    if not is_chat_on(message.chat.id): return
    if not message.reply_to_message:
        return await message.answer("⚠️ Чтобы вызвать на дуэль, ответь на сообщение противника текстом `+дуэль`!")

    p1, p2 = message.from_user, message.reply_to_message.from_user
    if p1.id == p2.id: return await message.answer("Самострел? Не в мою смену. 🤡")

    u1, u2 = get_u(p1.id), get_u(p2.id)
    if not u1 or not u2: return await message.answer("Оба нытика должны быть в базе (/skulistart)")
    if u1['total'] <= 0 or u2['total'] <= 0: return await message.answer(
        "У нищих дуэлей не бывает. Наскулите хоть что-то. 💸")

    duel_id = f"{p1.id}_{p2.id}"
    active_duels[duel_id] = {
        "p1_id": p1.id, "p1_name": p1.first_name,
        "p2_id": p2.id, "p2_name": p2.first_name,
        "bank": u1['total'] + u2['total'],
        "turn": p1.id, "status": "pending"
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ ПРИНЯТЬ (ВА-БАНК)", callback_data=f"d_acc_{duel_id}"),
        InlineKeyboardButton(text="🏳️ СЛИТЬСЯ КАК ЧУШКАН", callback_data=f"d_dec_{duel_id}")
    ]])

    await message.answer(
        f"⚔️ <b>ДУЭЛЬ НА ВЫЖИВАНИЕ!</b>\n\n"
        f"👤 {u1['name']} сгорел и вызывает {u2['name']}!\n"
        f"💰 <b>На кону всё:</b> {active_duels[duel_id]['bank']} дБ\n\n"
        f"<i>Проигравший обнуляется. Принимаешь или ссыкло?</i>",
        reply_markup=kb, parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("d_acc_"))
async def d_accept(call: types.CallbackQuery):
    d_id = call.data.replace("d_acc_", "")
    duel = active_duels.get(d_id)
    if not duel or call.from_user.id != duel['p2_id']:
        return await call.answer("Это не твой вызов! 👺", show_alert=True)

    duel['status'] = "fighting"
    await shoot_round(call.message, d_id)


async def shoot_round(msg, d_id):
    duel = active_duels[d_id]
    turn_name = duel['p1_name'] if duel['turn'] == duel['p1_id'] else duel['p2_name']
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💥 ВЫСТРЕЛ!", callback_data=f"d_shot_{d_id}")
    ]])
    await msg.edit_text(
        f"🔫 <b>ОЧЕРЕДЬ СТРЕЛЯТЬ:</b> {turn_name}\n"
        f"💰 Банк: {duel['bank']} дБ\n\n"
        f"<i>Кто же отправится бомжевать первым?...</i>",
        reply_markup=kb, parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("d_shot_"))
async def d_shoot(call: types.CallbackQuery):
    d_id = call.data.replace("d_shot_", "")
    duel = active_duels.get(d_id)

    if not duel or call.from_user.id != duel['turn']:
        return await call.answer("Сейчас не твой ход! ⏳", show_alert=True)

    if random.random() < 0.35:  # Шанс попадания
        winner_id = duel['turn']
        loser_id = duel['p2_id'] if winner_id == duel['p1_id'] else duel['p1_id']

        # Получаем точную сумму проигравшего перед обнулением
        u_loser = get_u(loser_id)
        loser_balance = u_loser['total'] if u_loser else 0

        death_phrases = ["ПОТРАЧЕНО! ⚰️", "В КАНАВУ! 🕳", "ОТКИС! 🧊", "ЗЕМЛЯ ПУХОМ! 🪦"]

        # ОБНОВЛЕНИЕ БАЛАНСА И СТАТИСТИКИ
        conn = sqlite3.connect(DB_NAME)
        # 1. Победителю: прибавляем дБ и +1 к победам
        conn.execute('UPDATE users SET total_whine = total_whine + ?, duel_wins = duel_wins + 1 WHERE user_id = ?',
                     (loser_balance, winner_id))
        # 2. Проигравшему: обнуляем и +1 к поражениям
        conn.execute('UPDATE users SET total_whine = 0, duel_losses = duel_losses + 1 WHERE user_id = ?', (loser_id,))
        conn.commit()
        conn.close()

        winner_name = duel['p1_name'] if winner_id == duel['p1_id'] else duel['p2_name']

        # ИСПРАВЛЕНО: Выровнял отступы (было слишком много пробелов)
        await call.message.edit_text(
            f"💀 <b>{random.choice(death_phrases)}</b>\n\n"
            f"🎯 Победитель забрал <b>{duel['bank']} дБ</b>!\n"
            f"🏆 Чемпион: <b>{winner_name}</b>\n"
            f"📉 Проигравший обнулен. Иди скули с нуля! 🐷",
            parse_mode="HTML"
        )

        # Обновляем ранги (КМС/МС)
        await update_score(winner_id, 0)
        await update_score(loser_id, 0)

        if d_id in active_duels:
            del active_duels[d_id]

    else:  # ПРОМАХ
        miss_phrases = [
            "МИМО! Пуля просвистела мимо уха... 💨",
            "КОСОЙ! Даже Дарвин Нуньес бы попал... 💩",
            "РИКОШЕТ! Пуля улетела в Мадрид! ✈️",
            "ОСЕЧКА! Твой ствол заклинило, как атаку Реала! 🔫🤡",
            "МАЗИЛА! Иди тренируйся на чушканах! 🐽",
            "ПЕРЕЛЕТ! Ты куда стреляешь, чучело? 👺"
        ]

        # Передача хода
        duel['turn'] = duel['p2_id'] if duel['turn'] == duel['p1_id'] else duel['p1_id']
        await shoot_round(call.message, d_id)

        # Бот выдает случайную фразу
        await call.answer(random.choice(miss_phrases), show_alert=False)


@dp.callback_query(F.data.startswith("d_dec_"))
async def d_decline(call: types.CallbackQuery):
    d_id = call.data.replace("d_dec_", "")
    duel = active_duels.get(d_id)
    if duel and (call.from_user.id == duel['p1_id'] or call.from_user.id == duel['p2_id']):
        await call.message.edit_text("🏳️ Дуэль отменена. Один из нытиков поджал хвост и убежал. 🐕‍🦺")
        del active_duels[d_id]


@dp.message(Command("shop"))
async def shop(message: Message):
    if not is_chat_on(message.chat.id):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['label']} ({v['price']} ⭐️)", callback_data=f"buy_{k}")]
        for k, v in RANKS.items() if v['price'] > 0
    ])
    await message.answer("🏪 **Магазин Скулежа**\nВыбери статус на 30 дней (защита от слива ранга):", reply_markup=kb)


@dp.message(Command("info"))
async def info_handler(message: Message):
    # ПРОВЕРКА: Если бот выключен в этом чате - игнорим (кроме твоей лички)
    if message.chat.type != "private" and not is_chat_on(message.chat.id):
        return

    text = (
        "ℹ️ **ИНФОРМАЦИЯ О ПОСКУЛИБОТЕ** ℹ️\n"
        "________________________________\n\n"

        "🎮 **ОСНОВНЫЕ КОМАНДЫ:**\n"
        "• `/skulistart` — Регистрация в системе\n"
        "• `/poskuli` или `+поскулить` — Замер скулежа (раз в 5 мин)\n"
        "• `/skulibet [сумма]` или `+казик [сумма]` — Казино\n"
        "• `+перевод [сумма]` (ответом юзеру) — Перевести дБ 💸\n"
        "• `+дуэль` (ответом юзеру) — Дуэль ва-банк ⚔️\n"
        "• `/skuliname [имя]` — Сменить погоняло\n"
        "• `/topskuli` — Топ нытиков чата\n"
        "• `/topglobal` — Мировой рейтинг (W/L)\n"
        "• `/shop` — Магазин статусов ⭐️\n\n"

        "📈 **РАНГИ ЗА дБ (Автоматически):**\n"
        "• 10к — КМС 🚀\n"
        "• 30к — МС 🌠\n"
        "• 100к — Ангел 👑\n"
        "• 500к — Бог 💎\n"
        "• 1.5м — Всемогущий 🌌\n"
        "• 1 млрд — Олимпиец 🏛️ (`+мут` до 10 минут)\n"
        "*(Если дБ упадут ниже порога — статус слетает!)*\n\n"

        "💎 **ПРИВИЛЕГИИ:**\n"
        "1. **Защита платных статусов:** статус не слетает при проигрыше в казино/дуэли 30 дней.\n"
        "2. **Буст:** множитель замера `/poskuli` до **x3.0** на Олимпийце.\n"
        "3. **Казино:** шансы подкручены вниз, особенно на высших рангах.\n"
        "4. **Олимпиец:** может мутить `+мут` максимум на 10 минут, кроме админов.\n"
        "5. **Архитектор:** `+списать`, `+мут`, `+бан`, `/grant`, `/vault`.\n\n"

        "📢 **ОФИЦИАЛЬНЫЙ КАНАЛ:**\n"
        "Следи за обновами здесь: https://t.me/MoopingERP\n"
        "________________________________\n"
        "⚙️ *Архитектор слышит твой скулёж...*"
    )

    await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("buy_"))
async def buy_call(call: types.CallbackQuery):
    # 1. Проверка: включен ли бот (должна быть внутри функции)
    if not is_chat_on(call.message.chat.id):
        return await call.answer("💤 Бот спит, магазин закрыт.", show_alert=True)

    # 2. Все следующие строки ДОЛЖНЫ быть с отступом (внутри функции)
    rank_k = call.data.replace("buy_", "")

    # Берем красивое название из твоего конфига RANKS
    rank_label = RANKS.get(rank_k, {}).get('label', rank_k)

    await bot.send_invoice(
        chat_id=call.message.chat.id,
        title=f"Статус {rank_label}",
        description="Привилегии на 30 дней",
        payload=f"pay_{rank_k}",
        currency="XTR",
        prices=[LabeledPrice(label="⭐️", amount=RANKS[rank_k]['price'])]
    )

    # ОБЯЗАТЕЛЬНО: убираем состояние загрузки с кнопки
    await call.answer()


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
    await m.answer(f"✨ ТЫ СРЕДИ БОГОВ ОЛИМПА! Ты теперь **{RANKS[rk]['label']}**! Скули с привелегиями!")


async def main(): init_db(); await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())



