import os
from datetime import timedelta


class Config:
    # ── Flask ────────────────────────────────────────────────
    SECRET_KEY                = os.getenv('FLASK_SECRET_KEY', 'creovibe-secret-key-change-in-production-2024')
    SESSION_TYPE              = 'filesystem'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=1)

    # ── Upload dirs ──────────────────────────────────────────
    PORTFOLIO_UPLOAD_DIR       = os.path.join('static', 'uploads', 'portfolio')
    PROFILE_PICTURE_UPLOAD_DIR = os.path.join('static', 'uploads', 'profile_pictures')

    # ── MySQL ────────────────────────────────────────────────
    MYSQL_HOST     = os.getenv('DB_HOST',     'localhost')
    MYSQL_USER     = os.getenv('DB_USER',     'root')
    MYSQL_PASSWORD = os.getenv('DB_PASSWORD', 'root123')
    MYSQL_DATABASE = os.getenv('DB_NAME',     'creovibe_db')

    # ── Razorpay ─────────────────────────────────────────────
    RAZORPAY_KEY_ID     = os.getenv('RAZORPAY_KEY_ID',     'rzp_test_SQ3PmH2onlaf9U')
    RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', '9z4mwVgKVudNPcEpZBjgt88w')

    # ── Gmail SMTP (for OTP emails) ──────────────────────────
    GMAIL_USER         = os.getenv('GMAIL_USER',         'creovibe09@gmail.com')
    GMAIL_APP_PASSWORD = os.getenv('GMAIL_APP_PASSWORD', 'padf uext utoi bqva')
