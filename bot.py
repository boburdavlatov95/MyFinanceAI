import os
import re
import json
import asyncio
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

CATEGORIES = {
    "Uy": "🏠 Uy",
    "Ish": "💼 Ish",
    "Shaxsiy": "👤 Shaxsiy",
    "Oila": "👨‍👩‍👧 Oila",
    "Boshqa": "📦 Boshqa",
}


def normalize_category(category):
    if not category:
        return "Boshqa"

    c = str(category).strip().lower()

    mapping = {
        "uy": "Uy",
        "🏠 uy": "Uy",

        "ish": "Ish",
        "💼 ish": "Ish",

        "shaxsiy": "Shaxsiy",
        "👤 shaxsiy": "Shaxsiy",

        "oila": "Oila",
        "👨‍👩‍👧 oila": "Oila",

        "boshqa": "Boshqa",
        "📦 boshqa": "Boshqa",
    }

    return mapping.get(c, "Boshqa")


def category_display(category):
    return CATEGORIES.get(
        normalize_category(category),
        "📦 Boshqa"
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
                INSERT INTO finance_periods (
                    user_id,
                    title
                )
                VALUES (%s, %s)
                RETURNING id, title
            """, (
                user_id,
                "Hisob"
            ))

            row = cur.fetchone()

        conn.commit()

        return row


def create_new_period(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO finance_periods (
                    user_id,
                    title
                )
                VALUES (%s, %s)
                RETURNING id, title
            """, (
                user_id,
                "Yangi hisob"
            ))

            row = cur.fetchone()

        conn.commit()

        return row


# =========================================================
# FORMAT
# =========================================================

def money(value):
    try:
        value = float(value)
    except Exception:
        value = 0

    if abs(value - round(value)) < 0.01:
        return f"{int(round(value)):,}".replace(",", " ")

    return f"{value:,.2f}".replace(",", " ")


def clean_person(person):
    if person is None:
        return None

    person = str(person).strip()

    if not person:
        return None

    bad = {
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


# =========================================================
# SUMMA ANIQLASH
# =========================================================

def parse_amount(text):
    if not text:
        return None

    t = str(text).lower()

    # 2 500 000
    # 2.500.000
    # 2,500,000
    m = re.search(
        r"(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+)(?!\d)",
        t
    )

    if m:
        try:
            return float(
                re.sub(r"[\s.,]", "", m.group(1))
            )
        except Exception:
            pass

    # 2.5 mln
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*"
        r"(mln|million|millon|миллион|млн)\b",
        t,
        re.IGNORECASE
    )

    if m:
        try:
            return float(
                m.group(1).replace(",", ".")
            ) * 1_000_000
        except Exception:
            pass

    # 450 ming
    # 450 min
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*"
        r"(ming|min|минг|мин)\b",
        t,
        re.IGNORECASE
    )

    if m:
        try:
            return float(
                m.group(1).replace(",", ".")
            ) * 1_000
        except Exception:
            pass

    # 700 so'm
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*"
        r"(so.?m|som|sum|сум)\b",
        t,
        re.IGNORECASE
    )

    if m:
        try:
            return float(
                m.group(1).replace(",", ".")
            )
        except Exception:
            pass

    # Oddiy son
    m = re.search(
        r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)",
        t
    )

    if m:
        try:
            return float(
                m.group(1).replace(",", ".")
            )
        except Exception:
            pass

    return None


# =========================================================
# ODAM ISMINI TOPISH
# =========================================================

NOT_PERSON_WORDS = {
    "uy",
    "uyga",
    "ishga",
    "ish",
    "ishxona",
    "ishxonaga",

    "oilam",
    "oilamga",
    "oilaga",

    "onam",
    "onamga",
    "otam",
    "otamga",
    "akam",
    "akamga",
    "ukam",
    "ukamga",
    "opam",
    "opamga",
    "singlim",
    "singlimga",

    "bolam",
    "bolamga",
    "farzandim",
    "farzandimga",

    "reklama",
    "reklamaga",
    "banner",
    "bannerga",

    "mashina",
    "mashinaga",
    "taksi",
    "taksiga",

    "ishchi",
    "ishchilar",
    "ishchilarga",

    "o'zim",
    "o'zimga",
    "ozim",
    "ozimga",
}


def guess_person(text):
    text = text or ""

    # Masalan:
    # Ads 700 min qarz
    # Murod 500 min qarz
    m = re.search(
        r"^\s*([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)\s+(?=\d)",
        text,
        re.IGNORECASE
    )

    if m:
        person = clean_person(m.group(1))

        if person and person.lower() not in NOT_PERSON_WORDS:
            return person

    # Murodga
    # Muroddan
    m = re.search(
        r"\b([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)"
        r"(?:ga|dan)\b",
        text,
        re.IGNORECASE
    )

    if m:
        person = clean_person(m.group(1))

        if person and person.lower() not in NOT_PERSON_WORDS:
            return person

    # Ads qarzini berdi
    m = re.search(
        r"^\s*([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)\s+qarz",
        text,
        re.IGNORECASE
    )

    if m:
        person = clean_person(m.group(1))

        if person and person.lower() not in NOT_PERSON_WORDS:
            return person

    return None


# =========================================================
# QARZ QAYTARISHNI TEZ ANIQLASH
# =========================================================

