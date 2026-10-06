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
WHISPER_MODEL = "whisper-large-v3-turbo"

PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

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
print("Whisper:", WHISPER_MODEL)
print("Database: Supabase PostgreSQL")
print("Port:", PORT)


# =========================================================
# DATABASE
# =========================================================

def db():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
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
            INSERT INTO finance_periods
            (user_id, title)
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
        INSERT INTO finance_periods
        (user_id, title)
        VALUES (%s, %s)
        RETURNING id
    """, (
        user_id,
        f"Hisob #{number}"
    ))

    period_id = cur.fetchone()["id"]

    conn.commit()
    cur.close()
    conn.close()

    return period_id


# =========================================================
# MONEY
# =========================================================

def parse_amount(text):
    text = (
        text.lower()
        .replace(",", ".")
        .replace(" ", "")
    )

    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(mln|million|млн)",
        text
    )

    if m:
        return float(m.group(1)) * 1_000_000

    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(ming|тыс)",
        text
    )

    if m:
        return float(m.group(1)) * 1_000

    m = re.search(
        r"(\d+(?:\.\d+)?)\s*m\b",
        text
    )

    if m:
        return float(m.group(1)) * 1_000_000

    numbers = re.findall(
        r"\d+(?:\.\d+)?",
        text
    )

    if numbers:
        return float(numbers[-1])

    return None


def money(n):
    return (
        f"{float(n):,.0f}"
        .replace(",", " ")
        + " so'm"
    )


# =========================================================
# FAST PARSER
# =========================================================

def fast_parse(text):
    t = text.lower()

    amount = parse_amount(t)

    if not amount:
        return None

    # Qarzlarni AI'ga yuboramiz
    debt_words = [
        "qarz",
        "qarzim",
        "qarzni",
        "qarzga",
        "qarzdor",
        "qarzini",
        "qaytardi",
        "qaytardim",
        "qaytarib",
        "borrow",
        "lend",
    ]

    if any(x in t for x in debt_words):
        return None

    clear_expense_categories = [
        "materialga",
        "material uchun",
        "ishchiga",
        "ishchi uchun",
        "transportga",
        "transport uchun",
        "ovqatga",
        "ovqat uchun",
        "ijaraga",
        "ijara uchun",
        "reklamaga",
        "reklama uchun",
        "soliqqa",
        "soliq uchun",
        "elektrga",
        "elektr uchun",
        "gazga",
        "gaz uchun",
    ]

    expense_actions = [
        "ketdi",
        "sarfladim",
        "sarflandi",
        "to'ladim",
        "toladim",
        "ishlatdim",
        "ishlatildi",
        "sarflab yubordim",
    ]

    if (
        any(x in t for x in clear_expense_categories)
        and any(x in t for x in expense_actions)
    ):
        category = "Xarajat"

        if "material" in t:
            category = "Material"

        elif "ishchi" in t:
            category = "Ishchi"

        elif "transport" in t:
            category = "Transport"

        elif "ovqat" in t:
            category = "Ovqat"

        elif "reklama" in t:
            category = "Reklama"

        elif "ijara" in t:
            category = "Ijara"

        elif "soliq" in t:
            category = "Soliq"

        elif "elektr" in t:
            category = "Elektr"

        elif "gaz" in t:
            category = "Gaz"

        return {
            "transactions": [{
                "type": "EXPENSE",
                "amount": amount,
                "person": None,
                "category": category,
                "note": text,
                "debt_action": "NONE"
            }],
            "debts": []
        }

    return None


# =========================================================
# VOICE -> TEXT
# =========================================================

def transcribe_audio(audio_bytes, filename="voice.ogg"):

    if not GROQ_API_KEY:
        return None

    url = (
        "https://api.groq.com/openai/v1/"
        "audio/transcriptions"
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}"
    }

    files = {
        "file": (
            filename,
            audio_bytes,
            "audio/ogg"
        )
    }

    data = {
        "model": WHISPER_MODEL,
        "response_format": "json",
        "temperature": "0",
        "prompt": (
            "O'zbek tilidagi moliyaviy suhbat. "
            "Pul summalarini aniq eshitib yoz. "
            "So'm, ming, million, mln, mingta, "
            "qarz, qarzdor, qaytardi, qaytardim, "
            "berdim, oldim, tushdi, keldi, ketdi, "
            "sarfladim, ishlatdim kabi so'zlar bo'lishi mumkin. "
            "Ads, Aziz, Jasur kabi odam nomlarini "
            "imkon qadar aniq yoz."
        )
    }

    try:

        r = requests.post(
            url,
            headers=headers,
            files=files,
            data=data,
            timeout=60
        )

        r.raise_for_status()

        result = r.json()

        text = result.get(
            "text",
            ""
        ).strip()

        print(
            "VOICE RAW:",
            text
        )

        return text if text else None

    except Exception as e:

        print(
            "VOICE ERROR:",
            repr(e)
        )

        return None


# =========================================================
# VOICE TEXT NORMALIZER
# =========================================================

def normalize_voice_text(text):

    if not text:
        return text

    if not GROQ_API_KEY:
        return text

    url = (
        "https://api.groq.com/openai/v1/"
        "chat/completions"
    )

    system = """
