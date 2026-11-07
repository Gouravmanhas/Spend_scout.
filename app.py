"""SpendScout: Flask backend.

Run:  pip install -r requirements.txt && python app.py   ->  http://127.0.0.1:5000
"""
import os
import re
import secrets
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import Flask, g, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SPENDSCOUT_DB", os.path.join(BASE, "spendscout.db"))

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # reject oversized bodies

PLACES = [
    {"name": "Chai Point", "category": "Cafe", "per_person": 90, "distance": "0.4 km"},
    {"name": "Campus Canteen", "category": "Food", "per_person": 70, "distance": "0.2 km"},
    {"name": "Momo Junction", "category": "Food", "per_person": 110, "distance": "0.8 km"},
    {"name": "Brew & Books", "category": "Cafe", "per_person": 180, "distance": "1.1 km"},
    {"name": "Pixel Arena", "category": "Gaming", "per_person": 150, "distance": "1.5 km"},
    {"name": "Pizza Hub", "category": "Food", "per_person": 200, "distance": "1.2 km"},
    {"name": "Level Up Lounge", "category": "Gaming", "per_person": 250, "distance": "2.0 km"},
    {"name": "Open Mic Night", "category": "Events", "per_person": 100, "distance": "0.6 km"},
    {"name": "Rooftop Grill", "category": "Food", "per_person": 380, "distance": "2.4 km"},
    {"name": "Fest Entry: Rhythm", "category": "Events", "per_person": 300, "distance": "1.0 km"},
]

CATEGORY_RULES = [
    ("Food", re.compile(r"zomato|swiggy|momo|pizza|canteen|restaurant", re.I)),
    ("Cafe", re.compile(r"chai|cafe|coffee|brew", re.I)),
    ("Transport", re.compile(r"uber|ola|rapido|metro|irctc", re.I)),
    ("Subscriptions", re.compile(r"netflix|spotify|prime|hotstar", re.I)),
    ("Gaming", re.compile(r"arena|game|steam|pixel", re.I)),
]
INSIGHTS = {
    "Food": "Food delivery adds up. A group pool order could split the fees.",
    "Cafe": "Cafe runs are cheaper split from a pool.",
    "Transport": "Sharing rides? Pool the fare and refund the rest.",
    "Subscriptions": "Subscription spotted. Consider splitting it with friends.",
    "Gaming": "Gaming night logged. Check Discovery for group deals.",
}
AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
MERCHANT_RE = re.compile(
    r"(?:to|at)\s+([A-Za-z][A-Za-z0-9 &.-]*?)(?:\s+(?:on|via|using|ref|upi|a/c)\b|\.|$)", re.I
)
DEBIT_RE = re.compile(r"debited|paid|sent|spent", re.I)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


