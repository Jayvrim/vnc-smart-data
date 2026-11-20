import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

import bcrypt
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
DB_PATH = os.environ.get("SMARTDATA_DB", str(BASE_DIR / "smart_data.db"))
DEMO_WALLET_SEED_KOBO = 10_000_000
INTERSWITCH_MODE = os.environ.get("INTERSWITCH_MODE", "TEST").strip().upper()
if INTERSWITCH_MODE not in {"TEST", "LIVE"}:
    raise RuntimeError("INTERSWITCH_MODE must be TEST or LIVE")

app = Flask(__name__)
CORS(app, supports_credentials=True)
app.config["SECRET_KEY"] = os.environ.get("SMARTDATA_SECRET") or secrets.token_hex(32)

PLANS = {
    "daily": {"name": "Daily access", "gb": 2, "days": 1, "price": 300},
    "test_500mb": {"name": "500 MB test plan", "gb": 0.5, "days": 1, "price": 10},
    "weekly": {"name": "Weekly access", "gb": 12, "days": 7, "price": 1500},
    "monthly": {"name": "Monthly access", "gb": 40, "days": 30, "price": 4500},
}


def utcnow():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def interswitch_requery_url(mode):
    configured_url = os.environ.get("INTERSWITCH_REQUERY_URL")
    if configured_url:
        return configured_url
    host = "sandbox.interswitchng.com" if mode == "TEST" else "webpay.interswitchng.com"
    return f"https://{host}/collections/api/v1/gettransaction.json"


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer',
            email_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS packages (
            key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            gb REAL NOT NULL,
            days INTEGER NOT NULL,
            price_kobo INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS wallet_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            gb_delta REAL NOT NULL,
            naira_delta INTEGER NOT NULL DEFAULT 0,
            reference TEXT,
            description TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            package_key TEXT,
            amount_kobo INTEGER NOT NULL,
            txn_ref TEXT NOT NULL UNIQUE,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            gateway_response TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS wallet_purchase_requests (
            user_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            package_key TEXT NOT NULL,
            txn_ref TEXT NOT NULL UNIQUE,
            amount_kobo INTEGER NOT NULL,
            gb_added REAL NOT NULL,
            cash_wallet_balance_kobo INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id,idempotency_key),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            package_key TEXT NOT NULL,
            status TEXT NOT NULL,
            auto_renew INTEGER NOT NULL DEFAULT 0,
            starts_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            bytes_used INTEGER NOT NULL,
            gb_used REAL NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS route_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            route_name TEXT NOT NULL,
            simulated INTEGER NOT NULL,
            decision TEXT NOT NULL,
            explanation TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_demo_wallet_seed_once
            ON wallet_ledger(user_id) WHERE type='demo_wallet_seed';
        """
    )
    for key, plan in PLANS.items():
        con.execute(
            "INSERT OR REPLACE INTO packages (key,name,gb,days,price_kobo,active) VALUES (?,?,?,?,?,1)",
            (key, plan["name"], plan["gb"], plan["days"], plan["price"] * 100),
        )
    con.commit()
    seed_email = os.environ.get("SMARTDATA_ADMIN_EMAIL", "").strip().lower()
    seed_password = os.environ.get("SMARTDATA_ADMIN_PASSWORD", "")
    if bool(seed_email) != bool(seed_password):
        con.close()
        raise RuntimeError("Set both SMARTDATA_ADMIN_EMAIL and SMARTDATA_ADMIN_PASSWORD to seed an administrator.")
    if seed_email:
        user = con.execute("SELECT id FROM users WHERE email=?", (seed_email,)).fetchone()
        if not user:
            con.execute(
                "INSERT INTO users (name,email,password_hash,role,email_verified,created_at) VALUES (?,?,?,?,1,?)",
                ("VNC Admin", seed_email, bcrypt.hashpw(seed_password.encode(), bcrypt.gensalt()).decode(), "admin", utcnow()),
            )
            con.commit()
    if os.environ.get("SMARTDATA_ALLOW_DEMO_WALLET", "0") == "1":
        for existing_user in con.execute("SELECT id FROM users").fetchall():
            seed_demo_wallet(con, existing_user["id"])
        con.commit()
    con.close()


def get_bearer_token():
    header = request.headers.get("Authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else None


def current_user(required_role=None):
    token = get_bearer_token()
    if not token:
        return None
    con = db()
    row = con.execute(
        """
        SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token=? AND s.expires_at>?
        """,
        (token, utcnow()),
    ).fetchone()
    con.close()
    if not row:
        return None
    if required_role and row["role"] != required_role:
        return None
    return dict(row)


def auth(required_role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user(required_role)
            if not user:
                return jsonify({"error": "Authentication required"}), 401
            request.current_user = user
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def wallet_balance(con, user_id):
    row = con.execute("SELECT COALESCE(SUM(gb_delta),0) AS gb FROM wallet_ledger WHERE user_id=?", (user_id,)).fetchone()
    return max(0.0, float(row["gb"]))


def cash_wallet_balance_kobo(con, user_id):
    row = con.execute(
        "SELECT COALESCE(SUM(naira_delta),0) AS balance FROM wallet_ledger WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return max(0, int(row["balance"]))


def seed_demo_wallet(con, user_id):
    if os.environ.get("SMARTDATA_ALLOW_DEMO_WALLET", "0") != "1":
        return
    con.execute(
        "INSERT OR IGNORE INTO wallet_ledger (user_id,type,gb_delta,naira_delta,description,created_at) VALUES (?,?,?,?,?,?)",
        (
            user_id,
            "demo_wallet_seed",
            0,
            DEMO_WALLET_SEED_KOBO,
            "DEMO starting wallet funds; not cash and not redeemable",
            utcnow(),
        ),
    )


def audit(actor, action, target=None, detail=None):
    con = db()
    con.execute(
        "INSERT INTO audit_log (actor,action,target,detail,created_at) VALUES (?,?,?,?,?)",
        (actor, action, target, detail, utcnow()),
    )
    con.commit()
    con.close()


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "payment_mode": INTERSWITCH_MODE})


@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if len(name) < 2 or "@" not in email or len(password) < 8:
        return jsonify({"error": "Enter a name, valid email and password of at least 8 characters"}), 400
    con = db()
    if con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        con.close()
        return jsonify({"error": "An account already exists for this email"}), 409
    cursor = con.execute(
        "INSERT INTO users (name,email,password_hash,role,email_verified,created_at) VALUES (?,?,?,?,0,?)",
        (name, email, bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(), "customer", utcnow()),
    )
    user_id = cursor.lastrowid
    seed_demo_wallet(con, user_id)
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat(timespec="seconds") + "Z"
    con.execute("INSERT INTO sessions (token,user_id,expires_at,created_at) VALUES (?,?,?,?)", (token, user_id, expires, utcnow()))
    con.commit()
    con.close()
    audit(email, "auth.register")
    return jsonify({"token": token, "user": {"id": user_id, "name": name, "email": email, "role": "customer"}})


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    con = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not bcrypt.checkpw(password.encode(), (user["password_hash"] or "").encode()):
        con.close()
        return jsonify({"error": "Invalid email or password"}), 401
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat(timespec="seconds") + "Z"
    con.execute("INSERT INTO sessions (token,user_id,expires_at,created_at) VALUES (?,?,?,?)", (token, user["id"], expires, utcnow()))
    con.commit()
    con.close()
    audit(email, "auth.login")
    return jsonify({"token": token, "user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}})


@app.route("/api/auth/logout", methods=["POST"])
@auth()
def logout():
    token = get_bearer_token()
    con = db()
    con.execute("DELETE FROM sessions WHERE token=?", (token,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.route("/api/auth/reset", methods=["POST"])
def reset():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    con = db()
    user = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if user:
        con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        con.commit()
    con.close()
    audit(email, "auth.password_reset_requested")
    return jsonify({"ok": True, "message": "If the account exists, a reset process can continue."})


@app.route("/api/plans")
def plans():
    con = db()
    rows = con.execute("SELECT * FROM packages WHERE active=1 ORDER BY days").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/dashboard")
@auth()
def dashboard():
    user = request.current_user
    con = db()
    balance = wallet_balance(con, user["id"])
    subscription = con.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    usage = con.execute(
        """
        SELECT COALESCE(SUM(gb_used),0) AS gb,
               COALESCE(SUM(CASE WHEN source='demo_simulation' THEN gb_used ELSE 0 END),0) AS demo_gb
        FROM usage_events WHERE user_id=?
        """,
        (user["id"],),
    ).fetchone()
    notifications = con.execute(
        "SELECT id,title,body,read,created_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (user["id"],),
    ).fetchall()
    payments = con.execute(
        """
        SELECT p.txn_ref,p.package_key,p.amount_kobo,p.mode,p.status,p.created_at,
               COALESCE(pk.name,'Wallet top-up') AS package_name
        FROM payments p LEFT JOIN packages pk ON pk.key=p.package_key
        WHERE p.user_id=? ORDER BY p.id DESC LIMIT 10
        """,
        (user["id"],),
    ).fetchall()
    cash_balance_kobo = cash_wallet_balance_kobo(con, user["id"])
    con.close()
    return jsonify({
        "balance_gb": round(balance, 3),
        "cash_wallet_balance_kobo": cash_balance_kobo,
        "usage_gb": round(float(usage["gb"]), 3),
        "demo_usage_gb": round(float(usage["demo_gb"]), 3),
        "subscription": dict(subscription) if subscription else None,
        "notifications": [dict(r) for r in notifications],
        "payments": [dict(r) for r in payments],
    })


@app.route("/api/payments/initiate", methods=["POST"])
@auth()
def initiate_payment():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    package_key = data.get("package")
    merchant_code = os.environ.get("INTERSWITCH_MERCHANT_CODE", "MX153376").strip()
    pay_item_id = os.environ.get("INTERSWITCH_PAY_ITEM_ID", "9381029").strip()
    if not merchant_code or not pay_item_id:
        return jsonify({"error": "Configure the Interswitch-issued merchant code and pay item ID on the server before checkout."}), 503
    con = db()
    package = con.execute("SELECT * FROM packages WHERE key=? AND active=1", (package_key,)).fetchone()
    if not package:
        con.close()
        return jsonify({"error": "Unknown package"}), 400
    txn_ref = f"SD-{secrets.token_hex(8).upper()}"
    con.execute(
        "INSERT INTO payments (user_id,package_key,amount_kobo,txn_ref,mode,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (user["id"], package_key, package["price_kobo"], txn_ref, INTERSWITCH_MODE, "pending", utcnow(), utcnow()),
    )
    con.commit()
    con.close()
    audit(user["email"], "payment.initiated", txn_ref, package_key)
    return jsonify({
        "txn_ref": txn_ref,
        "amount_kobo": package["price_kobo"],
        "merchant_code": merchant_code,
        "pay_item_id": pay_item_id,
        "mode": INTERSWITCH_MODE,
        "verification_required": True,
    })


@app.route("/api/wallet/topups/initiate", methods=["POST"])
@auth()
def initiate_wallet_topup():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    merchant_code = os.environ.get("INTERSWITCH_MERCHANT_CODE", "MX153376").strip()
    pay_item_id = os.environ.get("INTERSWITCH_PAY_ITEM_ID", "9381029").strip()
    if not merchant_code or not pay_item_id:
        return jsonify({"error": "Configure the Interswitch-issued merchant code and pay item ID on the server before checkout."}), 503
    try:
        amount_naira = Decimal(str(data.get("amount_naira", "")))
        amount_kobo_decimal = amount_naira * 100
        if not amount_naira.is_finite() or amount_kobo_decimal != amount_kobo_decimal.to_integral_value():
            raise InvalidOperation
        amount_kobo = int(amount_kobo_decimal)
    except (InvalidOperation, TypeError, ValueError):
        return jsonify({"error": "Enter a valid top-up amount in naira"}), 400
    if amount_kobo < 1000:
        return jsonify({"error": "The minimum wallet top-up is ₦10"}), 400

    txn_ref = f"SD-WALLET-{secrets.token_hex(8).upper()}"
    con = db()
    con.execute(
        "INSERT INTO payments (user_id,package_key,amount_kobo,txn_ref,mode,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (user["id"], None, amount_kobo, txn_ref, INTERSWITCH_MODE, "pending", utcnow(), utcnow()),
    )
    con.commit()
    con.close()
    audit(user["email"], "wallet.topup_initiated", txn_ref, str(amount_kobo))
    return jsonify({
        "txn_ref": txn_ref,
        "amount_kobo": amount_kobo,
        "merchant_code": merchant_code,
        "pay_item_id": pay_item_id,
        "mode": INTERSWITCH_MODE,
        "verification_required": True,
    })


@app.route("/api/wallet/purchase", methods=["POST"])
@auth()
def purchase_with_wallet():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    package_key = data.get("package")
    idempotency_key = data.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128:
        return jsonify({"error": "A valid idempotency key is required"}), 400
    idempotency_key = idempotency_key.strip()

    con = db()
    con.execute("BEGIN IMMEDIATE")
    previous_request = con.execute(
        "SELECT * FROM wallet_purchase_requests WHERE user_id=? AND idempotency_key=?",
        (user["id"], idempotency_key),
    ).fetchone()
    if previous_request:
        con.rollback()
        con.close()
        if previous_request["package_key"] != package_key:
            return jsonify({"error": "This purchase request was already used for a different package"}), 409
        return jsonify({
            "status": "approved",
            "txn_ref": previous_request["txn_ref"],
            "amount_kobo": previous_request["amount_kobo"],
            "gb_added": previous_request["gb_added"],
            "cash_wallet_balance_kobo": previous_request["cash_wallet_balance_kobo"],
        })

    package = con.execute(
        "SELECT * FROM packages WHERE key=? AND active=1",
        (package_key,),
    ).fetchone()
    if not package:
        con.rollback()
        con.close()
        return jsonify({"error": "Unknown package"}), 400

    balance_kobo = cash_wallet_balance_kobo(con, user["id"])
    if balance_kobo < package["price_kobo"]:
        con.rollback()
        con.close()
        return jsonify({"error": "Insufficient VNC wallet balance"}), 409

    txn_ref = f"SD-WALLET-PURCHASE-{secrets.token_hex(6).upper()}"
    now = utcnow()
    con.execute(
        "INSERT INTO payments (user_id,package_key,amount_kobo,txn_ref,mode,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (user["id"], package["key"], package["price_kobo"], txn_ref, "VNC_WALLET", "paid", now, now),
    )
    con.execute(
        "INSERT INTO wallet_ledger (user_id,type,gb_delta,naira_delta,reference,description,created_at) VALUES (?,?,?,?,?,?,?)",
        (user["id"], "wallet_data_purchase", 0, -package["price_kobo"], txn_ref, f"{package['name']} paid from VNC wallet", utcnow()),
    )
    con.execute(
        "INSERT INTO wallet_ledger (user_id,type,gb_delta,naira_delta,reference,description,created_at) VALUES (?,?,?,?,?,?,?)",
        (user["id"], "wallet_purchase_credit", package["gb"], 0, txn_ref, f"{package['name']} wallet purchase", utcnow()),
    )
    expires = (datetime.utcnow() + timedelta(days=package["days"])).isoformat(timespec="seconds") + "Z"
    con.execute(
        "INSERT INTO subscriptions (user_id,package_key,status,auto_renew,starts_at,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
        (user["id"], package["key"], "active", 0, now, expires, now),
    )
    con.execute(
        "INSERT INTO notifications (user_id,title,body,read,created_at) VALUES (?,?,?,?,?)",
        (user["id"], "Plan activated", f"{package['gb']} GB was purchased from your VNC wallet.", 0, now),
    )
    new_balance_kobo = cash_wallet_balance_kobo(con, user["id"])
    con.execute(
        "INSERT INTO wallet_purchase_requests (user_id,idempotency_key,package_key,txn_ref,amount_kobo,gb_added,cash_wallet_balance_kobo,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (user["id"], idempotency_key, package["key"], txn_ref, package["price_kobo"], package["gb"], new_balance_kobo, now),
    )
    con.commit()
    con.close()
    audit(user["email"], "wallet.data_purchase", txn_ref, package["key"])
    return jsonify({
        "status": "approved",
        "txn_ref": txn_ref,
        "amount_kobo": package["price_kobo"],
        "gb_added": package["gb"],
        "cash_wallet_balance_kobo": new_balance_kobo,
    })


@app.route("/api/payments/verify", methods=["POST"])
@auth()
def verify_payment():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    txn_ref = data.get("txn_ref")
    con = db()
    payment = con.execute("SELECT * FROM payments WHERE txn_ref=? AND user_id=?", (txn_ref, user["id"])).fetchone()
    if not payment:
        con.close()
        return jsonify({"error": "Payment not found"}), 404
    credit_type = "wallet_topup_credit" if payment["package_key"] is None else "purchase_credit"
    credited = con.execute(
        "SELECT id FROM wallet_ledger WHERE reference=? AND type=?",
        (txn_ref, credit_type),
    ).fetchone()
    if credited:
        con.close()
        return jsonify({"status": "approved", "message": "Already verified"})

    verify_url = interswitch_requery_url(payment["mode"])
    merchant_code = os.environ.get("INTERSWITCH_MERCHANT_CODE", "MX153376").strip()
    try:
        response = requests.get(
            verify_url,
            params={
                "merchantcode": merchant_code,
                "transactionreference": txn_ref,
                "amount": payment["amount_kobo"],
            },
            timeout=10,
        )
        response.raise_for_status()
        verified = response.json()
    except (requests.RequestException, ValueError) as exc:
        con.close()
        return jsonify({"error": "Verification service unavailable", "detail": str(exc)}), 502

    if not isinstance(verified, dict):
        con.execute("UPDATE payments SET status='pending_verification',updated_at=? WHERE id=?", (utcnow(), payment["id"]))
        con.commit()
        con.close()
        audit(user["email"], "payment.verification_pending", txn_ref, "Requery response was not a transaction object")
        return jsonify({
            "status": "pending_verification",
            "message": "Interswitch has not confirmed this transaction yet. No wallet credit was issued.",
        })

    response_code = str(verified.get("ResponseCode", ""))
    payment_cancelled = verified.get("PaymentCancelled")
    is_cancelled = payment_cancelled is True or (
        isinstance(payment_cancelled, str) and payment_cancelled.strip().lower() == "true"
    )
    if response_code != "00" or is_cancelled:
        con.execute("UPDATE payments SET status='failed',gateway_response=?,updated_at=? WHERE id=?", (str(verified), utcnow(), payment["id"]))
        con.commit()
        con.close()
        return jsonify({
            "status": "failed",
            "message": verified.get("ResponseDescription") or "Interswitch did not approve this transaction.",
        })

    if verified.get("MerchantReference") != txn_ref:
        con.execute("UPDATE payments SET status='pending_verification',gateway_response=?,updated_at=? WHERE id=?", (str(verified), utcnow(), payment["id"]))
        con.commit()
        con.close()
        audit(user["email"], "payment.verification_pending", txn_ref, "Requery merchant reference did not match")
        return jsonify({
            "status": "pending_verification",
            "message": "Interswitch has not confirmed this transaction reference yet. No wallet credit was issued.",
        })

    verified_amount = verified.get("Amount")
    try:
        if isinstance(verified_amount, bool):
            raise ValueError("Invalid verified amount")
        verified_amount_decimal = Decimal(str(verified_amount))
        if not verified_amount_decimal.is_finite() or verified_amount_decimal != verified_amount_decimal.to_integral_value():
            raise ValueError("Verified amount must be an integer number of kobo")
        verified_amount = int(verified_amount_decimal)
    except (InvalidOperation, TypeError, ValueError):
        con.execute("UPDATE payments SET status='pending_verification',gateway_response=?,updated_at=? WHERE id=?", (str(verified), utcnow(), payment["id"]))
        con.commit()
        con.close()
        audit(user["email"], "payment.verification_pending", txn_ref, "Verified amount_kobo is missing or invalid")
        return jsonify({
            "status": "pending_verification",
            "message": "The verification response did not confirm the paid amount. No data was added.",
        })

    if verified_amount != payment["amount_kobo"]:
        con.execute("UPDATE payments SET status='failed',gateway_response=?,updated_at=? WHERE id=?", (str(verified), utcnow(), payment["id"]))
        con.commit()
        con.close()
        audit(user["email"], "payment.amount_mismatch", txn_ref, "Verified amount did not match expected amount")
        return jsonify({
            "status": "failed",
            "message": "The verified amount does not match the purchase amount. No data was added.",
        })

    package = None
    if payment["package_key"] is not None:
        package = con.execute("SELECT * FROM packages WHERE key=?", (payment["package_key"],)).fetchone()
    con.execute("BEGIN IMMEDIATE")
    if con.execute(
        "SELECT id FROM wallet_ledger WHERE reference=? AND type=?",
        (txn_ref, credit_type),
    ).fetchone():
        con.commit()
        con.close()
        return jsonify({"status": "approved", "message": "Already verified"})
    con.execute("UPDATE payments SET status='approved',gateway_response=?,updated_at=? WHERE id=?", (str(verified), utcnow(), payment["id"]))
    if payment["package_key"] is None:
        con.execute(
            "INSERT INTO wallet_ledger (user_id,type,gb_delta,naira_delta,reference,description,created_at) VALUES (?,?,?,?,?,?,?)",
            (user["id"], "wallet_topup_credit", 0, payment["amount_kobo"], txn_ref, "Verified Interswitch wallet top-up", utcnow()),
        )
        con.execute(
            "INSERT INTO notifications (user_id,title,body,read,created_at) VALUES (?,?,?,?,?)",
            (user["id"], "Wallet topped up", f"₦{payment['amount_kobo'] / 100:,.2f} was added to your VNC wallet.", 0, utcnow()),
        )
        new_balance_kobo = cash_wallet_balance_kobo(con, user["id"])
        con.commit()
        con.close()
        audit(user["email"], "wallet.topup_approved", txn_ref, str(payment["amount_kobo"]))
        return jsonify({
            "status": "approved",
            "wallet_credit_kobo": payment["amount_kobo"],
            "cash_wallet_balance_kobo": new_balance_kobo,
        })

    con.execute(
        "INSERT INTO wallet_ledger (user_id,type,gb_delta,naira_delta,reference,description,created_at) VALUES (?,?,?,?,?,?,?)",
        (user["id"], "purchase_credit", package["gb"], 0, txn_ref, f"{package['name']} purchase", utcnow()),
    )
    con.execute(
        "INSERT INTO subscriptions (user_id,package_key,status,auto_renew,starts_at,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
        (user["id"], package["key"], "active", 0, utcnow(), (datetime.utcnow() + timedelta(days=package["days"])).isoformat(timespec="seconds") + "Z", utcnow()),
    )
    con.execute(
        "INSERT INTO notifications (user_id,title,body,read,created_at) VALUES (?,?,?,?,?)",
        (user["id"], "Plan activated", f"{package['gb']} GB was added to your VNC wallet.", 0, utcnow()),
    )
    con.commit()
    con.close()
    audit(user["email"], "payment.approved", txn_ref, package["key"])
    return jsonify({"status": "approved", "gb_added": package["gb"]})


@app.route("/api/payments/demo-credit", methods=["POST"])
@auth()
def demo_credit():
    if os.environ.get("SMARTDATA_ALLOW_DEMO_CREDIT", "0") != "1":
        return jsonify({"error": "Demo credits are disabled on this server"}), 403
    user = request.current_user
    data = request.get_json(silent=True) or {}
    package_key = data.get("package")
    con = db()
    package = con.execute("SELECT * FROM packages WHERE key=? AND active=1", (package_key,)).fetchone()
    if not package:
        con.close()
        return jsonify({"error": "Unknown package"}), 400
    txn_ref = f"SD-DEMO-{secrets.token_hex(5).upper()}"
    con.execute("BEGIN")
    con.execute(
        "INSERT INTO payments (user_id,package_key,amount_kobo,txn_ref,mode,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (user["id"], package_key, package["price_kobo"], txn_ref, "SIMULATED_DEMO", "demo_approved", utcnow(), utcnow()),
    )
    con.execute(
        "INSERT INTO wallet_ledger (user_id,type,gb_delta,naira_delta,reference,description,created_at) VALUES (?,?,?,?,?,?,?)",
        (user["id"], "demo_credit", package["gb"], 0, txn_ref, f"SIMULATED {package['name']} credit", utcnow()),
    )
    con.execute(
        "INSERT INTO subscriptions (user_id,package_key,status,auto_renew,starts_at,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
        (user["id"], package_key, "active", 0, utcnow(), (datetime.utcnow() + timedelta(days=package["days"])).isoformat(timespec="seconds") + "Z", utcnow()),
    )
    con.execute(
        "INSERT INTO notifications (user_id,title,body,read,created_at) VALUES (?,?,?,?,?)",
        (user["id"], "Demo package added", f"{package['gb']} GB demo credit was added. No payment was taken.", 0, utcnow()),
    )
    con.commit()
    con.close()
    audit(user["email"], "payment.demo_approved", txn_ref, "No real payment occurred")
    return jsonify({"status": "demo_approved", "gb_added": package["gb"], "txn_ref": txn_ref})


@app.route("/api/usage/report", methods=["POST"])
@auth()
def report_usage():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    try:
        gb_used = float(data.get("gb_used", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid usage"}), 400
    source = data.get("source")
    if gb_used <= 0 or gb_used > 1:
        return jsonify({"error": "Usage report exceeds allowed incremental limit"}), 400
    if source != "demo_simulation":
        return jsonify({"error": "Only simulated usage is available; device-level metering is not integrated"}), 400
    if os.environ.get("SMARTDATA_ALLOW_DEMO_USAGE", "0") != "1":
        return jsonify({"error": "Simulated usage is disabled on this server"}), 403
    con = db()
    con.execute("BEGIN IMMEDIATE")
    balance = wallet_balance(con, user["id"])
    if gb_used > balance:
        con.rollback()
        con.close()
        return jsonify({"error": "Insufficient available balance for demo usage"}), 409
    con.execute("INSERT INTO usage_events (user_id,source,bytes_used,gb_used,recorded_at) VALUES (?,?,?,?,?)", (user["id"], source, int(gb_used * 1073741824), gb_used, utcnow()))
    con.execute(
        "INSERT INTO wallet_ledger (user_id,type,gb_delta,naira_delta,reference,description,created_at) VALUES (?,?,?,?,?,?,?)",
        (user["id"], "usage_deduction", -gb_used, 0, None, f"Usage report from {source}", utcnow()),
    )
    con.commit()
    con.close()
    return jsonify({"ok": True, "used_gb": gb_used, "available_balance_gb": round(balance - gb_used, 3)})


@app.route("/api/routes")
@auth()
def routes():
    user = request.current_user
    con = db()
    rows = con.execute("SELECT * FROM route_events WHERE user_id=? ORDER BY id DESC LIMIT 20", (user["id"],)).fetchall()
    con.close()
    candidates = [
        {"name": "Smart Route A", "simulated": True},
        {"name": "Smart Route B", "simulated": True},
        {"name": "Smart Route C", "simulated": True},
    ]
    return jsonify({"candidates": candidates, "history": [dict(r) for r in rows], "disclaimer": "Simulated routes. No physical network was changed."})


@app.route("/api/routes/select", methods=["POST"])
@auth()
def select_route():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    candidates = {"A": "Smart Route A", "B": "Smart Route B", "C": "Smart Route C"}
    key = data.get("route", "B")
    if key not in candidates:
        return jsonify({"error": "Unknown simulated route"}), 400
    name = candidates[key]
    explanation = f"Deterministic prototype score selected {name}; it is a clearly labelled simulated route. No authorized telecom handover occurred."
    con = db()
    con.execute(
        "INSERT INTO route_events (user_id,route_name,simulated,decision,explanation,recorded_at) VALUES (?,?,?,?,?,?)",
        (user["id"], name, 1, "selected_simulated_route", explanation, utcnow()),
    )
    con.commit()
    con.close()
    return jsonify({"route": name, "simulated": True, "explanation": explanation})


@app.route("/api/admin/overview")
@auth("admin")
def admin_overview():
    con = db()
    users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    payments = con.execute("SELECT COUNT(*) n FROM payments").fetchall()[0]["n"]
    pending = con.execute("SELECT COUNT(*) n FROM payments WHERE status='pending_verification'").fetchone()["n"]
    balance = con.execute("SELECT COALESCE(SUM(gb_delta),0) n FROM wallet_ledger").fetchone()["n"]
    recent = con.execute(
        "SELECT p.id,p.txn_ref,p.amount_kobo,p.status,p.mode,p.created_at,u.email,u.name FROM payments p JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 20"
    ).fetchall()
    con.close()
    return jsonify({"users": users, "payments": payments, "pending_verifications": pending, "network_wallet_gb": round(float(balance), 3), "recent_payments": [dict(r) for r in recent]})


@app.route("/api/profile", methods=["GET", "PUT"])
@auth()
def profile():
    user = request.current_user
    if request.method == "GET":
        return jsonify({"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]})
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if len(name) < 2:
        return jsonify({"error": "Name is too short"}), 400
    con = db()
    con.execute("UPDATE users SET name=? WHERE id=?", (name, user["id"]))
    con.commit()
    con.close()
    return jsonify({"ok": True, "name": name})


@app.route("/checkout.html")
def app_interswitch_checkout():
    return send_from_directory(str(FRONTEND_DIR), "checkout.html")


@app.route("/")
@app.route("/<path:path>")
def frontend(path="index.html"):
    candidate = FRONTEND_DIR / path
    if path and candidate.is_file():
        return send_from_directory(FRONTEND_DIR, path)
    return send_from_directory(FRONTEND_DIR, "index.html")


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=os.environ.get("SMARTDATA_DEBUG") == "1")
