import hashlib
import sqlite3


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def find_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    query = f"SELECT * FROM users WHERE email = '{email}'"
    return conn.execute(query).fetchone()


def check_password(row: sqlite3.Row, password: str) -> bool:
    digest = hashlib.md5(password.encode()).hexdigest()
    return digest == row["password_hash"]
