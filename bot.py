import re
import json
import sqlite3
import asyncio
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# SOZLAMALAR
# =========================================================

import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

DB_FILE = "finance.db"


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Asosiy tranzaksiyalar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount REAL NOT NULL,
            person TEXT,
            category TEXT,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Qarzlar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            person TEXT,
            amount REAL NOT NULL
        )
    """)

    # debt_type ustuni eski DBda bo'lmasa qo'shamiz
    columns = cur.execute(
        "PRAGMA table_info(debts)"
    ).fetchall()

    names = [x["name"] for x in columns]

    if "debt_type" not in names:
        cur.execute("""
            ALTER TABLE debts
            ADD COLUMN debt_type TEXT DEFAULT 'I_OWE'
        """)

    # Hisob davrlari
    cur.execute("""
        CREATE TABLE IF NOT EXISTS finance_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Tranzaksiya -> hisob davri
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transaction_periods (
            transaction_id INTEGER PRIMARY KEY,
            period_id INTEGER NOT NULL
        )
    """)

    # Eski tranzaksiyalari bor userlar
    users = cur.execute("""
        SELECT DISTINCT user_id
        FROM transactions
    """).fetchall()

    for user in users:

        user_id = user["user_id"]

        # User uchun period bormi?
        first_period = cur.execute("""
            SELECT id
            FROM finance_periods
            WHERE user_id = ?
            ORDER BY id ASC
            LIMIT 1
        """, (user_id,)).fetchone()

        # Bo'lmasa eski ma'lumotlar uchun 1-period yaratamiz
        if not first_period:

            cur.execute("""
                INSERT INTO finance_periods (user_id)
                VALUES (?)
            """, (user_id,))

            first_period_id = cur.lastrowid

        else:

            first_period_id = first_period["id"]

        # Periodga biriktirilmagan eski tranzaksiyalarni
        # birinchi periodga biriktiramiz.
        cur.execute("""
            INSERT OR IGNORE INTO transaction_periods
            (
                transaction_id,
                period_id
            )
            SELECT
                t.id,
                ?
            FROM transactions t
            LEFT JOIN transaction_periods tp
                ON tp.transaction_id = t.id
            WHERE t.user_id = ?
            AND tp.transaction_id IS NULL
        """, (
            first_period_id,
            user_id
        ))

    conn.commit()
    conn.close()


# =========================================================
# HISOB DAVRI
# =========================================================

def get_current_period(user_id):

    conn = get_db()

    row = conn.execute("""
        SELECT id
        FROM finance_periods
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (user_id,)).fetchone()

    if not row:

        cur = conn.execute("""
            INSERT INTO finance_periods (user_id)
            VALUES (?)
        """, (user_id,))

        period_id = cur.lastrowid

        conn.commit()
        conn.close()

        return period_id

    period_id = row["id"]

    conn.close()

    return period_id


def create_new_period(user_id):

    conn = get_db()

    cur = conn.cursor()

    cur.execute("""
        INSERT INTO finance_periods (user_id)
        VALUES (?)
    """, (user_id,))

    period_id = cur.lastrowid

    conn.commit()
    conn.close()

    return period_id


# =========================================================
# PUL FORMAT
# =========================================================

def money(value):

    value = float(value)

    if value.is_integer():
        return f"{int(value):,}".replace(",", " ")

    return f"{value:,.2f}".replace(",", " ")


# =========================================================
# TRANSACTION QO'SHISH
# =========================================================

def add_transaction(
    user_id,
    kind,
    amount,
    category=None,
    note=None,
    person=None
):

    period_id = get_current_period(user_id)

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO transactions
        (
            user_id,
            kind,
            amount,
            person,
            category,
            note
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        kind,
        amount,
        person,
        category,
        note
    ))

    transaction_id = cur.lastrowid

    cur.execute("""
        INSERT INTO transaction_periods
        (
            transaction_id,
            period_id
        )
        VALUES (?, ?)
    """, (
        transaction_id,
        period_id
    ))

    conn.commit()
    conn.close()

    return transaction_id


# =========================================================
# OXIRGI TRANZAKSIYANI O'CHIRISH
# =========================================================

