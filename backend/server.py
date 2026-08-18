from pathlib import Path
import os

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
# Only load .env if it exists AND no VAPID keys are set in environment
# This ensures Render env vars take precedence
if not (os.environ.get('VAPID_PRIVATE_KEY_B64') or os.environ.get('VAPID_PRIVATE_KEY')):
    load_dotenv(ROOT_DIR / '.env')

import re
import json
import base64
import uuid
import logging
import asyncio
import secrets as pysecrets
from datetime import datetime, timezone, timedelta

import jwt
import bcrypt
import requests
import resend
from pywebpush import webpush, WebPushException
from py_vapid import Vapid01 as Vapid
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from fastapi import (
    FastAPI, APIRouter, Depends, HTTPException, Request, Response,
    UploadFile, File, Form, Query, Header
)
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional
from bson import ObjectId

# ---------------------------------------------------------------------------
# Config / DB
# ---------------------------------------------------------------------------
def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in your Render service (Environment tab) or render.yaml before deploying."
        )

_require_env('MONGO_URL', 'JWT_SECRET', 'OWNER_EMAIL', 'OWNER_PASSWORD')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'robotics_hub')]

JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGORITHM = "HS256"
OWNER_EMAIL = os.environ['OWNER_EMAIL'].lower()
OWNER_PASSWORD = os.environ['OWNER_PASSWORD']
OWNER_NAME = os.environ.get('OWNER_NAME', 'Owner')

EMERGENT_KEY = os.environ.get('EMERGENT_LLM_KEY')
STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"
APP_NAME = "robotics-hub"

# Storage provider: "emergent" (default, used inside Emergent) or "r2" (Cloudflare R2 / any S3-compatible) for self-hosting
STORAGE_PROVIDER = os.environ.get('STORAGE_PROVIDER', 'emergent').lower()
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET = os.environ.get('R2_BUCKET', '')
R2_ENDPOINT = os.environ.get('R2_ENDPOINT', '') or (f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else '')

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'onboarding@resend.dev')

VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY_B64 = os.environ.get('VAPID_PRIVATE_KEY_B64', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_CLAIM_EMAIL = os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:admin@example.com')


def _b64pad(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def _load_vapid_from_value(raw: str) -> Vapid:
    raw = raw.strip()
    if "BEGIN" in raw and "PRIVATE KEY" in raw:
        return Vapid.from_pem(raw.encode("utf-8"))
    for decoder in (
        base64.b64decode,
        lambda value: base64.urlsafe_b64decode(_b64pad(value)),
    ):
        try:
            data = decoder(raw)
        except Exception:
            continue
        if b"BEGIN" in data and b"PRIVATE KEY" in data:
            return Vapid.from_pem(data)
        if len(data) == 32:
            return Vapid.from_raw(raw.encode("utf-8"))
        try:
            return Vapid.from_der(raw.encode("utf-8"))
        except Exception:
            pass
    return Vapid.from_string(private_key=raw)


def _load_vapid() -> Vapid | None:
    for raw in (VAPID_PRIVATE_KEY_B64, VAPID_PRIVATE_KEY):
        if not raw:
            continue
        try:
            return _load_vapid_from_value(raw)
        except Exception as e:
            logging.getLogger("robotics-hub").exception(f"[vapid] load failed for configured private key: {e}")
    return None


def _vapid_public_key_b64url(vapid: Vapid) -> str:
    return base64.urlsafe_b64encode(
        vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    ).rstrip(b"=").decode("utf-8")


_vapid = _load_vapid()
_vapid_public_key_match: bool | None = None
if _vapid and VAPID_PUBLIC_KEY:
    _vapid_public_key_match = _vapid_public_key_b64url(_vapid) == VAPID_PUBLIC_KEY.replace("=", "")

# US carrier email-to-SMS gateways (free, best-effort)
CARRIER_GATEWAYS = {
    "verizon": "vtext.com",
    "att": "txt.att.net",
    "tmobile": "tmomail.net",
    "sprint": "messaging.sprintpcs.com",
    "boost": "sms.myboostmobile.com",
    "cricket": "sms.cricketwireless.net",
    "uscellular": "email.uscc.net",
    "metropcs": "mymetropcs.com",
    "googlefi": "msg.fi.google.com",
    "xfinity": "vtext.com",
    "virgin": "vmobl.com",
}

# Granular permission model (stored overrides merged with role defaults)
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
        return defaults
    stored = user.get("permissions")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except Exception:
            stored = {}
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in defaults and isinstance(value, bool):
                defaults[key] = value
    return defaults


def has_permission(user: dict, key: str) -> bool:
    return get_permissions(user).get(key, False)


# Monthly cap on outbound notification emails (digest + email-to-SMS) to protect quota
EMAIL_MONTHLY_LIMIT = int(os.environ.get('EMAIL_MONTHLY_LIMIT', '2500'))

# Field-level encryption at rest
_DATA_KEY = os.environ.get('DATA_ENCRYPTION_KEY')
_fernet = Fernet(_DATA_KEY.encode()) if _DATA_KEY else None


def encrypt_field(plaintext: str) -> str:
    if not _fernet or not plaintext:
        return plaintext
    return _fernet.encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt_field(ciphertext: str) -> str:
    if not _fernet or not ciphertext:
        return ciphertext
    try:
        return _fernet.decrypt(ciphertext.encode('utf-8')).decode('utf-8')
    except (InvalidToken, Exception):
        return ciphertext


# ---------------------------------------------------------------------------
# Content filtering — block profanity, slurs, sexual innuendos, 18+ content
# ---------------------------------------------------------------------------
_BLOCKED_WORDS = {
    "fuck", "shit", "ass", "bitch", "damn", "crap", "dick", "cock",
    "pussy", "whore", "slut", "bastard", "motherfucker", "fucker", "asshole",
    "bullshit", "dumbass", "jackass", "piss", "cunt", "twat", "wanker",
    "nigger", "nigga", "faggot", "retard", "retarded",
    "porn", "hentai", "nude", "nudes", "naked", "xxx", "nsfw", "onlyfans",
    "blowjob", "handjob", "dildo", "orgasm", "masturbat", "cumshot", "creampie",
    "anal", "bondage", "fetish", "kinky", "horny", "sexy", "sexting",
}

_BLOCKED_IMAGE_EXTENSIONS = {".exe", ".bat", ".cmd", ".scr", ".com"}


def _contains_blocked_content(text: str) -> str | None:
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


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("robotics-hub")
if _vapid and VAPID_PUBLIC_KEY:
    if _vapid_public_key_match:
        logger.info("[vapid] loaded, public key matches")
    else:
        logger.error("[vapid] PUBLIC/PRIVATE KEY MISMATCH public key does not match loaded private key")

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ---------------------------------------------------------------------------
# Channel definitions
# ---------------------------------------------------------------------------
# Per-program channels: VEX uses a single General chat; FRC has full categories incl. Design.
PROGRAM_CHANNELS = {
    "vex": {"label": "VEX", "subs": [("general", "General")]},
    "frc": {"label": "FRC", "subs": [
        ("programming", "Programming"),
        ("building", "Building"),
        ("business", "Business"),
        ("team", "Team Chat"),
        ("design", "Design"),
    ]},
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


def channel_visible_to(channel_id: str, user: dict) -> bool:
    if channel_id not in CHANNEL_IDS:
        return False
    if channel_id == "members-only":
        role = user.get("role", "member")
        if role == "owner":
            return True
        return has_permission(user, "can_view_members_only")
    return True


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


def serialize_user(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "email": user["email"],
        "name": user.get("name", ""),
        "role": user.get("role", "member"),
        "phone": decrypt_field(user.get("phone", "")),
        "carrier": user.get("carrier", ""),
        "email_notifications": user.get("email_notifications", True),
        "sms_notifications": user.get("sms_notifications", False),
        "status": user.get("status", "approved"),
        "permissions": get_permissions(user),
        "created_at": user.get("created_at"),
    }


async def get_current_user(request: Request) -> dict:
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
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if user.get("status") == "pending":
            raise HTTPException(status_code=403, detail="Your account is awaiting owner approval")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


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
# Object storage helpers
# ---------------------------------------------------------------------------
storage_key = None
_r2_client = None


def _get_r2_client():
    global _r2_client
    if _r2_client is None:
        import boto3
        from botocore.config import Config
        _r2_client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _r2_client


def init_storage():
    """Emergent storage needs a session key; R2 needs nothing."""
    if STORAGE_PROVIDER == "r2":
        _get_r2_client()
        return "r2"
    global storage_key
    if storage_key:
        return storage_key
    resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": EMERGENT_KEY}, timeout=30)
    resp.raise_for_status()
    storage_key = resp.json()["storage_key"]
    return storage_key


def put_object(path: str, data: bytes, content_type: str) -> dict:
    if STORAGE_PROVIDER == "r2":
        _get_r2_client().put_object(Bucket=R2_BUCKET, Key=path, Body=data, ContentType=content_type)
        return {"path": path, "size": len(data)}
    key = init_storage()
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data, timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def delete_object(path: str):
    if STORAGE_PROVIDER == "r2":
        _get_r2_client().delete_object(Bucket=R2_BUCKET, Key=path)
        return
    key = init_storage()
    resp = requests.delete(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": key}, timeout=60)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()


def get_object(path: str):
    if STORAGE_PROVIDER == "r2":
        obj = _get_r2_client().get_object(Bucket=R2_BUCKET, Key=path)
        return obj["Body"].read(), obj.get("ContentType", "application/octet-stream")
    key = init_storage()
    resp = requests.get(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": key}, timeout=60)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


# ---------------------------------------------------------------------------
# Notification helpers (graceful no-op if keys missing)
# ---------------------------------------------------------------------------
def _send_email_sync(to_email: str, subject: str, html: str):
    if not RESEND_API_KEY:
        logger.info("[email skipped - no key]")
        return
    try:
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({"from": SENDER_EMAIL, "to": [to_email], "subject": subject, "html": html})
        logger.info("[email sent] ok")
    except Exception as e:
        logger.error(f"[email failed] {e}")


def _send_email_plain_sync(to_email: str, subject: str, text: str):
    """Plain-text email, used for email-to-SMS carrier gateways."""
    if not RESEND_API_KEY:
        logger.info("[sms-email skipped - no key]")
        return
    try:
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({"from": SENDER_EMAIL, "to": [to_email], "subject": subject, "text": text})
        logger.info("[sms-email sent] ok")
    except Exception as e:
        logger.error(f"[sms-email failed] {e}")


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]


def _send_webpush_sync(subscription_info: dict, payload: dict):
    if not VAPID_PUBLIC_KEY:
        logger.info("[webpush skipped] VAPID_PUBLIC_KEY not set")
        return False
    if not _vapid:
        logger.info("[webpush skipped] no usable VAPID private key")
        return False
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=_vapid,
            vapid_claims={"sub": VAPID_CLAIM_EMAIL},
        )
        logger.info("[webpush sent] ok")
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            return "expired"
        logger.error(f"[webpush failed] {e}")
        return False
    except Exception as e:
        logger.error(f"[webpush error] {e}")
        return False


def _file_download_token(file_id: str, user_id: str) -> str:
    payload = {
        "type": "file_download",
        "file_id": file_id,
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=FILE_DOWNLOAD_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def _file_visible_to_user(file_id: str, user: dict) -> bool:
    uid = str(user["_id"])
    if not user.get("role"):
        return False
    rec = await db.files.find_one({"id": file_id, "is_deleted": False}, {"_id": 0})
    if not rec:
        return False
    if rec.get("uploaded_by") == uid:
        return True

    visible_channels = [c["id"] for c in CHANNELS if channel_visible_to(c["id"], user)]
    if visible_channels:
        channel_match = await db.messages.find_one(
            {"channel_id": {"$in": visible_channels}, "attachment.file_id": file_id},
            {"_id": 0},
        )
        if channel_match:
            return True

    dm_match = await db.dm_messages.find_one(
        {
            "attachment.file_id": file_id,
            "$or": [{"sender_id": uid}, {"recipient_id": uid}],
        },
        {"_id": 0},
    )
    return dm_match is not None


async def _issue_file_download_token_or_403(file_id: str, user: dict) -> str:
    if not await _file_visible_to_user(file_id, user):
        raise HTTPException(status_code=403, detail="Not allowed to access this file")
    return _file_download_token(file_id, str(user["_id"]))


async def _resolve_file_download_user(file_id: str, request: Request, download_token: Optional[str]) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return await get_current_user(request)
    if not download_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(download_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Download token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid download token")
    if payload.get("type") != "file_download":
        raise HTTPException(status_code=401, detail="Invalid download token")
    if payload.get("file_id") != file_id:
        raise HTTPException(status_code=401, detail="Invalid download token")
    try:
        user = await db.users.find_one({"_id": ObjectId(payload["user_id"])})
    except Exception:
        user = None
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("status") == "pending":
        raise HTTPException(status_code=403, detail="Your account is awaiting owner approval")
    return user


def _email_template(title: str, body: str) -> str:
    return f"""
    <div style="font-family:Arial,sans-serif;background:#f4f6fb;padding:24px;">
      <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e6e9f0;">
        <div style="background:#1d4ed8;padding:20px 28px;color:#fff;font-size:18px;font-weight:bold;">Robotics Team Hub</div>
        <div style="padding:28px;color:#1a1a1a;">
          <h2 style="margin:0 0 12px;font-size:20px;">{title}</h2>
          <div style="font-size:15px;line-height:1.6;color:#333;">{body}</div>
        </div>
        <div style="padding:16px 28px;background:#f9fafb;color:#888;font-size:12px;">You receive this weekly digest because email notifications are enabled in your Robotics Hub profile.</div>
      </div>
    </div>"""


async def _push_to_user(user_id: str, payload: dict):
    subs = await db.push_subscriptions.find({"user_id": user_id}).to_list(50)
    for s in subs:
        result = await asyncio.to_thread(_send_webpush_sync, s["subscription"], payload)
        if result == "expired":
            await db.push_subscriptions.delete_one({"endpoint": s["endpoint"]})


async def _reserve_email_quota() -> bool:
    """Atomically reserve one slot of the monthly outbound-email budget.
    Returns False once EMAIL_MONTHLY_LIMIT is reached for the current month."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    doc = await db.email_usage.find_one_and_update(
        {"month": month, "count": {"$lt": EMAIL_MONTHLY_LIMIT}},
        {"$inc": {"count": 1}},
        return_document=True,
    )
    if doc:
        return True
    existing = await db.email_usage.find_one({"month": month})
    if existing is None:
        try:
            await db.email_usage.insert_one({"month": month, "count": 1})
            return True
        except Exception:
            doc = await db.email_usage.find_one_and_update(
                {"month": month, "count": {"$lt": EMAIL_MONTHLY_LIMIT}},
                {"$inc": {"count": 1}}, return_document=True,
            )
            return doc is not None
    return False  # monthly limit reached


async def dispatch_email(to_email: str, subject: str, html: str):
    if await _reserve_email_quota():
        asyncio.create_task(asyncio.to_thread(_send_email_sync, to_email, subject, html))
    else:
        logger.warning(f"[email quota reached - {EMAIL_MONTHLY_LIMIT}/mo] skipped")


async def dispatch_sms_email(addr: str, subject: str, text: str):
    if await _reserve_email_quota():
        asyncio.create_task(asyncio.to_thread(_send_email_plain_sync, addr, subject, text))
    else:
        logger.warning(f"[sms quota reached - {EMAIL_MONTHLY_LIMIT}/mo] skipped")


async def notify_owner_pending_signup(new_user: dict):
    """Email the owner that someone requested to join and is awaiting approval."""
    try:
        name = new_user.get("name", "")
        email = new_user.get("email", "")
        subject = f"New member request: {name or email}"
        html = (
            f"<p><strong>{name or email}</strong> ({email}) has requested to join your robotics team.</p>"
            "<p>Sign in and open the Team page to approve or decline this request.</p>"
        )
        await dispatch_email(OWNER_EMAIL, subject, html)
    except Exception as e:
        logger.error(f"pending signup notify failed: {e}")


async def notify_new_message(channel: dict, msg: dict):
    """Web push + email-to-SMS to users with access to the channel (except sender)."""
    channel_id = channel["id"]
    sender_id = msg["user_id"]
    # recipients: users who can see this channel
    users = await db.users.find({}).to_list(1000)
    title = f"#{channel['name']}"
    preview = msg.get("text") or "Shared a file"
    body_text = f"{msg['user_name']}: {preview}"[:160]
    payload = {"title": title, "body": body_text, "url": "/chat"}
    for u in users:
        uid = str(u["_id"])
        if uid == sender_id:
            continue
        if not channel_visible_to(channel_id, u):
            continue
        # web push (always on if subscribed)
        await _push_to_user(uid, payload)
        # email-to-SMS (counts against monthly cap)
        if u.get("sms_notifications") and u.get("phone") and u.get("carrier") in CARRIER_GATEWAYS:
            digits = _normalize_phone(decrypt_field(u["phone"]))
            if len(digits) == 10:
                addr = f"{digits}@{CARRIER_GATEWAYS[u['carrier']]}"
                await dispatch_sms_email(addr, title, body_text)


# ---------------------------------------------------------------------------
# Weekly digest (Wednesdays 10:00 America/Phoenix)
# ---------------------------------------------------------------------------
async def build_and_send_weekly_digest():
    logger.info("Running weekly digest job")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    week_ahead = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    # New messages per channel (this week)
    pipeline = [
        {"$match": {"created_at": {"$gte": week_ago}}},
        {"$group": {"_id": "$channel_id", "count": {"$sum": 1}}},
    ]
    msg_counts = {d["_id"]: d["count"] async for d in db.messages.aggregate(pipeline)}

    new_files = await db.files.find(
        {"is_deleted": False, "created_at": {"$gte": week_ago}}, {"_id": 0}
    ).sort("created_at", -1).to_list(50)

    upcoming = await db.events.find(
        {"date": {"$gte": now_iso, "$lte": week_ahead}}, {"_id": 0}
    ).sort("date", 1).to_list(50)

    channel_name = {c["id"]: c for c in CHANNELS}

    users = await db.users.find({"email_notifications": True}).to_list(1000)
    for u in users:
        if not u.get("email"):
            continue
        role = u.get("role")
        # messages section respecting visibility
        rows = []
        for cid, cnt in msg_counts.items():
            ch = channel_name.get(cid)
            if not ch or not channel_visible_to(cid, u):
                continue
            label = ch["name"] if ch["program"] == "private" else f"{ch['program_label']} · {ch['name']}"
            rows.append(f"<li><b>{cnt}</b> new in {label}</li>")
        msgs_html = f"<ul>{''.join(rows)}</ul>" if rows else "<p>No new messages this week.</p>"

        visible_new_files = []
        for f in new_files:
            if await _file_visible_to_user(f["id"], u):
                visible_new_files.append(f)
        files_html = (
            "<ul>" + "".join(
                f"<li>{f['original_filename']} <span style='color:#888'>· {f['uploader_name']}</span></li>"
                for f in visible_new_files
            ) + "</ul>"
            if visible_new_files else "<p>No new files this week.</p>"
        )

        events_html = (
            "<ul>" + "".join(
                f"<li><b>{e['title']}</b> — {datetime.fromisoformat(e['date']).strftime('%a %b %d, %I:%M %p')}"
                + (f" @ {e['location']}" if e.get('location') else "") + "</li>"
                for e in upcoming
            ) + "</ul>"
            if upcoming else "<p>No events in the next 7 days.</p>"
        )

        body = f"""
          <h3 style="margin:18px 0 6px;">💬 New Messages</h3>{msgs_html}
          <h3 style="margin:18px 0 6px;">📁 New Files</h3>{files_html}
          <h3 style="margin:18px 0 6px;">📅 Upcoming Events</h3>{events_html}
        """
        html = _email_template(f"Your Weekly Team Digest", body)
        await dispatch_email(u["email"], "Robotics Hub — Weekly Digest", html)


def _send_email_sync(to_email: str, subject: str, html: str):
    if not RESEND_API_KEY:
        logger.info("[email skipped - no key]")
        return
    try:
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({"from": SENDER_EMAIL, "to": [to_email], "subject": subject, "html": html})
        logger.info("[email sent] ok")
    except Exception as e:
        logger.error(f"[email failed] {e}")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "member"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class MessageCreate(BaseModel):
    text: str
    attachment_file_id: Optional[str] = None


class EventCreate(BaseModel):
    title: str
    description: str = ""
    date: str  # ISO date/datetime string
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


FILE_DOWNLOAD_TOKEN_TTL_SECONDS = 5 * 60


class PushUnsubscribe(BaseModel):
    endpoint: str


class RoleUpdate(BaseModel):
    role: str


class CreateUserRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "member"


class ApproveRequest(BaseModel):
    role: str = "member"


class PermissionsUpdate(BaseModel):
    permissions: dict


class TodoCreate(BaseModel):
    title: str
    description: str = ""
    deadline: Optional[str] = None  # ISO datetime
    assigned_to: Optional[str] = None  # user ID


class TodoUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    deadline: Optional[str] = None
    assigned_to: Optional[str] = None
    completed: Optional[bool] = None


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@api_router.post("/auth/register")
async def register(req: RegisterRequest, response: Response):
    email = req.email.lower()
    if email == OWNER_EMAIL:
        raise HTTPException(status_code=400, detail="This email is reserved. Please sign in instead.")
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    doc = {
        "email": email,
        "password_hash": hash_password(req.password),
        "name": req.name.strip() or email.split("@")[0],
        "role": "member",
        "status": "pending",
        "phone": "",
        "email_notifications": True,
        "sms_notifications": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    asyncio.create_task(notify_owner_pending_signup(doc))
    return {
        "pending": True,
        "message": "Your request to join has been sent. You'll be able to sign in once the team owner approves your account.",
        "user": serialize_user(doc),
    }


@api_router.post("/auth/login")
async def login(req: LoginRequest, response: Response):
    email = req.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.get("status") == "pending":
        raise HTTPException(status_code=403, detail="Your account is awaiting owner approval. You'll be able to sign in once approved.")
    token = create_access_token(str(user["_id"]), email)
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
async def update_settings(req: SettingsUpdate, user: dict = Depends(get_current_user)):
    updates = {}
    if req.name is not None:
        updates["name"] = req.name.strip()
    if req.phone is not None:
        updates["phone"] = encrypt_field(req.phone.strip())
    if req.carrier is not None:
        updates["carrier"] = req.carrier.strip()
    if req.email_notifications is not None:
        updates["email_notifications"] = req.email_notifications
    if req.sms_notifications is not None:
        updates["sms_notifications"] = req.sms_notifications
    if updates:
        await db.users.update_one({"_id": user["_id"]}, {"$set": updates})
    updated = await db.users.find_one({"_id": user["_id"]})
    return serialize_user(updated)


# ---------------------------------------------------------------------------
# Channels & messages
# ---------------------------------------------------------------------------
@api_router.get("/channels")
async def list_channels(user: dict = Depends(get_current_user)):
    visible = [c for c in CHANNELS if channel_visible_to(c["id"], user)]
    return visible


@api_router.get("/channels/{channel_id}/messages")
async def get_messages(channel_id: str, user: dict = Depends(get_current_user)):
    if channel_id not in CHANNEL_IDS:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel_visible_to(channel_id, user):
        raise HTTPException(status_code=403, detail="You don't have access to this channel")
    msgs = await db.messages.find({"channel_id": channel_id}, {"_id": 0}).sort("created_at", 1).to_list(1000)
    for m in msgs:
        if m.get("text"):
            m["text"] = decrypt_field(m["text"])
    return msgs


@api_router.post("/channels/{channel_id}/messages")
async def post_message(channel_id: str, req: MessageCreate, user: dict = Depends(get_current_user)):
    if channel_id not in CHANNEL_IDS:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel_visible_to(channel_id, user):
        raise HTTPException(status_code=403, detail="You don't have access to this channel")
    if not has_permission(user, "can_chat"):
        raise HTTPException(status_code=403, detail="You don't have permission to send messages")
    blocked = _contains_blocked_content(req.text)
    if blocked:
        raise HTTPException(status_code=400, detail=f"Message contains inappropriate content and cannot be sent.")
    attachment = None
    if req.attachment_file_id:
        f = await db.files.find_one({"id": req.attachment_file_id, "is_deleted": False}, {"_id": 0})
        if f:
            attachment = {
                "file_id": f["id"],
                "filename": f["original_filename"],
                "content_type": f["content_type"],
                "kind": f["kind"],
            }
    msg = {
        "id": str(uuid.uuid4()),
        "channel_id": channel_id,
        "user_id": str(user["_id"]),
        "user_name": user.get("name", ""),
        "user_role": user.get("role", "member"),
        "text": req.text,
        "attachment": attachment,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    store = dict(msg)
    store["text"] = encrypt_field(store["text"])
    await db.messages.insert_one(store)
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if channel:
        asyncio.create_task(notify_new_message(channel, msg))
    return msg


@api_router.delete("/channels/{channel_id}/messages/{message_id}")
async def delete_message(channel_id: str, message_id: str, user: dict = Depends(get_current_user)):
    msg = await db.messages.find_one({"id": message_id, "channel_id": channel_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg["user_id"] != str(user["_id"]) and not has_permission(user, "can_delete_any_message"):
        raise HTTPException(status_code=403, detail="Not allowed to delete this message")
    await db.messages.delete_one({"id": message_id})
    return {"ok": True}


@api_router.delete("/dm/{other_id}/messages/{message_id}")
async def delete_dm_message(other_id: str, message_id: str, user: dict = Depends(get_current_user)):
    msg = await db.dm_messages.find_one({"id": message_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg["sender_id"] != str(user["_id"]) and not has_permission(user, "can_delete_any_message"):
        raise HTTPException(status_code=403, detail="Not allowed to delete this message")
    await db.dm_messages.delete_one({"id": message_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Web Push subscriptions
# ---------------------------------------------------------------------------
@api_router.get("/push/public-key")
async def push_public_key(user: dict = Depends(get_current_user)):
    return {"publicKey": VAPID_PUBLIC_KEY}


@api_router.post("/push/subscribe")
async def push_subscribe(sub: PushSubscription, user: dict = Depends(get_current_user)):
    keys = dict(sub.keys)
    doc = {
        "user_id": str(user["_id"]),
        "endpoint": sub.endpoint,
        "subscription": {"endpoint": sub.endpoint, "keys": keys},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.push_subscriptions.update_one(
        {"endpoint": sub.endpoint}, {"$set": doc}, upsert=True
    )
    # Send a test notification so the user knows it works
    test_payload = {
        "title": "Robotics Hub",
        "body": "Push notifications are working! You'll get alerts for new messages.",
        "url": "/settings",
        "tag": "robotics-hub-test",
    }
    sub_info = {"endpoint": sub.endpoint, "keys": keys}
    try:
        result = await asyncio.to_thread(_send_webpush_sync, sub_info, test_payload)
        logger.info(f"[test push] result={result} user={str(user['_id'])}")
    except Exception as e:
        logger.error(f"[test push error] {e}")
    return {"ok": True}


@api_router.post("/push/unsubscribe")
async def push_unsubscribe(req: PushUnsubscribe, user: dict = Depends(get_current_user)):
    await db.push_subscriptions.delete_one({"endpoint": req.endpoint, "user_id": str(user["_id"])})
    return {"ok": True}


@api_router.get("/push/status")
async def push_status(endpoint: Optional[str] = None, user: dict = Depends(get_current_user)):
    count = await db.push_subscriptions.count_documents({"user_id": str(user["_id"])})
    subscribed = False
    if endpoint:
        subscribed = await db.push_subscriptions.find_one({"endpoint": endpoint}) is not None
    return {"device_count": count, "subscribed": subscribed}


@api_router.get("/push/debug")
async def push_debug(user: dict = Depends(require_roles("owner"))):
    subscription_count = await db.push_subscriptions.count_documents({})
    return {
        "public_key_set": bool(VAPID_PUBLIC_KEY),
        "private_key_loaded": bool(_vapid),
        "public_private_match": _vapid_public_key_match,
        "vapid_public_key_prefix": VAPID_PUBLIC_KEY[:12],
        "subscription_count": subscription_count,
        "claim_email": VAPID_CLAIM_EMAIL,
    }


@api_router.post("/digest/send-now")
async def send_digest_now(user: dict = Depends(require_roles("owner"))):
    """Owner-only: trigger the weekly digest immediately (for testing/manual sends)."""
    await build_and_send_weekly_digest()
    return {"ok": True, "message": "Weekly digest sent to opted-in members."}


# ---------------------------------------------------------------------------
# Direct messages (person-to-person private chat)
# ---------------------------------------------------------------------------
def dm_conversation_id(a: str, b: str) -> str:
    return "dm:" + ":".join(sorted([a, b]))


async def notify_dm(recipient: dict, sender: dict, msg: dict):
    rid = str(recipient["_id"])
    sender_name = sender.get("name", "")
    title = f"DM from {sender_name}"
    preview = msg.get("text") or "Sent a file"
    body_text = f"{sender_name}: {preview}"[:160]
    payload = {"title": title, "body": body_text, "url": "/chat"}
    await _push_to_user(rid, payload)
    if recipient.get("sms_notifications") and recipient.get("phone") and recipient.get("carrier") in CARRIER_GATEWAYS:
        digits = _normalize_phone(decrypt_field(recipient["phone"]))
        if len(digits) == 10:
            addr = f"{digits}@{CARRIER_GATEWAYS[recipient['carrier']]}"
            await dispatch_sms_email(addr, title, body_text)


@api_router.get("/users/search")
async def search_users(q: Optional[str] = None, user: dict = Depends(get_current_user)):
    me = str(user["_id"])
    query = {}
    if q:
        rx = re.escape(q.strip())
        query = {"$or": [
            {"name": {"$regex": rx, "$options": "i"}},
            {"email": {"$regex": rx, "$options": "i"}},
        ]}
    users = await db.users.find(query).sort("name", 1).to_list(50)
    return [
        {"id": str(u["_id"]), "name": u.get("name", ""), "role": u.get("role", "member")}
        for u in users if str(u["_id"]) != me
    ][:20]


@api_router.get("/dm/threads")
async def dm_threads(user: dict = Depends(get_current_user)):
    me = str(user["_id"])
    msgs = await db.dm_messages.find(
        {"$or": [{"sender_id": me}, {"recipient_id": me}]}, {"_id": 0}
    ).sort("created_at", -1).to_list(2000)
    threads = {}
    for m in msgs:
        other = m["recipient_id"] if m["sender_id"] == me else m["sender_id"]
        if other not in threads:
            threads[other] = m  # latest first due to desc sort
    result = []
    for other_id, last in threads.items():
        try:
            ou = await db.users.find_one({"_id": ObjectId(other_id)})
        except Exception:
            ou = None
        if not ou:
            continue
        result.append({
            "user_id": other_id,
            "name": ou.get("name", ""),
            "role": ou.get("role", "member"),
            "last_text": decrypt_field(last.get("text") or "") or "Attachment",
            "last_at": last["created_at"],
        })
    result.sort(key=lambda x: x["last_at"], reverse=True)
    return result


async def _resolve_other(other_id: str, me: str):
    if other_id == me:
        raise HTTPException(status_code=400, detail="You cannot message yourself")
    try:
        ou = await db.users.find_one({"_id": ObjectId(other_id)})
    except Exception:
        ou = None
    if not ou:
        raise HTTPException(status_code=404, detail="User not found")
    return ou


@api_router.get("/dm/{other_id}/messages")
async def get_dm(other_id: str, user: dict = Depends(get_current_user)):
    me = str(user["_id"])
    ou = await _resolve_other(other_id, me)
    conv = dm_conversation_id(me, other_id)
    raw = await db.dm_messages.find({"conversation_id": conv}, {"_id": 0}).sort("created_at", 1).to_list(2000)
    messages = [{
        **m,
        "text": decrypt_field(m.get("text", "")),
        "user_id": m["sender_id"],
        "user_name": m["sender_name"],
        "user_role": m.get("sender_role", "member"),
    } for m in raw]
    return {"other": {"id": other_id, "name": ou.get("name", ""), "role": ou.get("role", "member")}, "messages": messages}


@api_router.post("/dm/{other_id}/messages")
async def post_dm(other_id: str, req: MessageCreate, user: dict = Depends(get_current_user)):
    if not has_permission(user, "can_chat"):
        raise HTTPException(status_code=403, detail="You don't have permission to send messages")
    blocked = _contains_blocked_content(req.text)
    if blocked:
        raise HTTPException(status_code=400, detail=f"Message contains inappropriate content and cannot be sent.")
    me = str(user["_id"])
    ou = await _resolve_other(other_id, me)
    attachment = None
    if req.attachment_file_id:
        f = await db.files.find_one({"id": req.attachment_file_id, "is_deleted": False}, {"_id": 0})
        if f:
            attachment = {
                "file_id": f["id"], "filename": f["original_filename"],
                "content_type": f["content_type"], "kind": f["kind"],
            }
    conv = dm_conversation_id(me, other_id)
    msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv,
        "sender_id": me,
        "sender_name": user.get("name", ""),
        "sender_role": user.get("role", "member"),
        "recipient_id": other_id,
        "text": req.text,
        "attachment": attachment,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    store = dict(msg)
    store["text"] = encrypt_field(store["text"])
    await db.dm_messages.insert_one(store)
    asyncio.create_task(notify_dm(ou, user, msg))
    return {**msg, "user_id": me, "user_name": user.get("name", ""), "user_role": user.get("role", "member")}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def classify_file(filename: str, content_type: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if content_type.startswith("image/") or ext in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        return "image"
    if ext == "zip" or content_type in ("application/zip", "application/x-zip-compressed"):
        return "zip"
    return "code"


@api_router.post("/files/upload")
async def upload_file(file: UploadFile = File(...), user: dict = Depends(require_permission("can_upload_files"))):
    blocked = _contains_blocked_content(file.filename)
    if blocked:
        raise HTTPException(status_code=400, detail="File name contains inappropriate content.")
    fname_lower = file.filename.lower()
    if any(fname_lower.endswith(ext) for ext in _BLOCKED_IMAGE_EXTENSIONS):
        raise HTTPException(status_code=400, detail="This file type is not allowed.")
    content_type = file.content_type or "application/octet-stream"
    if content_type.startswith("video/") or content_type.startswith("image/"):
        blocked = _contains_blocked_content(file.filename.rsplit(".", 1)[0] if "." in file.filename else file.filename)
        if blocked:
            raise HTTPException(status_code=400, detail="File contains inappropriate content reference.")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 25MB)")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "bin"
    path = f"{APP_NAME}/uploads/{str(user['_id'])}/{uuid.uuid4()}.{ext}"
    try:
        result = await asyncio.to_thread(put_object, path, data, content_type)
    except Exception as e:
        logger.error(f"upload failed: {e}")
        raise HTTPException(status_code=500, detail="Upload failed")
    kind = classify_file(file.filename, content_type)
    rec = {
        "id": str(uuid.uuid4()),
        "storage_path": result["path"],
        "original_filename": file.filename,
        "content_type": content_type,
        "size": result.get("size", len(data)),
        "kind": kind,
        "uploaded_by": str(user["_id"]),
        "uploader_name": user.get("name", ""),
        "is_deleted": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.files.insert_one(dict(rec))
    rec.pop("_id", None)
    return rec


@api_router.get("/files")
async def list_files(kind: Optional[str] = None, user: dict = Depends(get_current_user)):
    query = {"is_deleted": False}
    if kind:
        query["kind"] = kind
    files = await db.files.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)
    visible = []
    for f in files:
        if await _file_visible_to_user(f["id"], user):
            visible.append(f)
    return visible


@api_router.get("/files/{file_id}/download")
async def download_file(file_id: str, request: Request, download_token: Optional[str] = Query(None)):
    user = await _resolve_file_download_user(file_id, request, download_token)
    rec = await db.files.find_one({"id": file_id, "is_deleted": False}, {"_id": 0})
    if not rec:
        raise HTTPException(status_code=404, detail="File not found")
    if not await _file_visible_to_user(file_id, user):
        raise HTTPException(status_code=403, detail="Not allowed to access this file")
    try:
        content, ctype = await asyncio.to_thread(get_object, rec["storage_path"])
    except Exception as e:
        logger.error(f"download failed: {e}")
        raise HTTPException(status_code=500, detail="Download failed")
    headers = {"Content-Disposition": f'inline; filename="{rec["original_filename"]}"'}
    return Response(content=content, media_type=rec.get("content_type", ctype), headers=headers)


@api_router.get("/files/{file_id}/download-token")
async def file_download_token(file_id: str, user: dict = Depends(get_current_user)):
    token = await _issue_file_download_token_or_403(file_id, user)
    return {
        "token": token,
        "expires_in": FILE_DOWNLOAD_TOKEN_TTL_SECONDS,
    }


@api_router.delete("/files/{file_id}")
async def delete_file(file_id: str, user: dict = Depends(get_current_user)):
    rec = await db.files.find_one({"id": file_id, "is_deleted": False})
    if not rec:
        raise HTTPException(status_code=404, detail="File not found")
    if rec["uploaded_by"] != str(user["_id"]) and not has_permission(user, "can_delete_any_file"):
        raise HTTPException(status_code=403, detail="Not allowed to delete this file")
    try:
        await asyncio.to_thread(delete_object, rec["storage_path"])
    except Exception as e:
        logger.error(f"delete failed: {e}")
        raise HTTPException(status_code=500, detail="Delete failed")
    await db.files.update_one({"id": file_id}, {"$set": {"is_deleted": True}})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Events / Calendar
# ---------------------------------------------------------------------------
@api_router.get("/events")
async def list_events(user: dict = Depends(get_current_user)):
    events = await db.events.find({}, {"_id": 0}).sort("date", 1).to_list(1000)
    return events


@api_router.post("/events")
async def create_event(req: EventCreate, user: dict = Depends(require_permission("can_edit_calendar"))):
    ev = {
        "id": str(uuid.uuid4()),
        "title": req.title,
        "description": req.description,
        "date": req.date,
        "location": req.location,
        "created_by": str(user["_id"]),
        "creator_name": user.get("name", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.events.insert_one(dict(ev))
    ev.pop("_id", None)
    return ev


@api_router.put("/events/{event_id}")
async def update_event(event_id: str, req: EventCreate, user: dict = Depends(require_permission("can_edit_calendar"))):
    existing = await db.events.find_one({"id": event_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Event not found")
    await db.events.update_one({"id": event_id}, {"$set": {
        "title": req.title, "description": req.description, "date": req.date, "location": req.location,
    }})
    updated = await db.events.find_one({"id": event_id}, {"_id": 0})
    return updated


@api_router.delete("/events/{event_id}")
async def delete_event(event_id: str, user: dict = Depends(require_permission("can_edit_calendar"))):
    res = await db.events.delete_one({"id": event_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------
@api_router.get("/todos")
async def list_todos(user: dict = Depends(get_current_user)):
    uid = str(user["_id"])
    role = user.get("role")
    if role == "owner":
        todos = await db.todos.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    else:
        todos = await db.todos.find(
            {"$or": [{"assigned_to": uid}, {"created_by": uid}, {"assigned_to": None}]},
            {"_id": 0},
        ).sort("created_at", -1).to_list(500)
    return todos


@api_router.post("/todos")
async def create_todo(req: TodoCreate, user: dict = Depends(require_permission("can_manage_todos"))):
    blocked = _contains_blocked_content(req.title)
    if blocked:
        raise HTTPException(status_code=400, detail="Title contains inappropriate content.")
    blocked = _contains_blocked_content(req.description)
    if blocked:
        raise HTTPException(status_code=400, detail="Description contains inappropriate content.")
    todo = {
        "id": str(uuid.uuid4()),
        "title": req.title.strip(),
        "description": req.description.strip(),
        "deadline": req.deadline,
        "assigned_to": req.assigned_to,
        "created_by": str(user["_id"]),
        "creator_name": user.get("name", ""),
        "completed": False,
        "reminder_sent": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.todos.insert_one(dict(todo))
    todo.pop("_id", None)
    return todo


@api_router.put("/todos/{todo_id}")
async def update_todo(todo_id: str, req: TodoUpdate, user: dict = Depends(get_current_user)):
    existing = await db.todos.find_one({"id": todo_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Todo not found")
    uid = str(user["_id"])
    if existing["created_by"] != uid and not has_permission(user, "can_manage_todos"):
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
        updates["deadline"] = req.deadline
    if req.assigned_to is not None:
        updates["assigned_to"] = req.assigned_to if req.assigned_to else None
    if req.completed is not None:
        updates["completed"] = req.completed
    if updates:
        await db.todos.update_one({"id": todo_id}, {"$set": updates})
    updated = await db.todos.find_one({"id": todo_id}, {"_id": 0})
    return updated


@api_router.delete("/todos/{todo_id}")
async def delete_todo(todo_id: str, user: dict = Depends(get_current_user)):
    existing = await db.todos.find_one({"id": todo_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Todo not found")
    uid = str(user["_id"])
    if existing["created_by"] != uid and not has_permission(user, "can_manage_todos"):
        raise HTTPException(status_code=403, detail="Not allowed to delete this todo")
    await db.todos.delete_one({"id": todo_id})
    return {"ok": True}


async def check_todo_reminders():
    now = datetime.now(timezone.utc)
    one_hour_later = now + timedelta(hours=1)
    upcoming = await db.todos.find({
        "completed": False,
        "reminder_sent": False,
        "deadline": {"$ne": None},
    }).to_list(200)
    for todo in upcoming:
        try:
            deadline = datetime.fromisoformat(todo["deadline"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if now <= deadline <= one_hour_later:
            assigned = todo.get("assigned_to") or todo.get("created_by")
            if assigned:
                payload = {
                    "title": "Task Due Soon",
                    "body": f'"{todo["title"]}" is due within 1 hour!',
                    "url": "/todos",
                    "tag": f"todo-reminder-{todo['id']}",
                }
                await _push_to_user(assigned, payload)
            await db.todos.update_one({"id": todo["id"]}, {"$set": {"reminder_sent": True}})


# ---------------------------------------------------------------------------
# Admin / team management (owner only — only the owner can change permissions)
# ---------------------------------------------------------------------------
@api_router.get("/users")
async def list_users(user: dict = Depends(require_roles("owner"))):
    users = await db.users.find({"status": {"$ne": "pending"}}).sort("created_at", 1).to_list(1000)
    return [serialize_user(u) for u in users]


@api_router.get("/users/pending")
async def list_pending_users(user: dict = Depends(require_roles("owner"))):
    users = await db.users.find({"status": "pending"}).sort("created_at", 1).to_list(1000)
    return [serialize_user(u) for u in users]


@api_router.post("/users/{user_id}/approve")
async def approve_user(user_id: str, req: ApproveRequest, user: dict = Depends(require_roles("owner"))):
    if req.role not in ("member", "mentor"):
        raise HTTPException(status_code=400, detail="Role must be member or mentor")
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user id")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("status") != "pending":
        raise HTTPException(status_code=400, detail="This member has already been reviewed")
    perms = DEFAULT_ROLE_PERMISSIONS.get(req.role, DEFAULT_ROLE_PERMISSIONS["member"]).copy()
    await db.users.update_one({"_id": oid}, {"$set": {"status": "approved", "role": req.role, "permissions": perms}})
    updated = await db.users.find_one({"_id": oid})
    asyncio.create_task(dispatch_email(
        updated["email"],
        "You're in! Your robotics team account is approved",
        "<p>Your account has been approved. You can now sign in to the Robotics Hub.</p>",
    ))
    return serialize_user(updated)


@api_router.post("/users/{user_id}/reject")
async def reject_user(user_id: str, user: dict = Depends(require_roles("owner"))):
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user id")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("status") != "pending":
        raise HTTPException(status_code=400, detail="This member has already been reviewed")
    await db.users.delete_one({"_id": oid})
    return {"ok": True}


@api_router.get("/users/{user_id}/permissions")
async def get_user_permissions(user_id: str, user: dict = Depends(require_roles("owner"))):
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user id")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return {"permissions": get_permissions(target)}


@api_router.put("/users/{user_id}/permissions")
async def update_user_permissions(user_id: str, req: PermissionsUpdate, user: dict = Depends(require_roles("owner"))):
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user id")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("role") == "owner":
        raise HTTPException(status_code=400, detail="Owner permissions cannot be edited")
    cleaned = {k: bool(v) for k, v in (req.permissions or {}).items() if k in PERMISSION_KEYS}
    await db.users.update_one({"_id": oid}, {"$set": {"permissions": cleaned}})
    updated = await db.users.find_one({"_id": oid})
    return serialize_user(updated)


@api_router.put("/users/{user_id}/role")
async def update_user_role(user_id: str, req: RoleUpdate, user: dict = Depends(require_roles("owner"))):
    if req.role not in ("member", "mentor"):
        raise HTTPException(status_code=400, detail="Role must be member or mentor (owner is reserved)")
    target = await db.users.find_one({"_id": ObjectId(user_id)})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("role") == "owner":
        raise HTTPException(status_code=400, detail="Cannot change the owner's role")
    await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"role": req.role}})
    updated = await db.users.find_one({"_id": ObjectId(user_id)})
    return serialize_user(updated)


@api_router.post("/users")
async def create_user(req: CreateUserRequest, user: dict = Depends(require_roles("owner"))):
    email = req.email.lower()
    if email == OWNER_EMAIL:
        raise HTTPException(status_code=400, detail="This email is reserved for the owner")
    if req.role not in ("member", "mentor"):
        raise HTTPException(status_code=400, detail="Role must be member or mentor (owner is reserved)")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="A user with this email already exists")
    doc = {
        "email": email,
        "password_hash": hash_password(req.password),
        "name": req.name.strip() or email.split("@")[0],
        "role": req.role,
        "permissions": DEFAULT_ROLE_PERMISSIONS.get(req.role, DEFAULT_ROLE_PERMISSIONS["member"]).copy(),
        "phone": "",
        "carrier": "",
        "email_notifications": True,
        "sms_notifications": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    return serialize_user(doc)


@api_router.delete("/users/{user_id}")
async def delete_user(user_id: str, user: dict = Depends(require_roles("owner"))):
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user id")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("role") == "owner":
        raise HTTPException(status_code=400, detail="The owner account cannot be deleted")
    owned_files = await db.files.find({"uploaded_by": user_id, "is_deleted": False}, {"_id": 0}).to_list(1000)
    for f in owned_files:
        try:
            await asyncio.to_thread(delete_object, f["storage_path"])
        except Exception as e:
            logger.error(f"delete failed: {e}")
            raise HTTPException(status_code=500, detail="Delete failed")
    await db.users.delete_one({"_id": oid})
    await db.push_subscriptions.delete_many({"user_id": user_id})
    # Cascade: remove all user data
    await db.messages.delete_many({"user_id": user_id})
    await db.dm_messages.delete_many({"$or": [{"sender_id": user_id}, {"recipient_id": user_id}]})
    await db.files.update_many({"uploaded_by": user_id}, {"$set": {"is_deleted": True}})
    await db.events.update_many(
        {"created_by": user_id},
        {"$set": {"created_by": None, "creator_name": "Deleted user"}},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@api_router.get("/dashboard")
async def dashboard(user: dict = Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    next_event = await db.events.find_one({"date": {"$gte": now}}, {"_id": 0}, sort=[("date", 1)])
    if not next_event:
        next_event = await db.events.find_one({}, {"_id": 0}, sort=[("date", -1)])
    all_files = await db.files.find({"is_deleted": False}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    recent_files = []
    file_count = 0
    for f in all_files:
        if await _file_visible_to_user(f["id"], user):
            file_count += 1
            recent_files.append(f)
        if len(recent_files) == 4:
            break
    visible_ids = [c["id"] for c in CHANNELS if channel_visible_to(c["id"], user)]
    msg_count = await db.messages.count_documents({"channel_id": {"$in": visible_ids}})
    member_count = await db.users.count_documents({})
    return {
        "next_event": next_event,
        "file_count": file_count,
        "message_count": msg_count,
        "member_count": member_count,
        "recent_files": recent_files,
    }


@api_router.delete("/auth/me")
async def delete_own_account(user: dict = Depends(get_current_user)):
    """Allow a user to delete their own account and all associated data."""
    uid = str(user["_id"])
    if user.get("role") == "owner":
        raise HTTPException(status_code=400, detail="The owner account cannot be self-deleted")
    owned_files = await db.files.find({"uploaded_by": uid, "is_deleted": False}, {"_id": 0}).to_list(1000)
    for f in owned_files:
        try:
            await asyncio.to_thread(delete_object, f["storage_path"])
        except Exception as e:
            logger.error(f"delete failed: {e}")
            raise HTTPException(status_code=500, detail="Delete failed")
    await db.users.delete_one({"_id": user["_id"]})
    await db.push_subscriptions.delete_many({"user_id": uid})
    await db.messages.delete_many({"user_id": uid})
    await db.dm_messages.delete_many({"$or": [{"sender_id": uid}, {"recipient_id": uid}]})
    await db.files.update_many({"uploaded_by": uid}, {"$set": {"is_deleted": True}})
    await db.events.update_many(
        {"created_by": uid},
        {"$set": {"created_by": None, "creator_name": "Deleted user"}},
    )
    return {"ok": True}


@api_router.get("/")
async def root():
    return {"message": "Robotics Team Hub API"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.push_subscriptions.create_index("endpoint", unique=True)
    if _vapid:
        logger.info("[vapid] ready")
    else:
        logger.info("[webpush disabled] no VAPID key configured")
    # Seed owner
    existing = await db.users.find_one({"email": OWNER_EMAIL})
    if existing is None:
        await db.users.insert_one({
            "email": OWNER_EMAIL,
            "password_hash": hash_password(OWNER_PASSWORD),
            "name": OWNER_NAME,
            "role": "owner",
            "phone": "",
            "carrier": "",
            "email_notifications": True,
            "sms_notifications": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Owner account seeded")
    else:
        update = {"role": "owner"}
        if not verify_password(OWNER_PASSWORD, existing["password_hash"]):
            update["password_hash"] = hash_password(OWNER_PASSWORD)
        await db.users.update_one({"email": OWNER_EMAIL}, {"$set": update})
    try:
        await asyncio.to_thread(init_storage)
        logger.info("Storage initialized")
    except Exception as e:
        logger.error(f"Storage init failed: {e}")
    # Weekly digest scheduler — Wednesdays 10:00 America/Phoenix
    try:
        scheduler = AsyncIOScheduler(timezone="America/Phoenix")
        scheduler.add_job(
            build_and_send_weekly_digest,
            CronTrigger(day_of_week="wed", hour=10, minute=0, timezone="America/Phoenix"),
            id="weekly_digest", replace_existing=True,
        )
        scheduler.add_job(
            check_todo_reminders,
            IntervalTrigger(minutes=15),
            id="todo_reminders", replace_existing=True,
        )
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("Weekly digest scheduler started (Wed 10:00 America/Phoenix)")
        logger.info("Todo reminder scheduler started (every 15 min)")
    except Exception as e:
        logger.error(f"Scheduler start failed: {e}")


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