def fast_repayment_parse(text):
    t = (text or "").lower()

    # Foydalanuvchi O'ZINING qarzini qaytardi
    own_patterns = [
        r"qarzimni\s+(?:qaytardim|qaytardik|to.?ladim|berdim)",
        r"qarzimni\s+to.?ladim",
    ]

    if any(
        re.search(p, t, re.IGNORECASE)
        for p in own_patterns
    ):
        amount = parse_amount(text)
        person = guess_person(text)

        if amount and person:
            return {
                "transactions": [{
                    "type": "EXPENSE",
                    "amount": amount,
                    "person": person,
                    "category": "Boshqa",
                    "note": text,
                    "debt_action": "REPAY",
                }],
                "debts": []
            }

    # Boshqa odam sizga qarzini qaytardi
    incoming_patterns = [
        r"qarzini\s+berdi",
        r"qarzini\s+qaytardi",
        r"qarzini\s+to.?ladi",
        r"qarzidan\s+.+\s+berdi",
        r"qarzidan\s+.+\s+qaytardi",
    ]

    if any(
        re.search(p, t, re.IGNORECASE)
        for p in incoming_patterns
    ):
        amount = parse_amount(text)
        person = guess_person(text)

        if amount and person:
            return {
                "transactions": [{
                    "type": "INCOME",
                    "amount": amount,
                    "person": person,
                    "category": "Boshqa",
                    "note": text,
                    "debt_action": "REPAY",
                }],
                "debts": []
            }

    # Masalan:
    # Ads 400 min qaytardi
    if "qaytardi" in t:
        amount = parse_amount(text)
        person = guess_person(text)

        if amount and person and "qarz" in t:
            return {
                "transactions": [{
                    "type": "INCOME",
                    "amount": amount,
                    "person": person,
                    "category": "Boshqa",
                    "note": text,
                    "debt_action": "REPAY",
                }],
                "debts": []
            }

    return None


# =========================================================
# BIRTA OPERATSIYANI LOCAL PARSE
# =========================================================

def fast_parse_one(text):
    t = (text or "").lower().strip()

    if not t:
        return None

    if not re.search(r"\d", t):
        return None

    # Avval qarz qaytimi
    repayment = fast_repayment_parse(text)

    if repayment:
        return repayment

    amount = parse_amount(text)

    if amount is None or amount <= 0:
        return None

    person = guess_person(text)

    # =====================================================
    # QARZ
    # =====================================================

    if "qarz" in t:

        # Azizga 500 ming qarz berdim
        if (
            "qarz berdim" in t
            or "qarz berdi" in t
            or "qarz bergandim" in t
        ):
            return {
                "transactions": [{
                    "type": "DEBT_OUT",
                    "amount": amount,
                    "person": person,
                    "category": "Boshqa",
                    "note": text,
                    "debt_action": "DEBT_OUT",
                }],
                "debts": []
            }

        # Muroddan 2 mln qarz oldim
        if (
            "qarz oldim" in t
            or "qarz oldi" in t
            or "qarz olganman" in t
        ):
            return {
                "transactions": [{
                    "type": "DEBT_IN",
                    "amount": amount,
                    "person": person,
                    "category": "Boshqa",
                    "note": text,
                    "debt_action": "DEBT_IN",
                }],
                "debts": []
            }

        # Ads 700 min qarz
        # Ads 700 min qarzdor
        if person and (
            "qarzdor" in t
            or t.endswith("qarz")
            or " qarz " in f" {t} "
        ):
            return {
                "transactions": [],
                "debts": [{
                    "action": "ADD",
                    "person": person,
                    "amount": amount,
                    "debt_type": "OWES_ME",
                }]
            }

    # =====================================================
    # KATEGORIYA
    # =====================================================

    family_words = [
        "onam",
        "otam",
        "akam",
        "ukam",
        "opam",
        "singlim",
        "xotinim",
        "erim",
        "bolam",
        "farzandim",
        "oilam",
        "oilaga",
        "oilamga",
    ]

    personal_words = [
        "o'zim",
        "ozim",
        "o'zimga",
        "ozimga",
        "o'zim uchun",
        "ozim uchun",
        "shaxsiy",
    ]

    home_words = [
        "uyga",
        "uy uchun",
        "uyimga",
        "svet",
        "elektr",
        "gaz",
        "suv",
        "kommunal",
        "kommunalk",
        "internet",
    ]

    work_words = [
        "ishxona",
        "ishxonaga",
        "ish uchun",
        "ishga",
        "benzin",
        "mashina",
        "mashinaga",
        "yo'l kira",
        "yol kira",
        "taksi",
        "transport",
        "ishchi",
        "ishchilar",
        "abet",
        "ujen",
        "material",
        "reklama",
        "banner",
    ]

    category = "Boshqa"

    if any(x in t for x in family_words):
        category = "Oila"

    elif any(x in t for x in personal_words):
        category = "Shaxsiy"

    elif any(x in t for x in home_words):
        category = "Uy"

    elif any(x in t for x in work_words):
        category = "Ish"

    # =====================================================
    # ANIQL ODDIY XARAJAT
    # =====================================================

    expense_words = [
        "ishlatdim",
        "ishlatdi",
        "sarfladim",
        "sarfladi",
        "xarajat",
        "ketdi",
        "to'ladim",
        "toladim",
        "sotib oldim",
        "xarid qildim",
    ]

    if any(x in t for x in expense_words):
        return {
            "transactions": [{
                "type": "EXPENSE",
                "amount": amount,
                "person": person,
                "category": category,
                "note": text,
                "debt_action": "NONE",
            }],
            "debts": []
        }

    # =====================================================
    # YO'NALISH BO'YICHA XARAJAT
    #
    # 45 000 ishga Banner uchun
    # 41 600 oilamga
    # 150 000 uyga
    # =====================================================

    destination_expense_words = [
        "ishga",
        "ish uchun",
        "ishxonaga",
        "uyga",
        "uy uchun",

        "oilamga",
        "oilaga",
        "onamga",
        "otamga",
        "akamga",
        "ukamga",
        "opamga",
        "singlimga",
        "bolamga",
        "farzandimga",

        "o'zimga",
        "ozimga",

        "reklamaga",
        "banner uchun",
        "bannerga",

        "mashinaga",
        "benzin",
        "taksiga",
        "ishchilarga",
    ]

    if any(
        x in t
        for x in destination_expense_words
    ):
        return {
            "transactions": [{
                "type": "EXPENSE",
                "amount": amount,
                "person": person,
                "category": category,
                "note": text,
                "debt_action": "NONE",
            }],
            "debts": []
        }

    # =====================================================
    # TUSHUM
    # =====================================================

    income_words = [
        "tushdi",
        "tushum",
        "keldi",
        "daromad",
        "topdim",
        "topdi",
        "mijoz berdi",
        "klent berdi",
        "klient berdi",
    ]

    if any(x in t for x in income_words):
        return {
            "transactions": [{
                "type": "INCOME",
                "amount": amount,
                "person": person,
                "category": category,
                "note": text,
                "debt_action": "NONE",
            }],
            "debts": []
        }

    # "500 ming oldim" — eski botdagi mantiqni saqlaymiz:
    # faqat "oldim" bo'lsa tushum.
    #
    # Lekin "sotib oldim" yuqorida xarajat sifatida ushlangan.

    if re.search(r"\boldim\b", t):
        return {
            "transactions": [{
                "type": "INCOME",
                "amount": amount,
                "person": person,
                "category": category,
                "note": text,
                "debt_action": "NONE",
            }],
            "debts": []
        }

    return None


