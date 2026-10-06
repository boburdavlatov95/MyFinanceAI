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
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

GROQ_MODEL = "openai/gpt-oss-20b"
WHISPER_MODEL = "whisper-large-v3-turbo"

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL topilmadi")

# =========================================================
# KATEGORIYALAR
# =========================================================

CATEGORY_HOME = "Uy"
CATEGORY_WORK = "Ish"
CATEGORY_PERSONAL = "Shaxsiy"
CATEGORY_FAMILY = "Oila"
CATEGORY_OTHER = "Boshqa"

CATEGORY_DISPLAY = {
    "Uy": "🏠 Uy",
    "Ish": "💼 Ish",
    "Shaxsiy": "👤 Shaxsiy",
    "Oila": "👨‍👩‍‍👧 Oila",
    "Boshqa": "📦 Boshqa",
}

def category_display(category):
    return CATEGORY_DISPLAY.get(
        category,
        f"📦 {category or 'Boshqa'}"
    )

# =========================================================
# DATABASE
# =========================================================

def get_conn():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row
    )

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
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
                    debt_action TEXT DEFAULT 'NONE',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                ALTER TABLE transactions
                ADD COLUMN IF NOT EXISTS debt_action TEXT DEFAULT 'NONE'
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

def get_current_period(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title
                FROM finance_periods
                WHERE user_id = %s
                ORDER BY id DESC
                LIMIT 1
            """, (user_id,))

            row = cur.fetchone()
            if row:
                return row

            cur.execute("""
                INSERT INTO finance_periods (user_id, title)
                VALUES (%s, %s)
                RETURNING id, title
            """, (user_id, "Hisob"))

            row = cur.fetchone()
            conn.commit()
            return row

def create_new_period(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO finance_periods (user_id, title)
                VALUES (%s, %s)
                RETURNING id, title
            """, (user_id, "Yangi hisob"))

            row = cur.fetchone()
            conn.commit()
            return row

# =========================================================
# FORMAT VA NORMALIZE
# =========================================================

def money(value):
    try:
        value = float(value)
    except Exception:
        value = 0

    if abs(value - round(value)) < 0.01:
        value = int(round(value))
        return f"{value:,}".replace(",", " ")

    return f"{value:,.2f}".replace(",", " ")

def clean_person(person):
    if person is None:
        return None

    person = str(person).strip()
    # Apostroflarni bir xillashtirish (SQL va izlashda xatolik bermasligi uchun)
    person = re.sub(r"['`’‘ʻ]", "’", person)

    if not person:
        return None

    bad = {
        "",
        "noma'lum",
        "noma’lum",
        "nomalum",
        "unknown",
        "null",
        "none",
    }

    if person.lower() in bad:
        return None

    return person

def normalize_category(category):
    if not category:
        return CATEGORY_OTHER

    c = str(category).strip().lower()

    mapping = {
        "uy": CATEGORY_HOME,
        "🏠 uy": CATEGORY_HOME,
        "ish": CATEGORY_WORK,
        "💼 ish": CATEGORY_WORK,
        "shaxsiy": CATEGORY_PERSONAL,
        "👤 shaxsiy": CATEGORY_PERSONAL,
        "oila": CATEGORY_FAMILY,
        "👨‍👩‍👧 oila": CATEGORY_FAMILY,
        "boshqa": CATEGORY_OTHER,
        "📦 boshqa": CATEGORY_OTHER,
    }

    return mapping.get(c, CATEGORY_OTHER)

# =========================================================
# AMOUNT PARSE (Regex to'g'rilandi)
# =========================================================

def parse_amount(text):
    if not text:
        return None

    t = str(text).lower()

    # 1. 2.5 mln / 2,5 mln / 2 million
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(mln|million|millon|миллион|млн)', t, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ".")) * 1_000_000

    # 2. 450 ming / 450 min / 450 мин
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ming|min|минг|мин)', t, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ".")) * 1_000

    # 3. 700 so'm / sum / som
    m = re.search(r'(\d+(?:[\s.,]\d+)*)\s*(so.?m|som|sum|сум)', t, re.IGNORECASE)
    if m:
        raw = re.sub(r'[\s.,]', '', m.group(1))
        try:
            return float(raw)
        except Exception:
            pass

    # 4. Katta sonlar (masalan: 2 500 000 yoki 2.500.000)
    m = re.search(r'(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+)(?!\d)', t)
    if m:
        raw = re.sub(r'[\s.,]', '', m.group(1))
        try:
            return float(raw)
        except Exception:
            pass

    # 5. Oddiy son (masalan: 50000)
    m = re.search(r'(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)', t)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except Exception:
            pass

    return None