Sen MyFinance AI uchun O'ZBEKCHA OVOZLI XABARNI
tozalovchi yordamchisan.

Whisper chiqargan matnni moliyaviy parser
oson tushunadigan aniq matnga aylantir.

ASOSIY QOIDA:

Gapning ma'nosini O'ZGARTIRMA.

Faqat noto'g'ri tanilgan so'zlar,
raqamlar va pul birliklarini tuzat.

=========================================================
1. SONLARNI RAQAMGA AYLANTIR
=========================================================

"yetti yuz ming"
→ "700 ming"

"to'rt yuz ming"
→ "400 ming"

"besh yuz ming"
→ "500 ming"

"besh million"
→ "5 mln"

"bir million"
→ "1 mln"

"ikki million"
→ "2 mln"

"bir million ikki yuz ming"
→ "1.2 mln"

"ikki million besh yuz ming"
→ "2.5 mln"

"uch million yetti yuz ming"
→ "3.7 mln"

=========================================================
2. PUL BIRLIKLARI
=========================================================

"ming" → ming

"million" → mln

"million so'm" → mln

"ming so'm" → ming

=========================================================
3. MA'NOLI SO'ZLARNI SAQLA
=========================================================

qarz
qarzdor
qarzim
qaytardi
qaytardim
berdim
oldim
tushdi
keldi
ketdi
sarfladim
ishlatdim

=========================================================
4. ISMLARNI O'ZGARTIRMA
=========================================================

Ads
Aziz
Jasur
Sardor
Akmal

kabi nomlarni imkon qadar Whisper bergan
ko'rinishida saqla.

=========================================================
5. MISOLLAR
=========================================================

Input:
Ads yetti yuz ming qarzdor

Output:
Qarzdor Ads 700 ming


Input:
Ads to'rt yuz ming qaytardi

Output:
Ads 400 ming qaytardi


Input:
Klientdan besh million tushdi

Output:
Klientdan 5 mln tushdi


Input:
Materialga bir million ikki yuz ming ketdi

Output:
Materialga 1.2 mln ketdi


Input:
Azizdan ikki million qarz oldim

Output:
Azizdan 2 mln qarz oldim


Input:
Azizga ikki million qarz berdim

Output:
Azizga 2 mln qarz berdim


Input:
Reklamaga ikki yuz ming ishlatdim

Output:
Reklamaga 200 ming ishlatdim

=========================================================
6. BIR NECHTA OPERATSIYA
=========================================================

Agar bir nechta operatsiya bo'lsa,
hammasini saqla.

Masalan:

"klientdan besh million tushdi
materialga bir million ikki yuz ming ketdi
ishchiga sakkiz yuz ming berdim"

Output:

"Klientdan 5 mln tushdi
Materialga 1.2 mln ketdi
Ishchiga 800 ming berdim"

