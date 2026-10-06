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
FOYDALANUVCHINING HAR BIR PULGA OID XABARINI TAHLIL QILIB