# =========================================================
# PERSON TOPISH
# =========================================================

def guess_person(text):
    if not text:
        return None

    patterns = [
        r'^\s*([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)\s+\d',
        r'\b([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)ga\b',
        r'\b([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)dan\b',
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            person = clean_person(m.group(1))
            if person:
                return person

    return None

# =========================================================
# LOCAL FALLBACK
# =========================================================

def fast_repayment_parse(text):
    t = text.lower().strip()
    patterns = [
        r'qarzini\s+berdi',
        r'qarzini\s+qaytardi',
        r'qarzini\s+to.?ladi',
        r'qarzidan\s+.+\s+berdi',
        r'qarzidan\s+.+\s+qaytardi',
    ]

    found = any(re.search(pattern, t, re.IGNORECASE) for pattern in patterns)
    if not found:
        return None

    amount = parse_amount(text)
    if amount is None:
        return None

    person = guess_person(text)
    if not person:
        return None

    return {
        "transactions": [
            {
                "type": "INCOME",
                "amount": amount,
                "person": person,
                "category": "Boshqa",
                "note": text,
                "debt_action": "REPAY"
            }
        ],
        "debts": []
    }

def fast_parse(text):
    if not text:
        return None

    t = text.lower().strip()
    repayment = fast_repayment_parse(text)
    if repayment:
        return repayment

    amount = parse_amount(text)
    if amount is None:
        return None

    person = guess_person(text)

    # QARZ
    if "qarz" in t:
        if "qarz oldim" in t or "qarz oldi" in t:
            return {
                "transactions": [
                    {
                        "type": "DEBT_IN",
                        "amount": amount,
                        "person": person,
                        "category": CATEGORY_OTHER,
                        "note": text,
                        "debt_action": "DEBT_IN"
                    }
                ],
                "debts": []
            }

        if "qarz berdim" in t or "qarz berdi" in t:
            return {
                "transactions": [
                    {
                        "type": "DEBT_OUT",
                        "amount": amount,
                        "person": person,
                        "category": CATEGORY_OTHER,
                        "note": text,
                        "debt_action": "DEBT_OUT"
                    }
                ],
                "debts": []
            }

        if "qarzdor" in t or t.endswith("qarz") or " qarz " in f" {t} ":
            if person:
                return {
                    "transactions": [],
                    "debts": [
                        {
                            "action": "ADD",
                            "person": person,
                            "amount": amount,
                            "debt_type": "OWES_ME"
                        }
                    ]
                }

    # KATEGORIYALAR
    category = CATEGORY_OTHER
    home_words = ["uyga", "uy uchun", "svet", "elektr", "gaz", "suv", "kommunal", "internet"]
    work_words = ["ishxona", "ish uchun", "ishga", "benzin", "zapchast", "moy", "mashina", "yol kira", "taksi", "ishchi", "abet", "ujen", "material", "reklama"]
    family_words = ["onam", "otam", "akam", "ukam", "opam", "singlim", "xotinim", "erim", "farzandim", "bolam", "oilam"]
    personal_words = ["o'zim", "ozim", "shaxsiy"]

    if any(word in t for word in family_words):
        category = CATEGORY_FAMILY
    elif any(word in t for word in personal_words):
        category = CATEGORY_PERSONAL
    elif any(word in t for word in work_words):
        category = CATEGORY_WORK
    elif any(word in t for word in home_words):
        category = CATEGORY_HOME

    # TUSHUM
    income_words = ["tushdi", "tushum", "keldi", "oldim", "daromad", "topdim", "berdi", "klent berdi", "mijoz berdi"]
    if any(word in t for word in income_words):
        return {
            "transactions": [
                {
                    "type": "INCOME",
                    "amount": amount,
                    "person": person,
                    "category": category,
                    "note": text,
                    "debt_action": "NONE"
                }
            ],
            "debts": []
        }

    # XARAJAT
    expense_words = ["ishlatdim", "sarfladim", "xarajat", "ketdi", "to'ladim", "toladim", "sotib oldim"]
    if any(word in t for word in expense_words):
        return {
            "transactions": [
                {
                    "type": "EXPENSE",
                    "amount": amount,
                    "person": person,
                    "category": category,
                    "note": text,
                    "debt_action": "NONE"
                }
            ],
            "debts": []
        }

    return None

# =========================================================
# GROQ AI (JSON Strict Format bilan)
# =========================================================

def groq_parse(text):
    if not GROQ_API_KEY:
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"

    system_prompt = r"""
Sen MyFinance AI moliya botining ASOSIY AI parserisan.
FOYDALANUVCHINING HAR BIR PULGA OID XABARINI TAHLIL QILIB, FAQAT VALID JSON QAYTAR.

FORMAT:
{
  "transactions": [
    {
      "type": "INCOME" | "EXPENSE" | "DEBT_IN" | "DEBT_OUT",
      "amount": number,
      "person": string | null,
      "category": "Uy" | "Ish" | "Shaxsiy" | "Oila" | "Boshqa",
      "note": string,
      "debt_action": "NONE" | "REPAY" | "DEBT_IN" | "DEBT_OUT"
    }
  ],
  "debts": [
    {
      "action": "ADD" | "REPAY",
      "person": string,
      "amount": number,
      "debt_type": "OWES_ME" | "I_OWE"
    }
  ]
}
"""

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},  # GROQ avtomatik JSON beradi
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    }

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
        return json.loads(content)
    except Exception as e:
        print("GROQ ERROR:", e)
        return None