=========================================================
7. FAQAT TOZALANGAN MATNNI QAYTAR
=========================================================

JSON yozma.

Izoh yozma.

Markdown yozma.

Tushuntirish yozma.

Faqat tayyorlangan matnni qaytar.
"""

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": system
            },
            {
                "role": "user",
                "content": text
            }
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

        normalized = (
            data["choices"][0]["message"]["content"]
            .strip()
        )

        normalized = re.sub(
            r"^```.*?\n",
            "",
            normalized,
            flags=re.DOTALL
        )

        normalized = re.sub(
            r"\n```$",
            "",
            normalized
        )

        normalized = normalized.strip()

        print(
            "VOICE NORMALIZED:",
            normalized
        )

        return (
            normalized
            if normalized
            else text
        )

    except Exception as e:

        print(
            "VOICE NORMALIZE ERROR:",
            repr(e)
        )

        return text


# =========================================================
# GROQ AI PARSER
# =========================================================

def groq_parse(text):

    if not GROQ_API_KEY:
        return None

    url = (
        "https://api.groq.com/openai/v1/"
        "chat/completions"
    )

    system = """
Sen MyFinance AI nomli pul hisob botining
asosiy moliyaviy parserisan.

Foydalanuvchi oddiy o'zbek tilida yozadi.

Sening vazifang:
gapning MA'NOSINI tushunish.

Faqat JSON qaytar.

FORMAT:

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
  ],
  "debts": [
    {
      "action": "NEW | REPAY",
      "person": "",
      "amount": 0,
      "debt_type": "OWES_ME"
    }
  ]
}


=========================================================
1. ODDIY TUSHUM
=========================================================

"Klientdan 5 mln tushdi"
→ INCOME

"5 mln pul keldi"
→ INCOME

"Bugun 3 mln oldim"
→ INCOME

"Reklamadan 7 mln tushdi"
→ INCOME


=========================================================
2. ODDIY XARAJAT
=========================================================

"Materialga 1.2 mln ketdi"
→ EXPENSE

"Ishchiga 800 ming berdim"
→ EXPENSE

"500 ming sarfladim"
→ EXPENSE

"Material uchun 1 mln to'ladim"
→ EXPENSE

"Reklamaga 200 ming ishlatdim"
→ EXPENSE


=========================================================
3. QARZDAN PUL OLISH
=========================================================

"Azizdan 2 mln qarz oldim"

→ transaction:
DEBT_IN
amount = 2000000
person = Aziz
debt_action = NEW

→ debt:
debt_type = I_OWE

Bu pul balansga QO'SHILADI.


=========================================================
4. BOSHQA ODAMGA QARZ BERISH
=========================================================

"Azizga 2 mln qarz berdim"

→ transaction:
DEBT_OUT
amount = 2000000
person = Aziz
debt_action = NEW

→ debt:
debt_type = OWES_ME

Bu pul balansdan AYRILADI.


=========================================================
5. MENGA QARZDOR ODAM
=========================================================

Bu gaplarda pul harakati yo'q.

Transaction yaratma.

"Qarzdor Ads 700 ming"

→ debt:
OWES_ME


"Ads 700 000 qarz"

→ debt:
OWES_ME


"Ads menga 700 ming qarz"

→ debt:
OWES_ME


"Ads menga 700 ming berishi kerak"

→ debt:
OWES_ME


"Adsdan 700 ming olishim kerak"

→ debt:
OWES_ME


=========================================================
6. MENING QARZIM
=========================================================

"Azizga 2 mln qarzim bor"

Bu faqat qarzdorlik.

transaction yaratma.

debt_type = I_OWE

Bu qarz "Qarzdorlar" ro'yxatida
KO'RSATILMAYDI.


=========================================================
7. MENGA QARZDOR PULNI QAYTARDI
=========================================================

"Ads 400 ming qaytardi"

→ transaction:
INCOME
amount = 400000
person = Ads
debt_action = REPAY

