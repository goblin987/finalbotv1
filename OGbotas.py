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
    # Add longer timeout for large messages
    if len(text) > 1000:
        kwargs.setdefault('read_timeout', 30)
        kwargs.setdefault('write_timeout', 30)
        kwargs.setdefault('connect_timeout', 20)
    else:
        kwargs.setdefault('read_timeout', 20)
        kwargs.setdefault('write_timeout', 20)
        kwargs.setdefault('connect_timeout', 10)
    
    for attempt in range(retries):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except telegram.error.TimedOut:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
                continue
            logger.error(f"Message send timeout after {retries} attempts to chat {chat_id}")
            # Try one more time with shorter message if original was long
            if len(text) > 1000:
                short_text = text[:800] + "\n\n... (Žinutė sutrumpinta dėl klaidų)"
                try:
                    return await bot.send_message(chat_id=chat_id, text=short_text)
                except:
                    pass
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
            if attempt < retries - 1:
                await asyncio.sleep(1)
                continue
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

# Bot initialization with improved timeout settings
application = Application.builder().token(TOKEN).post_init(configure_scheduler).read_timeout(30).write_timeout(30).connect_timeout(20).build()
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

# Scammer tracking system
pending_scammer_reports = load_data('pending_scammer_reports.pkl', {})  # report_id: {username, reporter_id, proof, timestamp, chat_id}
confirmed_scammers = load_data('confirmed_scammers.pkl', {})  # username: {confirmed_by, reporter_id, proof, timestamp, reports_count}
scammer_report_id = load_data('scammer_report_id.pkl', 0)

def is_allowed_group(chat_id: str) -> bool:
    return str(chat_id) in allowed_groups

# Message deletion function
async def delete_message_job(context: telegram.ext.CallbackContext):
    """Delete message job with proper error handling"""
    job = context.job
    if not job or not job.data:
        logger.warning("Delete message job called without proper data")
        return
    
    try:
        chat_id, message_id = job.data
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.debug(f"Successfully deleted message {message_id} from chat {chat_id}")
    except telegram.error.BadRequest as e:
        if "Message to delete not found" in str(e) or "Message can't be deleted" in str(e):
            logger.debug(f"Message {message_id} already deleted or cannot be deleted")
        else:
            logger.warning(f"Failed to delete message {message_id}: {str(e)}")
    except telegram.error.TelegramError as e:
        logger.warning(f"Telegram error deleting message {message_id}: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error deleting message {message_id}: {str(e)}")
    
    # Ensure job completion
    try:
        if hasattr(context.job_queue, 'scheduler') and context.job_queue.scheduler:
            context.job_queue.scheduler.remove_job(job.id)
    except:
        pass  # Job might already be removed

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
                "📊 /balsuoti - Balsuoti už pardavėjus balsavimo grupėje\n"
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
                            [InlineKeyboardButton("Redaguoti /balsuoti tekstą", callback_data="admin_editpardavejai")]
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

