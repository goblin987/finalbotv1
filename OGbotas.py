# -*- coding: utf-8 -*-
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
import pytz
from collections import defaultdict
from datetime import datetime, timedelta, time
import random
import logging
import asyncio
import pickle
import os
import sys
import re
import html
import json
import sqlite3
from pathlib import Path

# Configure logging with rotating logs
from logging.handlers import RotatingFileHandler
log_dir = Path(os.getenv('DATA_DIR', '/opt/render/data')) / 'logs'
log_dir.mkdir(parents=True, exist_ok=True)

# Setup rotating file handler
file_handler = RotatingFileHandler(
    log_dir / 'bot.log', 
    maxBytes=10*1024*1024,  # 10MB per file
    backupCount=5
)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Setup console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Configure root logger
logging.basicConfig(
    level=logging.INFO, 
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)
logger.info(f"Running on Python {sys.version}")

# Analytics and Metrics System
class BotAnalytics:
    def __init__(self, db_path):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize analytics database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS command_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    success BOOLEAN DEFAULT TRUE,
                    error_message TEXT
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS user_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    activity_type TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS system_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
    
    def log_command_usage(self, command, user_id, chat_id, success=True, error=None):
        """Log command usage for analytics"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO command_usage (command, user_id, chat_id, success, error_message)
                    VALUES (?, ?, ?, ?, ?)
                ''', (command, user_id, chat_id, success, error))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log command usage: {str(e)}")
    
    def log_user_activity(self, user_id, chat_id, activity_type, metadata=None):
        """Log user activity for engagement tracking"""
        try:
            metadata_json = json.dumps(metadata) if metadata else None
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO user_activity (user_id, chat_id, activity_type, metadata)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, chat_id, activity_type, metadata_json))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log user activity: {str(e)}")
    
    def log_system_metric(self, metric_name, value):
        """Log system performance metrics"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO system_metrics (metric_name, metric_value)
                    VALUES (?, ?)
                ''', (metric_name, value))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log system metric: {str(e)}")
    
    def get_usage_stats(self, days=7):
        """Get command usage statistics"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('''
                    SELECT command, COUNT(*) as count, 
                           AVG(CASE WHEN success THEN 1.0 ELSE 0.0 END) as success_rate
                    FROM command_usage 
                    WHERE timestamp > datetime('now', '-{} days')
                    GROUP BY command
                    ORDER BY count DESC
                '''.format(days))
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to get usage stats: {str(e)}")
            return []

# Initialize analytics
analytics_db_path = os.path.join(os.getenv('DATA_DIR', '/opt/render/data'), 'analytics.db')
analytics = BotAnalytics(analytics_db_path)

# Input validation functions
def sanitize_username(username: str) -> str:
    """Sanitize username input to prevent injection"""
    if not username:
        return ""
    # Remove non-alphanumeric characters except @ and underscore
    sanitized = re.sub(r'[^@a-zA-Z0-9_]', '', username)
    # Ensure it starts with @
    if not sanitized.startswith('@'):
        sanitized = '@' + sanitized.lstrip('@')
    # Limit length
    return sanitized[:33]  # Telegram username max is 32 chars + @

def sanitize_text_input(text: str, max_length: int = 500) -> str:
    """Sanitize text input to prevent XSS and limit length"""
    if not text:
        return ""
    # HTML escape the text
    sanitized = html.escape(text.strip())
    # Limit length
    return sanitized[:max_length]

def validate_amount(amount_str: str) -> tuple[bool, int]:
    """Validate and convert amount string to int with bounds checking"""
    try:
        amount = int(amount_str)
        # Reasonable bounds for points/votes
        if amount < -10000 or amount > 10000:
            return False, 0
        return True, amount
    except (ValueError, TypeError):
        return False, 0

def validate_chat_id(chat_id_str: str) -> tuple[bool, int]:
    """Validate chat ID format"""
    try:
        chat_id = int(chat_id_str)
        # Telegram chat IDs are typically negative for groups/channels
        if abs(chat_id) > 10**15:  # Reasonable upper bound
            return False, 0
        return True, chat_id
    except (ValueError, TypeError):
        return False, 0

# Network resilience functions
async def safe_send_message(bot, chat_id: int, text: str, retries: int = 3, **kwargs):
    """Send message with retry logic and error handling"""
    for attempt in range(retries):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except telegram.error.TimedOut:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
                continue
            logger.error(f"Message send timeout after {retries} attempts to chat {chat_id}")
            raise
        except telegram.error.NetworkError as e:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            logger.error(f"Network error after {retries} attempts: {str(e)}")
            raise
        except telegram.error.BadRequest as e:
            logger.warning(f"Bad request sending message to {chat_id}: {str(e)}")
            raise  # Don't retry bad requests
        except Exception as e:
            logger.error(f"Unexpected error sending message: {str(e)}")
            raise

async def safe_bot_operation(operation_func, *args, retries: int = 2, **kwargs):
    """Generic wrapper for bot operations with retry logic"""
    for attempt in range(retries):
        try:
            return await operation_func(*args, **kwargs)
        except (telegram.error.TimedOut, telegram.error.NetworkError) as e:
            if attempt < retries - 1:
                await asyncio.sleep(1.5 ** attempt)
                continue
            logger.error(f"Bot operation failed after {retries} attempts: {str(e)}")
            raise
        except Exception as e:
            logger.warning(f"Bot operation error: {str(e)}")
            raise

# Get sensitive information from environment variables
TOKEN = os.getenv('TELEGRAM_TOKEN')
try:
    ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
except (ValueError, TypeError):
    ADMIN_CHAT_ID = 0

try:
    GROUP_CHAT_ID = int(os.getenv('GROUP_CHAT_ID', '0'))
except (ValueError, TypeError):
    GROUP_CHAT_ID = 0

try:
    VOTING_GROUP_CHAT_ID = int(os.getenv('VOTING_GROUP_CHAT_ID', '0'))
except (ValueError, TypeError):
    VOTING_GROUP_CHAT_ID = 0

PASSWORD = os.getenv('PASSWORD', 'shoebot123')
VOTING_GROUP_LINK = os.getenv('VOTING_GROUP_LINK')
DATA_DIR = os.getenv('DATA_DIR', '/opt/render/data')  # Configurable data directory

# Check if required environment variables are set
if not TOKEN:
    logger.error("TELEGRAM_TOKEN environment variable is not set.")
    sys.exit(1)
if not ADMIN_CHAT_ID:
    logger.error("ADMIN_CHAT_ID environment variable is not set.")
    sys.exit(1)
if not GROUP_CHAT_ID:
    logger.error("GROUP_CHAT_ID environment variable is not set.")
    sys.exit(1)
if not VOTING_GROUP_CHAT_ID:
    logger.error("VOTING_GROUP_CHAT_ID environment variable is not set.")
    sys.exit(1)
if not VOTING_GROUP_LINK:
    logger.error("VOTING_GROUP_LINK environment variable is not set.")
    sys.exit(1)

# Constants
TIMEZONE = pytz.timezone('Europe/Vilnius')
COINFLIP_STICKER_ID = 'CAACAgIAAxkBAAEN32tnuPb-ovynJR5WNO1TQyv_ea17AC-RkAAtswEEqAzfrZRd8B1zYE'

# Data loading and saving functions
def load_data(filename, default):
    filepath = os.path.join(DATA_DIR, filename)
    try:
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            with open(filepath, 'rb') as f:
                data = pickle.load(f)
                logger.info(f"Loaded data from {filepath}")
                return data
        logger.info(f"No data found at {filepath}, returning default")
        return default
    except (FileNotFoundError, EOFError, pickle.UnpicklingError) as e:
        logger.error(f"Failed to load {filepath}: {str(e)}, returning default")
        return default

def save_data(data, filename):
    filepath = os.path.join(DATA_DIR, filename)
    if isinstance(data, defaultdict):
        data = dict(data)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(filepath, 'wb') as f:
            pickle.dump(data, f)
        logger.info(f"Saved data to {filepath}")
    except Exception as e:
        logger.error(f"Failed to save {filepath}: {str(e)}")
        raise  # Re-raise to catch persistence issues

# Load initial data
featured_media_id = load_data('featured_media_id.pkl', None)
featured_media_type = load_data('featured_media_type.pkl', None)
barygos_media_id = load_data('barygos_media_id.pkl', None)
barygos_media_type = load_data('barygos_media_type.pkl', None)
voting_message_id = load_data('voting_message_id.pkl', None)

PARDAVEJAI_MESSAGE_FILE = 'pardavejai_message.pkl'
DEFAULT_PARDAVEJAI_MESSAGE = "Pasirink pardavėją, už kurį nori balsuoti:"
pardavejai_message = load_data(PARDAVEJAI_MESSAGE_FILE, DEFAULT_PARDAVEJAI_MESSAGE)
last_addftbaryga_message = None
last_addftbaryga2_message = None

def save_pardavejai_message():
    save_data(pardavejai_message, PARDAVEJAI_MESSAGE_FILE)

# Scheduler setup
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
scheduler.add_executor(ThreadPoolExecutor(max_workers=10), alias='default')

async def configure_scheduler(application):
    logger.info("Configuring scheduler...")
    application.job_queue.scheduler = scheduler
    try:
        if not scheduler.running:
            scheduler.start()
            logger.info("Scheduler started successfully.")
        else:
            logger.info("Scheduler was already running.")
    except Exception as e:
        logger.error(f"Scheduler failed to start: {str(e)}")
        raise
    await initialize_voting_message(application)

# Bot initialization
application = Application.builder().token(TOKEN).post_init(configure_scheduler).build()
logger.info("Bot initialized")

# Data structures
trusted_sellers = ['@Seller1', '@Seller2', '@Seller3']
votes_weekly = load_data('votes_weekly.pkl', defaultdict(int))
votes_monthly = load_data('votes_monthly.pkl', defaultdict(list))
votes_alltime = load_data('votes_alltime.pkl', defaultdict(int))
voters = set()
downvoters = set()
pending_downvotes = {}
approved_downvotes = {}
vote_history = load_data('vote_history.pkl', defaultdict(list))
last_vote_attempt = defaultdict(lambda: datetime.min.replace(tzinfo=TIMEZONE))
last_downvote_attempt = defaultdict(lambda: datetime.min.replace(tzinfo=TIMEZONE))
complaint_id = 0
user_points = load_data('user_points.pkl', defaultdict(int))
coinflip_challenges = {}
daily_messages = defaultdict(lambda: defaultdict(int))
weekly_messages = defaultdict(int)
alltime_messages = load_data('alltime_messages.pkl', defaultdict(int))
chat_streaks = load_data('chat_streaks.pkl', defaultdict(int))
last_chat_day_raw = load_data('last_chat_day.pkl', {})
last_chat_day = defaultdict(lambda: datetime.min.replace(tzinfo=TIMEZONE), last_chat_day_raw)
allowed_groups = {str(GROUP_CHAT_ID)}  # Store as strings for consistency
valid_licenses = {'LICENSE-XYZ123', 'LICENSE-ABC456'}
pending_activation = {}
username_to_id = {}
polls = {}

def is_allowed_group(chat_id: str) -> bool:
    return str(chat_id) in allowed_groups

# Message deletion function
async def delete_message_job(context: telegram.ext.CallbackContext):
    job = context.job
    chat_id, message_id = job.data
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except telegram.error.BadRequest as e:
        if "Message to delete not found" in str(e):
            pass
        else:
            logger.error(f"Failed to delete message: {str(e)}")

# Initialize or update the persistent voting message
async def update_voting_message(context):
    global voting_message_id
    keyboard = [[InlineKeyboardButton(seller, callback_data=f"vote_{seller}")] for seller in trusted_sellers]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if voting_message_id:
            try:
                if featured_media_type == 'photo':
                    await context.bot.edit_message_media(
                        chat_id=VOTING_GROUP_CHAT_ID,
                        message_id=voting_message_id,
                        media=telegram.InputMediaPhoto(media=featured_media_id, caption=pardavejai_message),
                        reply_markup=reply_markup
                    )
                elif featured_media_type == 'animation':
                    await context.bot.edit_message_media(
                        chat_id=VOTING_GROUP_CHAT_ID,
                        message_id=voting_message_id,
                        media=telegram.InputMediaAnimation(media=featured_media_id, caption=pardavejai_message),
                        reply_markup=reply_markup
                    )
                elif featured_media_type == 'video':
                    await context.bot.edit_message_media(
                        chat_id=VOTING_GROUP_CHAT_ID,
                        message_id=voting_message_id,
                        media=telegram.InputMediaVideo(media=featured_media_id, caption=pardavejai_message),
                        reply_markup=reply_markup
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=VOTING_GROUP_CHAT_ID,
                        message_id=voting_message_id,
                        text=pardavejai_message,
                        reply_markup=reply_markup
                    )
                logger.info(f"Successfully updated voting message ID {voting_message_id}")
            except telegram.error.BadRequest as e:
                logger.warning(f"Failed to edit voting message ID {voting_message_id}: {str(e)}. Recreating...")
                voting_message_id = None
        if not voting_message_id:
            if featured_media_type == 'photo':
                msg = await context.bot.send_photo(
                    chat_id=VOTING_GROUP_CHAT_ID,
                    photo=featured_media_id,
                    caption=pardavejai_message,
                    reply_markup=reply_markup
                )
            elif featured_media_type == 'animation':
                msg = await context.bot.send_animation(
                    chat_id=VOTING_GROUP_CHAT_ID,
                    animation=featured_media_id,
                    caption=pardavejai_message,
                    reply_markup=reply_markup
                )
            elif featured_media_type == 'video':
                msg = await context.bot.send_video(
                    chat_id=VOTING_GROUP_CHAT_ID,
                    video=featured_media_id,
                    caption=pardavejai_message,
                    reply_markup=reply_markup
                )
            else:
                msg = await context.bot.send_message(
                    chat_id=VOTING_GROUP_CHAT_ID,
                    text=pardavejai_message,
                    reply_markup=reply_markup
                )
            voting_message_id = msg.message_id
            await context.bot.pin_chat_message(chat_id=VOTING_GROUP_CHAT_ID, message_id=voting_message_id)
            save_data(voting_message_id, 'voting_message_id.pkl')
            logger.info(f"Created and pinned new voting message ID {voting_message_id}")
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to update voting message: {str(e)}")

async def initialize_voting_message(application):
    if not voting_message_id:
        await update_voting_message(application)

# Command handlers
async def debug(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if user_id != ADMIN_CHAT_ID:
        await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        return
    chat_id = update.message.chat_id
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        admin_list = "\n".join([f"@{m.user.username or m.user.id} (ID: {m.user.id})" for m in admins])
        msg = await update.message.reply_text(f"Matomi adminai:\n{admin_list}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    except telegram.error.TelegramError as e:
        msg = await update.message.reply_text(f"Debug failed: {str(e)}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def whoami(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        username = f"@{member.user.username}" if member.user.username else "No username"
        msg = await update.message.reply_text(f"Jūs esate: {username} (ID: {user_id})")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    except telegram.error.TelegramError as e:
        msg = await update.message.reply_text(f"Error: {str(e)}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def startas(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if chat_id != user_id:
        if is_allowed_group(chat_id):
            msg = await update.message.reply_text(
                "🤖 Sveiki! Štai galimi veiksmai:\n\n"
                "📊 /balsuoju - Balsuoti už pardavėjus\n"
                "👎 /nepatiko @pardavejas priežastis - Pateikti skundą (5 tšk)\n"
                "💰 /points - Patikrinti savo taškus ir seriją\n"
                "👑 /chatking - Pokalbių lyderiai\n"
                "📈 /barygos - Pardavėjų reitingai\n"
                "🎯 /coinflip suma @vartotojas - Monetos metimas\n"
                "📋 /apklausa klausimas - Sukurti apklausą\n\n"
                "💬 Rašyk kasdien - gauk 1-3 taškus + serijos bonusą!"
            )
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        else:
            msg = await update.message.reply_text("Šis botas skirtas tik mano grupėms! Siųsk /startas Password privačiai!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    else:
        # Private chat
        if len(context.args) < 1:
            await update.message.reply_text("Naudok: /startas Password privačiai!")
            return
        
        password = sanitize_text_input(" ".join(context.args), max_length=100)
        if password == PASSWORD:
            pending_activation[user_id] = "password"
            await update.message.reply_text("Slaptažodis teisingas! Siųsk /activate_group GroupChatID.")
        else:
            await update.message.reply_text("Neteisingas slaptažodis!")
            logger.warning(f"Failed password attempt from user {user_id}")

async def activate_group(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if user_id != ADMIN_CHAT_ID:
        await update.message.reply_text("Tik adminas gali aktyvuoti grupes!")
        return
    if user_id not in pending_activation:
        await update.message.reply_text("Pirma įvesk slaptažodį privačiai!")
        return
    try:
        group_id = context.args[0]
        if group_id in allowed_groups:
            await update.message.reply_text("Grupė jau aktyvuota!")
        else:
            allowed_groups.add(group_id)
            if pending_activation[user_id] != "password":
                valid_licenses.remove(pending_activation[user_id])
            del pending_activation[user_id]
            await update.message.reply_text(f"Grupė {group_id} aktyvuota! Use /startas in the group.")
    except IndexError:
        await update.message.reply_text("Naudok: /activate_group GroupChatID")

async def privatus(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    chat_id = update.message.chat_id
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    keyboard = [[InlineKeyboardButton("Valdyti privačiai", url=f"https://t.me/{context.bot.username}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text("Spausk mygtuką, kad valdytum botą privačiai:", reply_markup=reply_markup)
    context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))

async def start_private(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if chat_id == user_id and user_id == ADMIN_CHAT_ID:
        keyboard = [
            [InlineKeyboardButton("Pridėti pardavėją", callback_data="admin_addseller")],
            [InlineKeyboardButton("Pašalinti pardavėją", callback_data="admin_removeseller")],
            [InlineKeyboardButton("Redaguoti /balsuoju tekstą", callback_data="admin_editpardavejai")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Sveikas, admin! Ką nori valdyti?", reply_markup=reply_markup)

async def handle_admin_button(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_CHAT_ID:
        await query.answer("Tik adminas gali tai daryti!")
        return
    chat_id = query.message.chat_id
    if chat_id != user_id:
        await query.answer("Šią komandą naudok privačiai!")
        return

    data = query.data
    if data == "admin_addseller":
        await query.edit_message_text("Įvesk: /addseller @VendorTag")
    elif data == "admin_removeseller":
        await query.edit_message_text("Įvesk: /removeseller @VendorTag")
    elif data == "admin_editpardavejai":
        await query.edit_message_text("Įvesk: /editpardavejai 'Naujas tekstas'")
    await query.answer()

async def balsuoju(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return

    msg = await update.message.reply_text(
        f'<a href="{VOTING_GROUP_LINK}">Spauskite čia</a> norėdami eiti į balsavimo grupę.\nTen rasite balsavimo mygtukus!',
        parse_mode=telegram.constants.ParseMode.HTML
    )
    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def handle_vote_button(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        logger.error("No callback query received")
        return
    
    user_id = query.from_user.id
    if query.message is None:
        await query.answer("Klaida: Balsavimo žinutė nerasta.")
        logger.error(f"Message is None for user_id={user_id}, callback_data={query.data}")
        return
    
    chat_id = query.message.chat_id
    data = query.data

    logger.info(f"Vote attempt by user_id={user_id} in chat_id={chat_id}, callback_data={data}")

    if not data.startswith("vote_"):
        logger.warning(f"Invalid callback data: {data} from user_id={user_id}")
        return

    seller = data.replace("vote_", "")
    if seller not in trusted_sellers:
        await query.answer("Šis pardavėjas nebegalioja!")
        logger.warning(f"Attempt to vote for invalid seller '{seller}' by user_id={user_id}")
        return

    now = datetime.now(TIMEZONE)
    last_vote = last_vote_attempt.get(user_id, datetime.min.replace(tzinfo=TIMEZONE))
    cooldown_remaining = timedelta(days=7) - (now - last_vote)
    if cooldown_remaining > timedelta(0):
        hours_left = max(1, int(cooldown_remaining.total_seconds() // 3600))
        await query.answer(f"Tu jau balsavai! Liko ~{hours_left} valandų iki kito balsavimo.")
        logger.info(f"User_id={user_id} blocked by cooldown, {hours_left} hours left.")
        return

    user_points.setdefault(user_id, 0)
    votes_weekly.setdefault(seller, 0)
    votes_alltime.setdefault(seller, 0)
    votes_monthly.setdefault(seller, [])

    votes_weekly[seller] += 1
    votes_monthly[seller].append((now, 1))
    votes_alltime[seller] += 1
    voters.add(user_id)
    vote_history[seller].append((user_id, "up", "Button vote", now))
    user_points[user_id] += 5
    last_vote_attempt[user_id] = now

    await query.answer("Ačiū už jūsų balsą, 5 taškai buvo pridėti prie jūsų sąskaitos.")
    
    # Get voter's username
    voter_username = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name or f"User {user_id}"
    
    confirmation_msg = await context.bot.send_message(chat_id=chat_id, text=f"{voter_username} balsavo už {seller}, 5 taškai buvo pridėti!")
    
    context.job_queue.run_once(delete_message_job, 120, data=(chat_id, confirmation_msg.message_id))
    
    save_data(votes_weekly, 'votes_weekly.pkl')
    save_data(votes_monthly, 'votes_monthly.pkl')
    save_data(votes_alltime, 'votes_alltime.pkl')
    save_data(vote_history, 'vote_history.pkl')
    save_data(user_points, 'user_points.pkl')

async def updatevoting(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali atnaujinti balsavimo mygtukus!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    await update_voting_message(context)
    msg = await update.message.reply_text("Balsavimo mygtukai atnaujinti!")
    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def addftbaryga(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali pridėti media!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    if not update.message.reply_to_message:
        msg = await update.message.reply_text("Atsakyk į žinutę su paveikslėliu, GIF ar video!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    global featured_media_id, featured_media_type, last_addftbaryga_message
    reply = update.message.reply_to_message
    if reply.photo:
        media = reply.photo[-1]
        featured_media_id = media.file_id
        featured_media_type = 'photo'
        last_addftbaryga_message = "Paveikslėlis pridėtas prie /balsuoju!"
    elif reply.animation:
        media = reply.animation
        featured_media_id = media.file_id
        featured_media_type = 'animation'
        last_addftbaryga_message = "GIF pridėtas prie /balsuoju!"
    elif reply.video:
        media = reply.video
        featured_media_id = media.file_id
        featured_media_type = 'video'
        last_addftbaryga_message = "Video pridėtas prie /balsuoju!"
    else:
        msg = await update.message.reply_text("Atsakyk į žinutę su paveikslėliu, GIF ar video!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    save_data(featured_media_id, 'featured_media_id.pkl')
    save_data(featured_media_type, 'featured_media_type.pkl')
    msg = await update.message.reply_text(last_addftbaryga_message)
    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    await update_voting_message(context)

async def addftbaryga2(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali pridėti media!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    if not update.message.reply_to_message:
        msg = await update.message.reply_text("Atsakyk į žinutę su paveikslėliu, GIF ar video!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    global barygos_media_id, barygos_media_type, last_addftbaryga2_message
    reply = update.message.reply_to_message
    if reply.photo:
        media = reply.photo[-1]
        barygos_media_id = media.file_id
        barygos_media_type = 'photo'
        last_addftbaryga2_message = "Paveikslėlis pridėtas prie /barygos!"
    elif reply.animation:
        media = reply.animation
        barygos_media_id = media.file_id
        barygos_media_type = 'animation'
        last_addftbaryga2_message = "GIF pridėtas prie /barygos!"
    elif reply.video:
        media = reply.video
        barygos_media_id = media.file_id
        barygos_media_type = 'video'
        last_addftbaryga2_message = "Video pridėtas prie /barygos!"
    else:
        msg = await update.message.reply_text("Atsakyk į žinutę su paveikslėliu, GIF ar video!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    save_data(barygos_media_id, 'barygos_media_id.pkl')
    save_data(barygos_media_type, 'barygos_media_type.pkl')
    msg = await update.message.reply_text(last_addftbaryga2_message)
    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def editpardavejai(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali redaguoti šį tekstą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return

    try:
        new_message = " ".join(context.args)
        if not new_message:
            msg = await update.message.reply_text("Naudok: /editpardavejai 'Naujas tekstas'")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        global pardavejai_message
        pardavejai_message = new_message
        save_pardavejai_message()
        msg = await update.message.reply_text(f"Pardavėjų žinutė atnaujinta: '{pardavejai_message}'")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        await update_voting_message(context)
    except IndexError:
        msg = await update.message.reply_text("Naudok: /editpardavejai 'Naujas tekstas'")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def apklausa(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id

    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return

    try:
        question = " ".join(context.args)
        if not question:
            msg = await update.message.reply_text("Naudok: /apklausa 'Klausimas'")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return

        poll_id = f"{chat_id}_{user_id}_{int(datetime.now(TIMEZONE).timestamp())}"
        polls[poll_id] = {"question": question, "yes": 0, "no": 0, "voters": set()}
        logger.info(f"Created poll with ID: {poll_id}")

        keyboard = [
            [InlineKeyboardButton("Taip (0)", callback_data=f"poll_{poll_id}_yes"),
             InlineKeyboardButton("Ne (0)", callback_data=f"poll_{poll_id}_no")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"📊 Apklausa: {question}", reply_markup=reply_markup)
    except IndexError:
        msg = await update.message.reply_text("Naudok: /apklausa 'Klausimas'")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def handle_poll_button(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if not data.startswith("poll_"):
        return

    parts = data.rsplit("_", 1)
    if len(parts) != 2:
        logger.error(f"Invalid callback data format: {data}")
        await query.answer("Klaida: Netinkamas balsavimo formatas!")
        return

    poll_id, vote = parts[0][5:], parts[1]
    if poll_id not in polls:
        await query.answer("Ši apklausa nebegalioja!")
        return

    poll = polls[poll_id]
    if user_id in poll["voters"]:
        await query.answer("Jau balsavai šioje apklausoje!")
        return

    poll["voters"].add(user_id)
    if vote == "yes":
        poll["yes"] += 1
    elif vote == "no":
        poll["no"] += 1
    else:
        logger.error(f"Invalid vote type: {vote}")
        await query.answer("Klaida balsuojant!")
        return

    keyboard = [
        [InlineKeyboardButton(f"Taip ({poll['yes']})", callback_data=f"poll_{poll_id}_yes"),
         InlineKeyboardButton(f"Ne ({poll['no']})", callback_data=f"poll_{poll_id}_no")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"📊 Apklausa: {poll['question']}\nBalsai: Taip - {poll['yes']}, Ne - {poll['no']}", reply_markup=reply_markup)
    await query.answer("Tavo balsas užskaitytas!")

async def cleanup_old_polls():
    """Clean up polls older than 24 hours to prevent memory leaks"""
    current_time = datetime.now(TIMEZONE).timestamp()
    polls_to_remove = []
    
    for poll_id in polls:
        try:
            # Extract timestamp from poll_id
            poll_timestamp = int(poll_id.split('_')[-1])
            if current_time - poll_timestamp > 86400:  # 24 hours
                polls_to_remove.append(poll_id)
        except (ValueError, IndexError):
            # If we can't parse timestamp, remove old format polls
            polls_to_remove.append(poll_id)
    
    for poll_id in polls_to_remove:
        del polls[poll_id]
    
    if polls_to_remove:
        logger.info(f"Cleaned up {len(polls_to_remove)} old polls")

async def cleanup_expired_challenges():
    """Clean up expired coinflip challenges to prevent memory leaks"""
    current_time = datetime.now(TIMEZONE)
    challenges_to_remove = []
    
    for user_id, (initiator_id, amount, timestamp, initiator_username, opponent_username, chat_id) in coinflip_challenges.items():
        if current_time - timestamp > timedelta(minutes=10):  # 10 minutes expiry
            challenges_to_remove.append(user_id)
    
    for user_id in challenges_to_remove:
        del coinflip_challenges[user_id]
    
    if challenges_to_remove:
        logger.info(f"Cleaned up {len(challenges_to_remove)} expired coinflip challenges")

async def nepatiko(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    now = datetime.now(TIMEZONE)
    last_downvote_attempt[user_id] = last_downvote_attempt.get(user_id, datetime.min.replace(tzinfo=TIMEZONE))
    if now - last_downvote_attempt[user_id] < timedelta(days=7):
        msg = await update.message.reply_text("Palauk 7 dienas po paskutinio nepritarimo!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Input validation
    if len(context.args) < 2:
        msg = await update.message.reply_text("Naudok: /nepatiko @VendorTag 'Reason'")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Sanitize vendor username
    vendor = sanitize_username(context.args[0])
    if not vendor or len(vendor) < 2:
        msg = await update.message.reply_text("Netinkamas pardavėjo vardas! Naudok @username formatą.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Sanitize and validate reason
    reason = sanitize_text_input(" ".join(context.args[1:]), max_length=200)
    if not reason or len(reason.strip()) < 3:
        msg = await update.message.reply_text("Prašau nurodyti išsamią priežastį (bent 3 simboliai)!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check if vendor exists in trusted sellers
    if vendor not in trusted_sellers:
        msg = await update.message.reply_text(f"{vendor} nėra patikimų pardavėjų sąraše!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Rate limiting check
    user_complaints_today = sum(1 for _, (_, uid, _, ts) in pending_downvotes.items() 
                               if uid == user_id and now - ts < timedelta(hours=24))
    if user_complaints_today >= 3:
        msg = await update.message.reply_text("Per daug skundų per dieną! Palauk.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        global complaint_id
        complaint_id += 1
        pending_downvotes[complaint_id] = (vendor, user_id, reason, now)
        downvoters.add(user_id)
        vote_history.setdefault(vendor, []).append((user_id, "down", reason, now))
        user_points[user_id] = user_points.get(user_id, 0) + 5
        last_downvote_attempt[user_id] = now
        
        admin_message = f"Skundas #{complaint_id}: {vendor} - '{reason}' by User {user_id}. Patvirtinti su /approve {complaint_id}"
        await safe_send_message(context.bot, ADMIN_CHAT_ID, admin_message)
        
        msg = await update.message.reply_text(f"Skundas pateiktas! Atsiųsk įrodymus @kunigasnew dėl Skundo #{complaint_id}. +5 taškų!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(vote_history, 'vote_history.pkl')
        save_data(user_points, 'user_points.pkl')
    except Exception as e:
        logger.error(f"Error processing complaint: {str(e)}")
        msg = await update.message.reply_text("Klaida pateikiant skundą. Bandyk vėliau.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def approve(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        return
    if not (is_allowed_group(chat_id) or chat_id == user_id):
        msg = await update.message.reply_text("Ši komanda veikia tik grupėje arba privačiai!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        cid = int(context.args[0])
        if cid not in pending_downvotes:
            msg = await update.message.reply_text("Neteisingas skundo ID!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        vendor, user_id, reason, timestamp = pending_downvotes[cid]
        votes_weekly[vendor] -= 1
        votes_monthly[vendor].append((timestamp, -1))
        votes_alltime[vendor] -= 1
        approved_downvotes[cid] = pending_downvotes[cid]
        del pending_downvotes[cid]
        msg = await update.message.reply_text(f"Skundas patvirtintas dėl {vendor}!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(votes_weekly, 'votes_weekly.pkl')
        save_data(votes_monthly, 'votes_monthly.pkl')
        save_data(votes_alltime, 'votes_alltime.pkl')
    except (IndexError, ValueError):
        msg = await update.message.reply_text("Naudok: /approve ComplaintID")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def addseller(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali pridėti pardavėją!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    if not is_allowed_group(chat_id) and chat_id != user_id:
        msg = await update.message.reply_text("Botas neveikia šioje grupėje arba naudok privačiai!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Input validation
    if len(context.args) < 1:
        msg = await update.message.reply_text("Naudok: /addseller @VendorTag")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Sanitize vendor username
    vendor = sanitize_username(context.args[0])
    if not vendor or len(vendor) < 2:
        msg = await update.message.reply_text("Netinkamas pardavėjo vardas! Naudok @username formatą.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check if already exists
    if vendor in trusted_sellers:
        msg = await update.message.reply_text(f"{vendor} jau yra patikimų pardavėjų sąraše!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check seller limit
    if len(trusted_sellers) >= 50:  # Reasonable limit
        msg = await update.message.reply_text("Per daug pardavėjų! Pašalink senus prieš pridedant naujus.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        trusted_sellers.append(vendor)
        msg = await update.message.reply_text(f"Pardavėjas {vendor} pridėtas! Jis dabar matomas /balsuoju sąraše.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        await update_voting_message(context)
        logger.info(f"Admin {user_id} added seller: {vendor}")
    except Exception as e:
        logger.error(f"Error adding seller: {str(e)}")
        trusted_sellers.remove(vendor) if vendor in trusted_sellers else None
        msg = await update.message.reply_text("Klaida pridedant pardavėją!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def removeseller(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali pašalinti pardavėją!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    if not is_allowed_group(chat_id) and chat_id != user_id:
        msg = await update.message.reply_text("Botas neveikia šioje grupėje arba naudok privačiai!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Input validation
    if len(context.args) < 1:
        msg = await update.message.reply_text("Naudok: /removeseller @VendorTag")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Sanitize vendor username
    vendor = sanitize_username(context.args[0])
    if not vendor or len(vendor) < 2:
        msg = await update.message.reply_text("Netinkamas pardavėjo vardas! Naudok @username formatą.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if vendor not in trusted_sellers:
        msg = await update.message.reply_text(f"'{vendor}' nėra patikimų pardavėjų sąraše! Sąrašas: {trusted_sellers}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        trusted_sellers.remove(vendor)
        votes_weekly.pop(vendor, None)
        votes_monthly.pop(vendor, None)
        votes_alltime.pop(vendor, None)
        vote_history.pop(vendor, None)  # Also remove vote history
        
        msg = await update.message.reply_text(f"Pardavėjas {vendor} pašalintas iš sąrašo ir balsų!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        await update_voting_message(context)
        
        # Save all affected data
        save_data(votes_weekly, 'votes_weekly.pkl')
        save_data(votes_monthly, 'votes_monthly.pkl')
        save_data(votes_alltime, 'votes_alltime.pkl')
        save_data(vote_history, 'vote_history.pkl')
        logger.info(f"Admin {user_id} removed seller: {vendor}")
    except Exception as e:
        logger.error(f"Error removing seller: {str(e)}")
        msg = await update.message.reply_text("Klaida šalinant pardavėją!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def sellerinfo(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        vendor = context.args[0]
        if not vendor.startswith('@'):
            vendor = '@' + vendor
        if vendor not in trusted_sellers:
            msg = await update.message.reply_text(f"{vendor} nėra patikimas pardavėjas!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        now = datetime.now(TIMEZONE)
        monthly_score = sum(s for ts, s in votes_monthly[vendor] if now - ts < timedelta(days=30))
        downvotes_30d = sum(1 for cid, (v, _, _, ts) in approved_downvotes.items() if v == vendor and now - ts < timedelta(days=30))
        info = f"{vendor} Info:\nSavaitė: {votes_weekly[vendor]}\nMėnuo: {monthly_score}\nViso: {votes_alltime[vendor]}\nNeigiami (30d): {downvotes_30d}"
        msg = await update.message.reply_text(info)
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    except IndexError:
        msg = await update.message.reply_text("Naudok: /pardavejoinfo @VendorTag")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def barygos(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    now = datetime.now(TIMEZONE)
    
    # Create mobile-friendly header
    header = "🏆 **PARDAVĖJŲ REITINGAI** 🏆\n"
    header += f"📅 {now.strftime('%Y-%m-%d %H:%M')}\n\n"
    
    # Add custom admin message if exists
    if last_addftbaryga2_message:
        header += f"📢 {last_addftbaryga2_message}\n\n"
    
    # Build mobile-friendly Weekly Leaderboard
    weekly_board = "🔥 **SAVAITĖS ČEMPIONAI** 🔥\n"
    weekly_board += f"📊 {now.strftime('%V savaitė')}\n\n"
    
    if not votes_weekly:
        weekly_board += "😴 Dar nėra balsų šią savaitę\n"
        weekly_board += "Būk pirmas - balsuok dabar!\n\n"
    else:
        sorted_weekly = sorted(votes_weekly.items(), key=lambda x: x[1], reverse=True)
        
        for i, (vendor, score) in enumerate(sorted_weekly[:10], 1):
            # Create trophy icons based on position
            if i == 1:
                icon = "🥇"
            elif i == 2:
                icon = "🥈"
            elif i == 3:
                icon = "🥉"
            elif i <= 5:
                icon = "🏅"
            else:
                icon = "📈"
            
            # Format vendor name (remove @)
            vendor_name = vendor[1:] if vendor.startswith('@') else vendor
            
            weekly_board += f"{icon} **{i}. {vendor_name}** — {score} balsų\n"
    
    weekly_board += "\n" + "─" * 25 + "\n\n"
    
    # Build Monthly Leaderboard
    monthly_board = "🗓️ **MĖNESIO LYDERIAI** 🗓️\n"
    monthly_board += f"📊 {now.strftime('%B %Y')}\n\n"
    
    # Calculate current calendar month totals
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_totals = defaultdict(int)
    for vendor, votes_list in votes_monthly.items():
        current_month_votes = [(ts, s) for ts, s in votes_list if ts >= month_start]
        monthly_totals[vendor] = sum(s for _, s in current_month_votes)
    
    if not monthly_totals:
        monthly_board += "🌱 Naujas mėnuo - nauji tikslai\n"
        monthly_board += "Pradėk balsuoti dabar!\n\n"
    else:
        sorted_monthly = sorted(monthly_totals.items(), key=lambda x: x[1], reverse=True)
        
        for i, (vendor, score) in enumerate(sorted_monthly[:10], 1):
            # Create crown icons for monthly leaders
            if i == 1:
                icon = "👑"
            elif i == 2:
                icon = "💎"
            elif i == 3:
                icon = "⭐"
            else:
                icon = "🌟"
            
            vendor_name = vendor[1:] if vendor.startswith('@') else vendor
            monthly_board += f"{icon} **{i}. {vendor_name}** — {score} balsų\n"
    
    monthly_board += "\n" + "─" * 25 + "\n\n"
    
    # Build All-Time Hall of Fame
    alltime_board = "🌟 **VISŲ LAIKŲ LEGENDOS** 🌟\n"
    alltime_board += "📈 Istoriniai rekordai\n\n"
    
    if not votes_alltime:
        alltime_board += "🎯 Istorija tik prasideda\n"
        alltime_board += "Tapk pirmąja legenda!\n\n"
    else:
        sorted_alltime = sorted(votes_alltime.items(), key=lambda x: x[1], reverse=True)
        
        for i, (vendor, score) in enumerate(sorted_alltime[:10], 1):
            # Special icons for hall of fame
            if i == 1:
                icon = "🏆"
            elif i == 2:
                icon = "🎖️"
            elif i == 3:
                icon = "🎗️"
            elif score >= 100:
                icon = "💫"
            elif score >= 50:
                icon = "⚡"
            else:
                icon = "🔸"
            
            vendor_name = vendor[1:] if vendor.startswith('@') else vendor
            alltime_board += f"{icon} **{i}. {vendor_name}** — {score} balsų\n"
    
    alltime_board += "\n" + "─" * 25 + "\n\n"
    
    # Add simplified footer
    footer = "📊 **STATISTIKOS**\n"
    total_weekly_votes = sum(votes_weekly.values())
    total_monthly_votes = sum(monthly_totals.values())
    total_alltime_votes = sum(votes_alltime.values())
    active_sellers = len([v for v in votes_weekly.values() if v > 0])
    
    footer += f"📈 Savaitės balsų: **{total_weekly_votes}**\n"
    footer += f"📅 Mėnesio balsų: **{total_monthly_votes}**\n"
    footer += f"🌟 Visų laikų balsų: **{total_alltime_votes}**\n"
    footer += f"👥 Aktyvūs pardavėjai: **{active_sellers}**\n\n"
    
    # Add next reset information
    next_sunday = now + timedelta(days=(6 - now.weekday()))
    next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
    
    footer += "⏰ **KITAS RESTARTAS**\n"
    footer += f"• Savaitės: {next_sunday.strftime('%m-%d %H:%M')}\n"
    footer += f"• Mėnesio: {next_month.strftime('%m-%d %H:%M')}\n\n"
    
    footer += "💡 Balsuok kas savaitę už mėgstamus pardavėjus!\n"
    footer += "🎯 Skundai padeda kokybei (+5 tšk)"
    
    # Combine all sections - ensure all parts are included
    full_message = header + weekly_board + monthly_board + alltime_board + footer
    
    # Debug: Log message length for troubleshooting
    logger.info(f"Barygos message length: {len(full_message)} characters")
    
    try:
        # Check message length - if too long for caption, send as separate text message
        if len(full_message) > 1000 and barygos_media_id and barygos_media_type:
            # Send media without caption first
            if barygos_media_type == 'photo':
                await context.bot.send_photo(chat_id=chat_id, photo=barygos_media_id)
            elif barygos_media_type == 'animation':
                await context.bot.send_animation(chat_id=chat_id, animation=barygos_media_id)
            elif barygos_media_type == 'video':
                await context.bot.send_video(chat_id=chat_id, video=barygos_media_id)
            
            # Then send full message as text
            msg = await context.bot.send_message(
                chat_id=chat_id, 
                text=full_message,
                parse_mode='Markdown'
            )
        elif barygos_media_id and barygos_media_type:
            # Message is short enough for caption
            if barygos_media_type == 'photo':
                msg = await context.bot.send_photo(
                    chat_id=chat_id, 
                    photo=barygos_media_id, 
                    caption=full_message,
                    parse_mode='Markdown'
                )
            elif barygos_media_type == 'animation':
                msg = await context.bot.send_animation(
                    chat_id=chat_id, 
                    animation=barygos_media_id, 
                    caption=full_message,
                    parse_mode='Markdown'
                )
            elif barygos_media_type == 'video':
                msg = await context.bot.send_video(
                    chat_id=chat_id, 
                    video=barygos_media_id, 
                    caption=full_message,
                    parse_mode='Markdown'
                )
            else:
                msg = await context.bot.send_message(
                    chat_id=chat_id, 
                    text=full_message,
                    parse_mode='Markdown'
                )
        else:
            # No media, send as text
            msg = await context.bot.send_message(
                chat_id=chat_id, 
                text=full_message,
                parse_mode='Markdown'
            )
        
        context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))  # Keep longer for reading
        
    except telegram.error.TelegramError as e:
        # Fallback without markdown if formatting fails
        logger.error(f"Error sending formatted barygos message: {str(e)}")
        try:
            fallback_message = full_message.replace('**', '').replace('*', '')
            msg = await context.bot.send_message(chat_id=chat_id, text=fallback_message)
            context.job_queue.run_once(delete_message_job, 90, data=(chat_id, msg.message_id))
        except Exception as fallback_error:
            logger.error(f"Fallback message also failed: {str(fallback_error)}")
            msg = await update.message.reply_text("❌ Klaida gaunant pardavėjų reitingus!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def chatking(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if not alltime_messages:
        msg = await update.message.reply_text(
            "👑 **POKALBIŲ LYDERIAI** 👑\n\n"
            "🤐 Dar nėra žinučių istorijoje!\n"
            "Pradėk pokalbį ir tapk pirmuoju!"
        )
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Build beautiful header
    now = datetime.now(TIMEZONE)
    header = "👑✨ **POKALBIŲ IMPERATORIAI** ✨👑\n"
    header += f"📅 {now.strftime('%Y-%m-%d %H:%M')}\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Get top chatters
    sorted_chatters = sorted(alltime_messages.items(), key=lambda x: x[1], reverse=True)[:15]
    max_messages = sorted_chatters[0][1] if sorted_chatters else 1
    
    leaderboard = "🏆 **VISŲ LAIKŲ TOP POKALBININKAI** 🏆\n"
    leaderboard += "┌───────────────────────────────────────┐\n"
    
    for i, (user_id, msg_count) in enumerate(sorted_chatters, 1):
        try:
            # Try to get username from our mapping first
            username = next((k for k, v in username_to_id.items() if v == user_id), None)
            
            if not username:
                # Try to get from Telegram API
                try:
                    member = await context.bot.get_chat_member(chat_id, user_id)
                    if member.user.username:
                        username = f"@{member.user.username}"
                    else:
                        username = member.user.first_name or f"User {user_id}"
                except telegram.error.TelegramError:
                    username = f"User {user_id}"
            
            # Create crown icons based on ranking
            if i == 1:
                icon = "👑"
            elif i == 2:
                icon = "🥈"
            elif i == 3:
                icon = "🥉"
            elif i <= 5:
                icon = "🏅"
            elif i <= 10:
                icon = "⭐"
            else:
                icon = "🌟"
            
            # Create progress bar
            progress = msg_count / max(max_messages, 1)
            bar_length = 12
            filled = int(progress * bar_length)
            progress_bar = "█" * filled + "░" * (bar_length - filled)
            
            # Format username (remove @ if present, limit length)
            display_name = username[1:] if username.startswith('@') else username
            display_name = display_name[:10] if len(display_name) > 10 else display_name
            
            # Create achievement levels
            if msg_count >= 10000:
                level = "🔥LEGENDA"
            elif msg_count >= 5000:
                level = "⚡EKSPERTAS"
            elif msg_count >= 1000:
                level = "💎MEISTRAS"
            elif msg_count >= 500:
                level = "🌟AKTYVUS"
            elif msg_count >= 100:
                level = "📈NAUJOKAS"
            else:
                level = "🌱PRADŽIA"
            
            leaderboard += f"│{icon} {i:2d}. {display_name:<10} │{msg_count:4d}│{progress_bar}│{level}\n"
            
        except Exception as e:
            logger.error(f"Error processing user {user_id} in chatking: {str(e)}")
            leaderboard += f"│💬 {i:2d}. User {user_id}     │{msg_count:4d}│{'█' * 8 + '░' * 4}│🤖ERROR\n"
    
    leaderboard += "└───────────────────────────────────────┘\n\n"
    
    # Add statistics and achievements info
    footer = "📊 **GRUPĖS STATISTIKOS**\n"
    total_messages = sum(alltime_messages.values())
    active_users = len([count for count in alltime_messages.values() if count >= 10])
    super_active = len([count for count in alltime_messages.values() if count >= 1000])
    
    footer += f"• Visų žinučių: {total_messages:,} 💬\n"
    footer += f"• Aktyvūs nariai: {active_users} 👥\n"
    footer += f"• Super aktyvūs: {super_active} 🔥\n"
    footer += f"• Vidurkis per narį: {total_messages // len(alltime_messages) if alltime_messages else 0} 📈\n\n"
    
    footer += "🎯 **PASIEKIMŲ LYGIAI**\n"
    footer += "🌱 Pradžia: 1-99 žinučių\n"
    footer += "📈 Naujokas: 100-499 žinučių\n"
    footer += "🌟 Aktyvus: 500-999 žinučių\n"
    footer += "💎 Meistras: 1,000-4,999 žinučių\n"
    footer += "⚡ Ekspertas: 5,000-9,999 žinučių\n"
    footer += "🔥 Legenda: 10,000+ žinučių\n\n"
    
    footer += "💬 Tęsk pokalbius ir kilk lyderių lentoje!"
    
    full_message = header + leaderboard + footer
    
    try:
        msg = await update.message.reply_text(full_message, parse_mode='Markdown')
        context.job_queue.run_once(delete_message_job, 90, data=(chat_id, msg.message_id))
    except telegram.error.TelegramError as e:
        # Fallback without markdown
        logger.error(f"Error sending formatted chatking: {str(e)}")
        fallback_message = full_message.replace('**', '').replace('*', '')
        msg = await update.message.reply_text(fallback_message)
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))

async def handle_message(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chat_id = update.message.chat_id
        if not is_allowed_group(chat_id):
            return
        
        # Check message validity
        if not update.message.text or update.message.text.startswith('/'):
            return
        
        # Check message length to prevent spam
        if len(update.message.text) > 4000:  # Telegram's limit is 4096
            return
        
        user_id = update.message.from_user.id
        username = update.message.from_user.username
        
        # Validate user data
        if not user_id or user_id <= 0:
            logger.warning(f"Invalid user_id received: {user_id}")
            return
        
        # Update username mapping if available
        if username and len(username) <= 32:  # Telegram username limit
            clean_username = re.sub(r'[^a-zA-Z0-9_]', '', username)
            if clean_username:
                username_to_id[f"@{clean_username.lower()}"] = user_id
        
        today = datetime.now(TIMEZONE)
        daily_messages[user_id][today.date()] += 1
        weekly_messages[user_id] += 1
        alltime_messages.setdefault(user_id, 0)
        alltime_messages[user_id] += 1
        
        # Update chat streaks safely
        yesterday = today - timedelta(days=1)
        last_day = last_chat_day[user_id].date()
        if last_day == yesterday.date():
            chat_streaks[user_id] = chat_streaks.get(user_id, 0) + 1
        elif last_day == today.date():
            # User already chatted today, don't increment streak
            pass
        elif last_day < yesterday.date():
            chat_streaks[user_id] = 1  # Reset if more than a day has passed
        last_chat_day[user_id] = today
        
        # Save data less frequently to improve performance
        # Only save every 10th message or for new users
        if alltime_messages[user_id] % 10 == 0 or alltime_messages[user_id] == 1:
            try:
                save_data(alltime_messages, 'alltime_messages.pkl')
                save_data(chat_streaks, 'chat_streaks.pkl')
                save_data(last_chat_day, 'last_chat_day.pkl')
            except Exception as e:
                logger.error(f"Error saving message data: {str(e)}")
                # Continue execution even if save fails
    
    except Exception as e:
        logger.error(f"Error in handle_message: {str(e)}")
        # Don't crash the bot on message handling errors

async def award_daily_points(context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(TIMEZONE).date()
    yesterday = today - timedelta(days=1)
    for user_id in daily_messages:
        msg_count = daily_messages[user_id].get(yesterday, 0)
        if msg_count < 50:
            continue
        
        chat_points = min(3, max(1, msg_count // 50))
        streak_bonus = max(0, chat_streaks.get(user_id, 0) // 3)
        total_points = chat_points + streak_bonus
        user_points[user_id] = user_points.get(user_id, 0) + total_points
        
        msg = f"Gavai {chat_points} taškus už {msg_count} žinučių vakar!"
        if streak_bonus > 0:
            msg += f" +{streak_bonus} už {chat_streaks[user_id]}-dienų seriją!"
        
        try:
            username = next((k for k, v in username_to_id.items() if v == user_id), f"User {user_id}")
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"{username}, {msg} Dabar turi {user_points[user_id]} taškų!"
            )
        except (StopIteration, telegram.error.TelegramError) as e:
            logger.error(f"Failed to send daily points message to user {user_id}: {str(e)}")
    
    daily_messages.clear()
    save_data(user_points, 'user_points.pkl')

async def weekly_recap(context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Send weekly recap and reset weekly votes"""
    logger.info("Starting weekly recap and reset...")
    
    # Send chat recap first
    if not weekly_messages:
        try:
            await context.bot.send_message(GROUP_CHAT_ID, "Šią savaitę nebuvo pokalbių!")
        except telegram.error.TelegramError as e:
            logger.error(f"Failed to send weekly recap (no messages): {str(e)}")
    else:
        sorted_chatters = sorted(weekly_messages.items(), key=lambda x: x[1], reverse=True)[:3]
        recap = "📢 Savaitės Pokalbių Karaliai 📢\n"
        for user_id, msg_count in sorted_chatters:
            try:
                username = next((k for k, v in username_to_id.items() if v == user_id), f"User {user_id}")
                recap += f"{username}: {msg_count} žinučių\n"
            except Exception as e:
                logger.error(f"Error processing user {user_id} in weekly recap: {str(e)}")
                recap += f"User {user_id}: {msg_count} žinučių\n"
        
        try:
            await context.bot.send_message(GROUP_CHAT_ID, recap)
        except telegram.error.TelegramError as e:
            logger.error(f"Failed to send weekly recap: {str(e)}")
    
    # Send voting recap if there were votes
    if votes_weekly:
        sorted_sellers = sorted(votes_weekly.items(), key=lambda x: x[1], reverse=True)[:5]
        vote_recap = "🏆 Savaitės Balsavimo Nugalėtojai 🏆\n"
        for seller, votes in sorted_sellers:
            vote_recap += f"{seller[1:]}: {votes} balsų\n"
        
        try:
            await context.bot.send_message(GROUP_CHAT_ID, vote_recap)
        except telegram.error.TelegramError as e:
            logger.error(f"Failed to send voting recap: {str(e)}")
    
    # Reset weekly data
    await reset_weekly_data(context)
    
    logger.info("Weekly recap and reset completed.")

async def reset_weekly_data(context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Reset all weekly data"""
    global votes_weekly, voters, downvoters, pending_downvotes, complaint_id, last_vote_attempt, weekly_messages
    
    logger.info("Resetting weekly data...")
    
    # Clear weekly votes and related data
    votes_weekly.clear()
    voters.clear()
    downvoters.clear()
    pending_downvotes.clear()
    last_vote_attempt.clear()
    weekly_messages.clear()
    complaint_id = 0
    
    # Save cleared data
    save_data(votes_weekly, 'votes_weekly.pkl')
    save_data(user_points, 'user_points.pkl')  # Save any pending point changes
    
    # Notify group
    try:
        await context.bot.send_message(GROUP_CHAT_ID, "🔄 Nauja balsavimo savaitė prasidėjo! Visi gali vėl balsuoti.")
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to send weekly reset notification: {str(e)}")
    
    logger.info("Weekly data reset completed.")

async def monthly_recap_and_reset(context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Send monthly recap and reset monthly votes"""
    logger.info("Starting monthly recap and reset...")
    
    # Calculate current month totals
    now = datetime.now(TIMEZONE)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    if votes_monthly:
        monthly_totals = defaultdict(int)
        for vendor, votes_list in votes_monthly.items():
            current_month_votes = [(ts, s) for ts, s in votes_list if ts >= month_start]
            monthly_totals[vendor] = sum(s for _, s in current_month_votes)
        
        if monthly_totals:
            sorted_monthly = sorted(monthly_totals.items(), key=lambda x: x[1], reverse=True)[:5]
            monthly_recap = "🗓️ Mėnesio Balsavimo Čempionai 🗓️\n"
            for seller, votes in sorted_monthly:
                monthly_recap += f"{seller[1:]}: {votes} balsų\n"
            
            try:
                await context.bot.send_message(GROUP_CHAT_ID, monthly_recap)
            except telegram.error.TelegramError as e:
                logger.error(f"Failed to send monthly recap: {str(e)}")
    
    # Reset monthly data  
    await reset_monthly_data(context)
    
    logger.info("Monthly recap and reset completed.")

async def reset_monthly_data(context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Reset monthly voting data"""
    global votes_monthly
    
    logger.info("Resetting monthly data...")
    
    # Clear monthly votes
    votes_monthly.clear()
    save_data(votes_monthly, 'votes_monthly.pkl')
    
    # Notify group
    try:
        await context.bot.send_message(GROUP_CHAT_ID, "📅 Naujas balsavimo mėnuo prasidėjo!")
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to send monthly reset notification: {str(e)}")
    
    logger.info("Monthly data reset completed.")

async def coinflip(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    initiator_id = update.message.from_user.id
    
    # Input validation
    if len(context.args) < 2:
        msg = await update.message.reply_text("Naudok: /coinflip Amount @Username")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Validate amount
    valid_amount, amount = validate_amount(context.args[0])
    if not valid_amount or amount <= 0:
        msg = await update.message.reply_text("Netinkama suma! Naudok teigiamą skaičių (1-10000).")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Sanitize opponent username
    opponent = sanitize_username(context.args[1])
    if not opponent or len(opponent) < 2:
        msg = await update.message.reply_text("Netinkamas vartotojo vardas! Naudok @username formatą.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check initiator points
    if user_points.get(initiator_id, 0) < amount:
        msg = await update.message.reply_text("Neturi pakankamai taškų!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        initiator_member = await safe_bot_operation(context.bot.get_chat_member, chat_id, initiator_id)
        initiator_username = f"@{initiator_member.user.username}" if initiator_member.user.username else f"@User{initiator_id}"

        target_id = username_to_id.get(opponent.lower(), None)
        if not target_id or target_id == initiator_id:
            msg = await update.message.reply_text("Negalima mesti iššūkio sau ar neegzistuojančiam vartotojui!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        
        if user_points.get(target_id, 0) < amount:
            msg = await update.message.reply_text(f"{opponent} neturi pakankamai taškų!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        
        # Check for existing challenge
        if target_id in coinflip_challenges:
            msg = await update.message.reply_text(f"{opponent} jau turi aktyvų iššūkį!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        
        coinflip_challenges[target_id] = (initiator_id, amount, datetime.now(TIMEZONE), initiator_username, opponent, chat_id)
        msg = await update.message.reply_text(f"{initiator_username} iššaukė {opponent} monetos metimui už {amount} taškų! Priimk su /accept_coinflip!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        context.job_queue.run_once(expire_challenge, 300, data=(target_id, context))
    except telegram.error.TelegramError as e:
        logger.error(f"Error in coinflip: {str(e)}")
        msg = await update.message.reply_text("Klaida gaunant vartotojo informaciją!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def accept_coinflip(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    if user_id not in coinflip_challenges:
        msg = await update.message.reply_text("Nėra aktyvaus iššūkio!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    initiator_id, amount, timestamp, initiator_username, opponent_username, original_chat_id = coinflip_challenges[user_id]
    now = datetime.now(TIMEZONE)
    if now - timestamp > timedelta(minutes=5) or chat_id != original_chat_id:
        del coinflip_challenges[user_id]
        msg = await update.message.reply_text("Iššūkis pasibaigė arba neteisinga grupė!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    result = random.choice([initiator_id, user_id])
    await context.bot.send_sticker(chat_id=chat_id, sticker=COINFLIP_STICKER_ID)
    if result == initiator_id:
        user_points[initiator_id] += amount
        user_points[user_id] -= amount
        msg = await update.message.reply_text(f"🪙 {initiator_username} laimėjo {amount} taškų prieš {opponent_username}!")
    else:
        user_points[user_id] += amount
        user_points[initiator_id] -= amount
        msg = await update.message.reply_text(f"🪙 {opponent_username} laimėjo {amount} taškų prieš {initiator_username}!")
    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    del coinflip_challenges[user_id]
    save_data(user_points, 'user_points.pkl')

async def expire_challenge(context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    job_context = context.job.data
    if not isinstance(job_context, tuple) or len(job_context) != 2:
        logger.error("Invalid job context for expire_challenge")
        return
    
    opponent_id, ctx = job_context
    if opponent_id in coinflip_challenges:
        _, amount, _, initiator_username, opponent_username, chat_id = coinflip_challenges[opponent_id]
        del coinflip_challenges[opponent_id]
        try:
            msg = await ctx.bot.send_message(chat_id, f"Iššūkis tarp {initiator_username} ir {opponent_username} už {amount} taškų pasibaigė!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        except telegram.error.TelegramError as e:
            logger.error(f"Failed to send expiration message: {str(e)}")

async def addpoints(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali pridėti taškus!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Input validation
    if len(context.args) < 2:
        msg = await update.message.reply_text("Naudok: /addpoints Amount @UserID")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Validate amount
    valid_amount, amount = validate_amount(context.args[0])
    if not valid_amount:
        msg = await update.message.reply_text("Netinkama suma! Naudok skaičių tarp -10000 ir 10000.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Validate target format
    target = context.args[1]
    if not target.startswith('@User'):
        msg = await update.message.reply_text("Naudok: /addpoints Amount @UserID")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        target_id = int(target.strip('@User'))
        if target_id <= 0:
            raise ValueError("Invalid user ID")
    except ValueError:
        msg = await update.message.reply_text("Netinkamas vartotojo ID!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        user_points[target_id] = user_points.get(target_id, 0) + amount
        # Prevent negative points
        if user_points[target_id] < 0:
            user_points[target_id] = 0
        
        msg = await update.message.reply_text(f"Pridėta {amount} taškų @User{target_id}! Dabar: {user_points[target_id]}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(user_points, 'user_points.pkl')
        logger.info(f"Admin {user_id} added {amount} points to user {target_id}")
    except Exception as e:
        logger.error(f"Error adding points: {str(e)}")
        msg = await update.message.reply_text("Klaida pridedant taškus!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def pridetitaskus(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        seller = context.args[0]
        if not seller.startswith('@'):
            seller = '@' + seller
        amount = int(context.args[1])
        if seller not in trusted_sellers:
            msg = await update.message.reply_text(f"{seller} nėra patikimų pardavėjų sąraše!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        votes_alltime[seller] += amount
        msg = await update.message.reply_text(f"Pridėta {amount} taškų {seller} visų laikų balsams. Dabar: {votes_alltime[seller]}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(votes_alltime, 'votes_alltime.pkl')
    except (IndexError, ValueError):
        msg = await update.message.reply_text("Naudok: /pridetitaskus @Seller Amount")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def points(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    logger.info(f"/points called by user_id={user_id} in chat_id={chat_id}")

    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        logger.warning(f"Chat_id={chat_id} not in allowed_groups={allowed_groups}")
        return

    points = user_points.get(user_id, 0)
    streak = chat_streaks.get(user_id, 0)
    msg = await update.message.reply_text(f"Jūsų taškai: {points}\nSerija: {streak} dienų")
    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    logger.info(f"Points for user_id={user_id}: {points}, Streak: {streak}")

# New admin commands for resetting votes
async def reset_weekly(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    await reset_weekly_data(context)
    msg = await update.message.reply_text("✅ Savaitės balsai iš naujo nukryžiuoti!")
    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def reset_monthly(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    await reset_monthly_data(context)
    msg = await update.message.reply_text("✅ Mėnesio balsai iš naujo nukryžiuoti!")
    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

# New admin commands to add points
async def add_weekly_points(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        seller = context.args[0]
        if not seller.startswith('@'):
            seller = '@' + seller
        amount = int(context.args[1])
        if seller not in trusted_sellers:
            msg = await update.message.reply_text(f"{seller} nėra patikimų pardavėjų sąraše!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        votes_weekly[seller] += amount
        msg = await update.message.reply_text(f"Pridėta {amount} taškų {seller} savaitės balsams. Dabar: {votes_weekly[seller]}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(votes_weekly, 'votes_weekly.pkl')
    except (IndexError, ValueError):
        msg = await update.message.reply_text("Naudok: /add_weekly_points @Seller Amount")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def add_monthly_points(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        seller = context.args[0]
        if not seller.startswith('@'):
            seller = '@' + seller
        amount = int(context.args[1])
        if seller not in trusted_sellers:
            msg = await update.message.reply_text(f"{seller} nėra patikimų pardavėjų sąraše!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        now = datetime.now(TIMEZONE)
        votes_monthly[seller].append((now, amount))
        msg = await update.message.reply_text(f"Pridėta {amount} taškų {seller} mėnesio balsams.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(votes_monthly, 'votes_monthly.pkl')
    except (IndexError, ValueError):
        msg = await update.message.reply_text("Naudok: /add_monthly_points @Seller Amount")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def add_alltime_points(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        seller = context.args[0]
        if not seller.startswith('@'):
            seller = '@' + seller
        amount = int(context.args[1])
        if seller not in trusted_sellers:
            msg = await update.message.reply_text(f"{seller} nėra patikimų pardavėjų sąraše!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        votes_alltime[seller] += amount
        msg = await update.message.reply_text(f"Pridėta {amount} taškų {seller} visų laikų balsams. Dabar: {votes_alltime[seller]}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(votes_alltime, 'votes_alltime.pkl')
    except (IndexError, ValueError):
        msg = await update.message.reply_text("Naudok: /add_alltime_points @Seller Amount")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

# New admin commands to remove points
async def remove_weekly_points(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        seller = context.args[0]
        if not seller.startswith('@'):
            seller = '@' + seller
        amount = int(context.args[1])
        if seller not in trusted_sellers:
            msg = await update.message.reply_text(f"{seller} nėra patikimų pardavėjų sąraše!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        votes_weekly[seller] -= amount
        msg = await update.message.reply_text(f"Pašalinta {amount} taškų iš {seller} savaitės balsų. Dabar: {votes_weekly[seller]}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(votes_weekly, 'votes_weekly.pkl')
    except (IndexError, ValueError):
        msg = await update.message.reply_text("Naudok: /remove_weekly_points @Seller Amount")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def remove_monthly_points(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        seller = context.args[0]
        if not seller.startswith('@'):
            seller = '@' + seller
        amount = int(context.args[1])
        if seller not in trusted_sellers:
            msg = await update.message.reply_text(f"{seller} nėra patikimų pardavėjų sąraše!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        now = datetime.now(TIMEZONE)
        votes_monthly[seller].append((now, -amount))  # Append a negative vote
        msg = await update.message.reply_text(f"Pašalinta {amount} taškų iš {seller} mėnesio balsų.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(votes_monthly, 'votes_monthly.pkl')
    except (IndexError, ValueError):
        msg = await update.message.reply_text("Naudok: /remove_monthly_points @Seller Amount")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def remove_alltime_points(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    try:
        seller = context.args[0]
        if not seller.startswith('@'):
            seller = '@' + seller
        amount = int(context.args[1])
        if seller not in trusted_sellers:
            msg = await update.message.reply_text(f"{seller} nėra patikimų pardavėjų sąraše!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        votes_alltime[seller] -= amount
        msg = await update.message.reply_text(f"Pašalinta {amount} taškų iš {seller} visų laikų balsų. Dabar: {votes_alltime[seller]}")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        save_data(votes_alltime, 'votes_alltime.pkl')
    except (IndexError, ValueError):
        msg = await update.message.reply_text("Naudok: /remove_alltime_points @Seller Amount")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))



async def help_command(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show comprehensive help information"""
    chat_id = update.message.chat_id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    help_text = """
🤖 **ShoeBot - Visų Komandų Sąrašas**

**📊 Pagrindinės Komandos:**
📊 /balsuoju - Balsuoti už pardavėjus balsavimo grupėje
👎 /nepatiko @pardavejas priežastis - Pateikti skundą (+5 tšk)
💰 /points - Patikrinti savo taškus ir pokalbių seriją
👑 /chatking - Visų laikų pokalbių lyderiai
📈 /barygos - Pardavėjų reitingai ir statistika

**🎮 Žaidimai ir Veikla:**
🎯 /coinflip suma @vartotojas - Iššūkis monetos metimui
📋 /apklausa klausimas - Sukurti grupės apklausą

**🏆 Pasiekimai ir Iššūkiai:**
🏅 /achievements - Tavo pasiekimai ir pažanga
🎯 /challenges - Savaitės iššūkiai ir užduotys
🏆 /leaderboard - Bendros lyderių lentos
📊 /mystats - Detalūs tavo statistikos

**⚙️ Informacija:**
/debug - Grupės administratoriai (tik admin)
/whoami - Tavo vartotojo informacija

**🎖️ Taškų Sistema:**
• Balsavimas už pardavėją: +5 taškų (1x per savaitę)
• Skundas pardavėjui: +5 taškų (1x per savaitę)  
• Kasdieniai pokalbiai: 1-3 taškų + serijos bonusas
• Serijos bonusas: +1 tšk už kiekvieną 3 dienų seriją
• Pasiekimai: 10-200 taškų už specialius veiksmus
• Savaitės iššūkiai: 60-100 taškų už užbaigimą

**🎉 Sezoniniai Renginiai:**
Specialūs renginiai su bonusiniais taškais:
• 🎄 Kalėdos (2x taškai)
• 🐰 Velykos (1.5x taškai)  
• ☀️ Vasara (1.3x taškai)

**🛡️ Automatinis Moderavimas:**
• Anti-spam apsauga
• Automatinis perspėjimų sistema
• Blokuojami spam šablonai
• Komandų laikmatis (cooldown)

**📅 Automatiniai Restartimai:**
• Savaitės balsai nukryžiuojami kas sekmadienį 23:00
• Mėnesio balsai nukryžiuojami kiekvieno mėnesio 1-ą dieną

**👨‍💼 Admin Komandos:**
/botstats - Bot analitikos ir statistikos
/moderation - Moderacijos pultas ir nustatymai
/reset_weekly - Rankiniu būdu nukryžiuoti savaitės balsus
/reset_monthly - Rankiniu būdu nukryžiuoti mėnesio balsus
"""
    
    msg = await update.message.reply_text(help_text)
    context.job_queue.run_once(delete_message_job, 90, data=(chat_id, msg.message_id))

async def achievements(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show user achievements and progress"""
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        # Log command usage
        analytics.log_command_usage('achievements', user_id, chat_id)
        
        user_achievements = achievement_system.get_user_achievements(user_id)
        total_achievements = len(achievement_system.achievements)
        
        if not user_achievements:
            msg = await update.message.reply_text(
                "🏆 Dar neturi pasiekimų!\n\n"
                "Balsuok, rašyk žinutes ir dalyvauk veikloje, kad gautum pasiekimus!"
            )
        else:
            achievement_text = "🏆 Tavo Pasiekimai 🏆\n\n"
            for achievement in user_achievements:
                achievement_text += f"{achievement['name']}\n"
                achievement_text += f"📝 {achievement['description']}\n"
                achievement_text += f"🎯 +{achievement['points']} taškų\n\n"
            
            achievement_text += f"📊 Progresą: {len(user_achievements)}/{total_achievements} pasiekimų"
            
            msg = await update.message.reply_text(achievement_text)
        
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))
    except Exception as e:
        logger.error(f"Error in achievements command: {str(e)}")
        analytics.log_command_usage('achievements', user_id, chat_id, False, str(e))

async def challenges(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show weekly challenges and progress"""
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        # Log command usage
        analytics.log_command_usage('challenges', user_id, chat_id)
        
        user_challenges = challenge_system.get_weekly_challenges(user_id)
        event_id, event = achievement_system.get_current_event()
        
        challenge_text = "🎯 Savaitės Iššūkiai 🎯\n\n"
        
        if event:
            challenge_text += f"🎉 {event['name']} - {event['bonus_multiplier']}x taškai!\n\n"
        
        for challenge_data in user_challenges:
            challenge = challenge_data['challenge']
            progress = challenge_data['progress']
            completed = challenge_data['completed']
            
            status = "✅" if completed else "⏳"
            challenge_text += f"{status} {challenge['name']}\n"
            challenge_text += f"📝 {challenge['description']}\n"
            challenge_text += f"📊 Progresą: {progress}/{challenge['target']}\n"
            challenge_text += f"🎁 Atlygis: {challenge['reward_points']} taškų\n\n"
        
        msg = await update.message.reply_text(challenge_text)
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))
    except Exception as e:
        logger.error(f"Error in challenges command: {str(e)}")
        analytics.log_command_usage('challenges', user_id, chat_id, False, str(e))

async def leaderboard(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show comprehensive leaderboards"""
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        # Log command usage
        analytics.log_command_usage('leaderboard', user_id, chat_id)
        
        # Create beautiful header
        now = datetime.now(TIMEZONE)
        header = "🏆✨ **BENDROS LYDERIŲ LENTOS** ✨🏆\n"
        header += f"📅 {now.strftime('%Y-%m-%d %H:%M')}\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # Points leaderboard
        sorted_points = sorted(user_points.items(), key=lambda x: x[1], reverse=True)[:10]
        max_points = sorted_points[0][1] if sorted_points else 1
        
        points_board = "💰 **TAŠKŲ MAGNATAI** 💰\n"
        points_board += "┌─────────────────────────────────────┐\n"
        
        if not sorted_points:
            points_board += "│       Dar nėra taškų lyderių       │\n"
        else:
            for i, (uid, points) in enumerate(sorted_points, 1):
                try:
                    username = next((k for k, v in username_to_id.items() if v == uid), f"User {uid}")
                    
                    # Create wealth icons based on points
                    if points >= 1000:
                        icon = "💎"
                    elif points >= 500:
                        icon = "🥇"
                    elif points >= 200:
                        icon = "🥈"
                    elif points >= 100:
                        icon = "🥉"
                    elif points >= 50:
                        icon = "⭐"
                    else:
                        icon = "🌟"
                    
                    # Create progress bar
                    progress = points / max(max_points, 1)
                    bar_length = 15
                    filled = int(progress * bar_length)
                    progress_bar = "█" * filled + "░" * (bar_length - filled)
                    
                    # Format username
                    display_name = username[1:] if username.startswith('@') else username
                    display_name = display_name[:12] if len(display_name) > 12 else display_name
                    
                    points_board += f"│{icon} {i:2d}. {display_name:<12} │{points:4d}│{progress_bar}│\n"
                except:
                    points_board += f"│💰 {i:2d}. User {uid}       │{points:4d}│{'█' * 8 + '░' * 7}│\n"
        
        points_board += "└─────────────────────────────────────┘\n\n"
        
        # Messages leaderboard
        sorted_messages = sorted(alltime_messages.items(), key=lambda x: x[1], reverse=True)[:10]
        max_messages = sorted_messages[0][1] if sorted_messages else 1
        
        messages_board = "💬 **POKALBIŲ ČEMPIONAI** 💬\n"
        messages_board += "┌─────────────────────────────────────┐\n"
        
        if not sorted_messages:
            messages_board += "│      Dar nėra pokalbių lyderių     │\n"
        else:
            for i, (uid, msg_count) in enumerate(sorted_messages, 1):
                try:
                    username = next((k for k, v in username_to_id.items() if v == uid), f"User {uid}")
                    
                    # Create chat activity icons
                    if msg_count >= 5000:
                        icon = "🔥"
                    elif msg_count >= 1000:
                        icon = "⚡"
                    elif msg_count >= 500:
                        icon = "💎"
                    elif msg_count >= 100:
                        icon = "🌟"
                    elif msg_count >= 50:
                        icon = "⭐"
                    else:
                        icon = "📈"
                    
                    # Create progress bar
                    progress = msg_count / max(max_messages, 1)
                    bar_length = 15
                    filled = int(progress * bar_length)
                    progress_bar = "█" * filled + "░" * (bar_length - filled)
                    
                    # Format username
                    display_name = username[1:] if username.startswith('@') else username
                    display_name = display_name[:12] if len(display_name) > 12 else display_name
                    
                    messages_board += f"│{icon} {i:2d}. {display_name:<12} │{msg_count:4d}│{progress_bar}│\n"
                except:
                    messages_board += f"│💬 {i:2d}. User {uid}       │{msg_count:4d}│{'█' * 8 + '░' * 7}│\n"
        
        messages_board += "└─────────────────────────────────────┘\n\n"
        
        # Achievement leaderboard
        achievement_counts = {}
        for user_id_ach, achievements in achievement_system.user_achievements.items():
            achievement_counts[user_id_ach] = len(achievements)
        
        sorted_achievements = sorted(achievement_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        max_achievements = sorted_achievements[0][1] if sorted_achievements else 1
        
        achievements_board = "🏅 **PASIEKIMŲ KOLEKCIONIERIAI** 🏅\n"
        achievements_board += "┌─────────────────────────────────────┐\n"
        
        if not sorted_achievements:
            achievements_board += "│    Dar nėra pasiekimų kolekcininkų  │\n"
        else:
            for i, (uid, ach_count) in enumerate(sorted_achievements, 1):
                try:
                    username = next((k for k, v in username_to_id.items() if v == uid), f"User {uid}")
                    
                    # Create achievement icons
                    if ach_count >= 8:
                        icon = "🏆"
                    elif ach_count >= 6:
                        icon = "🎖️"
                    elif ach_count >= 4:
                        icon = "🥇"
                    elif ach_count >= 2:
                        icon = "🏅"
                    else:
                        icon = "⭐"
                    
                    # Create progress bar
                    progress = ach_count / max(max_achievements, 1)
                    bar_length = 15
                    filled = int(progress * bar_length)
                    progress_bar = "█" * filled + "░" * (bar_length - filled)
                    
                    # Format username
                    display_name = username[1:] if username.startswith('@') else username
                    display_name = display_name[:12] if len(display_name) > 12 else display_name
                    
                    achievements_board += f"│{icon} {i:2d}. {display_name:<12} │{ach_count:4d}│{progress_bar}│\n"
                except:
                    achievements_board += f"│🏅 {i:2d}. User {uid}       │{ach_count:4d}│{'█' * 8 + '░' * 7}│\n"
        
        achievements_board += "└─────────────────────────────────────┘\n\n"
        
        # Community statistics
        stats = "📊 **BENDRUOMENĖS STATISTIKOS**\n"
        total_users = len(user_points)
        total_points = sum(user_points.values())
        total_messages = sum(alltime_messages.values())
        total_achievements = sum(len(ach) for ach in achievement_system.user_achievements.values())
        
        stats += f"• Narių: {total_users} 👥\n"
        stats += f"• Taškų: {total_points:,} 💰\n"
        stats += f"• Žinučių: {total_messages:,} 💬\n"
        stats += f"• Pasiekimų: {total_achievements} 🏆\n"
        stats += f"• Vidurkis taškų: {total_points // total_users if total_users else 0} 📈\n\n"
        
        # Tips and motivation
        footer = "🎯 **KAIP KILTI AUKŠTYN**\n"
        footer += "• Dalyvaukite kasdienių pokalbių (+1-3 tšk)\n"
        footer += "• Balsuokite už pardavėjus (+5 tšk/sav)\n"
        footer += "• Pildykite savaitės iššūkius (+60-100 tšk)\n"
        footer += "• Gaukite pasiekimus (+10-200 tšk)\n"
        footer += "• Palaikykite pokalbių seijas (bonusai)\n\n"
        footer += "🚀 Dalyvaukite aktyviai ir tapkite lyderiais!"
        
        full_message = header + points_board + messages_board + achievements_board + stats + footer
        
        try:
            msg = await update.message.reply_text(full_message, parse_mode='Markdown')
            context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))
        except telegram.error.TelegramError as e:
            # Fallback without markdown
            logger.error(f"Error sending formatted leaderboard: {str(e)}")
            fallback_message = full_message.replace('**', '').replace('*', '')
            msg = await update.message.reply_text(fallback_message)
            context.job_queue.run_once(delete_message_job, 90, data=(chat_id, msg.message_id))
        
    except Exception as e:
        logger.error(f"Error in leaderboard command: {str(e)}")
        analytics.log_command_usage('leaderboard', user_id, chat_id, False, str(e))

async def mystats(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed user statistics"""
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        # Log command usage
        analytics.log_command_usage('mystats', user_id, chat_id)
        
        points = user_points.get(user_id, 0)
        streak = chat_streaks.get(user_id, 0)
        messages = alltime_messages.get(user_id, 0)
        achievements = len(achievement_system.get_user_achievements(user_id))
        
        # Calculate user rank
        sorted_points = sorted(user_points.items(), key=lambda x: x[1], reverse=True)
        user_rank = next((i for i, (uid, _) in enumerate(sorted_points, 1) if uid == user_id), "N/A")
        
        stats_text = f"📊 Tavo Statistikos 📊\n\n"
        stats_text += f"💰 Taškai: {points}\n"
        stats_text += f"🔥 Serija: {streak} dienų\n"
        stats_text += f"💬 Žinutės: {messages}\n"
        stats_text += f"🏆 Pasiekimai: {achievements}\n"
        stats_text += f"📈 Ranka: #{user_rank}\n\n"
        
        # Add weekly stats
        today = datetime.now(TIMEZONE).date()
        week_start = today - timedelta(days=today.weekday())
        weekly_msgs = sum(daily_messages[user_id].get(week_start + timedelta(days=i), 0) for i in range(7))
        stats_text += f"📅 Šios savaitės žinutės: {weekly_msgs}\n"
        
        # Add voting stats
        total_votes = sum(1 for vendor_votes in vote_history.values() 
                         for vote in vendor_votes if vote[0] == user_id and vote[1] == "up")
        total_complaints = sum(1 for vendor_votes in vote_history.values() 
                              for vote in vendor_votes if vote[0] == user_id and vote[1] == "down")
        
        stats_text += f"🗳️ Balsai: {total_votes}\n"
        stats_text += f"👎 Skundai: {total_complaints}\n"
        
        msg = await update.message.reply_text(stats_text)
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))
    except Exception as e:
        logger.error(f"Error in mystats command: {str(e)}")
        analytics.log_command_usage('mystats', user_id, chat_id, False, str(e))

async def botstats(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot analytics and statistics (admin only)"""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        # Log command usage
        analytics.log_command_usage('botstats', user_id, chat_id)
        
        usage_stats = analytics.get_usage_stats(7)
        
        stats_text = "📈 Bot Statistikos (7 dienos) 📈\n\n"
        
        if usage_stats:
            stats_text += "🔥 Populiariausios komandos:\n"
            for command, count, success_rate in usage_stats[:10]:
                stats_text += f"/{command}: {count}x ({success_rate:.1%} sėkmė)\n"
        
        stats_text += f"\n👥 Viso vartotojų: {len(user_points)}\n"
        stats_text += f"💬 Viso žinučių: {sum(alltime_messages.values())}\n"
        stats_text += f"🗳️ Viso balsų: {sum(votes_alltime.values())}\n"
        stats_text += f"📊 Viso apklausų: {len(polls)}\n"
        stats_text += f"🏆 Viso pasiekimų: {sum(len(achievements) for achievements in achievement_system.user_achievements.values())}\n"
        
        # System health
        stats_text += f"\n🖥️ Sistemos būklė:\n"
        stats_text += f"Leistinos grupės: {len(allowed_groups)}\n"
        stats_text += f"Patikimi pardavėjai: {len(trusted_sellers)}\n"
        stats_text += f"Laukiantys skundai: {len(pending_downvotes)}\n"
        
        msg = await update.message.reply_text(stats_text)
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))
    except Exception as e:
        logger.error(f"Error in botstats command: {str(e)}")
        analytics.log_command_usage('botstats', user_id, chat_id, False, str(e))

async def moderation_command(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Moderation panel for admins"""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali naudoti šią komandą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        # Show moderation options
        keyboard = [
            [InlineKeyboardButton("Perspėjimų sąrašas", callback_data="mod_warnings")],
            [InlineKeyboardButton("Patikimi vartotojai", callback_data="mod_trusted")],
            [InlineKeyboardButton("Uždrausti žodžiai", callback_data="mod_banned_words")],
            [InlineKeyboardButton("Moderacijos logai", callback_data="mod_logs")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = await update.message.reply_text(
            "🛡️ Moderacijos Pultas 🛡️\n\nPasirink veiksmą:",
            reply_markup=reply_markup
        )
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))
    except Exception as e:
        logger.error(f"Error in moderation command: {str(e)}")

# Add handlers
application.add_handler(CommandHandler(['startas'], startas))
application.add_handler(CommandHandler(['activate_group'], activate_group))
application.add_handler(CommandHandler(['nepatiko'], nepatiko))
application.add_handler(CommandHandler(['approve'], approve))
application.add_handler(CommandHandler(['addseller'], addseller))
application.add_handler(CommandHandler(['removeseller'], removeseller))
application.add_handler(CommandHandler(['pardavejoinfo'], sellerinfo))
application.add_handler(CommandHandler(['barygos'], barygos))
application.add_handler(CommandHandler(['balsuoju'], balsuoju))
application.add_handler(CommandHandler(['chatking'], chatking))
application.add_handler(CommandHandler(['coinflip'], coinflip))
application.add_handler(CommandHandler(['accept_coinflip'], accept_coinflip))
application.add_handler(CommandHandler(['addpoints'], addpoints))
application.add_handler(CommandHandler(['pridetitaskus'], pridetitaskus))
application.add_handler(CommandHandler(['points'], points))
application.add_handler(CommandHandler(['debug'], debug))
application.add_handler(CommandHandler(['whoami'], whoami))
application.add_handler(CommandHandler(['addftbaryga'], addftbaryga))
application.add_handler(CommandHandler(['addftbaryga2'], addftbaryga2))
application.add_handler(CommandHandler(['editpardavejai'], editpardavejai))
application.add_handler(CommandHandler(['apklausa'], apklausa))
application.add_handler(CommandHandler(['updatevoting'], updatevoting))
application.add_handler(CommandHandler(['privatus'], privatus))
application.add_handler(CommandHandler(['reset_weekly'], reset_weekly))
application.add_handler(CommandHandler(['reset_monthly'], reset_monthly))
application.add_handler(CommandHandler(['add_weekly_points'], add_weekly_points))
application.add_handler(CommandHandler(['add_monthly_points'], add_monthly_points))
application.add_handler(CommandHandler(['add_alltime_points'], add_alltime_points))
application.add_handler(CommandHandler(['remove_weekly_points'], remove_weekly_points))
application.add_handler(CommandHandler(['remove_monthly_points'], remove_monthly_points))
application.add_handler(CommandHandler(['remove_alltime_points'], remove_alltime_points))
application.add_handler(CommandHandler(['help'], help_command))
application.add_handler(CommandHandler(['achievements'], achievements))
application.add_handler(CommandHandler(['challenges'], challenges))
application.add_handler(CommandHandler(['leaderboard'], leaderboard))
application.add_handler(CommandHandler(['mystats'], mystats))
application.add_handler(CommandHandler(['botstats'], botstats))
application.add_handler(CommandHandler(['moderation'], moderation_command))
application.add_handler(MessageHandler(filters.Regex('^/start$') & filters.ChatType.PRIVATE, start_private))
application.add_handler(CallbackQueryHandler(handle_vote_button, pattern="vote_"))
application.add_handler(CallbackQueryHandler(handle_poll_button, pattern="poll_"))
application.add_handler(CallbackQueryHandler(handle_admin_button, pattern="admin_"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Schedule jobs
application.job_queue.run_daily(award_daily_points, time=time(hour=0, minute=0))
application.job_queue.run_repeating(cleanup_old_polls, interval=3600, first=10)  # Cleanup polls every hour
application.job_queue.run_repeating(cleanup_expired_challenges, interval=300, first=30)  # Cleanup challenges every 5 minutes

# Weekly recap and reset - Every Sunday at 23:00
application.job_queue.scheduler.add_job(
    lambda: asyncio.create_task(weekly_recap(application)), 
    CronTrigger(day_of_week='sun', hour=23, minute=0, timezone=TIMEZONE), 
    id='weekly_recap'
)

# Monthly recap and reset - First day of each month at 00:30
application.job_queue.scheduler.add_job(
    lambda: asyncio.create_task(monthly_recap_and_reset(application)), 
    CronTrigger(day=1, hour=0, minute=30, timezone=TIMEZONE), 
    id='monthly_recap'
)

# Achievement and Badge System
class AchievementSystem:
    def __init__(self):
        self.achievements = {
            'first_vote': {'name': '🗳️ Pirmasis Balsas', 'description': 'Pirmą kartą balsavai', 'points': 10},
            'voter_streak_7': {'name': '🔥 Balsavimo Entuziastas', 'description': '7 dienas iš eilės balsavai', 'points': 50},
            'chat_master_100': {'name': '💬 Pokalbių Meistras', 'description': '100 žinučių parašyta', 'points': 25},
            'chat_king_1000': {'name': '👑 Pokalbių Karalius', 'description': '1000 žinučių parašyta', 'points': 100},
            'coinflip_winner_5': {'name': '🪙 Monetos Valdovas', 'description': '5 coinflip laimėjimai', 'points': 30},
            'complaint_investigator': {'name': '🕵️ Tyrėjas', 'description': 'Pateikė 10 skundų', 'points': 75},
            'early_bird': {'name': '🌅 Ankstyvas Paukštis', 'description': 'Rašo iki 6 ryto', 'points': 20},
            'night_owl': {'name': '🦉 Nakties Pelėda', 'description': 'Rašo po 22 vakaro', 'points': 20},
            'weekend_warrior': {'name': '⚔️ Savaitgalio Karys', 'description': 'Aktyvus savaitgaliais', 'points': 15},
            'monthly_champion': {'name': '🏆 Mėnesio Čempionas', 'description': '#1 mėnesio pokalbių lyderis', 'points': 200},
        }
        
        self.seasonal_events = {
            'christmas': {
                'name': '🎄 Kalėdų Šventė',
                'start_date': '12-20',
                'end_date': '01-07',
                'bonus_multiplier': 2.0,
                'special_achievements': ['santa_helper', 'gift_giver']
            },
            'easter': {
                'name': '🐰 Velykos',
                'start_date': '03-20',
                'end_date': '04-20',
                'bonus_multiplier': 1.5,
                'special_achievements': ['egg_hunter']
            },
            'summer': {
                'name': '☀️ Vasaros Šventė',
                'start_date': '06-20',
                'end_date': '08-31',
                'bonus_multiplier': 1.3,
                'special_achievements': ['summer_vibes']
            }
        }
        
        self.load_user_achievements()
    
    def load_user_achievements(self):
        """Load user achievements from file"""
        self.user_achievements = load_data('user_achievements.pkl', defaultdict(set))
        self.user_progress = load_data('user_progress.pkl', defaultdict(dict))
    
    def save_achievements(self):
        """Save achievements to file"""
        save_data(dict(self.user_achievements), 'user_achievements.pkl')
        save_data(dict(self.user_progress), 'user_progress.pkl')
    
    def check_achievement(self, user_id, achievement_id, current_value=None):
        """Check if user earned an achievement"""
        if achievement_id in self.user_achievements[user_id]:
            return False  # Already has this achievement
        
        earned = False
        
        # Check specific achievement conditions
        if achievement_id == 'first_vote' and current_value == 1:
            earned = True
        elif achievement_id == 'chat_master_100' and current_value >= 100:
            earned = True
        elif achievement_id == 'chat_king_1000' and current_value >= 1000:
            earned = True
        elif achievement_id == 'early_bird':
            current_hour = datetime.now(TIMEZONE).hour
            if current_hour < 6:
                earned = True
        elif achievement_id == 'night_owl':
            current_hour = datetime.now(TIMEZONE).hour
            if current_hour >= 22:
                earned = True
        elif achievement_id == 'weekend_warrior':
            if datetime.now(TIMEZONE).weekday() >= 5:  # Saturday or Sunday
                earned = True
        
        if earned:
            self.user_achievements[user_id].add(achievement_id)
            achievement = self.achievements[achievement_id]
            user_points[user_id] = user_points.get(user_id, 0) + achievement['points']
            self.save_achievements()
            save_data(user_points, 'user_points.pkl')
            return achievement
        
        return False
    
    def get_user_achievements(self, user_id):
        """Get all achievements for a user"""
        user_achievement_ids = self.user_achievements.get(user_id, set())
        return [self.achievements[aid] for aid in user_achievement_ids if aid in self.achievements]
    
    def get_current_event(self):
        """Get currently active seasonal event"""
        now = datetime.now(TIMEZONE)
        current_date = now.strftime('%m-%d')
        
        for event_id, event in self.seasonal_events.items():
            start_date = event['start_date']
            end_date = event['end_date']
            
            # Handle year wrap-around (e.g., Christmas)
            if start_date > end_date:
                if current_date >= start_date or current_date <= end_date:
                    return event_id, event
            else:
                if start_date <= current_date <= end_date:
                    return event_id, event
        
        return None, None

# Weekly Challenge System
class WeeklyChallengeSystem:
    def __init__(self):
        self.challenges = [
            {
                'id': 'message_master',
                'name': '💬 Žinučių Meistras',
                'description': 'Parašyk 50 žinučių per savaitę',
                'target': 50,
                'reward_points': 100,
                'type': 'messages'
            },
            {
                'id': 'voting_champion',
                'name': '🗳️ Balsavimo Čempionas',
                'description': 'Balsuok už 3 skirtingus pardavėjus',
                'target': 3,
                'reward_points': 75,
                'type': 'unique_votes'
            },
            {
                'id': 'poll_creator',
                'name': '📊 Apklausų Kūrėjas',
                'description': 'Sukurk 3 apklausas',
                'target': 3,
                'reward_points': 60,
                'type': 'polls_created'
            },
            {
                'id': 'social_butterfly',
                'name': '🦋 Socialus Drugelis',
                'description': 'Pokalbiauk 5 skirtingas dienas',
                'target': 5,
                'reward_points': 80,
                'type': 'active_days'
            }
        ]
        
        self.load_weekly_progress()
    
    def load_weekly_progress(self):
        """Load weekly challenge progress"""
        self.weekly_progress = load_data('weekly_progress.pkl', defaultdict(dict))
    
    def save_weekly_progress(self):
        """Save weekly challenge progress"""
        save_data(dict(self.weekly_progress), 'weekly_progress.pkl')
    
    def update_progress(self, user_id, challenge_type, amount=1):
        """Update user progress for challenges"""
        week_key = datetime.now(TIMEZONE).strftime('%Y-W%U')
        
        if week_key not in self.weekly_progress[user_id]:
            self.weekly_progress[user_id][week_key] = defaultdict(int)
        
        self.weekly_progress[user_id][week_key][challenge_type] += amount
        self.save_weekly_progress()
        
        # Check for completed challenges
        completed = []
        for challenge in self.challenges:
            if challenge['type'] == challenge_type:
                current_progress = self.weekly_progress[user_id][week_key][challenge_type]
                if current_progress >= challenge['target']:
                    completed_key = f"{week_key}_{challenge['id']}"
                    if completed_key not in self.weekly_progress[user_id].get('completed', set()):
                        if 'completed' not in self.weekly_progress[user_id]:
                            self.weekly_progress[user_id]['completed'] = set()
                        self.weekly_progress[user_id]['completed'].add(completed_key)
                        completed.append(challenge)
                        
                        # Award points
                        user_points[user_id] = user_points.get(user_id, 0) + challenge['reward_points']
                        save_data(user_points, 'user_points.pkl')
        
        return completed
    
    def get_weekly_challenges(self, user_id):
        """Get current week's challenges and progress"""
        week_key = datetime.now(TIMEZONE).strftime('%Y-W%U')
        user_progress = self.weekly_progress[user_id].get(week_key, defaultdict(int))
        completed_challenges = self.weekly_progress[user_id].get('completed', set())
        
        result = []
        for challenge in self.challenges:
            completed_key = f"{week_key}_{challenge['id']}"
            current_progress = user_progress[challenge['type']]
            
            result.append({
                'challenge': challenge,
                'progress': current_progress,
                'completed': completed_key in completed_challenges
            })
        
        return result

# Initialize systems
achievement_system = AchievementSystem()
challenge_system = WeeklyChallengeSystem()

# Advanced Moderation System
class ModerationSystem:
    def __init__(self):
        self.load_moderation_data()
        self.spam_patterns = [
            r'(https?://\S+)',  # URLs
            r'(@[a-zA-Z0-9_]{5,})',  # Potential spam usernames
            r'(\b\d{10,}\b)',  # Long numbers (phone numbers)
            r'(telegram\.me|t\.me)',  # Telegram links
            r'(\b[A-Z]{5,}\b)',  # Excessive caps
        ]
        
        self.warning_thresholds = {
            'spam': 3,
            'caps': 2,
            'flood': 5,
            'links': 2
        }
        
        self.auto_actions = {
            'warn': 'warning',
            'mute': 'temporary_restriction',
            'ban': 'permanent_restriction'
        }
    
    def load_moderation_data(self):
        """Load moderation data"""
        self.user_warnings = load_data('user_warnings.pkl', defaultdict(list))
        self.banned_words = load_data('banned_words.pkl', set())
        self.trusted_users = load_data('trusted_users.pkl', set())
        self.moderation_logs = load_data('moderation_logs.pkl', [])
    
    def save_moderation_data(self):
        """Save moderation data"""
        save_data(dict(self.user_warnings), 'user_warnings.pkl')
        save_data(self.banned_words, 'banned_words.pkl')
        save_data(self.trusted_users, 'trusted_users.pkl')
        save_data(self.moderation_logs, 'moderation_logs.pkl')
    
    def check_spam(self, user_id, message_text, chat_id):
        """Check if message is spam"""
        if user_id in self.trusted_users:
            return False, None
        
        issues = []
        
        # Check for banned words
        message_lower = message_text.lower()
        for word in self.banned_words:
            if word.lower() in message_lower:
                issues.append(('banned_word', f"Banned word: {word}"))
        
        # Check spam patterns
        for pattern in self.spam_patterns:
            if re.search(pattern, message_text, re.IGNORECASE):
                pattern_name = {
                    r'(https?://\S+)': 'links',
                    r'(@[a-zA-Z0-9_]{5,})': 'mentions',
                    r'(\b\d{10,}\b)': 'phone_numbers',
                    r'(telegram\.me|t\.me)': 'telegram_links',
                    r'(\b[A-Z]{5,}\b)': 'caps'
                }.get(pattern, 'unknown')
                issues.append((pattern_name, f"Spam pattern: {pattern_name}"))
        
        # Check message length
        if len(message_text) > 1000:
            issues.append(('long_message', "Message too long"))
        
        # Check for excessive emoji
        emoji_count = len(re.findall(r'[^\w\s]', message_text))
        if emoji_count > 20:
            issues.append(('emoji_spam', f"Too many emojis: {emoji_count}"))
        
        return len(issues) > 0, issues
    
    def add_warning(self, user_id, chat_id, reason, moderator_id=None):
        """Add warning to user"""
        warning = {
            'timestamp': datetime.now(TIMEZONE),
            'reason': reason,
            'chat_id': chat_id,
            'moderator_id': moderator_id
        }
        
        self.user_warnings[user_id].append(warning)
        self.moderation_logs.append({
            'action': 'warning',
            'user_id': user_id,
            'chat_id': chat_id,
            'reason': reason,
            'moderator_id': moderator_id,
            'timestamp': datetime.now(TIMEZONE)
        })
        
        self.save_moderation_data()
        
        # Check if action needed
        recent_warnings = [w for w in self.user_warnings[user_id] 
                          if datetime.now(TIMEZONE) - w['timestamp'] < timedelta(days=7)]
        
        warning_count = len(recent_warnings)
        if warning_count >= 3:
            return 'mute'
        elif warning_count >= 5:
            return 'ban'
        
        return 'warn'
    
    def get_user_warnings(self, user_id, days=30):
        """Get user warnings in last N days"""
        cutoff = datetime.now(TIMEZONE) - timedelta(days=days)
        return [w for w in self.user_warnings.get(user_id, []) 
                if w['timestamp'] > cutoff]
    
    def is_flooding(self, user_id, chat_id, time_window=60, message_limit=10):
        """Check if user is flooding (too many messages)"""
        # This would need to track recent messages per user
        # For now, return False - implement based on analytics data
        return False

# Rate Limiting System
class RateLimiter:
    def __init__(self):
        self.command_cooldowns = defaultdict(dict)
        self.global_cooldowns = {
            'coinflip': 30,  # 30 seconds between coinflips
            'apklausa': 60,  # 1 minute between polls
            'nepatiko': 300,  # 5 minutes between complaints
            'balsuoju': 10,  # 10 seconds between votes
        }
    
    def check_cooldown(self, user_id, command):
        """Check if user is on cooldown for command"""
        if command not in self.global_cooldowns:
            return True, 0
        
        now = datetime.now(TIMEZONE)
        cooldown_time = self.global_cooldowns[command]
        
        if user_id in self.command_cooldowns and command in self.command_cooldowns[user_id]:
            last_use = self.command_cooldowns[user_id][command]
            time_since = (now - last_use).total_seconds()
            
            if time_since < cooldown_time:
                remaining = cooldown_time - time_since
                return False, remaining
        
        # Update last use time
        if user_id not in self.command_cooldowns:
            self.command_cooldowns[user_id] = {}
        self.command_cooldowns[user_id][command] = now
        
        return True, 0
    
    def format_cooldown_message(self, remaining_seconds):
        """Format cooldown message"""
        if remaining_seconds < 60:
            return f"Palauk {int(remaining_seconds)} sekundžių"
        else:
            minutes = int(remaining_seconds // 60)
            return f"Palauk {minutes} minučių"

# Initialize moderation systems
moderation_system = ModerationSystem()
rate_limiter = RateLimiter()

if __name__ == '__main__':
    try:
        logger.info("Starting bot polling...")
        application.run_polling()
    except Exception as e:
        logger.error(f"Polling failed: {str(e)}")
    logger.info("Bot polling stopped.")