# =========================================================
# KO'P OPERATSIYANI LOCAL PARSE
# =========================================================

def fast_parse(text):
    text = (text or "").strip()

    if not text:
        return None

    # Qatorlarga ajratamiz
    parts = [
        x.strip()
        for x in re.split(r"[\r\n;]+", text)
        if x.strip()
    ]

    if not parts:
        return None

    # Bitta operatsiya
    if len(parts) == 1:
        return fast_parse_one(parts[0])

    combined = {
        "transactions": [],
        "debts": []
    }

    for part in parts:

        # "." kabi qatorlarni tashlab ketamiz
        if not re.search(r"\d", part):
            continue

        result = fast_parse_one(part)

        if not result:
            # Bitta qator tushunilmasa,
            # to'liq xabarni AI ko'radi.
            return None

        combined["transactions"].extend(
            result.get("transactions", [])
        )

        combined["debts"].extend(
            result.get("debts", [])
        )

    if (
        combined["transactions"]
        or combined["debts"]
    ):
        return combined

    return None


# =========================================================
# GROQ
# =========================================================

SYSTEM_PROMPT = r"""
Sen MyFinance AI moliya botining parserisan.

Foydalanuvchining moliyaviy xabarini tahlil qil va
FAQAT valid JSON qaytar.

FORMAT:

{
  "transactions": [
    {
      "type": "INCOME",
      "amount": 0,
      "person": null,
      "category": "Uy",
      "note": "",
      "debt_action": "NONE"
    }
  ],
  "debts": [
    {
      "action": "ADD",
      "person": "",
      "amount": 0,
      "debt_type": "OWES_ME"
    }
  ]
}

type:
INCOME
EXPENSE
DEBT_IN
DEBT_OUT

category:
Uy
Ish
Shaxsiy
Oila
Boshqa

debt_action:
NONE
REPAY
DEBT_IN
DEBT_OUT

450 ming = 450000
450 min = 450000
450 000 = 450000
2 mln = 2000000

KATEGORIYALAR:

Uy:
uy, uyga, internet, svet, elektr, gaz, suv, kommunal.

Ish:
ish, ishxona, ishga, transport, mashina, benzin,
taksi, ishchilar, abet, ujen, material, reklama, banner.

Shaxsiy:
o'zim, o'zimga, shaxsiy.

Oila:
onam, otam, aka, uka, opa, singil, xotin, er,
bola, farzand, oilam.

Qolganlari:
Boshqa.

MUHIM MISOLLAR:

"45 000 ishga Banner uchun"
=
EXPENSE
45000
category=Ish

"41 600 oilamga"
=
EXPENSE
41600
category=Oila

"150 000 uyga"
=
EXPENSE
150000
category=Uy

"200 min ishlatdim"
=
EXPENSE
200000

"500 min tushdi"
=
INCOME
500000

"500 min oldim"
=
INCOME
500000

"Azizga 500 min qarz berdim"
=
DEBT_OUT
500000
person=Aziz

"Muroddan 300 min qarz oldim"
=
DEBT_IN
300000
person=Murod

"Ads 700 min qarz"
=
debts ADD OWES_ME

"Aziz 500 min qarzdor"
=
debts ADD OWES_ME

"Ads 700 min qarzini berdi"
=
INCOME
700000
person=Ads
debt_action=REPAY

"Ads 700 min qarzini qaytardi"
=
INCOME
700000
person=Ads
debt_action=REPAY

"Azizga 400 min qarzimni qaytardim"
=
EXPENSE
400000
person=Aziz
debt_action=REPAY

Bir xabarda bir nechta operatsiya bo'lsa,
hammasini alohida qaytar.

Odam nomini o'ylab topma.
"""