→ debt:
REPAY
person = Ads
amount = 400000
debt_type = OWES_ME

Balansga pul QO'SHILADI.


=========================================================
8. MEN QARZIMNI QAYTARDIM
=========================================================

"Azizga 400 ming qarzimni qaytardim"

→ transaction:
EXPENSE
amount = 400000
person = Aziz
debt_action = REPAY

→ debt:
REPAY
person = Aziz
amount = 400000
debt_type = I_OWE

Balansdan pul AYRILADI.


=========================================================
9. QARZ YOZISH BALANSNI O'ZGARTIRMAYDI
=========================================================

"Qarzdor Ads 700 ming"

Balansga +700000 QO'SHILMASIN.

Faqat qarzdorlikka yozilsin.


=========================================================
10. BIR NECHTA OPERATSIYA
=========================================================

"Klientdan 5 mln tushdi
materialga 1.2 mln ketdi
ishchiga 800 ming ketdi"

3 ta transaction yarat.


=========================================================
11. PERSON
=========================================================

"Qarzdor Ads 700 ming"

person = Ads

"Azizdan 2 mln qarz oldim"

person = Aziz

"Jasur 500 ming qarz"

person = Jasur

Agar odam aniq aytilgan bo'lsa
person = null qilma.


=========================================================
12. AMOUNT
=========================================================

"5 mln" = 5000000

"1.2 mln" = 1200000

"800 ming" = 800000

"700 000" = 700000

"700 ming" = 700000

"2 million" = 2000000


=========================================================
13. FAQAT QARZ HAQIDA GAP
=========================================================

"Qarzdor Ads 700 ming"

Natija:

{
 "transactions": [],
 "debts": [
   {
     "action": "NEW",
     "person": "Ads",
     "amount": 700000,
     "debt_type": "OWES_ME"
   }
 ]
}


=========================================================
14. JAVOB
=========================================================

Faqat JSON qaytar.

Markdown yozma.

