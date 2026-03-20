from flask import Flask, request, jsonify, session, render_template, make_response, redirect, url_for, current_app, flash, send_file
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import pymysql
import bcrypt
from pymysql.cursors import DictCursor
from jinja2 import TemplateNotFound
import os
import io
import secrets
import json
import uuid
import hmac
import hashlib
import requests
import random
import re
import base64
from functools import wraps
import sys
import logging
from config import Config
from email_sender import send_otp_email
from subscription_routes import subscription_bp

print("Python executable:", sys.executable)
print("Current working directory:", os.getcwd())
print("Templates folder exists:", os.path.exists('templates'))
print("login.html exists:", os.path.exists(os.path.join('templates', 'login.html')))


app = Flask(__name__, static_folder='static', template_folder='templates')
app.config.from_object(Config)
app.register_blueprint(subscription_bp)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/test')
def test():
    return "Flask is working!"

ALLOWED_PORTFOLIO_EXTENSIONS = {'jpg', 'jpeg', 'png', 'mp4'}
ALLOWED_PROFILE_PICTURE_EXTENSIONS = {'jpg', 'jpeg', 'png'}

SUBSCRIPTION_PLANS = {

    "basic": {
        "plan_id": 1,
        "plan_name": "Basic",
        "plan_type": "basic",
        "amount": 199,
        "duration_days": 30,
        "duration_label": "1 Month",
        "features": [
            "Unlimited Bookings",
            "1 Month Validity",
            "Standard Listing"
        ]
    },

    "premium": {
        "plan_id": 2,
        "plan_name": "Premium",
        "plan_type": "premium",
        "amount": 399,
        "duration_days": 90,
        "duration_label": "3 Months",
        "features": [
            "Unlimited Bookings",
            "3 Months Validity",
            "Priority Listing"
        ]
    },

    "pro": {
        "plan_id": 3,
        "plan_name": "Pro",
        "plan_type": "pro",
        "amount": 599,
        "duration_days": 180,
        "duration_label": "6 Months",
        "features": [
            "Unlimited Bookings",
            "6 Months Validity",
            "Featured Artist Listing"
        ]
    }
}


def parse_portfolio_paths(raw_value):
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            if ',' in raw_value:
                return [x.strip() for x in raw_value.split(',') if x.strip()]
            return [raw_value]
    return []


def is_allowed_portfolio_file(filename):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_PORTFOLIO_EXTENSIONS


def is_allowed_profile_picture_file(filename):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_PROFILE_PICTURE_EXTENSIONS


def get_profile_picture_column(cur):
    cur.execute("SHOW COLUMNS FROM artist_table")
    columns = cur.fetchall() or []
    fields = {str(col.get('Field', '')).lower(): col.get('Field') for col in columns}
    for candidate in [
        'profile_picture_path',
        'profile_pic_path',
        'profile_picture',
        'profile_pic',
        'avatar_path',
        'photo_path'
    ]:
        if candidate in fields:
            return fields[candidate]
    return None


def ensure_artist_schema(cur):
    cur.execute("SHOW COLUMNS FROM artist_table")
    existing = {str(col.get('Field', '')).lower() for col in (cur.fetchall() or [])}
    if 'profile_pic' not in existing:
        cur.execute("ALTER TABLE artist_table ADD COLUMN profile_pic VARCHAR(255) NULL")
    if 'portfolio_files' not in existing:
        cur.execute("ALTER TABLE artist_table ADD COLUMN portfolio_files VARCHAR(1000) NULL")
    if 'working_start_time' not in existing:
        cur.execute("ALTER TABLE artist_table ADD COLUMN working_start_time VARCHAR(5) NULL")
    if 'working_end_time' not in existing:
        cur.execute("ALTER TABLE artist_table ADD COLUMN working_end_time VARCHAR(5) NULL")


CATEGORY_NAME_TO_ID = {
    'singer': 1,
    'dancer': 2,
    'photographer': 3
}


def resolve_category_id(cur, category_value):
    if category_value is None:
        return None
    raw_value = str(category_value).strip()
    if not raw_value:
        return None

    if raw_value.isdigit():
        return int(raw_value)

    cur.execute(
        """
        SELECT category_id
        FROM category_table
        WHERE LOWER(category_name) = LOWER(%s)
        LIMIT 1
        """,
        (raw_value,)
    )
    row = cur.fetchone()
    if row and row.get('category_id') is not None:
        return int(row['category_id'])

    return CATEGORY_NAME_TO_ID.get(raw_value.lower())


def ensure_calendar_schema(cur):
    cur.execute("SHOW COLUMNS FROM calendar_table")
    cols = cur.fetchall() or []
    field_map = {}
    type_map = {}
    for col in cols:
        actual = str(col.get('Field', ''))
        lower = actual.lower()
        field_map[lower] = actual
        type_map[lower] = str(col.get('Type', '')).lower()
    if 'slot_type' not in field_map:
        cur.execute("ALTER TABLE calendar_table ADD COLUMN slot_type ENUM('Communication','Performance') DEFAULT 'Performance'")
    elif 'enum' not in type_map.get('slot_type', ''):
        actual_name = field_map['slot_type']
        cur.execute(f"ALTER TABLE calendar_table MODIFY COLUMN `{actual_name}` ENUM('Communication','Performance') DEFAULT 'Performance'")
    else:
        # Ensure the ENUM has the correct values
        current_enum = type_map.get('slot_type', '')
        if 'performance' not in current_enum:
            actual_name = field_map['slot_type']
            cur.execute(f"ALTER TABLE calendar_table MODIFY COLUMN `{actual_name}` ENUM('Communication','Performance') DEFAULT 'Performance'")
    if 'price' not in field_map:
        cur.execute("ALTER TABLE calendar_table ADD COLUMN price DECIMAL(10,2) DEFAULT 0")


def ensure_subscription_schema(cur):
    # Intentionally no schema changes here.
    return


def get_table_columns(cur, table_name):
    cur.execute(f"SHOW COLUMNS FROM {table_name}")
    return [row.get('Field') for row in (cur.fetchall() or []) if row.get('Field')]


def pick_column(columns, candidates):
    lookup = {str(col).lower(): col for col in (columns or [])}
    for name in candidates:
        key = str(name).lower()
        if key in lookup:
            return lookup[key]
    return None


def _to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, 'isoformat'):
        try:
            return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
        except Exception:
            return None
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d', '%d/%m/%Y'):
            try:
                return datetime.strptime(value[:10], fmt).date()
            except Exception:
                continue
    return None


def _time_to_hhmm(value, fallback='00:00'):
    if hasattr(value, 'total_seconds'):
        total = int(value.total_seconds())
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"
    raw = str(value or '').strip()
    if not raw:
        return fallback
    for fmt in ('%H:%M:%S', '%H:%M', '%I:%M %p'):
        try:
            return datetime.strptime(raw, fmt).strftime('%H:%M')
        except Exception:
            continue
    return raw[:5] if len(raw) >= 5 else fallback


def _fmt_date_ddmmyyyy(value):
    d = _to_date(value)
    return d.strftime('%d/%m/%Y') if d else ''


def _fmt_ampm(hhmm):
    try:
        return datetime.strptime(hhmm, '%H:%M').strftime('%I:%M %p')
    except Exception:
        return str(hhmm or '')


def _booking_start_end_dt(slot_date, start_time, end_time):
    d = _to_date(slot_date)
    if not d:
        return (None, None)
    st = _time_to_hhmm(start_time, '00:00')
    et = _time_to_hhmm(end_time, '00:00')
    try:
        start_dt = datetime.strptime(f"{d.isoformat()} {st}", '%Y-%m-%d %H:%M')
        end_dt = datetime.strptime(f"{d.isoformat()} {et}", '%Y-%m-%d %H:%M')
        return (start_dt, end_dt)
    except Exception:
        return (None, None)


def cleanup_expired_pending_bookings(cur, artist_id=None):
    """
    Pending bookings must remain pending/available.
    Intentionally no-op to avoid any auto status change.
    """
    return


def add_artist_notification(cur, artist_id, title, message, booking_id=None):
    notif_cols = get_table_columns(cur, 'notification_table')
    n_artist_col = pick_column(notif_cols, ['artist_id'])
    n_title_col = pick_column(notif_cols, ['title'])
    n_message_col = pick_column(notif_cols, ['message'])
    n_is_read_col = pick_column(notif_cols, ['is_read'])
    n_rec_type_col = pick_column(notif_cols, ['recipient_type'])
    n_booking_col = pick_column(notif_cols, ['booking_id'])
    if not n_artist_col or not n_message_col:
        return

    cols = [f"`{n_artist_col}`", f"`{n_message_col}`"]
    vals = [artist_id, str(message or '').strip()]
    ph = ['%s', '%s']

    if n_title_col:
        cols.append(f"`{n_title_col}`")
        vals.append(str(title or 'Notification').strip())
        ph.append('%s')
    if n_rec_type_col:
        cols.append(f"`{n_rec_type_col}`")
        vals.append('artist')
        ph.append('%s')
    if n_is_read_col:
        cols.append(f"`{n_is_read_col}`")
        vals.append(0)
        ph.append('%s')
    if n_booking_col and booking_id is not None:
        cols.append(f"`{n_booking_col}`")
        vals.append(booking_id)
        ph.append('%s')

    cur.execute(
        f"INSERT INTO notification_table ({', '.join(cols)}) VALUES ({', '.join(ph)})",
        tuple(vals)
    )


def fetch_artist_notification_count():
    artist_id = session.get('artist_id')
    if not artist_id:
        return 0
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) as count
            FROM notification_table
            WHERE artist_id=%s AND is_read=0
            """,
            (artist_id,)
        )
        result = cur.fetchone()
        if result:
            return result['count']
        return 0
    except Exception as e:
        print("Notification count error:", e)
        return 0
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def get_plan_definition(plan_type):
    return SUBSCRIPTION_PLANS.get(str(plan_type or '').lower())


def seed_subscription_plans(cur):
    cur.execute("SELECT COUNT(*) AS total FROM subscription_plan_table")
    row = cur.fetchone() or {}
    if int(row.get('total') or 0) > 0:
        return
    plan_rows = [
        (1, 'Basic',   199, 30,  0, 0),  # 1 Month
        (2, 'Premium', 399, 90,  1, 0),  # 3 Months
        (3, 'Pro',     599, 180, 1, 1)   # 6 Months
    ]
    cur.executemany(
        """
        INSERT INTO subscription_plan_table
            (plan_id, plan_name, amount, duration_days, has_priority, has_featured)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        plan_rows
    )


def get_plan_by_id(cur, plan_id):
    cur.execute(
        """
        SELECT plan_id, plan_name, amount, duration_days, has_priority, has_featured
        FROM subscription_plan_table
        WHERE plan_id = %s
        LIMIT 1
        """,
        (plan_id,)
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        'plan_id': int(row.get('plan_id')),
        'plan_name': row.get('plan_name') or '',
        'plan_type': str(row.get('plan_name') or '').strip().lower(),
        'amount': float(row.get('amount') or 0),
        'duration_days': int(row.get('duration_days') or 0),
        'has_priority': bool(row.get('has_priority')),
        'has_featured': bool(row.get('has_featured'))
    }


def resolve_plan(cur, payload):
    raw_plan_id = payload.get('plan_id')
    if raw_plan_id is not None and str(raw_plan_id).strip().isdigit():
        return get_plan_by_id(cur, int(raw_plan_id))

    raw_plan_type = str(payload.get('plan_type') or '').strip().lower()
    if raw_plan_type:
        cur.execute(
            """
            SELECT plan_id, plan_name, amount, duration_days, has_priority, has_featured
            FROM subscription_plan_table
            WHERE LOWER(plan_name) = %s
            LIMIT 1
            """,
            (raw_plan_type,)
        )
        row = cur.fetchone()
        if row:
            return {
                'plan_id': int(row.get('plan_id')),
                'plan_name': row.get('plan_name') or '',
                'plan_type': str(row.get('plan_name') or '').strip().lower(),
                'amount': float(row.get('amount') or 0),
                'duration_days': int(row.get('duration_days') or 0),
                'has_priority': bool(row.get('has_priority')),
                'has_featured': bool(row.get('has_featured'))
            }
    return None


def expire_outdated_subscriptions(cur, artist_id):
    cur.execute(
        """
        UPDATE subscription_table
        SET status = 'inactive'
        WHERE artist_id = %s
          AND end_date < CURDATE()
          AND LOWER(status) = 'active'
        """,
        (artist_id,)
    )


def create_free_trial_if_missing(cur, artist_id):
    cur.execute(
        "SELECT subscription_id FROM subscription_table WHERE artist_id = %s ORDER BY subscription_id DESC LIMIT 1",
        (artist_id,)
    )
    existing = cur.fetchone()
    if existing:
        return

    start_date = datetime.now().date()
    end_date = start_date + timedelta(days=30)
    cur.execute("SELECT plan_id FROM subscription_plan_table WHERE LOWER(plan_name) = 'basic' LIMIT 1")
    plan_row = cur.fetchone() or {}
    basic_plan_id = int(plan_row.get('plan_id') or 1)
    cur.execute(
        """
        INSERT INTO subscription_table
        (artist_id, plan_id, start_date, end_date, status)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (artist_id, basic_plan_id, start_date, end_date, 'active')
    )

def get_current_subscription(cur, artist_id):
    cur.execute(
        """
        SELECT
            s.subscription_id,
            s.artist_id,
            s.plan_id,
            p.plan_name,
            p.duration_days,
            p.amount,
            s.start_date,
            s.end_date,
            s.status
        FROM subscription_table s
        LEFT JOIN subscription_plan_table p ON p.plan_id = s.plan_id
        WHERE s.artist_id = %s
        ORDER BY s.subscription_id DESC
        LIMIT 1
        """,
        (artist_id,)
    )
    sub = cur.fetchone()
    if not sub:
        return None
    
    start_date = sub.get('start_date')
    end_date = sub.get('end_date')
    plan_type = str(sub.get('plan_name') or '').strip().lower()
    amount = float(sub.get('amount') or 0)
    trial_expired = bool(end_date and end_date < datetime.now().date())
    normalized_status = str(sub.get('status') or '').lower()
    if normalized_status not in ('active', 'inactive'):
        normalized_status = 'inactive' if trial_expired else 'active'
    if trial_expired:
        normalized_status = 'inactive'

    billing_cycle = 'yearly' if start_date and end_date and (end_date - start_date).days >= 365 else 'monthly'

    display_status = 'expired' if trial_expired else normalized_status
    return {
        'subscription_id': sub.get('subscription_id'),
        'plan_id': sub.get('plan_id'),
        'plan_name': sub.get('plan_name') or '',
        'plan_type': plan_type or 'basic',
        'billing_cycle': billing_cycle,
        'next_billing_date': end_date.isoformat() if (end_date and end_date >= datetime.now().date()) else None,
        'amount': amount,
        'status': display_status,
        'payment_status': 'success' if amount > 0 else 'trial',
        'start_date': start_date.isoformat() if start_date else None,
        'end_date': end_date.isoformat() if end_date else None,
        'trial_expired': trial_expired,
        'requires_paid_plan': trial_expired
    }
    # ... rest of function remains the same

'''def get_current_subscription(cur, artist_id):
    cur.execute(
        """
        SELECT
            s.subscription_id,
            s.artist_id,
            s.plan_id,
            p.plan_name,
            p.duration_days,
            s.amount,
            s.start_date,
            s.end_date,
            s.status
        FROM subscription_table s
        LEFT JOIN subscription_plan_table p ON p.plan_id = s.plan_id
        WHERE artist_id = %s
        ORDER BY s.subscription_id DESC
        LIMIT 1
        """,
        (artist_id,)
    )'''
    


def has_active_subscription(cur, artist_id):
    expire_outdated_subscriptions(cur, artist_id)
    cur.execute(
        """
        SELECT subscription_id
        FROM subscription_table
        WHERE artist_id = %s
          AND LOWER(status) = 'active'
          AND end_date >= CURDATE()
        ORDER BY subscription_id DESC
        LIMIT 1
        """,
        (artist_id,)
    )
    return bool(cur.fetchone())


def get_billing_history(cur, artist_id):
    cur.execute(
        """
        SELECT
            s.start_date,
            s.end_date,
            s.status,
            p.plan_name,
            p.amount
        FROM subscription_table s
        LEFT JOIN subscription_plan_table p ON p.plan_id = s.plan_id
        WHERE s.artist_id = %s
        ORDER BY s.subscription_id DESC
        """,
        (artist_id,)
    )
    rows = cur.fetchall() or []
    history = []
    for row in rows:
        history.append({
            'date': row.get('start_date').isoformat() if row.get('start_date') else None,
            'description': f"{str(row.get('plan_name') or '').strip()} subscription",
            'amount': float(row.get('amount') or 0),
            'payment_method': 'Razorpay' if float(row.get('amount') or 0) > 0 else 'Free Trial',
            'status': str(row.get('status') or '').lower() or 'active',
            'end_date': row.get('end_date').isoformat() if row.get('end_date') else None
        })
    return history


def activate_paid_subscription(cur, artist_id, plan, payment_id, order_id):
    start_date = datetime.now().date()
    end_date = start_date + timedelta(days=int(plan['duration_days']))
    cur.execute("UPDATE subscription_table SET status = 'inactive' WHERE artist_id = %s AND LOWER(status) = 'active'", (artist_id,))
    cur.execute(
        """
        INSERT INTO subscription_table
        (artist_id, plan_id, start_date, end_date, status)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            artist_id,
            int(plan['plan_id']),
            start_date,
            end_date,
            'active'
        )
    )
    # Record payment (if payment_table has subscription-friendly columns).
    try:
        pay_cols = get_table_columns(cur, 'payment_table')
        p_sub_col = pick_column(pay_cols, ['subscription_id'])
        p_booking_col = pick_column(pay_cols, ['booking_id'])
        p_amount_col = pick_column(pay_cols, ['amount'])
        p_status_col = pick_column(pay_cols, ['payment_status', 'status'])
        p_method_col = pick_column(pay_cols, ['payment_method'])
        p_txn_col = pick_column(pay_cols, ['transaction_id', 'gateway_payment_id'])
        p_order_col = pick_column(pay_cols, ['order_id', 'gateway_order_id'])

        if p_amount_col and (p_sub_col or p_booking_col):
            cur.execute("SELECT subscription_id FROM subscription_table WHERE artist_id=%s ORDER BY subscription_id DESC LIMIT 1", (artist_id,))
            sub_row = cur.fetchone() or {}
            subscription_id = sub_row.get('subscription_id')
            cols = []
            vals = []
            ph = []
            if p_sub_col:
                cols.append(f"`{p_sub_col}`")
                vals.append(subscription_id)
                ph.append('%s')
            elif p_booking_col:
                cols.append(f"`{p_booking_col}`")
                vals.append(None)
                ph.append('%s')
            cols.append(f"`{p_amount_col}`")
            vals.append(float(plan.get('amount') or 0))
            ph.append('%s')
            if p_status_col:
                cols.append(f"`{p_status_col}`")
                vals.append('success')
                ph.append('%s')
            if p_method_col:
                cols.append(f"`{p_method_col}`")
                vals.append('Razorpay')
                ph.append('%s')
            if p_txn_col:
                cols.append(f"`{p_txn_col}`")
                vals.append(payment_id)
                ph.append('%s')
            if p_order_col:
                cols.append(f"`{p_order_col}`")
                vals.append(order_id)
                ph.append('%s')
            cur.execute(
                f"INSERT INTO payment_table ({', '.join(cols)}) VALUES ({', '.join(ph)})",
                tuple(vals)
            )
    except Exception:
        pass

    add_artist_notification(
        cur,
        artist_id,
        "Subscription Activated",
        f"Your {plan.get('plan_name', 'subscription')} plan is active from {_fmt_date_ddmmyyyy(start_date)}."
    )

# ========== DATABASE ==========
def get_db() -> pymysql.connections.Connection:
    return pymysql.connect(
        host='localhost',
        user='root',
        password='root123',
        database='creovibe_db',
        cursorclass=DictCursor,
        charset='utf8mb4',
        autocommit=False
    )


# ========== MIDDLEWARE ==========
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'artist_id' not in session:
            return jsonify({'error': 'Login required'}), 401
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_notification_count():
    return {'notification_count': fetch_artist_notification_count()}

# ========== STATIC PAGES ==========
# ========== STATIC PAGES ==========

# ========== STATIC PAGES ==========
@app.route('/')
def index():
    return render_template('login.html')

