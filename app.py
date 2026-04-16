import os
import sqlite3
import secrets
from datetime import date, datetime

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

app = Flask(__name__)
DEFAULT_SECRET_KEY = "study-hall-dev-secret"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"
DEFAULT_APP_ENV = "development"


class DBConnection:
    def __init__(self, backend, raw_connection):
        self.backend = backend
        self.raw_connection = raw_connection

    def adapt_query(self, query):
        if self.backend == "postgres":
            return query.replace("?", "%s")
        return query

    def execute(self, query, params=None):
        cursor = self.raw_connection.execute(self.adapt_query(query), params or ())
        return cursor

    def executemany(self, query, param_sets):
        if self.backend == "postgres":
            cursor = self.raw_connection.cursor()
            cursor.executemany(self.adapt_query(query), param_sets)
            return cursor
        return self.raw_connection.executemany(self.adapt_query(query), param_sets)

    def cursor(self):
        return DBCursor(self, self.raw_connection.cursor())

    def commit(self):
        self.raw_connection.commit()

    def rollback(self):
        self.raw_connection.rollback()

    def close(self):
        self.raw_connection.close()


class DBCursor:
    def __init__(self, connection, raw_cursor):
        self.connection = connection
        self.raw_cursor = raw_cursor

    def execute(self, query, params=None):
        self.raw_cursor.execute(
            self.connection.adapt_query(query),
            params or (),
        )
        return self

    def executemany(self, query, param_sets):
        self.raw_cursor.executemany(
            self.connection.adapt_query(query),
            param_sets,
        )
        return self

    def fetchone(self):
        return self.raw_cursor.fetchone()

    def fetchall(self):
        return self.raw_cursor.fetchall()


def resolve_app_env():
    raw_env = os.environ.get("STUDY_HALL_ENV", DEFAULT_APP_ENV).strip().lower()
    return raw_env if raw_env in {"development", "production"} else DEFAULT_APP_ENV


def resolve_db_path(app_env):
    explicit_path = os.environ.get("STUDY_HALL_DB_PATH")
    if explicit_path:
        return explicit_path

    project_dir = os.path.dirname(__file__)
    default_paths = {
        "development": os.path.join(project_dir, "database.db"),
        "production": os.path.join(project_dir, "instance", "production.db"),
    }
    return default_paths.get(app_env, default_paths["development"])