Izoh yozma.
"""

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": system
            },
            {
                "role": "user",
                "content": text
            }
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
            timeout=30
        )

        r.raise_for_status()

        data = r.json()

        content = (
            data["choices"][0]["message"]["content"]
            .strip()
        )

        content = re.sub(
            r"^```json",
            "",
            content,
            flags=re.IGNORECASE
        )

        content = re.sub(
            r"^```",
            "",
            content
        )

        content = re.sub(
            r"```$",
            "",
            content
        )

        content = content.strip()

        parsed = json.loads(content)

        return {
            "transactions": parsed.get(
                "transactions",
                []
            ),
            "debts": parsed.get(
                "debts",
                []
            )
        }

    except Exception as e:

        print(
            "GROQ ERROR:",
            repr(e)
        )

        return None


# =========================================================
# PARSE TEXT
# =========================================================

def parse_text(text):

    fast = fast_parse(text)

    if fast:

        print(
            "FAST:",
            fast
        )

        return fast

    ai = groq_parse(text)

    print(
        "AI:",
        ai
    )

    return ai


# =========================================================
# DEBT FUNCTIONS
# =========================================================

def add_debt(
    user_id,
    period_id,
    person,
    amount,
    debt_type,
    conn=None
):

    if not person:
        return

    person = person.strip()

    if not person:
        return

    own_conn = False

    if conn is None:

        conn = db()
        own_conn = True

    cur = conn.cursor()

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

        new_amount = (
            float(row["amount"])
            + float(amount)
        )

        cur.execute("""
            UPDATE debts
            SET amount = %s,
                person = %s
            WHERE id = %s
        """, (
            new_amount,
            person,
            row["id"]
        ))

    else:

        cur.execute("""
            INSERT INTO debts
            (
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

    if own_conn:

        conn.commit()
        cur.close()
        conn.close()


def change_debt(
    user_id,
    period_id,
    person,
    amount,
    debt_type,
    conn=None
):

    if not person:
        return

    person = person.strip()

    own_conn = False

    if conn is None:

        conn = db()
        own_conn = True

    cur = conn.cursor()

    cur.execute("""
        SELECT id, amount
        FROM debts
        WHERE user_id = %s
          AND period_id = %s
          AND LOWER(TRIM(person))
              = LOWER(TRIM(%s))
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

        new_amount = (
            float(row["amount"])
            - float(amount)
        )

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

    if own_conn:

        conn.commit()
        cur.close()
        conn.close()


# =========================================================
# SAVE DATA
# =========================================================

def save_data(user_id, parsed):

    period_id = get_current_period(
        user_id
    )

    transactions = parsed.get(
        "transactions",
        []
    )

    debts = parsed.get(
        "debts",
        []
    )

    saved = []
    debt_changes = []

    conn = db()
    cur = conn.cursor()

    try:

        # =================================================
        # DEBT ONLY
        # =================================================

        for debt in debts:

            action = (
                debt.get("action")
                or "NEW"
            )

            person = (
                debt.get("person")
                or ""
            ).strip()

            amount = float(
                debt.get("amount")
                or 0
            )

            debt_type = (
                debt.get("debt_type")
                or "OWES_ME"
            )

            if not person:
                continue

            if amount <= 0:
                continue

            if debt_type not in [
                "OWES_ME",
                "I_OWE"
            ]:
                continue

            if action == "NEW":

                add_debt(
                    user_id,
                    period_id,
                    person,
                    amount,
                    debt_type,
                    conn
                )

                debt_changes.append({
                    "action": "NEW",
                    "person": person,
                    "amount": amount,
                    "debt_type": debt_type
                })

            elif action == "REPAY":

                change_debt(
                    user_id,
                    period_id,
                    person,
                    amount,
                    debt_type,
                    conn
                )

                debt_changes.append({
                    "action": "REPAY",
                    "person": person,
                    "amount": amount,
                    "debt_type": debt_type
                })

        # =================================================
        # TRANSACTIONS
        # =================================================

        for item in transactions:

            kind = item.get("type")

            amount = float(
                item.get("amount")
                or 0
            )

            if amount <= 0:
                continue

            if kind not in [
                "INCOME",
                "EXPENSE",
                "DEBT_IN",
                "DEBT_OUT"
            ]:
                continue

            person = item.get(
                "person"
            )

            category = (
                item.get("category")
                or (
                    "Qarzdan kirim"
                    if kind == "DEBT_IN"
                    else "Qarzga chiqim"
                    if kind == "DEBT_OUT"
                    else "Xarajat"
                )
            )

            note = (
                item.get("note")
                or ""
            )

            debt_action = (
                item.get("debt_action")
                or "NONE"
            )

            cur.execute("""
                INSERT INTO transactions
                (
                    user_id,
                    period_id,
                    kind,
                    amount,
                    person,
                    category,
                    note
                )
                VALUES
                (%s, %s, %s, %s, %s, %s, %s)
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

            transaction_id = (
                cur.fetchone()["id"]
            )

            # Qarzdan pul olish
            if kind == "DEBT_IN":

                add_debt(
                    user_id,
                    period_id,
                    person,
                    amount,
                    "I_OWE",
                    conn
                )

            # Birovga qarz berish
            elif kind == "DEBT_OUT":

                add_debt(
                    user_id,
                    period_id,
                    person,
                    amount,
                    "OWES_ME",
                    conn
                )

            # Qarz qaytarish
            elif debt_action == "REPAY":

                if kind == "EXPENSE":

                    change_debt(
                        user_id,
                        period_id,
                        person,
                        amount,
                        "I_OWE",
                        conn
                    )

                elif kind == "INCOME":

                    change_debt(
                        user_id,
                        period_id,
                        person,
                        amount,
                        "OWES_ME",
                        conn
                    )

            saved.append({
                "id": transaction_id,
                "type": kind,
                "amount": amount,
                "category": category,
                "person": person,
                "debt_action": debt_action
            })

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        cur.close()
        conn.close()

    return {
        "transactions": saved,
        "debts": debt_changes
    }


# =========================================================
# REPORT
# =========================================================

def get_report(user_id):

    period_id = get_current_period(
        user_id
    )

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT

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

    income = float(
        row["income"]
    )

    expense = float(
        row["expense"]
    )

    debt_in = float(
        row["debt_in"]
    )

    debt_out = float(
        row["debt_out"]
    )

    balance = (
        income
        + debt_in
        - expense
        - debt_out
    )

    return {
        "income": income,
        "expense": expense,
        "debt_in": debt_in,
        "debt_out": debt_out,
        "balance": balance
    }


# =========================================================
# TRANSACTIONS
# =========================================================

def get_transactions(
    user_id,
    kind=None
):

    period_id = get_current_period(
        user_id
    )

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
# DEBTORS
# =========================================================

def get_debtors(user_id):

    period_id = get_current_period(
        user_id
    )

    conn = db()
    cur = conn.cursor()

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

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# =========================================================
# DELETE LAST TRANSACTION
# =========================================================

def delete_last_transaction(user_id):

    period_id = get_current_period(
        user_id
    )

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

    # =====================================================
    # QARZ OLISHNI O'CHIRISH
    # =====================================================

    if row["kind"] == "DEBT_IN":

        cur.execute("""
            SELECT id, amount
            FROM debts
            WHERE user_id = %s
              AND period_id = %s
              AND LOWER(TRIM(person))
                  = LOWER(TRIM(%s))
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

            new_amount = (
                float(debt["amount"])
                - float(row["amount"])
            )

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

    # =====================================================
    # QARZ BERISHNI O'CHIRISH
    # =====================================================

    elif row["kind"] == "DEBT_OUT":

        cur.execute("""
            SELECT id, amount
            FROM debts
            WHERE user_id = %s
              AND period_id = %s
              AND LOWER(TRIM(person))
                  = LOWER(TRIM(%s))
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

            new_amount = (
                float(debt["amount"])
                - float(row["amount"])
            )

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

    # =====================================================
    # QARZ QAYTARISHNI O'CHIRISH
    # =====================================================

    elif row["debt_action"] == "REPAY":

        if row["kind"] == "INCOME":

            add_debt(
                user_id,
                period_id,
                row["person"],
                row["amount"],
                "OWES_ME",
                conn
            )

        elif row["kind"] == "EXPENSE":

            add_debt(
                user_id,
                period_id,
                row["person"],
                row["amount"],
                "I_OWE",
                conn
            )

    # =====================================================
    # TRANSACTIONNI O'CHIRISH
    # =====================================================

    cur.execute(
        "DELETE FROM transactions WHERE id = %s",
        (row["id"],)
    )

    conn.commit()

    cur.close()
    conn.close()

    return row


# =========================================================
# KEYBOARD
# =========================================================

def keyboard():

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
            ],
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
        "👋 MyFinance AI ga xush kelibsiz!\n\n"
        "Pul harakatini oddiy tilda yozing "
        "yoki 🎤 ovozli xabar yuboring.\n\n"
        "Masalan:\n"
        "• Klientdan 5 mln tushdi\n"
        "• Materialga 1.2 mln ketdi\n"
        "• Qarzdor Ads 700 ming\n"
        "• Ads 400 ming qaytardi",
        reply_markup=keyboard()
    )


# =========================================================
# INCOME
# =========================================================

async def show_income(
    update,
    context
):

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

    total = sum(
        float(x["amount"])
        for x in rows
    )

    text = "💰 TUSHUMLAR\n\n"

    for row in reversed(rows):

        text += (
            f"• {row['category']}: "
            f"{money(row['amount'])}\n"
        )

    text += (
        f"\nJami: {money(total)}"
    )

    await update.message.reply_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# EXPENSE
# =========================================================

async def show_expenses(
    update,
    context
):

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

    total = sum(
        float(x["amount"])
        for x in rows
    )

    text = "💸 XARAJATLAR\n\n"

    for row in reversed(rows):

        text += (
            f"• {row['category']}: "
            f"{money(row['amount'])}\n"
        )

    text += (
        f"\nJami: {money(total)}"
    )

    await update.message.reply_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# REPORT
# =========================================================

async def show_report(
    update,
    context
):

    r = get_report(
        update.effective_user.id
    )

    text = (
        "📊 HISOBOT\n\n"

        f"💰 Tushum: "
        f"{money(r['income'])}\n"

        f"💸 Xarajat: "
        f"{money(r['expense'])}\n\n"

        f"🤝 Qarzdan kirim: "
        f"{money(r['debt_in'])}\n"

        f"🤝 Qarzga chiqim: "
        f"{money(r['debt_out'])}\n\n"

        f"🟢 Qoldiq: "
        f"{money(r['balance'])}"
    )

    await update.message.reply_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# DEBTORS
# =========================================================

async def show_debtors(
    update,
    context
):

    rows = get_debtors(
        update.effective_user.id
    )

    if not rows:

        await update.message.reply_text(
            "🤝 QARZDORLAR\n\n"
            "Hozircha sizga qarzdor odam yo'q.",
            reply_markup=keyboard()
        )

        return

    text = "🤝 QARZDORLAR\n\n"

    total = 0

    for i, row in enumerate(
        rows,
        1
    ):

        amount = float(
            row["amount"]
        )

        total += amount

        text += (
            f"{i}. {row['person']} — "
            f"{money(amount)}\n"
        )

    text += (
        f"\n💰 Jami: {money(total)}"
    )

    await update.message.reply_text(
        text,
        reply_markup=keyboard()
    )


# =========================================================
# DELETE
# =========================================================

async def delete_last(
    update,
    context
):

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
        f"{row['category']} — "
        f"{money(row['amount'])}",
        reply_markup=keyboard()
    )


