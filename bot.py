import os
import re
import json
import requests
import tempfile

import psycopg
from psycopg.rows import dict_row

from telegram import (
    Update,
    ReplyKeyboardMarkup,
)
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

            # Eski bazaga ham yangi ustun qo'shiladi
            cur.execute("""
                ALTER TABLE transactions
                ADD COLUMN IF NOT EXISTS debt_action TEXT DEFAULT 'NONE'
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
# AMOUNT PARSE
# =========================================================

def parse_amount(text):
    if not text:
        return None

    t = text.lower()

    # 2 500 000
    m = re.search(r'(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+)(?!\d)', t)
    if m:
        raw = re.sub(r'[\s.,]', '', m.group(1))
        try:
            return float(raw)
        except Exception:
            pass

    # 2.5 mln / 2,5 mln
    m = re.search(
        r'(\d+(?:[.,]\d+)?)\s*(mln|million|millon|млн)',
        t
    )
    if m:
        return float(m.group(1).replace(",", ".")) * 1_000_000

    # 700 ming
    m = re.search(
        r'(\d+(?:[.,]\d+)?)\s*(ming|минг)',
        t
    )
    if m:
        return float(m.group(1).replace(",", ".")) * 1_000

    # 700 000 so'm
    m = re.search(
        r'(\d+(?:[\s.,]\d+)*)\s*(so.?m|som|sum|сум)',
        t
    )
    if m:
        raw = re.sub(r'[\s.,]', '', m.group(1))
        try:
            return float(raw)
        except Exception:
            pass

    # oddiy son
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

def clean_person(person):
    if not person:
        return None

    person = str(person).strip()

    if not person:
        return None

    bad = {
        "noma'lum",
        "noma'lum",
        "unknown",
        "null",
        "none",
    }

    if person.lower() in bad:
        return None

    return person


def guess_person(text):
    t = text.strip()

    patterns = [
        # Ads 700 ming qarzini berdi
        r'^\s*([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)\s+\d',
        # Adsga
        r'([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)ga\b',
        # Adsdan
        r'([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)dan\b',
        # Ads menga
        r'([A-Za-zА-Яа-яЎўҚқҒғҲҳ0-9_.-]+)\s+menga\b',
    ]

    for pattern in patterns:
        m = re.search(pattern, t, re.IGNORECASE)
        if m:
            p = clean_person(m.group(1))
            if p:
                return p

    return None


# =========================================================
# ENG MUHIM: QARZINI BERDI / QAYTARDI
# =========================================================

def fast_repayment_parse(text):
    """
    Masalan:
      Ads 700 ming qarzini berdi
      Ads 700 ming qarzini qaytardi
      Ads menga 700 ming qarzini berdi
      Ads qarzidan 700 ming berdi
      Ads 700 ming qarzini to'ladi

    Bular:
      INCOME + REPAY + OWES_ME
    """

    t = text.lower().strip()

    repayment_patterns = [
        r'qarzini\s+(?:berdi|qaytardi|to.?ladi)',
        r'qarzidan\s+.+\s+berdi',
        r'qarzidan\s+.+\s+qaytardi',
        r'qarzini\s+.+\s+(?:berdi|qaytardi|to.?ladi)',
    ]

    is_repayment = any(
        re.search(p, t, re.IGNORECASE)
        for p in repayment_patterns
    )

    if not is_repayment:
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
                "debt_action": "REPAY",
            }
        ],
        "debts": [
            {
                "action": "REPAY",
                "person": person,
                "amount": amount,
                "debt_type": "OWES_ME",
            }
        ]
    }


# =========================================================
# FAST PARSER
# =========================================================

def fast_parse(text):
    t = text.lower().strip()

    # Avval qarz qaytimini tekshiramiz
    repayment = fast_repayment_parse(text)
    if repayment:
        return repayment

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

    amount = parse_amount(text)

    if amount is None:
        return None

    expense_words = [
        "ishlatdim",
        "sarfladim",
        "xarajat",
        "ketdi",
        "oldim",
        "sotib oldim",
        "to'ladim",
        "toladim",
        "berdim",
    ]

    income_words = [
        "oldim",
        "tushdi",
        "tushum",
        "keldi",
        "topdim",
        "daromad",
    ]

    category = "Boshqa"

    category_map = {
        "reklama": "Reklama",
        "ishchi": "Ishchi",
        "material": "Material",
        "yo'l": "Yo‘l",
        "transport": "Transport",
        "ovqat": "Ovqat",
        "telefon": "Telefon",
        "internet": "Internet",
    }

    for word, cat in category_map.items():
        if word in t:
            category = cat
            break

    # "oldim" ba'zida xarajat, ba'zida tushum bo'ladi.
    # "pul oldim" => tushum
    if (
        "pul oldim" in t
        or "mijozdan oldim" in t
        or "klientdan oldim" in t
        or "tushdi" in t
        or "tushum" in t
    ):
        return {
            "transactions": [
                {
                    "type": "INCOME",
                    "amount": amount,
                    "person": None,
                    "category": category,
                    "note": text,
                    "debt_action": "NONE",
                }
            ],
            "debts": []
        }

    if any(x in t for x in expense_words):
        return {
            "transactions": [
                {
                    "type": "EXPENSE",
                    "amount": amount,
                    "person": None,
                    "category": category,
                    "note": text,
                    "debt_action": "NONE",
                }
            ],
            "debts": []
        }

    if any(x in t for x in income_words):
        return {
            "transactions": [
                {
                    "type": "INCOME",
                    "amount": amount,
                    "person": None,
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

    url = "https://api.groq.com/openai/v1/chat/completions"

    system_prompt = r"""
Sen o'zbek tilidagi shaxsiy moliya botining parserisan.

Faqat JSON qaytar.

FORMAT:

{
  "transactions": [
    {
      "type": "INCOME" yoki "EXPENSE",
      "amount": 0,
      "person": null,
      "category": "Boshqa",
      "note": "",
      "debt_action": "NONE" yoki "REPAY"
    }
  ],
  "debts": [
    {
      "action": "ADD" yoki "REPAY",
      "person": "",
      "amount": 0,
      "debt_type": "OWES_ME" yoki "I_OWE"
    }
  ]
}

=========================================================
ODDIY TUSHUM
=========================================================

"Mijozdan 2 mln oldim"
=> INCOME

"2 mln tushdi"
=> INCOME

=========================================================
ODDIY XARAJAT
=========================================================

"Reklamaga 300 ming ishlatdim"
=> EXPENSE

"200 ming sarfladim"
=> EXPENSE

"Ishchiga 1 mln berdim"
=> EXPENSE

=========================================================
QARZ: MEN PUL OLDIM
=========================================================

"Azizdan 2 mln qarz oldim"

Bu:
=> DEBT_IN

Balans:
=> +2000000

Debt:
=> I_OWE

Transactions:

{
  "type": "INCOME",
  "amount": 2000000,
  "person": "Aziz",
  "category": "Qarz olindi",
  "debt_action": "NONE"
}

Debts:

{
  "action": "ADD",
  "person": "Aziz",
  "amount": 2000000,
  "debt_type": "I_OWE"
}

=========================================================
QARZ: MEN BOSHQA ODAMGA PUL BERDIM
=========================================================

"Azizga 2 mln qarz berdim"
"Men Azizga 2 mln qarz berdim"

Bu:
=> DEBT_OUT

Balans:
=> -2000000

Debt:
=> OWES_ME

=========================================================
DEBT-ONLY
=========================================================

"Qarzdor Ads 700 ming"
"Ads 700 ming qarz"
"Ads yana 300 ming qarz"

Bu faqat qarzdorlik:

=> debt ADD
=> OWES_ME

Balans O'ZGARMAYDI.

=========================================================
MENING QARZIM
=========================================================

"Azizga 2 mln qarzim bor"
"Azizga 2 mln qarzdorman"

Bu:

=> debt ADD
=> I_OWE

Balans O'ZGARMAYDI.

Bunday odam Qarzdorlar ro'yxatida ko'rsatilmaydi.

=========================================================
JUDA MUHIM: QARZINI BERDI / QARZINI QAYTARDI
=========================================================

"Ads 700 ming qarzini berdi"
"Ads 700 ming qarzini qaytardi"
"Ads menga 700 ming qarzini berdi"
"Ads 700 ming qarzini to'ladi"
"Ads qarzidan 700 ming berdi"

BULARNING HAMMASI:

=> INCOME
=> debt_action = REPAY
=> person = Ads

Va debt:

=> action = REPAY
=> debt_type = OWES_ME

Balansga PUL QO'SHILADI.

Misol:

"Ads 700 ming qarzini berdi"

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
  "debts": [
    {
      "action": "REPAY",
      "person": "Ads",
      "amount": 700000,
      "debt_type": "OWES_ME"
    }
  ]
}

