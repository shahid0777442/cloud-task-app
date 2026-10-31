"""Cloud Task Manager - Flask + PostgreSQL (+ optional S3 uploads).

Config comes from environment variables. The DB password comes from AWS
Secrets Manager in the cloud, or DB_USER/DB_PASSWORD env vars for local dev.
Secrets are never logged.
"""
import json
import logging
import os
import sys
import uuid
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from flask import Flask, abort, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

# ---------- Logging (stdout -> journald -> CloudWatch) ----------
logging.basicConfig(
    stream=sys.stdout,
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("taskapp")

# ---------- Config ----------
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "appdb")
DB_SECRET_NAME = os.getenv("DB_SECRET_NAME")  # set in cloud
DB_SSLMODE = os.getenv("DB_SSLMODE", "prefer")  # use "require" on RDS
S3_BUCKET = os.getenv("S3_BUCKET")  # optional
ALLOWED_EXT = {"png", "jpg", "jpeg", "pdf", "txt"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload limit

_creds_cache = None
_s3 = None


def get_db_creds():
    """Fetch DB credentials from Secrets Manager (cloud) or env (local)."""
    global _creds_cache
    if _creds_cache:
        return _creds_cache
    if DB_SECRET_NAME:
        import boto3
        sm = boto3.client("secretsmanager", region_name=AWS_REGION)
        secret = sm.get_secret_value(SecretId=DB_SECRET_NAME)
        _creds_cache = json.loads(secret["SecretString"])
        log.info("DB credentials loaded from Secrets Manager")
    else:
        _creds_cache = {
            "username": os.getenv("DB_USER", "postgres"),
            "password": os.getenv("DB_PASSWORD", ""),
        }
        log.info("DB credentials loaded from environment (local mode)")
    return _creds_cache


def s3_client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=AWS_REGION)
    return _s3


@contextmanager
def db():
    """Yield a cursor; commit on success, rollback on error, always close."""
    creds = get_db_creds()
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=creds["username"], password=creds["password"],
        sslmode=DB_SSLMODE, connect_timeout=5,
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                done BOOLEAN NOT NULL DEFAULT FALSE,
                file_key TEXT,
                file_name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    log.info("Database schema ready")


def allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


# ---------- Routes ----------
@app.route("/health")
def health():
    """Liveness check (no DB). Use for load balancer health checks."""
    return {"status": "ok"}


@app.route("/health/db")
def health_db():
    """Readiness check (verifies DB connectivity)."""
    try:
        with db() as cur:
            cur.execute("SELECT 1")
        return {"status": "ok", "database": "up"}
    except Exception:
        log.exception("DB health check failed")
        return {"status": "error", "database": "down"}, 503


@app.route("/")
def index():
    with db() as cur:
        cur.execute("SELECT * FROM tasks ORDER BY done, created_at DESC")
        tasks = cur.fetchall()
    return render_template("index.html", tasks=tasks, s3_enabled=bool(S3_BUCKET))


@app.route("/tasks", methods=["POST"])
def add_task():
    title = request.form.get("title", "").strip()
    if not title or len(title) > 200:
        flash("Title 1 se 200 characters ka hona chahiye.")
        return redirect(url_for("index"))
    with db() as cur:
        cur.execute("INSERT INTO tasks (title) VALUES (%s)", (title,))
    log.info("Task created")
    return redirect(url_for("index"))


@app.route("/tasks/<int:task_id>/toggle", methods=["POST"])
def toggle_task(task_id):
    with db() as cur:
        cur.execute("UPDATE tasks SET done = NOT done WHERE id = %s", (task_id,))
    return redirect(url_for("index"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    with db() as cur:
        cur.execute("SELECT file_key FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    if row and row["file_key"] and S3_BUCKET:
        s3_client().delete_object(Bucket=S3_BUCKET, Key=row["file_key"])
    log.info("Task %s deleted", task_id)
    return redirect(url_for("index"))


@app.route("/tasks/<int:task_id>/upload", methods=["POST"])
def upload(task_id):
    if not S3_BUCKET:
        flash("S3_BUCKET set nahi hai, upload disabled hai.")
        return redirect(url_for("index"))
    f = request.files.get("file")
    if not f or not f.filename or not allowed(f.filename):
        flash("File type allowed nahi: " + ", ".join(sorted(ALLOWED_EXT)))
        return redirect(url_for("index"))
    name = secure_filename(f.filename)
    key = f"uploads/{uuid.uuid4().hex}-{name}"
    s3_client().upload_fileobj(
        f, S3_BUCKET, key, ExtraArgs={"ServerSideEncryption": "AES256"}
    )
    with db() as cur:
        cur.execute(
            "UPDATE tasks SET file_key = %s, file_name = %s WHERE id = %s",
            (key, name, task_id),
        )
    log.info("File uploaded to s3 for task %s", task_id)
    return redirect(url_for("index"))


@app.route("/tasks/<int:task_id>/file")
def download(task_id):
    """Redirect to a short-lived presigned URL (bucket stays private)."""
    with db() as cur:
        cur.execute("SELECT file_key FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    if not row or not row["file_key"] or not S3_BUCKET:
        abort(404)
    url = s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": row["file_key"]},
        ExpiresIn=300,
    )
    return redirect(url)


@app.errorhandler(413)
def too_large(_):
    flash("File 5 MB se choti honi chahiye.")
    return redirect(url_for("index"))


@app.errorhandler(500)
def server_error(_):
    return "Internal server error", 500


# Create tables on startup (safe: IF NOT EXISTS). Never crash the app on boot.
try:
    init_db()
except Exception:
    log.exception("init_db failed - check DB settings; /health/db will report status")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=os.getenv("FLASK_ENV") == "development")