@app.route('/subscription.html')
@login_required
def subscription_page():
    conn = None
    cur = None
    try:
        artist_id = session['artist_id']
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM subscription_table
            WHERE artist_id = %s
            ORDER BY subscription_id DESC
            """,
            (artist_id,)
        )
        subscriptions = cur.fetchall() or []
    except Exception:
        subscriptions = []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return render_template(
        'subscription.html',
        subscriptions=subscriptions,
        razorpay_key_id=current_app.config.get("RAZORPAY_KEY_ID", "")
    )

@app.route('/subscription/receipt/<int:subscription_id>')
@login_required
def download_receipt(subscription_id):
    conn = None
    cur = None
    try:
        artist_id = session['artist_id']
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                s.subscription_id,
                s.start_date,
                s.end_date,
                s.status,
                p.plan_name,
                p.amount AS price,
                p.duration_days,
                a.first_name,
                a.last_name
            FROM subscription_table s
            JOIN subscription_plan_table p ON s.plan_id = p.plan_id
            JOIN artist_table a ON s.artist_id = a.artist_id
            WHERE s.subscription_id = %s AND s.artist_id = %s
            LIMIT 1
            """,
            (subscription_id, artist_id)
        )
        row = cur.fetchone()

        if not row:
            return jsonify({'error': 'Subscription not found'}), 404

        artist_name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or 'N/A'
        plan_name = row.get('plan_name') or 'N/A'
        price = float(row.get('price') or 0)
        start_date = row.get('start_date')
        end_date = row.get('end_date')
        status = str(row.get('status') or '').strip().lower()
        sub_id = row.get('subscription_id')
        payment_date_fmt = start_date.strftime('%d/%m/%Y') if start_date else 'N/A'
        payment_time_fmt = start_date.strftime('%d %b %Y, %I:%M %p') if start_date else 'N/A'
        expiry_date_fmt = end_date.strftime('%d/%m/%Y') if end_date else 'N/A'
        receipt_no = f'RCP-{sub_id}'

        # ── reportlab imports ──
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import inch, mm
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

        # ── Website theme colors (extracted from CSS) ──
        # Navbar:  linear-gradient(135deg, #667eea 0%, #764ba2 100%)
        # Sidebar: linear-gradient(180deg, #667eea 0%, #764ba2 55%, #5c3691 100%)
        GRAD_START = '#667eea'
        GRAD_END = '#764ba2'
        GRAD_DARK = '#5c3691'
        COLOR_PRIMARY = colors.HexColor(GRAD_START)
        COLOR_ACCENT = colors.HexColor(GRAD_END)
        COLOR_DARK = colors.HexColor(GRAD_DARK)
        COLOR_BG = colors.HexColor('#F5F7FF')
        COLOR_BORDER = colors.HexColor('#e1e4eb')
        COLOR_TEXT = colors.HexColor('#333333')
        COLOR_MUTED = colors.HexColor('#777777')
        COLOR_LABEL = colors.HexColor('#666666')
        COLOR_SUCCESS_BG = colors.HexColor('#d4edda')
        COLOR_SUCCESS_TEXT = colors.HexColor('#155724')
        COLOR_WHITE = colors.white

        PAGE_W, PAGE_H = A4
        MARGIN = 40
        CONTENT_W = PAGE_W - 2 * MARGIN

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            topMargin=MARGIN, bottomMargin=MARGIN,
            leftMargin=MARGIN, rightMargin=MARGIN
        )

        styles = getSampleStyleSheet()

        # ── Custom paragraph styles ──
        s_header_brand = ParagraphStyle('HBrand', parent=styles['Normal'],
            fontSize=20, fontName='Helvetica-Bold', textColor=COLOR_WHITE, leading=24)
        s_header_sub = ParagraphStyle('HSub', parent=styles['Normal'],
            fontSize=9, textColor=colors.HexColor('#d0d0ff'), leading=12)
        s_header_right = ParagraphStyle('HRight', parent=styles['Normal'],
            fontSize=9, textColor=COLOR_WHITE, alignment=TA_RIGHT, leading=13)
        s_header_right_bold = ParagraphStyle('HRightBold', parent=styles['Normal'],
            fontSize=10, fontName='Helvetica-Bold', textColor=COLOR_WHITE, alignment=TA_RIGHT, leading=14)

        s_section_title = ParagraphStyle('SecTitle', parent=styles['Normal'],
            fontSize=11, fontName='Helvetica-Bold', textColor=COLOR_WHITE, leading=15)
        s_label = ParagraphStyle('Lbl', parent=styles['Normal'],
            fontSize=9, textColor=COLOR_MUTED, leading=12)
        s_value = ParagraphStyle('Val', parent=styles['Normal'],
            fontSize=10, fontName='Helvetica-Bold', textColor=COLOR_TEXT, leading=14)
        s_amount_big = ParagraphStyle('AmtBig', parent=styles['Normal'],
            fontSize=28, fontName='Helvetica-Bold', textColor=COLOR_PRIMARY, alignment=TA_CENTER, leading=34)
        s_amount_label = ParagraphStyle('AmtLbl', parent=styles['Normal'],
            fontSize=9, textColor=COLOR_MUTED, alignment=TA_CENTER, leading=12)
        s_badge = ParagraphStyle('Badge', parent=styles['Normal'],
            fontSize=10, fontName='Helvetica-Bold', textColor=COLOR_SUCCESS_TEXT, alignment=TA_CENTER, leading=14)
        s_footer = ParagraphStyle('Ftr', parent=styles['Normal'],
            fontSize=8, textColor=COLOR_MUTED, alignment=TA_CENTER, leading=11)
        s_footer_brand = ParagraphStyle('FtrBrand', parent=styles['Normal'],
            fontSize=10, fontName='Helvetica-Bold', textColor=COLOR_PRIMARY, alignment=TA_CENTER, leading=14)

        elements = []

        # ════════════════════════════════════════════════
        # SECTION 1: GRADIENT HEADER (diagonal gradient simulation)
        # Matches: linear-gradient(135deg, #667eea 0%, #764ba2 100%)
        # ════════════════════════════════════════════════
        # Color interpolation helpers (also used by footer)
        def hex_to_rgb(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

        def rgb_to_hex(r, g, b):
            return f'#{int(r):02x}{int(g):02x}{int(b):02x}'

        def interpolate_color(c1_hex, c2_hex, steps):
            r1, g1, b1 = hex_to_rgb(c1_hex)
            r2, g2, b2 = hex_to_rgb(c2_hex)
            result = []
            for i in range(steps):
                t = i / max(steps - 1, 1)
                result.append(rgb_to_hex(
                    r1 + (r2 - r1) * t,
                    g1 + (g2 - g1) * t,
                    b1 + (b2 - b1) * t
                ))
            return result

        # Footer still uses these
        GRAD_STEPS = 20
        grad_colors = interpolate_color(GRAD_START, GRAD_END, GRAD_STEPS)
        grad_cell_w = CONTENT_W / GRAD_STEPS

        # ── 6-column header gradient: left→right color transition ──
        HEADER_COLORS = ['#667eea', '#6f6edc', '#7560cf', '#764ba2', '#6a4098', '#5c3691']
        NUM_HDR_COLS = len(HEADER_COLORS)

        logo_path = os.path.join(app.static_folder or 'static', 'images', 'logo.png')

        # Left content: logo + brand
        left_parts = []
        if os.path.isfile(logo_path):
            try:
                left_parts.append(RLImage(logo_path, width=80, height=40))
            except Exception:
                left_parts.append(Paragraph('CreoVibe', s_header_brand))
        else:
            left_parts.append(Paragraph('CreoVibe', s_header_brand))
        left_parts.append(Spacer(1, 3))
        left_parts.append(Paragraph('Artist Booking Platform', s_header_sub))

        left_table = Table([[p] for p in left_parts], colWidths=[CONTENT_W * 0.42])
        left_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))

        # Right content: receipt info
        right_parts = [
            Paragraph('PAYMENT RECEIPT', s_header_right_bold),
            Spacer(1, 4),
            Paragraph(f'Receipt No: {receipt_no}', s_header_right),
            Paragraph(f'Date: {payment_date_fmt}', s_header_right),
        ]
        right_table = Table([[p] for p in right_parts], colWidths=[CONTENT_W * 0.42])
        right_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))

        # Build 6-column header: [left_content, grad, grad, grad, grad, right_content]
        # Col 0 and col 5 are wide (content), cols 1-4 are thin strips (gradient fill)
        left_w = CONTENT_W * 0.42
        right_w = CONTENT_W * 0.42
        mid_total = CONTENT_W - left_w - right_w
        mid_w = mid_total / (NUM_HDR_COLS - 2)

        hdr_col_widths = [left_w] + [mid_w] * (NUM_HDR_COLS - 2) + [right_w]
        hdr_row_data = [left_table] + [''] * (NUM_HDR_COLS - 2) + [right_table]

        header_table = Table([hdr_row_data], colWidths=hdr_col_widths, rowHeights=[90])
        hdr_style = [
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('LEFTPADDING', (0, 0), (0, 0), 18),
            ('RIGHTPADDING', (-1, 0), (-1, 0), 18),
            ('LEFTPADDING', (1, 0), (-2, 0), 0),
            ('RIGHTPADDING', (0, 0), (-2, 0), 0),
        ]
        for i, hc in enumerate(HEADER_COLORS):
            hdr_style.append(('BACKGROUND', (i, 0), (i, 0), colors.HexColor(hc)))
        header_table.setStyle(TableStyle(hdr_style))
        elements.append(header_table)

        elements.append(Spacer(1, 16))

        # ════════════════════════════════════════════════
        # SECTION 2: STATUS BADGE + TOTAL AMOUNT
        # ════════════════════════════════════════════════
        status_text = 'SUCCESS' if status == 'active' else status.upper()
        badge_bg = COLOR_SUCCESS_BG if status == 'active' else colors.HexColor('#fff3cd')
        badge_fg = COLOR_SUCCESS_TEXT if status == 'active' else colors.HexColor('#856404')
        s_badge_dyn = ParagraphStyle('BadgeDyn', parent=styles['Normal'],
            fontSize=10, fontName='Helvetica-Bold', textColor=badge_fg, alignment=TA_CENTER, leading=14)

        badge_table = Table(
            [[Paragraph(f'\u2713  {status_text}', s_badge_dyn)]],
            colWidths=[120], rowHeights=[28]
        )
        badge_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), badge_bg),
            ('ALIGN', (0, 0), (0, 0), 'CENTER'),
            ('VALIGN', (0, 0), (0, 0), 'MIDDLE'),
            ('ROUNDEDCORNERS', [14, 14, 14, 14]),
            ('LEFTPADDING', (0, 0), (0, 0), 8),
            ('RIGHTPADDING', (0, 0), (0, 0), 8),
        ]))

        # Rupee symbol — use 'Rs.' for font safety
        amount_table = Table(
            [
                [Paragraph(f'Rs. {price:,.2f}', s_amount_big)],
                [Paragraph('Total Amount Paid', s_amount_label)]
            ],
            colWidths=[CONTENT_W * 0.4], rowHeights=[42, 18]
        )
        amount_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOX', (0, 0), (0, 0), 1.2, COLOR_PRIMARY),
            ('ROUNDEDCORNERS', [8, 8, 8, 8]),
            ('TOPPADDING', (0, 0), (0, 0), 8),
            ('BOTTOMPADDING', (0, 1), (0, 1), 4),
        ]))

        # Center badge and amount in a row
        status_amount_row = Table(
            [[badge_table, amount_table]],
            colWidths=[CONTENT_W * 0.5, CONTENT_W * 0.5]
        )
        status_amount_row.setStyle(TableStyle([
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        elements.append(status_amount_row)
        elements.append(Spacer(1, 20))

        # ════════════════════════════════════════════════
        # HELPER: Section with themed header
        # ════════════════════════════════════════════════
        def build_section(title, rows_data):
            """Build a styled section with a gradient header and alternating rows."""
            section_elements = []

            # Section header
            hdr = Table(
                [[Paragraph(title, s_section_title)]],
                colWidths=[CONTENT_W], rowHeights=[30]
            )
            hdr.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, 0), COLOR_PRIMARY),
                ('VALIGN', (0, 0), (0, 0), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (0, 0), 14),
                ('ROUNDEDCORNERS', [6, 6, 0, 0]),
            ]))
            section_elements.append(hdr)

            # Content rows
            table_data = []
            for label, value in rows_data:
                table_data.append([
                    Paragraph(label, s_label),
                    Paragraph(str(value), s_value)
                ])

            content = Table(table_data, colWidths=[CONTENT_W * 0.4, CONTENT_W * 0.6])
            style_cmds = [
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
                ('LEFTPADDING', (0, 0), (0, -1), 14),
                ('LEFTPADDING', (1, 0), (1, -1), 10),
                ('LINEBELOW', (0, 0), (-1, -2), 0.5, COLOR_BORDER),
                ('BOX', (0, 0), (-1, -1), 0.5, COLOR_BORDER),
            ]
            # Alternating row backgrounds
            for i in range(len(table_data)):
                if i % 2 == 0:
                    style_cmds.append(('BACKGROUND', (0, i), (-1, i), COLOR_BG))
                else:
                    style_cmds.append(('BACKGROUND', (0, i), (-1, i), COLOR_WHITE))
            content.setStyle(TableStyle(style_cmds))
            section_elements.append(content)

            return section_elements

        # ════════════════════════════════════════════════
        # SECTION 3: EVENT DETAILS
        # ════════════════════════════════════════════════
        for el in build_section('EVENT DETAILS', [
            ('Artist Name', artist_name),
            ('Plan Name', plan_name),
            ('Start Date', payment_date_fmt),
            ('End Date', expiry_date_fmt),
        ]):
            elements.append(el)

        elements.append(Spacer(1, 14))

        # ════════════════════════════════════════════════
        # SECTION 4: PAYMENT DETAILS
        # ════════════════════════════════════════════════
        for el in build_section('PAYMENT DETAILS', [
            ('Payment Method', 'Razorpay'),
            ('Payment Status', 'SUCCESS' if status == 'active' else status.upper()),
            ('Paid At', payment_time_fmt),
        ]):
            elements.append(el)

        elements.append(Spacer(1, 14))

        # ════════════════════════════════════════════════
        # SECTION 5: TRANSACTION INFO
        # ════════════════════════════════════════════════
        for el in build_section('TRANSACTION INFO', [
            ('Payment ID', f'pay_{sub_id}'),
            ('Subscription ID', str(sub_id)),
            ('Receipt Number', receipt_no),
        ]):
            elements.append(el)

        elements.append(Spacer(1, 28))

        # ════════════════════════════════════════════════
        # SECTION 6: FOOTER
        # ════════════════════════════════════════════════
        # Thin gradient line
        footer_line = Table([[''] * GRAD_STEPS], colWidths=[grad_cell_w] * GRAD_STEPS, rowHeights=[3])
        fl_cmds = []
        for i, gc in enumerate(grad_colors):
            fl_cmds.append(('BACKGROUND', (i, 0), (i, 0), colors.HexColor(gc)))
        fl_cmds.append(('TOPPADDING', (0, 0), (-1, -1), 0))
        fl_cmds.append(('BOTTOMPADDING', (0, 0), (-1, -1), 0))
        footer_line.setStyle(TableStyle(fl_cmds))
        elements.append(footer_line)

        elements.append(Spacer(1, 10))
        elements.append(Paragraph('Thank you for choosing CreoVibe!', s_footer_brand))
        elements.append(Spacer(1, 4))
        elements.append(Paragraph('This is a computer-generated receipt and does not require a signature.', s_footer))
        elements.append(Paragraph(f'Generated on {datetime.now().strftime("%d/%m/%Y %I:%M %p")}', s_footer))

        doc.build(elements)
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name=f'CreoVibe_Receipt_{sub_id}.pdf',
            mimetype='application/pdf'
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/<path:page>')  # Changed <page> to <path:page> to catch ALL urls, even with slashes
def static_page(page):
    # 1. Clean up the URL if the browser accidentally includes 'templates/'
    if page.startswith('templates/'):
        page = page.replace('templates/', '', 1)
        
    # 2. Add .html if the URL doesn't have an extension
    if not page.endswith('.html') and '.' not in page:
        page += '.html'
        
    # 3. Print exactly what the server is trying to find to your terminal
    print(f"\n---> BROWSER IS LOOKING FOR: {page} <---")
        
    try:
        # Safely render the template
        return render_template(page)
    except TemplateNotFound:
        # If the file isn't in the folder, print a massive error to the terminal
        print(f"---> CRITICAL ERROR: '{page}' IS MISSING FROM THE 'templates' FOLDER! <---\n")
        
        # Show a helpful error directly on the browser screen instead of a generic 404
        error_html = f"""
            <div style="font-family: Arial; padding: 40px; text-align: center;">
                <h2 style="color: #ff4757;">File Not Found</h2>
                <p>Flask is looking for the file <b>{page}</b>, but it is missing from your <b>templates</b> folder.</p>
                <p>Please ensure {page} is saved exactly at: <i>D:\\ADHYA\\SDP\\CODE\\creovibe_artist\\templates\\{page}</i></p>
            </div>
        """
        return error_html, 404


# ========== API ROUTES ==========

# 1. STATES API ENDPOINT
@app.route('/api/states')
@login_required
def api_states():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT state_id, state_name FROM state_table ORDER BY state_name")
        states = cur.fetchall()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'states': states})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# 2. CITIES API ENDPOINT
@app.route('/api/cities/<int:state_id>')
@login_required
def api_cities(state_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT city_id, city_name FROM city_table WHERE state_id = %s ORDER BY city_name", (state_id,))
        cities = cur.fetchall()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'cities': cities})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# 3. CATEGORIES
@app.route('/api/categories')
def api_categories():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT category_id, category_name
            FROM category_table
            ORDER BY category_name
            """
        )
        categories = cur.fetchall() or []
        return jsonify({'success': True, 'categories': categories})
    except Exception as e:
        logger.exception("Categories fetch failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/register', methods=['POST'])
def api_register():
    conn = None
    cur = None
    try:
        payload = request.form if request.form else (request.get_json(silent=True) or {})

        first_name = str(payload.get('first_name') or '').strip()
        last_name = str(payload.get('last_name') or '').strip()
        username = str(payload.get('username') or '').strip()
        email = str(payload.get('email') or '').strip()
        raw_password = str(payload.get('password') or '').strip()
        gender = str(payload.get('gender') or '').strip()
        dob = str(payload.get('dob') or '').strip()
        phone_number = str(payload.get('phone_number') or payload.get('phone') or '').strip()
        pincode = str(payload.get('pincode') or '').strip()
        state_id = payload.get('state_id')
        city_id = payload.get('city_id')
        category_id = payload.get('category_id')
        category_name = str(payload.get('category') or '').strip()

        if not all([first_name, last_name, username, raw_password, gender, dob, phone_number, pincode]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        if not category_id and not category_name:
            return jsonify({'success': False, 'error': 'Category is required'}), 400
        if not state_id or not city_id:
            return jsonify({'success': False, 'error': 'State and city are required'}), 400

        conn = get_db()
        cur = conn.cursor()

        if category_id is None or str(category_id).strip() == '':
            category_id = resolve_category_id(cur, category_name)
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Invalid category_id'}), 400

        cur.execute("SELECT category_name FROM category_table WHERE category_id = %s LIMIT 1", (category_id,))
        category_row = cur.fetchone()
        if not category_row:
            return jsonify({'success': False, 'error': 'Invalid category selected'}), 400
        category_text = category_row.get('category_name') or ''

        cur.execute("SELECT COUNT(*) AS total FROM state_table WHERE state_id = %s", (state_id,))
        if int((cur.fetchone() or {}).get('total') or 0) == 0:
            return jsonify({'success': False, 'error': 'Invalid state selected'}), 400

        cur.execute("SELECT COUNT(*) AS total FROM city_table WHERE city_id = %s AND state_id = %s", (city_id, state_id))
        if int((cur.fetchone() or {}).get('total') or 0) == 0:
            return jsonify({'success': False, 'error': 'Invalid city selected'}), 400
        
        cur.execute(
            "SELECT artist_id FROM artist_table WHERE username = %s OR phone_number = %s LIMIT 1",
            (username, phone_number)
        )
        if cur.fetchone():
            return jsonify({'success': False, 'error': 'Username or phone number already exists'}), 400

        portfolio_files = request.files.getlist('portfolio_files')
        portfolio_paths = []
        if portfolio_files:
            os.makedirs(app.config['PORTFOLIO_UPLOAD_DIR'], exist_ok=True)
            for file_obj in portfolio_files:
                if not file_obj or not file_obj.filename:
                    continue
                if not is_allowed_portfolio_file(file_obj.filename):
                    return jsonify({'success': False, 'error': 'Only jpg, jpeg, png, mp4 portfolio files are allowed'}), 400
                safe_name = secure_filename(file_obj.filename)
                ext = safe_name.rsplit('.', 1)[1].lower()
                stored_name = f"new_{uuid.uuid4().hex}.{ext}"
                abs_path = os.path.join(app.config['PORTFOLIO_UPLOAD_DIR'], stored_name)
                file_obj.save(abs_path)
                portfolio_paths.append('/static/uploads/portfolio/' + stored_name)

        hashed_password = bcrypt.hashpw(raw_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        primary_portfolio_path = portfolio_paths[0] if portfolio_paths else '/static/uploads/portfolio/default_portfolio.jpg'

        cur.execute(
            """
            INSERT INTO artist_table
            (first_name, last_name, username, password, email, gender, dob, phone_number,
             state_id, city_id, category_id, portfolio_path, verification_status, is_enabled,
             profile_pic, portfolio_files, rating)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 1,
                    %s, %s, %s)
            """,
            (
                first_name, last_name, username, hashed_password, email or None,
                gender, dob, phone_number,
                state_id, city_id, category_id, primary_portfolio_path,
                '', json.dumps(portfolio_paths), 0.0
            )
        )
        new_artist_id = cur.lastrowid
        conn.commit()

        return jsonify({
            'success': True,
            'message': 'Registration submitted. Account is pending approval.',
            'artist_id': new_artist_id
        })
        
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("Registration failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/forgot_password', methods=['POST'])
def api_forgot_password():
    conn = None
    cur = None
    try:
        data = request.get_json() or {}
        username = str(data.get('username') or '').strip()
        email = str(data.get('email') or '').strip()
        if not username and not email:
            return jsonify({'success': False, 'error': 'username or email is required'}), 400

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT artist_id, username, email
            FROM artist_table
            WHERE LOWER(username) = LOWER(%s) OR LOWER(email) = LOWER(%s)
            LIMIT 1
            """,
            (username or email, email or username)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({'success': True, 'message': 'If account exists, reset instructions have been sent'})

        reset_token = uuid.uuid4().hex
        logger.info("Password reset token for artist_id=%s: %s", row.get('artist_id'), reset_token)

        return jsonify({
            'success': True,
            'message': 'Password reset email queued'
        })
    except Exception as e:
        logger.exception("Forgot password failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# 3. LOGIN - FIXED VERSION

# 3. LOGIN - WORKING VERSION

@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Invalid request'}), 400

        data = request.get_json() or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()

        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and password required'}), 400

        conn = get_db()
        cur = conn.cursor()

        # Use correct schema column names for artist_table
        cur.execute("""
            SELECT 
                artist_id,
                first_name,
                last_name,
                username,
                email,
                password,
                verification_status,
                is_enabled,
                category_id
            FROM artist_table
            WHERE LOWER(username) = LOWER(%s) OR LOWER(email) = LOWER(%s)
            LIMIT 1
        """, (username, username))

        artist = cur.fetchone()

        if not artist:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'User not found'})

        # Check verification status
        status = str(artist.get('verification_status') or '').strip().lower()
        if status != 'approved':
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Your account is pending approval'})

        # Check if enabled
        try:
            enabled = int(artist.get('is_enabled') or 0)
        except (TypeError, ValueError):
            enabled = 0
        if enabled != 1:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Account disabled'})

        # Check password
        stored_hash = str(artist.get('password') or '')
        if not stored_hash or not bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid password'})

        # Set session
        session['artist_id'] = artist['artist_id']
        session['username'] = artist['username']
        session.permanent = True

        # Create free trial if needed
        sub_conn = get_db()
        try:
            sub_cur = sub_conn.cursor()
            create_free_trial_if_missing(sub_cur, artist['artist_id'])
            sub_conn.commit()
            sub_cur.close()
        finally:
            sub_conn.close()

        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'artist_id': artist['artist_id'],
            'name': f"{artist.get('first_name', '')} {artist.get('last_name', '')}".strip(),
            'email': artist.get('email', '')
        })

    except Exception as e:
        logger.exception("Login error")
        return jsonify({'success': False, 'error': str(e)})