BU HOLATDA HECH QACHON DEBT_OUT QILMA.

=========================================================
FARQ
=========================================================

"Men Adsga 700 ming qarz berdim"
=> DEBT_OUT
=> Ads menga qarzdor bo'ladi
=> balans -700000

"Ads menga 700 ming qarzini berdi"
=> INCOME + REPAY
=> Ads qarzi kamayadi
=> balans +700000

"Ads 700 ming qaytardi"
=> INCOME + REPAY

"Ads 700 ming qarzini qaytardi"
=> INCOME + REPAY

=========================================================
REPAY
=========================================================

"Ads 400 ming qaytardi"

=> INCOME
=> debt_action = REPAY
=> debt_type = OWES_ME

=========================================================
MEN O'Z QARZIMNI QAYTARDIM
=========================================================

"Azizga 400 ming qarzimni qaytardim"

=> EXPENSE
=> debt_action = REPAY

Debt:

=> I_OWE kamayadi

Balans:
=> -400000

=========================================================
MUHIM
=========================================================

"qarzini berdi" degan iborada odam PULNI FOYDALANUVCHIGA BERGAN bo'ladi.

"qarz berdim" degan iborada foydalanuvchi boshqa odamga PUL BERGAN bo'ladi.

Bu ikkisini aslo aralashtirma.

