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
        value = int(round(value))
        return f"{value:,}".replace(",", " ")

    return f"{value:,.2f}".replace(",", " ")


def clean_person(person):
    if person is None:
        return None

    person = str(person).strip()

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


# =========================================================
# AMOUNT PARSE
# =========================================================

def parse_amount(text):
    if not text:
        return None

    t = str(text).lower()

    # 2 500 000 / 2.500.000
    m = re.search(
        r'(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+)(?!\d)',
        t
    )

    if m:
        raw = re.sub(r'[\s.,]', '', m.group(1))

        try:
            return float(raw)
        except Exception:
            pass

    # 2.5 mln / 2,5 mln
    m = re.search(
        r'(\d+(?:[.,]\d+)?)\s*(mln|million|millon|миллион|млн)',
        t,
        re.IGNORECASE
    )

    if m:
        number = float(
            m.group(1).replace(",", ".")
        )
        return number * 1_000_000

    # 450 ming / 450 min / 450 мин
    m = re.search(
        r'(\d+(?:[.,]\d+)?)\s*(ming|min|минг|мин)',
        t,
        re.IGNORECASE
    )

    if m:
        number = float(
            m.group(1).replace(",", ".")
        )
        return number * 1_000

    # 700 so'm
    m = re.search(
        r'(\d+(?:[\s.,]\d+)*)\s*(so.?m|som|sum|сум)',
        t,
        re.IGNORECASE
    )

    if m:
        raw = re.sub(
            r'[\s.,]',
            '',
            m.group(1)
        )

        try:
            return float(raw)
        except Exception:
            pass

    # oddiy son
    m = re.search(
        r'(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)',
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
# LOCAL PERSON TOPISH
# =========================================================

def guess_person(text):
    if not text:
        return None

    patterns = [
        # ads 700 ming qarz
        r'^\s*([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)\s+\d',

        # adsga 700 ming
        r'\b([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)ga\b',

        # adsdan 700 ming
        r'\b([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)dan\b',
    ]

    for pattern in patterns:

        m = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if m:
            person = clean_person(
                m.group(1)
            )

            if person:
                return person

    return None


# =========================================================
# MAXSUS REPAYMENT FALLBACK
# =========================================================

def fast_repayment_parse(text):
    """
    AI ishlamasa fallback.

    Ads 700 ming qarzini berdi
    Ads 700 ming qarzini qaytardi
    Ads qarzini to'ladi
    """

    t = text.lower().strip()

    patterns = [
        r'qarzini\s+berdi',
        r'qarzini\s+qaytardi',
        r'qarzini\s+to.?ladi',
        r'qarzidan\s+.+\s+berdi',
        r'qarzidan\s+.+\s+qaytardi',
    ]

    found = any(
        re.search(
            pattern,
            t,
            re.IGNORECASE
        )
        for pattern in patterns
    )

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
                "category": "Qarz qaytimi",
                "note": text,
                "debt_action": "REPAY"
            }
        ],
        "debts": []
    }


# =========================================================
# LOCAL FALLBACK
# =========================================================