def groq_parse_sync(text):
    if not GROQ_API_KEY:
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": text
            }
        ]
    }

    try:
        response = requests.post(
            url,
            headers={
                "Authorization":
                    f"Bearer {GROQ_API_KEY}",
                "Content-Type":
                    "application/json"
            },
            json=payload,
            timeout=(5, 15)
        )

        response.raise_for_status()

        content = (
            response
            .json()["choices"][0]["message"]["content"]
            .strip()
        )

        content = re.sub(
            r"^```json\s*",
            "",
            content,
            flags=re.IGNORECASE
        )

        content = re.sub(
            r"^```\s*",
            "",
            content
        )

        content = re.sub(
            r"\s*```$",
            "",
            content
        )

        start = content.find("{")
        end = content.rfind("}")

        if start == -1 or end == -1:
            return None

        return json.loads(
            content[start:end + 1]
        )

    except Exception as e:
        print(
            "GROQ ERROR:",
            repr(e)
        )
        return None


async def groq_parse(text):
    # ENG MUHIM TUZATISH:
    # requests botning Telegram event loopini bloklamaydi.
    return await asyncio.to_thread(
        groq_parse_sync,
        text
    )


# =========================================================
# AI NATIJASINI TOZALASH
# =========================================================

def normalize_ai_result(parsed, source_text):
    if not isinstance(parsed, dict):
        return None

    transactions = parsed.get(
        "transactions",
        []
    )

    debts = parsed.get(
        "debts",
        []
    )

    if not isinstance(transactions, list):
        transactions = []

    if not isinstance(debts, list):
        debts = []

    clean_transactions = []

    for tx in transactions:

        if not isinstance(tx, dict):
            continue

        tx_type = str(
            tx.get("type", "")
        ).upper().strip()

        if tx_type not in {
            "INCOME",
            "EXPENSE",
            "DEBT_IN",
            "DEBT_OUT"
        }:
            continue

        try:
            amount = float(
                tx.get("amount", 0)
            )
        except Exception:
            amount = 0

        if amount <= 0:
            continue

        person = clean_person(
            tx.get("person")
        )

        category = normalize_category(
            tx.get("category")
        )

        debt_action = str(
            tx.get("debt_action", "NONE")
        ).upper().strip()

        if tx_type == "DEBT_IN":
            debt_action = "DEBT_IN"

        elif tx_type == "DEBT_OUT":
            debt_action = "DEBT_OUT"

        elif debt_action not in {
            "NONE",
            "REPAY"
        }:
            debt_action = "NONE"

        clean_transactions.append({
            "type": tx_type,
            "amount": amount,
            "person": person,
            "category": category,
            "note": tx.get("note") or source_text,
            "debt_action": debt_action
        })

    clean_debts = []

    for debt in debts:

        if not isinstance(debt, dict):
            continue

        action = str(
            debt.get("action", "")
        ).upper().strip()

        if action not in {
            "ADD",
            "REPAY"
        }:
            continue

        person = clean_person(
            debt.get("person")
        )

        try:
            amount = float(
                debt.get("amount", 0)
            )
        except Exception:
            amount = 0

        debt_type = str(
            debt.get("debt_type", "")
        ).upper().strip()

        if (
            not person
            or amount <= 0
            or debt_type not in {
                "OWES_ME",
                "I_OWE"
            }
        ):
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


# =========================================================
# UMUMIY PARSER
# =========================================================

async def parse_text(text):
    text = (text or "").strip()

    if not text:
        return None

    # "." ni Groqga yuborishning umuman hojati yo'q
    if not re.search(r"\d", text):
        return None

    # 1. AVVAL LOCAL PARSER
    result = fast_parse(text)

    if result:
        return result

    # 2. LOCAL TUSHUNMASA GROQ
    ai_result = await groq_parse(text)

    if ai_result:
        normalized = normalize_ai_result(
            ai_result,
            text
        )

        if normalized and (
            normalized["transactions"]
            or normalized["debts"]
        ):
            return normalized

    return None


# =========================================================
# QARZLAR
# =========================================================

def add_debt_db(
    cur,
    user_id,
    period_id,
    person,
    amount,
    debt_type
):
    person = clean_person(person)

    if not person or amount <= 0:
        return

    cur.execute("""
        SELECT id
        FROM debts
        WHERE user_id = %s
          AND period_id = %s
          AND LOWER(TRIM(person))
              = LOWER(TRIM(%s))
          AND debt_type = %s
        ORDER BY id DESC
        LIMIT 1
    """, (
        user_id,
        period_id,
        person,
        debt_type
    ))

    row = cur.fetchone()

    if row:
        cur.execute("""
            UPDATE debts
            SET amount = amount + %s
            WHERE id = %s
        """, (
            amount,
            row["id"]
        ))

    else:
        cur.execute("""
            INSERT INTO debts (
                user_id,
                period_id,
                person,
                amount,
                debt_type
            )
            VALUES (%s, %s, %s, %s, %s)
        """, (
            user_id,
            period_id,
            person,
            amount,
            debt_type
        ))


def subtract_debt_db(
    cur,
    user_id,
    period_id,
    person,
    amount,
    debt_type
):
    person = clean_person(person)

    if not person or amount <= 0:
        return

    remaining = float(amount)

    cur.execute("""
        SELECT id, amount
        FROM debts
        WHERE user_id = %s
          AND period_id = %s
          AND LOWER(TRIM(person))
              = LOWER(TRIM(%s))
          AND debt_type = %s
        ORDER BY id ASC
    """, (
        user_id,
        period_id,
        person,
        debt_type
    ))

    rows = cur.fetchall()

    for row in rows:

        if remaining <= 0:
            break

        current = float(
            row["amount"] or 0
        )

        if current <= remaining:

            remaining -= current

            cur.execute("""
                DELETE FROM debts
                WHERE id = %s
            """, (
                row["id"],
            ))

        else:

            cur.execute("""
                UPDATE debts
                SET amount = %s
                WHERE id = %s
            """, (
                current - remaining,
                row["id"]
            ))

            remaining = 0


