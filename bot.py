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
    category = normalize_category(category)
    return CATEGORIES.get(category, "📦 Boshqa")


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
# AMOUNT
# =========================================================

def parse_amount(text):
    if not text:
        return None

    t = str(text).lower()

    # 2 500 000
    m = re.search(
        r"(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+)(?!\d)",
        t
    )

    if m:
        try:
            return float(
                re.sub(
                    r"[\s.,]",
                    "",
                    m.group(1)
                )
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
        return (
            float(
                m.group(1).replace(",", ".")
            )
            * 1_000_000
        )

    # 450 ming / 450 min
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*"
        r"(ming|min|минг|мин)\b",
        t,
        re.IGNORECASE
    )

    if m:
        return (
            float(
                m.group(1).replace(",", ".")
            )
            * 1_000
        )

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

    # oddiy son
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
# PERSON
# =========================================================

def guess_person(text):
    patterns = [
        r"^\s*([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)\s+\d",
        r"\b([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)ga\b",
        r"\b([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)dan\b",
    ]

    for pattern in patterns:

        m = re.search(
            pattern,
            text or "",
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
# REPAYMENT FALLBACK
# =========================================================

def fast_repayment_parse(text):

    t = (text or "").lower()

    patterns = [
        r"qarzini\s+berdi",
        r"qarzini\s+qaytardi",
        r"qarzini\s+to.?ladi",
        r"qarzidan\s+.+\s+berdi",
        r"qarzidan\s+.+\s+qaytardi",
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
    person = guess_person(text)

    if amount is None or not person:
        return None

    return {
        "transactions": [
            {
                "type": "INCOME",
                "amount": amount,
                "person": person,
                "category": "Boshqa",
                "note": text,
                "debt_action": "REPAY",
            }
        ],
        "debts": []
    }


# =========================================================
# LOCAL FALLBACK
# =========================================================

def fast_parse(text):

    repayment = fast_repayment_parse(text)

    if repayment:
        return repayment

    t = (text or "").lower().strip()

    amount = parse_amount(text)

    if amount is None:
        return None

    person = guess_person(text)

    # -----------------------------------------------------
    # QARZ
    # -----------------------------------------------------

    if "qarz" in t:

        # men qarz berdim
        if (
            "qarz berdim" in t
            or "qarz berdi" in t
        ):
            return {
                "transactions": [
                    {
                        "type": "DEBT_OUT",
                        "amount": amount,
                        "person": person,
                        "category": "Boshqa",
                        "note": text,
                        "debt_action": "DEBT_OUT",
                    }
                ],
                "debts": []
            }

        # men qarz oldim
        if (
            "qarz oldim" in t
            or "qarz oldi" in t
        ):
            return {
                "transactions": [
                    {
                        "type": "DEBT_IN",
                        "amount": amount,
                        "person": person,
                        "category": "Boshqa",
                        "note": text,
                        "debt_action": "DEBT_IN",
                    }
                ],
                "debts": []
            }

        # person qarzdor
        if (
            person
            and (
                "qarzdor" in t
                or t.endswith("qarz")
                or " qarz " in f" {t} "
            )
        ):
            return {
                "transactions": [],
                "debts": [
                    {
                        "action": "ADD",
                        "person": person,
                        "amount": amount,
                        "debt_type": "OWES_ME",
                    }
                ]
            }

    # -----------------------------------------------------
    # KATEGORIYA
    # -----------------------------------------------------

    category = "Boshqa"

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
    ]

    personal_words = [
        "o'zim",
        "ozim",
        "o'zimga",
        "o'zim uchun",
        "shaxsiy",
    ]

    home_words = [
        "uyga",
        "uy uchun",
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

    if any(
        x in t for x in family_words
    ):
        category = "Oila"

    elif any(
        x in t for x in personal_words
    ):
        category = "Shaxsiy"

    elif any(
        x in t for x in home_words
    ):
        category = "Uy"

    elif any(
        x in t for x in work_words
    ):
        category = "Ish"

    # -----------------------------------------------------
    # TUSHUM
    # -----------------------------------------------------

    income_words = [
        "tushdi",
        "tushum",
        "keldi",
        "oldim",
        "oldi",
        "daromad",
        "topdim",
        "topdi",
        "berdi",
        "mijoz berdi",
        "klent berdi",
        "klient berdi",
    ]

    if any(
        x in t
        for x in income_words
    ):
        return {
            "transactions": [
                {
                    "type": "INCOME",
                    "amount": amount,
                    "person": person,
                    "category": category,
                    "note": text,
                    "debt_action": "NONE",
                }
            ],
            "debts": []
        }

    # -----------------------------------------------------
    # XARAJAT
    # -----------------------------------------------------

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
        "oldim",
    ]

    if any(
        x in t
        for x in expense_words
    ):
        return {
            "transactions": [
                {
                    "type": "EXPENSE",
                    "amount": amount,
                    "person": person,
                    "category": category,
                    "note": text,
                    "debt_action": "NONE",
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
        "https://api.groq.com/openai/v1/"
        "chat/completions"
    )

    system_prompt = r'''
Sen MyFinance AI moliya botining ASOSIY AI parserisan.

FOYDALANUVCHINING HAR BIR MOLIYAVIY XABARINI
AVVAL AI SIFATIDA TAHLIL QIL.

Gapning umumiy ma'nosini tushun.
Faqat bitta kalit so'zga qarab qaror qilma.

Faqat VALID JSON qaytar.

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


=========================================================
MIQDOR
=========================================================

450 ming = 450000
450 min = 450000
450 мин = 450000
450 000 = 450000

2 mln = 2000000
2 million = 2000000


=========================================================
🏠 UY
=========================================================

Uy kategoriyasiga:

uy
uyga xarajat
uy uchun xarajat
uyga olingan narsa
internet
svet
elektr
gaz
suv
kommunal
kommunalkalar

Misollar:

"Uyga 120 min narsa oldim"
=> EXPENSE
=> category = Uy

"Uyga internet 150 min"
=> EXPENSE
=> category = Uy

"Gazga 200 ming to'ladim"
=> EXPENSE
=> category = Uy


=========================================================
💼 ISH
=========================================================

Ish kategoriyasiga:

ish
ishxona
ish uchun
transport
yo'l kira
taksi ish uchun bo'lsa
mashina
mashinaga benzin
moy
zapchast
remont
ishchilar
ishchilar bilan ovqat
abet
ujen
material
reklama
banner

Misollar:

"Ishxonaga 300 ming narsa oldim"
=> Ish

"Mashinaga 200 ming benzin"
=> Ish

"Ishchilar bilan abet 450 min"
=> Ish

"Ishchilar bilan ujen 500 min"
=> Ish

"Yo'l kira 100 ming"
=> Ish


=========================================================
👤 SHAXSIY
=========================================================

Faqat FOYDALANUVCHINING O'ZI uchun:

o'z telefoni
o'z telefon to'lovi
o'z ovqati
o'zining xaridi
o'zim
shaxsiy

Misollar:

"O'zimning telefonimga 100 min"
=> Shaxsiy

"O'zimga ovqat 80 min"
=> Shaxsiy


=========================================================
👨‍👩‍👧 OILA
=========================================================

Oila a'zolari uchun:

ona
onam
ota
otam
aka
uka
opa
singil
xotin
er
farzand
bola
oilam

Misollar:

"Onamning telefoniga 100 min"
=> Oila

"Bolamga 200 min kiyim"
=> Oila

"Oilam bilan ovqat 300 min"
=> Oila


MUHIM:

"Telefon"ning o'zi Shaxsiy degani emas.

Kim uchun ekaniga qarab aniqlanadi.


=========================================================
📦 BOSHQA
=========================================================

Yuqoridagi kategoriyalarga kirmasa:
=> Boshqa


=========================================================
ODDIY TUSHUM
=========================================================

"500 min oldim"
=> INCOME

"2 mln tushdi"
=> INCOME

"Mijoz 1 mln berdi"
=> INCOME

"Klent 500 min berdi"
=> INCOME

"500 min klent berdi"
=> INCOME


=========================================================
ODDIY XARAJAT
=========================================================

"120 min uyga narsa oldim"
=> EXPENSE
=> Uy

"65 000 uyga narsa oldim"
=> EXPENSE
=> Uy

"Ishchilar bilan abet 450 min"
=> EXPENSE
=> Ish


=========================================================
ODAM SIZGA QARZDOR
=========================================================

"Ads 700 min qarz"

=> debt ADD
=> person = Ads
=> amount = 700000
=> debt_type = OWES_ME

BALANS O'ZGARMAYDI.


"Murod 450 min qarz"

=> Murod sizga 450000 qarzdor

"Ads yana 300 min qarz"

=> Ads mavjud qarziga 300000 qo'shiladi


=========================================================
SIZ BOSHQA ODAMGA QARZDORSIZ
=========================================================

"Azizga 500 min qarzim bor"

=> debt ADD
=> debt_type = I_OWE

BALANS O'ZGARMAYDI.

I_OWE Qarzdorlar ro'yxatida ko'rinmaydi.


=========================================================
MEN QARZ OLDIM
=========================================================

"Muroddan 300 min qarz oldim"

=> DEBT_IN
=> balance +300000
=> person = Murod


"Azizdan 2 mln qarz oldim"

=> DEBT_IN
=> balance +2000000
=> person = Aziz


=========================================================
MEN QARZ BERDIM
=========================================================

"Men Adsga 700 min qarz berdim"

=> DEBT_OUT
=> balance -700000
=> person = Ads

Ads sizga qarzdor bo'ladi.


=========================================================
JUDA MUHIM: QARZINI BERDI
=========================================================

"Ads 700 min qarzini berdi"

Bu Ads foydalanuvchiga o'z qarzini qaytardi.

=> INCOME
=> +700000
=> debt_action = REPAY
=> person = Ads

Adsning OWES_ME qarzi 700000 ga kamayadi.


Quyidagilarning hammasi bir xil:

"Ads 700 min qarzini berdi"
"Ads 700 min qarzini qaytardi"
"Ads menga 700 min qarzini berdi"
"Ads 700 min qarzini to'ladi"
"Ads qarzidan 700 min berdi"

=> INCOME + REPAY + OWES_ME

BULARNI HECH QACHON DEBT_OUT QILMA.


=========================================================
MEN O'Z QARZIMNI QAYTARDIM
=========================================================

"Azizga 400 min qarzimni qaytardim"

=> EXPENSE
=> -400000
=> debt_action = REPAY
=> debt_type = I_OWE


=========================================================
QAYTIM
=========================================================

"Ads 400 min qaytardi"

=> INCOME
=> +400000
=> debt_action = REPAY
=> debt_type = OWES_ME


=========================================================
MUROD 300 MIN QARZ OLDI
=========================================================

"Murod 300 min qarz oldi"

Gapning ma'nosiga qarab aniqlanadi.

Agar Murod foydalanuvchidan qarz olgan bo'lsa:

=> debt ADD
=> OWES_ME
=> balance o'zgarmaydi


"Muroddan 300 min qarz oldim"

Bu foydalanuvchi Muroddan qarz olgan:

=> DEBT_IN
=> balance +300000
=> I_OWE


=========================================================
KO'P OPERATSIYA
=========================================================

Bir xabarda bir nechta operatsiya bo'lsa,
hammasini alohida chiqar.

Masalan:

"500 min oldim
200 min ishlatdim
Ads 300 min qarz"

transactions:
INCOME 500000
EXPENSE 200000

debts:
Ads OWES_ME 300000


=========================================================
PERSON
=========================================================

Person nomini aniq top.

Ads
Murod
Aziz
Klent
Mijoz

AI odam nomini o'ylab topmasin.

Agar nom yo'q bo'lsa:
person = null


=========================================================
ENG MUHIM
=========================================================

Har bir xabarni AI avval tahlil qiladi.

"qarz berdim"
va
"qarzini berdi"

bir xil emas.

"telefon"
ham avtomatik Shaxsiy emas.

Kim uchun ekanini aniqlash kerak.
'''


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
                "Authorization":
                    f"Bearer {GROQ_API_KEY}",
                "Content-Type":
                    "application/json"
            },
            json=payload,
            timeout=60
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
            e
        )

        return None


# =========================================================
# AI NATIJASINI TEKSHIRISH
# =========================================================

def normalize_ai_result(
    parsed,
    source_text
):

    if not isinstance(
        parsed,
        dict
    ):
        return None

    transactions = parsed.get(
        "transactions",
        []
    )

    debts = parsed.get(
        "debts",
        []
    )

    if not isinstance(
        transactions,
        list
    ):
        transactions = []

    if not isinstance(
        debts,
        list
    ):
        debts = []

    clean_transactions = []

    for tx in transactions:

        if not isinstance(
            tx,
            dict
        ):
            continue

        tx_type = str(
            tx.get(
                "type",
                ""
            )
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
                tx.get(
                    "amount",
                    0
                )
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
            tx.get(
                "debt_action",
                "NONE"
            )
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
            "note": (
                tx.get("note")
                or source_text
            ),
            "debt_action": debt_action
        })

    clean_debts = []

    for debt in debts:

        if not isinstance(
            debt,
            dict
        ):
            continue

        action = str(
            debt.get(
                "action",
                ""
            )
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
                debt.get(
                    "amount",
                    0
                )
            )
        except Exception:
            amount = 0

        debt_type = str(
            debt.get(
                "debt_type",
                ""
            )
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
# PARSE
# =========================================================

def parse_text(text):

    # AI HAR DOIM BIRINCHI
    ai_result = groq_parse(
        text
    )

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

    # fallback
    result = fast_repayment_parse(
        text
    )

    if result:
        return result

    return fast_parse(
        text
    )


# =========================================================
# DEBT DB
# =========================================================

def add_debt_db(
    cur,
    user_id,
    period_id,
    person,
    amount,
    debt_type
):

    person = clean_person(
        person
    )

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

    person = clean_person(
        person
    )

    if not person or amount <= 0:
        return

    remaining = float(
        amount
    )

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
# SAVE
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

            # ------------------------------------------------
            # TRANSACTIONS
            # ------------------------------------------------

            for tx in parsed.get(
                "transactions",
                []
            ):

                tx_type = str(
                    tx.get(
                        "type",
                        ""
                    )
                ).upper().strip()

                try:
                    amount = float(
                        tx.get(
                            "amount",
                            0
                        )
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

                # DEBT_IN
                if tx_type == "DEBT_IN":

                    db_kind = "INCOME"
                    debt_action = "DEBT_IN"
                    category = "Boshqa"

                # DEBT_OUT
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

                # DEBT_IN
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

                # DEBT_OUT
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

                # REPAY
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

            # ------------------------------------------------
            # DEBT ONLY
            # ------------------------------------------------

            for debt in parsed.get(
                "debts",
                []
            ):

                action = str(
                    debt.get(
                        "action",
                        ""
                    )
                ).upper().strip()

                person = clean_person(
                    debt.get(
                        "person"
                    )
                )

                try:
                    amount = float(
                        debt.get(
                            "amount",
                            0
                        )
                    )
                except Exception:
                    amount = 0

                debt_type = str(
                    debt.get(
                        "debt_type",
                        ""
                    )
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
# DEBTORS
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
                  AND LOWER(TRIM(person))
                      <> 'noma''lum'
                GROUP BY person
                HAVING SUM(amount) > 0
                ORDER BY SUM(amount) DESC
            """, (
                user_id,
                period["id"]
            ))

            return cur.fetchall()


# =========================================================
# DELETE LAST
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

            # repayment o'chirilsa
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
                    "OWES_ME"
                    if kind == "INCOME"
                    else "I_OWE"
                )

            # DEBT_IN o'chirilsa
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

            # DEBT_OUT o'chirilsa
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
# VOICE
# =========================================================

def transcribe_audio(
    audio_bytes,
    filename="voice.ogg"
):

    if not GROQ_API_KEY:
        return None

    try:

        response = requests.post(
            "https://api.groq.com/openai/v1/"
            "audio/transcriptions",

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
                "prompt": (
                    "O'zbek moliyaviy gap. "
                    "ming, min, million, mln, "
                    "qarz, qarzini berdi, "
                    "qarzini qaytardi, "
                    "uy, ish, ishxona, "
                    "mashina, benzin, "
                    "ishchilar, abet, ujen, "
                    "kommunal, internet, "
                    "Murod, Ads, Aziz, "
                    "klient, klent."
                )
            },

            timeout=120
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
            e
        )

        return None


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

    get_current_period(
        user_id
    )

    await update.message.reply_text(
        "💰 <b>MyFinance AI</b>\n\n"
        "Masalan:\n"
        "• 500 min oldim\n"
        "• 200 min ishlatdim\n"
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

        if action == "REPAY":

            label = "Qarz qaytimi"

        else:

            label = category_display(
                row["category"]
            )

        lines.append(
            f"{i}. +{money(amount)} "
            f"so'm — {label}"
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
            f"{i}. -{money(amount)} "
            f"so'm — {label}"
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
# DELETE
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

    balance = get_balance(
        update.effective_user.id,
        get_current_period(
            update.effective_user.id
        )["id"]
    )

    sign = (
        "+"
        if tx["kind"] == "INCOME"
        else "-"
    )

    await update.message.reply_text(
        "🗑 <b>Oxirgi yozuv o'chirildi.</b>\n\n"
        f"{sign}{money(tx['amount'])} so'm\n"
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

    parsed = parse_text(
        text
    )

    if not parsed:

        await update.message.reply_text(
            "❌ Tushunmadim.\n\n"
            "Masalan:\n"
            "• Ads 700 min qarz\n"
            "• Muroddan 300 min qarz oldim\n"
            "• Ads 700 min qarzini berdi\n"
            "• Ishchilar bilan abet 450 min\n"
            "• Uyga internet 150 min"
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

    # -----------------------------------------------------
    # TRANSACTIONS
    # -----------------------------------------------------

    for tx in parsed.get(
        "transactions",
        []
    ):

        tx_type = str(
            tx.get(
                "type",
                ""
            )
        ).upper()

        try:
            amount = float(
                tx.get(
                    "amount",
                    0
                )
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
                f" — {category_display(tx.get('category'))}"
            )

    # -----------------------------------------------------
    # DEBTS
    # -----------------------------------------------------

    for debt in parsed.get(
        "debts",
        []
    ):

        action = str(
            debt.get(
                "action",
                ""
            )
        ).upper()

        person = clean_person(
            debt.get("person")
        )

        try:
            amount = float(
                debt.get(
                    "amount",
                    0
                )
            )
        except Exception:
            amount = 0

        debt_type = str(
            debt.get(
                "debt_type",
                ""
            )
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
                f"🤝 Qarzdor: "
                f"{person} — "
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

    # -----------------------------------------------------
    # BALANCE
    # -----------------------------------------------------

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
    # YANGI HISOB
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
                update.message
                .voice
                .get_file()
            )

            audio = await (
                voice_file
                .download_as_bytearray()
            )

            transcribed = transcribe_audio(
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
                e
            )

            await update.message.reply_text(
                "❌ Ovozli xabarni "
                "qayta ishlashda xato."
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

    # =====================================================
    # RENDER
    # =====================================================

    if RENDER_EXTERNAL_URL:

        base = (
            RENDER_EXTERNAL_URL.rstrip("/")
        )

        path = BOT_TOKEN

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=path,
            webhook_url=f"{base}/{path}",
            drop_pending_updates=True,
        )

    else:

        app.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()