def fast_parse(text):
    if not text:
        return None

    t = text.lower().strip()

    # Avval repayment
    repayment = fast_repayment_parse(text)

    if repayment:
        return repayment

    amount = parse_amount(text)

    if amount is None:
        return None

    person = guess_person(text)

    category = "Boshqa"

    categories = {
        "reklama": "Reklama",
        "ishchi": "Ishchi",
        "material": "Material",
        "yo'l": "Yo‘l",
        "yol": "Yo‘l",
        "transport": "Transport",
        "ovqat": "Ovqat",
        "telefon": "Telefon",
        "internet": "Internet",
    }

    for word, cat in categories.items():
        if word in t:
            category = cat
            break

    # Qarzdorlik
    if "qarz" in t:

        # Men qarz oldim
        if (
            "qarz oldim" in t
            or "qarz oldi" in t
            or "qarz oldik" in t
            or "qarz oldi" in t
        ):
            return {
                "transactions": [
                    {
                        "type": "DEBT_IN",
                        "amount": amount,
                        "person": person,
                        "category": "Qarz olindi",
                        "note": text,
                        "debt_action": "DEBT_IN"
                    }
                ],
                "debts": []
            }

        # Men qarz berdim
        if (
            "qarz berdim" in t
            or "qarz berdi" in t
            or "qarz berdik" in t
        ):
            return {
                "transactions": [
                    {
                        "type": "DEBT_OUT",
                        "amount": amount,
                        "person": person,
                        "category": "Qarz berildi",
                        "note": text,
                        "debt_action": "DEBT_OUT"
                    }
                ],
                "debts": []
            }

        # Qarzdor ...
        if (
            "qarzdor" in t
            or t.endswith("qarz")
            or " qarz " in f" {t} "
        ):
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

    # Tushum
    income_words = [
        "tushdi",
        "tushum",
        "keldi",
        "oldim",
        "oldi",
        "daromad",
        "topdim",
        "topdi",
    ]

    if any(word in t for word in income_words):

        # "pul oldim" tushum
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

    # Xarajat
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
    ]

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
# GROQ AI
# =========================================================