Bir xabarda bir nechta operatsiya bo'lsa, hammasini transactions va debts ichiga alohida yoz.
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
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )

        r.raise_for_status()

        data = r.json()

        content = data["choices"][0]["message"]["content"].strip()

        # Markdown JSON bo'lsa tozalaymiz
        content = re.sub(r"^```json", "", content, flags=re.I).strip()
        content = re.sub(r"^```", "", content).strip()
        content = re.sub(r"```$", "", content).strip()

        parsed = json.loads(content)

        if not isinstance(parsed, dict):
            return None

        parsed.setdefault("transactions", [])
        parsed.setdefault("debts", [])

        return parsed

    except Exception as e:
        print("GROQ ERROR:", e)
        return None


# =========================================================
# PARSE TEXT
# =========================================================

def parse_text(text):
    # 1. Maxsus repayment
    result = fast_repayment_parse(text)
    if result:
        return result

    # 2. Oddiy fast parser
    result = fast_parse(text)
    if result:
        return result

    # 3. AI
    return groq_parse(text)


# =========================================================
# DEBT FUNCTIONS
# =========================================================

def add_debt(user_id, period_id, person, amount, debt_type):
    person = clean_person(person)

    if not person or amount <= 0:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:

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
                debt_type,
            ))

            row = cur.fetchone()

            if row:
                cur.execute("""
                    UPDATE debts
                    SET amount = amount + %s
                    WHERE id = %s
                """, (
                    amount,
                    row["id"],
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
                    debt_type,
                ))

        conn.commit()


def change_debt(user_id, period_id, person, amount, debt_type):
    person = clean_person(person)

    if not person or amount <= 0:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:

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
                debt_type,
            ))

            row = cur.fetchone()

            if not row:
                conn.commit()
                return

            new_amount = float(row["amount"]) - float(amount)

            if new_amount <= 0:
                cur.execute("""
                    DELETE FROM debts
                    WHERE id = %s
                """, (row["id"],))
            else:
                cur.execute("""
                    UPDATE debts
                    SET amount = %s
                    WHERE id = %s
                """, (
                    new_amount,
                    row["id"],
                ))

        conn.commit()


# =========================================================
# SAVE DATA
# =========================================================