def delete_last_transaction(user_id):

    period_id = get_current_period(user_id)

    conn = get_db()

    row = conn.execute("""
        SELECT t.*
        FROM transactions t
        INNER JOIN transaction_periods tp
            ON tp.transaction_id = t.id
        WHERE t.user_id = ?
        AND tp.period_id = ?
        ORDER BY t.id DESC
        LIMIT 1
    """, (
        user_id,
        period_id
    )).fetchone()

    if not row:

        conn.close()

        return None

    conn.execute("""
        DELETE FROM transactions
        WHERE id = ?
    """, (row["id"],))

    conn.execute("""
        DELETE FROM transaction_periods
        WHERE transaction_id = ?
    """, (row["id"],))

    conn.commit()
    conn.close()

    return row


# =========================================================
# BALANS
# =========================================================

def get_balance(user_id):

    period_id = get_current_period(user_id)

    conn = get_db()

    rows = conn.execute("""
        SELECT t.*
        FROM transactions t
        INNER JOIN transaction_periods tp
            ON tp.transaction_id = t.id
        WHERE t.user_id = ?
        AND tp.period_id = ?
    """, (
        user_id,
        period_id
    )).fetchall()

    conn.close()

    balance = 0

    for row in rows:

        if row["kind"] == "INCOME":
            balance += float(row["amount"])

        elif row["kind"] == "EXPENSE":
            balance -= float(row["amount"])

        elif row["kind"] == "DEBT_IN":
            balance += float(row["amount"])

        elif row["kind"] == "DEBT_OUT":
            balance -= float(row["amount"])

    return balance


# =========================================================
# TRANZAKSIYALAR RO'YXATI
# =========================================================

def get_transactions(
    user_id,
    kind=None,
    days=None
):

    period_id = get_current_period(user_id)

    conn = get_db()

    query = """
        SELECT t.*
        FROM transactions t
        INNER JOIN transaction_periods tp
            ON tp.transaction_id = t.id
        WHERE t.user_id = ?
        AND tp.period_id = ?
    """

    params = [
        user_id,
        period_id
    ]

    if kind:

        query += """
            AND t.kind = ?
        """

        params.append(kind)

    if days:

        query += """
            AND datetime(t.created_at)
            >= datetime('now', ?)
        """

        params.append(
            f"-{days} days"
        )

    query += """
        ORDER BY t.id DESC
    """

    rows = conn.execute(
        query,
        params
    ).fetchall()

    conn.close()

    return rows


# =========================================================
# HISOBOT
# =========================================================

def get_report(user_id, days=None):

    period_id = get_current_period(user_id)

    conn = get_db()

    query_base = """
        FROM transactions t
        INNER JOIN transaction_periods tp
            ON tp.transaction_id = t.id
        WHERE t.user_id = ?
        AND tp.period_id = ?
    """

    params_base = [
        user_id,
        period_id
    ]

    if days:

        query_base += """
            AND datetime(t.created_at)
            >= datetime('now', ?)
        """

        params_base.append(
            f"-{days} days"
        )

    income = conn.execute(
        f"""
        SELECT COALESCE(SUM(t.amount), 0)
        {query_base}
        AND t.kind = 'INCOME'
        """,
        params_base
    ).fetchone()[0]

    expense = conn.execute(
        f"""
        SELECT COALESCE(SUM(t.amount), 0)
        {query_base}
        AND t.kind = 'EXPENSE'
        """,
        params_base
    ).fetchone()[0]

    debt_in = conn.execute(
        f"""
        SELECT COALESCE(SUM(t.amount), 0)
        {query_base}
        AND t.kind = 'DEBT_IN'
        """,
        params_base
    ).fetchone()[0]

    debt_out = conn.execute(
        f"""
        SELECT COALESCE(SUM(t.amount), 0)
        {query_base}
        AND t.kind = 'DEBT_OUT'
        """,
        params_base
    ).fetchone()[0]

    conn.close()

    balance = (
        float(income)
        - float(expense)
        + float(debt_in)
        - float(debt_out)
    )

    return (
        float(income),
        float(expense),
        float(debt_in),
        float(debt_out),
        balance
    )


# =========================================================
# SUMMA ANIQLASH
# =========================================================

