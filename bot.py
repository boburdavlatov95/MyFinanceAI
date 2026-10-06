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
    "Oila": "👨‍👩‍👧 Oila",
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

    return mapping.get(
        c,
        CATEGORY_OTHER
    )


# =========================================================
# AMOUNT PARSE
# =========================================================

def parse_amount(text):
    if not text:
        return None

    t = str(text).lower()

    # 2 500 000
    m = re.search(
        r'(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+)(?!\d)',
        t
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

    # 2.5 mln / 2,5 mln
    m = re.search(
        r'(\d+(?:[.,]\d+)?)\s*'
        r'(mln|million|millon|миллион|млн)',
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
        r'(\d+(?:[.,]\d+)?)\s*'
        r'(ming|min|минг|мин)',
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
        r'(\d+(?:[\s.,]\d+)*)\s*'
        r'(so.?m|som|sum|сум)',
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

    # Oddiy son
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
# MAXSUS QARZ QAYTIMI
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
                "category": "Boshqa",
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

    # -----------------------------------------
    # QARZ
    # -----------------------------------------

    if "qarz" in t:

        # Men qarz oldim
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
                        "category": CATEGORY_OTHER,
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
        ):

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

        # Qarzdor
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

    # -----------------------------------------
    # KATEGORIYALAR
    # -----------------------------------------

    category = CATEGORY_OTHER

    # Uy
    home_words = [
        "uyga",
        "uy uchun",
        "uyga narsa",
        "svet",
        "elektr",
        "gaz",
        "suv",
        "kommunal",
        "kommunalk",
        "internet",
    ]

    # Ish
    work_words = [
        "ishxona",
        "ishxonaga",
        "ish uchun",
        "ishga",
        "benzin",
        "zapchast",
        "moy",
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
        "ovqatlandik",
        "material",
        "reklama",
        "banner",
    ]

    # Oila
    family_words = [
        "onam",
        "otam",
        "akam",
        "ukam",
        "opam",
        "singlim",
        "akamga",
        "ukamga",
        "opamga",
        "singlimga",
        "xotinim",
        "erim",
        "farzandim",
        "bolam",
        "oilam",
        "oilaga",
    ]

    # Shaxsiy
    personal_words = [
        "o'zim",
        "ozim",
        "o'zim uchun",
        "o'zimga",
        "shaxsiy",
    ]

    if any(word in t for word in family_words):

        category = CATEGORY_FAMILY

    elif any(word in t for word in personal_words):

        category = CATEGORY_PERSONAL

    elif any(word in t for word in work_words):

        category = CATEGORY_WORK

    elif any(word in t for word in home_words):

        category = CATEGORY_HOME

    # -----------------------------------------
    # TUSHUM
    # -----------------------------------------

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
        "klent berdi",
        "klient berdi",
        "mijoz berdi",
    ]

    if any(
        word in t
        for word in income_words
    ):

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

    # -----------------------------------------
    # XARAJAT
    # -----------------------------------------

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
        word in t
        for word in expense_words
    ):

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
        "https://api.groq.com/openai/v1/"
        "chat/completions"
    )

    system_prompt = r"""
Sen MyFinance AI moliya botining ASOSIY AI parserisan.

FOYDALANUVCHINING HAR BIR PULGA OID XABARINI
AVVAL TO'LIQ MA'NOSI BO'YICHA TAHLIL QIL.

So'zma-so'z emas, gapning ma'nosini tushun.

Faqat JSON qaytar.

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


=========================================================
1. MIQDOR
=========================================================

Quyidagilarning barchasi bir xil:

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
2. ASOSIY KATEGORIYALAR
=========================================================

FAqat quyidagi 5 ta asosiy kategoriya ishlatiladi:

"Uy"
"Ish"
"Shaxsiy"
"Oila"
"Boshqa"


=========================================================
🏠 UY
=========================================================

Uy ichiga:

- uyga olingan narsalar
- uy xarajatlari
- internet
- svet
- elektr
- gaz
- suv
- kommunal
- kommunal to'lovlar

Misollar:

"Uyga 120 min narsa oldim"
=> category = "Uy"

"Uyga internetga 150 ming to'ladim"
=> category = "Uy"

"Gazga 200 ming"
=> category = "Uy"

"Svettga 100 ming"
=> category = "Uy"


=========================================================
💼 ISH
=========================================================

Ish ichiga:

- ishxona xarajatlari
- ish uchun xarajatlar
- transport
- yo'l kira
- taksi ish uchun bo'lsa
- moshina xarajatlari
- benzin
- moy
- zapchast
- remont
- ishchilar
- ishchilar bilan abet
- ishchilar bilan ujen
- ovqatlanish ish bilan bog'liq bo'lsa
- material
- reklama
- banner
- boshqa ish xarajatlari

Misollar:

"Ishxonaga 300 ming narsa oldim"
=> Ish

"Ish uchun 200 ming sarfladim"
=> Ish

"Moshinaga 250 ming benzin oldim"
=> Ish

"Ishchilar bilan abet 450 ming"
=> Ish

"Ishchilar bilan ujen 500 ming"
=> Ish

"Yo'l kira 100 ming"
=> Ish

"Materialga 600 ming"
=> Ish

"Reklamaga 300 ming"
=> Ish


=========================================================
👤 SHAXSIY
=========================================================

Faqat FOYDALANUVCHINING O'ZI UCHUN:

- o'z telefon to'lovi
- o'zining ovqati
- o'zining xaridi
- shaxsiy xarajatlari

Misollar:

"O'zimning telefonimga 100 ming to'ladim"
=> Shaxsiy

"O'zimga 200 ming kiyim oldim"
=> Shaxsiy

"O'zimga ovqat 80 ming"
=> Shaxsiy


MUHIM:

"telefon" so'zining o'zi Shaxsiy degani emas.

Kim uchun ekaniga qarab aniqlanadi.


=========================================================
👨‍👩‍👧 OILA
=========================================================

Oila a'zolari uchun:

- telefon
- ovqat
- kiyim
- xarid
- boshqa oilaviy xarajat

Misollar:

"Onamga 100 ming telefon to'lovi"
=> Oila

"Bolamga 200 ming kiyim oldim"
=> Oila

"Xotinimga 150 ming telefon to'ladim"
=> Oila

"Oilamga 300 ming xarajat qildim"
=> Oila


=========================================================
📦 BOSHQA
=========================================================

Yuqoridagi 4 kategoriyaga aniq kirmasa:

=> Boshqa


=========================================================
3. ODDIY TUSHUM
=========================================================

"500 ming oldim"
=> INCOME

"2 mln tushdi"
=> INCOME

"Mijozdan 1 mln keldi"
=> INCOME

"Klent 500 min berdi"
=> INCOME


=========================================================
4. XARAJAT
=========================================================

"120 min uyga narsa oldim"
=> EXPENSE
=> category = Uy

"65 000 uyga narsa oldim"
=> EXPENSE
=> category = Uy

"Ishchilar bilan abet 500 ming"
=> EXPENSE
=> category = Ish


=========================================================
5. MEN QARZ OLDIM
=========================================================

"Muroddan 300 min qarz oldim"

=> DEBT_IN
=> balance +300000
=> person = Murod
=> debt_type = I_OWE


=========================================================
6. MEN QARZ BERDIM
=========================================================

"Men Adsga 700 ming qarz berdim"

=> DEBT_OUT
=> balance -700000
=> person = Ads
=> debt_type = OWES_ME


=========================================================
7. ODAM SIZGA QARZDOR
=========================================================

"Ads 700 min qarz"

=> faqat debt ADD
=> person = Ads
=> amount = 700000
=> debt_type = OWES_ME

BALANS O'ZGARMAYDI.


"Murod 450 min qarz"

=> Murod sizga 450000 qarzdor


"Ads yana 300 min qarz"

=> mavjud Ads qarziga yana 300000 qo'shiladi


=========================================================
8. SIZ BOSHQA ODAMGA QARZDORSIZ
=========================================================

"Azizga 500 min qarzim bor"

=> debt ADD
=> debt_type = I_OWE
=> balance o'zgarmaydi

I_OWE hech qachon Qarzdorlar ro'yxatida chiqmaydi.


=========================================================
9. JUDA MUHIM: QARZINI BERDI
=========================================================

"Ads 700 min qarzini berdi"

MA'NOSI:

Ads o'z qarzini foydalanuvchiga qaytardi.

=> INCOME
=> +700000
=> debt_action = REPAY
=> person = Ads

Va Adsning OWES_ME qarzi 700000 ga kamayadi.


"Ads 700 min qarzini qaytardi"
=> xuddi shunday


"Ads menga 700 min qarzini berdi"
=> xuddi shunday


"Ads qarzidan 700 min berdi"
=> xuddi shunday


"Ads 700 min qarzini to'ladi"
=> xuddi shunday


HECH QACHON DEBT_OUT QILMA.


=========================================================
10. FARQ
=========================================================

"Men Adsga 700 min qarz berdim"

=> DEBT_OUT
=> Ads sizga qarzdor


"Ads menga 700 min qarzini berdi"

=> INCOME + REPAY
=> Adsning qarzi kamayadi


=========================================================
11. MEN O'Z QARZIMNI QAYTARDIM
=========================================================

"Azizga 400 min qarzimni qaytardim"

=> EXPENSE
=> debt_action = REPAY
=> debt_type = I_OWE
=> balance -400000


=========================================================
12. ODDIY QAYTIM
=========================================================

"Ads 400 min qaytardi"

=> INCOME
=> debt_action = REPAY
=> debt_type = OWES_ME
=> balance +400000


=========================================================
13. MUROD 300 MIN QARZ OLDI
=========================================================

Juda muhim:

"Murod 300 min qarz oldi"

Bu gapda SUBYEKT Murod.

Agar ma'no Murod foydalanuvchidan qarz olgan bo'lsa:

=> debt ADD
=> person = Murod
=> debt_type = OWES_ME
=> balance o'zgarmaydi

Lekin:

"Muroddan 300 min qarz oldim"

=> foydalanuvchi Muroddan qarz oldi
=> DEBT_IN
=> balance +300000
=> debt_type = I_OWE


=========================================================
14. BIR XABARDA KO'P OPERATSIYA
=========================================================

"500 min oldim
200 min ishlatdim
Ads 300 min qarz"

=> INCOME 500000
=> EXPENSE 200000
=> Ads OWES_ME 300000


=========================================================
15. MUHIM
=========================================================

Kategoriya tanlashda butun gap ma'nosini hisobga ol.

"Telefon" => avtomatik Shaxsiy emas.

"Onamning telefoni" => Oila.

"O'zimning telefonim" => Shaxsiy.

"Ishchilar bilan abet" => Ish.

"Ishdan keyin oilam bilan abet" => Oila.

"Mashinamga benzin" => Ish.

"Uyga internet" => Uy.

"Klient berdi" => INCOME.

"Qarzini berdi" => REPAY.


=========================================================
16. PERSON
=========================================================

Ismni aniq top.

Ads
ads
Murod
murod
Aziz

AI odam nomini o'ylab topmasin.

Agar odam ko'rsatilmagan bo'lsa:
person = null


=========================================================
17. FAQAT JSON
=========================================================

Hech qanday tushuntirish yozma.

Faqat valid JSON qaytar.
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
            timeout=60
        )

        response.raise_for_status()

        data = response.json()

        content = (
            data["choices"][0]["message"]["content"]
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

        if start != -1 and end != -1:

            content = content[
                start:end + 1
            ]

        parsed = json.loads(
            content
        )

        if not isinstance(
            parsed,
            dict
        ):
            return None

        return parsed

    except Exception as e:

        print(
            "GROQ ERROR:",
            e
        )

        return None


# =========================================================
# AI NATIJASINI TOZALASH
# =========================================================

def normalize_ai_result(
    parsed,
    source_text
):

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

        if tx_type == "DEBT_IN":

            debt_action = "DEBT_IN"
            category = CATEGORY_OTHER

        elif tx_type == "DEBT_OUT":

            debt_action = "DEBT_OUT"
            category = CATEGORY_OTHER

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

        if not person:
            continue

        try:
            amount = float(
                debt.get(
                    "amount",
                    0
                )
            )
        except Exception:
            amount = 0

        if amount <= 0:
            continue

        debt_type = str(
            debt.get(
                "debt_type",
                ""
            )
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

    # AI doim birinchi
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

    # AI ishlamasa fallback
    repayment = fast_repayment_parse(
        text
    )

    if repayment:
        return repayment

    return fast_parse(
        text
    )


# =========================================================
# DEBT DATABASE
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

    if not person:
        return

    if amount <= 0:
        return

    cur.execute("""
        SELECT id, amount
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

    if not person:
        return

    if amount <= 0:
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
                row["id"]
            ))

        else:

            new_amount = (
                current - remaining
            )

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

                # DEBT_IN:
                # balansga pul kiradi
                if tx_type == "DEBT_IN":

                    db_kind = "INCOME"
                    debt_action = "DEBT_IN"

                    category = CATEGORY_OTHER

                # DEBT_OUT:
                # balansdan pul chiqadi
                elif tx_type == "DEBT_OUT":

                    db_kind = "EXPENSE"
                    debt_action = "DEBT_OUT"

                    category = CATEGORY_OTHER

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

                # DEBT_IN
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

                # DEBT_OUT
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

                # REPAY
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
            # DEBT ONLY
            # -----------------------------------------

            for debt in debts:

                action = str(
                    debt.get(
                        "action",
                        ""
                    )
                ).upper().strip()

                person = clean_person(
                    debt.get("person")
                )

                if not person:
                    continue

                try:
                    amount = float(
                        debt.get(
                            "amount",
                            0
                        )
                    )
                except Exception:
                    amount = 0

                if amount <= 0:
                    continue

                debt_type = str(
                    debt.get(
                        "debt_type",
                        ""
                    )
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
                tx["debt_action"]
                or "NONE"
            ).upper().strip()

            # REPAY ni bekor qilish
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

            # DEBT_IN ni bekor qilish
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

            # DEBT_OUT ni bekor qilish
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
            "Murod, Ads, Aziz, "
            "uy, ish, ishxona, mashina, "
            "ishchilar, abet, ujen, "
            "kommunal, internet kabi so'zlar "
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
        "Masalan:\n\n"
        "• Uyga 120 min narsa oldim\n"
        "• Uyga internet 150 ming\n"
        "• Moshinaga 200 min benzin\n"
        "• Ishchilar bilan abet 450 ming\n"
        "• O'zimning telefonimga 100 ming\n"
        "• Onamga telefon 100 ming\n"
        "• Ads 700 min qarz\n"
        "• Ads 700 min qarzini berdi",
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

        category = normalize_category(
            row["category"]
        )

        lines.append(
            f"{index}. +{money(amount)} "
            f"so'm — {category_display(category)}"
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

        category = normalize_category(
            row["category"]
        )

        debt_action = str(
            row["debt_action"]
            or "NONE"
        ).upper()

        # Qarz berish alohida ko'rsatiladi
        if debt_action == "DEBT_OUT":

            category_text = "Qarz berildi"

        else:

            category_text = category_display(
                category
            )

        lines.append(
            f"{index}. -{money(amount)} "
            f"so'm — {category_text}"
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
# QARZDORLAR
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
        "🔄 Yangi hisobni 0 dan "
        "boshlaymizmi?\n\n"
        "Eski hisob o'chirilmaydi.\n\n"
        "Tasdiqlash uchun <b>ha</b> deb yozing.",
        parse_mode="HTML"
    )


# =========================================================
# PROCESS MONEY
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
            "• Uyga 120 min narsa oldim\n"
            "• Ishchilar bilan abet 450 ming\n"
            "• O'zimning telefonimga 100 ming\n"
            "• Onamga telefon 100 ming\n"
            "• Ads 700 min qarz\n"
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
        user_id,
        parsed,
        text
    )

    lines = []

    # =====================================================
    # TRANSACTIONS
    # =====================================================

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
                f"🤝 {person or 'Noma\\'lum'}dan "
                f"{money(amount)} so'm qarz olindi"
            )

        elif debt_action == "DEBT_OUT":

            lines.append(
                f"🤝 {person or 'Noma\\'lum'}ga "
                f"{money(amount)} so'm qarz berildi"
            )

        elif tx_type == "INCOME":

            lines.append(
                f"💰 Tushum: "
                f"+{money(amount)} so'm"
            )

        elif tx_type == "EXPENSE":

            category = normalize_category(
                tx.get("category")
            )

            lines.append(
                f"💸 Xarajat: "
                f"-{money(amount)} so'm"
                f" — {category_display(category)}"
            )

    # =====================================================
    # DEBTS
    # =====================================================

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
    # YANGI HISOB TASDIQLASH
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

    else:

        print(
            "BOT POLLING MODE"
        )

        application.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()