# =========================================================
# SAQLASH
# =========================================================

def save_data(
    user_id,
    parsed,
    source_text
):
    period = get_current_period(
        user_id
    )

    period_id = period["id"]

    with get_conn() as conn:
        with conn.cursor() as cur:

            # TRANSACTIONS
            for tx in parsed.get(
                "transactions",
                []
            ):

                tx_type = str(
                    tx.get("type", "")
                ).upper().strip()

                try:
                    amount = float(
                        tx.get("amount", 0)
                    )
                except Exception:
                    amount = 0

                if (
                    tx_type not in {
                        "INCOME",
                        "EXPENSE",
                        "DEBT_IN",
                        "DEBT_OUT"
                    }
                    or amount <= 0
                ):
                    continue

                person = clean_person(
                    tx.get("person")
                )

                category = normalize_category(
                    tx.get("category")
                )

                note = (
                    tx.get("note")
                    or source_text
                )

                debt_action = str(
                    tx.get(
                        "debt_action",
                        "NONE"
                    )
                ).upper().strip()

                db_kind = tx_type

                if tx_type == "DEBT_IN":

                    db_kind = "INCOME"
                    debt_action = "DEBT_IN"
                    category = "Boshqa"

                elif tx_type == "DEBT_OUT":

                    db_kind = "EXPENSE"
                    debt_action = "DEBT_OUT"
                    category = "Boshqa"

                cur.execute("""
                    INSERT INTO transactions (
                        user_id,
                        period_id,
                        kind,
                        amount,
                        person,
                        category,
                        note,
                        debt_action
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                """, (
                    user_id,
                    period_id,
                    db_kind,
                    amount,
                    person,
                    category,
                    note,
                    debt_action
                ))

                # Qarz oldi
                if (
                    debt_action == "DEBT_IN"
                    and person
                ):
                    add_debt_db(
                        cur,
                        user_id,
                        period_id,
                        person,
                        amount,
                        "I_OWE"
                    )

                # Qarz berdi
                elif (
                    debt_action == "DEBT_OUT"
                    and person
                ):
                    add_debt_db(
                        cur,
                        user_id,
                        period_id,
                        person,
                        amount,
                        "OWES_ME"
                    )

                # Qarz qaytdi
                elif (
                    debt_action == "REPAY"
                    and person
                ):
                    debt_type = (
                        "OWES_ME"
                        if db_kind == "INCOME"
                        else "I_OWE"
                    )

                    subtract_debt_db(
                        cur,
                        user_id,
                        period_id,
                        person,
                        amount,
                        debt_type
                    )

            # DEBT TABLE
            for debt in parsed.get(
                "debts",
                []
            ):

                action = str(
                    debt.get("action", "")
                ).upper().strip()

                person = clean_person(
                    debt.get("person")
                )

                try:
                    amount = float(
                        debt.get("amount", 0)
                    )
                except Exception:
                    amount = 0

                debt_type = str(
                    debt.get("debt_type", "")
                ).upper().strip()

                if (
                    not person
                    or amount <= 0
                    or debt_type not in {
                        "OWES_ME",
                        "I_OWE"
                    }
                ):
                    continue

                if action == "ADD":

                    add_debt_db(
                        cur,
                        user_id,
                        period_id,
                        person,
                        amount,
                        debt_type
                    )

                elif action == "REPAY":

                    subtract_debt_db(
                        cur,
                        user_id,
                        period_id,
                        person,
                        amount,
                        debt_type
                    )

        conn.commit()

    return period


# =========================================================
# BALANCE
# =========================================================

