#!/usr/bin/env python3
"""
Offline desktop backend for the Robotics Team Hub.

A single-file, self-contained FastAPI server that stores everything
locally (SQLite + filesystem) so the app can run without internet,
without installing anything, and without admin rights.

Run from source:
    python main.py

Package with PyInstaller:
    pyinstaller --onefile --name backend --clean --noconsole main.py
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import bcrypt
import jwt
import uvicorn
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import Response as FastResponse
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

# PyInstaller needs multiprocessing freeze support on Windows.
from multiprocessing import freeze_support

freeze_support()

# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------
APP_NAME = "RoboticsHub"
DEFAULT_PORT = int(os.environ.get("RH_PORT", "54114"))
JWT_ALGORITHM = "HS256"


def _get_env_secret(name: str, fallback: str) -> str:
    value = os.environ.get(name, "")
    return value if value else fallback


OWNER_EMAIL = _get_env_secret("OWNER_EMAIL", "owner@robohub.local").lower()
OWNER_PASSWORD = _get_env_secret("OWNER_PASSWORD", "Robotics2026!")
OWNER_NAME = _get_env_secret("OWNER_NAME", "Team Owner")

# Generate a stable JWT secret for this install. In portable mode the data
# directory stores it, so it survives between launches.

def get_data_dir() -> Path:
    """Return a writable directory for the SQLite database and uploaded files."""
    if os.environ.get("RH_DATA_DIR"):
        return Path(os.environ["RH_DATA_DIR"]).resolve()

    # If packaged with PyInstaller, prefer a 'data' folder next to the EXE.
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        return exe_dir / "data"

    # Development fallback.
    return Path(__file__).parent / "data"


def ensure_data_dir() -> Path:
    """Create the data directory, falling back to %LOCALAPPDATA% if not writable."""
    path = get_data_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
        # Try to write a sentinel to confirm write access.
        sentinel = path / ".writable"
        sentinel.write_text("ok", encoding="utf-8")
        return path
    except OSError:
        fallback = Path.home() / "AppData" / "Local" / APP_NAME / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = ensure_data_dir()
DB_PATH = DATA_DIR / "app.db"
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

JWT_SECRET_PATH = DATA_DIR / ".jwt_secret"
if JWT_SECRET_PATH.exists():
    JWT_SECRET = JWT_SECRET_PATH.read_text(encoding="utf-8").strip()
else:
    JWT_SECRET = secrets.token_urlsafe(32)
    JWT_SECRET_PATH.write_text(JWT_SECRET, encoding="utf-8")

LOG_PATH = DATA_DIR / "backend.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("robohub-backend")


# ---------------------------------------------------------------------------
# Content filtering
# ---------------------------------------------------------------------------
_BLOCKED_WORDS = {
    "fuck", "shit", "ass", "bitch", "damn", "hell", "crap", "dick", "cock",
    "pussy", "whore", "slut", "bastard", "motherfucker", "fucker", "asshole",
    "bullshit", "dumbass", "jackass", "piss", "cunt", "twat", "wanker",
    "nigger", "nigga", "faggot", "retard", "retarded",
    "porn", "hentai", "nude", "nudes", "naked", "xxx", "nsfw", "onlyfans",
    "blowjob", "handjob", "dildo", "orgasm", "masturbat", "cumshot", "creampie",
    "anal", "bondage", "fetish", "kinky", "horny", "sexy", "sexting",
}

_BLOCKED_FILE_EXTENSIONS = {".exe", ".bat", ".cmd", ".scr", ".com"}


def _contains_blocked_content(text: str) -> Optional[str]:
    """Check for blocked words. Matches whole words only so common words like
    'assemble', 'hello' or 'scent' are not falsely blocked."""
    if not text:
        return None
    lower = text.lower()
    words = re.split(r"[\s\W_]+", lower)
    for w in words:
        if w and w in _BLOCKED_WORDS:
            return w
    return None


# ---------------------------------------------------------------------------
# Channel definitions
# ---------------------------------------------------------------------------
PROGRAM_CHANNELS = {
    "vex": {"label": "VEX", "subs": [("general", "General")]},
    "frc": {
        "label": "FRC",
        "subs": [
            ("programming", "Programming"),
            ("building", "Building"),
            ("business", "Business"),
            ("team", "Team Chat"),
            ("design", "Design"),
        ],
    },
}

CHANNELS = []
for pkey, cfg in PROGRAM_CHANNELS.items():
    for ckey, clabel in cfg["subs"]:
        CHANNELS.append({
            "id": f"{pkey}-{ckey}",
            "name": clabel,
            "program": pkey,
            "program_label": cfg["label"],
            "private": False,
        })
CHANNELS.append({
    "id": "members-only",
    "name": "Members Only",
    "program": "private",
    "program_label": "Private",
    "private": True,
})
CHANNEL_IDS = {c["id"] for c in CHANNELS}


# ---------------------------------------------------------------------------
# Permission model
# ---------------------------------------------------------------------------
PERMISSION_KEYS = {
    "can_chat",
    "can_upload_files",
    "can_view_members_only",
    "can_edit_calendar",
    "can_manage_todos",
    "can_delete_any_message",
    "can_delete_any_file",
    "can_manage_members",
}

DEFAULT_ROLE_PERMISSIONS = {
    "owner": {k: True for k in PERMISSION_KEYS},
    "mentor": {
        "can_chat": True,
        "can_upload_files": True,
        "can_view_members_only": False,
        "can_edit_calendar": True,
        "can_manage_todos": True,
        "can_delete_any_message": True,
        "can_delete_any_file": True,
        "can_manage_members": False,
    },
    "member": {
        "can_chat": True,
        "can_upload_files": True,
        "can_view_members_only": True,
        "can_edit_calendar": False,
        "can_manage_todos": False,
        "can_delete_any_message": False,
        "can_delete_any_file": False,
        "can_manage_members": False,
    },
}


def get_permissions(user: dict) -> dict:
    """Return effective permissions, merging stored overrides with role defaults."""
    role = user.get("role", "member")
    defaults = DEFAULT_ROLE_PERMISSIONS.get(role, DEFAULT_ROLE_PERMISSIONS["member"]).copy()
    if role == "owner":
        # Owner permissions are always full and cannot be reduced.
        return defaults
    stored = user.get("permissions")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except Exception:
            stored = {}
    if isinstance(stored, dict):
        for k, v in stored.items():
            if k in defaults and isinstance(v, bool):
                defaults[k] = v
    return defaults


def has_permission(user: dict, key: str) -> bool:
    return get_permissions(user).get(key, False)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _file_download_token(file_id: str, user_id: str) -> str:
    payload = {
        "type": "file_download",
        "file_id": file_id,
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def serialize_user(row: aiosqlite.Row | dict) -> dict:
    if isinstance(row, aiosqlite.Row):
        row = dict(row)
    perms = get_permissions(row)
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "status": row["status"],
        "phone": row.get("phone", "") or "",
        "carrier": row.get("carrier", "") or "",
        "email_notifications": bool(row.get("email_notifications", True)),
        "sms_notifications": bool(row.get("sms_notifications", False)),
        "permissions": perms,
        "created_at": row.get("created_at"),
    }


async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def get_current_user(request: Request, db=Depends(get_db)) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = await fetch_user(db, user_id=payload["sub"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("status") == "pending":
        raise HTTPException(status_code=403, detail="Your account is awaiting owner approval")
    return user


def require_roles(*roles):
    async def checker(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return checker


def require_permission(key: str):
    async def checker(user: dict = Depends(get_current_user)) -> dict:
        if not has_permission(user, key):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return checker


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
async def fetch_user(db: aiosqlite.Connection, *, user_id: Optional[str] = None, email: Optional[str] = None):
    if user_id:
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
    elif email:
        async with db.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)) as cursor:
            row = await cursor.fetchone()
    else:
        return None
    return dict(row) if row else None


async def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                status TEXT NOT NULL DEFAULT 'pending',
                phone TEXT DEFAULT '',
                carrier TEXT DEFAULT '',
                email_notifications INTEGER DEFAULT 1,
                sms_notifications INTEGER DEFAULT 0,
                permissions TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                user_role TEXT NOT NULL,
                text TEXT NOT NULL,
                attachment TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dm_messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                sender_role TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                text TEXT NOT NULL,
                attachment TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                storage_path TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                kind TEXT NOT NULL,
                uploaded_by TEXT NOT NULL,
                uploader_name TEXT NOT NULL,
                is_deleted INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                date TEXT NOT NULL,
                location TEXT DEFAULT '',
                created_by TEXT,
                creator_name TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                deadline TEXT,
                assigned_to TEXT,
                created_by TEXT NOT NULL,
                creator_name TEXT NOT NULL,
                completed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                subscription TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, endpoint)
            )
        """)
        await db.commit()

    # Seed owner if missing.
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        owner = await fetch_user(db, email=OWNER_EMAIL)
        if not owner:
            uid = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """
                INSERT INTO users (id, email, password_hash, name, role, status, phone, carrier,
                                   email_notifications, sms_notifications, permissions, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    OWNER_EMAIL,
                    hash_password(OWNER_PASSWORD),
                    OWNER_NAME,
                    "owner",
                    "approved",
                    "",
                    "",
                    1,
                    0,
                    json.dumps(DEFAULT_ROLE_PERMISSIONS["owner"]),
                    now,
                ),
            )
            await db.commit()
            logger.info("Owner account seeded: %s", OWNER_EMAIL)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "member"


class LoginRequest(BaseModel):
    email: str
    password: str


class MessageCreate(BaseModel):
    text: str
    attachment_file_id: Optional[str] = None


class EventCreate(BaseModel):
    title: str
    description: str = ""
    date: str
    location: str = ""


class SettingsUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    carrier: Optional[str] = None
    email_notifications: Optional[bool] = None
    sms_notifications: Optional[bool] = None


class PushSubscription(BaseModel):
    endpoint: str
    keys: dict


class PushUnsubscribe(BaseModel):
    endpoint: str


class RoleUpdate(BaseModel):
    role: str


class CreateUserRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "member"


class ApproveRequest(BaseModel):
    role: str = "member"


class TodoCreate(BaseModel):
    title: str
    description: str = ""
    deadline: Optional[str] = None
    assigned_to: Optional[str] = None


class TodoUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    deadline: Optional[str] = None
    assigned_to: Optional[str] = None
    completed: Optional[bool] = None


class PermissionsUpdate(BaseModel):
    permissions: dict[str, bool]


# ---------------------------------------------------------------------------
# App / lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Backend data directory: %s", DATA_DIR)
    logger.info("Listening on http://127.0.0.1:%s", DEFAULT_PORT)
    yield
    logger.info("Backend shutting down")


app = FastAPI(lifespan=lifespan)
api_router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def channel_visible_to(channel_id: str, user: dict) -> bool:
    if channel_id not in CHANNEL_IDS:
        return False
    if channel_id == "members-only":
        return user.get("role") == "owner" or has_permission(user, "can_view_members_only")
    return True


def classify_file(filename: str, content_type: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if content_type.startswith("image/") or ext in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        return "image"
    if ext == "zip" or content_type in ("application/zip", "application/x-zip-compressed"):
        return "zip"
    return "code"


async def _resolve_file_download_user(file_id: str, request: Request, download_token: Optional[str], db: aiosqlite.Connection):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return await get_current_user(request, db=db)
    if not download_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(download_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Download token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid download token")
    if payload.get("type") != "file_download" or payload.get("file_id") != file_id:
        raise HTTPException(status_code=401, detail="Invalid download token")
    user = await fetch_user(db, user_id=payload.get("user_id"))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("status") == "pending":
        raise HTTPException(status_code=403, detail="Your account is awaiting owner approval")
    return user


async def _file_attachment(db: aiosqlite.Connection, file_id: Optional[str]):
    if not file_id:
        return None
    async with db.execute(
        "SELECT * FROM files WHERE id = ? AND is_deleted = 0",
        (file_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    f = dict(row)
    return {
        "file_id": f["id"],
        "filename": f["original_filename"],
        "content_type": f["content_type"],
        "kind": f["kind"],
    }


def dm_conversation_id(a: str, b: str) -> str:
    return "dm:" + ":".join(sorted([a, b]))


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@api_router.post("/auth/register")
async def register(req: RegisterRequest, response: Response, db=Depends(get_db)):
    email = req.email.lower().strip()
    if email == OWNER_EMAIL:
        raise HTTPException(status_code=400, detail="This email is reserved. Please sign in instead.")
    async with db.execute("SELECT 1 FROM users WHERE email = ?", (email,)) as cursor:
        if await cursor.fetchone():
            raise HTTPException(status_code=400, detail="Email already registered")
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO users (id, email, password_hash, name, role, status, phone, carrier,
                           email_notifications, sms_notifications, permissions, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            email,
            hash_password(req.password),
            req.name.strip() or email.split("@")[0],
            "member",
            "pending",
            "",
            "",
            1,
            0,
            "{}",
            now,
        ),
    )
    await db.commit()
    return {
        "pending": True,
        "message": "Your request to join has been sent. You'll be able to sign in once the team owner approves your account.",
    }


@api_router.post("/auth/login")
async def login(req: LoginRequest, response: Response, db=Depends(get_db)):
    email = req.email.lower().strip()
    user = await fetch_user(db, email=email)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.get("status") == "pending":
        raise HTTPException(status_code=403, detail="Your account is awaiting owner approval. You'll be able to sign in once approved.")
    token = create_access_token(user["id"], email)
    response.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=604800, path="/")
    return {"token": token, "user": serialize_user(user)}


@api_router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    return {"ok": True}


@api_router.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return serialize_user(user)


@api_router.put("/auth/me/settings")
async def update_settings(req: SettingsUpdate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    updates = {}
    if req.name is not None:
        updates["name"] = req.name.strip()
    if req.phone is not None:
        updates["phone"] = req.phone.strip()
    if req.carrier is not None:
        updates["carrier"] = req.carrier.strip()
    if req.email_notifications is not None:
        updates["email_notifications"] = 1 if req.email_notifications else 0
    if req.sms_notifications is not None:
        updates["sms_notifications"] = 1 if req.sms_notifications else 0

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [user["id"]]
        await db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        await db.commit()

    updated = await fetch_user(db, user_id=user["id"])
    return serialize_user(updated)


@api_router.delete("/auth/me")
async def delete_own_account(user: dict = Depends(get_current_user), db=Depends(get_db)):
    if user.get("role") == "owner":
        raise HTTPException(status_code=400, detail="The owner account cannot be self-deleted")
    await _delete_user_data(user["id"], db)
    return {"ok": True}


async def _delete_user_data(user_id: str, db: aiosqlite.Connection):
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM dm_messages WHERE sender_id = ? OR recipient_id = ?", (user_id, user_id))
    await db.execute("UPDATE files SET is_deleted = 1 WHERE uploaded_by = ?", (user_id,))
    await db.execute("UPDATE events SET created_by = NULL, creator_name = 'Deleted user' WHERE created_by = ?", (user_id,))
    await db.commit()


# ---------------------------------------------------------------------------
# Channels / messages
# ---------------------------------------------------------------------------
@api_router.get("/channels")
async def list_channels(user: dict = Depends(get_current_user)):
    return [c for c in CHANNELS if channel_visible_to(c["id"], user)]


@api_router.get("/channels/{channel_id}/messages")
async def get_messages(channel_id: str, user: dict = Depends(get_current_user), db=Depends(get_db)):
    if channel_id not in CHANNEL_IDS:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel_visible_to(channel_id, user):
        raise HTTPException(status_code=403, detail="You don't have access to this channel")
    async with db.execute(
        "SELECT * FROM messages WHERE channel_id = ? ORDER BY created_at ASC",
        (channel_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_serialize_message(dict(r)) for r in rows]


def _serialize_message(m: dict) -> dict:
    return {
        "id": m["id"],
        "channel_id": m.get("channel_id"),
        "user_id": m["user_id"],
        "user_name": m["user_name"],
        "user_role": m["user_role"],
        "text": m["text"],
        "attachment": json.loads(m["attachment"]) if m.get("attachment") else None,
        "created_at": m["created_at"],
    }


@api_router.post("/channels/{channel_id}/messages")
async def post_message(channel_id: str, req: MessageCreate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    if channel_id not in CHANNEL_IDS:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel_visible_to(channel_id, user):
        raise HTTPException(status_code=403, detail="You don't have access to this channel")
    if not has_permission(user, "can_chat"):
        raise HTTPException(status_code=403, detail="You do not have permission to post messages")
    blocked = _contains_blocked_content(req.text)
    if blocked:
        raise HTTPException(status_code=400, detail="Message contains inappropriate content and cannot be sent.")
    attachment = await _file_attachment(db, req.attachment_file_id)
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO messages (id, channel_id, user_id, user_name, user_role, text, attachment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, channel_id, user["id"], user.get("name", ""), user.get("role", "member"), req.text, json.dumps(attachment) if attachment else None, now),
    )
    await db.commit()
    return {
        "id": uid,
        "channel_id": channel_id,
        "user_id": user["id"],
        "user_name": user.get("name", ""),
        "user_role": user.get("role", "member"),
        "text": req.text,
        "attachment": attachment,
        "created_at": now,
    }


@api_router.delete("/channels/{channel_id}/messages/{message_id}")
async def delete_message(channel_id: str, message_id: str, user: dict = Depends(get_current_user), db=Depends(get_db)):
    async with db.execute("SELECT * FROM messages WHERE id = ? AND channel_id = ?", (message_id, channel_id)) as cursor:
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    msg = dict(row)
    if msg["user_id"] != user["id"] and not has_permission(user, "can_delete_any_message"):
        raise HTTPException(status_code=403, detail="Not allowed to delete this message")
    await db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    await db.commit()
    return {"ok": True}


@api_router.delete("/dm/{other_id}/messages/{message_id}")
async def delete_dm_message(other_id: str, message_id: str, user: dict = Depends(get_current_user), db=Depends(get_db)):
    async with db.execute("SELECT * FROM dm_messages WHERE id = ?", (message_id,)) as cursor:
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    msg = dict(row)
    if msg["sender_id"] != user["id"] and not has_permission(user, "can_delete_any_message"):
        raise HTTPException(status_code=403, detail="Not allowed to delete this message")
    await db.execute("DELETE FROM dm_messages WHERE id = ?", (message_id,))
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Users / direct messages
# ---------------------------------------------------------------------------
@api_router.get("/users/search")
async def search_users(q: Optional[str] = None, user: dict = Depends(get_current_user), db=Depends(get_db)):
    me = user["id"]
    query = "SELECT id, name, role FROM users WHERE status != 'pending' AND id != ?"
    params = [me]
    if q:
        term = f"%{q.strip()}%"
        query += " AND (name LIKE ? OR email LIKE ?)"
        params.extend([term, term])
    query += " ORDER BY name LIMIT 50"
    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
    return [{"id": r["id"], "name": r["name"], "role": r["role"]} for r in rows]


@api_router.get("/users")
async def list_users(user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    async with db.execute("SELECT * FROM users WHERE status != 'pending' ORDER BY created_at") as cursor:
        rows = await cursor.fetchall()
    return [serialize_user(r) for r in rows]


@api_router.get("/users/pending")
async def list_pending_users(user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    async with db.execute("SELECT * FROM users WHERE status = 'pending' ORDER BY created_at") as cursor:
        rows = await cursor.fetchall()
    return [serialize_user(r) for r in rows]


@api_router.post("/users/{user_id}/approve")
async def approve_user(user_id: str, req: ApproveRequest, user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    if req.role not in ("member", "mentor"):
        raise HTTPException(status_code=400, detail="Role must be member or mentor")
    target = await fetch_user(db, user_id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("status") != "pending":
        raise HTTPException(status_code=400, detail="This member has already been reviewed")
    await db.execute("UPDATE users SET status = 'approved', role = ? WHERE id = ?", (req.role, user_id))
    await db.commit()
    return serialize_user(await fetch_user(db, user_id=user_id))


@api_router.post("/users/{user_id}/reject")
async def reject_user(user_id: str, user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    target = await fetch_user(db, user_id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("status") != "pending":
        raise HTTPException(status_code=400, detail="This member has already been reviewed")
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    return {"ok": True}


@api_router.put("/users/{user_id}/role")
async def update_user_role(user_id: str, req: RoleUpdate, user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    if req.role not in ("member", "mentor"):
        raise HTTPException(status_code=400, detail="Role must be member or mentor")
    target = await fetch_user(db, user_id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("role") == "owner":
        raise HTTPException(status_code=400, detail="Cannot change the owner's role")
    await db.execute("UPDATE users SET role = ? WHERE id = ?", (req.role, user_id))
    await db.commit()
    return serialize_user(await fetch_user(db, user_id=user_id))


@api_router.post("/users")
async def create_user(req: CreateUserRequest, user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    email = req.email.lower().strip()
    if email == OWNER_EMAIL:
        raise HTTPException(status_code=400, detail="This email is reserved for the owner")
    if req.role not in ("member", "mentor"):
        raise HTTPException(status_code=400, detail="Role must be member or mentor")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    async with db.execute("SELECT 1 FROM users WHERE email = ?", (email,)) as cursor:
        if await cursor.fetchone():
            raise HTTPException(status_code=400, detail="A user with this email already exists")
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO users (id, email, password_hash, name, role, status, phone, carrier,
                           email_notifications, sms_notifications, permissions, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            email,
            hash_password(req.password),
            req.name.strip() or email.split("@")[0],
            req.role,
            "approved",
            "",
            "",
            1,
            0,
            "{}",
            now,
        ),
    )
    await db.commit()
    return serialize_user(await fetch_user(db, user_id=uid))


@api_router.delete("/users/{user_id}")
async def delete_user(user_id: str, user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    target = await fetch_user(db, user_id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("role") == "owner":
        raise HTTPException(status_code=400, detail="The owner account cannot be deleted")
    await _delete_user_data(user_id, db)
    return {"ok": True}


@api_router.get("/users/{user_id}/permissions")
async def get_user_permissions(user_id: str, user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    target = await fetch_user(db, user_id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("role") == "owner":
        return {"permissions": get_permissions(target)}
    return {"permissions": get_permissions(target)}


@api_router.put("/users/{user_id}/permissions")
async def update_user_permissions(user_id: str, req: PermissionsUpdate, user: dict = Depends(require_permission("can_manage_members")), db=Depends(get_db)):
    target = await fetch_user(db, user_id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("role") == "owner":
        raise HTTPException(status_code=400, detail="Owner permissions cannot be changed")
    cleaned = {k: bool(v) for k, v in req.permissions.items() if k in PERMISSION_KEYS}
    await db.execute("UPDATE users SET permissions = ? WHERE id = ?", (json.dumps(cleaned), user_id))
    await db.commit()
    return {"permissions": get_permissions(await fetch_user(db, user_id=user_id))}


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------
@api_router.get("/dm/threads")
async def dm_threads(user: dict = Depends(get_current_user), db=Depends(get_db)):
    me = user["id"]
    async with db.execute(
        "SELECT * FROM dm_messages WHERE sender_id = ? OR recipient_id = ? ORDER BY created_at DESC",
        (me, me),
    ) as cursor:
        rows = await cursor.fetchall()
    threads = {}
    for m in rows:
        other = m["recipient_id"] if m["sender_id"] == me else m["sender_id"]
        if other not in threads:
            threads[other] = m
    result = []
    for other_id, last in threads.items():
        other = await fetch_user(db, user_id=other_id)
        if not other:
            continue
        result.append({
            "user_id": other_id,
            "name": other.get("name", ""),
            "role": other.get("role", "member"),
            "last_text": last["text"] or "Attachment",
            "last_at": last["created_at"],
        })
    result.sort(key=lambda x: x["last_at"], reverse=True)
    return result


async def _resolve_other(other_id: str, me: str, db: aiosqlite.Connection):
    if other_id == me:
        raise HTTPException(status_code=400, detail="You cannot message yourself")
    other = await fetch_user(db, user_id=other_id)
    if not other:
        raise HTTPException(status_code=404, detail="User not found")
    if other.get("status") == "pending":
        raise HTTPException(status_code=400, detail="User is awaiting approval")
    return other


@api_router.get("/dm/{other_id}/messages")
async def get_dm(other_id: str, user: dict = Depends(get_current_user), db=Depends(get_db)):
    me = user["id"]
    other = await _resolve_other(other_id, me, db)
    conv = dm_conversation_id(me, other_id)
    async with db.execute(
        "SELECT * FROM dm_messages WHERE conversation_id = ? ORDER BY created_at ASC",
        (conv,),
    ) as cursor:
        rows = await cursor.fetchall()
    messages = []
    for r in rows:
        m = dict(r)
        messages.append({
            "id": m["id"],
            "conversation_id": m["conversation_id"],
            "user_id": m["sender_id"],
            "user_name": m["sender_name"],
            "user_role": m["sender_role"],
            "text": m["text"],
            "attachment": json.loads(m["attachment"]) if m.get("attachment") else None,
            "created_at": m["created_at"],
        })
    return {
        "other": {"id": other_id, "name": other.get("name", ""), "role": other.get("role", "member")},
        "messages": messages,
    }


@api_router.post("/dm/{other_id}/messages")
async def post_dm(other_id: str, req: MessageCreate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    if not has_permission(user, "can_chat"):
        raise HTTPException(status_code=403, detail="You do not have permission to send messages")
    blocked = _contains_blocked_content(req.text)
    if blocked:
        raise HTTPException(status_code=400, detail="Message contains inappropriate content and cannot be sent.")
    me = user["id"]
    other = await _resolve_other(other_id, me, db)
    attachment = await _file_attachment(db, req.attachment_file_id)
    conv = dm_conversation_id(me, other_id)
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO dm_messages (id, conversation_id, sender_id, sender_name, sender_role, recipient_id, text, attachment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, conv, me, user.get("name", ""), user.get("role", "member"), other_id, req.text, json.dumps(attachment) if attachment else None, now),
    )
    await db.commit()
    return {
        "id": uid,
        "conversation_id": conv,
        "user_id": me,
        "user_name": user.get("name", ""),
        "user_role": user.get("role", "member"),
        "recipient_id": other_id,
        "text": req.text,
        "attachment": attachment,
        "created_at": now,
    }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
@api_router.post("/files/upload")
async def upload_file(file: UploadFile = File(...), user: dict = Depends(get_current_user), db=Depends(get_db)):
    if not has_permission(user, "can_upload_files"):
        raise HTTPException(status_code=403, detail="You do not have permission to upload files")
    blocked = _contains_blocked_content(file.filename)
    if blocked:
        raise HTTPException(status_code=400, detail="File name contains inappropriate content.")
    fname_lower = file.filename.lower()
    if any(fname_lower.endswith(ext) for ext in _BLOCKED_FILE_EXTENSIONS):
        raise HTTPException(status_code=400, detail="This file type is not allowed.")
    content_type = file.content_type or "application/octet-stream"
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 25MB)")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "bin"
    uid = str(uuid.uuid4())
    rel_path = f"uploads/{uid}.{ext}"
    abs_path = DATA_DIR / rel_path
    abs_path.write_bytes(data)
    kind = classify_file(file.filename, content_type)
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO files (id, storage_path, original_filename, content_type, size, kind, uploaded_by, uploader_name, is_deleted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, rel_path, file.filename, content_type, len(data), kind, user["id"], user.get("name", ""), 0, now),
    )
    await db.commit()
    async with db.execute("SELECT * FROM files WHERE id = ?", (uid,)) as cursor:
        row = dict(await cursor.fetchone())
    return _serialize_file(row)


def _serialize_file(f: dict) -> dict:
    return {
        "id": f["id"],
        "storage_path": f["storage_path"],
        "original_filename": f["original_filename"],
        "content_type": f["content_type"],
        "size": f["size"],
        "kind": f["kind"],
        "uploaded_by": f["uploaded_by"],
        "uploader_name": f["uploader_name"],
        "is_deleted": bool(f["is_deleted"]),
        "created_at": f["created_at"],
    }


@api_router.get("/files")
async def list_files(user: dict = Depends(get_current_user), db=Depends(get_db)):
    async with db.execute("SELECT * FROM files WHERE is_deleted = 0 ORDER BY created_at DESC") as cursor:
        rows = await cursor.fetchall()
    return [_serialize_file(dict(r)) for r in rows]


@api_router.get("/files/{file_id}/download-token")
async def file_download_token(file_id: str, user: dict = Depends(get_current_user)):
    return {"token": _file_download_token(file_id, user["id"]), "expires_in": 300}


@api_router.get("/files/{file_id}/download")
async def download_file(file_id: str, request: Request, download_token: Optional[str] = Query(None), db=Depends(get_db)):
    user = await _resolve_file_download_user(file_id, request, download_token, db)
    async with db.execute("SELECT * FROM files WHERE id = ? AND is_deleted = 0", (file_id,)) as cursor:
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    f = dict(row)
    abs_path = DATA_DIR / f["storage_path"]
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    data = abs_path.read_bytes()
    headers = {"Content-Disposition": f'inline; filename="{f["original_filename"]}"'}
    return FastResponse(content=data, media_type=f["content_type"], headers=headers)


@api_router.delete("/files/{file_id}")
async def delete_file(file_id: str, user: dict = Depends(get_current_user), db=Depends(get_db)):
    async with db.execute("SELECT * FROM files WHERE id = ? AND is_deleted = 0", (file_id,)) as cursor:
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    f = dict(row)
    if f["uploaded_by"] != user["id"] and not has_permission(user, "can_delete_any_file"):
        raise HTTPException(status_code=403, detail="Not allowed to delete this file")
    await db.execute("UPDATE files SET is_deleted = 1 WHERE id = ?", (file_id,))
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Events / Calendar
# ---------------------------------------------------------------------------
@api_router.get("/events")
async def list_events(user: dict = Depends(get_current_user), db=Depends(get_db)):
    async with db.execute("SELECT * FROM events ORDER BY date ASC") as cursor:
        rows = await cursor.fetchall()
    return [_serialize_event(dict(r)) for r in rows]


def _serialize_event(e: dict) -> dict:
    return {
        "id": e["id"],
        "title": e["title"],
        "description": e.get("description", "") or "",
        "date": e["date"],
        "location": e.get("location", "") or "",
        "created_by": e.get("created_by"),
        "creator_name": e.get("creator_name", ""),
        "created_at": e["created_at"],
    }


@api_router.post("/events")
async def create_event(req: EventCreate, user: dict = Depends(require_permission("can_edit_calendar")), db=Depends(get_db)):
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO events (id, title, description, date, location, created_by, creator_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, req.title, req.description, req.date, req.location, user["id"], user.get("name", ""), now),
    )
    await db.commit()
    return _serialize_event(await _fetch_event(db, uid))


async def _fetch_event(db: aiosqlite.Connection, event_id: str):
    async with db.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


@api_router.put("/events/{event_id}")
async def update_event(event_id: str, req: EventCreate, user: dict = Depends(require_permission("can_edit_calendar")), db=Depends(get_db)):
    existing = await _fetch_event(db, event_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Event not found")
    await db.execute(
        "UPDATE events SET title = ?, description = ?, date = ?, location = ? WHERE id = ?",
        (req.title, req.description, req.date, req.location, event_id),
    )
    await db.commit()
    return _serialize_event(await _fetch_event(db, event_id))


@api_router.delete("/events/{event_id}")
async def delete_event(event_id: str, user: dict = Depends(require_permission("can_edit_calendar")), db=Depends(get_db)):
    existing = await _fetch_event(db, event_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Event not found")
    await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------
@api_router.get("/todos")
async def list_todos(user: dict = Depends(get_current_user), db=Depends(get_db)):
    uid = user["id"]
    if user.get("role") == "owner" or has_permission(user, "can_manage_todos"):
        async with db.execute("SELECT * FROM todos ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
    else:
        async with db.execute(
            "SELECT * FROM todos WHERE assigned_to = ? OR created_by = ? OR assigned_to IS NULL ORDER BY created_at DESC",
            (uid, uid),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_serialize_todo(dict(r)) for r in rows]


def _serialize_todo(t: dict) -> dict:
    return {
        "id": t["id"],
        "title": t["title"],
        "description": t.get("description", "") or "",
        "deadline": t.get("deadline"),
        "assigned_to": t.get("assigned_to"),
        "created_by": t["created_by"],
        "creator_name": t["creator_name"],
        "completed": bool(t["completed"]),
        "created_at": t["created_at"],
    }


@api_router.post("/todos")
async def create_todo(req: TodoCreate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    if not has_permission(user, "can_chat"):
        # Using can_chat as a proxy for "can use app features" for members.
        pass
    blocked = _contains_blocked_content(req.title)
    if blocked:
        raise HTTPException(status_code=400, detail="Title contains inappropriate content.")
    blocked = _contains_blocked_content(req.description)
    if blocked:
        raise HTTPException(status_code=400, detail="Description contains inappropriate content.")
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO todos (id, title, description, deadline, assigned_to, created_by, creator_name, completed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, req.title.strip(), req.description.strip(), req.deadline, req.assigned_to or None, user["id"], user.get("name", ""), 0, now),
    )
    await db.commit()
    return _serialize_todo(await _fetch_todo(db, uid))


async def _fetch_todo(db: aiosqlite.Connection, todo_id: str):
    async with db.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


@api_router.put("/todos/{todo_id}")
async def update_todo(todo_id: str, req: TodoUpdate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    existing = await _fetch_todo(db, todo_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Todo not found")
    if existing["created_by"] != user["id"] and not has_permission(user, "can_manage_todos"):
        raise HTTPException(status_code=403, detail="Not allowed to edit this todo")
    updates = {}
    if req.title is not None:
        blocked = _contains_blocked_content(req.title)
        if blocked:
            raise HTTPException(status_code=400, detail="Title contains inappropriate content.")
        updates["title"] = req.title.strip()
    if req.description is not None:
        blocked = _contains_blocked_content(req.description)
        if blocked:
            raise HTTPException(status_code=400, detail="Description contains inappropriate content.")
        updates["description"] = req.description.strip()
    if req.deadline is not None:
        updates["deadline"] = req.deadline if req.deadline else None
    if req.assigned_to is not None:
        updates["assigned_to"] = req.assigned_to if req.assigned_to else None
    if req.completed is not None:
        updates["completed"] = 1 if req.completed else 0
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [todo_id]
        await db.execute(f"UPDATE todos SET {set_clause} WHERE id = ?", values)
        await db.commit()
    return _serialize_todo(await _fetch_todo(db, todo_id))


@api_router.delete("/todos/{todo_id}")
async def delete_todo(todo_id: str, user: dict = Depends(get_current_user), db=Depends(get_db)):
    existing = await _fetch_todo(db, todo_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Todo not found")
    if existing["created_by"] != user["id"] and not has_permission(user, "can_manage_todos"):
        raise HTTPException(status_code=403, detail="Not allowed to delete this todo")
    await db.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@api_router.get("/dashboard")
async def dashboard(user: dict = Depends(get_current_user), db=Depends(get_db)):
    now = datetime.now(timezone.utc).isoformat()
    async with db.execute("SELECT * FROM events WHERE date >= ? ORDER BY date ASC LIMIT 1", (now,)) as cursor:
        row = await cursor.fetchone()
    next_event = _serialize_event(dict(row)) if row else None
    if not next_event:
        async with db.execute("SELECT * FROM events ORDER BY date DESC LIMIT 1") as cursor:
            row = await cursor.fetchone()
        next_event = _serialize_event(dict(row)) if row else None

    async with db.execute("SELECT * FROM files WHERE is_deleted = 0 ORDER BY created_at DESC LIMIT 4") as cursor:
        recent_files = [_serialize_file(dict(r)) for r in await cursor.fetchall()]

    visible_ids = [c["id"] for c in CHANNELS if channel_visible_to(c["id"], user)]
    placeholders = ",".join("?" * len(visible_ids)) if visible_ids else "''"
    async with db.execute(f"SELECT COUNT(*) FROM messages WHERE channel_id IN ({placeholders})", visible_ids) as cursor:
        msg_count = (await cursor.fetchone())[0]

    async with db.execute("SELECT COUNT(*) FROM users") as cursor:
        member_count = (await cursor.fetchone())[0]

    return {
        "next_event": next_event,
        "file_count": len(recent_files),
        "message_count": msg_count,
        "member_count": member_count,
        "recent_files": recent_files,
    }


# ---------------------------------------------------------------------------
# Push notifications (stub — no network required)
# ---------------------------------------------------------------------------
_DUMMY_PUBLIC_KEY = "B" * 87


@api_router.get("/push/public-key")
async def push_public_key(user: dict = Depends(get_current_user)):
    return {"publicKey": _DUMMY_PUBLIC_KEY}


@api_router.post("/push/subscribe")
async def push_subscribe(sub: PushSubscription, user: dict = Depends(get_current_user), db=Depends(get_db)):
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    keys = json.dumps(dict(sub.keys))
    await db.execute(
        "INSERT OR REPLACE INTO push_subscriptions (id, user_id, endpoint, subscription, created_at) VALUES (?, ?, ?, ?, ?)",
        (uid, user["id"], sub.endpoint, keys, now),
    )
    await db.commit()
    return {"ok": True}


@api_router.post("/push/unsubscribe")
async def push_unsubscribe(req: PushUnsubscribe, user: dict = Depends(get_current_user), db=Depends(get_db)):
    await db.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (req.endpoint, user["id"]))
    await db.commit()
    return {"ok": True}


@api_router.get("/push/status")
async def push_status(endpoint: Optional[str] = None, user: dict = Depends(get_current_user), db=Depends(get_db)):
    async with db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (user["id"],)) as cursor:
        count = (await cursor.fetchone())[0]
    subscribed = False
    if endpoint:
        async with db.execute("SELECT 1 FROM push_subscriptions WHERE user_id = ? AND endpoint = ?", (user["id"], endpoint)) as cursor:
            subscribed = await cursor.fetchone() is not None
    return {"device_count": count, "subscribed": subscribed}


@api_router.get("/push/debug")
async def push_debug(user: dict = Depends(require_permission("can_manage_members"))):
    return {"public_key_set": True, "private_key_loaded": False, "public_private_match": None, "subscription_count": 0}


@api_router.post("/digest/send-now")
async def send_digest_now(user: dict = Depends(require_permission("can_manage_members"))):
    return {"ok": True, "message": "Weekly digest skipped in offline mode."}


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------
@api_router.get("/")
async def root():
    return {"message": "Robotics Team Hub offline API"}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    port = int(os.environ.get("RH_PORT", DEFAULT_PORT))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