# =========================================================
# AI NATIJASINI TOZALASH
# =========================================================

def normalize_ai_result(parsed, source_text):
    if not parsed or not isinstance(parsed, dict):
        return None

    transactions = parsed.get("transactions", [])
    debts = parsed.get("debts", [])

    if not isinstance(transactions, list):
        transactions = []
    if not isinstance(debts, list):
        debts = []

    clean_transactions = []
    for tx in transactions:
        if not isinstance(tx, dict):
            continue

        tx_type = str(tx.get("type", "")).upper().strip()
        if tx_type not in {"INCOME", "EXPENSE", "DEBT_IN", "DEBT_OUT"}:
            continue

        try:
            amount = float(tx.get("amount", 0))
        except Exception:
            amount = 0

        if amount <= 0:
            continue

        person = clean_person(tx.get("person"))
        category = normalize_category(tx.get("category"))
        note = tx.get("note") or source_text or ""
        debt_action = str(tx.get("debt_action", "NONE")).upper().strip()

        if tx_type == "DEBT_IN":
            debt_action = "DEBT_IN"
            category = CATEGORY_OTHER
        elif tx_type == "DEBT_OUT":
            debt_action = "DEBT_OUT"
            category = CATEGORY_OTHER
        elif debt_action not in {"NONE", "REPAY"}:
            debt_action = "NONE"

        clean_transactions.append({
            "type": tx_type,
            "amount": amount,
            "person": person,
            "category": category,
            "note": note,
            "debt_action": debt_action
        })

    clean_debts = []
    for debt in debts:
        if not isinstance(debt, dict):
            continue

        action = str(debt.get("action", "")).upper().strip()
        if action not in {"ADD", "REPAY"}:
            continue

        person = clean_person(debt.get("person"))
        if not person:
            continue

        try:
            amount = float(debt.get("amount", 0))
        except Exception:
            amount = 0

        if amount <= 0:
            continue

        debt_type = str(debt.get("debt_type", "")).upper().strip()
        if debt_type not in {"OWES_ME", "I_OWE"}:
            continue

        clean_debts.append({
            "action": action,
            "person": person,
            "amount": amount,
            "debt_type": debt_type
        })

    return {
        "transactions": clean_transactions,
        "debts": clean_debts
    }

def parse_text(text):
    ai_result = groq_parse(text)
    if ai_result:
        normalized = normalize_ai_result(ai_result, text)
        if normalized and (normalized["transactions"] or normalized["debts"]):
            return normalized

    repayment = fast_repayment_parse(text)
    if repayment:
        return repayment

    return fast_parse(text)

# =========================================================
# DEBT DATABASE
# =========================================================

