from flask import Blueprint, request, jsonify, current_app, session
import base64
import requests
import pymysql
from pymysql.cursors import DictCursor
from datetime import datetime, timedelta

subscription_bp = Blueprint('subscription', __name__)


def get_db():
    return pymysql.connect(
        host=current_app.config.get('MYSQL_HOST', 'localhost'),
        user=current_app.config.get('MYSQL_USER', 'root'),
        password=current_app.config.get('MYSQL_PASSWORD', 'root123'),
        database=current_app.config.get('MYSQL_DATABASE', 'creovibe_db'),
        cursorclass=DictCursor,
        charset='utf8mb4',
        autocommit=False
    )


@subscription_bp.route("/subscription/create-order/<int:plan_id>", methods=["GET", "POST"])
def create_order(plan_id):
    db = None
    cur = None
    try:
        artist_id = session.get("artist_id")
        if not artist_id:
            return jsonify({"error": "Login required"}), 401

        db = get_db()
        cur = db.cursor()

        # Fetch plan from subscription_plan_table
        cur.execute(
            "SELECT * FROM subscription_plan_table WHERE plan_id = %s LIMIT 1",
            (plan_id,)
        )
        plan = cur.fetchone()
        print("PLAN:", plan)

        if not plan:
            return jsonify({"error": "Plan not found"}), 404

        # Get price — column is 'amount' in subscription_plan_table
        raw_amount = plan.get('amount') or plan.get('price') or plan.get('Amount') or 0
        amount = int(float(raw_amount) * 100)  # Convert to paise
        print("AMOUNT (paise):", amount)

        if amount <= 0:
            return jsonify({"error": "Invalid plan amount"}), 400

        # Razorpay order creation
        key_id = current_app.config["RAZORPAY_KEY_ID"]
        key_secret = current_app.config["RAZORPAY_KEY_SECRET"]
        auth = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()

        receipt = f"plan_{artist_id}_{plan_id}"
        response = requests.post(
            "https://api.razorpay.com/v1/orders",
            json={
                "amount": amount,
                "currency": "INR",
                "receipt": receipt
            },
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/json"
            },
            timeout=20
        )

        print("RAZORPAY RESPONSE:", response.status_code, response.text)

        if response.status_code >= 400:
            error_detail = "Failed to create Razorpay order"
            try:
                error_detail = response.json().get("error", {}).get("description", error_detail)
            except Exception:
                pass
            return jsonify({"error": error_detail}), 500

        data = response.json()
        return jsonify({
            "order_id": data["id"],
            "amount": amount,
            "currency": "INR",
            "key": key_id
        })

    except Exception as e:
        print("CREATE-ORDER ERROR:", e)
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if db:
            db.close()


@subscription_bp.route("/subscription/verify-payment", methods=["POST"])
def verify_payment():
    data = request.get_json(silent=True) or {}
    artist_id = session.get("artist_id")
    if not artist_id:
        return jsonify({"success": False, "error": "Login required"}), 401

    plan_id = data.get("plan_id")
    payment_id = data.get("razorpay_payment_id", "")

    if not plan_id:
        return jsonify({"success": False, "error": "plan_id is required"}), 400

    print("VERIFY-PAYMENT: artist_id=", artist_id, "plan_id=", plan_id, "payment_id=", payment_id)

    db = None
    cur = None
    try:
        db = get_db()
        cur = db.cursor()

        # Fetch plan details to get duration
        cur.execute(
            "SELECT * FROM subscription_plan_table WHERE plan_id = %s LIMIT 1",
            (plan_id,)
        )
        plan = cur.fetchone()
        duration_days = 30  # default
        if plan:
            duration_days = int(plan.get('duration_days') or plan.get('Duration_Days') or 30)

        start_date = datetime.now().date()
        end_date = start_date + timedelta(days=duration_days)

        # Deactivate any existing active subscriptions
        cur.execute(
            "UPDATE subscription_table SET status = 'inactive' WHERE artist_id = %s AND LOWER(status) = 'active'",
            (artist_id,)
        )

        # Insert new active subscription
        cur.execute(
            """
            INSERT INTO subscription_table
            (artist_id, plan_id, start_date, end_date, status)
            VALUES (%s, %s, %s, %s, 'active')
            """,
            (artist_id, plan_id, start_date, end_date)
        )

        db.commit()
        print("SUBSCRIPTION SAVED: artist_id=", artist_id, "plan_id=", plan_id, "end_date=", end_date)
        return jsonify({"success": True, "message": "Subscription activated successfully"})

    except Exception as e:
        if db:
            db.rollback()
        print("VERIFY-PAYMENT ERROR:", e)
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if db:
            db.close()


@subscription_bp.route("/subscription/cancel", methods=["POST"])
def cancel_subscription():
    artist_id = session.get("artist_id")
    if not artist_id:
        return jsonify({"success": False, "error": "Login required"}), 401

    db = None
    cur = None
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """
            UPDATE subscription_table
            SET status='cancelled'
            WHERE artist_id=%s AND status='active'
            """,
            (artist_id,)
        )
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        if db:
            db.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if db:
            db.close()