def find_amounts(text):

    pattern = (
        r"(\d+(?:[.,]\d+)?)"
        r"\s*(mln|million|ming|m|k)\b"
    )

    result = []

    for match in re.finditer(
        pattern,
        text.lower()
    ):

        number = float(
            match.group(1).replace(",", ".")
        )

        unit = match.group(2)

        if unit in (
            "mln",
            "million",
            "m"
        ):

            amount = number * 1_000_000

        else:

            amount = number * 1_000

        result.append({
            "amount": amount,
            "start": match.start(),
            "end": match.end()
        })

    return result


# =========================================================
# FAST PARSER
# =========================================================

def fast_parse(text):

    text_lower = text.lower()

    # Qarzga oid gaplarni Groqga yuboramiz
    debt_words = [
        "qarz",
        "qarzdor",
        "qarzim",
        "qarzman",
        "qarz oldim",
        "qarz berdim",
        "qarz qaytardim",
        "qarzni qaytardim"
    ]

    if any(
        word in text_lower
        for word in debt_words
    ):

        return []

    amounts = find_amounts(text)

    if not amounts:
        return []

    results = []

    income_words = [
        "tushdi",
        "tushum",
        "keldi",
        "daromad",
        "kirim",
        "oldim",
        "berdi",
        "berishdi"
    ]

    expense_words = [
        "ketdi",
        "chiqdi",
        "sarfladim",
        "sarflandi",
        "to'ladim",
        "toladim",
        "xarajat",
        "materialga",
        "ishchiga",
        "ijaraga",
        "berdim",
        "sotib oldim"
    ]

    for item in amounts:

        start = max(
            0,
            item["start"] - 100
        )

        end = min(
            len(text_lower),
            item["end"] + 100
        )

        area = text_lower[
            start:end
        ]

        kind = None

        if any(
            word in area
            for word in expense_words
        ):

            kind = "EXPENSE"

        elif any(
            word in area
            for word in income_words
        ):

            kind = "INCOME"

        if not kind:
            continue

        before = text[
            :item["start"]
        ]

        before = re.sub(
            r"\b(bugun|kecha|menga|men|pul)\b",
            "",
            before,
            flags=re.I
        )

        before = re.sub(
            r"\s+",
            " ",
            before
        ).strip(" ,.-")

        if not before:

            category = (
                "Tushum"
                if kind == "INCOME"
                else "Xarajat"
            )

        else:

            words = before.split()

            category = " ".join(
                words[-5:]
            )

        results.append({
            "type": kind,
            "amount": item["amount"],
            "category": category[:100],
            "note": text[:300]
        })

    return results


# =========================================================
# GROQ PARSER
# =========================================================