def get_balance(
    user_id,
    period_id
):
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT COALESCE(
                    SUM(
                        CASE
                            WHEN kind = 'INCOME'
                                THEN amount
                            WHEN kind = 'EXPENSE'
                                THEN -amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS balance
                FROM transactions
                WHERE user_id = %s
                  AND period_id = %s
            """, (
                user_id,
                period_id
            ))

            row = cur.fetchone()

            return float(
                row["balance"] or 0
            )


# =========================================================
# HISOBOT
# =========================================================

def get_report(user_id):
    period = get_current_period(
        user_id
    )

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    COALESCE(
                        SUM(
                            CASE
                                WHEN kind = 'INCOME'
                                    THEN amount
                                ELSE 0
                            END
                        ),
                        0
                    ) AS income,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN kind = 'EXPENSE'
                                    THEN amount
                                ELSE 0
                            END
                        ),
                        0
                    ) AS expense

                FROM transactions

                WHERE user_id = %s
                  AND period_id = %s
            """, (
                user_id,
                period["id"]
            ))

            row = cur.fetchone()

    income = float(
        row["income"] or 0
    )

    expense = float(
        row["expense"] or 0
    )

    return {
        "period": period,
        "income": income,
        "expense": expense,
        "balance": income - expense
    }


# =========================================================
# TRANSACTIONS
# =========================================================

def get_transactions(
    user_id,
    kind=None,
    limit=100
):
    period = get_current_period(
        user_id
    )

    with get_conn() as conn:
        with conn.cursor() as cur:

            if kind:

                cur.execute("""
                    SELECT *
                    FROM transactions
                    WHERE user_id = %s
                      AND period_id = %s
                      AND kind = %s
                    ORDER BY id DESC
                    LIMIT %s
                """, (
                    user_id,
                    period["id"],
                    kind,
                    limit
                ))

            else:

                cur.execute("""
                    SELECT *
                    FROM transactions
                    WHERE user_id = %s
                      AND period_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                """, (
                    user_id,
                    period["id"],
                    limit
                ))

            return cur.fetchall()


# =========================================================
# QARZDORLAR
# =========================================================

def get_debtors(user_id):
    period = get_current_period(
        user_id
    )

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    person,
                    SUM(amount) AS amount

                FROM debts

                WHERE user_id = %s
                  AND period_id = %s
                  AND debt_type = 'OWES_ME'
                  AND amount > 0
                  AND person IS NOT NULL
                  AND TRIM(person) <> ''

                GROUP BY person

                HAVING SUM(amount) > 0

                ORDER BY SUM(amount) DESC
            """, (
                user_id,
                period["id"]
            ))

            return cur.fetchall()


# =========================================================
# OXIRGISINI O'CHIRISH
# =========================================================

def delete_last_transaction(user_id):
    period = get_current_period(
        user_id
    )

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT *
                FROM transactions

                WHERE user_id = %s
                  AND period_id = %s

                ORDER BY id DESC
                LIMIT 1
            """, (
                user_id,
                period["id"]
            ))

            tx = cur.fetchone()

            if not tx:
                return None

            amount = float(
                tx["amount"] or 0
            )

            person = clean_person(
                tx["person"]
            )

            action = str(
                tx["debt_action"]
                or "NONE"
            ).upper().strip()

            kind = tx["kind"]

            # Qarz qaytimi o'chirilsa,
            # qarzni qayta tiklaymiz.
            if (
                person
                and action == "REPAY"
            ):
                add_debt_db(
                    cur,
                    user_id,
                    period["id"],
                    person,
                    amount,
                    (
                        "OWES_ME"
                        if kind == "INCOME"
                        else "I_OWE"
                    )
                )

            # Qarz oldim yozuvi o'chirilsa
            elif (
                person
                and action == "DEBT_IN"
            ):
                subtract_debt_db(
                    cur,
                    user_id,
                    period["id"],
                    person,
                    amount,
                    "I_OWE"
                )

            # Qarz berdim yozuvi o'chirilsa
            elif (
                person
                and action == "DEBT_OUT"
            ):
                subtract_debt_db(
                    cur,
                    user_id,
                    period["id"],
                    person,
                    amount,
                    "OWES_ME"
                )

            cur.execute("""
                DELETE FROM transactions
                WHERE id = %s
            """, (
                tx["id"],
            ))

        conn.commit()

    return tx


# =========================================================
# VOICE / WHISPER
# =========================================================

def transcribe_audio_sync(
    audio_bytes,
    filename="voice.ogg"
):
    if not GROQ_API_KEY:
        return None

    try:

        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",

            headers={
                "Authorization":
                    f"Bearer {GROQ_API_KEY}"
            },

            files={
                "file": (
                    filename,
                    audio_bytes,
                    "audio/ogg"
                )
            },

            data={
                "model": WHISPER_MODEL,
                "language": "uz",
                "response_format": "json",
                "temperature": "0",

                "prompt":
                    "O'zbek moliyaviy gap. "
                    "ming, min, million, mln, "
                    "qarz, qarzini berdi, "
                    "qarzini qaytardi, "
                    "uy, ish, ishxona, "
                    "mashina, benzin, "
                    "ishchilar, abet, ujen, "
                    "kommunal, internet."
            },

            timeout=(10, 60)
        )

        response.raise_for_status()

        text = (
            response
            .json()
            .get("text", "")
            .strip()
        )

        print(
            "VOICE TEXT:",
            text
        )

        return text

    except Exception as e:

        print(
            "WHISPER ERROR:",
            repr(e)
        )

        return None


async def transcribe_audio(
    audio_bytes,
    filename="voice.ogg"
):
    # Voice ham botni bloklamaydi
    return await asyncio.to_thread(
        transcribe_audio_sync,
        audio_bytes,
        filename
    )


