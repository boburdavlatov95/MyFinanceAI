import os
import re
import json
import requests
import psycopg
from psycopg.rows import dict_row

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

GROQ_MODEL = "openai/gpt-oss-20b"

PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

# Telegram webhook path
WEBHOOK_PATH = "telegram-webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL topilmadi")

print("=" * 50)
print("MyFinance AI")
print("=" * 50)
print("Groq:", "YOQILGAN" if GROQ_API_KEY else "O'CHIRILGAN")
print("Model:", GROQ_MODEL)
print("Database: Supabase PostgreSQL")
print("Port:", PORT)


# =========================================================
# DATABASE
# =========================================================

def db():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=10
    )


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS finance_periods (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            title TEXT DEFAULT 'Hisob',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            period_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount NUMERIC(18,2) NOT NULL,
            person TEXT,
            category TEXT,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            period_id INTEGER NOT NULL,
            person TEXT,
            amount NUMERIC(18,2) NOT NULL,
            debt_type TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    cur.close()
    conn.close()

    print("Database tayyor")


def get_current_period(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id
        FROM finance_periods
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 1
    """, (user_id,))

    row = cur.fetchone()

    if row:
        period_id = row["id"]
    else:
        cur.execute("""
            INSERT INTO finance_periods (user_id, title)
            VALUES (%s, 'Hisob #1')
            RETURNING id
        """, (user_id,))
        period_id = cur.fetchone()["id"]

    conn.commit()
    cur.close()
    conn.close()

    return period_id


def create_new_period(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM finance_periods
        WHERE user_id = %s
    """, (user_id,))

    number = int(cur.fetchone()["cnt"]) + 1

    cur.execute("""
        INSERT INTO finance_periods (user_id, title)
        VALUES (%s, %s)
        RETURNING id
    """, (user_id, f"Hisob #{number}"))

    period_id = cur.fetchone()["id"]

    conn.commit()
    cur.close()
    conn.close()

    return period_id


# =========================================================
# MONEY
# =========================================================

def parse_amount(text):
    text = text.lower().replace(",", ".").replace(" ", "")

    # 1.2 mln
    m = re.search(r"(\d+(?:\.\d+)?)\s*(mln|million|млн)", text)
    if m:
        return float(m.group(1)) * 1_000_000

    # 800 ming
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ming|тыс)", text)
    if m:
        return float(m.group(1)) * 1_000

    # 5m
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\b", text)
    if m:
        return float(m.group(1)) * 1_000_000

    # oddiy raqam
    numbers = re.findall(r"\d+(?:\.\d+)?", text)

    if numbers:
        return float(numbers[-1])

    return None


def money(n):
    return f"{float(n):,.0f}".replace(",", " ") + " so'm"


# =========================================================
# FAST PARSER
# =========================================================

def fast_parse(text):
    t = text.lower()

    amount = parse_amount(t)

    if not amount:
        return None

    # Qarzdorlik iboralari bo'lsa AI ga beramiz
    debt_words = [
        "qarz",
        "qarz oldim",
        "qarz berdim",
        "qarzim",
        "qaytardi",
        "qaytardim",
        "qarzni",
        "qarzga",
        "lend",
        "borrow",
    ]

    if any(x in t for x in debt_words):
        return None

    # INCOME
    income_words = [
        "tushdi",
        "tushum",
        "oldim",
        "keldi",
        "daromad",
        "sotdim",
        "mijozdan",
        "klientdan",
        "pul keldi",
    ]

    if any(x in t for x in income_words):
        category = "Tushum"

        for word in [
            "klientdan",
            "mijozdan",
            "reklamadan",
            "ishdan",
            "sotuvdan",
        ]:
            if word in t:
                category = word.capitalize()
                break

        return [{
            "type": "INCOME",
            "amount": amount,
            "person": None,
            "category": category,
            "note": text
        }]

    # EXPENSE
    expense_words = [
        "ketdi",
        "sarfladim",
        "sarflandi",
        "oldim",
        "to'ladim",
        "toladim",
        "xarajat",
        "uchun",
        "ga ketdi",
    ]

    if any(x in t for x in expense_words):

        category = "Xarajat"

        if "material" in t:
            category = "Material"
        elif "ishchi" in t:
            category = "Ishchi"
        elif "reklama" in t:
            category = "Reklama"
        elif "transport" in t:
            category = "Transport"
        elif "ovqat" in t:
            category = "Ovqat"

        return [{
            "type": "EXPENSE",
            "amount": amount,
            "person": None,
            "category": category,
            "note": text
        }]

    return None


