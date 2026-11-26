"""Postgres-backed accounts and job artifacts for single-instance cloud deployment."""
import os
from pathlib import Path

DATABASE_URL = os.environ.get('DATABASE_URL', '')
CLOUD = os.environ.get('WORKBENCH_CLOUD') == '1'
ROOT = Path(os.environ.get('WORKBENCH_DATA_DIR', Path(__file__).resolve().parent)).resolve()


def connect():
    if not DATABASE_URL:
        raise RuntimeError('云端必须设置 DATABASE_URL')
    import psycopg
    # Require both certificate and hostname validation regardless of URI parameters.
    return psycopg.connect(DATABASE_URL, sslmode='verify-full', connect_timeout=15)


def initialize():
    if not CLOUD:
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    with connect() as db, db.cursor() as cur:
        cur.execute('CREATE TABLE IF NOT EXISTS wb_kv (key TEXT PRIMARY KEY, data BYTEA NOT NULL)')
        cur.execute('CREATE TABLE IF NOT EXISTS wb_audit (id BIGSERIAL PRIMARY KEY, event JSONB NOT NULL)')


def get(key):
    with connect() as db, db.cursor() as cur:
        cur.execute('SELECT data FROM wb_kv WHERE key = %s', (key,))
        row = cur.fetchone()
        return bytes(row[0]) if row else None


def put(key, data):
    with connect() as db, db.cursor() as cur:
        cur.execute('INSERT INTO wb_kv(key,data) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET data=EXCLUDED.data', (key, data))


def save_job_file(path):
    if not CLOUD:
        return
    path = Path(path).resolve()
    rel = path.relative_to((ROOT / 'jobs').resolve())
    put('jobs/' + rel.as_posix(), path.read_bytes())


def restore_jobs():
    if not CLOUD:
        return
    base = (ROOT / 'jobs').resolve()
    with connect() as db, db.cursor() as cur:
        cur.execute("SELECT key,data FROM wb_kv WHERE key LIKE 'jobs/%%'")
        for key, content in cur:
            target = (base / key[len('jobs/'):]).resolve()
            target.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(content))


def audit(event):
    if not CLOUD:
        return
    import json
    with connect() as db, db.cursor() as cur:
        cur.execute('INSERT INTO wb_audit(event) VALUES (%s::jsonb)', (json.dumps(event, ensure_ascii=False),))