'''@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Invalid request'}), 400

        data = request.get_json() or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()

        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and password required'}), 400

        conn = get_db()
        cur = conn.cursor()

        # Use CORRECT lowercase column names
        cur.execute("""
            SELECT 
                artist_id,
                first_name,
                last_name,
                username,
                password,
                verification_status,
                is_enabled,
                category_id
            FROM artist_table
            WHERE LOWER(username) = LOWER(%s) OR LOWER(email) = LOWER(%s)
            LIMIT 1
        """, (username, username))

        artist = cur.fetchone()

        if not artist:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'User not found'})

        # Check verification status - use lowercase column name
        status = str(artist.get('verification_status') or '').strip().lower()
        if status != 'approved':
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Your account is pending approval'})

        # Check if enabled - use lowercase column name
        try:
            enabled = int(artist.get('is_enabled') or 0)
        except (TypeError, ValueError):
            enabled = 0
        if enabled != 1:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Account disabled'})

        # Check password
        stored_hash = str(artist.get('password') or '')
        if not stored_hash or not bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid password'})

        # Set session
        session['artist_id'] = artist['artist_id']
        session['username'] = artist['username']
        session.permanent = True

        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'artist_id': artist['artist_id'],
            'name': f"{artist.get('first_name', '')} {artist.get('last_name', '')}".strip(),
            'category_id': artist.get('category_id')
        })

    except Exception as e:
        logger.exception("Login error")
        return jsonify({'success': False, 'error': str(e)})'''

'''@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Invalid request'}), 400

        data = request.get_json() or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()

        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and password required'}), 400

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT 
                a.artist_id,
                a.first_name,
                a.last_name,
                a.username,
                a.password,
                a.verification_status,
                a.is_enabled,
                a.category_id,
                ct.category_name
            FROM artist_table a
            LEFT JOIN category_table ct 
                ON a.category_id = ct.category_id
            WHERE LOWER(a.username) = LOWER(%s)
            LIMIT 1
        """, (username,))


        artist = cur.fetchone()

        if not artist:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'User not found'})

        status = str(artist.get('Verification_status') or '').strip().lower()
        if status != 'approved':
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Your account is pending approval'})

        try:
            enabled = int(artist.get('Is_enabled') or 0)
        except (TypeError, ValueError):
            enabled = 0
        if enabled != 1:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Account disabled'})

        stored_hash = str(artist.get('Password') or '')
        if not stored_hash or not bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid password'})

        session['artist_id'] = artist['artist_id']
        session['username'] = artist['username']
        session.permanent = True

        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'artist_id': artist['artist_id'],
            'name': f"{artist['first_name']} {artist['last_name']}",
            'category': artist.get('category_name') or artist.get('Category') or '',
            'category_id': artist.get('category_id')
        })

    except Exception as e:
        logger.exception("Login error")
        return jsonify({'success': False, 'error': str(e)})'''


@app.route('/api/debug_passwords', methods=['GET'])
def debug_passwords():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT artist_ID, username, password, verification_status, is_enabled
            FROM artist_table
            ORDER BY artist_ID ASC
            """
        )
        artists = cur.fetchall()

        result = []
        for artist in artists:
            pwd = str(artist.get('password') or '')
            pwd_preview = pwd[:20] + "..." if len(pwd) > 20 else pwd
            is_bcrypt_prefix = pwd.startswith('$2b$') or pwd.startswith('$2a$') or pwd.startswith('$2y$')
            bcrypt_usable = False
            if is_bcrypt_prefix:
                try:
                    bcrypt.checkpw('Test@1234'.encode('utf-8'), pwd.encode('utf-8'))
                    bcrypt_usable = True
                except Exception:
                    bcrypt_usable = False
            result.append({
                'artist_ID': artist['artist_ID'],
                'username': artist['username'],
                'password_preview': pwd_preview,
                'password_length': len(pwd),
                'starts_with_bcrypt': is_bcrypt_prefix,
                'bcrypt_hash_readable': bcrypt_usable,
                'verification_status': artist.get('verification_status'),
                'is_enabled': artist.get('is_enabled')
            })

        return jsonify({'success': True, 'artists': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
    

# 4. LOGOUT

@app.route('/api/deactivate', methods=['POST'])
@login_required
def api_deactivate():
    """Deactivate the logged-in artist account (is_enabled = 0) then log out."""
    try:
        data = request.get_json(silent=True) or {}
        reason = str(data.get('reason') or '').strip()
        if len(reason) < 10:
            return jsonify({'success': False, 'error': 'Please provide at least 10 characters for deactivation reason'}), 400
        if reason.isdigit():
            return jsonify({'success': False, 'error': 'Reason cannot be only numbers'}), 400
        artist_id = session['artist_id']
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE artist_table SET is_enabled = 0 WHERE artist_id = %s",
            (artist_id,)
        )
        try:
            add_artist_notification(cur, artist_id, "Account Deactivated", f"Account deactivated: {reason}")
        except Exception:
            pass
        conn.commit()
        cur.close()
        conn.close()
        session.clear()
        return jsonify({'success': True, 'message': 'Account deactivated'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/change_password', methods=['POST'])
@login_required
def api_change_password():
    """Change the logged-in artist's password."""
    conn = None
    cur = None
    try:
        data = request.get_json(silent=True) or {}
        current_password = str(data.get('current_password') or '').strip()
        new_password = str(data.get('new_password') or '').strip()
        confirm_password = str(data.get('confirm_password') or '').strip()

        # --- Validation ---
        if not current_password or not new_password or not confirm_password:
            return jsonify({'success': False, 'error': 'All fields are required'}), 400

        if new_password != confirm_password:
            return jsonify({'success': False, 'error': 'New password and confirm password do not match'}), 400

        if len(new_password) < 6:
            return jsonify({'success': False, 'error': 'New password must be at least 6 characters'}), 400

        if current_password == new_password:
            return jsonify({'success': False, 'error': 'New password must be different from current password'}), 400

        artist_id = session['artist_id']

        # 1. Fetch stored password hash from DB
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT password FROM artist_table WHERE artist_id = %s",
            (artist_id,)
        )
        row = cur.fetchone()

        if not row:
            return jsonify({'success': False, 'error': 'Artist not found'}), 404

        stored_hash = str(row.get('password') or '')

        # 2. Validate old password
        if not stored_hash or not bcrypt.checkpw(
            current_password.encode('utf-8'),
            stored_hash.encode('utf-8')
        ):
            return jsonify({'success': False, 'error': 'Current password is incorrect'}), 400

        # 3. Hash the new password
        new_hashed = bcrypt.hashpw(
            new_password.encode('utf-8'),
            bcrypt.gensalt()
        ).decode('utf-8')

        # 4. Update password in DB
        cur.execute(
            "UPDATE artist_table SET password = %s WHERE artist_id = %s",
            (new_hashed, artist_id)
        )

        # 5. Commit changes
        conn.commit()

        return jsonify({'success': True, 'message': 'Password changed successfully'})

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    response = make_response(jsonify({'success': True}))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
    #return jsonify({'success': True})

@app.route('/api/auth/verify')
def api_auth_verify():
    if 'artist_id' in session:
        return jsonify({
            'authenticated': True,
            'artist_id': session.get('artist_id'),
            'username': session.get('username')
        })
    return jsonify({'authenticated': False}), 401


# ========== FORGOT PASSWORD — 3-STEP OTP FLOW ==========

@app.route('/api/forgot_password/send_otp', methods=['POST'])
def api_forgot_password_send_otp():
    """
    STEP 1 — Artist enters only their registered email.
    Looks up artist by email, sends OTP to that email address.
    """
    conn = None
    cur  = None
    try:
        data  = request.get_json(silent=True) or {}
        email = str(data.get('email') or '').strip().lower()

        if not email:
            return jsonify({'success': False, 'error': 'Email address is required'}), 400

        conn = get_db()
        cur  = conn.cursor()

        # Look up artist by email
        cur.execute(
            "SELECT artist_id, first_name FROM artist_table WHERE LOWER(email) = LOWER(%s) LIMIT 1",
            (email,)
        )
        artist = cur.fetchone()

        if not artist:
            return jsonify({'success': False, 'error': 'Email not registered'}), 404

        otp = str(random.randint(100000, 999999))

        session['reset_otp']        = otp
        session['reset_email']      = email
        session['reset_otp_expiry'] = (datetime.now() + timedelta(minutes=5)).isoformat()
        session['reset_artist_id']  = artist['artist_id']
        session.pop('reset_otp_verified', None)

        artist_name = str(artist.get('first_name') or '').strip()

        logger.info('Sending OTP %s to %s for artist_id=%s', otp, email, artist['artist_id'])

        sent = send_otp_email(email, otp, artist_name)

        if not sent:
            return jsonify({'success': False, 'error': 'Failed to send OTP email. Please try again.'}), 500

        return jsonify({
            'success': True,
            'message': f'OTP sent to {email}. Valid for 5 minutes.',
            'email':   email
        })

    except Exception as e:
        logger.exception('send_otp failed')
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:  cur.close()
        if conn: conn.close()


@app.route('/api/forgot_password/verify_otp', methods=['POST'])
def api_forgot_password_verify_otp():
    """STEP 2 — Validate OTP against session value and expiry."""
    try:
        data = request.get_json(silent=True) or {}
        otp  = str(data.get('otp') or '').strip()

        if not otp:
            return jsonify({'success': False, 'error': 'OTP is required'}), 400

        stored_otp    = session.get('reset_otp')
        stored_expiry = session.get('reset_otp_expiry')

        if not stored_otp or not stored_expiry:
            return jsonify({'success': False, 'error': 'No OTP found. Please request a new one.'}), 400

        if datetime.now() > datetime.fromisoformat(stored_expiry):
            session.pop('reset_otp', None)
            session.pop('reset_otp_expiry', None)
            session.pop('reset_email', None)
            session.pop('reset_artist_id', None)
            return jsonify({'success': False, 'error': 'OTP has expired. Please request a new one.'}), 400

        if otp != stored_otp:
            return jsonify({'success': False, 'error': 'Invalid OTP. Please try again.'}), 400

        session.pop('reset_otp', None)
        session.pop('reset_otp_expiry', None)
        session['reset_otp_verified'] = True

        return jsonify({'success': True, 'message': 'OTP verified. You can now reset your password.'})

    except Exception as e:
        logger.exception('verify_otp failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/forgot_password/reset_password', methods=['POST'])
def api_forgot_password_reset_password():
    """STEP 3 — Validate new password → bcrypt hash → save to DB."""
    conn = None
    cur  = None
    try:
        if not session.get('reset_otp_verified'):
            return jsonify({'success': False, 'error': 'OTP not verified. Please complete verification first.'}), 403

        email = session.get('reset_email')
        if not email:
            return jsonify({'success': False, 'error': 'Session expired. Please start over.'}), 400

        data         = request.get_json(silent=True) or {}
        new_password = str(data.get('new_password') or '').strip()
        confirm      = str(data.get('confirm_password') or '').strip()

        if not new_password or not confirm:
            return jsonify({'success': False, 'error': 'Both password fields are required'}), 400
        if new_password != confirm:
            return jsonify({'success': False, 'error': 'Passwords do not match'}), 400
        if len(new_password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters'}), 400
        if not re.search(r'[A-Z]', new_password):
            return jsonify({'success': False, 'error': 'Password must contain an uppercase letter'}), 400
        if not re.search(r'[a-z]', new_password):
            return jsonify({'success': False, 'error': 'Password must contain a lowercase letter'}), 400
        if not re.search(r'\d', new_password):
            return jsonify({'success': False, 'error': 'Password must contain a number'}), 400
        if not re.search(r'[^A-Za-z0-9]', new_password):
            return jsonify({'success': False, 'error': 'Password must contain a special character'}), 400

        hashed = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        conn = get_db()
        cur  = conn.cursor()
        cur.execute('UPDATE artist_table SET password = %s WHERE email = %s', (hashed, email))
        conn.commit()

        for key in ('reset_otp_verified', 'reset_artist_id', 'reset_email'):
            session.pop(key, None)

        return jsonify({'success': True, 'message': 'Password reset successfully. You can now log in.'})

    except Exception as e:
        if conn: conn.rollback()
        logger.exception('reset_password failed')
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# 5. CHECK SESSION
# 5. CHECK SESSION - FIXED
@app.route('/api/check_session')
def api_check_session():
    if 'artist_id' in session:
        conn = get_db()
        cur = conn.cursor()
        ensure_artist_schema(cur)
        cur.execute("SELECT first_name, last_name, profile_pic FROM artist_table WHERE artist_id = %s", (session['artist_id'],))
        artist = cur.fetchone()
        cur.close()
        conn.close()
        first_name = (artist or {}).get('first_name') or ''
        last_name = (artist or {}).get('last_name') or ''
        initials = ((first_name[:1] + last_name[:1]).upper() or (first_name[:2].upper() if first_name else 'CV'))
        return jsonify({
            'logged_in': True,
            'artist_id': session['artist_id'],
            'name': first_name,
            'user_initials': initials,
            'profile_pic': (artist or {}).get('profile_pic') or ''
        })
    return jsonify({'logged_in': False})

# 6. PROFILE
# 6. PROFILE - FIXED COLUMN NAMES
@app.route('/api/profile')
@login_required
def api_profile():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        # Try with c.pincode first; fall back without it if column doesn't exist
        profile_query_with_pincode = """
            SELECT
                a.artist_id,
                a.first_name,
                a.last_name,
                a.username,
                a.password,
                a.gender,
                a.dob,
                a.phone_number,
                a.state_id,
                a.city_id,
                a.portfolio_path,
                a.verification_status,
                a.is_enabled,
                a.created_at,
                a.profile_pic,
                a.portfolio_files,
                a.working_start_time,
                a.working_end_time,
                a.email,
                a.experience_years,
                a.price_per_hour,
                a.rating,
                a.category_id,
                ct.category_name,
                s.state_name,
                c.city_name,
                c.pincode,
                abd.bank_name,
                abd.account_number AS bank_account_number,
                abd.account_holder_name,
                abd.ifsc_code,
                abd.upi_id
            FROM artist_table a
            LEFT JOIN category_table ct ON a.category_id = ct.category_id
            LEFT JOIN state_table s ON a.state_id = s.state_id
            LEFT JOIN city_table c ON a.city_id = c.city_id
            LEFT JOIN artist_bank_details abd ON a.artist_id = abd.artist_id
            WHERE a.artist_id = %s
            LIMIT 1
        """
        profile_query_without_pincode = """
            SELECT
                a.artist_id,
                a.first_name,
                a.last_name,
                a.username,
                a.password,
                a.gender,
                a.dob,
                a.phone_number,
                a.state_id,
                a.city_id,
                a.portfolio_path,
                a.verification_status,
                a.is_enabled,
                a.created_at,
                a.profile_pic,
                a.portfolio_files,
                a.working_start_time,
                a.working_end_time,
                a.email,
                a.experience_years,
                a.price_per_hour,
                a.rating,
                a.category_id,
                ct.category_name,
                s.state_name,
                c.city_name,
                NULL AS pincode,
                abd.bank_name,
                abd.account_number AS bank_account_number,
                abd.account_holder_name,
                abd.ifsc_code,
                abd.upi_id
            FROM artist_table a
            LEFT JOIN category_table ct ON a.category_id = ct.category_id
            LEFT JOIN state_table s ON a.state_id = s.state_id
            LEFT JOIN city_table c ON a.city_id = c.city_id
            LEFT JOIN artist_bank_details abd ON a.artist_id = abd.artist_id
            WHERE a.artist_id = %s
            LIMIT 1
        """
        try:
            cur.execute(profile_query_with_pincode, (artist_id,))
        except Exception:
            cur.execute(profile_query_without_pincode, (artist_id,))

        '''cur.execute(
            """
            SELECT
                a.Artist_ID, a.First_Name, a.Last_Name, a.Username, a.Email,
                a.Gender, a.dob, a.Phone_Number, a.State_ID, a.City_ID,
                a.Portfolio_Path, a.verification_status, a.is_enabled, a.created_at,
                a.profile_pic, a.portfolio_files,
                a.working_start_time, a.working_end_time,
                a.experience_years, a.price_per_hour, a.rating, a.category_id,
                ct.category_name,
                s.state_name,
                c.city_name,
                c.pincode,
                abd.bank_name,
                abd.account_number  AS bank_account_number,
                abd.account_holder_name,
                abd.ifsc_code,
                abd.upi_id
            FROM artist_table a
            LEFT JOIN category_table ct  ON a.category_id = ct.category_id
            LEFT JOIN state_table s      ON a.State_ID    = s.state_id
            LEFT JOIN city_table c       ON a.City_ID     = c.city_id
            LEFT JOIN artist_bank_details abd ON a.Artist_ID = abd.artist_id
            WHERE a.Artist_ID = %s
            LIMIT 1
            """,
            (artist_id,)
        )'''



        artist = cur.fetchone()
        
        if not artist:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Artist not found'})

        portfolio_files = parse_portfolio_paths(artist.get('portfolio_files'))
        if not portfolio_files and artist.get('portfolio_path'):
            portfolio_files = [artist.get('portfolio_path')]

        artist_payload = {
            'artist_id': artist.get('artist_id'),
            'first_name': artist.get('first_name') or '',
            'last_name': artist.get('last_name') or '',
            'username': artist.get('username') or '',
            'email': artist.get('email') or '',
            'gender': artist.get('gender') or '',
            'dob': str(artist.get('dob') or ''),
            'phone_number': artist.get('phone_number') or '',
            'pincode': artist.get('pincode') or '',
            'state_id': artist.get('state_id'),
            'city_id': artist.get('city_id'),
            'state_name': artist.get('state_name') or '',
            'city_name': artist.get('city_name') or '',
            'category': artist.get('category_name') or '',
            'category_id': artist.get('category_id'),
            'category_name': artist.get('category_name') or '',
            'portfolio_path': artist.get('portfolio_path') or '',
            'portfolio_files': portfolio_files,
            'profile_pic': artist.get('profile_pic') or '',
            'verification_status': artist.get('verification_status') or '',
            'is_enabled': artist.get('is_enabled'),
            'created_at': artist.get('created_at').isoformat() if artist.get('created_at') else None,
            'working_start_time': artist.get('working_start_time') or '',
            'working_end_time': artist.get('working_end_time') or '',
            'bank_name': artist.get('bank_name') or '',
            'bank_account_number': artist.get('bank_account_number') or '',
            'account_holder_name': artist.get('account_holder_name') or '',
            'ifsc_code': artist.get('ifsc_code') or '',
            'upi_id': artist.get('upi_id') or '',
            'experience_years': artist.get('experience_years'),
            'price_per_hour': float(artist.get('price_per_hour') or 0),
            'rating': float(artist.get('rating') or 0.0)
        }
        
        cur.execute("SHOW TABLES")
        table_rows = cur.fetchall() or []
        table_names = {str(list(r.values())[0]).lower() for r in table_rows if r}

        stats = {
            'total_bookings': 0,
            'avg_rating': 0.0,
            'earnings': 0.0,
            'days_on_platform': 0
        }

        if 'booking_table' in table_names:
            booking_cols = get_table_columns(cur, 'booking_table')
            booking_artist_col = pick_column(booking_cols, ['artist_id'])
            if booking_artist_col:
                cur.execute(
                    f"SELECT COUNT(*) AS total FROM booking_table WHERE `{booking_artist_col}` = %s",
                    (artist_id,)
                )
                stats['total_bookings'] = int((cur.fetchone() or {}).get('total') or 0)

        if 'feedback_table' in table_names:
            feedback_cols = get_table_columns(cur, 'feedback_table')
            feedback_artist_col = pick_column(feedback_cols, ['artist_id'])
            feedback_rating_col = pick_column(feedback_cols, ['rating'])
            if feedback_artist_col and feedback_rating_col:
                cur.execute(
                    f"""
                    SELECT AVG(`{feedback_rating_col}`) AS avg_rating
                    FROM feedback_table
                    WHERE `{feedback_artist_col}` = %s
                    """,
                    (artist_id,)
                )
                stats['avg_rating'] = float((cur.fetchone() or {}).get('avg_rating') or 0.0)

        earnings_table = None
        for candidate in ('payment_table', 'earnings_table', 'booking_table'):
            if candidate in table_names:
                earnings_table = candidate
                break
        if earnings_table:
            earnings_cols = get_table_columns(cur, earnings_table)
            earnings_artist_col = pick_column(earnings_cols, ['artist_id'])
            earnings_amount_col = pick_column(earnings_cols, ['amount', 'payment_amount'])
            if earnings_artist_col and earnings_amount_col:
                cur.execute(
                    f"""
                    SELECT SUM(`{earnings_amount_col}`) AS total_earnings
                    FROM {earnings_table}
                    WHERE `{earnings_artist_col}` = %s
                    """,
                    (artist_id,)
                )
                stats['earnings'] = float((cur.fetchone() or {}).get('total_earnings') or 0.0)

        if artist.get('created_at'):
            cur.execute(
                "SELECT DATEDIFF(CURDATE(), DATE(created_at)) AS days_on_platform FROM artist_table WHERE artist_id = %s",
                (artist_id,)
            )
            stats['days_on_platform'] = int((cur.fetchone() or {}).get('days_on_platform') or 0)
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'artist': artist_payload,
            'stats': stats
        })
    except Exception as e:
        logger.exception("Profile fetch failed")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/profile/portfolio', methods=['POST'])