# =========================================================
# GROQ AI
# =========================================================

def groq_parse(text):
    if not GROQ_API_KEY:
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"

    system = """
Sen MyFinance AI nomli pul hisob botining parserisan.

Foydalanuvchi yozgan gapni pul operatsiyalariga ajrat.

Faqat JSON qaytar.

Format:

{
  "transactions": [
    {
      "type": "INCOME | EXPENSE | DEBT_IN | DEBT_OUT",
      "amount": 0,
      "person": null,
      "category": "",
      "note": "",
      "debt_action": "NEW | REPAY | NONE"
    }
  ]
}

Qoidalar:

INCOME:
Pul foydalanuvchiga oddiy daromad/tushum sifatida keldi.

EXPENSE:
Pul foydalanuvchidan oddiy xarajat sifatida chiqdi.

DEBT_IN:
Foydalanuvchi boshqa odamdan qarz oldi.
Bu pul balansni oshiradi.

DEBT_OUT:
Foydalanuvchi boshqa odamga qarz berdi.
Bu pul balansni kamaytiradi.

Qarz qaytarish:
- Foydalanuvchi o'zi olgan qarzni qaytarsa -> EXPENSE + debt_action REPAY
- Boshqa odam foydalanuvchiga qarzini qaytarsa -> INCOME + debt_action REPAY

Faqat "Azizga 5 mln qarzim bor" kabi gaplarda pul harakati bo'lmasa,
hech qanday transaction yaratma.

Bir gapda bir nechta operatsiya bo'lsa hammasini chiqar.

Misol:
"Klientdan 5 mln tushdi, materialga 1.2 mln, ishchiga 800 ming ketdi"

{
 "transactions": [
   {
    "type":"INCOME",
    "amount":5000000,
    "person":null,
    "category":"Klient",
    "note":"Klientdan 5 mln tushdi",
    "debt_action":"NONE"
   },
   {
    "type":"EXPENSE",
    "amount":1200000,
    "person":null,
    "category":"Material",
    "note":"Materialga 1.2 mln",
    "debt_action":"NONE"
   },
   {
    "type":"EXPENSE",
    "amount":800000,
    "person":null,
    "category":"Ishchi",
    "note":"Ishchiga 800 ming",
    "debt_action":"NONE"
   }
 ]
}

Faqat JSON qaytar.
"""

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text}
        ]
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=20
        )

        r.raise_for_status()

        data = r.json()

        content = data["choices"][0]["message"]["content"].strip()

        # Markdown JSON bo'lsa tozalash
        content = re.sub(r"^```json", "", content)
        content = re.sub(r"^```", "", content)
        content = re.sub(r"```$", "", content)
        content = content.strip()

        parsed = json.loads(content)

        return parsed.get("transactions", [])

    except Exception as e:
        print("GROQ ERROR:", repr(e))
        return None


def parse_text(text):
    fast = fast_parse(text)

    if fast:
        print("FAST:", fast)
        return fast

    ai = groq_parse(text)

    print("AI:", ai)

    return ai


# =========================================================
# DEBT
# =========================================================