# =========================================================
# KEYBOARD
# =========================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                "💰 Tushumlar",
                "💸 Xarajatlar"
            ],
            [
                "📊 Hisobot",
                "🤝 Qarzdorlar"
            ],
            [
                "🗑 Oxirgisini o'chirish"
            ],
            [
                "🔄 Yangi hisob — 0 dan"
            ]
        ],
        resize_keyboard=True
    )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    get_current_period(user_id)

    await update.message.reply_text(
        "💰 <b>MyFinance AI</b>\n\n"

        "Masalan:\n"

        "• 500 min oldim\n"
        "• 200 min ishlatdim\n"
        "• 45 000 ishga Banner uchun\n"
        "• 41 600 oilamga\n"
        "• Ads 700 min qarz\n"
        "• Muroddan 300 min qarz oldim\n"
        "• Ads 700 min qarzini berdi\n"
        "• Ishchilar bilan abet 450 min\n"
        "• Uyga internet 150 min",

        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# =========================================================
# TUSHUMLAR
# =========================================================

async def show_income(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    rows = get_transactions(
        update.effective_user.id,
        "INCOME"
    )

    if not rows:
        await update.message.reply_text(
            "💰 Tushumlar yo'q."
        )
        return

    total = 0

    lines = [
        "💰 <b>TUSHUMLAR</b>",
        ""
    ]

    for i, row in enumerate(
        rows,
        1
    ):
        amount = float(
            row["amount"] or 0
        )

        total += amount

        action = str(
            row["debt_action"]
            or "NONE"
        ).upper()

        label = (
            "Qarz qaytimi"
            if action == "REPAY"
            else category_display(
                row["category"]
            )
        )

        lines.append(
            f"{i}. +{money(amount)} so'm — {label}"
        )

    lines.extend([
        "",
        f"💰 <b>Jami: {money(total)} so'm</b>"
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================================================
# XARAJATLAR
# =========================================================

async def show_expenses(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    rows = get_transactions(
        update.effective_user.id,
        "EXPENSE"
    )

    if not rows:
        await update.message.reply_text(
            "💸 Xarajatlar yo'q."
        )
        return

    total = 0

    lines = [
        "💸 <b>XARAJATLAR</b>",
        ""
    ]

    for i, row in enumerate(
        rows,
        1
    ):
        amount = float(
            row["amount"] or 0
        )

        total += amount

        action = str(
            row["debt_action"]
            or "NONE"
        ).upper()

        if action == "DEBT_OUT":
            label = "Qarz berildi"

        elif action == "REPAY":
            label = "Qarz qaytimi"

        else:
            label = category_display(
                row["category"]
            )

        lines.append(
            f"{i}. -{money(amount)} so'm — {label}"
        )

    lines.extend([
        "",
        f"💸 <b>Jami: {money(total)} so'm</b>"
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================================================
# HISOBOT
# =========================================================

async def show_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    report = get_report(
        update.effective_user.id
    )

    await update.message.reply_text(
        "📊 <b>HISOBOT</b>\n\n"

        f"💰 Tushum: "
        f"+{money(report['income'])} so'm\n"

        f"💸 Xarajat: "
        f"-{money(report['expense'])} so'm\n"

        f"🟢 Qoldiq: "
        f"{money(report['balance'])} so'm",

        parse_mode="HTML"
    )


# =========================================================
# QARZDORLAR
# =========================================================

async def show_debtors(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    rows = get_debtors(
        update.effective_user.id
    )

    if not rows:
        await update.message.reply_text(
            "🤝 Hozir sizga qarzdor odamlar yo'q."
        )
        return

    total = 0

    lines = [
        "🤝 <b>QARZDORLAR</b>",
        ""
    ]

    for i, row in enumerate(
        rows,
        1
    ):
        person = clean_person(
            row["person"]
        )

        amount = float(
            row["amount"] or 0
        )

        total += amount

        lines.append(
            f"{i}. {person} — "
            f"{money(amount)} so'm"
        )

    lines.extend([
        "",
        f"💰 <b>Jami: {money(total)} so'm</b>"
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================================================
# OXIRGISINI O'CHIRISH
# =========================================================

async def delete_last(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    tx = delete_last_transaction(
        update.effective_user.id
    )

    if not tx:
        await update.message.reply_text(
            "🗑 O'chirish uchun yozuv yo'q."
        )
        return

    period = get_current_period(
        update.effective_user.id
    )

    balance = get_balance(
        update.effective_user.id,
        period["id"]
    )

    sign = (
        "+"
        if tx["kind"] == "INCOME"
        else "-"
    )

    await update.message.reply_text(
        "🗑 <b>Oxirgi yozuv o'chirildi.</b>\n\n"

        f"{sign}{money(tx['amount'])} so'm\n"

        f"🟢 Qoldiq: "
        f"{money(balance)} so'm",

        parse_mode="HTML"
    )


# =========================================================
# YANGI HISOB
# =========================================================

async def new_period(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    context.user_data[
        "waiting_new_period_confirm"
    ] = True

    await update.message.reply_text(
        "🔄 Yangi hisobni 0 dan boshlaymizmi?\n\n"

        "Eski hisob o'chirilmaydi.\n\n"

        "Tasdiqlash uchun "
        "<b>ha</b> deb yozing.",

        parse_mode="HTML"
    )


# =========================================================
# MOLIYAVIY XABARNI SAQLASH
# =========================================================

async def process_money_text(
    update: Update,
    text: str
):
    parsed = await parse_text(text)

    if not parsed:

        await update.message.reply_text(
            "❌ Tushunmadim.\n\n"

            "Masalan:\n"
            "• 45 000 ishga Banner uchun\n"
            "• 41 600 oilamga\n"
            "• 500 min oldim\n"
            "• 200 min ishlatdim\n"
            "• Ads 700 min qarz\n"
            "• Muroddan 300 min qarz oldim\n"
            "• Ads 700 min qarzini berdi"
        )

        return

    if not (
        parsed.get("transactions")
        or parsed.get("debts")
    ):
        await update.message.reply_text(
            "❌ Moliyaviy operatsiya topilmadi."
        )
        return

    period = save_data(
        update.effective_user.id,
        parsed,
        text
    )

    lines = []

    # TRANSACTIONS
    for tx in parsed.get(
        "transactions",
        []
    ):

        tx_type = str(
            tx.get("type", "")
        ).upper()

        try:
            amount = float(
                tx.get("amount", 0)
            )
        except Exception:
            amount = 0

        person = clean_person(
            tx.get("person")
        )

        action = str(
            tx.get(
                "debt_action",
                "NONE"
            )
        ).upper()

        if amount <= 0:
            continue

        if action == "REPAY":

            if tx_type == "EXPENSE":

                lines.append(
                    f"💸 Qarz qaytimi: "
                    f"-{money(amount)} so'm"
                    + (
                        f" — {person}"
                        if person
                        else ""
                    )
                )

            else:

                lines.append(
                    f"💰 Qarz qaytimi: "
                    f"+{money(amount)} so'm"
                    + (
                        f" — {person}"
                        if person
                        else ""
                    )
                )

        elif action == "DEBT_IN":

            lines.append(
                f"🤝 {person or 'Noma’lum'}dan "
                f"{money(amount)} so'm qarz olindi"
            )

        elif action == "DEBT_OUT":

            lines.append(
                f"🤝 {person or 'Noma’lum'}ga "
                f"{money(amount)} so'm qarz berildi"
            )

        elif tx_type == "INCOME":

            lines.append(
                f"💰 Tushum: "
                f"+{money(amount)} so'm"
            )

        elif tx_type == "EXPENSE":

            lines.append(
                f"💸 Xarajat: "
                f"-{money(amount)} so'm"
                f" — "
                f"{category_display(tx.get('category'))}"
            )

    # DEBTS
    for debt in parsed.get(
        "debts",
        []
    ):

        action = str(
            debt.get("action", "")
        ).upper()

        person = clean_person(
            debt.get("person")
        )

        try:
            amount = float(
                debt.get("amount", 0)
            )
        except Exception:
            amount = 0

        debt_type = str(
            debt.get("debt_type", "")
        ).upper()

        if not person or amount <= 0:
            continue

        if (
            action == "ADD"
            and debt_type == "OWES_ME"
        ):

            lines.append(
                f"🤝 Qarzdor: {person} — "
                f"{money(amount)} so'm"
            )

        elif (
            action == "ADD"
            and debt_type == "I_OWE"
        ):

            lines.append(
                f"🤝 Siz {person}ga "
                f"{money(amount)} so'm qarzdorsiz"
            )

        elif (
            action == "REPAY"
            and debt_type == "OWES_ME"
        ):

            lines.append(
                f"🤝 {person}ning qarzi "
                f"{money(amount)} so'mga kamaydi"
            )

        elif (
            action == "REPAY"
            and debt_type == "I_OWE"
        ):

            lines.append(
                f"🤝 {person}ga qarzingiz "
                f"{money(amount)} so'mga kamaydi"
            )

    balance = get_balance(
        update.effective_user.id,
        period["id"]
    )

    lines.append(
        f"\n🟢 Qoldiq: "
        f"{money(balance)} so'm"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================================================
# MESSAGE HANDLER
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user_id = update.effective_user.id

    # =====================================================
    # YANGI HISOB TASDIQLASH
    # =====================================================

    if context.user_data.get(
        "waiting_new_period_confirm"
    ):

        text = (
            update.message.text or ""
        ).strip().lower()

        if text in {
            "ha",
            "xa",
            "yes",
            "tasdiqlayman"
        }:

            create_new_period(user_id)

            context.user_data[
                "waiting_new_period_confirm"
            ] = False

            await update.message.reply_text(
                "✅ Yangi hisob boshlandi.\n\n"
                "🟢 Qoldiq: 0 so'm",
                reply_markup=main_keyboard()
            )

            return

        if text in {
            "yo'q",
            "yoq",
            "bekor",
            "cancel"
        }:

            context.user_data[
                "waiting_new_period_confirm"
            ] = False

            await update.message.reply_text(
                "❌ Bekor qilindi.",
                reply_markup=main_keyboard()
            )

            return

    # =====================================================
    # BUTTONLAR
    # =====================================================

    text = (
        update.message.text or ""
    ).strip()

    if text == "💰 Tushumlar":
        await show_income(
            update,
            context
        )
        return

    if text == "💸 Xarajatlar":
        await show_expenses(
            update,
            context
        )
        return

    if text == "📊 Hisobot":
        await show_report(
            update,
            context
        )
        return

    if text == "🤝 Qarzdorlar":
        await show_debtors(
            update,
            context
        )
        return

    if text == "🗑 Oxirgisini o'chirish":
        await delete_last(
            update,
            context
        )
        return

    if text == "🔄 Yangi hisob — 0 dan":
        await new_period(
            update,
            context
        )
        return

    # =====================================================
    # VOICE
    # =====================================================

    if update.message.voice:

        try:

            await update.message.chat.send_action(
                "typing"
            )

            voice_file = await update.message.voice.get_file()

            audio = await voice_file.download_as_bytearray()

            transcribed = await transcribe_audio(
                bytes(audio)
            )

            if not transcribed:

                await update.message.reply_text(
                    "❌ Ovozni tushunib bo'lmadi."
                )

                return

            await process_money_text(
                update,
                transcribed
            )

        except Exception as e:

            print(
                "VOICE HANDLE ERROR:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Ovozli xabarni qayta ishlashda xato."
            )

        return

    # =====================================================
    # TEXT
    # =====================================================

    if text:

        try:

            await update.message.chat.send_action(
                "typing"
            )

            await process_money_text(
                update,
                text
            )

        except Exception as e:

            print(
                "MESSAGE ERROR:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Xatolik yuz berdi."
            )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    print(
        "BOT ERROR:",
        repr(context.error)
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print("Database tekshirilmoqda...")

    init_db()

    print("Database OK")

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
        MessageHandler(
            filters.ALL,
            handle_message
        )
    )

    app.add_error_handler(
        error_handler
    )

    # RENDER
    if RENDER_EXTERNAL_URL:

        base = (
            RENDER_EXTERNAL_URL
            .rstrip("/")
        )

        path = BOT_TOKEN

        print(
            f"Starting webhook on port {PORT}"
        )

        print(
            f"Webhook URL: {base}/{path}"
        )

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=path,
            webhook_url=f"{base}/{path}",
            drop_pending_updates=True,
        )

    # LOCAL
    else:

        print(
            "Starting polling..."
        )

        app.run_polling(
            drop_pending_updates=True
        )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