def groq_parse(text):
    if not GROQ_API_KEY:
        return None

    url = (
        "https://api.groq.com/openai/v1/chat/completions"
    )

    system_prompt = r"""
Sen MyFinance AI nomli shaxsiy moliya botining
asosiy moliyaviy parserisan.

FOYDALANUVCHI yozgan HAR BIR xabarni avval o'zing
AI sifatida tahlil qil.

Faqat JSON qaytar.
Izoh yozma.
Markdown yozma.

JSON FORMAT:

{
  "transactions": [
    {
      "type": "INCOME",
      "amount": 0,
      "person": null,
      "category": "Boshqa",
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

type quyidagilardan biri bo'lishi mumkin:

INCOME
EXPENSE
DEBT_IN
DEBT_OUT

debt_action:

NONE
REPAY
DEBT_IN
DEBT_OUT


=========================================================
PUL MIQDORI
=========================================================

Quyidagilar bir xil:

450 ming
450 min
450 мин
450 000
450000

=> 450000

2 mln
2 million
2 миллион

=> 2000000


=========================================================
ODDIY TUSHUM
=========================================================

"500 ming oldim"

=> INCOME 500000

"2 mln tushdi"

=> INCOME 2000000

"Mijozdan 1 mln keldi"

=> INCOME 1000000


=========================================================
ODDIY XARAJAT
=========================================================

"200 ming ishlatdim"

=> EXPENSE 200000

"Reklamaga 300 ming sarfladim"

=> EXPENSE 300000

"Materialga 500 ming ketdi"

=> EXPENSE 500000


=========================================================
MEN BIR OdamDAN QARZ OLDIM
=========================================================

"Murod 300 min qarz oldi"

Agar ma'no foydalanuvchi emas, Murod qarz olgani
ekanligi aniq bo'lsa:

=> Murod I_OWE

Lekin:

"Muroddan 300 min qarz oldim"

=> foydalanuvchi Muroddan qarz oldi

=> DEBT_IN
=> balance +300000
=> debt_type I_OWE
=> person Murod


Misol:

"Muroddan 300 min qarz oldim"

{
  "transactions": [
    {
      "type": "DEBT_IN",
      "amount": 300000,
      "person": "Murod",
      "category": "Qarz olindi",
      "note": "Muroddan 300 min qarz oldim",
      "debt_action": "DEBT_IN"
    }
  ],
  "debts": []
}


=========================================================
MEN ODAMGA QARZ BERDIM
=========================================================

"Men Adsga 700 ming qarz berdim"

=> DEBT_OUT
=> person Ads
=> balance -700000
=> Ads foydalanuvchiga qarzdor

Misol:

{
  "transactions": [
    {
      "type": "DEBT_OUT",
      "amount": 700000,
      "person": "Ads",
      "category": "Qarz berildi",
      "note": "Men Adsga 700 ming qarz berdim",
      "debt_action": "DEBT_OUT"
    }
  ],
  "debts": []
}


=========================================================
ODAM SIZGA QARZDOR
=========================================================

"Ads 700 min qarz"

=> debt-only
=> Ads sizga 700000 qarzdor

BALANS O'ZGARMAYDI.

Natija:

{
  "transactions": [],
  "debts": [
    {
      "action": "ADD",
      "person": "Ads",
      "amount": 700000,
      "debt_type": "OWES_ME"
    }
  ]
}


"Ads yana 300 ming qarz"

=> eski 700000 ga yana 300000 qo'shiladi
=> jami 1000000


=========================================================
SIZ BOSHQA ODAMGA QARZDORSIZ
=========================================================

"Azizga 500 ming qarzim bor"

=> debt-only
=> I_OWE

BALANS O'ZGARMAYDI.

Bu odam "Qarzdorlar" ro'yxatida chiqmaydi.


=========================================================
JUDA MUHIM: QARZINI BERDI
=========================================================

"Ads 700 ming qarzini berdi"

Bu foydalanuvchi Adsga pul berdi degani EMAS.

Bu:

Ads o'z qarzini foydalanuvchiga qaytardi.

=> INCOME
=> +700000
=> debt_action REPAY
=> person Ads
=> Adsning OWES_ME qarzi 700000 ga kamayadi

Natija:

{
  "transactions": [
    {
      "type": "INCOME",
      "amount": 700000,
      "person": "Ads",
      "category": "Qarz qaytimi",
      "note": "Ads 700 ming qarzini berdi",
      "debt_action": "REPAY"
    }
  ],
  "debts": []
}


Quyidagilarning hammasi REPAY:

"Ads 700 ming qarzini berdi"
"Ads 700 ming qarzini qaytardi"
"Ads menga 700 ming qarzini berdi"
"Ads 700 ming qarzini to'ladi"
"Ads qarzidan 700 ming berdi"


=========================================================
FARQNI HECH QACHON ARALASHTIRMA
=========================================================

"Men Adsga 700 ming qarz berdim"

=> DEBT_OUT
=> balance -700000
=> Ads sizga qarzdor

"Ads menga 700 ming qarzini berdi"

=> INCOME + REPAY
=> balance +700000
=> Adsning qarzi kamayadi


=========================================================
MEN O'Z QARZIMNI QAYTARDIM
=========================================================

"Azizga 400 ming qarzimni qaytardim"

=> EXPENSE
=> -400000
=> debt_action REPAY

Lekin qarz turi:

I_OWE

bo'ladi.

Bu holatda Aziz Qarzdorlar ro'yxatiga chiqmaydi.


=========================================================
QAYTIM
=========================================================

"Ads 400 ming qaytardi"

=> INCOME
=> +400000
=> REPAY
=> OWES_ME


=========================================================
BIR XABARDA KO'P OPERATSIYA
=========================================================

Masalan:

"500 ming oldim
200 ming ishlatdim
Ads 300 ming qarz"

=> 3 ta operatsiya.

transactions:
1) INCOME 500000
2) EXPENSE 200000

debts:
1) Ads OWES_ME 300000


=========================================================
PERSON
=========================================================

Person nomini matndan aniq top.

Ads
ads
Murod
murod
Aziz

Nomlarni xuddi foydalanuvchi aytganidek saqlash mumkin.

Agar odam nomi yo'q bo'lsa:
person = null

"Noma'lum" deb o'ylab topma.


=========================================================
QARZDORLAR
=========================================================

Faqat:

OWES_ME

ko'rinadi.

I_OWE hech qachon Qarzdorlar ro'yxatiga kirmaydi.


=========================================================
ENG MUHIM QOIDA
=========================================================

AI o'zi mazmunni tahlil qilishi kerak.

Faqat so'zga qarab emas,
butun gapning ma'nosiga qarab qaror qil.

"qarz berdim"
va
"qarzini berdi"

bir xil emas.


"""


    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
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
                "Authorization": (
                    f"Bearer {GROQ_API_KEY}"
                ),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )

        response.raise_for_status()

        data = response.json()

        content = (
            data["choices"][0]["message"]["content"]
            .strip()
        )

        # JSON markdownni olib tashlash
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

        # Ba'zan AI oldidan/ketidan matn qo'shadi
        start = content.find("{")
        end = content.rfind("}")

        if start != -1 and end != -1:
            content = content[start:end + 1]

        parsed = json.loads(content)

        if not isinstance(parsed, dict):
            return None

        parsed.setdefault(
            "transactions",
            []
        )

        parsed.setdefault(
            "debts",
            []
        )

        return parsed

    except Exception as e:
        print("GROQ ERROR:", e)
        return None