# ---------- database ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS waitlist (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                 college TEXT, role TEXT, interests TEXT,
                 code TEXT NOT NULL UNIQUE, referred_by TEXT,
                 created TEXT DEFAULT CURRENT_TIMESTAMP)"""
        )


def position_of(code):
    """Rank = signup order, minus 3 places per successful referral."""
    rows = db().execute("SELECT id, code FROM waitlist").fetchall()
    refs = {r["code"]: 0 for r in rows}
    for r in db().execute("SELECT referred_by FROM waitlist WHERE referred_by IS NOT NULL"):
        if r["referred_by"] in refs:
            refs[r["referred_by"]] += 1
    ranked = sorted(rows, key=lambda r: (r["id"] - 3 * refs[r["code"]], r["id"]))
    return next(i for i, r in enumerate(ranked, 1) if r["code"] == code), len(rows)


# ---------- helpers ----------
def to_paise(v):
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Amounts must be numbers.")
    if not d.is_finite() or d <= 0 or d > 10_000_000:
        raise ValueError("Amounts must be positive and reasonable.")
    return int((d * 100).to_integral_value(ROUND_HALF_UP))


def clean(s, n):
    return str(s or "").strip()[:n]


def err(msg, code=400):
    return jsonify(error=msg), code


# ---------- API ----------
@app.get("/api/places")
def places():
    try:
        budget = float(request.args.get("budget", 0))
        group = max(1, min(int(request.args.get("group", 1)), 50))
    except ValueError:
        return err("budget and group must be numbers.")
    cat = request.args.get("cat", "")
    fit = [
        {**p, "total": p["per_person"] * group}
        for p in PLACES
        if (not cat or p["category"] == cat) and p["per_person"] * group <= budget
    ]
    fit.sort(key=lambda p: -p["per_person"])
    pool = [p for p in PLACES if not cat or p["category"] == cat]
    return jsonify(places=fit, fit=len(fit), total=len(pool))


@app.post("/api/parse")
def parse_sms():
    text = clean((request.get_json(silent=True) or {}).get("text"), 500)
    m = AMOUNT_RE.search(text)
    if not m or not DEBIT_RE.search(text):
        return err("Could not read a UPI debit from that.")
    who = MERCHANT_RE.search(text)
    merchant = who.group(1).strip() if who else "Unknown"
    category = next((c for c, rx in CATEGORY_RULES if rx.search(merchant)), "Other")
    return jsonify(
        amount=float(m.group(1).replace(",", "")),
        merchant=merchant,
        category=category,
        insight=INSIGHTS.get(category, "Logged automatically."),
    )


@app.post("/api/settle")
def settle():
    body = request.get_json(silent=True) or {}
    members, bills = body.get("members"), body.get("bills") or []
    if not isinstance(members, list) or not members or len(members) > 50 or not isinstance(bills, list) or len(bills) > 200:
        return err("Send 1-50 members and up to 200 bills.")
    try:
        paid_in = [(clean(m.get("name"), 40) or "Member", to_paise(m.get("amount"))) for m in members]
        spent = sum(to_paise(b.get("amount")) for b in bills)
    except (ValueError, AttributeError) as e:
        return err(str(e) if isinstance(e, ValueError) else "Malformed request.")
    total = sum(p for _, p in paid_in)
    if spent > total:
        return err("Bills exceed what is in the pool.")
    left = total - spent
    # Largest-remainder method: refunds always sum to exactly what is left.
    shares = [(left * p // total, (left * p) % total) for _, p in paid_in]
    shortfall = left - sum(s for s, _ in shares)
    order = sorted(range(len(shares)), key=lambda i: -shares[i][1])
    refunds = [s for s, _ in shares]
    for i in order[:shortfall]:
        refunds[i] += 1
    return jsonify(
        refunds=[{"name": n, "refund": r / 100} for (n, _), r in zip(paid_in, refunds)],
        pool=total / 100, spent=spent / 100, returned=left / 100,
    )


@app.post("/api/waitlist")
def join():
    b = request.get_json(silent=True) or {}
    name, email = clean(b.get("name"), 80), clean(b.get("email"), 120).lower()
    if not name or not EMAIL_RE.match(email):
        return err("Add your name and a valid email.")
    existing = db().execute("SELECT code FROM waitlist WHERE email=?", (email,)).fetchone()
    if existing:
        pos, total = position_of(existing["code"])
        return jsonify(position=pos, total=total, code=existing["code"], existing=True)
    ref = clean(b.get("ref"), 20).upper()
    if ref and not db().execute("SELECT 1 FROM waitlist WHERE code=?", (ref,)).fetchone():
        ref = ""
    interests = ",".join(clean(i, 30) for i in (b.get("interests") or [])[:6] if isinstance(i, str))
    for _ in range(5):  # retry on the rare code collision
        code = "SCOUT-" + secrets.token_hex(2).upper()
        try:
            db().execute(
                "INSERT INTO waitlist(name,email,college,role,interests,code,referred_by) VALUES(?,?,?,?,?,?,?)",
                (name, email, clean(b.get("college"), 80), clean(b.get("role"), 40), interests, code, ref or None),
            )
            db().commit()
            break
        except sqlite3.IntegrityError:
            db().rollback()
    else:
        return err("Please try again.", 500)
    pos, total = position_of(code)
    return jsonify(position=pos, total=total, code=code, existing=False)


@app.get("/api/waitlist/count")
def count():
    return jsonify(total=db().execute("SELECT COUNT(*) FROM waitlist").fetchone()[0])


@app.get("/")
def index():
    return app.send_static_file("index.html")


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