def add_debt_db(cur, user_id, period_id, person, amount, debt_type):
    person = clean_person(person)
    if not person or amount <= 0:
        return

    cur.execute("""
        SELECT id, amount
        FROM debts
        WHERE user_id = %s
          AND period_id = %s
          AND LOWER(TRIM(person)) = LOWER(TRIM(%s))
          AND debt_type = %s
        ORDER BY id DESC
        LIMIT 1
    """, (user_id, period_id, person, debt_type))

    row = cur.fetchone()
    if row:
        cur.execute("""
            UPDATE debts
            SET amount = amount + %s
            WHERE id = %s
        """, (amount, row["id"]))
    else:
        cur.execute("""
            INSERT INTO debts (user_id, period_id, person, amount, debt_type)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, period_id, person, amount, debt_type))

def subtract_debt_db(cur, user_id, period_id, person, amount, debt_type):
    person = clean_person(person)
    if not person or amount <= 0:
        return

    remaining = float(amount)
    cur.execute("""
        SELECT id, amount
        FROM debts
        WHERE user_id = %s
          AND period_id = %s
          AND LOWER(TRIM(person)) = LOWER(TRIM(%s))
          AND debt_type = %s
        ORDER BY id ASC
    """, (user_id, period_id, person, debt_type))

    rows = cur.fetchall()
    for row in rows:
        if remaining <= 0:
            break

        current = float(row["amount"] or 0)
        if current <= remaining:
            remaining -= current
            cur.execute("DELETE FROM debts WHERE id = %s", (row["id"],))
        else:
            new_amount = current - remaining
            remaining = 0
            cur.execute("UPDATE debts SET amount = %s WHERE id = %s", (new_amount, row["id"]))

# =========================================================
# SAVE DATA & BALANCE
# =========================================================

def save_data(user_id, parsed, source_text=None):
    period = get_current_period(user_id)
    period_id = period["id"]

    transactions = parsed.get("transactions", [])
    debts = parsed.get("debts", [])

    with get_conn() as conn:
        with conn.cursor() as cur:
            for tx in transactions:
                tx_type = str(tx.get("type", "")).upper().strip()
                if tx_type not in {"INCOME", "EXPENSE", "DEBT_IN", "DEBT_OUT"}:
                    continue

                try:
                    amount = float(tx.get("amount", 0))
                except Exception:
                    amount = 0

                if amount <= 0:
                    continue

                person = clean_person(tx.get("person"))
                category = normalize_category(tx.get("category"))
                note = tx.get("note") or source_text or ""
                debt_action = str(tx.get("debt_action", "NONE")).upper().strip()

                if tx_type == "DEBT_IN":
                    db_kind = "INCOME"
                    debt_action = "DEBT_IN"
                    category = CATEGORY_OTHER
                elif tx_type == "DEBT_OUT":
                    db_kind = "EXPENSE"
                    debt_action = "DEBT_OUT"
                    category = CATEGORY_OTHER
                else:
                    db_kind = tx_type

                cur.execute("""
                    INSERT INTO transactions (
                        user_id, period_id, kind, amount, person, category, note, debt_action
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (user_id, period_id, db_kind, amount, person, category, note, debt_action))

                if debt_action == "DEBT_IN" and person:
                    add_debt_db(cur, user_id, period_id, person, amount, "I_OWE")
                elif debt_action == "DEBT_OUT" and person:
                    add_debt_db(cur, user_id, period_id, person, amount, "OWES_ME")
                elif debt_action == "REPAY" and person:
                    debt_type = "OWES_ME" if db_kind == "INCOME" else "I_OWE"
                    subtract_debt_db(cur, user_id, period_id, person, amount, debt_type)

            for debt in debts:
                action = str(debt.get("action", "")).upper().strip()
                person = clean_person(debt.get("person"))
                if not person:
                    continue

                try:
                    amount = float(debt.get("amount", 0))
                except Exception:
                    amount = 0

                if amount <= 0:
                    continue

                debt_type = str(debt.get("debt_type", "")).upper().strip()
                if debt_type not in {"OWES_ME", "I_OWE"}:
                    continue

                if action == "ADD":
                    add_debt_db(cur, user_id, period_id, person, amount, debt_type)
                elif action == "REPAY":
                    subtract_debt_db(cur, user_id, period_id, person, amount, debt_type)

        conn.commit()

    return period

def get_balance(user_id, period_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(
                    CASE WHEN kind = 'INCOME' THEN amount
                         WHEN kind = 'EXPENSE' THEN -amount
                         ELSE 0 END
                ), 0) AS balance
                FROM transactions
                WHERE user_id = %s AND period_id = %s
            """, (user_id, period_id))
            row = cur.fetchone()
            return float(row["balance"] or 0)

def get_report(user_id):
    period = get_current_period(user_id)
    period_id = period["id"]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    COALESCE(SUM(CASE WHEN kind = 'INCOME' THEN amount ELSE 0 END), 0) AS income,
                    COALESCE(SUM(CASE WHEN kind = 'EXPENSE' THEN amount ELSE 0 END), 0) AS expense
                FROM transactions
                WHERE user_id = %s AND period_id = %s
            """, (user_id, period_id))
            row = cur.fetchone()

    income = float(row["income"] or 0)
    expense = float(row["expense"] or 0)

    return {
        "period": period,
        "income": income,
        "expense": expense,
        "balance": income - expense
    }