@login_required
def api_profile_portfolio_upload():
    try:
        files = request.files.getlist('portfolio_files')
        if not files:
            return jsonify({'success': False, 'error': 'No files selected'})

        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        ensure_artist_schema(cur)
        cur.execute("SELECT portfolio_files FROM artist_table WHERE artist_id = %s", (artist_id,))
        row = cur.fetchone()
        existing_files = parse_portfolio_paths((row or {}).get('portfolio_files'))

        if len(existing_files) + len(files) > 10:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Maximum 10 media files allowed'})

        os.makedirs(app.config['PORTFOLIO_UPLOAD_DIR'], exist_ok=True)
        uploaded_paths = []

        for file_obj in files:
            filename = file_obj.filename or ''
            if not is_allowed_portfolio_file(filename):
                cur.close()
                conn.close()
                return jsonify({'success': False, 'error': 'Only jpg, jpeg, png, mp4 files are allowed'})

            safe_name = secure_filename(filename)
            ext = safe_name.rsplit('.', 1)[1].lower()
            stored_name = f"{artist_id}_{uuid.uuid4().hex}.{ext}"
            abs_path = os.path.join(app.config['PORTFOLIO_UPLOAD_DIR'], stored_name)
            file_obj.save(abs_path)
            uploaded_paths.append('/static/uploads/portfolio/' + stored_name)

        final_files = existing_files + uploaded_paths

        # Minimum portfolio validation removed

        cur.execute(
            "UPDATE artist_table SET portfolio_files = %s WHERE artist_id = %s",
            (json.dumps(final_files), artist_id)
        )

        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'portfolio_files': final_files})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/artist/<int:artist_id>/portfolio')
