"""
Database helpers — thin wrappers around psycopg2.

A new connection is opened and closed per call. This is intentional:
calls are infrequent (~1-2/sec at peak during the pipeline), and a
persistent pool would add complexity without measurable benefit on a
single-process advisor running on one VM.
"""
import psycopg2
import psycopg2.extras
import os
from dotenv import load_dotenv
from datetime import date

load_dotenv('/home/ubuntu/advisor/.env')


def get_connection():
    return psycopg2.connect(
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT'),
        dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
    )


def execute(sql, params=None):
    """Run a write query (INSERT / UPDATE / DELETE). Auto-commits."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
    finally:
        conn.close()


def query(sql, params=None):
    """Run a SELECT and return all rows as a list of dicts (column name → value)."""
    conn = get_connection()
    try:
        # RealDictCursor means rows are addressed by column name, not index
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def log(level, component, message):
    """Persist a log entry to system_logs and echo it to stdout."""
    execute(
        "INSERT INTO system_logs (level, component, message) VALUES (%s, %s, %s)",
        (level, component, message),
    )
    print(f"[{level.upper()}] {component}: {message}")


def track_api_call(api_name):
    execute("""
        INSERT INTO api_usage (api_name, date, call_count)
        VALUES (%s, %s, 1)
        ON CONFLICT (api_name, date)
        DO UPDATE SET call_count = api_usage.call_count + 1
    """, (api_name, date.today()))


def get_api_calls_today(api_name):
    result = query(
        "SELECT call_count FROM api_usage WHERE api_name=%s AND date=%s",
        (api_name, date.today()),
    )
    return result[0]['call_count'] if result else 0


def get_setting(key, default):
    """Read a user_settings value, cast to the same type as default. Falls back to default on any error."""
    try:
        rows = query("SELECT value FROM user_settings WHERE key=%s", (key,))
        if rows and rows[0]['value'] not in (None, ''):
            return type(default)(rows[0]['value'])
    except Exception:
        pass
    return default


def can_use_alpha_vantage():
    """Alpha Vantage free tier caps at 25 req/day; we stay under with a 22-call limit."""
    limit = int(os.getenv('MAX_ALPHA_VANTAGE_CALLS', 22))
    used  = get_api_calls_today('alpha_vantage')
    return used < limit