# =========================================================
# AI RESULT TOZALASH
# =========================================================

def normalize_ai_result(parsed, source_text):
    if not parsed:
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

        category = (
            tx.get("category")
            or "Boshqa"
        )

        note = (
            tx.get("note")
            or source_text
        )

        debt_action = str(
            tx.get("debt_action", "NONE")
        ).upper().strip()

        # Type asosida debt_actionni to'g'rilaymiz
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
            "note": note,
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

        if not person:
            continue

        try:
            amount = float(
                debt.get("amount", 0)
            )
        except Exception:
            amount = 0

        if amount <= 0:
            continue

        debt_type = str(
            debt.get("debt_type", "")
        ).upper().strip()

        if debt_type not in {
            "OWES_ME",
            "I_OWE"
        }:
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
# PARSE TEXT
# =========================================================

def parse_text(text):
    """
    MUHIM:
    Avval DOIM AI.
    AI ishlamasa local fallback.
    """

    # 1. AI
    ai_result = groq_parse(text)

    if ai_result:
        normalized = normalize_ai_result(
            ai_result,
            text
        )

        if normalized:
            if (
                normalized["transactions"]
                or normalized["debts"]
            ):
                return normalized

    # 2. Fallback
    repayment = fast_repayment_parse(text)

    if repayment:
        return repayment

    # 3. Local parser
    return fast_parse(text)