# =========================================================
# NEW PERIOD
# =========================================================

async def new_period(
    update,
    context
):

    context.user_data[
        "confirm_new_period"
    ] = True

    await update.message.reply_text(
        "⚠️ Yangi hisob ochilsinmi?\n\n"
        "Eski hisoblar o'chirilmaydi.\n"
        "Faqat hozirgi hisob 0 dan boshlanadi.\n\n"
        "Tasdiqlash uchun: HA"
    )


# =========================================================
# PROCESS MONEY TEXT
# =========================================================

async def process_money_text(
    update,
    text,
    user_id
):

    print(
        "USER",
        user_id,
        ":",
        text
    )

    parsed = parse_text(
        text
    )

    if not parsed:

        await update.message.reply_text(
            "🤔 Tushunmadim.\n\n"
            "Masalan:\n"
            "Klientdan 5 mln tushdi\n"
            "Materialga 1.2 mln ketdi\n"
            "Qarzdor Ads 700 ming",
            reply_markup=keyboard()
        )

        return

    result_data = save_data(
        user_id,
        parsed
    )

    saved = result_data[
        "transactions"
    ]

    debt_changes = result_data[
        "debts"
    ]

    result = ""

    # =====================================================
    # DEBT ONLY
    # =====================================================

    for debt in debt_changes:

        if debt["debt_type"] == "OWES_ME":

            if debt["action"] == "NEW":

                result += (
                    f"🤝 Qarzdor: "
                    f"{debt['person']} — "
                    f"{money(debt['amount'])}\n"
                )

            elif debt["action"] == "REPAY":

                result += (
                    f"🤝 {debt['person']} qarzi "
                    f"{money(debt['amount'])} ga kamaydi\n"
                )

    # =====================================================
    # TRANSACTIONS
    # =====================================================

    for item in saved:

        if item["type"] == "INCOME":

            if item["debt_action"] == "REPAY":

                result += (
                    f"💰 {item['person'] or 'Qarz'} "
                    f"{money(item['amount'])} qaytardi\n"
                )

            else:

                result += (
                    f"💰 Tushum: "
                    f"{money(item['amount'])}\n"
                )

        elif item["type"] == "EXPENSE":

            if item["debt_action"] == "REPAY":

                result += (
                    f"💸 {item['person'] or 'Qarz'} "
                    f"uchun {money(item['amount'])} "
                    f"qaytarildi\n"
                )

            else:

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

    if not result:

        await update.message.reply_text(
            "🤔 Ma'lumotni tushundim, "
            "lekin saqlashga operatsiya topilmadi.",
            reply_markup=keyboard()
        )

        return

    report = get_report(
        user_id
    )

    result += (
        f"\n🟢 Qoldiq: "
        f"{money(report['balance'])}"
    )

    await update.message.reply_text(
        result,
        reply_markup=keyboard()
    )