def add_debt(user_id, period_id, person, amount, debt_type):
    if not person:
        person = "Noma'lum"

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO debts
        (user_id, period_id, person, amount, debt_type)
        VALUES (%s, %s, %s, %s, %s)
    """, (
        user_id,
        period_id,
        person,
        amount,
        debt_type
    ))

    conn.commit()
    cur.close()
    conn.close()


def change_debt(user_id, period_id, person, amount, debt_type):
    if not person:
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, amount
        FROM debts
        WHERE user_id = %s
          AND period_id = %s
          AND person = %s
          AND debt_type = %s
          AND amount > 0
        ORDER BY id
        LIMIT 1
    """, (
        user_id,
        period_id,
        person,
        debt_type
    ))

    row = cur.fetchone()

    if row:
        new_amount = float(row["amount"]) - float(amount)

        if new_amount <= 0:
            cur.execute(
                "DELETE FROM debts WHERE id = %s",
                (row["id"],)
            )
        else:
            cur.execute("""
                UPDATE debts
                SET amount = %s
                WHERE id = %s
            """, (
                new_amount,
                row["id"]
            ))

    conn.commit()
    cur.close()
    conn.close()


# =========================================================
# SAVE TRANSACTIONS
# =========================================================

def save_transactions(user_id, items):
    period_id = get_current_period(user_id)

    saved = []

    conn = db()
    cur = conn.cursor()

    for item in items:

        kind = item.get("type")
        amount = float(item.get("amount") or 0)

        if amount <= 0:
            continue

        person = item.get("person")
        category = item.get("category") or "Xarajat"
        note = item.get("note") or ""
        debt_action = item.get("debt_action", "NONE")

        if kind not in [
            "INCOME",
            "EXPENSE",
            "DEBT_IN",
            "DEBT_OUT"
        ]:
            continue

        # Qarzning o'zi bo'lsa
        if kind == "DEBT_IN":
            category = "Qarzdan kirim"

        elif kind == "DEBT_OUT":
            category = "Qarzga chiqim"

        cur.execute("""
            INSERT INTO transactions
            (user_id, period_id, kind, amount, person, category, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            user_id,
            period_id,
            kind,
            amount,
            person,
            category,
            note
        ))

        transaction_id = cur.fetchone()["id"]

        # Yangi qarz
        if kind == "DEBT_IN":
            add_debt(
                user_id,
                period_id,
                person,
                amount,
                "I_OWE"
            )

        elif kind == "DEBT_OUT":
            add_debt(
                user_id,
                period_id,
                person,
                amount,
                "OWES_ME"
            )

        # Qarz qaytarish
        elif debt_action == "REPAY":

            if kind == "EXPENSE":
                change_debt(
                    user_id,
                    period_id,
                    person,
                    amount,
                    "I_OWE"
                )

            elif kind == "INCOME":
                change_debt(
                    user_id,
                    period_id,
                    person,
                    amount,
                    "OWES_ME"
                )

        saved.append({
            "id": transaction_id,
            "type": kind,
            "amount": amount,
            "category": category
        })

    conn.commit()
    cur.close()
    conn.close()

    return saved


# =========================================================
# BALANCE / REPORT
# =========================================================

def get_report(user_id):
    period_id = get_current_period(user_id)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN kind IN ('INCOME', 'DEBT_IN')
                    THEN amount
                    ELSE 0
                END
            ), 0) AS incoming,

            COALESCE(SUM(
                CASE
                    WHEN kind IN ('EXPENSE', 'DEBT_OUT')
                    THEN amount
                    ELSE 0
                END
            ), 0) AS outgoing,

            COALESCE(SUM(
                CASE
                    WHEN kind = 'INCOME'
                    THEN amount
                    ELSE 0
                END
            ), 0) AS income,

            COALESCE(SUM(
                CASE
                    WHEN kind = 'EXPENSE'
                    THEN amount
                    ELSE 0
                END
            ), 0) AS expense,

            COALESCE(SUM(
                CASE
                    WHEN kind = 'DEBT_IN'
                    THEN amount
                    ELSE 0
                END
            ), 0) AS debt_in,

            COALESCE(SUM(
                CASE
                    WHEN kind = 'DEBT_OUT'
                    THEN amount
                    ELSE 0
                END
            ), 0) AS debt_out

        FROM transactions
        WHERE user_id = %s
          AND period_id = %s
    """, (
        user_id,
        period_id
    ))

    row = cur.fetchone()

    cur.close()
    conn.close()

    income = float(row["income"])
    expense = float(row["expense"])
    debt_in = float(row["debt_in"])
    debt_out = float(row["debt_out"])

    balance = income + debt_in - expense - debt_out

    return {
        "income": income,
        "expense": expense,
        "debt_in": debt_in,
        "debt_out": debt_out,
        "balance": balance
    }