def save_data(user_id, parsed, source_text=None):
    period = get_current_period(user_id)
    period_id = period["id"]

    transactions = parsed.get("transactions", [])
    debts = parsed.get("debts", [])

    saved = []

    with get_conn() as conn:
        with conn.cursor() as cur:

            # -----------------------------
            # TRANSACTIONS
            # -----------------------------

            for tx in transactions:

                tx_type = str(
                    tx.get("type", "")
                ).upper().strip()

                try:
                    amount = float(tx.get("amount", 0))
                except Exception:
                    amount = 0

                if tx_type not in {"INCOME", "EXPENSE"}:
                    continue

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

                debt_action = (
                    tx.get("debt_action")
                    or "NONE"
                )

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
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    user_id,
                    period_id,
                    tx_type,
                    amount,
                    person,
                    category,
                    note,
                    debt_action,
                ))

                tx_id = cur.fetchone()["id"]

                saved.append({
                    "id": tx_id,
                    "type": tx_type,
                    "amount": amount,
                    "person": person,
                    "category": category,
                    "debt_action": debt_action,
                })

                # DEBT_IN / qarz olindi
                if tx.get("debt_action") == "DEBT_IN":
                    if person:
                        add_debt(
                            user_id,
                            period_id,
                            person,
                            amount,
                            "I_OWE"
                        )

                # DEBT_OUT / qarz berildi
                elif tx.get("debt_action") == "DEBT_OUT":
                    if person:
                        add_debt(
                            user_id,
                            period_id,
                            person,
                            amount,
                            "OWES_ME"
                        )

            # -----------------------------
            # DEBTS
            # -----------------------------

            for debt in debts:

                action = str(
                    debt.get("action", "")
                ).upper().strip()

                person = clean_person(
                    debt.get("person")
                )

                debt_type = str(
                    debt.get("debt_type", "")
                ).upper().strip()

                try:
                    amount = float(
                        debt.get("amount", 0)
                    )
                except Exception:
                    amount = 0

                if not person or amount <= 0:
                    continue

                if action == "ADD":

                    add_debt(
                        user_id,
                        period_id,
                        person,
                        amount,
                        debt_type
                    )

                elif action == "REPAY":

                    change_debt(
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
                    COALESCE(SUM(
                        CASE
                            WHEN kind = 'INCOME'
                            THEN amount
                            WHEN kind = 'EXPENSE'
                            THEN -amount
                            ELSE 0
                        END
                    ), 0) AS balance
                FROM transactions
                WHERE user_id = %s
                  AND period_id = %s
            """, (
                user_id,
                period_id,
            ))

            row = cur.fetchone()

            return float(row["balance"] or 0)


# =========================================================
# REPORT
# =========================================================

def get_report(user_id):
    period = get_current_period(user_id)
    period_id = period["id"]

    with get_conn() as conn:
        with conn.cursor() as cur:

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
                    ), 0) AS expense

                FROM transactions
                WHERE user_id = %s
                  AND period_id = %s
            """, (
                user_id,
                period_id,
            ))

            row = cur.fetchone()

    income = float(row["income"] or 0)
    expense = float(row["expense"] or 0)
    balance = income - expense

    return {
        "period": period,
        "income": income,
        "expense": expense,
        "balance": balance,
    }


# =========================================================
# TRANSACTIONS
# =========================================================

def get_transactions(user_id, kind=None, limit=20):
    period = get_current_period(user_id)
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
                    limit,
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
                    limit,
                ))

            return cur.fetchall()


# =========================================================
# DEBTORS
# =========================================================

