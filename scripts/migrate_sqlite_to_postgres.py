import os
import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "database.db"


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def connect_sqlite(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def connect_postgres(url):
    return psycopg.connect(url, row_factory=dict_row)


def create_schema():
    database_url = require_env("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("STUDY_HALL_ENV", "production")
    import app  # noqa: F401


def sync_table(source_conn, target_conn, select_sql, upsert_sql, row_mapper):
    rows = source_conn.execute(select_sql).fetchall()
    for row in rows:
        target_conn.execute(upsert_sql, row_mapper(row))


def reset_sequence(target_conn, table_name):
    target_conn.execute(
        """
        SELECT setval(
            pg_get_serial_sequence(%s, 'id'),
            COALESCE((SELECT MAX(id) FROM """ + table_name + """), 1),
            true
        )
        """,
        (table_name,),
    )


def main():
    sqlite_path = Path(os.environ.get("SQLITE_DB_PATH", str(DEFAULT_SQLITE_PATH)))
    if not sqlite_path.exists():
        raise RuntimeError(f"SQLite database not found: {sqlite_path}")

    database_url = require_env("DATABASE_URL")
    create_schema()

    source_conn = connect_sqlite(sqlite_path)
    target_conn = connect_postgres(database_url)

    try:
        sync_table(
            source_conn,
            target_conn,
            "SELECT id, type FROM seats ORDER BY id",
            """
            INSERT INTO seats (id, type)
            VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET type = EXCLUDED.type
            """,
            lambda row: (row["id"], row["type"]),
        )
        sync_table(
            source_conn,
            target_conn,
            """
            SELECT id, name, phone, seat_id, shift, status, joining_date, monthly_fee
            FROM students
            ORDER BY id
            """,
            """
            INSERT INTO students (id, name, phone, seat_id, shift, status, joining_date, monthly_fee)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                phone = EXCLUDED.phone,
                seat_id = EXCLUDED.seat_id,
                shift = EXCLUDED.shift,
                status = EXCLUDED.status,
                joining_date = EXCLUDED.joining_date,
                monthly_fee = EXCLUDED.monthly_fee
            """,
            lambda row: (
                row["id"],
                row["name"],
                row["phone"],
                row["seat_id"],
                row["shift"],
                row["status"],
                row["joining_date"],
                row["monthly_fee"],
            ),
        )
        sync_table(
            source_conn,
            target_conn,
            """
            SELECT id, student_id, month, amount, payment_date
            FROM payments
            ORDER BY id
            """,
            """
            INSERT INTO payments (id, student_id, month, amount, payment_date)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                student_id = EXCLUDED.student_id,
                month = EXCLUDED.month,
                amount = EXCLUDED.amount,
                payment_date = EXCLUDED.payment_date
            """,
            lambda row: (
                row["id"],
                row["student_id"],
                row["month"],
                row["amount"],
                row["payment_date"],
            ),
        )
        sync_table(
            source_conn,
            target_conn,
            "SELECT key, value FROM settings ORDER BY key",
            """
            INSERT INTO settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            lambda row: (row["key"], row["value"]),
        )

        reset_sequence(target_conn, "students")
        reset_sequence(target_conn, "payments")
        target_conn.commit()
        print(f"Migration completed from {sqlite_path} to PostgreSQL.")
    except Exception:
        target_conn.rollback()
        raise
    finally:
        source_conn.close()
        target_conn.close()


if __name__ == "__main__":
    main()