def get_transactions(user_id, kind=None):
    period_id = get_current_period(user_id)

    conn = db()
    cur = conn.cursor()

    if kind:
        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id = %s
              AND period_id = %s
              AND kind = %s
            ORDER BY id DESC
            LIMIT 50
        """, (
            user_id,
            period_id,
            kind
        ))
    else:
        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id = %s
              AND period_id = %s
            ORDER BY id DESC
            LIMIT 50
        """, (
            user_id,
            period_id
        ))

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# =========================================================
# DEBT LIST
# =========================================================

def get_debts(user_id):
    period_id = get_current_period(user_id)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT person, amount, debt_type
        FROM debts
        WHERE user_id = %s
          AND period_id = %s
          AND amount > 0
        ORDER BY id DESC
    """, (
        user_id,
        period_id
    ))

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# =========================================================
# DELETE LAST
# =========================================================

def delete_last_transaction(user_id):
    period_id = get_current_period(user_id)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM transactions
        WHERE user_id = %s
          AND period_id = %s
        ORDER BY id DESC
        LIMIT 1
    """, (
        user_id,
        period_id
    ))

    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return None

    # Qarz transaction bo'lsa, debtni ham teskari qilamiz
    if row["kind"] == "DEBT_IN":
        cur.execute("""
            SELECT id, amount
            FROM debts
            WHERE user_id = %s
              AND period_id = %s
              AND person = %s
              AND debt_type = 'I_OWE'
            ORDER BY id DESC
            LIMIT 1
        """, (
            user_id,
            period_id,
            row["person"]
        ))

        debt = cur.fetchone()

        if debt:
            new_amount = float(debt["amount"]) - float(row["amount"])

            if new_amount <= 0:
                cur.execute(
                    "DELETE FROM debts WHERE id = %s",
                    (debt["id"],)
                )
            else:
                cur.execute("""
                    UPDATE debts
                    SET amount = %s
                    WHERE id = %s
                """, (
                    new_amount,
                    debt["id"]
                ))

    elif row["kind"] == "DEBT_OUT":
        cur.execute("""
            SELECT id, amount
            FROM debts
            WHERE user_id = %s
              AND period_id = %s
              AND person = %s
              AND debt_type = 'OWES_ME'
            ORDER BY id DESC
            LIMIT 1
        """, (
            user_id,
            period_id,
            row["person"]
        ))

        debt = cur.fetchone()

        if debt:
            new_amount = float(debt["amount"]) - float(row["amount"])

            if new_amount <= 0:
                cur.execute(
                    "DELETE FROM debts WHERE id = %s",
                    (debt["id"],)
                )
            else:
                cur.execute("""
                    UPDATE debts
                    SET amount = %s
                    WHERE id = %s
                """, (
                    new_amount,
                    debt["id"]
                ))

    cur.execute(
        "DELETE FROM transactions WHERE id = %s",
        (row["id"],)
    )

    conn.commit()
    cur.close()
    conn.close()

    return row


# =========================================================
# TELEGRAM KEYBOARD
# =========================================================

def keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 Tushumlar", "💸 Xarajatlar"],
            ["📊 Hisobot", "🤝 Qarzlar"],
            ["🗑 Oxirgisini o'chirish"],
            ["🔄 Yangi hisob — 0 dan"],
        ],
        resize_keyboard=True
    )


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    get_current_period(user_id)

    await update.message.reply_text(
        "👋 MyFinance AI ga xush kelibsiz!\n\n"
        "Pul harakatini oddiy tilda yozing.\n\n"
        "Masalan:\n"
        "• Klientdan 5 mln tushdi\n"
        "• Materialga 1.2 mln ketdi\n"
        "• Ishchiga 800 ming berdim\n\n"
        "Bot avtomatik hisoblaydi.",
        reply_markup=keyboard()
    )


# =========================================================
# INCOME
# =========================================================

async def show_income(update, context):
    rows = get_transactions(
        update.effective_user.id,
        "INCOME"
    )

    if not rows:
        await update.message.reply_text(
            "💰 Hozircha tushum yo'q.",
            reply_markup=keyboard()
        )
        return

    total = sum(float(x["amount"]) for x in rows)

    text = "💰 TUSHUMLAR\n\n"

    for row in reversed(rows):
        text += (
            f"• {row['category']}: "
            f"{money(row['amount'])}\n"
        )

    text += f"\nJami: {money(total)}"

    await update.message.reply_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# EXPENSE
# =========================================================

async def show_expenses(update, context):
    rows = get_transactions(
        update.effective_user.id,
        "EXPENSE"
    )

    if not rows:
        await update.message.reply_text(
            "💸 Hozircha xarajat yo'q.",
            reply_markup=keyboard()
        )
        return

    total = sum(float(x["amount"]) for x in rows)

    text = "💸 XARAJATLAR\n\n"

    for row in reversed(rows):
        text += (
            f"• {row['category']}: "
            f"{money(row['amount'])}\n"
        )

    text += f"\nJami: {money(total)}"

    await update.message.reply_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# REPORT
# =========================================================

async def show_report(update, context):
    r = get_report(update.effective_user.id)

    text = (
        "📊 HISOBOT\n\n"
        f"💰 Tushum: {money(r['income'])}\n"
        f"💸 Xarajat: {money(r['expense'])}\n\n"
        f"🤝 Qarzdan kirim: {money(r['debt_in'])}\n"
        f"🤝 Qarzga chiqim: {money(r['debt_out'])}\n\n"
        f"🟢 Qoldiq: {money(r['balance'])}"
    )

    await update.message.reply_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# DEBTS
# =========================================================

async def show_debts(update, context):
    rows = get_debts(update.effective_user.id)

    if not rows:
        await update.message.reply_text(
            "🤝 Hozircha qarz yo'q.",
            reply_markup=keyboard()
        )
        return

    owe = []
    owed = []

    for row in rows:
        if row["debt_type"] == "I_OWE":
            owe.append(row)
        elif row["debt_type"] == "OWES_ME":
            owed.append(row)

    text = "🤝 QARZLAR\n\n"

    if owe:
        text += "🔴 MEN QARZDORMAN:\n"
        for row in owe:
            text += (
                f"• {row['person']}: "
                f"{money(row['amount'])}\n"
            )
        text += "\n"

    if owed:
        text += "🟢 MENGA QARZ:\n"
        for row in owed:
            text += (
                f"• {row['person']}: "
                f"{money(row['amount'])}\n"
            )

    await update.message.reply_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# DELETE
# =========================================================

async def delete_last(update, context):
    row = delete_last_transaction(
        update.effective_user.id
    )

    if not row:
        await update.message.reply_text(
            "🗑 O'chirish uchun transaction yo'q.",
            reply_markup=keyboard()
        )
        return

    await update.message.reply_text(
        f"🗑 O'chirildi:\n"
        f"{row['category']} — {money(row['amount'])}",
        reply_markup=keyboard()
    )