# =========================================================
# DEBT HELPERS
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

    if not person:
        return

    if amount <= 0:
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

    if not person:
        return

    if amount <= 0:
        return

    remaining = amount

    cur.execute("""
        SELECT id, amount
        FROM debts
        WHERE user_id = %s
          AND period_id = %s
          AND LOWER(TRIM(person)) = LOWER(TRIM(%s))
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

            new_amount = current - remaining
            remaining = 0

            cur.execute("""
                UPDATE debts
                SET amount = %s
                WHERE id = %s
            """, (
                new_amount,
                row["id"]
            ))


# =========================================================
# SAVE DATA
# =========================================================

def save_data(
    user_id,
    parsed,
    source_text=None
):
    period = get_current_period(
        user_id
    )

    period_id = period["id"]

    transactions = parsed.get(
        "transactions",
        []
    )

    debts = parsed.get(
        "debts",
        []
    )

    with get_conn() as conn:
        with conn.cursor() as cur:

            # -----------------------------------------
            # TRANSACTIONS
            # -----------------------------------------

            for tx in transactions:

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

                category = (
                    tx.get("category")
                    or "Boshqa"
                )

                note = (
                    tx.get("note")
                    or source_text
                    or ""
                )

                debt_action = str(
                    tx.get(
                        "debt_action",
                        "NONE"
                    )
                ).upper().strip()

                # DEBT_IN aslida balansga KIRIM
                # DEBT_OUT aslida balansdan CHIQIM
                if tx_type == "DEBT_IN":
                    db_kind = "INCOME"
                    debt_action = "DEBT_IN"
                    category = "Qarz olindi"

                elif tx_type == "DEBT_OUT":
                    db_kind = "EXPENSE"
                    debt_action = "DEBT_OUT"
                    category = "Qarz berildi"

                else:
                    db_kind = tx_type

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

                # -------------------------------------
                # DEBT_IN
                # -------------------------------------

                if debt_action == "DEBT_IN":
                    if person:
                        add_debt_db(
                            cur,
                            user_id,
                            period_id,
                            person,
                            amount,
                            "I_OWE"
                        )

                # -------------------------------------
                # DEBT_OUT
                # -------------------------------------

                elif debt_action == "DEBT_OUT":
                    if person:
                        add_debt_db(
                            cur,
                            user_id,
                            period_id,
                            person,
                            amount,
                            "OWES_ME"
                        )

                # -------------------------------------
                # REPAY
                # -------------------------------------

                elif debt_action == "REPAY":
                    if person:

                        if db_kind == "INCOME":
                            debt_type = "OWES_ME"
                        else:
                            debt_type = "I_OWE"

                        subtract_debt_db(
                            cur,
                            user_id,
                            period_id,
                            person,
                            amount,
                            debt_type
                        )

            # -----------------------------------------
            # DEBT-ONLY
            # -----------------------------------------

            for debt in debts:

                action = str(
                    debt.get("action", "")
                ).upper().strip()

                person = clean_person(
                    debt.get("person")
                )

                if not person:
                    continue

                try:
                    amount = float(
                        debt.get("amount", 0)
                    )
                except Exception:
                    amount = 0

                if amount <= 0:
                    continue

                debt_type = str(
                    debt.get("debt_type", "")
                ).upper().strip()

                if debt_type not in {
                    "OWES_ME",
                    "I_OWE"
                }:
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

def get_balance(user_id, period_id):

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    COALESCE(
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
# REPORT
# =========================================================

def get_report(user_id):

    period = get_current_period(
        user_id
    )

    period_id = period["id"]

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
                period_id
            ))

            row = cur.fetchone()

    income = float(
        row["income"] or 0
    )

    expense = float(
        row["expense"] or 0
    )

    balance = income - expense

    return {
        "period": period,
        "income": income,
        "expense": expense,
        "balance": balance
    }


# =========================================================
# TRANSACTIONS
# =========================================================

def get_transactions(
    user_id,
    kind=None,
    limit=50
):
    period = get_current_period(
        user_id
    )

    period_id = period["id"]

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
                    period_id,
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
                    period_id,
                    limit
                ))

            return cur.fetchall()


# =========================================================
# DEBTORS
# =========================================================

def get_debtors(user_id):

    period = get_current_period(
        user_id
    )

    period_id = period["id"]

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
                  AND LOWER(TRIM(person))
                      <> 'noma''lum'
                GROUP BY person
                HAVING SUM(amount) > 0
                ORDER BY SUM(amount) DESC
            """, (
                user_id,
                period_id
            ))

            return cur.fetchall()


# =========================================================
# DELETE LAST TRANSACTION
# =========================================================

def delete_last_transaction(user_id):

    period = get_current_period(
        user_id
    )

    period_id = period["id"]

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
                period_id
            ))

            tx = cur.fetchone()

            if not tx:
                return None

            kind = tx["kind"]
            amount = float(
                tx["amount"] or 0
            )

            person = clean_person(
                tx["person"]
            )

            debt_action = str(
                tx["debt_action"] or "NONE"
            ).upper().strip()

            # -----------------------------------------
            # REPAY ni bekor qilish
            # -----------------------------------------

            if (
                debt_action == "REPAY"
                and person
            ):

                debt_type = (
                    "OWES_ME"
                    if kind == "INCOME"
                    else "I_OWE"
                )

                add_debt_db(
                    cur,
                    user_id,
                    period_id,
                    person,
                    amount,
                    debt_type
                )

            # -----------------------------------------
            # DEBT_IN ni bekor qilish
            # -----------------------------------------

            elif (
                debt_action == "DEBT_IN"
                and person
            ):

                subtract_debt_db(
                    cur,
                    user_id,
                    period_id,
                    person,
                    amount,
                    "I_OWE"
                )

            # -----------------------------------------
            # DEBT_OUT ni bekor qilish
            # -----------------------------------------

            elif (
                debt_action == "DEBT_OUT"
                and person
            ):

                subtract_debt_db(
                    cur,
                    user_id,
                    period_id,
                    person,
                    amount,
                    "OWES_ME"
                )

            # Transactionni o'chiramiz
            cur.execute("""
                DELETE FROM transactions
                WHERE id = %s
            """, (
                tx["id"],
            ))

        conn.commit()

    return tx