async def balsuoti(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
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
        last_addftbaryga_message = "Paveikslėlis pridėtas prie /balsuoti!"
    elif reply.animation:
        media = reply.animation
        featured_media_id = media.file_id
        featured_media_type = 'animation'
        last_addftbaryga_message = "GIF pridėtas prie /balsuoti!"
    elif reply.video:
        media = reply.video
        featured_media_id = media.file_id
        featured_media_type = 'video'
        last_addftbaryga_message = "Video pridėtas prie /balsuoti!"
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
        msg = await update.message.reply_text(f"Pardavėjas {vendor} pridėtas! Jis dabar matomas /balsuoti sąraše.")
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
        # Use safe_send_message for better reliability
        try:
            # Check message length - if too long for caption, send as separate text message
            if len(full_message) > 1000 and barygos_media_id and barygos_media_type:
                # Send media without caption first
                if barygos_media_type == 'photo':
                    await context.bot.send_photo(
                        chat_id=chat_id, 
                        photo=barygos_media_id,
                        read_timeout=30,
                        write_timeout=30
                    )
                elif barygos_media_type == 'animation':
                    await context.bot.send_animation(
                        chat_id=chat_id, 
                        animation=barygos_media_id,
                        read_timeout=30,
                        write_timeout=30
                    )
                elif barygos_media_type == 'video':
                    await context.bot.send_video(
                        chat_id=chat_id, 
                        video=barygos_media_id,
                        read_timeout=30,
                        write_timeout=30
                    )
                
                # Then send full message as text using safe_send_message
                msg = await safe_send_message(
                    context.bot,
                    chat_id,
                    full_message,
                    parse_mode='Markdown'
                )
            elif barygos_media_id and barygos_media_type:
                # Message is short enough for caption
                if barygos_media_type == 'photo':
                    msg = await context.bot.send_photo(
                        chat_id=chat_id, 
                        photo=barygos_media_id, 
                        caption=full_message,
                        parse_mode='Markdown',
                        read_timeout=30,
                        write_timeout=30
                    )
                elif barygos_media_type == 'animation':
                    msg = await context.bot.send_animation(
                        chat_id=chat_id, 
                        animation=barygos_media_id, 
                        caption=full_message,
                        parse_mode='Markdown',
                        read_timeout=30,
                        write_timeout=30
                    )
                elif barygos_media_type == 'video':
                    msg = await context.bot.send_video(
                        chat_id=chat_id, 
                        video=barygos_media_id, 
                        caption=full_message,
                        parse_mode='Markdown',
                        read_timeout=30,
                        write_timeout=30
                    )
                else:
                    msg = await safe_send_message(
                        context.bot,
                        chat_id,
                        full_message,
                        parse_mode='Markdown'
                    )
            else:
                # No media, send as text using safe_send_message
                msg = await safe_send_message(
                    context.bot,
                    chat_id,
                    full_message,
                    parse_mode='Markdown'
                )
            
            context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))  # Keep longer for reading
        
        except telegram.error.TelegramError as e:
            # Fallback without markdown if formatting fails
            logger.error(f"Error sending formatted barygos message: {str(e)}")
            try:
                fallback_message = full_message.replace('**', '').replace('*', '')
                msg = await safe_send_message(context.bot, chat_id, fallback_message)
                context.job_queue.run_once(delete_message_job, 90, data=(chat_id, msg.message_id))
            except Exception as fallback_error:
                logger.error(f"Fallback message also failed: {str(fallback_error)}")
                try:
                    msg = await context.bot.send_message(chat_id=chat_id, text="❌ Klaida gaunant pardavėjų reitingus!")
                    context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
                except:
                    logger.error(f"Even error message failed for chat {chat_id}")
                    
    except Exception as e:
        logger.error(f"Critical error in barygos command: {str(e)}")
        try:
            msg = await context.bot.send_message(chat_id=chat_id, text="❌ Sistemos klaida!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        except:
            logger.error(f"Failed to send system error message to chat {chat_id}")

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

# Scammer tracking system commands
async def scameris(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Report a scammer with proof"""
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check daily report limit (5 reports per day)
    now = datetime.now(TIMEZONE)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Count reports made today
    reports_today = sum(1 for report in pending_scammer_reports.values() 
                       if report['reporter_id'] == user_id and report['timestamp'] >= today_start)
    
    # Also count approved reports from today
    approved_today = sum(1 for scammer_info in confirmed_scammers.values() 
                        if scammer_info.get('reporter_id') == user_id and 
                        scammer_info['timestamp'] >= today_start)
    
    total_reports_today = reports_today + approved_today
    
    if total_reports_today >= 5:
        msg = await update.message.reply_text(f"Pasiekėte dienų limitą! Galite pateikti iki 5 pranešimų per dieną. Šiandien: {total_reports_today}/5")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Input validation
    if len(context.args) < 2:
        msg = await update.message.reply_text(
            "📋 **Naudojimas:** `/scameris @username įrodymai`\n\n"
            "**Pavyzdys:** `/scameris @scammer123 Nepavede prekės, ignoruoja žinutes`\n"
            "**Reikia:** Detalūs įrodymai kodėl šis žmogus yra scameris"
        )
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))
        return
    
    # Sanitize and validate inputs
    reported_username = sanitize_username(context.args[0])
    if not reported_username or len(reported_username) < 2:
        msg = await update.message.reply_text("❌ Netinkamas vartotojo vardas! Naudok @username formatą.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    proof = sanitize_text_input(" ".join(context.args[1:]), max_length=500)
    if not proof or len(proof.strip()) < 10:
        msg = await update.message.reply_text("❌ Prašau nurodyti detalius įrodymus (bent 10 simbolių)!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check if already confirmed scammer
    if reported_username.lower() in confirmed_scammers:
        msg = await update.message.reply_text(f"⚠️ {reported_username} jau yra patvirtintų scamerių sąraše!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check if user is trying to report themselves
    reporter_username = f"@{update.message.from_user.username}" if update.message.from_user.username else None
    if reporter_username and reported_username.lower() == reporter_username.lower():
        msg = await update.message.reply_text("❌ Negalite pranešti apie save!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        global scammer_report_id
        scammer_report_id += 1
        
        # Store the report
        pending_scammer_reports[scammer_report_id] = {
            'username': reported_username,
            'reporter_id': user_id,
            'reporter_username': reporter_username or f"User {user_id}",
            'proof': proof,
            'timestamp': now,
            'chat_id': chat_id
        }
        
        # Track that user made a report today (for daily limit counting)
        
        # Send to admin for review
        admin_message = (
            f"🚨 **NAUJAS SCAMER PRANEŠIMAS** 🚨\n\n"
            f"**Report ID:** #{scammer_report_id}\n"
            f"**Pranešė:** {reporter_username or f'User {user_id}'}\n"
            f"**Apie:** {reported_username}\n"
            f"**Įrodymai:** {proof}\n"
            f"**Laikas:** {now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"**Veiksmai:**\n"
            f"✅ `/approve_scammer {scammer_report_id}` - Patvirtinti\n"
            f"❌ `/reject_scammer {scammer_report_id}` - Atmesti\n"
            f"📋 `/scammer_details {scammer_report_id}` - Detalės"
        )
        
        await safe_send_message(context.bot, ADMIN_CHAT_ID, admin_message, parse_mode='Markdown')
        
        # Confirm to user
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ **Pranešimas pateiktas!**\n\n"
                     f"**Report ID:** #{scammer_report_id}\n"
                     f"**Apie:** {reported_username}\n"
                     f"**Statusas:** Laukia admin peržiūros\n\n"
                     f"Adminai peržiūrės jūsų pranešimą ir priims sprendimą. Ačiū už saugios bendruomenės kūrimą! 🛡️",
                parse_mode='Markdown'
            )
        except telegram.error.TelegramError as e:
            # Fallback without markdown if that fails
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Pranešimas pateiktas!\n\n"
                     f"Report ID: #{scammer_report_id}\n"
                     f"Apie: {reported_username}\n"
                     f"Statusas: Laukia admin peržiūros\n\n"
                     f"Adminai peržiūrės jūsų pranešimą ir priims sprendimą. Ačiū už saugios bendruomenės kūrimą!",
                parse_mode='Markdown'
            )
        context.job_queue.run_once(delete_message_job, 90, data=(chat_id, msg.message_id))
        
        # Save data
        save_data(pending_scammer_reports, 'pending_scammer_reports.pkl')
        save_data(scammer_report_id, 'scammer_report_id.pkl')
        
        # Add points for reporting
        user_points[user_id] = user_points.get(user_id, 0) + 3
        save_data(user_points, 'user_points.pkl')
        
        logger.info(f"Scammer report #{scammer_report_id}: {reported_username} reported by user {user_id}")
        
    except Exception as e:
        logger.error(f"Error processing scammer report: {str(e)}")
        try:
            msg = await context.bot.send_message(chat_id=chat_id, text="❌ Klaida pateikiant pranešimą. Bandykite vėliau.")
        except:
            # If even basic sending fails, log it and continue
            logger.error(f"Failed to send error message to chat {chat_id}")
            return
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def patikra(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Check if a user is in the scammer list"""
    chat_id = update.message.chat_id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if len(context.args) < 1:
        msg = await update.message.reply_text(
            "📋 **Naudojimas:** `/patikra @username`\n\n"
            "**Pavyzdys:** `/patikra @user123`\n"
            "Patikrinkite ar vartotojas yra scamerių sąraše"
        )
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Sanitize username
    check_username = sanitize_username(context.args[0])
    if not check_username or len(check_username) < 2:
        msg = await update.message.reply_text("❌ Netinkamas vartotojo vardas! Naudok @username formatą.")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check if in confirmed scammers list
    if check_username.lower() in confirmed_scammers:
        scammer_info = confirmed_scammers[check_username.lower()]
        confirmed_date = scammer_info['timestamp'].strftime('%Y-%m-%d')
        reports_count = scammer_info.get('reports_count', 1)
        
        msg = await update.message.reply_text(
            f"🚨 **SCAMER RASTAS!** 🚨\n\n"
            f"**Vartotojas:** {check_username}\n"
            f"**Statusas:** ❌ Patvirtintas scameris\n"
            f"**Patvirtinta:** {confirmed_date}\n"
            f"**Pranešimų:** {reports_count}\n"
            f"**Įrodymai:** {scammer_info.get('proof', 'Nenurodyta')}\n\n"
            f"⚠️ **ATSARGIAI!** Šis vartotojas yra žinomas scameris!"
        )
        context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))
    else:
        # Check if there are pending reports
        pending_count = sum(1 for report in pending_scammer_reports.values() 
                          if report['username'].lower() == check_username.lower())
        
        if pending_count > 0:
            msg = await update.message.reply_text(
                f"🔍 **PATIKRA ATLIKTA**\n\n"
                f"**Vartotojas:** {check_username}\n"
                f"**Statusas:** ⚠️ Yra nepatvirtintų pranešimų ({pending_count})\n"
                f"**Rekomendacija:** Būkite atsargūs, pranešimai dar tikrinami\n\n"
                f"✅ Nėra patvirtintų scam įrašų"
            )
        else:
            msg = await update.message.reply_text(
                f"✅ **PATIKRA ATLIKTA**\n\n"
                f"**Vartotojas:** {check_username}\n"
                f"**Statusas:** ✅ Švarus\n"
                f"**Pranešimų:** 0\n\n"
                f"Šis vartotojas nėra mūsų scamerių sąraše."
            )
        
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))