# =========================================================
# NEW PERIOD
# =========================================================

async def new_period(update, context):
    context.user_data["confirm_new_period"] = True

    await update.message.reply_text(
        "⚠️ Yangi hisob ochilsinmi?\n\n"
        "Eski hisoblar o'chirilmaydi.\n"
        "Faqat hozirgi hisob 0 dan boshlanadi.\n\n"
        "Tasdiqlash uchun: HA"
    )


# =========================================================
# MAIN TEXT HANDLER
# =========================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # Yangi hisob tasdiqlash
    if context.user_data.get("confirm_new_period"):

        if text.lower() in ["ha", "xa", "yes", "да"]:

            create_new_period(user_id)

            context.user_data["confirm_new_period"] = False

            await update.message.reply_text(
                "✅ Yangi hisob ochildi.\n\n"
                "🟢 Qoldiq: 0 so'm\n\n"
                "Eski hisoblar saqlanib qoldi.",
                reply_markup=keyboard()
            )

        else:
            context.user_data["confirm_new_period"] = False

            await update.message.reply_text(
                "❌ Bekor qilindi.",
                reply_markup=keyboard()
            )

        return

    # BUTTONS
    if text == "💰 Tushumlar":
        await show_income(update, context)
        return

    if text == "💸 Xarajatlar":
        await show_expenses(update, context)
        return

    if text == "📊 Hisobot":
        await show_report(update, context)
        return

    if text == "🤝 Qarzlar":
        await show_debts(update, context)
        return

    if text == "🗑 Oxirgisini o'chirish":
        await delete_last(update, context)
        return

    if text == "🔄 Yangi hisob — 0 dan":
        await new_period(update, context)
        return

    # MONEY TEXT
    print("USER", user_id, ":", text)

    items = parse_text(text)

    if not items:
        await update.message.reply_text(
            "🤔 Tushunmadim.\n\n"
            "Masalan:\n"
            "Klientdan 5 mln tushdi\n"
            "Materialga 1.2 mln ketdi\n"
            "Azizdan 3 mln qarz oldim",
            reply_markup=keyboard()
        )
        return

    saved = save_transactions(
        user_id,
        items
    )

    if not saved:
        await update.message.reply_text(
            "❌ Operatsiyani saqlab bo'lmadi.",
            reply_markup=keyboard()
        )
        return

    # Natija
    result = ""

    for item in saved:

        if item["type"] == "INCOME":
            result += (
                f"💰 Tushum: "
                f"{money(item['amount'])}\n"
            )

        elif item["type"] == "EXPENSE":
            result += (
                f"💸 {item['category']}: "
                f"{money(item['amount'])}\n"
            )

        elif item["type"] == "DEBT_IN":
            result += (
                f"🤝 Qarzdan kirim: "
                f"{money(item['amount'])}\n"
            )

        elif item["type"] == "DEBT_OUT":
            result += (
                f"🤝 Qarzga chiqim: "
                f"{money(item['amount'])}\n"
            )

    report = get_report(user_id)

    result += (
        f"\n🟢 Qoldiq: "
        f"{money(report['balance'])}"
    )

    await update.message.reply_text(
        result,
        reply_markup=keyboard()
    )


# =========================================================
# ERROR
# =========================================================

async def error_handler(update, context):
    print("BOT ERROR:", repr(context.error))


# =========================================================
# START
# =========================================================

def main():

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    app.add_error_handler(error_handler)

    if not RENDER_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL topilmadi. "
            "Render Web Service sifatida ishlayotganini tekshiring."
        )

    webhook_url = (
        f"{RENDER_URL.rstrip('/')}/{WEBHOOK_PATH}"
    )

    print("Webhook URL:", webhook_url)
    print("BOT ISHLAYAPTI...")
    print("PORT:", PORT)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
