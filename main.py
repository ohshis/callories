"""Telegram-бот: дневник калорий.
Поддерживает ввод КБЖУ как на готовое блюдо, так и на 100 г + граммы.
Сбрасывается автоматически в полночь по МСК.
"""
import os
import re
import sqlite3
import logging
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ConversationHandler, ContextTypes,
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 10000))
DB_PATH = os.environ.get("DB_PATH", "/var/data/bot.db")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN")

try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except (PermissionError, OSError):
    DB_PATH = "bot.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))


def msk_day():
    return datetime.now(MSK).date().isoformat()


# ===== БАЗА =====
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        gender TEXT, age INTEGER, height REAL, weight REAL,
        activity REAL, goal REAL,
        target_kcal REAL, target_p REAL, target_f REAL, target_c REAL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, day TEXT,
        name TEXT, kcal REAL, p REAL, f REAL, c REAL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS notifications (
        user_id INTEGER, day TEXT, kind TEXT,
        PRIMARY KEY (user_id, day, kind)
    )""")
    con.commit(); con.close()


def db_save_user(uid, d):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, d["gender"], d["age"], d["height"], d["weight"],
         d["activity"], d["goal"], d["target_kcal"],
         d["target_p"], d["target_f"], d["target_c"])
    )
    con.commit(); con.close()


def db_get_user(uid):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return dict(r) if r else None


def db_add_meal(uid, name, kcal, p, f, c):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO meals (user_id, day, name, kcal, p, f, c) VALUES (?,?,?,?,?,?,?)",
        (uid, msk_day(), name, kcal, p, f, c)
    )
    con.commit(); con.close()


def db_today_meals(uid):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM meals WHERE user_id=? AND day=? ORDER BY id",
        (uid, msk_day())
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def db_delete_last(uid):
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "SELECT id, name FROM meals WHERE user_id=? AND day=? ORDER BY id DESC LIMIT 1",
        (uid, msk_day())
    )
    r = cur.fetchone()
    if r:
        con.execute("DELETE FROM meals WHERE id=?", (r[0],))
        con.commit()
    con.close()
    return r[1] if r else None


def db_reset_today(uid):
    con = sqlite3.connect(DB_PATH)
    today = msk_day()
    con.execute("DELETE FROM meals WHERE user_id=? AND day=?", (uid, today))
    con.execute("DELETE FROM notifications WHERE user_id=? AND day=?", (uid, today))
    con.commit(); con.close()


def db_notif_seen(uid, kind):
    con = sqlite3.connect(DB_PATH)
    r = con.execute(
        "SELECT 1 FROM notifications WHERE user_id=? AND day=? AND kind=?",
        (uid, msk_day(), kind)
    ).fetchone()
    con.close()
    return r is not None


def db_notif_mark(uid, kind):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO notifications (user_id, day, kind) VALUES (?,?,?)",
        (uid, msk_day(), kind)
    )
    con.commit(); con.close()


# ===== ПРОФИЛЬ =====
S_GENDER, S_AGE, S_HEIGHT, S_WEIGHT, S_ACT, S_GOAL = range(6)
ACT_LEVELS = {
    "Сидячий": 1.2,
    "Лёгкие (1-3 р/нед)": 1.375,
    "Умеренные (3-5 р/нед)": 1.55,
    "Высокие (6-7 р/нед)": 1.725,
    "Экстремальные": 1.9,
}
GOALS = {
    "Похудение": -0.20,
    "Лёгкое похудение": -0.10,
    "Поддержание": 0.0,
    "Набор массы": 0.10,
}


def calc_targets(d):
    w, h, a = d["weight"], d["height"], d["age"]
    gen_mod = -161 if d["gender"] == "Женский" else 5
    bmr = 10 * w + 6.25 * h - 5 * a + gen_mod
    tdee = bmr * d["activity"]
    target = tdee * (1 + d["goal"])
    p_g = 2 * w
    f_g = 0.8 * w
    c_g = (target - p_g * 4 - f_g * 9) / 4
    return {"target_kcal": target, "target_p": p_g, "target_f": f_g, "target_c": c_g}


HELP_TEXT = (
    "📝 *Как добавить еду:*\n\n"
    "*Способ 1* — сразу полные калории:\n"
    "`овсянка 350 б15 ж8 у50`\n\n"
    "*Способ 2* — КБЖУ на 100 г и сколько съела:\n"
    "`овсянка 113 б4.9 ж4.1 у15 200г`\n"
    "_(113 ккал на 100г, съела 200г → бот посчитает сам)_\n\n"
    "*Минимум* — только калории:\n"
    "`шоколадка 200`\n"
    "`йогурт 60 на 100г 150г`\n\n"
    "Граммы помечай буквой `г`: 200г, 150 г, 80гр."
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = db_get_user(update.effective_user.id)
    if user:
        await update.message.reply_text(
            f"👋 С возвращением!\n\n"
            f"🎯 Норма: {user['target_kcal']:.0f} ккал/день\n"
            f"Б{user['target_p']:.0f} / Ж{user['target_f']:.0f} / У{user['target_c']:.0f}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"{HELP_TEXT}\n"
            f"━━━━━━━━━━━━━━\n\n"
            f"/today — итог дня\n"
            f"/undo — удалить последнее\n"
            f"/reset — обнулить день\n"
            f"/me — изменить профиль\n"
            f"/help — подсказка по вводу",
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Привет! Я твой дневник калорий.\n\n"
        "Сначала настроим профиль. Укажи пол:",
        reply_markup=ReplyKeyboardMarkup(
            [["Женский", "Мужской"]], one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return S_GENDER


async def me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Перенастроим профиль. Укажи пол:",
        reply_markup=ReplyKeyboardMarkup(
            [["Женский", "Мужской"]], one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return S_GENDER


async def gender(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in ("Женский", "Мужской"):
        await update.message.reply_text("Выбери кнопку")
        return S_GENDER
    ctx.user_data["gender"] = update.message.text
    await update.message.reply_text("Возраст?", reply_markup=ReplyKeyboardRemove())
    return S_AGE


async def age(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["age"] = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Число, например 25")
        return S_AGE
    await update.message.reply_text("Рост в см?")
    return S_HEIGHT


async def height(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["height"] = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Число, например 165")
        return S_HEIGHT
    await update.message.reply_text("Вес в кг?")
    return S_WEIGHT


async def weight(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["weight"] = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Число, например 60")
        return S_WEIGHT
    kb = [[k] for k in ACT_LEVELS.keys()]
    await update.message.reply_text(
        "Активность?",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return S_ACT


async def activity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    coef = ACT_LEVELS.get(update.message.text)
    if coef is None:
        await update.message.reply_text("Выбери из кнопок")
        return S_ACT
    ctx.user_data["activity"] = coef
    kb = [[k] for k in GOALS.keys()]
    await update.message.reply_text(
        "Цель?",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return S_GOAL


async def goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mod = GOALS.get(update.message.text)
    if mod is None:
        await update.message.reply_text("Выбери из кнопок")
        return S_GOAL
    ctx.user_data["goal"] = mod
    t = calc_targets(ctx.user_data)
    ctx.user_data.update(t)
    db_save_user(update.effective_user.id, ctx.user_data)
    await update.message.reply_text(
        f"✅ Готово!\n\n"
        f"🎯 Норма: *{t['target_kcal']:.0f} ккал/день*\n"
        f"🥩 Белки: {t['target_p']:.0f} г\n"
        f"🥑 Жиры: {t['target_f']:.0f} г\n"
        f"🍚 Углеводы: {t['target_c']:.0f} г\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"{HELP_TEXT}\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"/today — итог дня\n"
        f"/undo — удалить последнее\n"
        f"/reset — обнулить день\n"
        f"/help — подсказка по вводу",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


# ===== ПАРСЕР =====
def parse_meal(text):
    """
    Возвращает (name, kcal, p, f, c).
    Если в тексте указаны граммы (200г, 150 г, 80гр) — КБЖУ умножаются на (граммы / 100).
    Если граммы не указаны — КБЖУ берутся как есть (для готового блюда).
    """
    text = text.lower().strip()

    # 1. Граммы — ловим "200г", "150 г", "80гр", "200 грамм"
    grams = None
    m = re.search(r"(?<![\d.,])([\d.,]+)\s*(?:г|гр|грамм[ао]?в?)(?:\s|$)", text)
    if m:
        try:
            grams = float(m.group(1).replace(",", "."))
            text = text[:m.start()] + " " + text[m.end():]
        except ValueError:
            pass

    # 2. БЖУ
    patterns = {
        "p": r"(?:^|\s)(?:б|p|белк[иа])\s*([\d.,]+)",
        "f": r"(?:^|\s)(?:ж|f|жир[ыа])\s*([\d.,]+)",
        "c": r"(?:^|\s)(?:у|c|углевод[ыа])\s*([\d.,]+)",
    }
    found = {}
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            found[key] = float(m.group(1).replace(",", "."))
            text = re.sub(pat, " ", text)
    p = found.get("p", 0.0)
    f = found.get("f", 0.0)
    c = found.get("c", 0.0)

    # 3. Калории
    kcal = None
    m = re.search(r"([\d.,]+)\s*ккал", text)
    if m:
        kcal = float(m.group(1).replace(",", "."))
        text = re.sub(r"[\d.,]+\s*ккал", " ", text)
    else:
        # Игнорируем "на 100г" и подобные служебные слова
        text_clean = re.sub(r"на\s*100\s*г?р?", " ", text)
        m = re.search(r"(?:^|\s)([\d.,]+)(?:\s|$)", text_clean)
        if m:
            try:
                kcal = float(m.group(1).replace(",", "."))
                # удаляем именно это число из исходного text
                text = re.sub(r"(?:^|\s)" + re.escape(m.group(1)) + r"(?:\s|$)", " ", text, count=1)
            except ValueError:
                pass

    if kcal is None:
        return None

    # 4. Имя — то, что осталось (с очисткой служебных слов)
    name = re.sub(r"на\s*100\s*г?р?", " ", text)
    name = re.sub(r"\s+", " ", name).strip(" ,.-")
    if not name:
        name = "блюдо"

    # 5. Если указаны граммы — пересчитываем (КБЖУ были на 100 г)
    if grams is not None:
        k = grams / 100
        kcal *= k
        p *= k
        f *= k
        c *= k
        name = f"{name} ({grams:.0f} г)"

    return (name, kcal, p, f, c)


# ===== ДОСТИЖЕНИЯ =====
def check_achievements(uid, user, sums):
    notifications = []
    sum_k, sum_p, sum_f, sum_c = sums

    if sum_k >= user["target_kcal"] and not db_notif_seen(uid, "kcal_done"):
        notifications.append(
            "🎉 *Дневная норма калорий набрана!*\n"
            f"Съедено: {sum_k:.0f} из {user['target_kcal']:.0f} ккал"
        )
        db_notif_mark(uid, "kcal_done")

    if sum_k >= user["target_kcal"] + 200 and not db_notif_seen(uid, "kcal_over"):
        over = sum_k - user["target_kcal"]
        notifications.append(
            f"⚠️ *Перебор по калориям на {over:.0f} ккал*\n"
            "Ничего страшного — главное, не сдаваться. Завтра новый день 💪"
        )
        db_notif_mark(uid, "kcal_over")

    if sum_p >= user["target_p"] and not db_notif_seen(uid, "p_done"):
        notifications.append(
            f"🥩 *Норма белков достигнута!* {sum_p:.0f} / {user['target_p']:.0f} г\n"
            "Отлично — белок важен для мышц и сытости 💪"
        )
        db_notif_mark(uid, "p_done")

    if sum_f >= user["target_f"] and not db_notif_seen(uid, "f_done"):
        notifications.append(
            f"🥑 *Норма жиров достигнута!* {sum_f:.0f} / {user['target_f']:.0f} г\n"
            "Полезные жиры — важно для гормонов и кожи ✨"
        )
        db_notif_mark(uid, "f_done")

    if sum_c >= user["target_c"] and not db_notif_seen(uid, "c_done"):
        notifications.append(
            f"🍚 *Норма углеводов достигнута!* {sum_c:.0f} / {user['target_c']:.0f} г\n"
            "Энергия на день обеспечена 🔋"
        )
        db_notif_mark(uid, "c_done")

    bju_all = (sum_p >= user["target_p"] and
               sum_f >= user["target_f"] and
               sum_c >= user["target_c"])
    if bju_all and not db_notif_seen(uid, "bju_all"):
        notifications.append(
            "🏆 *Все нормы БЖУ закрыты!*\n"
            "Идеальный день по питанию. Так держать! 🌟"
        )
        db_notif_mark(uid, "bju_all")

    return notifications


# ===== ДОБАВЛЕНИЕ ЕДЫ =====
async def add_meal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = db_get_user(uid)
    if not user:
        await update.message.reply_text("Сначала настрой профиль: /start")
        return

    parsed = parse_meal(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял, сколько калорий.\n\n" + HELP_TEXT,
            parse_mode="Markdown",
        )
        return

    name, kcal, p, f, c = parsed
    db_add_meal(uid, name, kcal, p, f, c)

    meals = db_today_meals(uid)
    sum_k = sum(m["kcal"] for m in meals)
    sum_p = sum(m["p"] for m in meals)
    sum_f = sum(m["f"] for m in meals)
    sum_c = sum(m["c"] for m in meals)
    left_k = user["target_kcal"] - sum_k
    emoji = "✅" if left_k >= 0 else "⚠️"
    bju_part = ""
    if p or f or c:
        bju_part = f" (Б{p:.0f}/Ж{f:.0f}/У{c:.0f})"

    await update.message.reply_text(
        f"✅ +{kcal:.0f} ккал — {name}{bju_part}\n\n"
        f"📊 За день: *{sum_k:.0f} / {user['target_kcal']:.0f} ккал*\n"
        f"Б{sum_p:.0f}/Ж{sum_f:.0f}/У{sum_c:.0f}\n\n"
        f"{emoji} Осталось: *{left_k:.0f} ккал*",
        parse_mode="Markdown",
    )

    for msg in check_achievements(uid, user, (sum_k, sum_p, sum_f, sum_c)):
        await update.message.reply_text(msg, parse_mode="Markdown")


# ===== /today =====
async def today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = db_get_user(uid)
    if not user:
        await update.message.reply_text("Сначала настрой профиль: /start")
        return
    meals = db_today_meals(uid)
    if not meals:
        await update.message.reply_text(
            f"Сегодня пока ничего не съедено.\n\n"
            f"🎯 Норма: {user['target_kcal']:.0f} ккал\n"
            f"Б{user['target_p']:.0f} / Ж{user['target_f']:.0f} / У{user['target_c']:.0f}"
        )
        return
    sum_k = sum(m["kcal"] for m in meals)
    sum_p = sum(m["p"] for m in meals)
    sum_f = sum(m["f"] for m in meals)
    sum_c = sum(m["c"] for m in meals)
    left_k = user["target_kcal"] - sum_k
    left_p = user["target_p"] - sum_p
    left_f = user["target_f"] - sum_f
    left_c = user["target_c"] - sum_c
    lst = "\n".join(
        f"• {m['name']} — {m['kcal']:.0f} ккал"
        + (f" (Б{m['p']:.0f}/Ж{m['f']:.0f}/У{m['c']:.0f})" if (m['p'] or m['f'] or m['c']) else "")
        for m in meals
    )
    pct = sum_k / user["target_kcal"] * 100 if user["target_kcal"] else 0
    filled = min(10, int(pct / 10))
    bar = "▰" * filled + "▱" * (10 - filled)
    emoji = "✅" if left_k >= 0 else "⚠️"
    await update.message.reply_text(
        f"📊 *Сегодня съедено:*\n\n{lst}\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"Итого: *{sum_k:.0f} / {user['target_kcal']:.0f} ккал*\n"
        f"{bar} {pct:.0f}%\n\n"
        f"🥩 Белки: {sum_p:.0f} / {user['target_p']:.0f} г\n"
        f"🥑 Жиры: {sum_f:.0f} / {user['target_f']:.0f} г\n"
        f"🍚 Углеводы: {sum_c:.0f} / {user['target_c']:.0f} г\n\n"
        f"{emoji} *Осталось: {left_k:.0f} ккал*\n"
        f"Б{left_p:.0f} / Ж{left_f:.0f} / У{left_c:.0f}\n\n"
        f"_Сброс в 00:00 по МСК_",
        parse_mode="Markdown",
    )


async def undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = db_delete_last(update.effective_user.id)
    if name:
        await update.message.reply_text(f"🗑 Удалено: {name}")
    else:
        await update.message.reply_text("Нечего удалять.")


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db_reset_today(update.effective_user.id)
    await update.message.reply_text("🗑 День обнулён.")


# ===== HTTP =====
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"alive")
    def log_message(self, *a, **kw):
        return


def run_http():
    HTTPServer(("0.0.0.0", PORT), Ping).serve_forever()


def main():
    db_init()
    threading.Thread(target=run_http, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    profile_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("me", me)],
        states={
            S_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, gender)],
            S_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age)],
            S_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, height)],
            S_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, weight)],
            S_ACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, activity)],
            S_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(profile_conv)
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_meal))
    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