def groq_parse_sync(text):

    if not GROQ_API_KEY:
        return []

    system_prompt = """
Sen MyFinance AI moliyaviy botisan.

Foydalanuvchi o'zbek tilida pul harakatlarini yozadi.

MUHIM:

1. Oddiy klientdan kelgan pul:
INCOME

2. Oddiy xarajat:
EXPENSE

3. Qarzga oid real pul harakati:

Birovdan qarz oldim:
DEBT_IN

Birovga qarz berdim:
DEBT_OUT

Oldin olgan qarzimni qaytardim:
DEBT_OUT

Menga bergan qarzini qaytardi:
DEBT_IN

Qarzning o'zi INCOME yoki EXPENSE emas.

Lekin haqiqiy pul kirsa yoki chiqsa balansga ta'sir qiladi.

Misollar:

"Klientdan 5 mln tushdi"
INCOME 5000000

"Materialga 1.2 mln ketdi"
EXPENSE 1200000

"Azizdan 3 mln qarz oldim"
DEBT_IN 3000000

"Valiga 500 ming qarz berdim"
DEBT_OUT 500000

"Aziz qarzini 3 mln qaytardi"
DEBT_IN 3000000

"Azizga olgan 3 mln qarzimni qaytardim"
DEBT_OUT 3000000

Agar:
"Azizdan 5 mln qarzim bor"

Bu real pul harakati emas.
Transaction yaratma.

Summalar:

5 mln = 5000000
1.2 mln = 1200000
800 ming = 800000
500 ming = 500000

Bir gapda bir nechta operatsiya bo'lsa,
ularni alohida qaytar.

category qisqa va tushunarli bo'lsin.

Personni ham aniqlashga harakat qil:
Azizdan -> Aziz
Valiga -> Vali
Azizga -> Aziz

Faqat JSON qaytar:

{
  "transactions": [
    {
      "type": "INCOME",
      "amount": 5000000,
      "category": "Klientdan",
      "person": null,
      "note": "..."
    }
  ]
}

type faqat:

INCOME
EXPENSE
DEBT_IN
DEBT_OUT

bo'lishi mumkin.

Hech qanday markdown yozma.
Faqat JSON.
"""

    payload = {
        "model": GROQ_MODEL,

        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": text
            }
        ],

        "temperature": 0,

        "response_format": {
            "type": "json_object"
        },

        "max_tokens": 800
    }

    headers = {
        "Authorization":
            f"Bearer {GROQ_API_KEY}",

        "Content-Type":
            "application/json"
    }

    try:

        response = requests.post(
            GROQ_URL,
            headers=headers,
            json=payload,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        content = data[
            "choices"
        ][0][
            "message"
        ][
            "content"
        ]

        parsed = json.loads(content)

        transactions = parsed.get(
            "transactions",
            []
        )

        clean = []

        for item in transactions:

            transaction_type = item.get(
                "type"
            )

            if transaction_type not in (
                "INCOME",
                "EXPENSE",
                "DEBT_IN",
                "DEBT_OUT"
            ):
                continue

            try:

                amount = float(
                    item.get(
                        "amount",
                        0
                    )
                )

            except:

                continue

            if amount <= 0:
                continue

            person = item.get(
                "person"
            )

            if person:
                person = str(
                    person
                ).strip()[:100]

            clean.append({
                "type": transaction_type,

                "amount": amount,

                "category": str(
                    item.get(
                        "category"
                    )
                    or "Operatsiya"
                )[:100],

                "person": person,

                "note": str(
                    item.get(
                        "note"
                    )
                    or text
                )[:300]
            })

        return clean

    except Exception as e:

        print(
            "GROQ ERROR:",
            repr(e)
        )

        return []


async def groq_parse(text):

    return await asyncio.to_thread(
        groq_parse_sync,
        text
    )


# =========================================================
# QARZNI SAQLASH
# =========================================================

def update_debt(
    user_id,
    person,
    transaction_type,
    amount
):

    if not person:
        person = "Noma'lum"

    conn = get_db()

    # Men qarz oldim
    # -> men unga qarzman
    if transaction_type == "DEBT_IN":

        debt_type = "I_OWE"

    # Men qarz berdim
    # -> u menga qarz
    else:

        debt_type = "OWES_ME"

    # Mavjud qarzni topamiz
    row = conn.execute("""
        SELECT id, amount
        FROM debts
        WHERE user_id = ?
        AND person = ?
        AND debt_type = ?
        ORDER BY id DESC
        LIMIT 1
    """, (
        user_id,
        person,
        debt_type
    )).fetchone()

    if row:

        new_amount = (
            float(row["amount"])
            + float(amount)
        )

        conn.execute("""
            UPDATE debts
            SET amount = ?
            WHERE id = ?
        """, (
            new_amount,
            row["id"]
        ))

    else:

        conn.execute("""
            INSERT INTO debts
            (
                user_id,
                person,
                amount,
                debt_type
            )
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            person,
            amount,
            debt_type
        ))

    conn.commit()
    conn.close()


# =========================================================
# QARZNI QAYTARISH
# =========================================================

def repay_debt(
    user_id,
    person,
    transaction_type,
    amount
):

    if not person:
        person = "Noma'lum"

    conn = get_db()

    # DEBT_OUT:
    # Avval men qarzman -> I_OWE kamayadi
    #
    # DEBT_IN:
    # U menga qarz -> OWES_ME kamayadi

    if transaction_type == "DEBT_OUT":

        debt_type = "I_OWE"

    else:

        debt_type = "OWES_ME"

    row = conn.execute("""
        SELECT id, amount
        FROM debts
        WHERE user_id = ?
        AND person = ?
        AND debt_type = ?
        ORDER BY id ASC
        LIMIT 1
    """, (
        user_id,
        person,
        debt_type
    )).fetchone()

    if not row:

        conn.close()

        return

    current = float(
        row["amount"]
    )

    remaining = current - float(amount)

    if remaining <= 0:

        conn.execute("""
            DELETE FROM debts
            WHERE id = ?
        """, (
            row["id"],
        ))

    else:

        conn.execute("""
            UPDATE debts
            SET amount = ?
            WHERE id = ?
        """, (
            remaining,
            row["id"]
        ))

    conn.commit()
    conn.close()


# =========================================================
# KLAVIATURA
# =========================================================

def keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "💰 Tushumlar",
                callback_data="income"
            ),

            InlineKeyboardButton(
                "💸 Xarajatlar",
                callback_data="expense"
            )
        ],

        [
            InlineKeyboardButton(
                "📊 Hisobot",
                callback_data="report"
            ),

            InlineKeyboardButton(
                "🤝 Qarzlar",
                callback_data="debts"
            )
        ],

        [
            InlineKeyboardButton(
                "🗑 Oxirgisini o‘chirish",
                callback_data="delete"
            )
        ],

        [
            InlineKeyboardButton(
                "🔄 Yangi hisob — 0 dan",
                callback_data="new_period"
            )
        ]

    ])


# =========================================================
# START
# =========================================================

async def start(update, context):

    user_id = update.effective_user.id

    # Userda hali period bo'lmasa yaratadi.
    # MUHIM: yangi period ochmaydi.
    get_current_period(user_id)

    await update.message.reply_text(
        """
💰 MyFinance AI

Pul kirimi va xarajatlaringni yozaver.

Masalan:

Klientdan 5 mln tushdi

Materialga 1.2 mln ketdi

Ishchiga 800 ming berdim

Qarz:

Azizdan 3 mln qarz oldim

Valiga 500 ming qarz berdim

Aziz qarzini 500 ming qaytardi

Eski hisobni saqlagan holda 0 dan boshlash uchun:

🔄 Yangi hisob — 0 dan
        """,
        reply_markup=keyboard()
    )


# =========================================================
# TRANZAKSIYALARNI SAQLASH
# =========================================================

async def save_transactions(
    update,
    transactions
):

    user_id = update.effective_user.id

    lines = []

    for item in transactions:

        transaction_type = item["type"]

        person = item.get(
            "person"
        )

        # Oddiy transaction
        add_transaction(
            user_id=user_id,
            kind=transaction_type,
            amount=item["amount"],
            category=item["category"],
            note=item["note"],
            person=person
        )

        # Qarz bazasini yangilash
        if transaction_type == "DEBT_IN":

            # Bu yangi qarzmi yoki qaytarilgan qarzmi?
            text_lower = item["note"].lower()

            repayment_words = [
                "qaytardi",
                "qaytardi",
                "qaytarib berdi",
                "qarzini berdi",
                "qarzini qaytardi"
            ]

            if any(
                word in text_lower
                for word in repayment_words
            ):

                repay_debt(
                    user_id,
                    person,
                    "DEBT_IN",
                    item["amount"]
                )

            else:

                update_debt(
                    user_id,
                    person,
                    "DEBT_IN",
                    item["amount"]
                )

        elif transaction_type == "DEBT_OUT":

            text_lower = item["note"].lower()

            repayment_words = [
                "qaytardim",
                "qaytarib berdim",
                "qarzimni qaytardim",
                "qarzni qaytardim"
            ]

            if any(
                word in text_lower
                for word in repayment_words
            ):

                repay_debt(
                    user_id,
                    person,
                    "DEBT_OUT",
                    item["amount"]
                )

            else:

                update_debt(
                    user_id,
                    person,
                    "DEBT_OUT",
                    item["amount"]
                )

        # Javob
        if transaction_type == "INCOME":

            lines.append(
                f"💰 {item['category']} — "
                f"{money(item['amount'])} so'm"
            )

        elif transaction_type == "EXPENSE":

            lines.append(
                f"💸 {item['category']} — "
                f"{money(item['amount'])} so'm"
            )

        elif transaction_type == "DEBT_IN":

            lines.append(
                f"🤝 Qarzdan kirim — "
                f"{money(item['amount'])} so'm"
            )

        elif transaction_type == "DEBT_OUT":

            lines.append(
                f"🤝 Qarzga chiqim — "
                f"{money(item['amount'])} so'm"
            )

    balance = get_balance(
        user_id
    )

    lines.append("")

    lines.append(
        f"🟢 Qoldiq: "
        f"{money(balance)} so'm"
    )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=keyboard()
    )


# =========================================================
# XABAR
# =========================================================

async def message_handler(
    update,
    context
):

    text = update.message.text.strip()

    if not text:
        return

    print(
        f"USER {update.effective_user.id}: "
        f"{text}"
    )

    # Avval tezkor parser
    transactions = fast_parse(text)

    if transactions:

        print(
            "FAST:",
            transactions
        )

        await save_transactions(
            update,
            transactions
        )

        return

    # Keyin Groq
    if GROQ_API_KEY:

        msg = await update.message.reply_text(
            "⏳ Tushunib olayapman..."
        )

        transactions = await groq_parse(
            text
        )

        try:
            await msg.delete()
        except:
            pass

        if transactions:

            print(
                "GROQ:",
                transactions
            )

            await save_transactions(
                update,
                transactions
            )

            return

    await update.message.reply_text(
        "❓ Tushunmadim.\n\n"
        "Masalan:\n"
        "Klientdan 5 mln tushdi\n"
        "Materialga 1.2 mln ketdi\n"
        "Azizdan 3 mln qarz oldim",
        reply_markup=keyboard()
    )


# =========================================================
# TUSHUMLAR
# =========================================================

async def show_income(query):

    rows = get_transactions(
        query.from_user.id,
        kind="INCOME",
        days=30
    )

    if not rows:

        await query.edit_message_text(
            "💰 Tushumlar yo‘q.",
            reply_markup=keyboard()
        )

        return

    lines = [
        "💰 TUSHUMLAR — 30 KUN",
        ""
    ]

    total = 0

    for row in rows[:50]:

        amount = float(
            row["amount"]
        )

        category = (
            row["category"]
            or "Tushum"
        )

        total += amount

        lines.append(
            f"• {category} — "
            f"{money(amount)} so'm"
        )

    lines.append("")

    lines.append(
        f"Jami: "
        f"{money(total)} so'm"
    )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=keyboard()
    )


# =========================================================
# XARAJATLAR
# =========================================================

async def show_expenses(query):

    rows = get_transactions(
        query.from_user.id,
        kind="EXPENSE",
        days=30
    )

    if not rows:

        await query.edit_message_text(
            "💸 Xarajatlar yo‘q.",
            reply_markup=keyboard()
        )

        return

    lines = [
        "💸 XARAJATLAR — 30 KUN",
        ""
    ]

    total = 0

    for row in rows[:50]:

        amount = float(
            row["amount"]
        )

        category = (
            row["category"]
            or "Xarajat"
        )

        total += amount

        lines.append(
            f"• {category} — "
            f"{money(amount)} so'm"
        )

    lines.append("")

    lines.append(
        f"Jami: "
        f"{money(total)} so'm"
    )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=keyboard()
    )


# =========================================================
# HISOBOT
# =========================================================

async def show_report(query):

    (
        income,
        expense,
        debt_in,
        debt_out,
        balance
    ) = get_report(
        query.from_user.id,
        days=30
    )

    text = (
        "📊 HISOBOT — 30 KUN\n\n"

        f"💰 Tushum: "
        f"{money(income)} so'm\n"

        f"💸 Xarajat: "
        f"{money(expense)} so'm\n\n"

        f"🤝 Qarzdan kirim: "
        f"{money(debt_in)} so'm\n"

        f"🤝 Qarzga chiqim: "
        f"{money(debt_out)} so'm\n\n"

        f"🟢 Qoldiq: "
        f"{money(balance)} so'm"
    )

    await query.edit_message_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# QARZLAR
# =========================================================

def get_debts(user_id):

    conn = get_db()

    rows = conn.execute("""
        SELECT
            person,
            debt_type,
            SUM(amount) AS total
        FROM debts
        WHERE user_id = ?
        AND debt_type IN (
            'I_OWE',
            'OWES_ME'
        )
        GROUP BY person, debt_type
    """, (
        user_id,
    )).fetchall()

    conn.close()

    people = {}

    for row in rows:

        person = (
            row["person"]
            or "Noma'lum"
        )

        if person not in people:

            people[person] = {
                "I_OWE": 0,
                "OWES_ME": 0
            }

        people[
            person
        ][
            row["debt_type"]
        ] = float(
            row["total"]
        )

    return people


async def show_debts(query):

    people = get_debts(
        query.from_user.id
    )

    if not people:

        await query.edit_message_text(
            "🤝 Qarzlar yo‘q.",
            reply_markup=keyboard()
        )

        return

    lines = [
        "🤝 QARZLAR",
        ""
    ]

    for person, data in people.items():

        i_owe = data["I_OWE"]
        owes_me = data["OWES_ME"]

        if i_owe:

            lines.append(
                f"🔴 {person}: "
                f"men {money(i_owe)} "
                f"so'm qarzman"
            )

        if owes_me:

            lines.append(
                f"🟢 {person}: "
                f"menga {money(owes_me)} "
                f"so'm qarz"
            )

        net = owes_me - i_owe

        if net > 0:

            lines.append(
                f"   → {person} menga "
                f"{money(net)} so'm qarz"
            )

        elif net < 0:

            lines.append(
                f"   → Men {person}ga "
                f"{money(abs(net))} "
                f"so'm qarzman"
            )

        lines.append("")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=keyboard()
    )


# =========================================================
# O'CHIRISH
# =========================================================

async def delete_last(query):

    row = delete_last_transaction(
        query.from_user.id
    )

    if not row:

        await query.edit_message_text(
            "🗑 O‘chirish uchun "
            "operatsiya yo‘q.",
            reply_markup=keyboard()
        )

        return

    balance = get_balance(
        query.from_user.id
    )

    await query.edit_message_text(
        "🗑 Oxirgi operatsiya "
        "o‘chirildi.\n\n"

        f"🟢 Qoldiq: "
        f"{money(balance)} so'm",

        reply_markup=keyboard()
    )


# =========================================================
# YANGI HISOB
# =========================================================

async def new_period_confirm(query):

    keyboard_confirm = InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "✅ Ha, 0 dan boshlash",
                callback_data="new_period_yes"
            )
        ],

        [
            InlineKeyboardButton(
                "❌ Bekor qilish",
                callback_data="new_period_no"
            )
        ]

    ])

    await query.edit_message_text(
        "⚠️ YANGI HISOB\n\n"

        "Hozirgi hisob 0 dan boshlanadi.\n\n"

        "Eski ma'lumotlar o‘chirilmaydi. "
        "Faqat yangi hisob davri ochiladi.\n\n"

        "Davom etamizmi?",

        reply_markup=keyboard_confirm
    )


async def new_period_yes(query):

    create_new_period(
        query.from_user.id
    )

    await query.edit_message_text(
        "✅ Yangi hisob boshlandi.\n\n"

        "💰 Tushum: 0 so'm\n"

        "💸 Xarajat: 0 so'm\n"

        "🤝 Qarz harakati: 0 so'm\n\n"

        "🟢 Qoldiq: 0 so'm",

        reply_markup=keyboard()
    )


# =========================================================
# BUTTON
# =========================================================

async def button_handler(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    if query.data == "income":

        await show_income(query)

    elif query.data == "expense":

        await show_expenses(query)

    elif query.data == "report":

        await show_report(query)

    elif query.data == "debts":

        await show_debts(query)

    elif query.data == "delete":

        await delete_last(query)

    elif query.data == "new_period":

        await new_period_confirm(query)

    elif query.data == "new_period_yes":

        await new_period_yes(query)

    elif query.data == "new_period_no":

        await query.edit_message_text(
            "❌ Bekor qilindi.",
            reply_markup=keyboard()
        )


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update,
    context
):

    print(
        "BOT ERROR:",
        repr(context.error)
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print("=" * 50)
    print("MyFinance AI")
    print("=" * 50)

    if "BU_YERGA" in BOT_TOKEN:

        print(
            "XATO: BOT_TOKEN ni "
            "bot.py ichiga kiriting!"
        )

        return

    if "BU_YERGA" in GROQ_API_KEY:

        print(
            "XATO: GROQ_API_KEY ni "
            "bot.py ichiga kiriting!"
        )

        return

    print("Groq: YOQILGAN")
    print(f"Model: {GROQ_MODEL}")

    init_db()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            message_handler
        )
    )

    app.add_error_handler(
        error_handler
    )

    print("BOT ISHLAYAPTI...")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()