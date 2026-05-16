"""Telegram-бот: дневник калорий с быстрым вводом.
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
    """Текущая дата по МСК. Меняется ровно в 00:00 МСК."""
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
    con.execute("DELETE FROM meals WHERE user_id=? AND day=?", (uid, msk_day()))
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
GOALS = {"Похудение": -0.20, "Лёгкое похудение": -0.10, "Поддержание": 0.0, "Набор массы": 0.10}


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


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = db_get_user(update.effective_user.id)
    if user:
        # уже есть профиль — сразу показываем что делать
        await update.message.reply_text(
            f"👋 С возвращением!\n\n"
            f"🎯 Твоя норма: {user['target_kcal']:.0f} ккал/день\n"
            f"Б{user['target_p']:.0f} / Ж{user['target_f']:.0f} / У{user['target_c']:.0f}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"📝 *Как добавить еду* — одно сообщение:\n"
            f"`овсянка 350 б15 ж8 у50`\n\n"
            f"Порядок не важен:\n"
            f"`450 ккал банан б2 у30 ж1`\n\n"
            f"Если не знаешь Б/Ж/У — пиши только ккал:\n"
            f"`шоколадка 200`\n"
            f"━━━━━━━━━━━━━━\n\n"
            f"/today — что съедено за день\n"
            f"/undo — удалить последнее\n"
            f"/reset — обнулить день\n"
            f"/me — изменить профиль",
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
    """Пересоздать профиль вручную."""
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
        f"📝 Как добавить еду — одно сообщение:\n"
        f"`овсянка 350 б15 ж8 у50`\n\n"
        f"Если знаешь только калории:\n"
        f"`шоколадка 200`\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"/today — итог дня\n"
        f"/undo — удалить последнее\n"
        f"/reset — обнулить день",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ===== ПАРСЕР СЪЕДЕННОГО =====
def parse_meal(text):
    """
    Принимает свободный текст и возвращает (name, kcal, p, f, c).
    Поддерживает форматы:
      овсянка 350 б15 ж8 у50
      350 ккал банан б2 у30 ж1
      шоколадка 200
      курица 250 ккал белки 30 жиры 10 углеводы 0
    """
    text = text.lower().strip()
    p = f = c = 0.0
    kcal = None

    # Б/Ж/У: ловим "б15", "б 15", "белки 15", "p15"
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

    # ккал: число рядом со словом "ккал" или просто первое крупное число
    m = re.search(r"([\d.,]+)\s*ккал", text)
    if m:
        kcal = float(m.group(1).replace(",", "."))
        text = re.sub(r"[\d.,]+\s*ккал", " ", text)
    else:
        # ищем первое число
        m = re.search(r"(?:^|\s)([\d.,]+)(?:\s|$)", text)
        if m:
            kcal = float(m.group(1).replace(",", "."))
            text = text.replace(m.group(0), " ", 1)

    # имя — то, что осталось от текста
    name = re.sub(r"\s+", " ", text).strip(" ,.-") or "блюдо"
    if kcal is None:
        return None
    return (name, kcal, p, f, c)


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
            "❓ Не понял, сколько калорий. Напиши примерно так:\n\n"
            "`овсянка 350 б15 ж8 у50`\n"
            "`шоколадка 200`\n"
            "`банан 90 ккал б1 у22`",
            parse_mode="Markdown",
        )
        return

    name, kcal, p, f, c = parsed
    db_add_meal(uid, name, kcal, p, f, c)

    # Сразу показываем итог дня
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
    # любое текстовое сообщение НЕ внутри диалога = добавление еды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_meal))
    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