# =========================================================
# VOICE
# =========================================================

def transcribe_audio(
    audio_bytes,
    filename="voice.ogg"
):

    if not GROQ_API_KEY:
        return None

    url = (
        "https://api.groq.com/openai/v1/"
        "audio/transcriptions"
    )

    files = {
        "file": (
            filename,
            audio_bytes,
            "audio/ogg"
        )
    }

    data = {
        "model": WHISPER_MODEL,
        "language": "uz",
        "response_format": "json",
        "temperature": "0",
        "prompt": (
            "O'zbek tilidagi moliyaviy gap. "
            "So'm, ming, min, million, mln, "
            "qarz, qarzdor, qarzini berdi, "
            "qarzini qaytardi, ishlatdim, "
            "sarfladim, oldim, berdim, "
            "Murod, Ads, Aziz kabi ismlar "
            "bo'lishi mumkin."
        )
    }

    try:

        response = requests.post(
            url,
            headers={
                "Authorization": (
                    f"Bearer {GROQ_API_KEY}"
                )
            },
            files=files,
            data=data,
            timeout=120
        )

        response.raise_for_status()

        result = response.json()

        text = (
            result.get("text")
            or ""
        ).strip()

        print(
            "VOICE TEXT:",
            text
        )

        return text

    except Exception as e:

        print(
            "WHISPER ERROR:",
            e
        )

        return None


# =========================================================
# KEYBOARD
# =========================================================