def get_transactions(user_id, kind=None, limit=100):
    period = get_current_period(user_id)
    period_id = period["id"]

    with get_conn() as conn:
        with conn.cursor() as cur:
            if kind:
                cur.execute("""
                    SELECT * FROM transactions
                    WHERE user_id = %s AND period_id = %s AND kind = %s
                    ORDER BY id DESC LIMIT %s
                """, (user_id, period_id, kind, limit))
            else:
                cur.execute("""
                    SELECT * FROM transactions
                    WHERE user_id = %s AND period_id = %s
                    ORDER BY id DESC LIMIT %s
                """, (user_id, period_id, limit))
            return cur.fetchall()

def get_debtors(user_id):
    period = get_current_period(user_id)
    period_id = period["id"]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT person, SUM(amount) AS amount
                FROM debts
                WHERE user_id = %s AND period_id = %s AND debt_type = 'OWES_ME' AND amount > 0
                GROUP BY person
                HAVING SUM(amount) > 0
                ORDER BY SUM(amount) DESC
            """, (user_id, period_id))
            return cur.fetchall()

def delete_last_transaction(user_id):
    period = get_current_period(user_id)
    period_id = period["id"]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM transactions
                WHERE user_id = %s AND period_id = %s
                ORDER BY id DESC LIMIT 1
            """, (user_id, period_id))
            tx = cur.fetchone()

            if not tx:
                return None

            kind = tx["kind"]
            amount = float(tx["amount"] or 0)
            person = clean_person(tx["person"])
            debt_action = str(tx["debt_action"] or "NONE").upper().strip()

            if debt_action == "REPAY" and person:
                debt_type = "OWES_ME" if kind == "INCOME" else "I_OWE"
                add_debt_db(cur, user_id, period_id, person, amount, debt_type)
            elif debt_action == "DEBT_IN" and person:
                subtract_debt_db(cur, user_id, period_id, person, amount, "I_OWE")
            elif debt_action == "DEBT_OUT" and person:
                subtract_debt_db(cur, user_id, period_id, person, amount, "OWES_ME")

            cur.execute("DELETE FROM transactions WHERE id = %s", (tx["id"],))
        conn.commit()

    return tx

# =========================================================
# VOICE (WHISPER)
# =========================================================

def transcribe_audio(audio_bytes, filename="voice.ogg"):
    if not GROQ_API_KEY:
        return None

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    files = {"file": (filename, audio_bytes, "audio/ogg")}
    data = {
        "model": WHISPER_MODEL,
        "language": "uz",
        "response_format": "json",
        "temperature": "0",
        "prompt": "O'zbek tilidagi moliyaviy gap. So'm, ming, min, million, mln, qarz, qarzdor, qarzini berdi..."
    }

    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files=files,
            data=data,
            timeout=120
        )
        response.raise_for_status()
        result = response.json()
        return (result.get("text") or "").strip()
    except Exception as e:
        print("WHISPER ERROR:", e)
        return None

# =========================================================
# KEYBOARD & HANDLERS
# =========================================================