app.secret_key = os.environ.get("STUDY_HALL_SECRET_KEY", DEFAULT_SECRET_KEY)
app.config["APP_ENV"] = resolve_app_env()
app.config["DATABASE_URL"] = os.environ.get("DATABASE_URL", "").strip()
app.config["DB_BACKEND"] = "postgres" if app.config["DATABASE_URL"] else "sqlite"
app.config["ADMIN_USERNAME"] = os.environ.get("STUDY_HALL_ADMIN_USER", DEFAULT_ADMIN_USERNAME)
app.config["ADMIN_PASSWORD"] = os.environ.get("STUDY_HALL_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
app.config["DB_PATH"] = resolve_db_path(app.config["APP_ENV"])
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("STUDY_HALL_SECURE_COOKIE", "0") == "1"


@app.before_request
def require_login():
    public_endpoints = {"login", "static", "healthz"}

    if request.endpoint in {"static", "healthz"}:
        return None

    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not submitted_token or not session_token or submitted_token != session_token:
            flash("Your session form token was invalid. Please try again.", "error")
            fallback = url_for("login") if request.endpoint == "login" else (request.referrer or url_for("home"))
            return redirect(fallback)

    if request.endpoint in public_endpoints:
        return None

    if not session.get("logged_in"):
        return redirect(url_for("login", next=request.path))

    return None


def ensure_db_directory():
    if app.config["DB_BACKEND"] != "sqlite":
        return
    db_dir = os.path.dirname(app.config["DB_PATH"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def get_db_connection():
    if app.config["DB_BACKEND"] == "postgres":
        if psycopg is None:
            raise RuntimeError(
                "PostgreSQL support requires psycopg. Install dependencies from requirements.txt."
            )
        raw_conn = psycopg.connect(
            app.config["DATABASE_URL"],
            row_factory=dict_row,
        )
        return DBConnection("postgres", raw_conn)

    ensure_db_directory()
    raw_conn = sqlite3.connect(app.config["DB_PATH"], timeout=10)
    raw_conn.row_factory = sqlite3.Row
    raw_conn.execute("PRAGMA foreign_keys = ON")
    return DBConnection("sqlite", raw_conn)


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_template_helpers():
    return {"csrf_token": get_csrf_token}


def get_table_columns(conn, table_name):
    if conn.backend == "postgres":
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table_name,),
        ).fetchall()
        return {row["column_name"] for row in rows}

    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def ensure_column(conn, table_name, column_name, definition):
    if conn.backend == "postgres":
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {definition}"
        )
        return

    columns = get_table_columns(conn, table_name)
    if column_name not in columns:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def ensure_seat_validation_rules(conn):
    if conn.backend == "postgres":
        conn.execute(
            """
            CREATE OR REPLACE FUNCTION validate_student_seat_assignment()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.status = 'active' AND NEW.seat_id IS NOT NULL THEN
                    IF NEW.shift = 'full' AND EXISTS (
                        SELECT 1 FROM students
                        WHERE seat_id = NEW.seat_id
                          AND status = 'active'
                          AND id <> COALESCE(NEW.id, -1)
                    ) THEN
                        RAISE EXCEPTION 'Full seat cannot share with another active student.';
                    END IF;

                    IF NEW.shift IN ('morning', 'evening') AND EXISTS (
                        SELECT 1 FROM students
                        WHERE seat_id = NEW.seat_id
                          AND status = 'active'
                          AND shift = 'full'
                          AND id <> COALESCE(NEW.id, -1)
                    ) THEN
                        RAISE EXCEPTION 'Half seat cannot be assigned when a full student is already in the seat.';
                    END IF;

                    IF NEW.shift IN ('morning', 'evening') AND EXISTS (
                        SELECT 1 FROM students
                        WHERE seat_id = NEW.seat_id
                          AND status = 'active'
                          AND shift = NEW.shift
                          AND id <> COALESCE(NEW.id, -1)
                    ) THEN
                        RAISE EXCEPTION 'This shift is already assigned in the selected seat.';
                    END IF;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        conn.execute(
            """
            DROP TRIGGER IF EXISTS trg_students_validate_insert ON students
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_students_validate_insert
            BEFORE INSERT ON students
            FOR EACH ROW
            EXECUTE FUNCTION validate_student_seat_assignment()
            """
        )
        conn.execute(
            """
            DROP TRIGGER IF EXISTS trg_students_validate_update ON students
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_students_validate_update
            BEFORE UPDATE OF seat_id, shift, status ON students
            FOR EACH ROW
            EXECUTE FUNCTION validate_student_seat_assignment()
            """
        )
        return

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_students_validate_insert
        BEFORE INSERT ON students
        WHEN NEW.status = 'active' AND NEW.seat_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'Full seat cannot share with another active student.')
            WHERE NEW.shift = 'full'
            AND EXISTS (
                SELECT 1 FROM students
                WHERE seat_id = NEW.seat_id AND status = 'active'
            );

            SELECT RAISE(ABORT, 'Half seat cannot be assigned when a full student is already in the seat.')
            WHERE NEW.shift IN ('morning', 'evening')
            AND EXISTS (
                SELECT 1 FROM students
                WHERE seat_id = NEW.seat_id AND status = 'active' AND shift = 'full'
            );

            SELECT RAISE(ABORT, 'This shift is already assigned in the selected seat.')
            WHERE NEW.shift IN ('morning', 'evening')
            AND EXISTS (
                SELECT 1 FROM students
                WHERE seat_id = NEW.seat_id AND status = 'active' AND shift = NEW.shift
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_students_validate_update
        BEFORE UPDATE OF seat_id, shift, status ON students
        WHEN NEW.status = 'active' AND NEW.seat_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'Full seat cannot share with another active student.')
            WHERE NEW.shift = 'full'
            AND EXISTS (
                SELECT 1 FROM students
                WHERE seat_id = NEW.seat_id AND status = 'active' AND id != NEW.id
            );

            SELECT RAISE(ABORT, 'Half seat cannot be assigned when a full student is already in the seat.')
            WHERE NEW.shift IN ('morning', 'evening')
            AND EXISTS (
                SELECT 1 FROM students
                WHERE seat_id = NEW.seat_id AND status = 'active' AND shift = 'full' AND id != NEW.id
            );

            SELECT RAISE(ABORT, 'This shift is already assigned in the selected seat.')
            WHERE NEW.shift IN ('morning', 'evening')
            AND EXISTS (
                SELECT 1 FROM students
                WHERE seat_id = NEW.seat_id AND status = 'active' AND shift = NEW.shift AND id != NEW.id
            );
        END
        """
    )


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    ensure_column(conn, "students", "joining_date", "TEXT")
    ensure_column(conn, "students", "monthly_fee", "REAL")
    ensure_column(conn, "payments", "amount", "REAL")
    ensure_column(conn, "payments", "payment_date", "TEXT")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_student_month
        ON payments(student_id, month)
        """
    )
    ensure_seat_validation_rules(conn)


def normalize_phone_number(raw_value, student_id, used_numbers):
    digits = "".join(ch for ch in (raw_value or "") if ch.isdigit())
    candidates = []

    if len(digits) >= 10:
        candidates.append(digits[-10:])
    elif len(digits) > 0:
        candidates.append(digits.zfill(10))

    candidates.append(f"7{student_id:09d}")
    candidates.append(f"8{student_id:09d}")

    for candidate in candidates:
        if candidate not in used_numbers and len(candidate) == 10:
            return candidate

    counter = student_id
    while True:
        candidate = f"{9000000000 + counter:010d}"[-10:]
        if candidate not in used_numbers:
            return candidate
        counter += 1


def normalize_existing_phone_numbers(conn):
    rows = conn.execute(
        "SELECT id, phone FROM students ORDER BY id"
    ).fetchall()
    used_numbers = set()
    updates = []

    for row in rows:
        phone = row["phone"] or ""
        if phone.isdigit() and len(phone) == 10 and phone not in used_numbers:
            used_numbers.add(phone)
            continue

        normalized = normalize_phone_number(phone, row["id"], used_numbers)
        used_numbers.add(normalized)
        updates.append((normalized, row["id"]))

    if updates:
        conn.executemany(
            "UPDATE students SET phone = ? WHERE id = ?",
            updates
        )


def get_setting(conn, key):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    ).fetchone()
    return row["value"] if row else None


def set_setting(conn, key, value):
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value)
    )


def delete_setting(conn, key):
    conn.execute(
        "DELETE FROM settings WHERE key = ?",
        (key,)
    )


def ensure_login_settings(conn):
    if not get_setting(conn, "admin_username"):
        set_setting(conn, "admin_username", app.config["ADMIN_USERNAME"])
    password_hash = get_setting(conn, "admin_password_hash")
    legacy_password = get_setting(conn, "admin_password")

    if not password_hash:
        if legacy_password:
            set_setting(conn, "admin_password_hash", generate_password_hash(legacy_password))
        else:
            set_setting(
                conn,
                "admin_password_hash",
                generate_password_hash(app.config["ADMIN_PASSWORD"])
            )
    if legacy_password:
        delete_setting(conn, "admin_password")


def get_login_credentials(conn):
    username = get_setting(conn, "admin_username") or app.config["ADMIN_USERNAME"]
    password_hash = get_setting(conn, "admin_password_hash")
    legacy_password = get_setting(conn, "admin_password")

    if not password_hash:
        if legacy_password:
            password_hash = generate_password_hash(legacy_password)
            set_setting(conn, "admin_password_hash", password_hash)
        else:
            password_hash = generate_password_hash(app.config["ADMIN_PASSWORD"])
            set_setting(conn, "admin_password_hash", password_hash)
    if legacy_password:
        delete_setting(conn, "admin_password")

    return username, password_hash


def get_login_config_warnings(conn):
    warnings = []

    if app.secret_key == DEFAULT_SECRET_KEY:
        warnings.append(
            "The app is using the default secret key. Set STUDY_HALL_SECRET_KEY before real deployment."
        )

    username, password_hash = get_login_credentials(conn)
    if (
        username == DEFAULT_ADMIN_USERNAME
        and check_password_hash(password_hash, DEFAULT_ADMIN_PASSWORD)
    ):
        warnings.append(
            "The app is still using the default login credentials. Change them inside the app or set STUDY_HALL_ADMIN_USER and STUDY_HALL_ADMIN_PASSWORD before real deployment."
        )

    return warnings


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_month_label(value):
    if not value or value == "No dues yet":
        return value
    try:
        return datetime.strptime(value, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return value


def format_full_date(value):
    if not value:
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return value


@app.template_filter("month_label")
def month_label_filter(value):
    return format_month_label(value)


@app.template_filter("full_date")
def full_date_filter(value):
    return format_full_date(value)


def shift_month(month_date, offset):
    year = month_date.year
    month = month_date.month + offset

    while month <= 0:
        month += 12
        year -= 1

    while month > 12:
        month -= 12
        year += 1

    return month_date.replace(year=year, month=month)


def recent_month_labels(count=4, ref_date=None):
    ref_month = (ref_date or date.today()).replace(day=1)
    months = []

    for offset in range(count - 1, -1, -1):
        month_date = shift_month(ref_month, -offset)
        months.append(month_date.strftime("%Y-%m"))

    return months


def month_range(start_date, end_date):
    months = []
    cursor = start_date.replace(day=1)
    limit = end_date.replace(day=1)

    while cursor <= limit:
        months.append(cursor.strftime("%Y-%m"))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    return months


def get_due_months(student, upto_date=None):
    joining_date = parse_date(student["joining_date"])
    upto_date = upto_date or date.today()

    if not joining_date:
        return []

    start_month = joining_date.replace(day=1)
    end_month = upto_date.replace(day=1)

    if start_month > end_month:
        return []

    return month_range(start_month, end_month)


def get_payment_status(conn, student):
    paid_months = {
        row["month"] for row in conn.execute(
            "SELECT month FROM payments WHERE student_id = ?",
            (student["id"],)
        ).fetchall()
    }
    due_months = get_due_months(student)
    month_states = build_month_states(due_months, paid_months)
    unpaid_months = [month for month in due_months if month_states[month] == "unpaid"]
    last_due_month = due_months[-1] if due_months else None
    seat_type = "full" if student["shift"] == "full" else "half"

    if not due_months:
        paid_till = "No due month yet"
    else:
        paid_till = "No fee month paid yet"

    for month in due_months:
        if month_states[month] == "paid":
            paid_till = month
        else:
            break

    return {
        "seat_type": seat_type,
        "paid_till": paid_till,
        "last_due_month": last_due_month,
        "last_month_paid": bool(last_due_month)
        and month_states.get(last_due_month) == "paid",
        "unpaid_months": unpaid_months,
        "unpaid_count": len(unpaid_months),
    }


def build_month_states(due_months, paid_months):
    states = {}
    gap_found = False

    for month in due_months:
        if month in paid_months and not gap_found:
            states[month] = "paid"
        elif month in paid_months and gap_found:
            states[month] = "advance_paid"
        else:
            states[month] = "unpaid"
            gap_found = True

    return states


def get_selectable_payment_months(due_months, paid_months):
    states = build_month_states(due_months, paid_months)
    selectable = []
    first_unpaid_found = False

    for month in due_months:
        state = states[month]
        if state == "unpaid":
            selectable.append(month)
            first_unpaid_found = True
        elif first_unpaid_found:
            break

    return selectable, states


def build_student_summary(conn, student, recent_months):
    payment_rows = conn.execute(
        "SELECT month, payment_date FROM payments WHERE student_id = ?",
        (student["id"],)
    ).fetchall()
    payment_map = {row["month"]: row["payment_date"] for row in payment_rows}
    paid_months = set(payment_map.keys())
    due_months = get_due_months(student)
    due_month_set = set(due_months)
    month_states = build_month_states(due_months, paid_months)
    seat_type = "Full" if student["shift"] == "full" else "Half"
    month_statuses = []

    for month in recent_months:
        if month in due_month_set:
            status = month_states[month]
        else:
            status = "not_due"

        month_statuses.append(
            {
                "month": month,
                "status": status,
                "payment_date": payment_map.get(month),
            }
        )

    return {
        "id": student["id"],
        "name": student["name"],
        "phone": student["phone"],
        "seat_id": student["seat_id"],
        "seat_type": seat_type,
        "shift": student["shift"].capitalize(),
        "joining_date": student["joining_date"],
        "monthly_fee": student["monthly_fee"],
        "status": student["status"],
        "month_statuses": month_statuses,
        "unpaid_months": [month for month in due_months if month_states[month] == "unpaid"],
    }


def build_payment_history(conn, student_id):
    rows = conn.execute(
        """
        SELECT month, amount, payment_date
        FROM payments
        WHERE student_id = ?
        ORDER BY month
        """,
        (student_id,)
    ).fetchall()

    history = []
    for row in rows:
        history.append(
            {
                "month": row["month"],
                "amount": row["amount"],
                "payment_date": row["payment_date"],
            }
        )

    return history


def build_dashboard_summary(conn):
    unpaid_students = build_unpaid_students(conn)
    total_pending_amount = sum(item["pending_amount"] for item in unpaid_students)
    seat_counts = {
        row["type"]: row["count"]
        for row in conn.execute(
            "SELECT type, COUNT(*) AS count FROM seats GROUP BY type"
        ).fetchall()
    }
    active_students = conn.execute(
        "SELECT * FROM students WHERE status = 'active' ORDER BY seat_id, id"
    ).fetchall()

    return {
        "active_students": len(active_students),
        "empty_seats": seat_counts.get("empty", 0),
        "half_seats": seat_counts.get("half", 0),
        "full_seats": seat_counts.get("full", 0),
        "students_with_dues": len(unpaid_students),
        "total_pending_amount": total_pending_amount,
        "unpaid_students": unpaid_students[:6],
    }


def build_unpaid_students(conn):
    active_students = conn.execute(
        "SELECT * FROM students WHERE status = 'active' ORDER BY seat_id, id"
    ).fetchall()
    unpaid_students = []

    for student in active_students:
        payment_rows = conn.execute(
            "SELECT month FROM payments WHERE student_id = ?",
            (student["id"],)
        ).fetchall()
        paid_months = {row["month"] for row in payment_rows}
        due_months = get_due_months(student)
        month_states = build_month_states(due_months, paid_months)
        unpaid_months = [
            month for month in due_months if month_states[month] == "unpaid"
        ]

        if unpaid_months:
            pending_amount = (student["monthly_fee"] or 0) * len(unpaid_months)
            unpaid_students.append(
                {
                    "id": student["id"],
                    "name": student["name"],
                    "phone": student["phone"],
                    "seat_id": student["seat_id"],
                    "pending_months": len(unpaid_months),
                    "latest_unpaid_month": unpaid_months[0],
                    "pending_amount": pending_amount,
                    "monthly_fee": student["monthly_fee"] or 0,
                }
            )

    unpaid_students.sort(
        key=lambda item: (item["latest_unpaid_month"], item["seat_id"] or 9999, item["name"])
    )

    return unpaid_students


def build_seat_view(conn, seat):
    students = conn.execute(
        """
        SELECT * FROM students
        WHERE seat_id = ? AND status = 'active'
        ORDER BY CASE shift
            WHEN 'full' THEN 0
            WHEN 'morning' THEN 1
            WHEN 'evening' THEN 2
            ELSE 3
        END
        """,
        (seat["id"],)
    ).fetchall()

    occupied_labels = []
    if any(student["shift"] == "full" for student in students):
        occupied_labels.append("Full")
    else:
        for shift in ("morning", "evening"):
            if any(student["shift"] == shift for student in students):
                occupied_labels.append(shift.capitalize())

    return {
        "id": seat["id"],
        "type": seat["type"],
        "students": students,
        "occupied_labels": occupied_labels,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_url = request.values.get("next") or url_for("home")
    conn = get_db_connection()
    username_value, password_hash = get_login_credentials(conn)
    config_warnings = get_login_config_warnings(conn)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == username_value and check_password_hash(password_hash, password):
            conn.close()
            session["logged_in"] = True
            flash("You are now logged in.", "success")
            return redirect(next_url if next_url.startswith("/") else url_for("home"))

        error = "Invalid username or password."

    conn.close()
    return render_template(
        "login.html",
        error=error,
        next_url=next_url,
        config_warnings=config_warnings,
    )


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account_settings():
    conn = get_db_connection()
    current_username, current_password_hash = get_login_credentials(conn)
    error = None
    success = None

    if request.method == "POST":
        old_password = request.form.get("current_password", "")
        new_username = request.form.get("username", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password_hash(current_password_hash, old_password):
            error = "Current password is not correct."
        elif not new_username:
            error = "Login ID cannot be empty."
        elif len(new_password) < 6:
            error = "New password must be at least 6 characters."
        elif new_password != confirm_password:
            error = "New password and confirm password must match."
        else:
            set_setting(conn, "admin_username", new_username)
            set_setting(conn, "admin_password_hash", generate_password_hash(new_password))
            delete_setting(conn, "admin_password")
            conn.commit()
            current_username, current_password_hash = new_username, generate_password_hash(new_password)
            success = "Login ID and password updated successfully."

    config_warnings = get_login_config_warnings(conn)
    conn.close()
    return render_template(
        "account.html",
        current_username=current_username,
        error=error,
        success=success,
        config_warnings=config_warnings,
    )


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


def get_available_shift_options(conn, seat_id):
    students = conn.execute(
        "SELECT shift FROM students WHERE seat_id = ? AND status = 'active'",
        (seat_id,)
    ).fetchall()
    occupied_shifts = {student["shift"] for student in students}

    if "full" in occupied_shifts:
        return []
    if not occupied_shifts:
        return ["full", "morning", "evening"]

    options = []
    if "morning" not in occupied_shifts:
        options.append("morning")
    if "evening" not in occupied_shifts:
        options.append("evening")
    return options


def build_available_seat_options(conn):
    seat_rows = conn.execute("SELECT * FROM seats ORDER BY id").fetchall()
    options = []

    for seat in seat_rows:
        available_shifts = get_available_shift_options(conn, seat["id"])
        if not available_shifts:
            display_type = "full"
        elif len(available_shifts) == 3:
            display_type = "empty"
        else:
            display_type = "half"
        options.append(
            {
                "id": seat["id"],
                "type": seat["type"],
                "display_type": display_type,
                "available_shifts": available_shifts,
                "is_available": bool(available_shifts),
            }
        )

    return options


def get_fee_for_shift(shift):
    return 1000.0 if shift == "full" else 500.0


def insert_student_record(conn, name, phone, seat_id, shift, joining_date, monthly_fee):
    if conn.backend == "postgres":
        row = conn.execute(
            """
            INSERT INTO students (name, phone, seat_id, shift, joining_date, monthly_fee)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (name, phone, seat_id, shift, joining_date, monthly_fee),
        ).fetchone()
        return row["id"]

    conn.execute(
        """
        INSERT INTO students (name, phone, seat_id, shift, joining_date, monthly_fee)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, phone, seat_id, shift, joining_date, monthly_fee),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def save_payment_record(conn, student_id, month, amount, payment_date):
    if conn.backend == "postgres":
        conn.execute(
            """
            INSERT INTO payments (student_id, month, amount, payment_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (student_id, month) DO NOTHING
            """,
            (student_id, month, amount, payment_date),
        )
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO payments (student_id, month, amount, payment_date)
        VALUES (?, ?, ?, ?)
        """,
        (student_id, month, amount, payment_date),
    )


def get_change_seat_options(conn, desired_shift=None, show_all=False, current_student_id=None):
    seat_rows = conn.execute("SELECT * FROM seats ORDER BY id").fetchall()
    options = []

    for seat in seat_rows:
        occupants = conn.execute(
            """
            SELECT * FROM students
            WHERE seat_id = ? AND status = 'active' AND id != ?
            ORDER BY CASE shift
                WHEN 'full' THEN 0
                WHEN 'morning' THEN 1
                WHEN 'evening' THEN 2
                ELSE 3
            END
            """,
            (seat["id"], current_student_id or -1)
        ).fetchall()
        occupied_shifts = {student["shift"] for student in occupants}
        available_shifts = []

        if "full" not in occupied_shifts:
            if not occupied_shifts:
                available_shifts.append("full")
            if "morning" not in occupied_shifts:
                available_shifts.append("morning")
            if "evening" not in occupied_shifts:
                available_shifts.append("evening")

        options.append(
            {
                "id": seat["id"],
                "type": seat["type"],
                "occupants": occupants,
                "available_shifts": available_shifts,
                "is_available": bool(available_shifts),
                "matches_filter": show_all or not desired_shift or desired_shift in available_shifts,
            }
        )

    return options


@app.route("/")
def home():
    conn = get_db_connection()
    dashboard = build_dashboard_summary(conn)
    conn.close()

    return render_template("index.html", dashboard=dashboard)


@app.route("/dues")
def dues():
    conn = get_db_connection()
    unpaid_students = build_unpaid_students(conn)
    total_pending_amount = sum(item["pending_amount"] for item in unpaid_students)
    conn.close()

    return render_template(
        "dues.html",
        unpaid_students=unpaid_students,
        total_pending_amount=total_pending_amount,
    )


@app.route("/student/<int:student_id>/edit-phone", methods=["GET", "POST"])
def edit_student_phone(student_id):
    conn = get_db_connection()
    student = conn.execute(
        "SELECT * FROM students WHERE id = ?",
        (student_id,)
    ).fetchone()

    if not student:
        conn.close()
        flash("Student was not found or is already inactive.", "error")
        return redirect(url_for("students"))

    error = None
    success = None
    return_to = request.values.get("return_to") or url_for("students", seat_id=student["seat_id"])

    if request.method == "POST":
        phone = request.form.get("phone", "").strip()

        if not phone.isdigit() or len(phone) != 10:
            error = "Phone number must be exactly 10 digits."
        elif conn.execute(
            "SELECT 1 FROM students WHERE phone = ? AND status = 'active' AND id != ?",
            (phone, student_id)
        ).fetchone():
            error = "Another active student already uses this phone number."
        else:
            conn.execute(
                "UPDATE students SET phone = ? WHERE id = ?",
                (phone, student_id)
            )
            conn.commit()
            success = "Mobile number updated successfully."
            student = conn.execute(
                "SELECT * FROM students WHERE id = ?",
                (student_id,)
            ).fetchone()

    conn.close()
    return render_template(
        "edit_phone.html",
        student=student,
        return_to=return_to,
        error=error,
        success=success,
    )


@app.route("/seat/<int:seat_id>")
def seat_detail(seat_id):
    conn = get_db_connection()

    seat = conn.execute(
        "SELECT * FROM seats WHERE id = ?", (seat_id,)
    ).fetchone()

    if not seat:
        conn.close()
        return redirect(url_for("seats"))

    students = conn.execute(
        """
        SELECT * FROM students
        WHERE seat_id = ? AND status = 'active'
        ORDER BY CASE shift
            WHEN 'full' THEN 0
            WHEN 'morning' THEN 1
            WHEN 'evening' THEN 2
            ELSE 3
        END
        """,
        (seat_id,)
    ).fetchall()

    payment_summaries = {
        student["id"]: get_payment_status(conn, student) for student in students
    }

    conn.close()
    return render_template(
        "seat.html",
        seat=seat,
        students=students,
        payment_summaries=payment_summaries,
    )



@app.route("/remove_student/<int:student_id>", methods=["POST"])
def remove_student(student_id):
    conn = get_db_connection()

    student = conn.execute(
        "SELECT * FROM students WHERE id = ? AND status = 'active'",
        (student_id,)
    ).fetchone()

    if not student:
        conn.close()
        return redirect(url_for("students"))

    seat_id = student["seat_id"]

    conn.execute(
        "UPDATE students SET status = 'inactive', seat_id = NULL WHERE id = ?",
        (student_id,)
    )

    # Update seat
    update_seat_type(conn, seat_id)

    conn.commit()
    conn.close()

    flash("Student removed from the seat successfully.", "success")
    return redirect(url_for("seat_detail", seat_id=seat_id))

@app.route("/seats")
def seats():
    conn = get_db_connection()
    seat_rows = conn.execute("SELECT * FROM seats ORDER BY id").fetchall()
    seats = [build_seat_view(conn, seat) for seat in seat_rows]
    conn.close()

    return render_template("seats.html", seats=seats)


@app.route("/change-seat/<int:student_id>", methods=["GET", "POST"])
def change_seat_plan(student_id):
    conn = get_db_connection()

    student = conn.execute(
        "SELECT * FROM students WHERE id = ? AND status = 'active'",
        (student_id,)
    ).fetchone()

    if not student:
        conn.close()
        flash("Student was not found.", "error")
        return redirect(url_for("students"))

    selected_plan = request.values.get("seat_plan") or (
        "full" if student["shift"] == "full" else "half"
    )
    selected_shift = request.values.get("shift") or student["shift"]
    show_all = request.values.get("show_all") == "1"
    error = None

    if selected_plan == "full":
        selected_shift = "full"
    elif selected_shift not in {"morning", "evening"}:
        selected_shift = "morning"

    seat_options = get_change_seat_options(
        conn,
        desired_shift=selected_shift,
        show_all=show_all,
        current_student_id=student_id,
    )
    visible_seats = [
        seat for seat in seat_options
        if seat["is_available"] and (show_all or seat["matches_filter"])
    ]

    if request.method == "POST":
        new_seat_id = int(request.form["seat_id"])
        selected_plan = request.form.get("seat_plan", selected_plan)
        selected_shift = request.form.get("shift", selected_shift)

        if selected_plan == "full":
            selected_shift = "full"
        elif selected_shift not in {"morning", "evening"}:
            error = "Please select a valid shift for half seat."

        target_seat = next((seat for seat in seat_options if seat["id"] == new_seat_id), None)

        if not error and (not target_seat or not target_seat["is_available"]):
            error = "Selected seat is not available."
        elif not error and selected_shift not in target_seat["available_shifts"]:
            error = "Selected seat does not support that plan or shift."

        if not error:
            old_seat_id = student["seat_id"]
            conn.execute(
                """
                UPDATE students
                SET seat_id = ?, shift = ?, monthly_fee = ?
                WHERE id = ?
                """,
                (new_seat_id, selected_shift, get_fee_for_shift(selected_shift), student_id)
            )
            if old_seat_id == new_seat_id:
                update_seat_type(conn, new_seat_id)
            else:
                update_seat_type(conn, old_seat_id)
                update_seat_type(conn, new_seat_id)
            conn.commit()
            conn.close()
            flash("Seat plan updated successfully.", "success")
            return redirect(url_for("seat_detail", seat_id=new_seat_id))

    conn.close()
    return render_template(
        "change_seat.html",
        student=student,
        seat_options=visible_seats,
        selected_plan=selected_plan,
        selected_shift=selected_shift,
        show_all=show_all,
        error=error,
        fee_amount=get_fee_for_shift(selected_shift),
    )

@app.route("/add_student", methods=["GET", "POST"])
def add_student():
    conn = get_db_connection()
    seat_options = build_available_seat_options(conn)
    selected_seat_id = request.values.get("seat_id", type=int)
    error = None
    success_student_id = None
    success_seat_id = None

    if selected_seat_id is None:
        first_available = next(
            (seat["id"] for seat in seat_options if seat["is_available"]),
            None
        )
        selected_seat_id = first_available

    selected_seat = next(
        (seat for seat in seat_options if seat["id"] == selected_seat_id),
        None
    )

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        shift = request.form.get("shift", "").strip()
        joining_date = request.form.get("joining_date", "").strip()

        if not selected_seat or not selected_seat["is_available"]:
            error = "Selected seat is not available."
        elif not name or not phone or not shift or not joining_date:
            error = "Please fill in all fields."
        elif shift not in selected_seat["available_shifts"]:
            error = "Selected shift is no longer available for this seat."
        elif get_fee_for_shift(shift) <= 0:
            error = "Selected shift is not valid."
        else:
            monthly_fee_value = get_fee_for_shift(shift)
            joining_date_value = parse_date(joining_date)

            if not phone.isdigit() or len(phone) != 10:
                error = "Phone number must be exactly 10 digits."
            elif not joining_date_value:
                error = "Joining date must be in YYYY-MM-DD format."
            elif joining_date_value > date.today():
                error = "Joining date cannot be in the future."
            elif conn.execute(
                "SELECT 1 FROM students WHERE phone = ? AND status = 'active'",
                (phone,)
            ).fetchone():
                error = "An active student with this phone number already exists."
            else:
                student_id = insert_student_record(
                    conn,
                    name,
                    phone,
                    selected_seat_id,
                    shift,
                    joining_date,
                    monthly_fee_value,
                )
                update_seat_type(conn, selected_seat_id)
                conn.commit()
                success_student_id = student_id
                success_seat_id = selected_seat_id

                seat_options = build_available_seat_options(conn)
                selected_seat = next(
                    (seat for seat in seat_options if seat["id"] == selected_seat_id),
                    None
                )

    conn.close()
    return render_template(
        "add_student.html",
        seat_options=seat_options,
        selected_seat=selected_seat,
        error=error,
        success_student_id=success_student_id,
        success_seat_id=success_seat_id,
    )

@app.route("/students")
def students():
    conn = get_db_connection()
    recent_months = recent_month_labels(4)
    selected_seat_id = request.args.get("seat_id", type=int)
    search_query = request.args.get("q", "").strip()
    search_value = search_query.lower()
    student_rows = conn.execute(
        """
        SELECT * FROM students
        ORDER BY
            CASE status WHEN 'active' THEN 0 ELSE 1 END,
            CASE WHEN seat_id IS NULL THEN 999 ELSE seat_id END,
            name
        """
    ).fetchall()
    student_summaries = [
        build_student_summary(conn, student, recent_months)
        for student in student_rows
    ]

    if search_value:
        student_summaries = [
            student for student in student_summaries
            if search_value in (student["name"] or "").lower()
            or search_value in (student["phone"] or "").lower()
            or search_value in str(student["seat_id"] or "")
        ]

    seat_groups = []
    grouped = {}

    for student in student_summaries:
        if student["seat_id"] is None:
            continue
        grouped.setdefault(student["seat_id"], []).append(student)

    for seat_id in sorted(grouped):
        seat_groups.append(
            {
                "seat_id": seat_id,
                "students": grouped[seat_id],
            }
        )

    if selected_seat_id is None and seat_groups:
        selected_seat_id = seat_groups[0]["seat_id"]

    selected_seat_group = next(
        (seat_group for seat_group in seat_groups if seat_group["seat_id"] == selected_seat_id),
        None
    )

    inactive_students = [
        student for student in student_summaries
        if student["status"] != "active" or student["seat_id"] is None
    ]
    conn.close()

    return render_template(
        "students.html",
        seat_groups=seat_groups,
        selected_seat_group=selected_seat_group,
        selected_seat_id=selected_seat_id,
        inactive_students=inactive_students,
        search_query=search_query,
    )

@app.route("/payments", methods=["GET", "POST"])
def payments():
    conn = get_db_connection()

    seats = conn.execute("SELECT * FROM seats ORDER BY id").fetchall()
    valid_seat_ids = {seat["id"] for seat in seats}
    selected_seat_id = request.values.get("seat_id", type=int)
    selected_student_id = request.values.get("student_id", type=int)
    students_in_seat = []
    selected_student = None
    unpaid_months = []
    selectable_months = []
    error = None
    success = None

    if selected_seat_id and selected_seat_id not in valid_seat_ids:
        error = "Selected seat was not found."
        selected_seat_id = None

    if selected_seat_id:
        students_in_seat = conn.execute(
            """
            SELECT * FROM students
            WHERE seat_id = ? AND status = 'active'
            ORDER BY CASE shift
                WHEN 'full' THEN 0
                WHEN 'morning' THEN 1
                WHEN 'evening' THEN 2
                ELSE 3
            END
            """,
            (selected_seat_id,)
        ).fetchall()

    if selected_student_id:
        selected_student = conn.execute(
            "SELECT * FROM students WHERE id = ? AND status = 'active'",
            (selected_student_id,)
        ).fetchone()
        if selected_student:
            selected_seat_id = selected_student["seat_id"]
            students_in_seat = conn.execute(
                """
                SELECT * FROM students
                WHERE seat_id = ? AND status = 'active'
                ORDER BY CASE shift
                    WHEN 'full' THEN 0
                    WHEN 'morning' THEN 1
                    WHEN 'evening' THEN 2
                    ELSE 3
                END
                """,
                (selected_seat_id,)
            ).fetchall()
            paid_months = {
                row["month"] for row in conn.execute(
                    "SELECT month FROM payments WHERE student_id = ?",
                    (selected_student_id,)
                ).fetchall()
            }
            due_months = get_due_months(selected_student)
            selectable_months, month_states = get_selectable_payment_months(due_months, paid_months)
            unpaid_months = [
                month for month in due_months if month_states[month] == "unpaid"
            ]
        else:
            error = "Selected student was not found."

    if request.method == "POST":
        action = request.form.get("action")

        if action == "save_payment":
            selected_months = request.form.getlist("months")
            selected_student_id = request.form.get("student_id", type=int)

            if selected_student_id:
                selected_student = conn.execute(
                    "SELECT * FROM students WHERE id = ? AND status = 'active'",
                    (selected_student_id,)
                ).fetchone()

            if not selected_student:
                error = "Please select a valid student."
            elif not selected_months:
                error = "Please select at least one unpaid month."
            else:
                paid_months = {
                    row["month"] for row in conn.execute(
                        "SELECT month FROM payments WHERE student_id = ?",
                        (selected_student_id,)
                    ).fetchall()
                }
                due_months = get_due_months(selected_student)
                selectable_months, month_states = get_selectable_payment_months(due_months, paid_months)
                expected_sequence = selectable_months[:len(selected_months)]

                if selected_months != expected_sequence:
                    error = "Payments must be saved from the oldest unpaid month in order."
                else:
                    payment_date = date.today().isoformat()
                    for month in selected_months:
                        save_payment_record(
                            conn,
                            selected_student_id,
                            month,
                            selected_student["monthly_fee"],
                            payment_date,
                        )
                    conn.commit()
                    success = "Payment saved successfully."
                    paid_months = {
                        row["month"] for row in conn.execute(
                            "SELECT month FROM payments WHERE student_id = ?",
                            (selected_student_id,)
                        ).fetchall()
                    }
                    due_months = get_due_months(selected_student)
                    selectable_months, month_states = get_selectable_payment_months(due_months, paid_months)
                    unpaid_months = [
                        month for month in due_months if month_states[month] == "unpaid"
                    ]

    conn.close()
    return render_template(
        "payments.html",
        seats=seats,
        selected_seat_id=selected_seat_id,
        students_in_seat=students_in_seat,
        selected_student=selected_student,
        unpaid_months=unpaid_months,
        selectable_months=selectable_months,
        error=error,
        success=success,
        today=date.today().isoformat(),
    )


@app.route("/student/<int:student_id>/payments")
def student_payment_history(student_id):
    conn = get_db_connection()
    student = conn.execute(
        "SELECT * FROM students WHERE id = ?",
        (student_id,)
    ).fetchone()

    if not student:
        conn.close()
        return redirect(url_for("students"))

    payment_history = build_payment_history(conn, student_id)
    due_months = get_due_months(student)
    paid_months = {item["month"] for item in payment_history}
    month_states = build_month_states(due_months, paid_months)
    unpaid_months = [
        month for month in due_months if month_states[month] == "unpaid"
    ]
    conn.close()

    return render_template(
        "student_payments.html",
        student=student,
        payment_history=payment_history,
        unpaid_months=unpaid_months,
    )



def update_seat_type(conn, seat_id):
    students = conn.execute(
        "SELECT * FROM students WHERE seat_id = ? AND status = 'active'",
        (seat_id,)
    ).fetchall()

    if len(students) == 0:
        seat_type = "empty"
    elif any(s["shift"] == "full" for s in students):
        seat_type = "full"
    else:
        seat_type = "half"

    conn.execute(
        "UPDATE seats SET type = ? WHERE id = ?",
        (seat_type, seat_id)
    )
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    if conn.backend == "postgres":
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS seats (
                id INTEGER PRIMARY KEY,
                type TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                name TEXT,
                phone TEXT,
                seat_id INTEGER REFERENCES seats(id) ON DELETE SET NULL,
                shift TEXT,
                status TEXT DEFAULT 'active',
                joining_date TEXT,
                monthly_fee DOUBLE PRECISION
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                month TEXT,
                amount DOUBLE PRECISION,
                payment_date TEXT
            )
            """
        )
    else:
        # Seats table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS seats (
            id INTEGER PRIMARY KEY,
            type TEXT
        )
        """)

        # Students table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            seat_id INTEGER,
            shift TEXT,
            status TEXT DEFAULT 'active',
            joining_date TEXT,
            monthly_fee REAL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            month TEXT,
            amount REAL,
            payment_date TEXT
        )
        """)

    ensure_schema(conn)
    ensure_login_settings(conn)

    # Insert seats if empty
    cur.execute("SELECT COUNT(*) FROM seats")
    if cur.fetchone()[0] == 0:
        for i in range(1, 31):
            if conn.backend == "postgres":
                cur.execute(
                    """
                    INSERT INTO seats (id, type) VALUES (?, ?)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (i, "empty")
                )
            else:
                cur.execute(
                    "INSERT INTO seats (id, type) VALUES (?, ?)",
                    (i, "empty")
                )

    conn.commit()
    conn.close()

init_db()

if __name__ == "__main__":
    app.run(debug=app.config["APP_ENV"] == "development")