def main_keyboard():

    keyboard = [
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
    ]

    return ReplyKeyboardMarkup(
        keyboard,
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

    get_current_period(
        user_id
    )

    await update.message.reply_text(
        "💰 <b>MyFinance AI</b>\n\n"
        "Pulni oddiy yozing:\n\n"
        "• 500 ming oldim\n"
        "• 200 ming ishlatdim\n"
        "• Ads 700 min qarz\n"
        "• Muroddan 300 min qarz oldim\n"
        "• Ads 700 ming qarzini berdi\n"
        "• Azizga 500 ming qarzim bor",
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

    user_id = update.effective_user.id

    rows = get_transactions(
        user_id,
        "INCOME",
        100
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

    for index, row in enumerate(
        rows,
        1
    ):

        amount = float(
            row["amount"] or 0
        )

        total += amount

        category = (
            row["category"]
            or "Boshqa"
        )

        debt_action = (
            row["debt_action"]
            or "NONE"
        )

        if debt_action == "REPAY":

            lines.append(
                f"{index}. +{money(amount)} "
                f"so'm — {category}"
            )

        else:

            lines.append(
                f"{index}. +{money(amount)} "
                f"so'm — {category}"
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

    user_id = update.effective_user.id

    rows = get_transactions(
        user_id,
        "EXPENSE",
        100
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

    for index, row in enumerate(
        rows,
        1
    ):

        amount = float(
            row["amount"] or 0
        )

        total += amount

        category = (
            row["category"]
            or "Boshqa"
        )

        lines.append(
            f"{index}. -{money(amount)} "
            f"so'm — {category}"
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
# REPORT
# =========================================================

async def show_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    report = get_report(
        user_id
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
# DEBTORS
# =========================================================

async def show_debtors(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    rows = get_debtors(
        user_id
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

    for index, row in enumerate(
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
            f"{index}. {person} — "
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
# DELETE
# =========================================================

async def delete_last(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    tx = delete_last_transaction(
        user_id
    )

    if not tx:

        await update.message.reply_text(
            "🗑 O'chirish uchun yozuv yo'q."
        )

        return

    amount = float(
        tx["amount"] or 0
    )

    kind = tx["kind"]

    sign = (
        "+"
        if kind == "INCOME"
        else "-"
    )

    period = get_current_period(
        user_id
    )

    balance = get_balance(
        user_id,
        period["id"]
    )

    await update.message.reply_text(
        "🗑 <b>Oxirgi yozuv o'chirildi.</b>\n\n"
        f"{sign}{money(amount)} so'm\n"
        f"🟢 Qoldiq: {money(balance)} so'm",
        parse_mode="HTML"
    )


# =========================================================
# NEW PERIOD
# =========================================================

async def new_period(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data[
        "waiting_new_period_confirm"
    ] = True

    await update.message.reply_text(
        "🔄 Yangi hisobni 0 dan "
        "boshlaymizmi?\n\n"
        "Eski hisob o'chirilmaydi.\n\n"
        "Tasdiqlash uchun <b>ha</b> deb yozing.",
        parse_mode="HTML"
    )


# =========================================================
# PROCESS
# =========================================================

async def process_money_text(
    update: Update,
    text: str
):

    user_id = update.effective_user.id

    parsed = parse_text(
        text
    )

    if not parsed:

        await update.message.reply_text(
            "❌ Tushunmadim.\n\n"
            "Masalan:\n"
            "• 500 ming oldim\n"
            "• 200 ming ishlatdim\n"
            "• Ads 700 min qarz\n"
            "• Muroddan 300 min qarz oldim\n"
            "• Ads 700 ming qarzini berdi"
        )

        return

    has_transactions = bool(
        parsed.get("transactions")
    )

    has_debts = bool(
        parsed.get("debts")
    )

    if not has_transactions and not has_debts:

        await update.message.reply_text(
            "❌ Moliyaviy operatsiya topilmadi."
        )

        return

    period = save_data(
        user_id,
        parsed,
        text
    )

    lines = []

    # =====================================================
    # TRANSACTION NATIJALARI
    # =====================================================

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

        debt_action = str(
            tx.get(
                "debt_action",
                "NONE"
            )
        ).upper()

        if amount <= 0:
            continue

        if debt_action == "REPAY":

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

        elif debt_action == "DEBT_IN":

            lines.append(
                f"🤝 {person or 'Noma\'lum'}dan "
                f"{money(amount)} so'm qarz olindi"
            )

        elif debt_action == "DEBT_OUT":

            lines.append(
                f"🤝 {person or 'Noma\'lum'}ga "
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
            )

    # =====================================================
    # DEBT NATIJALARI
    # =====================================================

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

        if (
            not person
            or amount <= 0
        ):
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

    # =====================================================
    # BALANCE
    # =====================================================

    balance = get_balance(
        user_id,
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
    # NEW PERIOD CONFIRM
    # =====================================================

    if context.user_data.get(
        "waiting_new_period_confirm"
    ):

        text = (
            update.message.text
            or ""
        ).strip().lower()

        if text in {
            "ha",
            "xa",
            "yes",
            "tasdiqlayman"
        }:

            create_new_period(
                user_id
            )

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
    # BUTTONS
    # =====================================================

    text = (
        update.message.text
        or ""
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

            voice_file = await (
                update.message.voice.get_file()
            )

            audio_bytes = await (
                voice_file.download_as_bytearray()
            )

            transcribed = transcribe_audio(
                bytes(audio_bytes),
                "voice.ogg"
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
                e
            )

            await update.message.reply_text(
                "❌ Ovozli xabarni qayta "
                "ishlashda xato."
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
                e
            )

            await update.message.reply_text(
                "❌ Xatolik yuz berdi."
            )


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "BOT ERROR:",
        context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL,
            handle_message
        )
    )

    application.add_error_handler(
        error_handler
    )

    # =====================================================
    # RENDER WEBHOOK
    # =====================================================

    if RENDER_EXTERNAL_URL:

        base_url = (
            RENDER_EXTERNAL_URL.rstrip("/")
        )

        webhook_path = BOT_TOKEN

        webhook_url = (
            f"{base_url}/{webhook_path}"
        )

        print(
            "WEBHOOK URL:",
            webhook_url
        )

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )

    # =====================================================
    # LOCAL
    # =====================================================

    else:

        print(
            "BOT POLLING MODE"
        )

        application.run_polling(
            drop_pending_updates=True
        )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