def main_keyboard():
    keyboard = [
        ["💰 Tushumlar", "💸 Xarajatlar"],
        ["📊 Hisobot", "🤝 Qarzdorlar"],
        ["🗑 Oxirgisini o'chirish"],
        ["🔄 Yangi hisob — 0 dan"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_current_period(user_id)
    await update.message.reply_text(
        "💰 <b>MyFinance AI</b>\n\n"
        "Masalan:\n"
        "• Uyga 120 min narsa oldim\n"
        "• Moshinaga 200 min benzin\n"
        "• Ads 700 min qarzini berdi",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

async def show_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_transactions(user_id, "INCOME", 100)
    if not rows:
        await update.message.reply_text("💰 Tushumlar yo'q.")
        return

    total = 0
    lines = ["💰 <b>TUSHUMLAR</b>", ""]
    for index, row in enumerate(rows, 1):
        amount = float(row["amount"] or 0)
        total += amount
        category = normalize_category(row["category"])
        lines.append(f"{index}. +{money(amount)} so'm — {category_display(category)}")

    lines.extend(["", f"💰 <b>Jami: {money(total)} so'm</b>"])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def show_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_transactions(user_id, "EXPENSE", 100)
    if not rows:
        await update.message.reply_text("💸 Xarajatlar yo'q.")
        return

    total = 0
    lines = ["💸 <b>XARAJATLAR</b>", ""]
    for index, row in enumerate(rows, 1):
        amount = float(row["amount"] or 0)
        total += amount
        category = normalize_category(row["category"])
        debt_action = str(row["debt_action"] or "NONE").upper()
        category_text = "Qarz berildi" if debt_action == "DEBT_OUT" else category_display(category)
        lines.append(f"{index}. -{money(amount)} so'm — {category_text}")

    lines.extend(["", f"💸 <b>Jami: {money(total)} so'm</b>"])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    report = get_report(user_id)
    await update.message.reply_text(
        "📊 <b>HISOBOT</b>\n\n"
        f"💰 Tushum: +{money(report['income'])} so'm\n"
        f"💸 Xarajat: -{money(report['expense'])} so'm\n"
        f"🟢 Qoldiq: {money(report['balance'])} so'm",
        parse_mode="HTML"
    )

async def show_debtors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_debtors(user_id)
    if not rows:
        await update.message.reply_text("🤝 Hozir sizga qarzdor odamlar yo'q.")
        return

    total = 0
    lines = ["🤝 <b>QARZDORLAR</b>", ""]
    for index, row in enumerate(rows, 1):
        person = clean_person(row["person"])
        amount = float(row["amount"] or 0)
        total += amount
        lines.append(f"{index}. {person} — {money(amount)} so'm")

    lines.extend(["", f"💰 <b>Jami: {money(total)} so'm</b>"])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tx = delete_last_transaction(user_id)
    if not tx:
        await update.message.reply_text("🗑 O'chirish uchun yozuv yo'q.")
        return

    amount = float(tx["amount"] or 0)
    sign = "+" if tx["kind"] == "INCOME" else "-"
    period = get_current_period(user_id)
    balance = get_balance(user_id, period["id"])

    await update.message.reply_text(
        "🗑 <b>Oxirgi yozuv o'chirildi.</b>\n\n"
        f"{sign}{money(amount)} so'm\n"
        f"🟢 Qoldiq: {money(balance)} so'm",
        parse_mode="HTML"
    )

async def new_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_new_period_confirm"] = True
    await update.message.reply_text(
        "🔄 Yangi hisobni 0 dan boshlaymizmi?\n\n"
        "Tasdiqlash uchun <b>ha</b> deb yozing.",
        parse_mode="HTML"
    )

async def process_money_text(update: Update, text: str):
    user_id = update.effective_user.id
    parsed = parse_text(text)

    if not parsed or not (parsed.get("transactions") or parsed.get("debts")):
        await update.message.reply_text("❌ Tushunmadim yoki moliyaviy operatsiya topilmadi.")
        return

    period = save_data(user_id, parsed, text)
    lines = []

    for tx in parsed.get("transactions", []):
        tx_type = str(tx.get("type", "")).upper()
        amount = float(tx.get("amount", 0))
        person = clean_person(tx.get("person"))
        debt_action = str(tx.get("debt_action", "NONE")).upper()

        if amount <= 0:
            continue

        if debt_action == "REPAY":
            sign = "-" if tx_type == "EXPENSE" else "+"
            lines.append(f"{'💸' if sign=='-' else '💰'} Qarz qaytimi: {sign}{money(amount)} so'm" + (f" — {person}" if person else ""))
        elif debt_action == "DEBT_IN":
            lines.append(f"🤝 {person or 'Noma\'lum'}dan {money(amount)} so'm qarz olindi")
        elif debt_action == "DEBT_OUT":
            lines.append(f"🤝 {person or 'Noma\'lum'}ga {money(amount)} so'm qarz berildi")
        elif tx_type == "INCOME":
            lines.append(f"💰 Tushum: +{money(amount)} so'm")
        elif tx_type == "EXPENSE":
            category = normalize_category(tx.get("category"))
            lines.append(f"💸 Xarajat: -{money(amount)} so'm — {category_display(category)}")

    for debt in parsed.get("debts", []):
        action = str(debt.get("action", "")).upper()
        person = clean_person(debt.get("person"))
        amount = float(debt.get("amount", 0))
        debt_type = str(debt.get("debt_type", "")).upper()

        if not person or amount <= 0:
            continue

        if action == "ADD" and debt_type == "OWES_ME":
            lines.append(f"🤝 Qarzdor: {person} — {money(amount)} so'm")
        elif action == "ADD" and debt_type == "I_OWE":
            lines.append(f"🤝 Siz {person}ga {money(amount)} so'm qarzdorsiz")

    balance = get_balance(user_id, period["id"])
    lines.append(f"\n🟢 Qoldiq: {money(balance)} so'm")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id

    if context.user_data.get("waiting_new_period_confirm"):
        text = (update.message.text or "").strip().lower()
        if text in {"ha", "xa", "yes", "tasdiqlayman"}:
            create_new_period(user_id)
            context.user_data["waiting_new_period_confirm"] = False
            await update.message.reply_text("✅ Yangi hisob boshlandi.\n\n🟢 Qoldiq: 0 so'm", reply_markup=main_keyboard())
            return
        if text in {"yo'q", "yoq", "bekor", "cancel"}:
            context.user_data["waiting_new_period_confirm"] = False
            await update.message.reply_text("❌ Bekor qilindi.", reply_markup=main_keyboard())
            return

    text = (update.message.text or "").strip()

    if text == "💰 Tushumlar":
        await show_income(update, context)
        return
    if text == "💸 Xarajatlar":
        await show_expenses(update, context)
        return
    if text == "📊 Hisobot":
        await show_report(update, context)
        return
    if text == "🤝 Qarzdorlar":
        await show_debtors(update, context)
        return
    if text == "🗑 Oxirgisini o'chirish":
        await delete_last(update, context)
        return
    if text == "🔄 Yangi hisob — 0 dan":
        await new_period(update, context)
        return

    if update.message.voice:
        try:
            await update.message.chat.send_action("typing")
            voice_file = await update.message.voice.get_file()
            audio_bytes = await voice_file.download_as_bytearray()
            transcribed = transcribe_audio(bytes(audio_bytes), "voice.ogg")
            if not transcribed:
                await update.message.reply_text("❌ Ovozni tushunib bo'lmadi.")
                return
            await process_money_text(update, transcribed)
        except Exception as e:
            print("VOICE ERROR:", e)
            await update.message.reply_text("❌ Ovozli xabarni qayta ishlashda xato.")
        return

    if text:
        try:
            await update.message.chat.send_action("typing")
            await process_money_text(update, text)
        except Exception as e:
            print("MESSAGE ERROR:", e)
            await update.message.reply_text("❌ Xatolik yuz berdi.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("BOT ERROR:", context.error)

# =========================================================
# MAIN (Render Webhook HTTPS bilan)
# =========================================================

def main():
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ALL, handle_message))
    application.add_error_handler(error_handler)

    if RENDER_EXTERNAL_URL:
        base_url = RENDER_EXTERNAL_URL.rstrip("/")
        # HTTPS majburiy tekshiruv
        if not base_url.startswith("https://"):
            base_url = base_url.replace("http://", "https://")

        webhook_path = BOT_TOKEN
        webhook_url = f"{base_url}/{webhook_path}"

        print("WEBHOOK URL:", webhook_url)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        print("BOT POLLING MODE")
        application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