# Admin commands for scammer management
async def approve_scammer(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to approve a scammer report"""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali patvirtinti scamer pranešimus!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if len(context.args) < 1:
        msg = await update.message.reply_text("Naudok: /approve_scammer [report_id]")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        report_id = int(context.args[0])
        if report_id not in pending_scammer_reports:
            msg = await update.message.reply_text(f"Pranešimas #{report_id} nerastas!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        
        report = pending_scammer_reports[report_id]
        username = report['username'].lower()
        
        # Move to confirmed scammers
        confirmed_scammers[username] = {
            'confirmed_by': user_id,
            'reporter_id': report['reporter_id'],  # Track original reporter for daily limits
            'proof': report['proof'],
            'timestamp': datetime.now(TIMEZONE),
            'reports_count': 1,
            'original_report_id': report_id
        }
        
        # Remove from pending
        del pending_scammer_reports[report_id]
        
        # Save data
        save_data(confirmed_scammers, 'confirmed_scammers.pkl')
        save_data(pending_scammer_reports, 'pending_scammer_reports.pkl')
        
        # Notify original reporter
        try:
            await context.bot.send_message(
                chat_id=report['chat_id'],
                text=f"✅ **PRANEŠIMAS PATVIRTINTAS**\n\n"
                     f"Jūsų pranešimas apie {report['username']} buvo patvirtintas!\n"
                     f"Vartotojas pridėtas į scamerių sąrašą. Ačiū už bendruomenės saugumą! 🛡️"
            )
        except:
            pass  # Chat might be unavailable
        
        msg = await update.message.reply_text(
            f"✅ **SCAMER PATVIRTINTAS**\n\n"
            f"**Report ID:** #{report_id}\n"
            f"**Scameris:** {report['username']}\n"
            f"**Pridėtas į sąrašą:** ✅\n\n"
            f"Vartotojas dabar bus rodomas kaip scameris per /patikra komandą."
        )
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))
        
        logger.info(f"Admin {user_id} approved scammer report #{report_id} for {report['username']}")
        
    except ValueError:
        msg = await update.message.reply_text("Neteisingas report ID!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    except Exception as e:
        logger.error(f"Error approving scammer: {str(e)}")
        msg = await update.message.reply_text("Klaida patvirtinant pranešimą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def reject_scammer(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to reject a scammer report"""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali atmesti scamer pranešimus!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if len(context.args) < 1:
        msg = await update.message.reply_text("Naudok: /reject_scammer [report_id]")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    try:
        report_id = int(context.args[0])
        if report_id not in pending_scammer_reports:
            msg = await update.message.reply_text(f"Pranešimas #{report_id} nerastas!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
            return
        
        report = pending_scammer_reports[report_id]
        
        # Remove from pending
        del pending_scammer_reports[report_id]
        save_data(pending_scammer_reports, 'pending_scammer_reports.pkl')
        
        # Notify original reporter
        try:
            await context.bot.send_message(
                chat_id=report['chat_id'],
                text=f"❌ **PRANEŠIMAS ATMESTAS**\n\n"
                     f"Jūsų pranešimas apie {report['username']} buvo atmestas.\n"
                     f"Įrodymai buvo nepakankant arba neteisingi."
            )
        except:
            pass
        
        msg = await update.message.reply_text(
            f"❌ **PRANEŠIMAS ATMESTAS**\n\n"
            f"**Report ID:** #{report_id}\n"
            f"**Apie:** {report['username']}\n"
            f"**Statusas:** Atmestas"
        )
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        
        logger.info(f"Admin {user_id} rejected scammer report #{report_id} for {report['username']}")
        
    except ValueError:
        msg = await update.message.reply_text("Neteisingas report ID!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
    except Exception as e:
        logger.error(f"Error rejecting scammer: {str(e)}")
        msg = await update.message.reply_text("Klaida atmestant pranešimą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

async def scameriai(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of confirmed scammers (public command)"""
    chat_id = update.message.chat_id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if not confirmed_scammers:
        msg = await update.message.reply_text("✅ Scamerių sąrašas tuščias! Bendruomenė švarūs. 🛡️")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Create paginated list for mobile-friendly display
    scammer_text = "🚨 **PATVIRTINTI SCAMERIAI** 🚨\n"
    scammer_text += f"📊 Viso: {len(confirmed_scammers)} | Būkite atsargūs!\n\n"
    
    # Sort by most recent first
    sorted_scammers = sorted(confirmed_scammers.items(), 
                           key=lambda x: x[1]['timestamp'], reverse=True)
    
    for i, (username, info) in enumerate(sorted_scammers[:20], 1):  # Show top 20
        date = info['timestamp'].strftime('%m-%d')
        proof_short = info['proof'][:40] + "..." if len(info['proof']) > 40 else info['proof']
        
        scammer_text += f"🚫 **{i}. @{username}**\n"
        scammer_text += f"   📅 {date} | 📝 {proof_short}\n\n"
    
    if len(confirmed_scammers) > 20:
        scammer_text += f"... ir dar {len(confirmed_scammers) - 20} scamerių\n\n"
    
    scammer_text += "🔍 Naudok `/patikra @username` specifinei patikriai\n"
    scammer_text += "🚨 Naudok `/scameris @user įrodymai` pranešti naują"
    
    msg = await update.message.reply_text(scammer_text, parse_mode='Markdown')
    context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))

async def scammer_list(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed list of confirmed scammers (admin only)"""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali matyti detalų scamerių sąrašą!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if not confirmed_scammers:
        msg = await update.message.reply_text("✅ Scamerių sąrašas tuščias!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    scammer_text = "🚨 **ADMIN - PATVIRTINTI SCAMERIAI** 🚨\n\n"
    
    for i, (username, info) in enumerate(confirmed_scammers.items(), 1):
        date = info['timestamp'].strftime('%Y-%m-%d %H:%M')
        proof_short = info['proof'][:60] + "..." if len(info['proof']) > 60 else info['proof']
        reporter_id = info.get('reporter_id', 'Unknown')
        confirmed_by = info.get('confirmed_by', 'Unknown')
        
        scammer_text += f"**{i}.** @{username}\n"
        scammer_text += f"   📅 {date}\n"
        scammer_text += f"   👤 Reporter: {reporter_id}\n"
        scammer_text += f"   ✅ Confirmed by: {confirmed_by}\n"
        scammer_text += f"   📝 {proof_short}\n\n"
    
    scammer_text += f"**Viso scamerių:** {len(confirmed_scammers)}"
    
    msg = await update.message.reply_text(scammer_text, parse_mode='Markdown')
    context.job_queue.run_once(delete_message_job, 180, data=(chat_id, msg.message_id))

async def pending_reports(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending scammer reports (admin only)"""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    if user_id != ADMIN_CHAT_ID:
        msg = await update.message.reply_text("Tik adminas gali matyti laukiančius pranešimus!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if not pending_scammer_reports:
        msg = await update.message.reply_text("✅ Nėra laukiančių pranešimų!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    reports_text = "⏳ **LAUKIANTYS PRANEŠIMAI** ⏳\n\n"
    
    for report_id, report in pending_scammer_reports.items():
        date = report['timestamp'].strftime('%m-%d %H:%M')
        proof_short = report['proof'][:40] + "..." if len(report['proof']) > 40 else report['proof']
        
        reports_text += f"**#{report_id}** {report['username']}\n"
        reports_text += f"   👤 {report['reporter_username']}\n"
        reports_text += f"   📅 {date}\n"
        reports_text += f"   📝 {proof_short}\n"
        reports_text += f"   ✅ `/approve_scammer {report_id}`\n"
        reports_text += f"   ❌ `/reject_scammer {report_id}`\n\n"
    
    reports_text += f"**Viso pranešimų:** {len(pending_scammer_reports)}"
    
    msg = await update.message.reply_text(reports_text, parse_mode='Markdown')
    context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))


async def help_command(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show comprehensive help information"""
    chat_id = update.message.chat_id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    help_text = """
🤖 Greitas Komandų Sąrašas

📚 Nori detalų vadovą? Naudok /komandos - pilnas vadovas su pavyzdžiais!

📊 Pagrindinės Komandos:
📊 /balsuoti - Balsuoti už pardavėjus balsavimo grupėje
👎 /nepatiko @pardavejas priežastis - Pateikti skundą (+5 tšk)
💰 /points - Patikrinti savo taškus ir pokalbių seriją
👑 /chatking - Visų laikų pokalbių lyderiai
📈 /barygos - Pardavėjų reitingai ir statistika

🛡️ Saugumo Sistema:
🚨 /scameris @username įrodymai - Pranešti apie scamerį (+3 tšk, 5/dieną)
🔍 /patikra @username - Patikrinti ar vartotojas scameris
📋 /scameriai - Peržiūrėti visų patvirtintų scamerių sąrašą

🎮 Žaidimai ir Veikla:
🎯 /coinflip suma @vartotojas - Iššūkis monetos metimui
📋 /apklausa klausimas - Sukurti grupės apklausą

ℹ️ Informacija:
📚 /komandos - Pilnas komandų vadovas
❓ /whoami - Tavo vartotojo informacija

🎖️ Taškų Sistema:
• Balsavimas už pardavėją: +5 taškų (1x per savaitę)
• Skundas pardavėjui: +5 taškų (1x per savaitę)  
• Scamerio pranešimas: +3 taškų (5x per dieną)
• Kasdieniai pokalbiai: 1-3 taškų + serijos bonusas
• Serijos bonusas: +1 tšk už kiekvieną 3 dienų seriją

💬 Rašyk kasdien kaupiant taškus ir seriją!
"""
    
    msg = await update.message.reply_text(help_text)
    context.job_queue.run_once(delete_message_job, 90, data=(chat_id, msg.message_id))

async def komandos(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show comprehensive list of all commands with detailed explanations"""
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    

    
    commands_text = f"""
📚 **VISŲ KOMANDŲ VADOVAS** 📚
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏆 **BALSAVIMO SISTEMA**
📊 `/barygos` - Pardavėjų reitingai (savaitės, mėnesio, visų laikų)
📊 `/balsuoti` - Nukreipia į balsavimo grupę (+5 tšk, 1x/savaitę)
👎 `/nepatiko @pardavejas priežastis` - Skundu pardavėją (+5 tšk, 1x/savaitę)

🛡️ **SAUGUMO SISTEMA**
🚨 `/scameris @username įrodymai` - Pranešti scamerį (+3 tšk, 5x/dieną)
🔍 `/patikra @username` - Patikrinti ar vartotojas scameris
📋 `/scameriai` - Peržiūrėti visų patvirtintų scamerių sąrašą

💰 **TAŠKŲ SISTEMA**
💰 `/points` - Patikrinti savo taškus ir pokalbių seriją
👑 `/chatking` - Visų laikų pokalbių lyderiai su pasiekimų lygiais

🎮 **ŽAIDIMAI IR VEIKLA**
🎯 `/coinflip suma @vartotojas` - Iššūkis monetos metimui (laimėtojas gauna taškus)
📋 `/apklausa klausimas` - Sukurti grupės apklausą

ℹ️ **INFORMACIJA**
📚 `/komandos` - Šis detalus komandų sąrašas
🤖 `/help` - Trumpas pagalbos tekstas
❓ `/whoami` - Tavo vartotojo informacija ir ID
🔧 `/debug` - Grupės administratoriai (tik adminams)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💎 **TAŠKŲ GAVIMO BŪDAI**
• 📊 Balsavimas už pardavėją: **+5 taškų** (1x per savaitę)
• 👎 Skundas pardavėjui: **+5 taškų** (1x per savaitę)
• 🚨 Scamerio pranešimas: **+3 taškų** (5x per dieną)
• 💬 Kasdieniai pokalbiai: **1-3 taškų** + serijos bonusas
• 🔥 Serijos bonusas: **+1 tšk** už kiekvieną 3 dienų seriją
• 🎯 Monetos metimas: **Laimėtojo suma** taškų

🏅 **POKALBIŲ LYGIAI**
🌱 **Pradžia:** 1-99 žinučių
📈 **Naujokas:** 100-499 žinučių  
🌟 **Aktyvus:** 500-999 žinučių
💎 **Meistras:** 1,000-4,999 žinučių
⚡ **Ekspertas:** 5,000-9,999 žinučių
🔥 **Legenda:** 10,000+ žinučių

⏰ **AUTOMATINIAI RESTARTAI**
• 🗓️ Savaitės balsai: kas sekmadienį 23:00
• 📅 Mėnesio balsai: kiekvieną mėnesio 1-ą dieną
• 💬 Pokalbių taškų suvestinė: kasdien 6:00

🔒 **SAUGUMO PATARIMAI**
• Visada naudok `/patikra @username` prieš sandorį
• Pranešk apie scamerius su detaliais įrodymais
• Saugok savo asmeninę informaciją
• Nenurodyti pin kodų ar slaptažodžių

📱 **NAUDOJIMO PATARIMAI**
• Komandos veikia tik šioje grupėje
• Naudok @ prieš vartotojo vardus
• Dalis komandų automatiškai ištrinamos po laiko
• Aktyvus dalyvavimas = daugiau taškų"""



    commands_text += f"""

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 **GREITI PAVYZDŽIAI**
• Balsuoti: `/balsuoti` → Spausk nuorodą → Rinktis pardavėją
• Patikrinti: `/patikra @username` → Gauni saugumo ataskaitą  
• Pranešti: `/scameris @blogas Nesiunčia prekių, ignoruoja`
• Žaisti: `/coinflip 10 @friends` → Mėtkyos monetą už 10 tšk
• Skundas: `/nepatiko @pardavejas Bloga kokybė, vėluoja`

📊 **STATISTIKOS**
• Aktyvūs vartotojai šiandien: ~{len(daily_messages)}
• Visų laikų žinučių: {sum(alltime_messages.values()):,}
• Patvirtinti scameriai: {len(confirmed_scammers)}
• Patikimi pardavėjai: {len(trusted_sellers)}

💡 **PRO PATARIMAI**
• Rašyk kasdien - serija didina taškų gavimą
• Dalyvaukite apklausose - stiprina bendruomenę  
• Praneškit apie scamerius - apsaugot kitus
• Sekite pardavėjų reitingus - raskite geriausius

Norint gauti pilną pagalbą: `/help`
"""

    try:
        msg = await update.message.reply_text(commands_text, parse_mode='Markdown')
        context.job_queue.run_once(delete_message_job, 180, data=(chat_id, msg.message_id))  # Keep longer for reading
        
        # Log command usage
        analytics.log_command_usage('komandos', user_id, chat_id)
        
    except telegram.error.TelegramError as e:
        # Fallback without markdown if formatting fails
        logger.error(f"Error sending formatted komandos: {str(e)}")
        try:
            fallback_text = commands_text.replace('**', '').replace('*', '')
            msg = await update.message.reply_text(fallback_text)
            context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))
        except Exception as fallback_error:
            logger.error(f"Fallback komandos also failed: {str(fallback_error)}")
            msg = await update.message.reply_text("❌ Klaida rodant komandų sąrašą!")
            context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))

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

# Global error handler
async def error_handler(update: object, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and handle them gracefully"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    # Try to send a simple error message if we have update info
    if isinstance(update, telegram.Update) and update.effective_message:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Oops! Something went wrong. Please try again.",
                reply_to_message_id=update.effective_message.message_id
            )
        except:
            # If we can't send to the chat, don't crash
            pass
    
    # Log the error with traceback for debugging
    logger.exception("Full error traceback:", exc_info=context.error)

# Enhanced scammer reporting with evidence
async def scameris_advanced(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Enhanced scammer reporting with evidence support"""
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Check if user is replying to evidence
    evidence_data = []
    if update.message.reply_to_message:
        reply_msg = update.message.reply_to_message
        
        # Extract evidence from replied message
        if reply_msg.photo:
            evidence_data.append({
                'type': 'screenshot',
                'file_id': reply_msg.photo[-1].file_id,
                'description': reply_msg.caption or 'Screenshot evidence',
                'metadata': {'message_id': reply_msg.message_id}
            })
        elif reply_msg.document:
            evidence_data.append({
                'type': 'document',
                'file_id': reply_msg.document.file_id,
                'description': reply_msg.caption or 'Document evidence',
                'metadata': {'filename': reply_msg.document.file_name}
            })
        elif reply_msg.video:
            evidence_data.append({
                'type': 'video',
                'file_id': reply_msg.video.file_id,
                'description': reply_msg.caption or 'Video evidence',
                'metadata': {'duration': reply_msg.video.duration}
            })
        elif reply_msg.voice:
            evidence_data.append({
                'type': 'voice',
                'file_id': reply_msg.voice.file_id,
                'description': 'Voice message evidence',
                'metadata': {'duration': reply_msg.voice.duration}
            })
        elif reply_msg.text:
            evidence_data.append({
                'type': 'chat_log',
                'content': reply_msg.text,
                'description': 'Chat log evidence',
                'metadata': {'from_user': reply_msg.from_user.username}
            })
    
    # Parse command arguments
    if len(context.args) < 3:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="📋 **GELTONIJI SCAMER RAPORTAVIMAS** 📋\n\n"
                 "**Naudojimas:** `/scameris_advanced @username kategorija aprašymas`\n\n"
                 "**Kategorijos:**\n"
                 "• `seller` - Pardavėjo scam\n"
                 "• `buyer` - Pirkėjo scam\n"
                 "• `phishing` - Phishing\n"
                 "• `fake` - Fake identity\n"
                 "• `payment` - Payment fraud\n"
                 "• `social` - Social engineering\n"
                 "• `bot` - Spam bot\n"
                 "• `other` - Kita\n\n"
                 "**Pavyzdys:** `/scameris_advanced @scammer123 seller Nepavede prekės, ignoruoja žinutes`\n\n"
                 "💡 **Patarimas:** Atsakyk į žinutę su įrodymais prieš naudodamas komandą!",
            parse_mode='Markdown'
        )
        context.job_queue.run_once(delete_message_job, 90, data=(chat_id, msg.message_id))
        return
    
    # Validate inputs
    reported_username = sanitize_username(context.args[0])
    category_map = {
        'seller': 'seller_scam',
        'buyer': 'buyer_scam',
        'phishing': 'phishing',
        'fake': 'fake_identity',
        'payment': 'payment_fraud',
        'social': 'social_engineering',
        'bot': 'spam_bot',
        'other': 'other'
    }
    
    category = category_map.get(context.args[1].lower(), 'other')
    description = sanitize_text_input(" ".join(context.args[2:]), max_length=1000)
    
    if not description or len(description) < 10:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Prašau nurodyti detalų aprašymą (bent 10 simbolių)!"
        )
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Add text evidence if no other evidence provided
    if not evidence_data:
        evidence_data.append({
            'type': 'text',
            'content': description,
            'description': 'Reporter description'
        })
    
    # Create reporter data
    reporter_data = {
        'user_id': user_id,
        'username': f"@{update.message.from_user.username}" if update.message.from_user.username else f"User {user_id}",
        'group_id': chat_id,
        'timestamp': datetime.now(TIMEZONE),
        'reputation': advanced_scammer_db.user_reputation.get(user_id, 0)
    }
    
    # Create scammer profile
    profile_id = advanced_scammer_db.create_scammer_profile(
        reported_username, 
        reporter_data, 
        evidence_data, 
        category
    )
    
    # Save database
    advanced_scammer_db.save_database()
    
    # Send confirmation
    category_display = advanced_scammer_db.scammer_categories[category]
    risk_display = advanced_scammer_db.risk_levels[advanced_scammer_db.scammer_profiles[profile_id]['risk_level']]
    
    confirmation_text = f"✅ **SCAMER RAPORTAJE GAVESITAS** ✅\n\n"
    confirmation_text += f"**ID:** `{profile_id}`\n"
    confirmation_text += f"**Apie:** {reported_username}\n"
    confirmation_text += f"**Kategorija:** {category_display}\n"
    confirmation_text += f"**Rizikos lygis:** {risk_display}\n"
    confirmation_text += f"**Įrodymų:** {len(evidence_data)}\n"
    confirmation_text += f"**Statusas:** 🔍 Tikrinamas\n\n"
    confirmation_text += f"Adminai peržiūrės per 24h. Ačiū už saugesnę bendruomenę! 🛡️"
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=confirmation_text,
        parse_mode='Markdown'
    )
    context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))
    
    # Notify admins
    admin_text = f"🚨 **NAUJAS IŠPLĖSTAS SCAMER PRANEŠIMAS** 🚨\n\n"
    admin_text += f"**Profile ID:** `{profile_id}`\n"
    admin_text += f"**Username:** {reported_username}\n"
    admin_text += f"**Kategorija:** {category_display}\n"
    admin_text += f"**Rizikos lygis:** {risk_display}\n"
    admin_text += f"**Reporteris:** {reporter_data['username']}\n"
    admin_text += f"**Grupė:** {chat_id}\n"
    admin_text += f"**Įrodymų tipai:** {', '.join(ev['type'] for ev in evidence_data)}\n"
    admin_text += f"**Aprašymas:** {description}\n\n"
    admin_text += f"**Veiksmai:**\n"
    admin_text += f"✅ `/approve_advanced {profile_id}` - Patvirtinti\n"
    admin_text += f"❌ `/reject_advanced {profile_id}` - Atmesti\n"
    admin_text += f"📋 `/profile_details {profile_id}` - Detalės\n"
    admin_text += f"🔗 `/add_to_network {profile_id} network_name` - Pridėti į tinklą"
    
    await safe_send_message(context.bot, ADMIN_CHAT_ID, admin_text, parse_mode='Markdown')
    
    # Add points
    user_points[user_id] = user_points.get(user_id, 0) + 5  # More points for detailed reports
    save_data(user_points, 'user_points.pkl')
    
    logger.info(f"Advanced scammer report {profile_id}: {reported_username} reported by user {user_id}")

# Enhanced scammer check with risk assessment
async def patikra_advanced(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Enhanced user safety check with risk assessment"""
    chat_id = update.message.chat_id
    
    if not is_allowed_group(chat_id):
        msg = await update.message.reply_text("Botas neveikia šioje grupėje!")
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    if len(context.args) < 1:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="📋 **IŠPLĖSTA SAUGUMO PATIKRA** 📋\n\n"
                 "**Naudojimas:** `/patikra_advanced @username`\n\n"
                 "**Funkcijos:**\n"
                 "• 🔍 Scamerių duomenų bazės patikra\n"
                 "• 📊 Rizikos lygio vertinimas\n"
                 "• 🌐 Kelių grupių duomenų analizė\n"
                 "• 🔗 Scamerių tinklų patikra\n"
                 "• 📈 Reputacijos analizė\n\n"
                 "**Pavyzdys:** `/patikra_advanced @user123`",
            parse_mode='Markdown'
        )
        context.job_queue.run_once(delete_message_job, 60, data=(chat_id, msg.message_id))
        return
    
    # Get username
    check_username = sanitize_username(context.args[0])
    if not check_username:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Netinkamas vartotojo vardas! Naudok @username formatą."
        )
        context.job_queue.run_once(delete_message_job, 45, data=(chat_id, msg.message_id))
        return
    
    # Get comprehensive risk assessment
    risk_assessment = advanced_scammer_db.get_user_risk_assessment(check_username)
    
    # Build response
    response_text = f"🔍 **SAUGUMO ATASKAITA** 🔍\n\n"
    response_text += f"**Vartotojas:** {check_username}\n"
    response_text += f"**Rizikos lygis:** {advanced_scammer_db.risk_levels[risk_assessment['risk_level']]}\n"
    response_text += f"**Statusas:** {risk_assessment['status'].replace('_', ' ').title()}\n"
    response_text += f"**Rekomendacija:** {risk_assessment['recommendation']}\n\n"
    
    # Add detailed information based on status
    if risk_assessment['status'] == 'confirmed_scammer':
        profiles = risk_assessment['profiles']
        response_text += f"⚠️ **PAVOJUS! PATVIRTINTAS SCAMERIS** ⚠️\n\n"
        for profile in profiles[:3]:  # Show max 3 profiles
            category = advanced_scammer_db.scammer_categories.get(profile['category'], 'Unknown')
            response_text += f"• **Kategorija:** {category}\n"
            response_text += f"• **Sukurta:** {profile['created_date'].strftime('%Y-%m-%d')}\n"
            response_text += f"• **Patvirtinta pranešimų:** {profile['confirmed_reports']}\n\n"
    
    elif risk_assessment['status'] == 'multiple_reports':
        reports = risk_assessment['reports']
        response_text += f"⚠️ **KELIŲ GRUPIŲ PRANEŠIMAI** ⚠️\n\n"
        response_text += f"• **Pranešimų:** {len(reports)}\n"
        recent_reports = sorted(reports, key=lambda x: x.get('timestamp', datetime.min), reverse=True)[:3]
        for report in recent_reports:
            if isinstance(report, dict) and 'timestamp' in report:
                response_text += f"• {report['timestamp'].strftime('%Y-%m-%d')}: {report.get('details', 'Report')}\n"
    
    elif risk_assessment['status'] == 'low_reputation':
        response_text += f"📉 **ŽEMA REPUTACIJA** 📉\n\n"
        response_text += f"• **Reputacijos taškai:** {risk_assessment['reputation']}\n"
        response_text += f"• **Patarimas:** Būkite atsargūs su sandoriais\n"
    
    else:
        response_text += f"✅ **ŠVARUS VARTOTOJAS** ✅\n\n"
        response_text += f"• Nėra žinomų pranešimų\n"
        response_text += f"• Nėra rizikos indikatorių\n"
        response_text += f"• Sandoriai turėtų būti saugūs\n"
    
    # Add general safety tips
    response_text += f"\n🛡️ **SAUGUMO PATARIMAI:**\n"
    response_text += f"• Visada patikrinkite prieš sandorį\n"
    response_text += f"• Naudokite apsaugotus mokėjimo būdus\n"
    response_text += f"• Nepateikite asmeninės informacijos\n"
    response_text += f"• Praneškit apie įtartinus veiksmus"
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=response_text,
        parse_mode='Markdown'
    )
    context.job_queue.run_once(delete_message_job, 120, data=(chat_id, msg.message_id))

# Add handlers
application.add_handler(CommandHandler(['startas'], startas))
application.add_handler(CommandHandler(['activate_group'], activate_group))
application.add_handler(CommandHandler(['nepatiko'], nepatiko))
application.add_handler(CommandHandler(['approve'], approve))
application.add_handler(CommandHandler(['addseller'], addseller))
application.add_handler(CommandHandler(['removeseller'], removeseller))
application.add_handler(CommandHandler(['pardavejoinfo'], sellerinfo))
application.add_handler(CommandHandler(['barygos'], barygos))
application.add_handler(CommandHandler(['balsuoti'], balsuoti))
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
application.add_handler(CommandHandler(['komandos'], komandos))
application.add_handler(CommandHandler(['achievements'], achievements))
application.add_handler(CommandHandler(['challenges'], challenges))
application.add_handler(CommandHandler(['leaderboard'], leaderboard))
application.add_handler(CommandHandler(['mystats'], mystats))
application.add_handler(CommandHandler(['botstats'], botstats))
application.add_handler(CommandHandler(['moderation'], moderation_command))

# Scammer tracking system handlers (Legacy + Advanced)
application.add_handler(CommandHandler(['scameris'], scameris))  # Use simple version for now
application.add_handler(CommandHandler(['scameris_advanced'], scameris_advanced))  # Advanced version
application.add_handler(CommandHandler(['patikra'], patikra))  # Use simple version for now
application.add_handler(CommandHandler(['patikra_advanced'], patikra_advanced))  # Advanced version
application.add_handler(CommandHandler(['scameriai'], scameriai))  # Public scammer list
application.add_handler(CommandHandler(['approve_scammer'], approve_scammer))
application.add_handler(CommandHandler(['reject_scammer'], reject_scammer))
application.add_handler(CommandHandler(['scammer_list'], scammer_list))  # Admin detailed list
application.add_handler(CommandHandler(['pending_reports'], pending_reports))

# Advanced scammer system handlers
application.add_handler(CommandHandler(['approve_advanced'], approve_advanced))
application.add_handler(CommandHandler(['reject_advanced'], reject_advanced))
application.add_handler(CommandHandler(['profile_details'], profile_details))
application.add_handler(CommandHandler(['scammer_networks'], scammer_networks))
application.add_handler(CommandHandler(['add_to_network'], add_to_network))
application.add_handler(CommandHandler(['scammer_analytics'], scammer_analytics))
application.add_handler(CommandHandler(['evidence_details'], evidence_details))
application.add_handler(CommandHandler(['auto_protection'], auto_protection))
application.add_handler(MessageHandler(filters.Regex('^/start$') & filters.ChatType.PRIVATE, start_private))
application.add_handler(CallbackQueryHandler(handle_vote_button, pattern="vote_"))
application.add_handler(CallbackQueryHandler(handle_poll_button, pattern="poll_"))
application.add_handler(CallbackQueryHandler(handle_admin_button, pattern="admin_"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Add error handler
application.add_error_handler(error_handler)

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
            'balsuoti': 10,  # 10 seconds between votes
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

# Enhanced Scammer Database System
class AdvancedScammerDatabase:
    def __init__(self):
        self.load_database()
        self.evidence_types = {
            'screenshot': '📸 Screenshot',
            'document': '📄 Document', 
            'voice': '🎵 Voice Message',
            'video': '🎥 Video',
            'text': '📝 Text Evidence',
            'transaction': '💳 Transaction Proof',
            'chat_log': '💬 Chat Log'
        }
        
        self.scammer_categories = {
            'seller_scam': '🛒 Seller Scam',
            'buyer_scam': '💰 Buyer Scam', 
            'phishing': '🎣 Phishing',
            'fake_identity': '👤 Fake Identity',
            'payment_fraud': '💳 Payment Fraud',
            'social_engineering': '🧠 Social Engineering',
            'spam_bot': '🤖 Spam Bot',
            'other': '❓ Other'
        }
        
        self.risk_levels = {
            1: '🟢 Low Risk',
            2: '🟡 Medium Risk',
            3: '🟠 High Risk', 
            4: '🔴 Critical Risk',
            5: '⚫ Extreme Danger'
        }
    
    def load_database(self):
        """Load enhanced scammer database"""
        self.scammer_profiles = load_data('scammer_profiles.pkl', {})
        self.evidence_storage = load_data('evidence_storage.pkl', {})
        self.cross_group_reports = load_data('cross_group_reports.pkl', defaultdict(list))
        self.scammer_networks = load_data('scammer_networks.pkl', defaultdict(set))
        self.user_reputation = load_data('user_reputation.pkl', defaultdict(int))
        self.appeal_cases = load_data('appeal_cases.pkl', {})
        self.group_settings = load_data('group_settings.pkl', defaultdict(dict))
        self.auto_actions_log = load_data('auto_actions_log.pkl', [])
    
    def save_database(self):
        """Save all database components"""
        save_data(self.scammer_profiles, 'scammer_profiles.pkl')
        save_data(self.evidence_storage, 'evidence_storage.pkl')
        save_data(dict(self.cross_group_reports), 'cross_group_reports.pkl')
        save_data(dict(self.scammer_networks), 'scammer_networks.pkl')
        save_data(dict(self.user_reputation), 'user_reputation.pkl')
        save_data(self.appeal_cases, 'appeal_cases.pkl')
        save_data(dict(self.group_settings), 'group_settings.pkl')
        save_data(self.auto_actions_log, 'auto_actions_log.pkl')
    
    def create_scammer_profile(self, username, reporter_data, evidence_data, category='other'):
        """Create comprehensive scammer profile"""
        profile_id = f"scam_{int(datetime.now(TIMEZONE).timestamp())}_{random.randint(1000, 9999)}"
        
        # Calculate initial risk score
        risk_score = self.calculate_risk_score(evidence_data, category)
        
        profile = {
            'id': profile_id,
            'username': username.lower(),
            'original_username': username,
            'category': category,
            'risk_level': risk_score,
            'status': 'under_review',
            'created_date': datetime.now(TIMEZONE),
            'last_updated': datetime.now(TIMEZONE),
            'reporters': [reporter_data],
            'evidence': [],
            'affected_groups': set(),
            'related_accounts': set(),
            'admin_notes': [],
            'public_warnings': 0,
            'confirmed_reports': 0,
            'total_damage_reported': 0.0,
            'appeal_status': None,
            'auto_actions': []
        }
        
        # Store evidence
        for evidence in evidence_data:
            evidence_id = self.store_evidence(evidence, profile_id)
            profile['evidence'].append(evidence_id)
        
        self.scammer_profiles[profile_id] = profile
        self.cross_group_reports[username.lower()].append(profile_id)
        
        return profile_id
    
    def store_evidence(self, evidence_data, profile_id):
        """Store evidence with metadata"""
        evidence_id = f"ev_{int(datetime.now(TIMEZONE).timestamp())}_{random.randint(100, 999)}"
        
        evidence = {
            'id': evidence_id,
            'profile_id': profile_id,
            'type': evidence_data.get('type', 'text'),
            'content': evidence_data.get('content', ''),
            'file_id': evidence_data.get('file_id', None),
            'description': evidence_data.get('description', ''),
            'timestamp': datetime.now(TIMEZONE),
            'verified': False,
            'quality_score': self.assess_evidence_quality(evidence_data),
            'metadata': evidence_data.get('metadata', {})
        }
        
        self.evidence_storage[evidence_id] = evidence
        return evidence_id
    
    def calculate_risk_score(self, evidence_data, category):
        """Calculate risk score based on evidence and category"""
        base_scores = {
            'seller_scam': 3,
            'buyer_scam': 2,
            'phishing': 4,
            'fake_identity': 3,
            'payment_fraud': 5,
            'social_engineering': 4,
            'spam_bot': 1,
            'other': 2
        }
        
        score = base_scores.get(category, 2)
        
        # Adjust based on evidence quality
        evidence_bonus = 0
        for evidence in evidence_data:
            if evidence.get('type') in ['screenshot', 'video', 'transaction']:
                evidence_bonus += 0.5
            elif evidence.get('type') in ['document', 'chat_log']:
                evidence_bonus += 0.3
        
        final_score = min(5, int(score + evidence_bonus))
        return final_score
    
    def assess_evidence_quality(self, evidence_data):
        """Assess quality of evidence (1-10 scale)"""
        quality = 5  # Base quality
        
        if evidence_data.get('type') in ['video', 'screenshot']:
            quality += 2
        elif evidence_data.get('type') in ['document', 'transaction']:
            quality += 1
        
        if len(evidence_data.get('description', '')) > 50:
            quality += 1
        
        if evidence_data.get('metadata', {}).get('verified_source'):
            quality += 2
        
        return min(10, quality)
    
    def add_cross_group_report(self, username, group_id, reporter_id, details):
        """Add report from another group"""
        report = {
            'group_id': group_id,
            'reporter_id': reporter_id,
            'details': details,
            'timestamp': datetime.now(TIMEZONE)
        }
        
        self.cross_group_reports[username.lower()].append(report)
        
        # Check if this creates a pattern
        if len(self.cross_group_reports[username.lower()]) >= 2:
            self.flag_for_investigation(username, "Multiple group reports")
    
    def flag_for_investigation(self, username, reason):
        """Flag user for admin investigation"""
        logger.warning(f"User {username} flagged for investigation: {reason}")
        # This could trigger admin notifications
    
    def check_scammer_network(self, user_data):
        """Check if user is part of known scammer network"""
        # Implementation for checking networks, similar usernames, etc.
        pass
    
    def get_user_risk_assessment(self, username, user_id=None):
        """Get comprehensive risk assessment for user"""
        username_lower = username.lower()
        
        # Check direct matches
        if username_lower in [profile['username'] for profile in self.scammer_profiles.values()]:
            matching_profiles = [p for p in self.scammer_profiles.values() if p['username'] == username_lower]
            highest_risk = max(p['risk_level'] for p in matching_profiles)
            return {
                'risk_level': highest_risk,
                'status': 'confirmed_scammer',
                'profiles': matching_profiles,
                'recommendation': 'BLOCK IMMEDIATELY'
            }
        
        # Check cross-group reports
        reports = self.cross_group_reports.get(username_lower, [])
        if len(reports) >= 2:
            return {
                'risk_level': 3,
                'status': 'multiple_reports',
                'reports': reports,
                'recommendation': 'EXERCISE CAUTION'
            }
        
        # Check user reputation
        reputation = self.user_reputation.get(user_id, 0) if user_id else 0
        if reputation < -5:
            return {
                'risk_level': 2,
                'status': 'low_reputation',
                'reputation': reputation,
                'recommendation': 'BE CAREFUL'
            }
        
        return {
            'risk_level': 1,
            'status': 'clean',
            'recommendation': 'APPEARS SAFE'
        }

# Initialize advanced scammer database
advanced_scammer_db = AdvancedScammerDatabase()



# Cross-group scammer alerts
async def send_cross_group_alert(scammer_username, profile_data):
    """Send alerts to all groups about confirmed scammer"""
    alert_text = f"🚨 **SCAMER Alert** 🚨\n\n"
    alert_text += f"**Vartotojas:** {scammer_username}\n"
    alert_text += f"**Kategorija:** {advanced_scammer_db.scammer_categories.get(profile_data['category'], 'Unknown')}\n"
    alert_text += f"**Rizikos lygis:** {advanced_scammer_db.risk_levels[profile_data['risk_level']]}\n"
    alert_text += f"**Patvirtinta:** {profile_data['created_date'].strftime('%Y-%m-%d %H:%M')}\n\n"
    alert_text += f"⚠️ Šis vartotojas buvo pridėtas į scamerių duomenų bazę!\n"
    alert_text += f"Naudok `/patikra {scammer_username}` daugiau informacijos."
    
    # Send to all allowed groups
    for group_id in allowed_groups:
        try:
            await application.bot.send_message(
                chat_id=int(group_id),
                text=alert_text,
                parse_mode='Markdown'
            )
            await asyncio.sleep(1)  # Rate limiting
        except Exception as e:
            logger.error(f"Failed to send alert to group {group_id}: {str(e)}")

# Admin commands for advanced system
async def approve_advanced(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Approve advanced scammer report"""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    if len(context.args) < 1:
        msg = await update.message.reply_text("Naudok: /approve_advanced [profile_id]")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    profile_id = context.args[0]
    if profile_id not in advanced_scammer_db.scammer_profiles:
        msg = await update.message.reply_text(f"Profilis {profile_id} nerastas!")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    # Approve the profile
    profile = advanced_scammer_db.scammer_profiles[profile_id]
    profile['status'] = 'confirmed'
    profile['confirmed_reports'] += 1
    profile['last_updated'] = datetime.now(TIMEZONE)
    
    # Add to confirmed scammers (legacy system compatibility)
    confirmed_scammers[profile['username']] = {
        'confirmed_by': user_id,
        'reporter_id': profile['reporters'][0]['user_id'],
        'proof': f"Advanced report ID: {profile_id}",
        'timestamp': datetime.now(TIMEZONE),
        'reports_count': profile['confirmed_reports'],
        'profile_id': profile_id
    }
    
    # Save data
    advanced_scammer_db.save_database()
    save_data(confirmed_scammers, 'confirmed_scammers.pkl')
    
    # Send cross-group alerts
    await send_cross_group_alert(profile['original_username'], profile)
    
    msg = await update.message.reply_text(
        f"✅ **PROFILIS PATVIRTINTAS**\n\n"
        f"**ID:** `{profile_id}`\n"
        f"**Scameris:** {profile['original_username']}\n"
        f"**Kategorija:** {advanced_scammer_db.scammer_categories[profile['category']]}\n"
        f"**Rizikos lygis:** {advanced_scammer_db.risk_levels[profile['risk_level']]}\n\n"
        f"Kryžminių grupių perspėjimai išsiųsti! 🚨",
        parse_mode='Markdown'
    )
    context.job_queue.run_once(delete_message_job, 90, data=(update.message.chat_id, msg.message_id))

async def reject_advanced(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Reject advanced scammer report"""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    if len(context.args) < 1:
        msg = await update.message.reply_text("Naudok: /reject_advanced [profile_id] [reason]")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    profile_id = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Insufficient evidence"
    
    if profile_id not in advanced_scammer_db.scammer_profiles:
        msg = await update.message.reply_text(f"Profilis {profile_id} nerastas!")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    # Reject the profile
    profile = advanced_scammer_db.scammer_profiles[profile_id]
    profile['status'] = 'rejected'
    profile['admin_notes'].append(f"Rejected by {user_id}: {reason}")
    profile['last_updated'] = datetime.now(TIMEZONE)
    
    # Save database
    advanced_scammer_db.save_database()
    
    msg = await update.message.reply_text(
        f"❌ **PROFILIS ATMESTAS**\n\n"
        f"**ID:** `{profile_id}`\n"
        f"**Vartotojas:** {profile['original_username']}\n"
        f"**Priežastis:** {reason}\n"
        f"**Statusas:** Atmestas",
        parse_mode='Markdown'
    )
    context.job_queue.run_once(delete_message_job, 60, data=(update.message.chat_id, msg.message_id))

async def profile_details(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed profile information"""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    if len(context.args) < 1:
        msg = await update.message.reply_text("Naudok: /profile_details [profile_id]")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    profile_id = context.args[0]
    if profile_id not in advanced_scammer_db.scammer_profiles:
        msg = await update.message.reply_text(f"Profilis {profile_id} nerastas!")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    profile = advanced_scammer_db.scammer_profiles[profile_id]
    
    # Build detailed report
    details_text = f"📋 **PROFILIO DETALĖS** 📋\n\n"
    details_text += f"**ID:** `{profile_id}`\n"
    details_text += f"**Username:** {profile['original_username']}\n"
    details_text += f"**Kategorija:** {advanced_scammer_db.scammer_categories[profile['category']]}\n"
    details_text += f"**Rizikos lygis:** {advanced_scammer_db.risk_levels[profile['risk_level']]}\n"
    details_text += f"**Statusas:** {profile['status'].title()}\n"
    details_text += f"**Sukurta:** {profile['created_date'].strftime('%Y-%m-%d %H:%M')}\n"
    details_text += f"**Atnaujinta:** {profile['last_updated'].strftime('%Y-%m-%d %H:%M')}\n\n"
    
    # Reporter information
    reporter = profile['reporters'][0]
    details_text += f"**📝 PRANEŠĖJAS:**\n"
    details_text += f"• Username: {reporter['username']}\n"
    details_text += f"• User ID: {reporter['user_id']}\n"
    details_text += f"• Grupė: {reporter['group_id']}\n"
    details_text += f"• Reputacija: {reporter['reputation']}\n\n"
    
    # Evidence summary
    details_text += f"**🔍 ĮRODYMAI ({len(profile['evidence'])}):**\n"
    for evidence_id in profile['evidence'][:5]:  # Show first 5
        evidence = advanced_scammer_db.evidence_storage.get(evidence_id, {})
        evidence_type = advanced_scammer_db.evidence_types.get(evidence.get('type', 'unknown'), 'Unknown')
        quality = evidence.get('quality_score', 0)
        details_text += f"• {evidence_type} (Kokybė: {quality}/10)\n"
    
    if len(profile['evidence']) > 5:
        details_text += f"• ... ir dar {len(profile['evidence']) - 5} įrodymų\n"
    
    details_text += f"\n**📊 STATISTIKOS:**\n"
    details_text += f"• Patvirtinta pranešimų: {profile['confirmed_reports']}\n"
    details_text += f"• Viešų perspėjimų: {profile['public_warnings']}\n"
    details_text += f"• Susiję accounts: {len(profile['related_accounts'])}\n"
    details_text += f"• Paveiktos grupės: {len(profile['affected_groups'])}\n"
    
    # Admin notes
    if profile['admin_notes']:
        details_text += f"\n**📝 ADMIN PASTABOS:**\n"
        for note in profile['admin_notes'][-3:]:  # Last 3 notes
            details_text += f"• {note}\n"
    
    msg = await update.message.reply_text(details_text, parse_mode='Markdown')
    context.job_queue.run_once(delete_message_job, 180, data=(update.message.chat_id, msg.message_id))

async def scammer_networks(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show scammer networks"""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    networks_text = f"🔗 **SCAMERIŲ TINKLAI** 🔗\n\n"
    
    if not advanced_scammer_db.scammer_networks:
        networks_text += "Dar nėra nustatytų scamerių tinklų.\n"
        networks_text += "Naudok `/add_to_network profile_id network_name` sukurti tinklą."
    else:
        for network_name, members in advanced_scammer_db.scammer_networks.items():
            networks_text += f"**🔗 {network_name}**\n"
            networks_text += f"Narių: {len(members)}\n"
            
            # Show some members
            member_list = list(members)[:5]
            for member in member_list:
                if member in advanced_scammer_db.scammer_profiles:
                    profile = advanced_scammer_db.scammer_profiles[member]
                    networks_text += f"• {profile['original_username']}\n"
            
            if len(members) > 5:
                networks_text += f"• ... ir dar {len(members) - 5} narių\n"
            networks_text += "\n"
    
    msg = await update.message.reply_text(networks_text, parse_mode='Markdown')
    context.job_queue.run_once(delete_message_job, 120, data=(update.message.chat_id, msg.message_id))

async def add_to_network(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Add scammer to network"""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    if len(context.args) < 2:
        msg = await update.message.reply_text("Naudok: /add_to_network [profile_id] [network_name]")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    profile_id = context.args[0]
    network_name = " ".join(context.args[1:])
    
    if profile_id not in advanced_scammer_db.scammer_profiles:
        msg = await update.message.reply_text(f"Profilis {profile_id} nerastas!")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    # Add to network
    advanced_scammer_db.scammer_networks[network_name].add(profile_id)
    
    # Update profile
    profile = advanced_scammer_db.scammer_profiles[profile_id]
    profile['admin_notes'].append(f"Added to network '{network_name}' by admin {user_id}")
    profile['last_updated'] = datetime.now(TIMEZONE)
    
    # Save database
    advanced_scammer_db.save_database()
    
    msg = await update.message.reply_text(
        f"✅ **PRIDĖTAS Į TINKLĄ**\n\n"
        f"**Profilis:** `{profile_id}`\n"
        f"**Vartotojas:** {profile['original_username']}\n"
        f"**Tinklas:** {network_name}\n"
        f"**Narių tinkle:** {len(advanced_scammer_db.scammer_networks[network_name])}",
        parse_mode='Markdown'
    )
    context.job_queue.run_once(delete_message_job, 60, data=(update.message.chat_id, msg.message_id))

async def scammer_analytics(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show scammer database analytics"""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    # Calculate statistics
    total_profiles = len(advanced_scammer_db.scammer_profiles)
    confirmed_profiles = len([p for p in advanced_scammer_db.scammer_profiles.values() if p['status'] == 'confirmed'])
    pending_profiles = len([p for p in advanced_scammer_db.scammer_profiles.values() if p['status'] == 'under_review'])
    rejected_profiles = len([p for p in advanced_scammer_db.scammer_profiles.values() if p['status'] == 'rejected'])
    
    # Category breakdown
    categories = {}
    for profile in advanced_scammer_db.scammer_profiles.values():
        cat = profile['category']
        categories[cat] = categories.get(cat, 0) + 1
    
    # Risk level breakdown
    risk_levels = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for profile in advanced_scammer_db.scammer_profiles.values():
        risk_levels[profile['risk_level']] += 1
    
    analytics_text = f"📊 **SCAMERIŲ DUOMENŲ BAZĖS ANALITIKA** 📊\n\n"
    analytics_text += f"**📈 BENDRAS APŽVALGA:**\n"
    analytics_text += f"• Viso profilių: {total_profiles}\n"
    analytics_text += f"• Patvirtinti: {confirmed_profiles}\n"
    analytics_text += f"• Laukiantys: {pending_profiles}\n"
    analytics_text += f"• Atmesti: {rejected_profiles}\n"
    analytics_text += f"• Tinklų: {len(advanced_scammer_db.scammer_networks)}\n"
    analytics_text += f"• Įrodymų: {len(advanced_scammer_db.evidence_storage)}\n\n"
    
    analytics_text += f"**🏷️ KATEGORIJOS:**\n"
    for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
        cat_name = advanced_scammer_db.scammer_categories.get(cat, cat)
        analytics_text += f"• {cat_name}: {count}\n"
    
    analytics_text += f"\n**⚠️ RIZIKOS LYGIAI:**\n"
    for level, count in risk_levels.items():
        if count > 0:
            level_name = advanced_scammer_db.risk_levels[level]
            analytics_text += f"• {level_name}: {count}\n"
    
    # Recent activity
    recent_profiles = sorted(
        [p for p in advanced_scammer_db.scammer_profiles.values()],
        key=lambda x: x['created_date'],
        reverse=True
    )[:5]
    
    analytics_text += f"\n**🕒 PASKUTINIAI PRANEŠIMAI:**\n"
    for profile in recent_profiles:
        analytics_text += f"• {profile['original_username']} ({profile['created_date'].strftime('%m-%d %H:%M')})\n"
    
    msg = await update.message.reply_text(analytics_text, parse_mode='Markdown')
    context.job_queue.run_once(delete_message_job, 120, data=(update.message.chat_id, msg.message_id))

async def evidence_details(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Show evidence details"""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    if len(context.args) < 1:
        msg = await update.message.reply_text("Naudok: /evidence_details [evidence_id]")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    evidence_id = context.args[0]
    if evidence_id not in advanced_scammer_db.evidence_storage:
        msg = await update.message.reply_text(f"Įrodymas {evidence_id} nerastas!")
        context.job_queue.run_once(delete_message_job, 45, data=(update.message.chat_id, msg.message_id))
        return
    
    evidence = advanced_scammer_db.evidence_storage[evidence_id]
    profile = advanced_scammer_db.scammer_profiles.get(evidence['profile_id'], {})
    
    details_text = f"🔍 **ĮRODYMO DETALĖS** 🔍\n\n"
    details_text += f"**ID:** `{evidence_id}`\n"
    details_text += f"**Tipas:** {advanced_scammer_db.evidence_types.get(evidence['type'], 'Unknown')}\n"
    details_text += f"**Kokybės rezultatas:** {evidence['quality_score']}/10\n"
    details_text += f"**Patvirtintas:** {'Taip' if evidence['verified'] else 'Ne'}\n"
    details_text += f"**Sukurta:** {evidence['timestamp'].strftime('%Y-%m-%d %H:%M')}\n\n"
    
    details_text += f"**📝 APRAŠYMAS:**\n{evidence['description']}\n\n"
    
    if evidence['content']:
        content_preview = evidence['content'][:200]
        if len(evidence['content']) > 200:
            content_preview += "..."
        details_text += f"**📄 TURINYS:**\n{content_preview}\n\n"
    
    details_text += f"**🔗 SUSIJĘS PROFILIS:**\n"
    if profile:
        details_text += f"• {profile['original_username']}\n"
        details_text += f"• Kategorija: {advanced_scammer_db.scammer_categories.get(profile['category'], 'Unknown')}\n"
    else:
        details_text += "• Profilis nerastas\n"
    
    # Metadata
    if evidence['metadata']:
        details_text += f"\n**⚙️ METADUOMENYS:**\n"
        for key, value in evidence['metadata'].items():
            details_text += f"• {key}: {value}\n"
    
    msg = await update.message.reply_text(details_text, parse_mode='Markdown')
    context.job_queue.run_once(delete_message_job, 120, data=(update.message.chat_id, msg.message_id))

async def auto_protection(update: telegram.Update, context: telegram.ext.ContextTypes.DEFAULT_TYPE) -> None:
    """Configure automatic protection settings"""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    protection_text = f"🛡️ **AUTOMATINĖ APSAUGA** 🛡️\n\n"
    protection_text += f"**🔧 GALIMI NUSTATYMAI:**\n\n"
    protection_text += f"**1. Auto-warn** - Automatiniai perspėjimai\n"
    protection_text += f"Komanda: `/auto_warn on/off`\n\n"
    protection_text += f"**2. Auto-ban** - Automatiniai banai\n"
    protection_text += f"Komanda: `/auto_ban risk_level`\n"
    protection_text += f"Pavyzdys: `/auto_ban 4` (ban 4+ rizikos)\n\n"
    protection_text += f"**3. New-user screening** - Naujų vartotojų tikrinimas\n"
    protection_text += f"Komanda: `/screening on/off`\n\n"
    protection_text += f"**4. Cross-group alerts** - Kryžminiai perspėjimai\n"
    protection_text += f"Komanda: `/cross_alerts on/off`\n\n"
    protection_text += f"**📊 DABARTINIAI NUSTATYMAI:**\n"
    
    # Show current settings for each group
    for group_id in allowed_groups:
        settings = advanced_scammer_db.group_settings.get(group_id, {})
        protection_text += f"• Grupė {group_id}:\n"
        protection_text += f"  - Auto-warn: {'ON' if settings.get('auto_warn', False) else 'OFF'}\n"
        protection_text += f"  - Auto-ban: Risk {settings.get('auto_ban_level', 'OFF')}\n"
        protection_text += f"  - Screening: {'ON' if settings.get('screening', False) else 'OFF'}\n"
    
    msg = await update.message.reply_text(protection_text, parse_mode='Markdown')
    context.job_queue.run_once(delete_message_job, 120, data=(update.message.chat_id, msg.message_id))

if __name__ == '__main__':
    try:
        logger.info("Starting bot polling...")
        application.run_polling()
    except Exception as e:
        logger.error(f"Polling failed: {str(e)}")
    logger.info("Bot polling stopped.")