def api_artist_portfolio(artist_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        ensure_artist_schema(cur)
        cur.execute("SELECT portfolio_files FROM artist_table WHERE artist_id = %s", (artist_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return jsonify({'success': False, 'error': 'Artist not found'}), 404

        return jsonify({'success': True, 'portfolio_files': parse_portfolio_paths(row.get('portfolio_files'))})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# 7. UPDATE PROFILE
# 7. UPDATE PROFILE - FIXED COLUMN NAMES
@app.route('/api/profile/update', methods=['POST'])
@login_required
def api_update_profile():
    try:
        data = request.json
        artist_id = session['artist_id']
        
        # Validate required fields
        required_fields = ['first_name', 'last_name', 'phone', 'category', 'state_id', 'city_id']
        for field in required_fields:
            if field not in data or not data[field]:
                return jsonify({'success': False, 'error': f'{field.replace("_", " ").title()} is required'})
        
        conn = get_db()
        cur = conn.cursor()
        
        # Check if state and city exist (lowercase per schema)
        cur.execute("SELECT COUNT(*) as count FROM state_table WHERE state_id = %s", (data['state_id'],))
        state_exists = cur.fetchone()['count'] > 0
        
        cur.execute("SELECT COUNT(*) as count FROM city_table WHERE city_id = %s AND state_id = %s", 
                   (data['city_id'], data['state_id']))
        city_exists = cur.fetchone()['count'] > 0
        
        if not state_exists:
            return jsonify({'success': False, 'error': 'Invalid state selected'})
        
        if not city_exists:
            return jsonify({'success': False, 'error': 'Invalid city selected'})

        category_id = resolve_category_id(cur, data.get('category'))
        if not category_id:
            return jsonify({'success': False, 'error': 'Invalid category selected'})

        # Update query with correct column names
        cur.execute("""
            UPDATE artist_table 
            SET first_name = %s, 
                last_name = %s, 
                phone_number = %s,
                state_id = %s, 
                city_id = %s, 
                category_id = %s,
                experience_years = %s,
                price_per_hour = %s,
                portfolio_path = %s
            WHERE artist_id = %s
        """, (
            data['first_name'],
            data['last_name'],
            data['phone'],
            data['state_id'],
            data['city_id'],
            category_id,
            int(data.get('experience_years') or 0),
            float(data.get('price_per_hour') or 0),
            str(data.get('portfolio_path') or '').strip(),
            artist_id
        ))
        
        # Update artist_table using correct PascalCase column names per schema
        '''cur.execute("""
            UPDATE artist_table 
            SET First_Name = %s, 
                Last_Name = %s, 
                Phone_Number = %s,
                Gender = %s, 
                dob = %s, 
                State_ID = %s, 
                City_ID = %s, 
                category_id = %s
            WHERE Artist_ID = %s
        """, (
            data['first_name'],
            data['last_name'],
            data['phone'],
            data['gender'],
            data['dob'],
            data['state_id'],
            data['city_id'],
            category_id,
            artist_id
        ))'''
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Profile updated successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/profile/update_with_media', methods=['POST'])
@login_required
def api_update_profile_with_media():
    try:
        data = request.form
        artist_id = session['artist_id']

        required_fields = ['first_name', 'last_name', 'phone', 'gender', 'dob',
                           'category', 'state_id', 'city_id']
        for field in required_fields:
            if field not in data or not data[field]:
                return jsonify({'success': False, 'error': f'{field.replace("_", " ").title()} is required'})

        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) as count FROM state_table WHERE state_id = %s", (data['state_id'],))
        state_exists = cur.fetchone()['count'] > 0

        cur.execute("SELECT COUNT(*) as count FROM city_table WHERE city_id = %s AND state_id = %s",
                    (data['city_id'], data['state_id']))
        city_exists = cur.fetchone()['count'] > 0

        if not state_exists:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid state selected'})
        if not city_exists:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid city selected'})

        category_id = resolve_category_id(cur, data.get('category'))
        if not category_id:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid category selected'})

        ensure_artist_schema(cur)
        cur.execute("SELECT portfolio_files, profile_pic FROM artist_table WHERE artist_id = %s", (artist_id,))
        artist_row = cur.fetchone() or {}
        existing_portfolio = parse_portfolio_paths(artist_row.get('portfolio_files'))

        removed_portfolio_indexes = set()
        removed_raw = data.get('removed_portfolio_indexes', '[]')
        try:
            removed_portfolio_indexes = {int(idx) for idx in json.loads(removed_raw)}
        except Exception:
            removed_portfolio_indexes = set()

        replacement_indices = [int(x) for x in request.form.getlist('replacement_indices') if str(x).strip().isdigit()]
        replacement_files = request.files.getlist('replacement_files')
        if len(replacement_indices) != len(replacement_files):
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid replacement portfolio payload'})

        replacement_map = {}
        for idx, file_obj in zip(replacement_indices, replacement_files):
            filename = file_obj.filename or ''
            if not is_allowed_portfolio_file(filename):
                cur.close()
                conn.close()
                return jsonify({'success': False, 'error': 'Only jpg, jpeg, png, mp4 files are allowed'})
            replacement_map[idx] = file_obj

        new_portfolio_files = request.files.getlist('portfolio_new_files')
        for file_obj in new_portfolio_files:
            filename = file_obj.filename or ''
            if not is_allowed_portfolio_file(filename):
                cur.close()
                conn.close()
                return jsonify({'success': False, 'error': 'Only jpg, jpeg, png, mp4 files are allowed'})

        os.makedirs(app.config['PORTFOLIO_UPLOAD_DIR'], exist_ok=True)

        updated_portfolio = []
        for idx, existing_path in enumerate(existing_portfolio):
            if idx in removed_portfolio_indexes:
                continue
            if idx in replacement_map:
                file_obj = replacement_map[idx]
                safe_name = secure_filename(file_obj.filename or 'portfolio_file')
                ext = safe_name.rsplit('.', 1)[1].lower()
                stored_name = f"{artist_id}_{uuid.uuid4().hex}.{ext}"
                abs_path = os.path.join(app.config['PORTFOLIO_UPLOAD_DIR'], stored_name)
                file_obj.save(abs_path)
                updated_portfolio.append('/static/uploads/portfolio/' + stored_name)
            else:
                updated_portfolio.append(existing_path)

        for file_obj in new_portfolio_files:
            if not file_obj or not file_obj.filename:
                continue
            safe_name = secure_filename(file_obj.filename)
            ext = safe_name.rsplit('.', 1)[1].lower()
            stored_name = f"{artist_id}_{uuid.uuid4().hex}.{ext}"
            abs_path = os.path.join(app.config['PORTFOLIO_UPLOAD_DIR'], stored_name)
            file_obj.save(abs_path)
            updated_portfolio.append('/static/uploads/portfolio/' + stored_name)

        # Minimum portfolio validation removed
        if len(updated_portfolio) > 10:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Maximum 10 media files allowed'})

        profile_picture_path = artist_row.get('profile_pic')
        profile_picture_file = request.files.get('profile_picture')
        if profile_picture_file and profile_picture_file.filename:
            if not is_allowed_profile_picture_file(profile_picture_file.filename):
                cur.close()
                conn.close()
                return jsonify({'success': False, 'error': 'Profile picture must be jpg, jpeg, or png'})

            os.makedirs(app.config['PROFILE_PICTURE_UPLOAD_DIR'], exist_ok=True)
            safe_name = secure_filename(profile_picture_file.filename)
            ext = safe_name.rsplit('.', 1)[1].lower()
            stored_name = f"{artist_id}_{uuid.uuid4().hex}.{ext}"
            abs_path = os.path.join(app.config['PROFILE_PICTURE_UPLOAD_DIR'], stored_name)
            profile_picture_file.save(abs_path)
            profile_picture_path = '/static/uploads/profile_pictures/' + stored_name

        query = """
            UPDATE artist_table 
            SET first_name = %s,
                last_name = %s,
                phone_number = %s,
                gender = %s,
                dob = %s,
                state_id = %s,
                city_id = %s,
                category_id = %s,
                experience_years = %s,
                price_per_hour = %s,
                portfolio_path = %s,
                portfolio_files = %s
        """

        '''query = """
            UPDATE artist_table 
            SET First_Name = %s,
                Last_Name = %s,
                Phone_Number = %s,
                Gender = %s,
                dob = %s,
                State_ID = %s,
                City_ID = %s,
                category_id = %s,
                portfolio_files = %s
        """'''
        params = [
            data['first_name'],
            data['last_name'],
            data['phone'],
            data['gender'],
            data['dob'],
            data['state_id'],
            data['city_id'],
            category_id,
            int(data.get('experience_years') or 0),
            float(data.get('price_per_hour') or 0),
            str(data.get('portfolio_path') or '').strip(),
            json.dumps(updated_portfolio)
        ]

        if profile_picture_path:
            query += ", profile_pic = %s"
            params.append(profile_picture_path)

        query += " WHERE artist_id = %s"
        params.append(artist_id)

        cur.execute(query, tuple(params))
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'message': 'Profile updated successfully',
            'portfolio_files': updated_portfolio,
            'profile_picture_path': profile_picture_path
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# 8. DASHBOARD
@app.route('/api/dashboard')
@login_required
def api_dashboard():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']
        auto_complete_bookings(artist_id)
        cleanup_expired_pending_bookings(cur, artist_id)
        conn.commit()

        ensure_artist_schema(cur)
        cur.execute("SHOW TABLES")
        table_rows = cur.fetchall() or []
        table_names = {str(list(r.values())[0]).lower() for r in table_rows if r}

        # Use known schema column names for artist_table directly
        cur.execute(
            """
            SELECT artist_id, first_name, last_name, username, profile_pic
            FROM artist_table
            WHERE artist_id = %s
            """,
            (artist_id,)
        )
        artist = cur.fetchone() or {}

        total_bookings = 0
        earnings = 0.0
        upcoming = []
        feedback = []

        if 'booking_table' in table_names:
            booking_cols = get_table_columns(cur, 'booking_table')
            booking_id_col = pick_column(booking_cols, ['booking_id'])
            booking_artist_col = pick_column(booking_cols, ['artist_id'])
            booking_date_col = pick_column(booking_cols, ['booking_date', 'slot_date', 'booked_at', 'created_at'])
            booking_time_col = pick_column(booking_cols, ['slot_time', 'start_time'])
            booking_status_col = pick_column(booking_cols, ['status', 'booking_status'])
            booking_type_col = pick_column(booking_cols, ['booking_type', 'slot_type', 'type', 'service_type'])
            booking_client_name_col = pick_column(booking_cols, ['client_name'])
            booking_client_id_col = pick_column(booking_cols, ['client_id'])
            booking_amount_col = pick_column(booking_cols, ['amount', 'payment_amount'])

            if booking_artist_col:
                cur.execute(
                    f"SELECT COUNT(*) AS total FROM booking_table WHERE `{booking_artist_col}` = %s",
                    (artist_id,)
                )
                total_bookings = int((cur.fetchone() or {}).get('total') or 0)

            # Earnings from payment_table with payment_status=success
            try:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(p.amount), 0) AS total_earnings
                    FROM payment_table p
                    JOIN booking_table b ON b.Booking_ID = p.booking_id
                    WHERE b.Artist_ID = %s AND p.payment_status = 'success'
                    """,
                    (artist_id,)
                )
                earnings = float((cur.fetchone() or {}).get('total_earnings') or 0.0)
            except Exception:
                earnings = 0.0

            # Fetch upcoming bookings using calendar_table join for precise datetime filtering
            cur.execute(
                """
                SELECT
                    b.Booking_ID AS booking_id,
                    cal.Slot_Date AS slot_date,
                    cal.Start_Time AS start_time,
                    cal.End_Time AS end_time,
                    cal.Slot_type AS slot_type,
                    b.Booking_Status AS booking_status,
                    b.Client_ID AS client_id,
                    c.first_name AS client_first_name,
                    c.last_name AS client_last_name
                FROM booking_table b
                LEFT JOIN calendar_table cal ON cal.Slot_ID = b.Slot_ID
                LEFT JOIN client_table c ON c.client_id = b.Client_ID
                WHERE b.Artist_ID = %s
                  AND LOWER(b.booking_status) = 'confirmed'
                  AND CONCAT(cal.Slot_Date, ' ', cal.End_Time) > NOW()
                ORDER BY cal.Slot_Date ASC, cal.Start_Time ASC
                LIMIT 10
                """,
                (artist_id,)
            )
            booking_rows = cur.fetchall() or []

            if booking_artist_col and booking_date_col:
                pass  # query already done above

                for row in booking_rows:
                    booking_date = row.get('slot_date')
                    if isinstance(booking_date, datetime):
                        booking_date = booking_date.date()
                    slot_date_str = booking_date.strftime('%d/%m/%Y') if booking_date else ''

                    def _parse_time(raw):
                        if hasattr(raw, 'total_seconds'):
                            t = int(raw.total_seconds())
                            return f"{t // 3600:02d}:{(t % 3600) // 60:02d}"
                        return str(raw or '00:00')[:5]

                    start_time = _parse_time(row.get('start_time'))
                    end_time   = _parse_time(row.get('end_time'))

                    raw_type = str(row.get('slot_type') or '').strip()
                    slot_type = raw_type if raw_type in ('Communication', 'Performance') else 'Performance'

                    status = str(row.get('booking_status') or 'confirmed').lower()

                    upcoming.append({
                        'Slot_Date': slot_date_str,
                        'first_name': row.get('client_first_name') or 'Client',
                        'last_name': row.get('client_last_name') or '',
                        'Start_Time': start_time,
                        'End_Time': end_time,
                        'Slot_Type': slot_type,
                        'Booking_Status': status
                    })

        # earnings already fetched from payment_table above

        if 'feedback_table' in table_names:
            cur.execute(
                """
                SELECT
                    f.rating,
                    f.comments,
                    CONCAT(COALESCE(c.first_name, ''), ' ', COALESCE(c.last_name, '')) AS client_name
                FROM feedback_table f
                JOIN client_table c ON c.client_id = f.client_id
                WHERE f.artist_id = %s
                ORDER BY f.feedback_id DESC
                LIMIT 5
                """,
                (artist_id,)
            )
            rows = cur.fetchall() or []
            for row in rows:
                client_name = str(row.get('client_name') or 'Client').strip() or 'Client'
                name_parts = client_name.split()
                first_name = name_parts[0] if name_parts else 'Client'
                last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''
                feedback.append({
                    'first_name': first_name,
                    'last_name': last_name,
                    'Rating': int(float(row.get('rating') or 0)),
                    'Comments': row.get('comments') or '',
                    'Created_At': datetime.now().isoformat()
                })

        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'artist': artist,
            'stats': {
                'total_bookings': total_bookings,
                'earnings': earnings
            },
            'upcoming_bookings': upcoming,
            'recent_feedback': feedback
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# 9. CHANGE PASSWORD

'''@app.route('/api/change_password', methods=['POST'])
@login_required
def api_change_password():
    conn = None
    cur = None
    try:
        data = request.get_json() or {}

        artist_id = session['artist_id']
        current_password = str(data.get('current_password') or data.get('old_password') or '')
        new_password = str(data.get('new_password') or '')
        confirm_password = str(data.get('confirm_password') or '')
        if not current_password or not new_password or not confirm_password:
            flash('Current password, new password and confirm password are required', 'error')
            return jsonify({'success': False, 'error': 'Current password, new password and confirm password are required'}), 400
        if new_password != confirm_password:
            flash('New password and confirm password must match', 'error')
            return jsonify({'success': False, 'error': 'New password and confirm password must match'}), 400
        if len(new_password) < 8:
            flash('New password must be at least 8 characters long', 'error')
            return jsonify({'success': False, 'error': 'New password must be at least 8 characters long'}), 400

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT password FROM artist_table WHERE artist_id=%s",
            (artist_id,)
        )

        artist = cur.fetchone()

        if not artist:
            flash('Artist not found', 'error')
            return jsonify({'success': False, 'error': 'Artist not found'})

        stored_password = str(artist.get('password') or '')
        if current_password != stored_password:
            flash('Incorrect current password', 'error')
            return jsonify({'success': False, 'error': 'Incorrect current password'}), 400

        cur.execute(
            "UPDATE artist_table SET password=%s WHERE artist_id=%s",
            (new_password, artist_id)
        )

        conn.commit()
        flash('Password updated successfully', 'success')

        return jsonify({'success': True, 'message': 'Password updated successfully'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()'''

# 10. SUBSCRIPTION
@app.route('/api/subscription')
@login_required
def api_subscription():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        # Current active subscription only — JOIN plan table for price.
        cur.execute(
            """
            SELECT
                s.*,
                p.plan_name,
                p.amount AS price,
                p.duration_days
            FROM subscription_table s
            JOIN subscription_plan_table p ON s.plan_id = p.plan_id
            WHERE s.artist_id = %s AND s.status = 'active'
            ORDER BY s.subscription_id DESC
            LIMIT 1
            """,
            (artist_id,)
        )
        active_row = cur.fetchone()

        current_plan = None
        if active_row:
            active_plan_name = str(active_row.get('plan_name') or 'Basic').strip()
            plan_type_val = active_plan_name.lower()
            duration_days = int(active_row.get('duration_days') or 30)
            billing_cycle = 'yearly' if duration_days >= 365 else 'monthly'
            start_date = active_row.get('start_date')
            end_date = active_row.get('end_date')
            current_plan = {
                'subscription_id': active_row.get('subscription_id'),
                'plan_id': active_row.get('plan_id'),
                'plan_name': active_plan_name,
                'plan_type': plan_type_val,
                'billing_cycle': billing_cycle,
                'next_billing_date': end_date.isoformat() if end_date else None,
                'amount': float(active_row.get('price') or 0),
                'status': 'active',
                'start_date': start_date.isoformat() if start_date else None,
                'end_date': end_date.isoformat() if end_date else None
            }

        # Full history — JOIN plan table for correct amount & plan name
        cur.execute(
            """
            SELECT
                s.*,
                p.plan_name,
                p.amount AS price,
                p.duration_days
            FROM subscription_table s
            JOIN subscription_plan_table p ON s.plan_id = p.plan_id
            WHERE s.artist_id = %s
            ORDER BY s.subscription_id DESC
            """,
            (artist_id,)
        )
        subscription_rows = cur.fetchall() or []

        billing_history = []
        for row in subscription_rows:
            start_date = row.get('start_date')
            status = str(row.get('status') or 'inactive').lower()
            plan_name = str(row.get('plan_name') or 'Subscription').strip()
            amount = float(row.get('price') or 0)
            billing_history.append({
                'date': start_date.isoformat() if start_date else None,
                'description': f'{plan_name} subscription',
                'amount': amount,
                'payment_method': 'Razorpay' if amount > 0 else 'Free Trial',
                'status': status if status in ('active', 'inactive', 'expired', 'cancelled') else 'inactive'
            })

        plans = []
        duration_labels = {30: '1 Month', 90: '3 Months', 180: '6 Months'}
        for _, p in SUBSCRIPTION_PLANS.items():
            days = int(p.get('duration_days') or 30)
            plans.append({
                'Plan_ID': p.get('plan_id'),
                'plan_id': p.get('plan_id'),
                'Plan_Name': p.get('plan_name'),
                'plan_name': p.get('plan_name'),
                'Plan_Type': p.get('plan_type'),
                'plan_type': p.get('plan_type'),
                'Amount': float(p.get('amount') or 0),
                'amount': float(p.get('amount') or 0),
                'Duration_Days': days,
                'duration_days': days,
                'duration_label': duration_labels.get(days, f'{days} Days'),
                'Features': list(p.get('features') or []),
                'features': list(p.get('features') or [])
            })

        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'plans': plans,
            'subscription': current_plan,
            'current_plan': current_plan,
            'billing_history': billing_history
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/create_order', methods=['POST'])
@app.route('/api/subscription/create_order', methods=['POST'])
@app.route('/subscription/create-order', methods=['POST'])
@login_required
def api_subscription_create_order():
    try:
        data = request.get_json() or {}
        plan = None
        raw_plan_type = str(data.get('plan_type') or '').strip().lower()
        raw_plan_id = str(data.get('plan_id') or '').strip()
        if raw_plan_type:
            plan = get_plan_definition(raw_plan_type)
        if not plan and raw_plan_id.isdigit():
            for p in SUBSCRIPTION_PLANS.values():
                if int(p.get('plan_id') or 0) == int(raw_plan_id):
                    plan = p
                    break
        if not plan:
            return jsonify({'success': False, 'error': 'Invalid plan selected'}), 400

        key_id = app.config.get('RAZORPAY_KEY_ID', '').strip()
        key_secret = app.config.get('RAZORPAY_KEY_SECRET', '').strip()
        if not key_id or not key_secret:
            return jsonify({'success': False, 'error': 'Payment gateway is not configured'}), 500

        amount_paise = int(float(plan['amount']) * 100)
        receipt = f"sub_{session['artist_id']}_{int(datetime.now().timestamp())}"
        payload = {
            'amount': amount_paise,
            'currency': 'INR',
            'receipt': receipt,
            'notes': {
                'artist_id': str(session['artist_id']),
                'plan_id': str(plan['plan_id'])
            }
        }

        resp = requests.post(
            'https://api.razorpay.com/v1/orders',
            auth=(key_id, key_secret),
            json=payload,
            timeout=20
        )
        if resp.status_code >= 400:
            return jsonify({'success': False, 'error': 'Failed to initialize payment'}), 500

        order = resp.json()
        return jsonify({
            'success': True,
            'order_id': order.get('id'),
            'amount': amount_paise,
            'currency': 'INR',
            'key_id': key_id,
            'plan_name': plan['plan_name'],
            'plan_type': plan['plan_type'],
            'plan_id': plan['plan_id']
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/payment/create-order/<int:subscription_id>', methods=['POST'])
@app.route('/subscription/create-order/<int:subscription_id>', methods=['POST'])
@login_required
def api_payment_create_order(subscription_id):
    conn = None
    cur = None
    try:
        artist_id = session['artist_id']
        key_id = current_app.config.get('RAZORPAY_KEY_ID', '').strip()
        key_secret = current_app.config.get('RAZORPAY_KEY_SECRET', '').strip()
        if not key_id or not key_secret:
            return jsonify({'error': 'Payment gateway is not configured'}), 500

        conn = get_db()
        cur = conn.cursor()
        assert cur is not None  # help type-checker: cursor is valid here

        # Use plans table (Basic/Premium/Pro), not subscription_table.
        plan_row = None
        for plan_table in ('plans_table', 'subscription_plan_table'):
            try:
                pcols = get_table_columns(cur, plan_table)
            except Exception:
                pcols = []
            if not pcols:
                continue
            p_id_col = pick_column(pcols, ['plan_id', 'id', 'subscription_id'])
            p_price_col = pick_column(pcols, ['price', 'amount'])
            if not p_id_col or not p_price_col:
                continue
            try:
                cur.execute(
                    f"SELECT `{p_price_col}` AS price FROM {plan_table} WHERE `{p_id_col}` = %s LIMIT 1",
                    (subscription_id,)
                )
                found = cur.fetchone()
                if found:
                    plan_row = found
                    break
            except Exception:
                continue

        if plan_row is None:
            return jsonify({'error': 'Subscription plan not found'}), 404

        price_value = float(plan_row.get('price') or 0)
        if price_value <= 0:
            return jsonify({'error': 'Invalid subscription amount'}), 400

        amount_paise = int(price_value * 100)
        receipt = f"sub_{artist_id}_{int(datetime.now().timestamp())}"
        payload = {
            'amount': amount_paise,
            'currency': 'INR',
            'receipt': receipt,
            'notes': {
                'artist_id': str(artist_id),
                'subscription_id': str(subscription_id)
            }
        }

        resp = requests.post(
            'https://api.razorpay.com/v1/orders',
            auth=(key_id, key_secret),
            json=payload,
            timeout=20
        )
        if resp.status_code >= 400:
            return jsonify({'error': 'Failed to initialize payment'}), 500

        order = resp.json()
        return jsonify({
            'key': key_id,
            'order_id': order.get('id'),
            'amount': amount_paise,
            'currency': 'INR'
        })
    except Exception as e:
        return jsonify({'error': str(e)})
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/subscription/cancel', methods=['POST'])
@app.route('/subscription/cancel', methods=['POST'])
@login_required
def api_cancel_subscription():
    conn = None
    cur = None
    try:
        artist_id = session['artist_id']
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE subscription_table
            SET status = 'cancelled'
            WHERE artist_id = %s
              AND LOWER(status) = 'active'
            """,
            (artist_id,)
        )
        affected = cur.rowcount
        conn.commit()
        return jsonify({
            'success': True,
            'message': 'Subscription cancelled successfully' if affected else 'No active subscription found'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/subscription/verify_payment', methods=['POST'])
@app.route('/subscription/payment-success', methods=['POST'])
@login_required
def api_subscription_verify_payment():
    try:
        data = request.get_json() or {}
        plan = None
        raw_plan_type = str(data.get('plan_type') or '').strip().lower()
        raw_plan_id = str(data.get('plan_id') or '').strip()
        if raw_plan_type:
            plan = get_plan_definition(raw_plan_type)
        if not plan and raw_plan_id.isdigit():
            for p in SUBSCRIPTION_PLANS.values():
                if int(p.get('plan_id') or 0) == int(raw_plan_id):
                    plan = p
                    break

        order_id = data.get('razorpay_order_id')
        payment_id = data.get('razorpay_payment_id')
        signature = data.get('razorpay_signature')

        if not order_id or not payment_id or not signature:
            return jsonify({'success': False, 'error': 'Missing payment verification fields'}), 400

        key_secret = app.config.get('RAZORPAY_KEY_SECRET', '').strip()
        key_id = app.config.get('RAZORPAY_KEY_ID', '').strip()
        if not key_id or not key_secret:
            return jsonify({'success': False, 'error': 'Payment gateway is not configured'}), 500

        if not plan and order_id:
            try:
                order_resp = requests.get(
                    f'https://api.razorpay.com/v1/orders/{order_id}',
                    auth=(key_id, key_secret),
                    timeout=20
                )
                if order_resp.status_code < 400:
                    order_data = order_resp.json()
                    notes = order_data.get('notes') or {}
                    noted_plan_id = str(notes.get('plan_id') or '').strip()
                    if noted_plan_id.isdigit():
                        for p in SUBSCRIPTION_PLANS.values():
                            if int(p.get('plan_id') or 0) == int(noted_plan_id):
                                plan = p
                                break
            except Exception:
                pass

        if not plan:
            return jsonify({'success': False, 'error': 'Invalid plan selected'}), 400

        signed_payload = f"{order_id}|{payment_id}"
        expected_signature = hmac.new(
            key_secret.encode('utf-8'),
            signed_payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()  # hmac.new() is valid in Python's hmac module

        if not hmac.compare_digest(expected_signature, signature):
            return jsonify({'success': False, 'error': 'Payment verification failed'}), 400

        # Verify payment status with Razorpay before activation.
        payment_resp = requests.get(
            f'https://api.razorpay.com/v1/payments/{payment_id}',
            auth=(key_id, key_secret),
            timeout=20
        )
        if payment_resp.status_code >= 400:
            return jsonify({'success': False, 'error': 'Unable to verify payment with gateway'}), 500

        payment_data = payment_resp.json()
        if payment_data.get('status') not in ('captured', 'authorized'):
            return jsonify({'success': False, 'error': 'Payment not captured'}), 400

        conn = get_db()
        cur = conn.cursor()
        activate_paid_subscription(cur, session['artist_id'], plan, payment_id, order_id)
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({'success': True, 'message': 'Payment Successful'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/subscription/verify-payment', methods=['POST'])
@login_required
def api_subscription_verify_payment_simple():
    return jsonify({'success': True, 'message': 'Payment verified'})

# 11. CALENDAR
@app.route('/api/calendar')
@login_required
def api_calendar():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']
        cleanup_expired_pending_bookings(cur, artist_id)
        conn.commit()

        ensure_artist_schema(cur)
        ensure_calendar_schema(cur)

        artist_cols = get_table_columns(cur, 'artist_table')
        artist_id_col = pick_column(artist_cols, ['Artist_ID', 'artist_id'])
        start_time_col = pick_column(artist_cols, ['working_start_time'])
        end_time_col = pick_column(artist_cols, ['working_end_time'])

        # One slot = one display row (dedup by Slot_ID via latest active booking per slot).
        cur.execute(
            """
            SELECT DISTINCT
                c.Slot_ID AS slot_id,
                c.Slot_Date AS slot_date,
                c.Start_Time AS start_time,
                c.End_Time AS end_time,
                c.Slot_type AS slot_type,
                c.price AS price,
                c.Status AS calendar_status,
                b.Booking_ID AS booking_id,
                b.Booking_Status AS booking_status,
                b.Client_ID AS client_id,
                b.reschedule_status AS reschedule_status,
                cl.first_name AS client_first_name,
                cl.last_name AS client_last_name,
                (
                    SELECT MAX(p.amount)
                    FROM payment_table p
                    WHERE p.booking_id = b.Booking_ID
                      AND p.payment_status = 'success'
                ) AS paid_amount
            FROM calendar_table c
            LEFT JOIN (
                SELECT b1.Booking_ID, b1.Slot_ID, b1.Booking_Status, b1.Client_ID, b1.reschedule_status
                FROM booking_table b1
                INNER JOIN (
                    SELECT Slot_ID, MAX(Booking_ID) AS booking_id
                    FROM booking_table
                    WHERE LOWER(Booking_Status) = 'confirmed'
                    GROUP BY Slot_ID
                ) bx ON bx.booking_id = b1.Booking_ID
            ) b ON c.Slot_ID = b.Slot_ID
            LEFT JOIN client_table cl ON cl.client_id = b.Client_ID
            WHERE c.Artist_ID = %s
            ORDER BY c.Slot_Date ASC, c.Start_Time ASC
            """,
            (artist_id,)
        )
        rows = cur.fetchall() or []

        def normalize_slot_type(raw_type):
            t = str(raw_type or '').strip().lower()
            if 'comm' in t:
                return 'Communication'
            return 'Performance'

        def normalize_booking_status(raw_status, raw_reschedule_status):
            status = str(raw_status or 'confirmed').strip().lower()
            if str(raw_reschedule_status or '').strip().lower() == 'requested':
                return 'reschedule'
            if status in ('confirmed', 'completed', 'cancelled', 'reschedule'):
                return status
            if status in ('canceled',):
                return 'cancelled'
            if status in ('rescheduled', 'reschedule requested', 'reschedule_request'):
                return 'reschedule'
            return 'confirmed'

        events = []
        for row in rows:
            slot_date = row.get('slot_date')
            if isinstance(slot_date, datetime):
                slot_date = slot_date.date()
            if isinstance(slot_date, str):
                try:
                    slot_date = datetime.strptime(slot_date[:10], '%Y-%m-%d').date()
                except Exception:
                    continue
            if not slot_date:
                continue

            start_hhmm = _time_to_hhmm(row.get('start_time'), '09:00')
            end_hhmm = _time_to_hhmm(row.get('end_time'), '10:00')
            line1 = f"{_fmt_ampm(start_hhmm)} - {_fmt_ampm(end_hhmm)}"
            slot_type = normalize_slot_type(row.get('slot_type'))
            booking_id = row.get('booking_id')
            has_booking = booking_id is not None

            status = 'Available'
            event_type = 'slot'
            if has_booking:
                event_type = 'booking'
                status = normalize_booking_status(row.get('booking_status'), row.get('reschedule_status'))
            else:
                cal_status = str(row.get('calendar_status') or 'Available').strip().lower()
                if cal_status in ('blocked', 'booked'):
                    status = 'confirmed'
                elif cal_status == 'cancelled':
                    status = 'cancelled'
                else:
                    status = 'available'

            client_name = f"{row.get('client_first_name') or ''} {row.get('client_last_name') or ''}".strip()
            events.append({
                'id': f"slot_{row.get('slot_id')}",
                'slot_id': row.get('slot_id'),
                'booking_id': booking_id,
                'start': f"{slot_date}T{start_hhmm}:00",
                'end': f"{slot_date}T{end_hhmm}:00",
                'title': line1,
                'line1': line1,
                'line2': slot_type,
                'type': event_type,
                'status': status,
                'slot_type': slot_type,
                'booking_type': slot_type,
                'client_id': row.get('client_id'),
                'client_name': client_name,
                'price': float(row.get('paid_amount') or row.get('price') or 0),
                'amount': float(row.get('paid_amount') or 0),
                'slot_date_display': _fmt_date_ddmmyyyy(slot_date)
            })

        # ---- Availability (working hours) ----
        availability = {'start_time': '09:00', 'end_time': '18:00'}
        if artist_id_col and start_time_col and end_time_col:
            cur.execute(
                f"SELECT `{start_time_col}` AS start_time, `{end_time_col}` AS end_time FROM artist_table WHERE `{artist_id_col}` = %s",
                (artist_id,)
            )
            wh_row = cur.fetchone() or {}
            if wh_row.get('start_time'):
                availability['start_time'] = wh_row.get('start_time')
            if wh_row.get('end_time'):
                availability['end_time'] = wh_row.get('end_time')

        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'events': events,
            'availability': availability
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# DELETE SLOT (available slots only)
@app.route('/api/delete_slot/<int:slot_id>', methods=['DELETE'])
@login_required
def api_delete_slot(slot_id):
    """Delete a calendar slot only if it belongs to the artist and status is 'Available'."""
    conn = None
    cur = None
    try:
        artist_id = session['artist_id']
        conn = get_db()
        cur = conn.cursor()

        # Verify slot exists, belongs to artist, and is Available
        cur.execute(
            """
            SELECT slot_id, status
            FROM calendar_table
            WHERE slot_id = %s AND artist_id = %s
            """,
            (slot_id, artist_id)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Slot not found'}), 404

        slot_status = str(row.get('status') or '').strip().lower()
        if slot_status != 'available':
            return jsonify({'success': False, 'error': f'Cannot delete slot with status: {row.get("status")}. Only available slots can be deleted.'}), 400

        cur.execute(
            "DELETE FROM calendar_table WHERE slot_id = %s AND artist_id = %s AND LOWER(status) = 'available'",
            (slot_id, artist_id)
        )
        conn.commit()

        return jsonify({'success': True, 'message': 'Slot deleted successfully'})

    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception('Delete slot failed')
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()



@app.route('/api/availability', methods=['POST'])
@login_required
def api_availability():
    try:
        data = request.get_json() or {}
        start_time = str(data.get('start_time') or '').strip()
        end_time = str(data.get('end_time') or '').strip()
        slot_type = str(data.get('slot_type') or 'Performance').strip()
        price = data.get('price')
        apply_to = str(data.get('apply_to') or 'selected_date').strip()
        selected_date = str(data.get('selected_date') or '').strip()

        if not start_time or not end_time:
            return jsonify({'success': False, 'error': 'Start and end time are required'}), 400

        datetime.strptime(start_time, '%H:%M')
        datetime.strptime(end_time, '%H:%M')

        if start_time >= end_time:
            return jsonify({'success': False, 'error': 'Start time must be before end time'}), 400

        if slot_type not in ('Communication', 'Performance'):
            slot_type = 'Performance'

        if price is None:
            return jsonify({'success': False, 'error': 'Price is required'}), 400
        price = float(price)

        # Price rules differ by slot type (Task 2 / Task 7)
        if slot_type == 'Communication':
            if price < 100:
                return jsonify({'success': False, 'error': 'Communication slot minimum price is ₹100'}), 400
            if price > 500:
                return jsonify({'success': False, 'error': 'Communication slot maximum price is ₹500'}), 400
        # Performance price is recalculated from artist.price_per_hour below;
        # the frontend-sent value is accepted as a fallback only.

        # Determine dates to generate slots for
        target_dates = []
        if apply_to == 'this_week':
            today = datetime.now().date()
            weekday = today.weekday()  # Monday=0
            monday = today - timedelta(days=weekday)
            for i in range(7):
                d = monday + timedelta(days=i)
                if d >= today:
                    target_dates.append(d)
        else:
            # selected_date
            if not selected_date:
                return jsonify({'success': False, 'error': 'Please select a date on the calendar'}), 400
            try:
                parsed = datetime.strptime(selected_date, '%Y-%m-%d').date()
            except ValueError:
                return jsonify({'success': False, 'error': 'Invalid date format'}), 400
            # Task 4: backend past-date guard (catches bypassed frontend)
            from datetime import date as _date
            if parsed < _date.today():
                return jsonify({'success': False, 'error': 'Cannot create slot for past dates.'}), 400
            target_dates.append(parsed)

        conn = get_db()
        cur = conn.cursor()
        ensure_artist_schema(cur)
        ensure_calendar_schema(cur)

        artist_id = session['artist_id']

        # Save working hours to artist_table
        artist_cols = get_table_columns(cur, 'artist_table')
        artist_id_col = pick_column(artist_cols, ['Artist_ID', 'artist_id'])
        wh_start_col = pick_column(artist_cols, ['working_start_time'])
        wh_end_col = pick_column(artist_cols, ['working_end_time'])

        if artist_id_col and wh_start_col and wh_end_col:
            cur.execute(
                f"UPDATE artist_table SET `{wh_start_col}` = %s, `{wh_end_col}` = %s WHERE `{artist_id_col}` = %s",
                (start_time, end_time, artist_id)
            )

        # Detect calendar_table columns
        cal_cols = get_table_columns(cur, 'calendar_table')
        cal_artist_col = pick_column(cal_cols, ['Artist_ID', 'artist_id'])
        cal_date_col = pick_column(cal_cols, ['Slot_Date', 'slot_date'])
        cal_start_col = pick_column(cal_cols, ['Start_Time', 'start_time'])
        cal_end_col = pick_column(cal_cols, ['End_Time', 'end_time'])
        cal_status_col = pick_column(cal_cols, ['Status', 'status'])
        cal_slot_type_col = pick_column(cal_cols, ['Slot_type', 'slot_type'])
        cal_price_col = pick_column(cal_cols, ['price'])

        if not cal_artist_col or not cal_date_col or not cal_start_col or not cal_end_col or not cal_status_col:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Calendar table is missing required columns'}), 500

        # ── Slot generation — behaviour differs by slot_type (Tasks 1–4 / 7) ─────
        # Communication → split time range into multiple 1-hour rows, same price each.
        # Performance   → ONE row for the full range; price = duration × price_per_hour.

        def _insert_slot(target_date, s_start, s_end, s_price):
            """Insert one slot row; skips silently when an identical slot already exists."""
            s_dt = datetime.strptime(s_start, '%H:%M')
            cur.execute(
                f"""
                SELECT COUNT(*) AS cnt FROM calendar_table
                WHERE `{cal_artist_col}` = %s
                  AND `{cal_date_col}` = %s
                  AND HOUR(`{cal_start_col}`) = %s
                  AND MINUTE(`{cal_start_col}`) = %s
                """,
                (artist_id, target_date, s_dt.hour, s_dt.minute)
            )
            if (cur.fetchone() or {}).get('cnt', 0) > 0:
                return 0  # already exists

            i_cols = [f"`{cal_artist_col}`", f"`{cal_date_col}`",
                      f"`{cal_start_col}`", f"`{cal_end_col}`",
                      f"`{cal_status_col}`"]
            i_vals = [artist_id, target_date, s_start, s_end, 'Available']
            i_ph   = ['%s'] * 5

            if cal_slot_type_col:
                i_cols.append(f"`{cal_slot_type_col}`"); i_vals.append(slot_type); i_ph.append('%s')
            if cal_price_col:
                i_cols.append(f"`{cal_price_col}`"); i_vals.append(s_price); i_ph.append('%s')

            cur.execute(
                f"INSERT INTO calendar_table ({', '.join(i_cols)}) VALUES ({', '.join(i_ph)})",
                tuple(i_vals)
            )
            return 1

        slots_created = 0
        for target_date in target_dates:
            start_dt = datetime.strptime(start_time, '%H:%M')
            end_dt   = datetime.strptime(end_time,   '%H:%M')

            if slot_type == 'Communication':
                # Task 1: split into 1-hour slots, all with the same artist-chosen price
                current = start_dt
                while current + timedelta(hours=1) <= end_dt:
                    slots_created += _insert_slot(
                        target_date,
                        current.strftime('%H:%M'),
                        (current + timedelta(hours=1)).strftime('%H:%M'),
                        price
                    )
                    current += timedelta(hours=1)

            else:
                # Task 3: Performance — ONE slot covering the full range
                # Task 4: price = duration_hours × price_per_hour from artist_table
                try:
                    cur.execute(
                        "SELECT price_per_hour FROM artist_table WHERE artist_id = %s LIMIT 1",
                        (artist_id,)
                    )
                    pph_row = cur.fetchone() or {}
                    price_per_hour = float(pph_row.get('price_per_hour') or 0)
                except Exception:
                    price_per_hour = 0.0

                duration_hours   = (end_dt - start_dt).seconds / 3600
                calculated_price = (duration_hours * price_per_hour) if price_per_hour > 0 else price
                slots_created   += _insert_slot(target_date, start_time, end_time, calculated_price)

        conn.commit()
        cur.close()
        conn.close()

        date_label = ', '.join(str(d) for d in target_dates)
        return jsonify({
            'success': True,
            'message': f'{slots_created} slot(s) created for {date_label}'
        })
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid time format'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ===== AUTO COMPLETE BOOKINGS =====
def auto_complete_bookings(artist_id):
    try:
        conn = get_db()
        cur = conn.cursor()

        # Mark confirmed bookings as completed if slot_date + end_time < NOW()
        # Uses calendar_table join for precise datetime comparison
        cur.execute("""
            UPDATE booking_table b
            JOIN calendar_table cal ON cal.Slot_ID = b.Slot_ID
            SET b.booking_status = 'completed'
            WHERE b.artist_id = %s
              AND b.booking_status = 'confirmed'
              AND CONCAT(cal.Slot_Date, ' ', cal.End_Time) < NOW()
        """, (artist_id,))

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("Auto completion error:", e)

# 12. BOOKINGS
@app.route('/api/bookings')
@login_required
def api_bookings():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']
        filter_booking_id = request.args.get('booking_id')  # Optional: filter single booking
        auto_complete_bookings(artist_id)
        cleanup_expired_pending_bookings(cur, artist_id)
        conn.commit()
        booking_cols = get_table_columns(cur, 'booking_table')
        client_cols = get_table_columns(cur, 'client_table')
        payment_cols = get_table_columns(cur, 'payment_table')
        has_cancelled_by = 'cancelled_by' in {str(c).lower() for c in booking_cols}
        has_cancelled_at = 'cancelled_at' in {str(c).lower() for c in booking_cols}
        client_email_col = pick_column(client_cols, ['email'])
        client_username_col = pick_column(client_cols, ['username', 'user_name', 'email'])
        payment_amount_col = pick_column(payment_cols, ['amount'])
        payment_status_col = pick_column(payment_cols, ['payment_status', 'status'])
        payment_refund_col = pick_column(payment_cols, ['refund_amount'])
        if client_email_col:
            client_email_expr = f"c.`{client_email_col}`"
        elif client_username_col:
            client_email_expr = f"c.`{client_username_col}`"
        else:
            client_email_expr = "''"
        client_username_expr = f"c.`{client_username_col}`" if client_username_col else client_email_expr
        if payment_amount_col and payment_status_col:
            paid_amount_expr = f"MAX(CASE WHEN p.`{payment_status_col}` = 'success' THEN p.`{payment_amount_col}` ELSE NULL END) AS paid_amount"
        elif payment_amount_col:
            paid_amount_expr = f"MAX(p.`{payment_amount_col}`) AS paid_amount"
        else:
            paid_amount_expr = "NULL AS paid_amount"
        if payment_refund_col:
            refund_amount_expr = f"MAX(COALESCE(p.`{payment_refund_col}`, 0)) AS refund_amount"
        else:
            refund_amount_expr = "0 AS refund_amount"

        cancelled_by_select = "b.cancelled_by AS cancelled_by," if has_cancelled_by else "NULL AS cancelled_by,"
        cancelled_at_select = "b.cancelled_at AS cancelled_at," if has_cancelled_at else "NULL AS cancelled_at,"
        
        cur.execute(
    f"""
    SELECT
        b.Booking_ID,
        b.Client_ID,
        b.Artist_ID,  -- booking_table uses PascalCase
        b.Slot_ID,
        b.Booking_Status,
        b.reschedule_status,
        b.reschedule_reason,
        b.was_rescheduled,
        {cancelled_by_select}
        {cancelled_at_select}
        c.first_name AS client_first_name,
        c.last_name  AS client_last_name,
        {client_email_expr} AS client_email,
        c.phone_number AS client_phone,
        {client_username_expr} AS client_username,
        ci.city_name AS client_city_name,
        s.state_name AS client_state_name,
        cal.Slot_Date,
        cal.Start_Time,
        cal.End_Time,
        cal.Slot_type,
        cal.price,
        {paid_amount_expr},
        {refund_amount_expr}
    FROM booking_table b
    LEFT JOIN client_table c ON c.client_id = b.Client_ID
    LEFT JOIN city_table ci ON ci.city_id = c.city_id
    LEFT JOIN state_table s ON s.state_id = c.state_id
    LEFT JOIN calendar_table cal ON cal.Slot_ID = b.Slot_ID
    LEFT JOIN payment_table p ON p.booking_id = b.Booking_ID
    WHERE b.Artist_ID = %s
    {"AND b.Booking_ID = %s" if filter_booking_id else ""}
    GROUP BY
        b.Booking_ID, b.Client_ID, b.Artist_ID, b.Slot_ID, b.Booking_Status, b.reschedule_status, b.reschedule_reason, b.was_rescheduled,
        c.first_name, c.last_name, {client_email_expr}, c.phone_number, {client_username_expr}, ci.city_name, s.state_name, cal.Slot_Date, cal.Start_Time, cal.End_Time, cal.Slot_type, cal.price
    ORDER BY cal.Slot_Date DESC, cal.Start_Time DESC, b.Booking_ID DESC
    """,
    (artist_id, filter_booking_id) if filter_booking_id else (artist_id,)
)


        rows = cur.fetchall() or []

        bookings = []
        for row in rows:
            booking_date = row.get('Slot_Date')
            start_time = str(row.get('Start_Time') or '09:00')[:5]
            end_time = str(row.get('End_Time') or '10:00')[:5]
            start_dt, end_dt = _booking_start_end_dt(booking_date, start_time, end_time)
            client_first = str(row.get('client_first_name') or '').strip()
            client_last = str(row.get('client_last_name') or '').strip()
            client_name = f"{client_first} {client_last}".strip() or 'Client'
            booking_id = row.get('Booking_ID')
            booking_type = str(row.get('Slot_type') or 'Performance')
            if booking_type not in ('Communication', 'Performance'):
                booking_type = 'Performance'
            days_ahead = None
            if isinstance(booking_date, datetime):
                days_ahead = (booking_date.date() - datetime.now().date()).days
            elif hasattr(booking_date, 'year') and hasattr(booking_date, 'month') and hasattr(booking_date, 'day'):
                days_ahead = (booking_date - datetime.now().date()).days
            elif isinstance(booking_date, str):
                try:
                    days_ahead = (datetime.strptime(booking_date[:10], '%Y-%m-%d').date() - datetime.now().date()).days
                except Exception:
                    days_ahead = None
            bookings.append({
                'id': str(booking_id) if booking_id is not None else f"{booking_date}_{start_time}",
                'booking_reference': f"BK{int(booking_id):04d}" if booking_id is not None and str(booking_id).isdigit() else f"BK-{booking_date}",
                'client_name': client_name,
                'client_id': row.get('Client_ID'),
                'client_first_name': client_first or 'Client',
                'client_last_name': client_last,
                'client_email': row.get('client_email') or '',
                'client_phone': row.get('client_phone') or '',
                'client_username': row.get('client_username') or '',
                'client_city_name': row.get('client_city_name') or '',
                'client_state_name': row.get('client_state_name') or '',
                'slot_type': booking_type,
                'service_type': booking_type,
                'booking_type': booking_type,
                'date_time': f"{booking_date}T{start_time}:00" if booking_date else None,
                'end_datetime': f"{booking_date}T{end_time}:00" if booking_date else None,
                'slot_date': str(booking_date) if booking_date else None,
                'slot_date_display': _fmt_date_ddmmyyyy(booking_date),
                'start_time': start_time,
                'end_time': end_time,
                'status': str(row.get('Booking_Status') or '').lower() or 'confirmed',
                'reschedule_status': row.get('reschedule_status'),
                'reschedule_reason': row.get('reschedule_reason') or '',
                'was_rescheduled': int(row.get('was_rescheduled') or 0),
                'amount': float(row.get('paid_amount') or 0),
                'refund_amount': float(row.get('refund_amount') or 0),
                'can_cancel': bool(
                    str(row.get('Booking_Status') or '').lower() == 'confirmed'
                    and start_dt and datetime.now() < start_dt
                ),
                'can_reschedule': bool(
                    str(row.get('Booking_Status') or '').lower() in ('confirmed', 'upcoming')
                ),
                'cancelled_by': row.get('cancelled_by'),
                'cancelled_at': row.get('cancelled_at').isoformat() if row.get('cancelled_at') else None
            })

        cur.close()
        conn.close()
        return jsonify({'success': True, 'bookings': bookings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bookings/<int:booking_id>/emergency_cancel', methods=['POST'])
@login_required
def api_emergency_cancel_booking(booking_id):
    try:
        data = request.get_json() or {}
        action = str(data.get('action') or 'cancel').strip().lower()
        cancelled_by = str(data.get('cancelled_by') or 'artist').strip().lower()
        if cancelled_by not in ('artist', 'client'):
            cancelled_by = 'artist'
        new_slot_id = data.get('new_slot_id')

        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        booking_cols = get_table_columns(cur, 'booking_table')
        booking_id_col = pick_column(booking_cols, ['booking_id'])
        booking_artist_col = pick_column(booking_cols, ['artist_id'])
        booking_status_col = pick_column(booking_cols, ['Booking_Status', 'booking_status', 'status'])
        booking_slot_col = pick_column(booking_cols, ['Slot_ID', 'slot_id'])
        booking_new_slot_col = pick_column(booking_cols, ['new_slot_id'])
        cancelled_by_col = pick_column(booking_cols, ['cancelled_by'])
        has_reschedule_status = 'reschedule_status' in {str(c).lower() for c in booking_cols}
        has_was_rescheduled = 'was_rescheduled' in {str(c).lower() for c in booking_cols}

        if not booking_id_col or not booking_artist_col or not booking_status_col:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Booking table is missing required columns'}), 500

        select_parts = [
            f"`{booking_id_col}` AS booking_id",
            f"`{booking_status_col}` AS booking_status",
            "reschedule_status AS reschedule_status" if 'reschedule_status' in {str(c).lower() for c in booking_cols} else "NULL AS reschedule_status",
            "reschedule_reason AS reschedule_reason" if 'reschedule_reason' in {str(c).lower() for c in booking_cols} else "NULL AS reschedule_reason",
            "Client_ID AS client_id"
        ]
        if booking_slot_col:
            select_parts.append(f"`{booking_slot_col}` AS slot_id")
        if booking_new_slot_col:
            select_parts.append(f"`{booking_new_slot_col}` AS new_slot_id")

        cur.execute(
            f"""
            SELECT {', '.join(select_parts)}
            FROM booking_table
            WHERE `{booking_id_col}` = %s AND `{booking_artist_col}` = %s
            """,
            (booking_id, artist_id)
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Booking not found'}), 404

        cur.execute(
            """
            SELECT c.first_name, c.last_name, cal.Slot_Date, cal.Start_Time, cal.End_Time, cal.Slot_type
            FROM booking_table b
            LEFT JOIN client_table c ON c.client_id = b.Client_ID
            LEFT JOIN calendar_table cal ON cal.Slot_ID = b.Slot_ID
            WHERE b.Booking_ID = %s AND b.Artist_ID = %s
            """,
            (booking_id, artist_id)
        )
        detail_row = cur.fetchone() or {}

        # ---------- Client initiated reschedule flow ----------
        # Client selects an Available slot. Booking_Status -> 'reschedule',
        # new_slot_id -> selected slot. Do NOT change Slot_ID. Do NOT modify calendar_table.
        if action == 'reschedule':
            if not new_slot_id:
                cur.close()
                conn.close()
                return jsonify({'success': False, 'error': 'new_slot_id is required for reschedule'}), 400

            # Verify the selected slot is Available
            calendar_cols = get_table_columns(cur, 'calendar_table')
            calendar_slot_col = pick_column(calendar_cols, ['Slot_ID', 'slot_id'])
            calendar_status_col = pick_column(calendar_cols, ['Status', 'status'])
            if calendar_slot_col and calendar_status_col:
                cur.execute(
                    f"SELECT `{calendar_status_col}` AS slot_status FROM calendar_table WHERE `{calendar_slot_col}` = %s",
                    (new_slot_id,)
                )
                slot_row = cur.fetchone()
                if not slot_row or str(slot_row.get('slot_status', '')).strip() != 'Available':
                    cur.close()
                    conn.close()
                    return jsonify({'success': False, 'error': 'Selected slot is not Available'}), 400

            update_parts = [f"`{booking_status_col}` = %s"]
            update_vals = ['reschedule']
            if booking_new_slot_col:
                update_parts.append(f"`{booking_new_slot_col}` = %s")
                update_vals.append(new_slot_id)
            if has_reschedule_status:
                update_parts.append("reschedule_status = %s")
                update_vals.append('requested')
            if cancelled_by_col:
                update_parts.append(f"`{cancelled_by_col}` = %s")
                update_vals.append('client')
            update_vals.extend([booking_id, artist_id])

            cur.execute(
                f"""
                UPDATE booking_table
                SET {', '.join(update_parts)}
                WHERE `{booking_id_col}` = %s AND `{booking_artist_col}` = %s
                """,
                tuple(update_vals)
            )
            client_name = f"{detail_row.get('first_name') or ''} {detail_row.get('last_name') or ''}".strip() or 'Client'
            slot_type = str(detail_row.get('Slot_type') or 'Performance')
            add_artist_notification(
                cur,
                artist_id,
                "Reschedule Request",
                f"Client {client_name} requested to reschedule the {slot_type.lower()} booking on {_fmt_date_ddmmyyyy(detail_row.get('Slot_Date'))} from {_fmt_ampm(_time_to_hhmm(detail_row.get('Start_Time')))} to {_fmt_ampm(_time_to_hhmm(detail_row.get('End_Time')))}."
            )
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({'success': True, 'message': 'Booking marked for reschedule'})

        # ---------- Artist accepts reschedule ----------
        # Retrieve old_slot_id (current Slot_ID) and new_slot_id from booking.
        # Update: Slot_ID = new_slot_id, new_slot_id = NULL, Booking_Status = 'confirmed'
        # Calendar: old_slot_id -> 'Available', new_slot_id -> 'Blocked'
        if action == 'accept_reschedule':
            old_slot_id = row.get('slot_id')
            resolved_new_slot_id = row.get('new_slot_id') or new_slot_id

            calendar_cols = get_table_columns(cur, 'calendar_table')
            calendar_slot_col = pick_column(calendar_cols, ['Slot_ID', 'slot_id'])
            calendar_status_col = pick_column(calendar_cols, ['Status', 'status'])

            update_parts = [f"`{booking_status_col}` = %s"]
            update_vals = ['confirmed']
            if booking_slot_col and resolved_new_slot_id:
                update_parts.append(f"`{booking_slot_col}` = %s")
                update_vals.append(resolved_new_slot_id)
            if booking_new_slot_col:
                update_parts.append(f"`{booking_new_slot_col}` = NULL")
            if has_reschedule_status:
                update_parts.append("reschedule_status = 'approved'")
            if has_was_rescheduled:
                update_parts.append("was_rescheduled = 1")
            update_vals.extend([booking_id, artist_id])

            cur.execute(
                f"""
                UPDATE booking_table
                SET {', '.join(update_parts)}
                WHERE `{booking_id_col}` = %s AND `{booking_artist_col}` = %s
                """,
                tuple(update_vals)
            )

            # Update calendar_table: old slot -> Available, new slot -> Blocked
            if calendar_slot_col and calendar_status_col:
                if old_slot_id is not None:
                    cur.execute(
                        f"UPDATE calendar_table SET `{calendar_status_col}` = 'Available' WHERE `{calendar_slot_col}` = %s",
                        (old_slot_id,)
                    )
                if resolved_new_slot_id:
                    cur.execute(
                        f"UPDATE calendar_table SET `{calendar_status_col}` = 'Blocked' WHERE `{calendar_slot_col}` = %s",
                        (resolved_new_slot_id,)
                    )

            client_name = f"{detail_row.get('first_name') or ''} {detail_row.get('last_name') or ''}".strip() or 'Client'
            add_artist_notification(
                cur,
                artist_id,
                "Reschedule Approved",
                f"Your reschedule request for the booking on {_fmt_date_ddmmyyyy(detail_row.get('Slot_Date'))} has been approved."
            )

            conn.commit()
            cur.close()
            conn.close()
            return jsonify({'success': True, 'message': 'Reschedule accepted and booking confirmed'})

        # ---------- Artist rejects reschedule ----------
        # new_slot_id = NULL, Booking_Status = 'confirmed'
        # Do NOT modify Slot_ID. Do NOT modify calendar_table.
        if action == 'reject_reschedule':
            update_parts = [f"`{booking_status_col}` = %s"]
            update_vals = ['confirmed']
            if booking_new_slot_col:
                update_parts.append(f"`{booking_new_slot_col}` = NULL")
            if has_reschedule_status:
                update_parts.append("reschedule_status = 'rejected'")
            update_vals.extend([booking_id, artist_id])

            cur.execute(
                f"""
                UPDATE booking_table
                SET {', '.join(update_parts)}
                WHERE `{booking_id_col}` = %s AND `{booking_artist_col}` = %s
                """,
                tuple(update_vals)
            )
            client_name = f"{detail_row.get('first_name') or ''} {detail_row.get('last_name') or ''}".strip() or 'Client'
            add_artist_notification(
                cur,
                artist_id,
                "Reschedule Rejected",
                f"Your reschedule request for the booking on {_fmt_date_ddmmyyyy(detail_row.get('Slot_Date'))} has been rejected."
            )
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({'success': True, 'message': 'Reschedule rejected, booking remains confirmed'})

        # ---------- Default: Emergency cancel ----------
        old_slot_id = row.get('slot_id')
        if cancelled_by_col:
            cur.execute(
                f"""
                UPDATE booking_table
                SET `{booking_status_col}` = %s, `{cancelled_by_col}` = %s
                WHERE `{booking_id_col}` = %s AND `{booking_artist_col}` = %s
                """,
                ('cancelled', cancelled_by, booking_id, artist_id)
            )
        else:
            cur.execute(
                f"""
                UPDATE booking_table
                SET `{booking_status_col}` = %s
                WHERE `{booking_id_col}` = %s AND `{booking_artist_col}` = %s
                """,
                ('cancelled', booking_id, artist_id)
            )

        calendar_cols = get_table_columns(cur, 'calendar_table')
        calendar_slot_col = pick_column(calendar_cols, ['Slot_ID', 'slot_id'])
        calendar_artist_col = pick_column(calendar_cols, ['Artist_ID', 'artist_id'])
        calendar_status_col = pick_column(calendar_cols, ['Status', 'status'])
        if old_slot_id is not None and calendar_slot_col:
            if cancelled_by == 'artist' and calendar_artist_col:
                cur.execute(
                    f"DELETE FROM calendar_table WHERE `{calendar_slot_col}` = %s AND `{calendar_artist_col}` = %s",
                    (old_slot_id, artist_id)
                )
            elif calendar_status_col:
                cur.execute(
                    f"UPDATE calendar_table SET `{calendar_status_col}` = 'Available' WHERE `{calendar_slot_col}` = %s",
                    (old_slot_id,)
                )

        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Booking cancelled in emergency flow'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ===== RESCHEDULE REQUEST (Artist requests, Client picks new slot) =====
@app.route('/booking/reschedule-request', methods=['POST'])
@login_required
def api_reschedule_request():
    """Artist requests to reschedule a booking.
    Sets reschedule_requested=1, reschedule_status='pending'.
    Artist does NOT select a slot; client will pick a new slot later.
    """
    conn = None
    cur = None
    try:
        data = request.get_json(silent=True) or {}
        booking_id = data.get('booking_id')
        reason = str(data.get('reason') or '').strip()

        if not booking_id:
            return jsonify({'success': False, 'error': 'booking_id is required'}), 400

        artist_id = session['artist_id']
        conn = get_db()
        cur = conn.cursor()

        # Verify booking belongs to this artist and status allows reschedule
        cur.execute(
            """
            SELECT b.Booking_ID, b.Booking_Status, b.Client_ID,
                   c.first_name AS client_first_name, c.last_name AS client_last_name,
                   cal.Slot_Date, cal.Start_Time, cal.End_Time, cal.Slot_type
            FROM booking_table b
            LEFT JOIN client_table c ON c.client_id = b.Client_ID
            LEFT JOIN calendar_table cal ON cal.Slot_ID = b.Slot_ID
            WHERE b.Booking_ID = %s AND b.Artist_ID = %s
            """,
            (booking_id, artist_id)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Booking not found'}), 404

        current_status = str(row.get('Booking_Status') or '').lower()
        if current_status not in ('confirmed', 'upcoming'):
            return jsonify({'success': False, 'error': f'Cannot reschedule a booking with status: {current_status}'}), 400

        # Update booking: set reschedule_requested=1, reschedule_status='pending'
        booking_cols = get_table_columns(cur, 'booking_table')
        booking_col_set = {str(c).lower() for c in booking_cols}

        update_parts = ["reschedule_status = 'pending'"]
        update_vals = []

        if 'reschedule_requested' in booking_col_set:
            update_parts.append("reschedule_requested = 1")

        if reason and 'reschedule_reason' in booking_col_set:
            update_parts.append("reschedule_reason = %s")
            update_vals.append(reason)

        update_vals.extend([booking_id, artist_id])

        cur.execute(
            f"""
            UPDATE booking_table
            SET {', '.join(update_parts)}
            WHERE Booking_ID = %s AND Artist_ID = %s
            """,
            tuple(update_vals)
        )

        # Insert notification for the client (stored as artist notification for now)
        client_name = f"{row.get('client_first_name') or ''} {row.get('client_last_name') or ''}".strip() or 'Client'
        slot_type = str(row.get('Slot_type') or 'Performance')
        slot_date_display = _fmt_date_ddmmyyyy(row.get('Slot_Date'))

        add_artist_notification(
            cur,
            artist_id,
            "Reschedule Requested",
            f"Artist has requested to reschedule your booking on {slot_date_display}. Client will select a new slot."
        )

        # If client notification table exists, insert there too
        try:
            client_id = row.get('Client_ID')
            if client_id:
                notif_cols = get_table_columns(cur, 'notification_table')
                notif_col_set = {str(c).lower() for c in notif_cols}
                has_client_id = 'client_id' in notif_col_set
                if has_client_id:
                    n_cols = ["`client_id`", "`message`"]
                    n_vals = [client_id, "Artist has requested to reschedule your booking"]
                    n_ph = ["%s", "%s"]
                    if 'title' in notif_col_set:
                        n_cols.append("`title`")
                        n_vals.append("Reschedule Request")
                        n_ph.append("%s")
                    if 'recipient_type' in notif_col_set:
                        n_cols.append("`recipient_type`")
                        n_vals.append("client")
                        n_ph.append("%s")
                    if 'is_read' in notif_col_set:
                        n_cols.append("`is_read`")
                        n_vals.append(0)
                        n_ph.append("%s")
                    cur.execute(
                        f"INSERT INTO notification_table ({', '.join(n_cols)}) VALUES ({', '.join(n_ph)})",
                        tuple(n_vals)
                    )
        except Exception:
            pass  # Client notification is best-effort

        conn.commit()
        return jsonify({'success': True, 'message': 'Reschedule request sent. Client will select a new slot.'})

    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception('Reschedule request failed')
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# 13. EARNINGS
@app.route('/api/earnings')
@login_required
def api_earnings():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        # Bank details come from artist_bank_details (separate table)
        cur.execute(
            """
            SELECT
                abd.bank_name,
                abd.account_number,
                abd.account_holder_name,
                abd.ifsc_code,
                abd.upi_id
            FROM artist_table a
            LEFT JOIN artist_bank_details abd ON a.artist_id = abd.artist_id
            WHERE a.artist_id = %s
            LIMIT 1
            """,
            (artist_id,)
        )
        bank_row = cur.fetchone() or {}
        bank_details = {
            'bank_name': bank_row.get('bank_name') or '',
            'account_number': bank_row.get('account_number') or '',
            'account_holder': bank_row.get('account_holder_name') or '',
            'ifsc_code': bank_row.get('ifsc_code') or '',
            'upi_id': bank_row.get('upi_id') or ''
        }

        transactions = []
        total_paid = 0.0
        total_refunded = 0.0

        # Include ALL payments — do NOT filter out cancelled/refunded
        cur.execute(
            """
            SELECT
                p.payment_id,
                p.amount,
                p.refund_amount,
                p.paid_at,
                p.payment_status,
                p.payment_method,
                b.booking_id AS Booking_ID,
                b.booking_status AS Booking_Status,
                b.client_id AS Client_ID,
                c.first_name AS client_first_name,
                c.last_name AS client_last_name,
                cal.slot_type AS Slot_type,
                cal.slot_date AS Slot_Date,
                cal.start_time AS Start_Time,
                cal.end_time AS End_Time,
                DATE_FORMAT(cal.slot_date, '%%d/%%m/%%Y') AS Slot_Date_Display
            FROM payment_table p
            JOIN booking_table b ON b.booking_id = p.booking_id
            JOIN calendar_table cal ON cal.slot_id = b.slot_id
            LEFT JOIN client_table c ON c.client_id = b.client_id
            WHERE b.artist_id = %s
            ORDER BY p.paid_at DESC
            """,
            (artist_id,)
        )
        rows = cur.fetchall() or []

        for row in rows:
            amount = float(row.get('amount') or 0)
            refund_amount = float(row.get('refund_amount') or 0)
            payment_status = str(row.get('payment_status') or '').lower()
            booking_status = str(row.get('Booking_Status') or '').lower()

            # Determine display status and label
            if refund_amount > 0:
                display_status = 'refunded'
                status_label = f"Refunded \u20b9{refund_amount:,.0f}"
            elif payment_status == 'success':
                display_status = 'completed'
                status_label = f"Paid \u20b9{amount:,.0f}"
            else:
                display_status = 'pending'
                status_label = 'Pending'

            # Accumulate totals
            if payment_status == 'success':
                total_paid += amount
            if refund_amount > 0:
                total_refunded += refund_amount

            client_name = f"{row.get('client_first_name') or ''} {row.get('client_last_name') or ''}".strip() or 'Client'
            slot_type = str(row.get('Slot_type') or 'Performance')
            if slot_type not in ('Communication', 'Performance'):
                slot_type = 'Performance'
            slot_date = row.get('Slot_Date')
            date_display = str(row.get('Slot_Date_Display') or _fmt_date_ddmmyyyy(slot_date))
            date_iso = f"{slot_date}T00:00:00" if slot_date else datetime.now().isoformat()

            transactions.append({
                'date': date_display,
                'date_iso': date_iso,
                'date_display': date_display,
                'client_name': client_name,
                'booking_type': slot_type,
                'booking_reference': (
                    f"BK{int(str(row.get('Booking_ID'))):04d}"
                    if str(row.get('Booking_ID') or '').isdigit() else '-'
                ),
                'amount': amount,
                'refund_amount': refund_amount,
                'status': display_status,
                'status_label': status_label,
                'booking_status': booking_status,
                'payment_id': str(row.get('payment_id') or '-'),
                'payment_method': row.get('payment_method') or '-',
                'reschedule_status': None
            })

        # Net earnings = total paid minus refunded
        net_earnings = total_paid - total_refunded

        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'stats': {
                'total': net_earnings,
                'available': net_earnings,
                'total_paid': total_paid,
                'total_refunded': total_refunded
            },
            'transactions': transactions,
            'bank_details': bank_details
        })
    except Exception as e:
        logger.exception("Earnings fetch failed")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/artist/bank-details', methods=['GET', 'POST'])
@login_required
def api_artist_bank_details():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        if request.method == 'GET':
            cur.execute(
                """
                SELECT bank_name, account_number, account_holder_name, ifsc_code, upi_id
                FROM artist_bank_details
                WHERE artist_id = %s
                LIMIT 1
                """,
                (artist_id,)
            )
            row = cur.fetchone() or {}
            return jsonify({
                'success': True,
                'bank_details': {
                    'bank_name': row.get('bank_name', ''),
                    'account_number': row.get('account_number', ''),
                    'account_holder': row.get('account_holder_name', ''),
                    'ifsc_code': row.get('ifsc_code', ''),
                    'upi_id': row.get('upi_id', '')
                }
            })

        # POST — upsert bank details in artist_bank_details
        data = request.get_json() or {}
        cur.execute(
            """
            INSERT INTO artist_bank_details
                (artist_id, bank_name, account_holder_name, account_number, ifsc_code, upi_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                bank_name           = VALUES(bank_name),
                account_holder_name = VALUES(account_holder_name),
                account_number      = VALUES(account_number),
                ifsc_code           = VALUES(ifsc_code),
                upi_id              = VALUES(upi_id)
            """,
            (
                artist_id,
                str(data.get('bank_name', '')).strip(),
                str(data.get('account_holder', '')).strip(),
                str(data.get('account_number', '')).strip(),
                str(data.get('ifsc_code', '')).strip(),
                str(data.get('upi_id', '')).strip()
            )
        )
        conn.commit()
        return jsonify({'success': True, 'message': 'Bank details saved'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@app.route('/api/earnings/bank_details', methods=['POST'])
@login_required
def api_earnings_bank_details():
    return api_artist_bank_details()


@app.route('/api/artist/favorites/count')
@login_required
def api_artist_favorites_count():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS total
            FROM favorite_table
            WHERE artist_id = %s
            """,
            (session['artist_id'],)
        )
        row = cur.fetchone() or {}
        return jsonify({'success': True, 'count': int(row.get('total') or 0)})
    except Exception as e:
        logger.exception("Favorites count failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# 14. NOTIFICATIONS

@app.route('/api/notifications/count')
@login_required
def api_notifications_count():
    """Returns the number of unread notifications for the logged-in artist."""
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']
        cur.execute(
            """
            SELECT COUNT(*) AS unread
            FROM notification_table
            WHERE artist_id = %s
              AND is_read = 0
            """,
            (artist_id,)
        )
        row = cur.fetchone() or {}
        unread = int(row.get('unread') or 0)
        cur.close()
        conn.close()
        return jsonify({'success': True, 'unread': unread, 'count': unread})
    except Exception as e:
        return jsonify({'success': False, 'unread': 0, 'count': 0, 'error': str(e)})


@app.route('/api/notifications')
@login_required
def api_notifications():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        notifications = []

        cur.execute(
            """
            SELECT
                n.notification_id,
                n.title,
                n.message,
                n.is_read,
                n.created_at,
                n.booking_id,
                c.first_name,
                c.last_name,
                b.booking_id AS b_booking_id,
                cal.slot_type,
                cal.slot_date,
                cal.start_time,
                cal.end_time
            FROM notification_table n
            LEFT JOIN booking_table b ON n.booking_id = b.booking_id
            LEFT JOIN client_table c ON b.client_id = c.client_id
            LEFT JOIN calendar_table cal ON b.slot_id = cal.slot_id
            WHERE n.recipient_type = 'artist'
              AND n.artist_id = %s
            ORDER BY n.created_at DESC
            LIMIT 100
            """,
            (artist_id,)
        )
        rows = cur.fetchall() or []

        def _detect_notification_type(title, message):
            t = (str(title or '') + ' ' + str(message or '')).lower()
            if any(k in t for k in ['booking', 'reschedule', 'cancelled', 'canceled', 'confirmed', 'completed']):
                return 'booking'
            if any(k in t for k in ['payment', 'earning', 'payout', 'refund']):
                return 'payment'
            if any(k in t for k in ['subscription', 'plan', 'trial', 'activated']):
                return 'subscription'
            if any(k in t for k in ['feedback', 'review', 'rating']):
                return 'feedback'
            if any(k in t for k in ['reminder']):
                return 'reminder'
            return 'system'

        for row in rows:
            ntitle = row.get('title') or 'Notification'
            nmessage = row.get('message') or 'You have a new update.'
            nstatus = ''
            msg_lower = nmessage.lower()
            if 'cancelled' in msg_lower or 'canceled' in msg_lower:
                nstatus = 'cancelled'
            elif 'confirmed' in msg_lower:
                nstatus = 'confirmed'
            elif 'reschedule' in msg_lower:
                nstatus = 'reschedule'
            elif 'completed' in msg_lower:
                nstatus = 'completed'

            # Build formatted message with client name and slot details
            client_first = str(row.get('first_name') or '').strip()
            client_last = str(row.get('last_name') or '').strip()
            client_name = f"{client_first} {client_last}".strip()
            booking_id_val = row.get('booking_id') or row.get('b_booking_id')

            # If we have client and slot info from the JOIN, build a rich message
            slot_type = str(row.get('slot_type') or '').strip()
            slot_date_raw = row.get('slot_date')
            start_time_raw = row.get('start_time')
            end_time_raw = row.get('end_time')

            if client_name and slot_date_raw and slot_type:
                slot_date_display = _fmt_date_ddmmyyyy(slot_date_raw)
                start_ampm = _fmt_ampm(_time_to_hhmm(start_time_raw, '')) if start_time_raw else ''
                end_ampm = _fmt_ampm(_time_to_hhmm(end_time_raw, '')) if end_time_raw else ''
                time_range = f" from {start_ampm} to {end_ampm}" if start_ampm and end_ampm else ''

                if 'cancelled' in msg_lower or 'canceled' in msg_lower:
                    formatted_msg = f"Client {client_name} cancelled a {slot_type.lower()} slot on {slot_date_display}{time_range}"
                elif 'reschedule' in msg_lower:
                    formatted_msg = f"Client {client_name} requested to reschedule a {slot_type.lower()} slot on {slot_date_display}{time_range}"
                elif 'completed' in msg_lower:
                    formatted_msg = f"Booking with Client {client_name} for a {slot_type.lower()} slot on {slot_date_display}{time_range} has been completed"
                else:
                    formatted_msg = f"Client {client_name} booked a {slot_type.lower()} slot on {slot_date_display}{time_range}"
            else:
                formatted_msg = nmessage

            # Format created_at as DD/MM/YYYY HH:MM AM/PM
            created_at_raw = row.get('created_at')
            if created_at_raw:
                try:
                    if isinstance(created_at_raw, str):
                        created_at_dt = datetime.fromisoformat(created_at_raw)
                    else:
                        created_at_dt = created_at_raw
                    created_at_display = created_at_dt.strftime('%d/%m/%Y %I:%M %p')
                    created_at_iso = created_at_dt.isoformat()
                except Exception:
                    created_at_display = str(created_at_raw)
                    created_at_iso = datetime.now().isoformat()
            else:
                created_at_display = ''
                created_at_iso = datetime.now().isoformat()

            notifications.append({
                'id': str(row.get('notification_id')),
                'type': _detect_notification_type(ntitle, nmessage),
                'title': ntitle,
                'message': formatted_msg,
                'timestamp': created_at_iso,
                'created_at_display': created_at_display,
                'read': bool(row.get('is_read')),
                'status': nstatus,
                'client_name': client_name or '',
                'booking_id': str(booking_id_val) if booking_id_val else ''
            })

        if not notifications:
            cur.execute(
                """
                SELECT
                    b.booking_id AS booking_id,
                    b.booking_status AS booking_status,
                    c.first_name AS first_name,
                    c.last_name AS last_name,
                    cal.slot_type AS slot_type,
                    cal.slot_date AS slot_date,
                    cal.start_time AS start_time,
                    cal.end_time AS end_time
                FROM booking_table b
                LEFT JOIN client_table c ON c.client_id = b.client_id
                LEFT JOIN calendar_table cal ON cal.slot_id = b.slot_id
                WHERE b.artist_id = %s
                ORDER BY b.booking_id DESC
                LIMIT 50
                """,
                (artist_id,)
            )
            fallback_rows = cur.fetchall() or []
            for row in fallback_rows:
                client_name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or 'Client'
                status = str(row.get('booking_status') or 'confirmed').lower()
                slot_type = str(row.get('slot_type') or 'Performance')
                slot_date_raw = row.get('slot_date')
                slot_date_display = _fmt_date_ddmmyyyy(slot_date_raw) if slot_date_raw else ''
                start_time_raw = row.get('start_time')
                end_time_raw = row.get('end_time')
                start_ampm = _fmt_ampm(_time_to_hhmm(start_time_raw, '')) if start_time_raw else ''
                end_ampm = _fmt_ampm(_time_to_hhmm(end_time_raw, '')) if end_time_raw else ''
                time_range = f" from {start_ampm} to {end_ampm}" if start_ampm and end_ampm else ''

                title = f"{slot_type} Booking"
                if status == 'confirmed':
                    title = 'Booking Confirmed'
                    message = f"Client {client_name} booked a {slot_type.lower()} slot on {slot_date_display}{time_range}" if slot_date_display else f"{slot_type} booking confirmed with {client_name}"
                elif status == 'reschedule':
                    title = 'Reschedule Requested'
                    message = f"Client {client_name} requested to reschedule a {slot_type.lower()} slot on {slot_date_display}{time_range}" if slot_date_display else f"Reschedule requested for {slot_type.lower()} booking"
                elif status in ('cancelled', 'canceled'):
                    title = f"{slot_type} Cancelled"
                    message = f"Client {client_name} cancelled a {slot_type.lower()} slot on {slot_date_display}{time_range}" if slot_date_display else f"Client cancelled {slot_type.lower()} booking"
                elif status == 'completed':
                    title = 'Booking Completed'
                    message = f"Booking with Client {client_name} for a {slot_type.lower()} slot on {slot_date_display}{time_range} has been completed" if slot_date_display else f"{slot_type} booking completed with {client_name}"
                else:
                    message = f"{slot_type} booking update from {client_name}"

                notifications.append({
                    'id': f"booking_{row.get('booking_id')}",
                    'type': 'booking',
                    'title': title,
                    'message': message,
                    'timestamp': f"{slot_date_raw}T00:00:00" if slot_date_raw else datetime.now().isoformat(),
                    'created_at_display': _fmt_date_ddmmyyyy(slot_date_raw) if slot_date_raw else '',
                    'read': False,
                    'booking_id': str(row.get('booking_id')),
                    'status': status,
                    'client_name': client_name
                })

        cur.close()
        conn.close()

        return jsonify({'success': True, 'notifications': notifications})
    except Exception as e:
        logger.exception("Notifications fetch failed")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/notifications/mark_all_read', methods=['POST'])
@login_required
def api_notifications_mark_all_read():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        cur.execute("""
            UPDATE notification_table
            SET is_read = 1
            WHERE recipient_type = 'artist'
            AND artist_id = %s
        """, (artist_id,))

        affected = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({'success': True, 'message': f'{affected} notification(s) marked as read'})
    except Exception as e:
        logger.exception("Mark all read failed")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/client_profile')
@login_required
def api_client_profile():
    try:
        client_id = request.args.get('client_id')
        if not client_id:
            return jsonify({'success': False, 'error': 'client_id is required'}), 400

        conn = get_db()
        cur = conn.cursor()
        client_cols = get_table_columns(cur, 'client_table')
        client_email_col = pick_column(client_cols, ['email'])
        client_username_col = pick_column(client_cols, ['username', 'user_name', 'email'])
        if client_email_col:
            client_email_expr = f"c.`{client_email_col}`"
        elif client_username_col:
            client_email_expr = f"c.`{client_username_col}`"
        else:
            client_email_expr = "''"

        cur.execute(f"""
            SELECT
                c.client_id,
                c.first_name,
                c.last_name,
                {client_email_expr} AS email,
                c.phone_number,
                ci.city_name,
                s.state_name
            FROM client_table c
            LEFT JOIN city_table ci ON ci.city_id = c.city_id
            LEFT JOIN state_table s ON s.state_id = c.state_id
            WHERE c.client_id = %s
        """, (client_id,))

        client = cur.fetchone()
        cur.close()
        conn.close()

        if not client:
            return jsonify({'success': False, 'error': 'Client not found'}), 404

        city = client.get('city_name') or ''
        state = client.get('state_name') or ''
        location = f"{city}, {state}" if city and state else (city or state or '')

        return jsonify({
            'success': True,
            'client': {
                'client_id': client.get('client_id'),
                'first_name': client.get('first_name') or '',
                'last_name': client.get('last_name') or '',
                'email': client.get('email') or '',
                'phone_number': client.get('phone_number') or '',
                'city_name': city,
                'state_name': state,
                'location': location
            }
        })
    except Exception as e:
        logger.exception("Client profile fetch failed")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bookings/<int:booking_id>/popup_details')
@login_required
def api_booking_popup_details(booking_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']
        client_cols = get_table_columns(cur, 'client_table')
        client_email_col = pick_column(client_cols, ['email'])
        client_username_col = pick_column(client_cols, ['username', 'user_name', 'email'])
        if client_email_col:
            client_email_expr = f"c.`{client_email_col}`"
        elif client_username_col:
            client_email_expr = f"c.`{client_username_col}`"
        else:
            client_email_expr = "''"
        cur.execute(
            f"""
            SELECT
                b.Booking_ID AS booking_id,
                b.Client_ID AS client_id,
                b.Booking_Status AS booking_status,
                c.first_name AS first_name,
                c.last_name AS last_name,
                {client_email_expr} AS email,
                c.phone_number AS phone_number,
                ci.city_name AS city_name,
                s.state_name AS state_name,
                cal.Slot_Date AS slot_date,
                cal.Start_Time AS start_time,
                cal.End_Time AS end_time,
                cal.Slot_type AS slot_type,
                (
                    SELECT MAX(p.amount)
                    FROM payment_table p
                    WHERE p.booking_id = b.Booking_ID
                      AND p.payment_status = 'success'
                ) AS amount
            FROM booking_table b
            LEFT JOIN client_table c ON c.client_id = b.Client_ID
            LEFT JOIN city_table ci ON ci.city_id = c.city_id
            LEFT JOIN state_table s ON s.state_id = c.state_id
            LEFT JOIN calendar_table cal ON cal.Slot_ID = b.Slot_ID
            WHERE b.Booking_ID = %s
              AND b.Artist_ID = %s
            LIMIT 1
            """,
            (booking_id, artist_id)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return jsonify({'success': False, 'error': 'Booking not found'}), 404

        return jsonify({
            'success': True,
            'booking': {
                'booking_id': row.get('booking_id'),
                'client_id': row.get('client_id'),
                'client_name': f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or 'Client',
                'client_email': row.get('email') or '',
                'client_phone': row.get('phone_number') or '',
                'client_city_name': row.get('city_name') or '',
                'client_state_name': row.get('state_name') or '',
                'date': _fmt_date_ddmmyyyy(row.get('slot_date')),
                'start_time': _fmt_ampm(_time_to_hhmm(row.get('start_time'))),
                'end_time': _fmt_ampm(_time_to_hhmm(row.get('end_time'))),
                'slot_type': str(row.get('slot_type') or 'Performance'),
                'status': str(row.get('booking_status') or 'confirmed').lower(),
                'amount': float(row.get('amount') or 0)
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bookings/<int:booking_id>/cancel', methods=['POST'])
@login_required
def api_cancel_booking(booking_id):
    try:
        data = request.get_json() or {}
        reason = str(data.get('cancellation_reason') or '').strip()
        if not reason:
            return jsonify({'success': False, 'error': 'Cancellation reason is required'}), 400

        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        # Verify booking belongs to this artist
        cur.execute("""
            SELECT b.Booking_ID, b.Booking_Status, b.Slot_ID, b.was_rescheduled,
                   b.Client_ID, c.first_name, c.last_name,
                   cal.Slot_Date, cal.Start_Time, cal.End_Time, cal.Slot_type
            FROM booking_table b
            LEFT JOIN client_table c ON c.client_id = b.Client_ID
            LEFT JOIN calendar_table cal ON cal.Slot_ID = b.Slot_ID
            WHERE b.Booking_ID = %s AND b.Artist_ID = %s
        """, (booking_id, artist_id))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Booking not found'}), 404

        if str(row.get('Booking_Status') or '').lower() == 'cancelled':
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Booking is already cancelled'}), 400

        slot_date = row.get('Slot_Date')
        start_time = row.get('Start_Time')
        end_time = row.get('End_Time')
        start_dt, _ = _booking_start_end_dt(slot_date, start_time, end_time)
        if not start_dt:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid booking slot date/time'}), 400
        if datetime.now() >= start_dt:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Booking cannot be cancelled once slot has started'}), 400

        # Refund calculation by date difference.
        cur.execute("SELECT DATEDIFF(%s, CURDATE()) AS days_diff", (_to_date(slot_date),))
        days_diff = int((cur.fetchone() or {}).get('days_diff') or 0)
        if days_diff >= 7:
            refund_percent = 100
        elif days_diff >= 3:
            refund_percent = 50
        elif days_diff > 0:
            refund_percent = 15
        else:
            refund_percent = 0

        if int(row.get('was_rescheduled') or 0) == 1:
            refund_percent = min(refund_percent, 50)

        paid_amount = 0.0
        try:
            cur.execute(
                """
                SELECT amount
                FROM payment_table
                WHERE booking_id = %s
                  AND payment_status = 'success'
                ORDER BY payment_id DESC
                LIMIT 1
                """,
                (booking_id,)
            )
            pay_row = cur.fetchone() or {}
            paid_amount = float(pay_row.get('amount') or 0)
        except Exception:
            paid_amount = 0.0
        refund_amount = round((paid_amount * refund_percent) / 100.0, 2)

        cur.execute("""
            UPDATE booking_table
            SET booking_status = 'cancelled',
                cancelled_by = 'artist',
                cancelled_at = NOW(),
                cancellation_reason = %s
            WHERE Booking_ID = %s AND Artist_ID = %s
        """, (reason, booking_id, artist_id))

        # Artist cancellation flow: remove slot from calendar_table.
        cur.execute("""
            DELETE cal
            FROM calendar_table cal
            JOIN booking_table b ON b.Slot_ID = cal.Slot_ID
            WHERE b.Booking_ID = %s
              AND b.Artist_ID = %s
        """, (booking_id, artist_id))

        # Best-effort payment refund metadata/status update.
        try:
            pay_cols = get_table_columns(cur, 'payment_table')
            p_status_col = pick_column(pay_cols, ['payment_status', 'status'])
            p_refund_amt_col = pick_column(pay_cols, ['refund_amount'])
            p_refund_pct_col = pick_column(pay_cols, ['refund_percentage', 'refund_percent'])
            sets = []
            vals = []
            if p_status_col:
                sets.append(f"`{p_status_col}` = %s"); vals.append('refunded')
            if p_refund_amt_col:
                sets.append(f"`{p_refund_amt_col}` = %s"); vals.append(refund_amount)
            if p_refund_pct_col:
                sets.append(f"`{p_refund_pct_col}` = %s"); vals.append(refund_percent)
            if sets:
                vals.extend([booking_id])
                cur.execute(f"UPDATE payment_table SET {', '.join(sets)} WHERE booking_id = %s", tuple(vals))
        except Exception:
            pass

        client_name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or 'Client'
        slot_type = str(row.get('Slot_type') or 'Performance')
        add_artist_notification(
            cur,
            artist_id,
            "Booking Cancelled",
            f"Client {client_name} cancelled the {slot_type.lower()} booking scheduled on {_fmt_date_ddmmyyyy(slot_date)} from {_fmt_ampm(_time_to_hhmm(start_time))} to {_fmt_ampm(_time_to_hhmm(end_time))}."
        )

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'message': 'Booking cancelled successfully',
            'refund_percent': refund_percent,
            'refund_amount': refund_amount
        })
    except Exception as e:
        logger.exception("Booking cancellation failed")
        return jsonify({'success': False, 'error': str(e)})


'''@app.route('/api/bookings/<int:booking_id>/reschedule_request', methods=['POST'])
@login_required
def api_reschedule_request(booking_id):
    try:
        data = request.get_json() or {}
        reason = str(data.get('reschedule_reason') or '').strip()
        new_slot_id = data.get('new_slot_id')

        if not reason:
            return jsonify({'success': False, 'error': 'Reschedule reason is required'}), 400
        if not new_slot_id:
            return jsonify({'success': False, 'error': 'New slot selection is required'}), 400

        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']

        # Verify booking belongs to this artist
        cur.execute("""
            SELECT b.Booking_ID, b.Booking_Status, b.Client_ID, cal.Slot_Date, cal.Start_Time, cal.End_Time
            FROM booking_table
            b
            JOIN calendar_table cal ON cal.Slot_ID = b.Slot_ID
            WHERE b.Booking_ID = %s AND b.Artist_ID = %s
        """, (booking_id, artist_id))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Booking not found'}), 404

        if str(row.get('Booking_Status') or '').lower() != 'confirmed':
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Only confirmed bookings can be rescheduled'}), 400

        slot_date = row.get('Slot_Date')
        if not slot_date or (_to_date(slot_date) - datetime.now().date()).days < 3:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Reschedule is allowed only for bookings at least 3 days ahead'}), 400

        # Verify the selected slot is Available
        cur.execute("""
            SELECT Slot_ID, Status, Slot_Date, Start_Time, End_Time
            FROM calendar_table
            WHERE Slot_ID = %s AND Artist_ID = %s AND Status = 'Available'
        """, (new_slot_id, artist_id))
        slot = cur.fetchone()
        if not slot:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Selected slot is not available'}), 400
        if (_to_date(slot.get('Slot_Date')) - datetime.now().date()).days < 3:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Please select a slot at least 3 days ahead'}), 400

        cur.execute("""
            UPDATE booking_table
            SET Booking_Status = 'reschedule',
                reschedule_status = 'requested',
                reschedule_reason = %s,
                reschedule_requested_at = NOW(),
                rescheduled_to_slot_id = %s
            WHERE Booking_ID = %s AND Artist_ID = %s
        """, (reason, new_slot_id, booking_id, artist_id))

        # Free old slot and reserve selected new slot.
        cur.execute("""
            UPDATE calendar_table cal
            JOIN booking_table b ON b.Slot_ID = cal.Slot_ID
            SET cal.Status = 'Available'
            WHERE b.Booking_ID = %s
        """, (booking_id,))
        cur.execute("UPDATE calendar_table SET Status = 'Blocked' WHERE Slot_ID = %s AND Artist_ID = %s", (new_slot_id, artist_id))

        # Artist-initiated request: notify client via notification table when supported.
        old_date = _fmt_date_ddmmyyyy(row.get('Slot_Date'))
        old_start = _fmt_ampm(_time_to_hhmm(row.get('Start_Time')))
        old_end = _fmt_ampm(_time_to_hhmm(row.get('End_Time')))
        new_date = _fmt_date_ddmmyyyy(slot.get('Slot_Date'))
        new_start = _fmt_ampm(_time_to_hhmm(slot.get('Start_Time')))
        new_end = _fmt_ampm(_time_to_hhmm(slot.get('End_Time')))
        slot_type = str(slot.get('Slot_type') or 'Performance')
        msg = (
            f"Artist requested to reschedule the {slot_type.lower()} booking on {old_date} "
            f"from {old_start} to {old_end}."
        )
        try:
            notif_cols = get_table_columns(cur, 'notification_table')
            n_client_col = pick_column(notif_cols, ['client_id'])
            n_rec_type_col = pick_column(notif_cols, ['recipient_type'])
            n_title_col = pick_column(notif_cols, ['title'])
            n_message_col = pick_column(notif_cols, ['message'])
            n_is_read_col = pick_column(notif_cols, ['is_read'])
            if n_client_col and n_message_col:
                cols = [f"`{n_client_col}`", f"`{n_message_col}`"]
                vals = [row.get('Client_ID'), msg]
                ph = ['%s', '%s']
                if n_rec_type_col:
                    cols.append(f"`{n_rec_type_col}`"); vals.append('client'); ph.append('%s')
                if n_title_col:
                    cols.append(f"`{n_title_col}`"); vals.append('Reschedule Request'); ph.append('%s')
                if n_is_read_col:
                    cols.append(f"`{n_is_read_col}`"); vals.append(0); ph.append('%s')
                cur.execute(f"INSERT INTO notification_table ({', '.join(cols)}) VALUES ({', '.join(ph)})", tuple(vals))
        except Exception:
            pass

        add_artist_notification(
            cur,
            artist_id,
            "Reschedule Request",
            f"Reschedule request for the {slot_type.lower()} booking on {old_date} from {old_start} to {old_end} has been sent to the client."
        )

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({'success': True, 'message': 'Reschedule request submitted'})
    except Exception as e:
        logger.exception("Reschedule request failed")
        return jsonify({'success': False, 'error': str(e)})'''


@app.route('/api/available_slots')
@login_required
def api_available_slots():
    """Get available slots for the logged-in artist (for reschedule modal)"""
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']
        cleanup_expired_pending_bookings(cur, artist_id)
        conn.commit()

        cur.execute("""
            SELECT
                c.Slot_ID,
                c.Slot_Date,
                c.Start_Time,
                c.End_Time,
                c.Slot_type,
                c.price
            FROM calendar_table c
            WHERE c.Artist_ID = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM booking_table b
                  WHERE b.Slot_ID = c.Slot_ID
                    AND LOWER(b.Booking_Status) = 'confirmed'
              )
              AND DATEDIFF(Slot_Date, CURDATE()) >= 3
            ORDER BY c.Slot_Date ASC, c.Start_Time ASC
        """, (artist_id,))
        rows = cur.fetchall() or []

        slots = []
        for row in rows:
            slot_date = row.get('Slot_Date')
            if isinstance(slot_date, datetime):
                slot_date = slot_date.date()
            start_raw = row.get('Start_Time')
            end_raw = row.get('End_Time')
            if hasattr(start_raw, 'total_seconds'):
                total = int(start_raw.total_seconds())
                start_str = f"{total // 3600:02d}:{(total % 3600) // 60:02d}"
            else:
                start_str = str(start_raw or '')[:5]
            if hasattr(end_raw, 'total_seconds'):
                total = int(end_raw.total_seconds())
                end_str = f"{total // 3600:02d}:{(total % 3600) // 60:02d}"
            else:
                end_str = str(end_raw or '')[:5]

            slots.append({
                'slot_id': row.get('Slot_ID'),
                'slot_date': str(slot_date) if slot_date else '',
                'slot_date_display': _fmt_date_ddmmyyyy(slot_date),
                'start_time': start_str,
                'end_time': end_str,
                'slot_type': str(row.get('Slot_type') or 'Performance'),
                'price': float(row.get('price') or 0)
            })

        cur.close()
        conn.close()
        return jsonify({'success': True, 'slots': slots})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# 15. FEEDBACK
@app.route('/api/feedback')
@login_required
def api_feedback():
    try:
        conn = get_db()
        cur = conn.cursor()
        artist_id = session['artist_id']
        client_cols = get_table_columns(cur, 'client_table')
        client_email_col = pick_column(client_cols, ['email'])
        client_username_col = pick_column(client_cols, ['username', 'user_name'])
        if client_email_col:
            client_email_expr = f"c.`{client_email_col}`"
        elif client_username_col:
            client_email_expr = f"c.`{client_username_col}`"
        else:
            client_email_expr = "''"

        cur.execute(
            f"""
            SELECT
                f.feedback_id,
                f.client_id,
                f.rating,
                f.comments,
                c.first_name AS first_name,
                c.last_name AS last_name,
                {client_email_expr} AS client_email
            FROM feedback_table f
            JOIN client_table c ON c.client_id = f.client_id
            WHERE f.artist_id = %s
            ORDER BY f.feedback_id DESC
            """,
            (artist_id,)
        )
        rows = cur.fetchall() or []

        feedback = []
        total_rating = 0.0

        for row in rows:
            rating = float(row.get('rating') or 0)
            total_rating += rating
            client_name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or 'Client'
            initials = ''.join([part[0] for part in client_name.split()[:2]]).upper() or 'C'

            feedback.append({
                'id': str(row.get('feedback_id')) if row.get('feedback_id') is not None else '',
                'client_id': row.get('client_id'),
                'client_name': client_name,
                'client_initials': initials,
                'client_email': row.get('client_email') or '',
                'rating': rating,
                'message': row.get('comments') or '',
                'timestamp': datetime.now().isoformat()
            })

        total_reviews = len(feedback)
        avg_rating = round((total_rating / total_reviews), 1) if total_reviews > 0 else 0.0

        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'feedback': feedback,
            'stats': {'rating': avg_rating, 'total_reviews': total_reviews}
        })
    except Exception as e:
        logger.exception("Feedback fetch failed")
        return jsonify({'success': False, 'error': str(e)})

# ========== DEMO DATA INSERTION ==========
def ensure_demo_artists():
    """Ensure demo artists exist in the database"""
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Ensure category table has data
        cur.execute("SELECT COUNT(*) AS total FROM category_table")
        if int((cur.fetchone() or {}).get('total') or 0) == 0:
            cur.executemany(
                """
                INSERT INTO category_table (category_id, category_name)
                VALUES (%s, %s)
                """,
                [(1, 'Singer'), (2, 'Dancer'), (3, 'Photographer')]
            )

        demo_artists = [
            {
                'first_name': 'Rohan',
                'last_name': 'Sharma',
                'username': 'rohan@gmail.com',
                'email': 'rohan@gmail.com',
                'password': bcrypt.hashpw('Test@1234'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'),
                'gender': 'Male',
                'dob': '1995-05-15',
                'phone_number': '9876543210',
                'state_id': 11,  # Karnataka
                'city_id': 38,   # Bengaluru (auto-increment id from schema)
                'category_id': 1,
                'portfolio_path': 'portfolio1.pdf',
                'verification_status': 'approved',
                'is_enabled': 1,
                'experience_years': 6,
                'price_per_hour': 1200,
                'rating': 4.6
            },
            {
                'first_name': 'Priya',
                'last_name': 'Patel',
                'username': 'priya@gmail.com',
                'email': 'priya@gmail.com',
                'password': bcrypt.hashpw('Test@1234'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'),
                'gender': 'Female',
                'dob': '1998-08-22',
                'phone_number': '9876543211',
                'state_id': 7,   # Gujarat
                'city_id': 26,   # Ahmedabad (auto-increment id from schema)
                'category_id': 2,
                'portfolio_path': 'portfolio2.pdf',
                'verification_status': 'approved',
                'is_enabled': 1,
                'experience_years': 4,
                'price_per_hour': 1500,
                'rating': 4.8
            },
            {
                'first_name': 'Amit',
                'last_name': 'Verma',
                'username': 'amit@gmail.com',
                'email': 'amit@gmail.com',
                'password': bcrypt.hashpw('Test@1234'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'),
                'gender': 'Male',
                'dob': '1993-12-10',
                'phone_number': '9876543212',
                'state_id': 14,  # Maharashtra
                'city_id': 48,   # Pune (auto-increment id from schema)
                'category_id': 3,
                'portfolio_path': 'portfolio3.pdf',
                'verification_status': 'approved',
                'is_enabled': 1,
                'experience_years': 8,
                'price_per_hour': 1800,
                'rating': 4.7
            }
        ]

        for artist in demo_artists:
            # Check if artist exists using correct PascalCase column names
            cur.execute(
                """
                SELECT artist_id, password, verification_status, is_enabled
                FROM artist_table
                WHERE Username = %s
                LIMIT 1
                """,
                (artist['username'],)
            )
            existing = cur.fetchone()

            if not existing:
                # Insert new artist using correct PascalCase column names per schema
                cur.execute("""
                    INSERT INTO artist_table 
                    (first_name, last_name, username, email, password, gender, dob, phone_number,
                     state_id, city_id, category_id, portfolio_path, verification_status, is_enabled,
                     experience_years, price_per_hour, rating)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    artist['first_name'], artist['last_name'], artist['username'],
                    artist['email'], artist['password'], artist['gender'], artist['dob'], artist['phone_number'],
                    artist['state_id'], artist['city_id'], artist['category_id'],
                    artist['portfolio_path'], artist['verification_status'], artist['is_enabled'],
                    artist['experience_years'], artist['price_per_hour'], artist['rating']
                ))
            else:

                current_password = str(existing.get('password') or '')
                should_reset_password = False

# Check if password is bcrypt hash
                if current_password.startswith(('$2b$', '$2a$', '$2y$')):
                    try:
        # Just check if it's a valid hash (no need to compare with Test@1234)
                        bcrypt.hashpw(b"test", current_password.encode())
                    except Exception:
                        should_reset_password = True
                else:
                    should_reset_password = True

# If invalid → reset password
                if should_reset_password:
                    new_password = "Test@1234"
                    hashed_password = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()

                cur.execute(
                    "UPDATE artist_table SET password = %s WHERE artist_id = %s",
                    (hashed_password, existing['artist_id'])
                    )
                
                hashed = bcrypt.hashpw("Test@1234".encode(), bcrypt.gensalt()).decode()

                cur.execute(
                    "UPDATE artist_table SET password = %s",
                    (hashed,)
                )
                # Update existing artist if needed
                """current_password = str(existing.get('Password') or '')
                should_reset_password = False
                
                if current_password.startswith('$2b$') or current_password.startswith('$2a$') or current_password.startswith('$2y$'):
                    try:
                        if not bcrypt.checkpw('Test@1234'.encode('utf-8'), current_password.encode('utf-8')):
                            should_reset_password = True
                    except Exception:
                        should_reset_password = True
                else:
                    should_reset_password = True

                if should_reset_password:
                    cur.execute(
                        "UPDATE artist_table SET password = %s WHERE artist_id = %s",
                        (artist['password'], existing['artist_id'])
                    )"""

                # Update verification status and other fields using correct PascalCase columns
                cur.execute(
                    """
                    UPDATE artist_table
                    SET verification_status = %s,
                        is_enabled = %s,
                        Email = %s,
                        category_id = %s,
                        experience_years = %s,
                        price_per_hour = %s,
                        rating = %s
                    WHERE artist_id = %s
                    """,
                    ('approved', 1, artist['email'], artist['category_id'],
                     artist['experience_years'], artist['price_per_hour'],
                     artist['rating'], existing['artist_id'])
                )

        conn.commit()
        print("Demo artists setup complete")
        
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error setting up demo artists: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

'''def ensure_demo_artists():
    """Ensure demo artists exist in the database"""
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM category_table")
        if int((cur.fetchone() or {}).get('total') or 0) == 0:
            cur.executemany(
                """
                INSERT INTO category_table (category_id, category_name)
                VALUES (%s, %s)
                """,
                [(1, 'Singer'), (2, 'Dancer'), (3, 'Photographer')]
            )

        demo_artists = [
            {
                'first_name': 'Rohan',
                'last_name': 'Sharma',
                'Username': 'rohan@gmail.com',
                'Email': 'rohan@gmail.com',
                'Password': bcrypt.hashpw('Test@1234'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'),
                'Gender': 'Male',
                'Dob': '1995-05-15',
                'phone_number': '9876543210',
                'state_id': 11,  # Karnataka
                'city_id': 149,  # Bengaluru
                'category_id': 1,
                'Category': 'Singer',
                'portfolio_path': 'portfolio1.pdf',
                'Verification_status': 'approved',
                'Is_enabled': 1,
                'Pincode': '560001',
                'profile_pic': '',
                'portfolio_files': json.dumps(['/static/uploads/portfolio/rohan_demo.jpg']),
                'working_start_time': '09:00',
                'working_end_time': '18:00',
                'bank_name': 'HDFC Bank',
                'bank_account_number': '123456789012',
                'account_holder_name': 'Rohan Sharma',
                'ifsc_code': 'HDFC0001234',
                'upi_id': 'rohan@upi',
                'experience_years': 6,
                'price_per_hour': 1200,
                'rating': 4.6
            },
            {
                'first_name': 'Priya',
                'last_name': 'Patel',
                'Username': 'priya@gmail.com',
                'Email': 'priya@gmail.com',
                'Password': bcrypt.hashpw('Test@1234'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'),
                'Gender': 'Female',
                'Dob': '1998-08-22',
                'phone_number': '9876543211',
                'state_id': 7,   # Gujarat
                'city_id': 132,  # Ahmedabad
                'category_id': 2,
                'Category': 'Dancer',
                'portfolio_path': 'portfolio2.pdf',
                'Verification_status': 'approved',
                'Is_enabled': 1,
                'Pincode': '380001',
                'profile_pic': '',
                'portfolio_files': json.dumps(['/static/uploads/portfolio/priya_demo.jpg']),
                'working_start_time': '10:00',
                'working_end_time': '19:00',
                'bank_name': 'ICICI Bank',
                'bank_account_number': '987654321000',
                'account_holder_name': 'Priya Patel',
                'ifsc_code': 'ICIC0009876',
                'upi_id': 'priya@upi',
                'experience_years': 4,
                'price_per_hour': 1500,
                'rating': 4.8
            },
            {
                'first_name': 'Amit',
                'last_name': 'Verma',
                'Username': 'amit@gmail.com',
                'Email': 'amit@gmail.com',
                'Password': bcrypt.hashpw('Test@1234'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'),
                'Gender': 'Male',
                'Dob': '1993-12-10',
                'phone_number': '9876543212',
                'state_id': 14,  # Maharashtra
                'city_id': 216,  # Pune
                'category_id': 3,
                'Category': 'Photographer',
                'portfolio_path': 'portfolio3.pdf',
                'Verification_status': 'approved',
                'Is_enabled': 1,
                'Pincode': '411001',
                'profile_pic': '',
                'portfolio_files': json.dumps(['/static/uploads/portfolio/amit_demo.jpg']),
                'working_start_time': '08:00',
                'working_end_time': '17:00',
                'bank_name': 'SBI',
                'bank_account_number': '456789123456',
                'account_holder_name': 'Amit Verma',
                'ifsc_code': 'SBIN0001111',
                'upi_id': 'amit@upi',
                'experience_years': 8,
                'price_per_hour': 1800,
                'rating': 4.7
            }
        ]

        for artist in demo_artists:
            cur.execute(
                """
                SELECT artist_id, password, verification_status, is_enabled
                FROM artist_table
                WHERE Username = %s
                LIMIT 1
                """,
                (artist['username'],)
            )
            existing = cur.fetchone()

            if not existing:
                cur.execute("""
                    INSERT INTO artist_table 
                    (first_name, last_name, username, email, password, gender, dob, phone_number,
                     state_id, city_id, category_id, portfolio_path, verification_status, is_enabled,
                     profile_pic, portfolio_files, working_start_time, working_end_time,
                     experience_years, price_per_hour, rating)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    artist['first_name'], artist['last_name'], artist['username'],
                    artist['email'], artist['password'], artist['gender'], artist['dob'], artist['phone_number'],
                    artist['state_id'], artist['city_id'], artist['category_id'],
                    artist['portfolio_path'], artist['verification_status'], artist['is_enabled'],
                    artist['profile_pic'], artist['portfolio_files'], artist['working_start_time'], artist['working_end_time'],
                    artist['experience_years'], artist['price_per_hour'], artist['rating']
                ))

            current_password = str(existing.get('Password') or '')
            should_reset_password = False
            if current_password.startswith('$2b$') or current_password.startswith('$2a$') or current_password.startswith('$2y$'):
                try:
                    # Ensure known demo password works for seeded accounts.
                    if not bcrypt.checkpw('Test@1234'.encode('utf-8'), current_password.encode('utf-8')):
                        should_reset_password = True
                except Exception:
                    should_reset_password = True
            else:
                should_reset_password = True

            if should_reset_password:
                repaired_hash = bcrypt.hashpw('artist123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                cur.execute(
                    "UPDATE artist_table SET password = %s WHERE artist_id = %s",
                    (repaired_hash, existing['artist_id'])
                )

            verification_status = str(existing.get('Verification_status') or '').strip().lower()
            try:
                is_enabled = int(existing.get('Is_enabled') or 0)
            except (TypeError, ValueError):
                is_enabled = 0
            if verification_status != 'approved' or is_enabled != 1:
                cur.execute(
                    """
                    UPDATE artist_table
                    SET Verification_status = %s,
                        Is_enabled = %s,
                        email = %s,
                        category_id = %s,
                        Category = %s,
                        Pincode = %s,
                        portfolio_files = %s,
                        working_start_time = %s,
                        working_end_time = %s,
                        bank_name = %s,
                        bank_account_number = %s,
                        account_holder_name = %s,
                        ifsc_code = %s,
                        upi_id = %s,
                        experience_years = %s,
                        price_per_hour = %s,
                        rating = %s
                    WHERE artist_id = %s
                    """,
                    ('approved', 1, artist['email'], artist['category_id'], artist['Category'], artist['Pincode'],
                     artist['portfolio_files'], artist['working_start_time'], artist['working_end_time'],
                     artist['bank_name'], artist['bank_account_number'], artist['account_holder_name'],
                     artist['ifsc_code'], artist['upi_id'], artist['experience_years'], artist['price_per_hour'],
                     artist['rating'], existing['artist_id'])
                )
            else:
                cur.execute(
                    """
                    UPDATE artist_table
                    SET email = %s,
                        category_id = %s,
                        Category = %s,
                        Pincode = %s,
                        portfolio_files = %s,
                        working_start_time = %s,
                        working_end_time = %s,
                        bank_name = %s,
                        bank_account_number = %s,
                        account_holder_name = %s,
                        ifsc_code = %s,
                        upi_id = %s,
                        experience_years = %s,
                        price_per_hour = %s,
                        rating = %s
                    WHERE artist_id = %s
                    """,
                    (artist['email'], artist['category_id'], artist['Category'], artist['Pincode'],
                     artist['portfolio_files'], artist['working_start_time'], artist['working_end_time'],
                     artist['bank_name'], artist['bank_account_number'], artist['account_holder_name'],
                     artist['ifsc_code'], artist['upi_id'], artist['experience_years'], artist['price_per_hour'],
                     artist['rating'], existing['artist_id'])
                )

        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error setting up demo artists: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()'''


# ========== RUN APP ==========
if __name__ == '__main__':
    try:
        print("CREOVIBE STARTING...")

        os.makedirs('templates', exist_ok=True)
        os.makedirs('static', exist_ok=True)

        print("Calling ensure_demo_artists()...")
        ensure_demo_artists()
        print("Demo artists done.")

        print("Starting Flask server...")
        app.run(debug=True, port=5000)

    except Exception as e:
        print("FATAL ERROR:")
        print(e)