def get_debtors(user_id):
    period = get_current_period(user_id)
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
                  AND LOWER(TRIM(person)) <> 'noma''lum'
                GROUP BY person
                HAVING SUM(amount) > 0
                ORDER BY SUM(amount) DESC
            """, (
                user_id,
                period_id,
            ))

            return cur.fetchall()


# =========================================================
# DELETE LAST TRANSACTION
# =========================================================

def delete_last_transaction(user_id):
    period = get_current_period(user_id)
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
                period_id,
            ))

            tx = cur.fetchone()

            if not tx:
                return None

            kind = tx["kind"]
            amount = float(tx["amount"] or 0)
            person = clean_person(tx["person"])
            debt_action = (
                tx["debt_action"]
                or "NONE"
            )

            # -------------------------------------
            # REPAY ni o'chirish
            # -------------------------------------

            if (
                debt_action == "REPAY"
                and person
            ):
                debt_type = (
                    "OWES_ME"
                    if kind == "INCOME"
                    else "I_OWE"
                )

                add_debt(
                    user_id,
                    period_id,
                    person,
                    amount,
                    debt_type
                )

            # -------------------------------------
            # DEBT IN / OUT bo'lsa
            # -------------------------------------

            elif (
                debt_action == "DEBT_IN"
                and person
            ):
                add_debt(
                    user_id,
                    period_id,
                    person,
                    amount,
                    "I_OWE"
                )

            elif (
                debt_action == "DEBT_OUT"
                and person
            ):
                add_debt(
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
# VOICE -> TEXT
# =========================================================

def transcribe_audio(audio_bytes, filename="voice.ogg"):
    if not GROQ_API_KEY:
        return None

    url = "https://api.groq.com/openai/v1/audio/transcriptions"

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
            "O'zbek tilidagi pul hisoboti. "
            "So'm, million, mln, ming, tushdi, "
            "keldi, ketdi, berdim, oldim, ishlatdim, "
            "sarfladim, qarz, qarzdor, "
            "qarzini berdi, qarzini qaytardi, "
            "qaytardi, material, ishchi, "
            "klient, mijoz, reklama kabi "
            "so'zlar bo'lishi mumkin."
        )
    }

    try:

        r = requests.post(
            url,
            headers={
                "Authorization":
                    f"Bearer {GROQ_API_KEY}"
            },
            files=files,
            data=data,
            timeout=120,
        )

        r.raise_for_status()

        result = r.json()

        text = (
            result.get("text")
            or ""
        ).strip()

        print("VOICE TEXT:", text)

        return text

    except Exception as e:
        print("WHISPER ERROR:", e)
        return None


# =========================================================
# KEYBOARD
# =========================================================

def main_keyboard():
    keyboard = [
        [
            "💰 Tushumlar",
            "💸 Xarajatlar",
        ],
        [
            "📊 Hisobot",
            "🤝 Qarzdorlar",
        ],
        [
            "🗑 Oxirgisini o'chirish",
        ],
        [
            "🔄 Yangi hisob — 0 dan",
        ],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )


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
    else:
        value = round(value, 2)

    return f"{value:,.0f}".replace(",", " ")


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    get_current_period(user_id)

    text = (
        "💰 <b>MyFinance AI</b>\n\n"
        "Daromad va xarajatlaringizni oddiy yozing:\n\n"
        "• Reklamaga 200 ming ishlatdim\n"
        "• 500 ming oldim\n"
        "• Ads 700 ming qarz\n"
        "• Ads 700 ming qarzini berdi\n\n"
        "Hammasini o'zi hisoblaydi."
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# =========================================================
# TUSHUMLAR
# =========================================================

async def show_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    rows = get_transactions(
        user_id,
        "INCOME",
        50
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

    for i, row in enumerate(rows, 1):

        amount = float(
            row["amount"] or 0
        )

        total += amount

        category = (
            row["category"]
            or "Boshqa"
        )

        lines.append(
            f"{i}. +{money(amount)} so'm — {category}"
        )

    lines += [
        "",
        f"💰 <b>Jami: {money(total)} so'm</b>"
    ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================================================
# XARAJATLAR
# =========================================================

async def show_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    rows = get_transactions(
        user_id,
        "EXPENSE",
        50
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

    for i, row in enumerate(rows, 1):

        amount = float(
            row["amount"] or 0
        )

        total += amount

        category = (
            row["category"]
            or "Boshqa"
        )

        lines.append(
            f"{i}. -{money(amount)} so'm — {category}"
        )

    lines += [
        "",
        f"💸 <b>Jami: {money(total)} so'm</b>"
    ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================================================
# REPORT
# =========================================================

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    report = get_report(user_id)

    text = (
        "📊 <b>HISOBOT</b>\n\n"
        f"💰 Tushum: +{money(report['income'])} so'm\n"
        f"💸 Xarajat: -{money(report['expense'])} so'm\n"
        f"🟢 Qoldiq: {money(report['balance'])} so'm\n"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# =========================================================
# DEBTORS
# =========================================================

async def show_debtors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    rows = get_debtors(user_id)

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

    for i, row in enumerate(rows, 1):

        person = clean_person(
            row["person"]
        )

        amount = float(
            row["amount"] or 0
        )

        total += amount

        lines.append(
            f"{i}. {person} — {money(amount)} so'm"
        )

    lines += [
        "",
        f"💰 <b>Jami: {money(total)} so'm</b>"
    ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================================================
# DELETE
# =========================================================

async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    tx = delete_last_transaction(user_id)

    if not tx:
        await update.message.reply_text(
            "🗑 O'chirish uchun yozuv yo'q."
        )
        return

    amount = float(
        tx["amount"] or 0
    )

    kind = tx["kind"]

    sign = "+" if kind == "INCOME" else "-"

    balance = get_balance(
        user_id,
        get_current_period(user_id)["id"]
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

async def new_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_new_period_confirm"] = True

    await update.message.reply_text(
        "🔄 Yangi hisobni 0 dan boshlaymizmi?\n\n"
        "Eski hisob o'chirilmaydi.\n\n"
        "Tasdiqlash uchun <b>ha</b> deb yozing.",
        parse_mode="HTML"
    )


# =========================================================
# PROCESS MONEY
# =========================================================

async def process_money_text(update: Update, text: str):
    user_id = update.effective_user.id

    parsed = parse_text(text)

    if not parsed:
        await update.message.reply_text(
            "❌ Tushunmadim.\n\n"
            "Masalan:\n"
            "• 500 ming oldim\n"
            "• 200 ming ishlatdim\n"
            "• Ads 700 ming qarz\n"
            "• Ads 700 ming qarzini berdi"
        )
        return

    period = save_data(
        user_id,
        parsed,
        text
    )

    balance = get_balance(
        user_id,
        period["id"]
    )

    lines = []

    # Transaction natijalari
    for tx in parsed.get("transactions", []):

        tx_type = (
            tx.get("type")
            or ""
        )

        try:
            amount = float(
                tx.get("amount", 0)
            )
        except Exception:
            amount = 0

        person = clean_person(
            tx.get("person")
        )

        category = (
            tx.get("category")
            or "Boshqa"
        )

        debt_action = (
            tx.get("debt_action")
            or "NONE"
        )

        if debt_action == "REPAY":
            lines.append(
                f"💰 Qarz qaytimi: +{money(amount)} so'm"
                + (
                    f" — {person}"
                    if person
                    else ""
                )
            )

        elif tx_type == "INCOME":
            lines.append(
                f"💰 Tushum: +{money(amount)} so'm"
            )

        elif tx_type == "EXPENSE":
            lines.append(
                f"💸 Xarajat: -{money(amount)} so'm"
            )

    # Debt natijalari
    for debt in parsed.get("debts", []):

        action = (
            debt.get("action")
            or ""
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

        debt_type = (
            debt.get("debt_type")
            or ""
        ).upper()

        if not person or amount <= 0:
            continue

        if action == "ADD" and debt_type == "OWES_ME":
            lines.append(
                f"🤝 Qarzdor: {person} — "
                f"{money(amount)} so'm"
            )

        elif action == "ADD" and debt_type == "I_OWE":
            lines.append(
                f"🤝 Siz {person}ga "
                f"{money(amount)} so'm qarzdorsiz"
            )

        elif action == "REPAY" and debt_type == "OWES_ME":
            lines.append(
                f"🤝 {person}ning qarzi "
                f"{money(amount)} so'mga kamaydi"
            )

        elif action == "REPAY" and debt_type == "I_OWE":
            lines.append(
                f"🤝 {person}ga qarzingiz "
                f"{money(amount)} so'mga kamaydi"
            )

    lines.append(
        f"\n🟢 Qoldiq: {money(balance)} so'm"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================================================
# MESSAGE HANDLER
# =========================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id

    # -------------------------------------
    # NEW PERIOD CONFIRM
    # -------------------------------------

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
            "tasdiqlayman",
        }:

            period = create_new_period(
                user_id
            )

            context.user_data[
                "waiting_new_period_confirm"
            ] = False

            await update.message.reply_text(
                "✅ Yangi hisob boshlandi.\n\n"
                "💰 Qoldiq: 0 so'm"
            )

            return

        if text in {
            "yo'q",
            "yoq",
            "bekor",
            "cancel",
        }:

            context.user_data[
                "waiting_new_period_confirm"
            ] = False

            await update.message.reply_text(
                "❌ Bekor qilindi."
            )

            return

    # -------------------------------------
    # BUTTONLAR
    # -------------------------------------

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

    # -------------------------------------
    # VOICE
    # -------------------------------------

    if update.message.voice:

        await update.message.chat.send_action(
            "typing"
        )

        try:
            voice = await update.message.voice.get_file()

            audio_bytes = await voice.download_as_bytearray()

            text = transcribe_audio(
                bytes(audio_bytes),
                "voice.ogg"
            )

            if not text:
                await update.message.reply_text(
                    "❌ Ovozni tushunib bo'lmadi."
                )
                return

            await process_money_text(
                update,
                text
            )

        except Exception as e:
            print("VOICE HANDLE ERROR:", e)

            await update.message.reply_text(
                "❌ Ovozli xabarni qayta ishlashda xato."
            )

        return

    # -------------------------------------
    # TEXT
    # -------------------------------------

    if text:
        await update.message.chat.send_action(
            "typing"
        )

        await process_money_text(
            update,
            text
        )


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    print(
        "ERROR:",
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

    # Render Webhook
    if RENDER_EXTERNAL_URL:

        webhook_url = (
            RENDER_EXTERNAL_URL.rstrip("/")
            + f"/{BOT_TOKEN}"
        )

        print(
            "WEBHOOK:",
            webhook_url
        )

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=webhook_url,
            secret_token=None,
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