# =========================================================
# MAIN HANDLER
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    # =====================================================
    # VOICE
    # =====================================================

    if update.message.voice:

        await update.message.reply_text(
            "🎤 Ovoz qabul qilindi...\n"
            "⏳ Ovoz tushunilmoqda..."
        )

        try:

            voice_file = (
                await update.message.voice
                .get_file()
            )

            audio_bytes = (
                await voice_file
                .download_as_bytearray()
            )

            # ---------------------------------------------
            # 1. WHISPER
            # ---------------------------------------------

            raw_text = transcribe_audio(
                bytes(audio_bytes),
                "voice.ogg"
            )

            if not raw_text:

                await update.message.reply_text(
                    "❌ Ovozni tushunib bo'lmadi.",
                    reply_markup=keyboard()
                )

                return

            print(
                "USER VOICE RAW:",
                user_id,
                ":",
                raw_text
            )

            # ---------------------------------------------
            # 2. VOICE TEXT NORMALIZATION
            # ---------------------------------------------

            normalized_text = (
                normalize_voice_text(
                    raw_text
                )
            )

            print(
                "USER VOICE NORMALIZED:",
                user_id,
                ":",
                normalized_text
            )

            # ---------------------------------------------
            # 3. FINANCE PARSER
            # ---------------------------------------------

            await process_money_text(
                update,
                normalized_text,
                user_id
            )

        except Exception as e:

            print(
                "VOICE HANDLER ERROR:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Ovozli xabarni qayta ishlashda "
                "xatolik yuz berdi.",
                reply_markup=keyboard()
            )

        return

    # =====================================================
    # TEXT
    # =====================================================

    text = (
        update.message.text or ""
    ).strip()

    # =====================================================
    # NEW PERIOD
    # =====================================================

    if context.user_data.get(
        "confirm_new_period"
    ):

        if text.lower() in [
            "ha",
            "xa",
            "yes",
            "да"
        ]:

            create_new_period(
                user_id
            )

            context.user_data[
                "confirm_new_period"
            ] = False

            await update.message.reply_text(
                "✅ Yangi hisob ochildi.\n\n"
                "🟢 Qoldiq: 0 so'm\n\n"
                "Eski hisoblar saqlanib qoldi.",
                reply_markup=keyboard()
            )

        else:

            context.user_data[
                "confirm_new_period"
            ] = False

            await update.message.reply_text(
                "❌ Bekor qilindi.",
                reply_markup=keyboard()
            )

        return

    # =====================================================
    # BUTTONS
    # =====================================================

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

    if text in [
        "🤝 Qarzlar",
        "🤝 Qarzdorlar"
    ]:

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
    # MONEY TEXT
    # =====================================================

    await process_money_text(
        update,
        text,
        user_id
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

    init_db()

    app = (
        Application.builder()
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
            (
                filters.TEXT
                | filters.VOICE
            )
            & ~filters.COMMAND,
            handle_message
        )
    )

    app.add_error_handler(
        error_handler
    )

    if not RENDER_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL topilmadi. "
            "Render Web Service sifatida "
            "ishlayotganini tekshiring."
        )

    webhook_url = (
        f"{RENDER_URL.rstrip('/')}/"
        f"{WEBHOOK_PATH}"
    )

    print(
        "Webhook URL:",
        webhook_url
    )

    print(
        "BOT ISHLAYAPTI..."
    )

    print(
        "PORT:",
        PORT
    )

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
