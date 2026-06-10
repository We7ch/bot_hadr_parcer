import json
import hashlib
import asyncio
import os
from datetime import datetime

import asyncpg
from dotenv import load_dotenv


load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
DB_RETRY_ATTEMPTS = 5
DB_CONNECT_TIMEOUT = 8


INIT_SQL = [
    """
    CREATE TABLE IF NOT EXISTS resumes (
        hh_resume_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        skills JSONB NOT NULL DEFAULT '[]'::jsonb,
        experience TEXT,
        salary INTEGER,
        raw_data JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_requests (
        id BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL,
        vacancy_description TEXT NOT NULL,
        result_summary TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidates (
        candidate_key TEXT PRIMARY KEY,
        telegram_id BIGINT,
        search_query TEXT NOT NULL,
        name TEXT,
        role TEXT,
        salary_text TEXT,
        status TEXT,
        updated_text TEXT,
        profile_url TEXT,
        skills JSONB NOT NULL DEFAULT '[]'::jsonb,
        raw_data JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
]


def _build_connection_args():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set in .env")

    return {
        "dsn": DATABASE_URL,
        "statement_cache_size": 0,
        "timeout": DB_CONNECT_TIMEOUT,
    }


def _is_retryable_db_error(error: Exception) -> bool:
    return isinstance(
        error,
        (
            asyncpg.PostgresConnectionError,
            asyncpg.ConnectionDoesNotExistError,
            asyncpg.InterfaceError,
            ConnectionError,
            OSError,
            TimeoutError,
            asyncio.TimeoutError,
        ),
    )


async def _connect_with_retry():
    last_error = None
    for attempt in range(DB_RETRY_ATTEMPTS):
        try:
            return await asyncpg.connect(**_build_connection_args())
        except Exception as e:
            if not _is_retryable_db_error(e):
                raise
            last_error = e
            if attempt < DB_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(1.5 + attempt)
    raise last_error


async def _close_connection(conn):
    try:
        await asyncio.wait_for(conn.close(), timeout=3)
    except Exception:
        conn.terminate()


async def _run_with_connection(operation):
    last_error = None
    for attempt in range(DB_RETRY_ATTEMPTS):
        conn = None
        try:
            conn = await _connect_with_retry()
            return await operation(conn)
        except Exception as e:
            if not _is_retryable_db_error(e):
                raise
            last_error = e
            if attempt < DB_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(1.5 + attempt)
        finally:
            if conn is not None:
                await _close_connection(conn)
    raise last_error


async def init_db():
    """Check database connection."""
    try:
        await _run_with_connection(lambda conn: conn.fetchval("SELECT 1"))
        for sql in INIT_SQL:
            await _run_with_connection(lambda conn, sql=sql: conn.execute(sql))
        print("DB connection is OK")
        return True
    except Exception as e:
        print(f"DB connection error: {type(e).__name__}: {e}")
        return False


class _ConnectionContext:
    def __init__(self):
        self._conn = None

    async def __aenter__(self):
        self._conn = await _connect_with_retry()
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        if self._conn is not None:
            await _close_connection(self._conn)


def _get_connection():
    return _ConnectionContext()


async def save_resume(pool, hh_resume_id: str, title: str, skills: list,
                      experience: str, salary: int, raw_data: dict):
    async def operation(conn):
        await conn.execute("""
            INSERT INTO resumes (
                hh_resume_id,
                title,
                skills,
                experience,
                salary,
                raw_data,
                updated_at
            )
            VALUES ($1, $2, $3::jsonb, $4, $5, $6::jsonb, $7)
            ON CONFLICT (hh_resume_id)
            DO UPDATE SET
                title = EXCLUDED.title,
                skills = EXCLUDED.skills,
                experience = EXCLUDED.experience,
                salary = EXCLUDED.salary,
                raw_data = EXCLUDED.raw_data,
                updated_at = EXCLUDED.updated_at
        """,
        hh_resume_id,
        title,
        json.dumps(skills, ensure_ascii=False),
        experience,
        salary,
        json.dumps(raw_data, ensure_ascii=False),
        datetime.now()
        )
    await _run_with_connection(operation)


async def get_resume(pool, hh_resume_id: str):
    """Get cached resume by hh.ru resume ID."""
    async def operation(conn):
        return await conn.fetchrow(
            "SELECT * FROM resumes WHERE hh_resume_id = $1",
            hh_resume_id,
        )
    return await _run_with_connection(operation)


async def save_user_request(pool, telegram_id: int, vacancy_description: str,
                            result_summary: str):
    """Save user request history."""
    async def operation(conn):
        await conn.execute("""
            INSERT INTO user_requests (telegram_id, vacancy_description, result_summary)
            VALUES ($1, $2, $3)
        """, telegram_id, vacancy_description, result_summary)
    await _run_with_connection(operation)


def build_candidate_key(candidate: dict, search_query: str) -> str:
    profile_url = (candidate.get("profile_url") or "").strip()
    if profile_url:
        return profile_url

    payload = "|".join(
        [
            search_query.strip().lower(),
            (candidate.get("name") or "").strip().lower(),
            (candidate.get("role") or "").strip().lower(),
            (candidate.get("salary_text") or "").strip().lower(),
            (candidate.get("status") or "").strip().lower(),
            (candidate.get("updated_text") or "").strip().lower(),
            ",".join(candidate.get("skills") or []).strip().lower(),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"habr_candidate_{digest}"


def _candidate_row(candidate: dict, telegram_id: int | None, search_query: str):
    raw_data = {key: value for key, value in candidate.items() if key != "raw_html"}
    return (
        build_candidate_key(candidate, search_query),
        telegram_id,
        search_query,
        candidate.get("name") or "",
        candidate.get("role") or "",
        candidate.get("salary_text") or "",
        candidate.get("status") or "",
        candidate.get("updated_text") or "",
        candidate.get("profile_url") or "",
        json.dumps(candidate.get("skills") or [], ensure_ascii=False),
        json.dumps(raw_data, ensure_ascii=False),
        datetime.now(),
    )


async def save_candidate(
    pool,
    *,
    telegram_id: int | None,
    search_query: str,
    candidate: dict,
):
    """Save one found Habr candidate."""
    await save_candidates(
        pool,
        telegram_id=telegram_id,
        search_query=search_query,
        candidates=[candidate],
    )


async def save_candidates(
    pool,
    *,
    telegram_id: int | None,
    search_query: str,
    candidates: list[dict],
) -> int:
    """Save found Habr candidates in one retryable batch."""
    if not candidates:
        return 0

    rows = [_candidate_row(candidate, telegram_id, search_query) for candidate in candidates]

    async def operation(conn):
        await conn.executemany(
            """
            INSERT INTO candidates (
                candidate_key,
                telegram_id,
                search_query,
                name,
                role,
                salary_text,
                status,
                updated_text,
                profile_url,
                skills,
                raw_data,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12)
            ON CONFLICT (candidate_key)
            DO UPDATE SET
                telegram_id = EXCLUDED.telegram_id,
                search_query = EXCLUDED.search_query,
                name = EXCLUDED.name,
                role = EXCLUDED.role,
                salary_text = EXCLUDED.salary_text,
                status = EXCLUDED.status,
                updated_text = EXCLUDED.updated_text,
                profile_url = EXCLUDED.profile_url,
                skills = EXCLUDED.skills,
                raw_data = EXCLUDED.raw_data,
                updated_at = EXCLUDED.updated_at
            """,
            rows,
        )

    await _run_with_connection(operation)
    return len(rows)


async def get_user_history(pool, telegram_id: int, limit: int = 10):
    """Get recent user requests."""
    async def operation(conn):
        return await conn.fetch("""
            SELECT vacancy_description, result_summary, created_at
            FROM user_requests
            WHERE telegram_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """, telegram_id, limit)
    return await _run_with_connection(operation)
