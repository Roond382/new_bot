import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    Application
)
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import sqlite3
import html
import re
import os
from dotenv import load_dotenv

# --- Constants ---
DB_FILE = 'db.sqlite'
BAD_WORDS_FILE = 'bad_words.txt'
DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
MAX_NAME_LENGTH = 50
MAX_CONGRAT_TEXT_LENGTH = 500
MAX_ANNOUNCE_NEWS_TEXT_LENGTH = 300
MAX_DB_TEXT_LENGTH = 4000 # Max length for TEXT field in DB (Telegram message limit)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables & Config ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID_STR = os.getenv("ADMIN_CHAT_ID")
GROUP_ID_STR = os.getenv("GROUP_ID")

ADMIN_CHAT_ID: Optional[int] = None
GROUP_ID: Optional[int] = None
IS_PUBLISHING_ENABLED = False

# Validate ADMIN_CHAT_ID
try:
    if ADMIN_CHAT_ID_STR:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_STR)
except ValueError:
    logger.critical(f"ADMIN_CHAT_ID ('{ADMIN_CHAT_ID_STR}') is not a valid integer!")

# Validate GROUP_ID and enable publishing if valid
try:
    if GROUP_ID_STR:
        GROUP_ID = int(GROUP_ID_STR)
        IS_PUBLISHING_ENABLED = True
        logger.info(f"Publishing enabled for GROUP_ID: {GROUP_ID}")
    else:
        logger.warning("GROUP_ID not set in environment variables. Publishing to channel is disabled.")
except ValueError:
    logger.warning(f"GROUP_ID ('{GROUP_ID_STR}') is not a valid integer. Publishing to channel is disabled.")

CHANNEL_NAME = "Небольшой Мир: Николаевск"

REQUEST_TYPES = {
    "congrat": {"name": "Поздравление", "icon": "🎉"},
    "announcement": {"name": "Объявление", "icon": "📢"},
    "news": {"name": "Новость от жителя", "icon": "🗞️"}
}
ANNOUNCE_SUBTYPES = {
    "ride": "🚗 Попутка",
    "offer": "💡 Предложение",
    "lost": "🔍 Потеряли/Нашли"
}
HOLIDAYS = {
    "🎄 Новый год": "01-01",
    "🪖 23 Февраля": "02-23",
    "💐 8 Марта": "03-08",
    "🏅 9 Мая": "05-09",
    "🇷🇺 12 Июня": "06-12",
    "🤝 4 Ноября": "11-04"
}
HOLIDAY_TEMPLATES = {
    holiday: message for holiday, message in {
        "🎄 Новый год": "С Новым годом!\nПусть исполняются все ваши желания!",
        "🪖 23 Февраля": "С Днём защитника Отечества!\nМужества, отваги и мирного неба над головой!",
        "💐 8 Марта": "С 8 Марта!\nКрасоты, счастья и весеннего настроения!",
        "🏅 9 Мая": "С Днём Победы!\nВечная память героям!",
        "🇷🇺 12 Июня": "С Днём России!\nМира, благополучия и процветания нашей стране!",
        "🤝 4 Ноября": "С Днём народного единства!\nСогласия, мира и добра!"
    }.items()
}

# --- Conversation States ---
(
    TYPE_SELECTION, 
    SENDER_NAME_INPUT, 
    RECIPIENT_NAME_INPUT, 
    CONGRAT_HOLIDAY_CHOICE, 
    CUSTOM_CONGRAT_MESSAGE_INPUT, 
    CONGRAT_DATE_INPUT, 
    ANNOUNCE_SUBTYPE_SELECTION, 
    ANNOUNCE_TEXT_INPUT, 
    WAIT_CENSOR_APPROVAL
) = range(9)

# --- Database Operations (with enhanced error handling) ---
def get_db_connection() -> Optional[sqlite3.Connection]:
    """Establishes a connection to the SQLite database."""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row # Access columns by name
        return conn
    except sqlite3.Error as e:
        logger.error(f"Database connection error: {e}", exc_info=True)
        return None

def init_db():
    """Initializes the database table if it doesn't exist."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                type TEXT NOT NULL,
                subtype TEXT,
                from_name TEXT,
                to_name TEXT,
                text TEXT NOT NULL,
                status TEXT DEFAULT 'pending', -- pending, approved, rejected, published
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                publish_date DATE,
                published_at TIMESTAMP,
                congrat_type TEXT
            )
            """)
        logger.info("Database initialized successfully.")
    except sqlite3.Error as e:
        logger.error(f"Database initialization error: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()

def add_application(data: Dict[str, Any]) -> Optional[int]:
    """Adds a new application to the database after validation."""
    # --- Validation before DB insertion ---
    from_name = data.get('from_name')
    if from_name is not None and len(from_name) > MAX_NAME_LENGTH:
        logger.error(f"Validation Error: from_name too long for user {data.get('user_id')}")
        return None
    
    to_name = data.get('to_name')
    if to_name is not None and len(to_name) > MAX_NAME_LENGTH:
        logger.error(f"Validation Error: to_name too long for user {data.get('user_id')}")
        return None
    
    text = data.get('text')
    if text is None:
        logger.error(f"Validation Error: text is None for user {data.get('user_id')}")
        return None
    if len(text) > MAX_DB_TEXT_LENGTH:
        logger.error(f"Validation Error: text too long for user {data.get('user_id')}")
        return None
    # --- End Validation ---

    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("""
            INSERT INTO applications 
            (user_id, username, type, subtype, from_name, to_name, text, publish_date, congrat_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['user_id'],
                data.get('username'),
                data['type'],
                data.get('subtype'),
                data.get('from_name'),
                data.get('to_name'),
                data['text'],
                data.get('publish_date'),
                data.get('congrat_type')
            ))
            app_id = cur.lastrowid
            logger.info(f"Application #{app_id} added for user {data['user_id']}.")
            return app_id
    except sqlite3.Error as e:
        logger.error(f"Database error adding application for user {data.get('user_id')}: {e}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()

def get_application_details(app_id: int) -> Optional[sqlite3.Row]:
    """Retrieves details for a specific application."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
            result = cur.fetchone()
            return result
    except sqlite3.Error as e:
        logger.error(f"Database error getting details for app #{app_id}: {e}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()

def get_approved_unpublished_applications() -> list:
    """Gets all applications approved but not yet published."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn:
            cur = conn.cursor()
            # Select only necessary fields
            cur.execute("SELECT id, user_id, username, type, subtype, from_name, to_name, text, publish_date, congrat_type, status FROM applications WHERE status = 'approved' AND published_at IS NULL")
            results = cur.fetchall()
            return results
    except sqlite3.Error as e:
        logger.error(f"Database error getting approved unpublished applications: {e}", exc_info=True)
        return []
    finally:
        if conn:
            conn.close()

def update_application_status(app_id: int, status: str) -> bool:
    """Updates the status of an application."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn:
            conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
        logger.info(f"Application #{app_id} status updated to '{status}'.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Database error updating status for app #{app_id} to '{status}': {e}", exc_info=True)
        return False
    finally:
        if conn:
            conn.close()

def mark_application_as_published(app_id: int) -> bool:
    """Marks an application as published by setting published_at and status."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn:
            conn.execute("UPDATE applications SET published_at = CURRENT_TIMESTAMP, status = 'published' WHERE id = ?", (app_id,))
        logger.info(f"Application #{app_id} marked as published.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Database error marking app #{app_id} as published: {e}", exc_info=True)
        return False
    finally:
        if conn:
            conn.close()

# --- Validation & Censorship ---
def validate_name(name: str) -> bool:
    """Validates sender/recipient names (length and characters)."""
    allowed_chars = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ -")
    name = name.strip()
    return name and 2 <= len(name) <= MAX_NAME_LENGTH and all(c in allowed_chars for c in name)

def is_holiday_active(holiday_date_str: str) -> bool:
    """Checks if a holiday is currently active (within +/- 5 days)."""
    try:
        current_year = datetime.now().year
        holiday_date = datetime.strptime(f"{current_year}-{holiday_date_str}", "%Y-%m-%d")
        start_active_period = holiday_date - timedelta(days=5)
        end_active_period = holiday_date + timedelta(days=5)
        today = datetime.now()
        return start_active_period.date() <= today.date() <= end_active_period.date()
    except Exception as e:
        logger.error(f"Error checking holiday period {holiday_date_str}: {e}")
        return False

def load_bad_words() -> list:
    """Loads bad words from file or uses default list."""
    try:
        bad_words = []
        with open(BAD_WORDS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip() and not line.strip().startswith('#'):
                    words = [word.strip().lower() for word in line.split(',') if word.strip()]
                    bad_words.extend(words)
        logger.info(f"Loaded {len(bad_words)} bad words from {BAD_WORDS_FILE}")
        return bad_words
    except FileNotFoundError:
        logger.warning(f"{BAD_WORDS_FILE} not found, using default bad words list.")
        return DEFAULT_BAD_WORDS
    except Exception as e:
        logger.error(f"Failed to load {BAD_WORDS_FILE}: {e}, using default bad words list.", exc_info=True)
        return DEFAULT_BAD_WORDS

BAD_WORDS_LIST = load_bad_words()

def censor_text(text: str) -> tuple[str, bool]:
    """
    Censors text using the loaded bad words list.
    Returns tuple (censored_text, contains_bad_words).
    """
    contains_bad_words = False
    censored_text = text
    
    for word in BAD_WORDS_LIST:
        try:
            # Escape special regex characters in the bad word
            pattern = re.escape(word)
            # Use word boundaries carefully, might need adjustment for specific cases
            # Using a simpler search first to detect presence
            if re.search(pattern, censored_text, flags=re.IGNORECASE | re.UNICODE):
                contains_bad_words = True
                # Replace occurrences
                censored_text = re.sub(pattern, '***', censored_text, flags=re.IGNORECASE | re.UNICODE)
        except re.error as e:
            logger.error(f"Regex error processing bad word '{word}': {e}")
            continue # Skip this word if regex fails

    # Censor contact info
    try:
        contact_pattern = r'(звоните|пишите|телефон|номер|тел\.?|т\.)[:;\s]*([\+\d\(\).\s-]{7,})'
        censored_text = re.sub(contact_pattern, 'Контактная информация скрыта (пишите в ЛС)', censored_text, flags=re.IGNORECASE | re.UNICODE)
    except re.error as e:
        logger.error(f"Regex error processing contact pattern: {e}")

    return (censored_text, contains_bad_words)

# --- Helper Functions ---
async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs):
    """Sends a message with error handling."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramError as e:
        logger.warning(f"Failed to send message to chat_id {chat_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending message to chat_id {chat_id}: {e}", exc_info=True)

async def safe_edit_message_text(query: Optional[Update.callback_query], text: str, **kwargs):
    """Edits a message text with error handling."""
    if not query or not query.message:
        logger.warning("safe_edit_message_text called without valid query or message.")
        return
    try:
        await query.edit_message_text(text=text, **kwargs)
    except TelegramError as e:
        # Common errors: message not modified, message to edit not found
        if "message is not modified" in str(e).lower():
            logger.info(f"Message not modified for query {query.id}: {e}") # Less severe
        else:
            logger.warning(f"Failed to edit message for query {query.id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error editing message for query {query.id}: {e}", exc_info=True)

async def safe_reply_text(update: Update, text: str, **kwargs):
    """Replies to a message or edits a query message with error handling."""
    if update.callback_query:
        await safe_edit_message_text(update.callback_query, text, **kwargs)
    elif update.message:
        try:
            await update.message.reply_text(text=text, **kwargs)
        except TelegramError as e:
            logger.warning(f"Failed to reply to message {update.message.message_id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error replying to message {update.message.message_id}: {e}", exc_info=True)
    else:
        logger.warning("safe_reply_text called without message or callback_query.")

# --- Conversation Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation or resets it."""
    if not update.message:
        return ConversationHandler.END
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton(f"{v['icon']} {v['name']}", callback_data=k)
         for k, v in REQUEST_TYPES.items()]
    ]
    await safe_reply_text(update, 
        f"Добро пожаловать в {CHANNEL_NAME}!\nВыберите тип заявки:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return TYPE_SELECTION

async def handle_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's choice of application type."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query {query.id}: {e}")
        
    context.user_data["type"] = query.data
    request_type = query.data

    # Common keyboard part
    keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]

    if request_type == "congrat":
        await safe_edit_message_text(query,
            "Как вас зовут? (кто поздравляет, например: Внук Виталий)",
            reply_markup=InlineKeyboardMarkup(keyboard_nav)
        )
        return SENDER_NAME_INPUT

    elif request_type == "announcement":
        keyboard = [[InlineKeyboardButton(v, callback_data=k)] for k, v in ANNOUNCE_SUBTYPES.items()]
        keyboard.extend(keyboard_nav)
        await safe_edit_message_text(query,
            "Выберите тип объявления:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ANNOUNCE_SUBTYPE_SELECTION

    elif request_type == "news":
        await safe_edit_message_text(query,
            f"Введите вашу новость (до {MAX_ANNOUNCE_NEWS_TEXT_LENGTH} символов):",
            reply_markup=InlineKeyboardMarkup(keyboard_nav)
        )
        return ANNOUNCE_TEXT_INPUT
    else:
        await safe_edit_message_text(query, "❌ Неизвестный тип заявки.")
        return ConversationHandler.END

async def get_sender_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets and validates the sender's name for congratulations."""
    if update.callback_query and update.callback_query.data == "back_to_start":
        try: await update.callback_query.answer() 
        except: pass
        return await start_command(update, context)
    
    if not update.message or not update.message.text:
        return SENDER_NAME_INPUT
    
    if update.message.text.startswith('/start'):
        return await start_command(update, context)
    
    name = update.message.text.strip()
    keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]

    if not validate_name(name):
        await safe_reply_text(update,
            f"Пожалуйста, введите корректное имя отправителя (2–{MAX_NAME_LENGTH} букв кириллицы, пробел, дефис):",
            reply_markup=InlineKeyboardMarkup(keyboard_nav)
        )
        return SENDER_NAME_INPUT
    
    censored_name, contains_bad_words = censor_text(name)
    if contains_bad_words:
        await safe_reply_text(update,
            "⚠️ В вашем имени обнаружены запрещенные слова. "
            "Заявка автоматически закрыта. Пожалуйста, начните заново с корректным текстом."
        )
        return ConversationHandler.END
    
    context.user_data["from_name"] = name
    
    keyboard = [
        [InlineKeyboardButton("✏️ Исправить имя отправителя", callback_data="edit_sender_name")],
        *keyboard_nav
    ]
    await safe_reply_text(update,
        f"Имя отправителя: {html.escape(name)}\n\n"
        "Кого вы хотите поздравить? (например: Бабушку Вику)",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return RECIPIENT_NAME_INPUT

async def edit_sender_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Allows the user to edit the sender's name."""
    query = update.callback_query
    if not query: return SENDER_NAME_INPUT
    try: await query.answer() 
    except: pass
    
    keyboard = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
    await safe_edit_message_text(query,
        "Введите исправленное имя отправителя (кто поздравляет):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SENDER_NAME_INPUT

async def get_recipient_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets and validates the recipient's name for congratulations."""
    if update.callback_query:
        if update.callback_query.data == "back_to_start":
            try: await update.callback_query.answer() 
            except: pass
            return await start_command(update, context)
        elif update.callback_query.data == "edit_sender_name":
            return await edit_sender_name(update, context)
    
    if not update.message or not update.message.text:
        return RECIPIENT_NAME_INPUT
    
    if update.message.text.startswith('/start'):
        return await start_command(update, context)
    
    name = update.message.text.strip()
    keyboard_nav = [
        [InlineKeyboardButton("✏️ Исправить имя отправителя", callback_data="edit_sender_name")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    if not validate_name(name):
        await safe_reply_text(update,
            f"Пожалуйста, введите корректное имя получателя (2–{MAX_NAME_LENGTH} букв кириллицы, пробел, дефис):",
            reply_markup=InlineKeyboardMarkup(keyboard_nav)
        )
        return RECIPIENT_NAME_INPUT
    
    censored_name, contains_bad_words = censor_text(name)
    if contains_bad_words:
        await safe_reply_text(update,
            "⚠️ В имени получателя обнаружены запрещенные слова. "
            "Заявка автоматически закрыта. Пожалуйста, начните заново с корректным текстом."
        )
        return ConversationHandler.END
    
    context.user_data["to_name"] = name

    keyboard_list = [[InlineKeyboardButton(h, callback_data=h)] for h in HOLIDAYS.keys()]
    keyboard_list.append([InlineKeyboardButton("💬 Своё поздравление", callback_data="custom")])
    keyboard_list.append([InlineKeyboardButton("✏️ Исправить имя получателя", callback_data="edit_recipient_name")])
    keyboard_list.extend(keyboard_nav)
    
    await safe_reply_text(update,
        f"Имя получателя: {html.escape(name)}\n\n"
        "Выберите праздник или тип поздравления:",
        reply_markup=InlineKeyboardMarkup(keyboard_list)
    )
    return CONGRAT_HOLIDAY_CHOICE

async def edit_recipient_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Allows the user to edit the recipient's name."""
    query = update.callback_query
    if not query: return RECIPIENT_NAME_INPUT
    try: await query.answer() 
    except: pass
    
    keyboard = [
        [InlineKeyboardButton("✏️ Исправить имя отправителя", callback_data="edit_sender_name")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_edit_message_text(query,
        "Введите исправленное имя получателя (кого поздравляете):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return RECIPIENT_NAME_INPUT

async def handle_congrat_holiday_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the choice of holiday or custom congratulation."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    try: await query.answer() 
    except: pass
    
    # Handle navigation first
    if query.data == "back_to_start": return await start_command(update, context)
    if query.data == "edit_sender_name": return await edit_sender_name(update, context)
    if query.data == "edit_recipient_name": return await edit_recipient_name(update, context)
    
    selected_choice = query.data
    context.user_data["congrat_type"] = selected_choice

    keyboard_nav_holiday = [
        [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    if selected_choice == "custom":
        await safe_edit_message_text(query,
            f"Введите ваш текст поздравления (до {MAX_CONGRAT_TEXT_LENGTH} символов):",
            reply_markup=InlineKeyboardMarkup(keyboard_nav_holiday)
        )
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    elif selected_choice in HOLIDAYS:
        holiday_name = selected_choice
        holiday_date_short = HOLIDAYS[holiday_name]
        if not is_holiday_active(holiday_date_short):
            await safe_edit_message_text(query,
                f"❗️Праздник '{html.escape(holiday_name)}' сейчас не актуален. Обратитесь позже.\n\n"
                "Выберите другой праздник или вернитесь к началу.",
                reply_markup=InlineKeyboardMarkup(keyboard_nav_holiday)
            )
            return CONGRAT_HOLIDAY_CHOICE
        
        context.user_data["text"] = HOLIDAY_TEMPLATES[holiday_name]
        try:
            current_year = datetime.now().year
            holiday_date_obj = datetime.strptime(f"{current_year}-{holiday_date_short}", "%Y-%m-%d")
            context.user_data["publish_date"] = holiday_date_obj.strftime("%Y-%m-%d")
            return await complete_request(update, context)
        except ValueError as e:
            logger.error(f"Error parsing holiday date {holiday_date_short}: {e}")
            await safe_edit_message_text(query, "❌ Ошибка определения даты праздника. Попробуйте снова.", reply_markup=InlineKeyboardMarkup(keyboard_nav_holiday))
            return CONGRAT_HOLIDAY_CHOICE
    else:
        await safe_edit_message_text(query, "❌ Некорректный выбор праздника.", reply_markup=InlineKeyboardMarkup(keyboard_nav_holiday))
        return CONGRAT_HOLIDAY_CHOICE

async def back_to_holiday_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Navigates back to the holiday selection step."""
    query = update.callback_query
    if not query: return CONGRAT_HOLIDAY_CHOICE
    try: await query.answer() 
    except: pass
    
    keyboard_list = [[InlineKeyboardButton(h, callback_data=h)] for h in HOLIDAYS.keys()]
    keyboard_list.append([InlineKeyboardButton("💬 Своё поздравление", callback_data="custom")])
    keyboard_list.append([InlineKeyboardButton("✏️ Исправить имя получателя", callback_data="edit_recipient_name")])
    keyboard_list.append([InlineKeyboardButton("✏️ Исправить имя отправителя", callback_data="edit_sender_name")])
    keyboard_list.append([InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")])
    
    await safe_edit_message_text(query,
        "Выберите праздник или тип поздравления:",
        reply_markup=InlineKeyboardMarkup(keyboard_list)
    )
    return CONGRAT_HOLIDAY_CHOICE

async def process_custom_congrat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the custom congratulation text input."""
    if update.callback_query:
        if update.callback_query.data == "back_to_start":
            try: await update.callback_query.answer() 
            except: pass
            return await start_command(update, context)
        elif update.callback_query.data == "back_to_holiday_choice":
            try: await update.callback_query.answer() 
            except: pass
            return await back_to_holiday_choice(update, context)
    
    if not update.message or not update.message.text:
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    
    if update.message.text.startswith('/start'):
        return await start_command(update, context)
    
    user_text = update.message.text.strip()
    keyboard_nav_custom = [
        [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    if not user_text:
        await safe_reply_text(update, "Текст поздравления не может быть пустым. Пожалуйста, введите текст:", reply_markup=InlineKeyboardMarkup(keyboard_nav_custom))
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    if len(user_text) > MAX_CONGRAT_TEXT_LENGTH:
        await safe_reply_text(update, f"Сообщение слишком длинное. Максимум {MAX_CONGRAT_TEXT_LENGTH} символов. Пожалуйста, сократите текст:", reply_markup=InlineKeyboardMarkup(keyboard_nav_custom))
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    censored_text, contains_bad_words = censor_text(user_text)
    
    if contains_bad_words:
        await safe_reply_text(update,
            "⚠️ В вашем сообщении обнаружены запрещенные слова. "
            "Заявка автоматически закрыта. Пожалуйста, начните заново с корректным текстом."
        )
        return ConversationHandler.END
    
    context.user_data["original_text"] = user_text
    context.user_data["censored_text"] = censored_text
    context.user_data["text"] = censored_text # Use censored text by default
    
    if censored_text != user_text:
        keyboard = [
            [InlineKeyboardButton("✅ Отправить как есть", callback_data="accept_censor")],
            [InlineKeyboardButton("✏️ Исправить текст", callback_data="edit_censor")],
            *keyboard_nav_custom
        ]
        await safe_reply_text(update,
            f"⚠️ Наш фильтр отредактировал ваше сообщение:\n{html.escape(censored_text)}\n\nВыберите действие:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_CENSOR_APPROVAL
    
    # If text is clean, proceed to date input
    keyboard_nav_date = [
        [InlineKeyboardButton("🔙 Вернуться к вводу текста", callback_data="back_to_custom_message")],
        *keyboard_nav_custom
    ]
    await safe_reply_text(update, "📅 Укажите дату публикации (ДД-ММ-ГГГГ или 'сегодня'):", reply_markup=InlineKeyboardMarkup(keyboard_nav_date))
    return CONGRAT_DATE_INPUT

async def back_to_custom_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Navigates back to the custom message input step."""
    query = update.callback_query
    if not query: return CUSTOM_CONGRAT_MESSAGE_INPUT
    try: await query.answer() 
    except: pass
    
    keyboard = [
        [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_edit_message_text(query, f"Введите ваш текст поздравления (до {MAX_CONGRAT_TEXT_LENGTH} символов):", reply_markup=InlineKeyboardMarkup(keyboard))
    return CUSTOM_CONGRAT_MESSAGE_INPUT

async def process_congrat_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the publication date input for congratulations."""
    if update.callback_query:
        if update.callback_query.data == "back_to_start":
            try: await update.callback_query.answer() 
            except: pass
            return await start_command(update, context)
        elif update.callback_query.data == "back_to_holiday_choice":
            try: await update.callback_query.answer() 
            except: pass
            return await back_to_holiday_choice(update, context)
        elif update.callback_query.data == "back_to_custom_message":
            try: await update.callback_query.answer() 
            except: pass
            return await back_to_custom_message(update, context)
    
    if not update.message or not update.message.text:
        return CONGRAT_DATE_INPUT
    
    if update.message.text.startswith('/start'):
        return await start_command(update, context)
    
    date_str = update.message.text.strip().lower()
    keyboard_nav_date = [
        [InlineKeyboardButton("🔙 Вернуться к вводу текста", callback_data="back_to_custom_message")],
        [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    
    publish_date: Optional[datetime] = None
    if date_str == "сегодня":
        publish_date = datetime.now()
    else:
        try:
            publish_date = datetime.strptime(date_str, "%d-%m-%Y")
            # Allow publishing today, but not in the past
            if publish_date.date() < datetime.now().date():
                await safe_reply_text(update, "Нельзя указать прошедшую дату. Введите сегодняшнюю или будущую дату (ДД-ММ-ГГГГ):", reply_markup=InlineKeyboardMarkup(keyboard_nav_date))
                return CONGRAT_DATE_INPUT
        except ValueError:
            await safe_reply_text(update, "Неверный формат даты. Введите дату в формате ДД-ММ-ГГГГ или 'сегодня':", reply_markup=InlineKeyboardMarkup(keyboard_nav_date))
            return CONGRAT_DATE_INPUT
    
    if publish_date:
        context.user_data["publish_date"] = publish_date.strftime("%Y-%m-%d")
        return await complete_request(update, context)
    else:
        # Should not happen if logic is correct, but as a fallback
        await safe_reply_text(update, "Произошла ошибка при обработке даты. Попробуйте снова.", reply_markup=InlineKeyboardMarkup(keyboard_nav_date))
        return CONGRAT_DATE_INPUT

async def handle_announce_subtype_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the selection of an announcement subtype."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    try: await query.answer() 
    except: pass
    
    if query.data == "back_to_start": return await start_command(update, context)
    
    context.user_data["subtype"] = query.data
    subtype_key = query.data

    guidance = {
        "ride": f"Укажите маршрут, дату и время. Например: 'Волгоград, 15 сентября, 8:00, 3 места' (до {MAX_ANNOUNCE_NEWS_TEXT_LENGTH} символов)",
        "offer": f"Опишите что предлагаете. Правило: запрещена реклама. (до {MAX_ANNOUNCE_NEWS_TEXT_LENGTH} символов)",
        "lost": f"Опишите что потеряли/нашли и где. (до {MAX_ANNOUNCE_NEWS_TEXT_LENGTH} символов)"
    }
    
    keyboard = [
        [InlineKeyboardButton("🔙 Вернуться к выбору подтипа", callback_data="back_to_subtype")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_edit_message_text(query,
        guidance.get(subtype_key, f"Введите текст вашего объявления (до {MAX_ANNOUNCE_NEWS_TEXT_LENGTH} символов):"),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ANNOUNCE_TEXT_INPUT

async def back_to_subtype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Navigates back to the announcement subtype selection."""
    query = update.callback_query
    if not query: return ANNOUNCE_SUBTYPE_SELECTION
    try: await query.answer() 
    except: pass
    
    keyboard = [[InlineKeyboardButton(v, callback_data=k)] for k, v in ANNOUNCE_SUBTYPES.items()]
    keyboard.append([InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")])
    
    await safe_edit_message_text(query, "Выберите тип объявления:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ANNOUNCE_SUBTYPE_SELECTION

async def process_announce_news_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the text input for announcements or news."""
    if update.callback_query:
        if update.callback_query.data == "back_to_start":
            try: await update.callback_query.answer() 
            except: pass
            return await start_command(update, context)
        elif update.callback_query.data == "back_to_subtype": # Only relevant for announcements
            if context.user_data.get("type") == "announcement":
                try: await update.callback_query.answer() 
                except: pass
                return await back_to_subtype(update, context)
    
    if not update.message or not update.message.text:
        return ANNOUNCE_TEXT_INPUT
    
    if update.message.text.startswith('/start'):
        return await start_command(update, context)
    
    user_text = update.message.text.strip()
    request_type = context.user_data.get("type")
    max_len = MAX_ANNOUNCE_NEWS_TEXT_LENGTH
    
    # Navigation depends on type (announcement has subtype step, news doesn't)
    keyboard_nav_announce = [
        [InlineKeyboardButton("🔙 Вернуться к выбору подтипа", callback_data="back_to_subtype")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    keyboard_nav_news = [
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    keyboard_nav = keyboard_nav_announce if request_type == "announcement" else keyboard_nav_news

    if not user_text:
        await safe_reply_text(update, "Текст не может быть пустым. Пожалуйста, введите текст:", reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return ANNOUNCE_TEXT_INPUT
    if len(user_text) > max_len:
        await safe_reply_text(update, f"Сообщение слишком длинное. Максимум {max_len} символов. Пожалуйста, сократите текст:", reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return ANNOUNCE_TEXT_INPUT

    censored_text, contains_bad_words = censor_text(user_text)
    
    if contains_bad_words:
        await safe_reply_text(update,
            "⚠️ В вашем сообщении обнаружены запрещенные слова. "
            "Заявка автоматически закрыта. Пожалуйста, начните заново с корректным текстом."
        )
        return ConversationHandler.END
    
    context.user_data["original_text"] = user_text
    context.user_data["censored_text"] = censored_text
    context.user_data["text"] = censored_text # Use censored text by default
    
    if censored_text != user_text:
        keyboard = [
            [InlineKeyboardButton("✅ Отправить как есть", callback_data="accept_censor")],
            [InlineKeyboardButton("✏️ Исправить текст", callback_data="edit_censor")],
            *keyboard_nav
        ]
        await safe_reply_text(update,
            f"⚠️ Наш фильтр отредактировал ваше сообщение:\n{html.escape(censored_text)}\n\nВыберите действие:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_CENSOR_APPROVAL
    
    # If text is clean, complete the request (date is always today for these types)
    context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d") 
    return await complete_request(update, context)

async def handle_censor_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user's decision after text was censored."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    try: await query.answer() 
    except: pass
    
    choice = query.data
    current_type = context.user_data.get("type")

    # Handle navigation first
    if choice == "back_to_start": return await start_command(update, context)
    if choice == "back_to_holiday_choice" and current_type == "congrat": return await back_to_holiday_choice(update, context)
    if choice == "back_to_subtype" and current_type == "announcement": return await back_to_subtype(update, context)
    
    # Handle main choices
    if choice == "accept_censor":
        # Text is already set to censored version
        if current_type == "congrat" and context.user_data.get("congrat_type") == "custom":
            keyboard_nav_date = [
                [InlineKeyboardButton("🔙 Вернуться к вводу текста", callback_data="back_to_custom_message")],
                [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            await safe_edit_message_text(query, "Текст принят с изменениями фильтра. 📅 Укажите дату публикации (ДД-ММ-ГГГГ или 'сегодня'):", reply_markup=InlineKeyboardMarkup(keyboard_nav_date))
            return CONGRAT_DATE_INPUT
        else: # Announcements, news
            context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d")
            return await complete_request(update, context)
            
    elif choice == "edit_censor":
        if current_type == "congrat" and context.user_data.get("congrat_type") == "custom":
            keyboard_nav_custom = [
                [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            await safe_edit_message_text(query, f"Введите исправленный текст поздравления (до {MAX_CONGRAT_TEXT_LENGTH} символов):", reply_markup=InlineKeyboardMarkup(keyboard_nav_custom))
            return CUSTOM_CONGRAT_MESSAGE_INPUT
        else: # Announcements, news
            max_len = MAX_ANNOUNCE_NEWS_TEXT_LENGTH
            keyboard_nav_announce = [
                [InlineKeyboardButton("🔙 Вернуться к выбору подтипа", callback_data="back_to_subtype")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            keyboard_nav_news = [
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            keyboard_nav = keyboard_nav_announce if current_type == "announcement" else keyboard_nav_news
            await safe_edit_message_text(query, f"Введите исправленный текст (до {max_len} символов):", reply_markup=InlineKeyboardMarkup(keyboard_nav))
            return ANNOUNCE_TEXT_INPUT
    else:
        keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
        await safe_edit_message_text(query, "Некорректный выбор.", reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return WAIT_CENSOR_APPROVAL

async def complete_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finalizes the request, saves to DB, and notifies admin."""
    user_data = context.user_data
    user = update.effective_user
    if not user:
        logger.warning("complete_request: Could not get effective_user")
        await safe_reply_text(update, "❌ Произошла ошибка: не удалось идентифицировать пользователя.")
        return ConversationHandler.END

    try:
        # Final censorship check (should be redundant but safe)
        if "text" in user_data:
            final_text = user_data["text"]
            censored_text, contains_bad_words = censor_text(final_text)
            if contains_bad_words:
                logger.warning(f"Bad words detected during final check for user {user.id}. Text: {final_text}")
                await safe_reply_text(update,
                    "⚠️ В вашем сообщении обнаружены запрещенные слова. "
                    "Заявка автоматически закрыта. Пожалуйста, начните заново с корректным текстом."
                )
                return ConversationHandler.END
            user_data["text"] = censored_text # Ensure it's the censored version
        else:
             logger.error(f"'text' key missing from user_data during complete_request for user {user.id}")
             await safe_reply_text(update, "❌ Произошла внутренняя ошибка: отсутствует текст заявки.")
             return ConversationHandler.END

        data_to_save = {
            "user_id": user.id,
            "username": user.username,
            "type": user_data.get("type"),
            "subtype": user_data.get("subtype"),
            "from_name": user_data.get("from_name"),
            "to_name": user_data.get("to_name"),
            "text": user_data["text"],
            "publish_date": user_data.get("publish_date"),
            "congrat_type": user_data.get("congrat_type")
        }
        
        # Add application to DB
        app_id = add_application(data_to_save)
        
        if app_id is None:
            logger.error(f"Failed to save application to DB for user {user.id}")
            await safe_reply_text(update, "❌ Произошла ошибка при сохранении вашей заявки. Попробуйте позже.")
            return ConversationHandler.END
            
        # Send to admin for moderation
        admin_data = {**data_to_save, "id": app_id}
        await send_to_admin_for_moderation(context.bot, admin_data)
        
        await safe_reply_text(update, "✅ Ваша заявка отправлена на модерацию!")

    except Exception as e:
        logger.error(f"Error completing request for user {user.id}: {e}", exc_info=True)
        await safe_reply_text(update, "❌ Произошла непредвиденная ошибка при обработке вашей заявки. Попробуйте позже.")
            
    context.user_data.clear()
    return ConversationHandler.END

async def send_to_admin_for_moderation(bot: Bot, data: Dict[str, Any]):
    """Sends the application details to the admin chat for approval."""
    if not ADMIN_CHAT_ID:
        logger.error(f"Cannot send application #{data.get('id')} to admin: ADMIN_CHAT_ID not set.")
        return
        
    try:
        contact_info = f"@{data['username']}" if data.get("username") else f"<a href='tg://user?id={data['user_id']}'>Пользователь {data['user_id']}</a>"
        app_id = data.get('id', 'N/A')
        app_type = data.get('type', 'unknown')
        type_info = REQUEST_TYPES.get(app_type, {})
        type_name = type_info.get('name', app_type)
        
        message_parts = [
            f"<b>Новая заявка #{app_id}</b>",
            f"<b>Тип:</b> {type_name}"
        ]
        if app_type == "announcement" and data.get('subtype'):
            subtype_name = ANNOUNCE_SUBTYPES.get(data['subtype'], data['subtype'])
            message_parts.append(f"<b>Подтип:</b> {subtype_name}")
        
        if app_type == "congrat":
            message_parts.append(f"<b>От кого:</b> {html.escape(str(data.get('from_name', '—')))}")
            message_parts.append(f"<b>Кому:</b> {html.escape(str(data.get('to_name', '—')))}")
            congrat_type = data.get('congrat_type')
            if congrat_type and congrat_type != 'custom':
                message_parts.append(f"<b>Праздник:</b> {html.escape(congrat_type)}")
                message_parts.append(f"<b>Текст (шаблон):</b>\n{html.escape(str(data.get('text', '')))}")
            else:
                message_parts.append(f"<b>Текст (свой):</b>\n{html.escape(str(data.get('text', '')))}")
        else: # News, Announcements
            message_parts.append(f"<b>Текст:</b>\n{html.escape(str(data.get('text', '')))}")

        if data.get('publish_date'):
            message_parts.append(f"<b>Дата публикации:</b> {data['publish_date']}")
        message_parts.append(f"<b>Отправитель:</b> {contact_info}")
        
        if not IS_PUBLISHING_ENABLED:
             message_parts.append("\n<b>⚠️ Публикация в канал отключена (GROUP_ID не настроен).</b>")

        admin_text = "\n".join(message_parts)
        
        keyboard = [
            [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
             InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")]
        ]
        
        await safe_send_message(bot, ADMIN_CHAT_ID, admin_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        
    except Exception as e:
        logger.error(f"Error sending application #{data.get('id')} to admin: {e}", exc_info=True)

async def handle_admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin's approve/reject decision."""
    query = update.callback_query
    if not query or not query.data:
        return
    try: 
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering admin callback query {query.id}: {e}")

    try:
        action, app_id_str = query.data.split('_', 1)
        app_id = int(app_id_str)
    except (ValueError, IndexError):
        logger.error(f"Invalid admin callback_data format: {query.data}")
        await safe_edit_message_text(query, "❌ Ошибка: неверный формат данных.")
        return

    app_details = get_application_details(app_id)
    if not app_details:
        await safe_edit_message_text(query, f"❌ Заявка #{app_id} не найдена в базе.")
        return

    new_status = None
    notify_user_text = None
    admin_confirm_text = None

    if action == "approve":
        if app_details['status'] == 'approved' or app_details['status'] == 'published':
             admin_confirm_text = f"ℹ️ Заявка #{app_id} уже одобрена/опубликована."
        else:
            if update_application_status(app_id, 'approved'):
                new_status = 'approved'
                admin_confirm_text = f"✅ Заявка #{app_id} ({app_details['type']}) одобрена."
                logger.info(f"Application #{app_id} approved by admin.")
                # Attempt immediate publication if conditions met
                if IS_PUBLISHING_ENABLED and app_details['publish_date'] and app_details['published_at'] is None:
                    try:
                        publish_date_obj = datetime.strptime(app_details['publish_date'], "%Y-%m-%d").date()
                        if publish_date_obj <= datetime.now().date():
                            logger.info(f"Admin approved: Triggering immediate publication for #{app_id}")
                            # Fetch fresh details before publishing
                            fresh_details = get_application_details(app_id)
                            if fresh_details:
                                await publish_application_to_channel(context.bot, dict(fresh_details))
                            else:
                                logger.error(f"Could not fetch fresh details for #{app_id} before immediate publication.")
                    except ValueError:
                         logger.error(f"Invalid date format '{app_details['publish_date']}' for app #{app_id} during immediate publish check.")
                    except Exception as e_pub:
                         logger.error(f"Error during immediate publication check for #{app_id}: {e_pub}", exc_info=True)
            else:
                admin_confirm_text = f"❌ Ошибка БД при одобрении заявки #{app_id}."
                
    elif action == "reject":
        if app_details['status'] == 'rejected':
            admin_confirm_text = f"ℹ️ Заявка #{app_id} уже отклонена."
        else:
            if update_application_status(app_id, 'rejected'):
                new_status = 'rejected'
                admin_confirm_text = f"❌ Заявка #{app_id} ({app_details['type']}) отклонена."
                notify_user_text = f"❌ Ваша заявка #{app_id} была отклонена модератором."
                logger.info(f"Application #{app_id} rejected by admin.")
            else:
                 admin_confirm_text = f"❌ Ошибка БД при отклонении заявки #{app_id}."
    else:
        admin_confirm_text = f"❌ Неизвестное действие '{action}' для заявки #{app_id}."

    # Update admin message and notify user if needed
    if admin_confirm_text:
        await safe_edit_message_text(query, admin_confirm_text)
    if notify_user_text and app_details['user_id']:
        await safe_send_message(context.bot, app_details['user_id'], notify_user_text)

async def publish_application_to_channel(bot: Bot, app_data: Dict[str, Any]):
    """Publishes an approved application to the designated channel."""
    app_id = app_data.get('id')
    if not IS_PUBLISHING_ENABLED or not GROUP_ID:
        logger.warning(f"Skipping publication of #{app_id}: Publishing is disabled.")
        return
        
    if app_data.get("status") != 'approved':
        logger.warning(f"Skipping publication of #{app_id}: Status is '{app_data.get('status')}' not 'approved'.")
        return
        
    if app_data.get("published_at") is not None:
        logger.info(f"Skipping publication of #{app_id}: Already published at {app_data['published_at']}.")
        # Ensure status is correct if published_at is set but status isn't 'published'
        if app_data.get("status") != 'published':
             update_application_status(app_id, 'published')
        return

    try:
        logger.info(f"Attempting to publish application #{app_id} to channel {GROUP_ID}")
        
        final_text_parts = []
        base_text = html.escape(str(app_data.get('text', '')))
        app_type = app_data.get('type')

        if app_type == "congrat":
            type_info = REQUEST_TYPES.get(app_type, {})
            icon = type_info.get('icon', '🎉')
            type_name = type_info.get('name', 'Поздравление')
            from_name = html.escape(str(app_data.get('from_name', 'Кто-то')))
            to_name = html.escape(str(app_data.get('to_name', 'кого-то')))
            congrat_type_display = html.escape(str(app_data.get('congrat_type', '')))
            
            title = f"<b>{icon} {type_name}</b>"
            if congrat_type_display and congrat_type_display != 'custom':
                title += f" ({congrat_type_display})"
            final_text_parts.append(title)
            final_text_parts.append(base_text)
            final_text_parts.append(f"\n<i>{from_name} поздравляет {to_name}.</i>")

        elif app_type == "announcement":
            type_info = REQUEST_TYPES.get(app_type, {})
            icon = type_info.get('icon', '📢')
            type_name = type_info.get('name', 'Объявление')
            subtype_key = app_data.get('subtype', '')
            subtype_display = ANNOUNCE_SUBTYPES.get(subtype_key, subtype_key)
            title = f"<b>{icon} {type_name} ({subtype_display})</b>"
            final_text_parts.append(title)
            final_text_parts.append(base_text)
            if app_data.get('username'):
                final_text_parts.append(f"\nОбращаться: @{app_data['username']}")
            else:
                final_text_parts.append(f"\nОбращаться: <a href='tg://user?id={app_data['user_id']}'>Написать автору</a>")
        
        elif app_type == "news":
            type_info = REQUEST_TYPES.get(app_type, {})
            icon = type_info.get('icon', '🗞️')
            type_name = type_info.get('name', 'Новость')
            title = f"<b>{icon} {type_name}</b>"
            final_text_parts.append(title)
            final_text_parts.append(base_text)
            if app_data.get('username'):
                 final_text_parts.append(f"\n<i>Новость от: @{app_data['username']}</i>")
            else:
                final_text_parts.append(f"\n<i>Новость от: <a href='tg://user?id={app_data['user_id']}'>жителя</a></i>")
        else:
            logger.warning(f"Unknown application type '{app_type}' for publication #{app_id}")
            return

        final_text_parts.append("\n\n#Николаевск")
        message_to_publish = "\n".join(final_text_parts)
        
        # Send to channel
        await safe_send_message(bot, GROUP_ID, message_to_publish, parse_mode="HTML")
        
        # Mark as published in DB
        if mark_application_as_published(app_id):
            logger.info(f"Application #{app_id} successfully published and marked in DB.")
            # Notify user
            if app_data.get('user_id'):
                await safe_send_message(bot, app_data['user_id'], f"✅ Ваша заявка #{app_id} опубликована в канале {CHANNEL_NAME}!")
        else:
             logger.error(f"Application #{app_id} published to channel, but FAILED to mark as published in DB!")

    except Exception as e:
        logger.error(f"Critical error publishing application #{app_id} to channel: {e}", exc_info=True)

async def scheduled_publication_check(application: Application):
    """Periodically checks for and publishes approved applications."""
    if not IS_PUBLISHING_ENABLED:
        logger.info("Scheduler: Publishing disabled, skipping check.")
        return
        
    logger.info("Scheduler: Checking for applications to publish...")
    try:
        approved_apps = get_approved_unpublished_applications()
        if not approved_apps:
            logger.info("Scheduler: No approved applications pending publication.")
            return

        published_count = 0
        for app_row in approved_apps:
            app_data = dict(app_row)
            app_id = app_data.get('id')
            
            # Double check status and published_at just in case
            if app_data.get('status') != 'approved' or app_data.get('published_at') is not None:
                continue 

            publish_date_str = app_data.get('publish_date')
            if not publish_date_str:
                logger.warning(f"Scheduler: Application #{app_id} has status 'approved' but no publish_date. Skipping.")
                continue
            
            try:
                publish_date_obj = datetime.strptime(publish_date_str, "%Y-%m-%d").date()
                if publish_date_obj <= datetime.now().date():
                    logger.info(f"Scheduler: Publishing application #{app_id} (Date: {publish_date_str})")
                    await publish_application_to_channel(application.bot, app_data)
                    published_count += 1
                # else: Date is in the future, do nothing
            except ValueError:
                logger.error(f"Scheduler: Invalid date format '{publish_date_str}' for app #{app_id}. Skipping.")
            except Exception as e_inner:
                 logger.error(f"Scheduler: Error processing application #{app_id} in loop: {e_inner}", exc_info=True)
        
        if published_count > 0:
             logger.info(f"Scheduler: Published {published_count} applications.")
        else:
             logger.info("Scheduler: No applications ready for publication based on date.")

    except Exception as e_scheduler:
        logger.error(f"Scheduler: Global error during publication check: {e_scheduler}", exc_info=True)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current conversation."""
    context.user_data.clear()
    await safe_reply_text(update, "❌ Действие отменено.")
    return ConversationHandler.END

# --- Main Bot Setup ---
def main():
    if not TELEGRAM_TOKEN:
        logger.critical("TELEGRAM_TOKEN not found in environment variables! Bot cannot start.")
        return
    if not ADMIN_CHAT_ID:
        logger.critical("ADMIN_CHAT_ID not found or invalid! Admin functions will fail.")
        # Decide if bot should run without admin features or exit
        # return 
            
    init_db()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Define handlers for navigation/editing
    navigation_handlers = [
        CallbackQueryHandler(back_to_holiday_choice, pattern=r"^back_to_holiday_choice$"),
        CallbackQueryHandler(back_to_custom_message, pattern=r"^back_to_custom_message$"),
        CallbackQueryHandler(back_to_subtype, pattern=r"^back_to_subtype$"),
        CallbackQueryHandler(edit_sender_name, pattern=r"^edit_sender_name$"),
        CallbackQueryHandler(edit_recipient_name, pattern=r"^edit_recipient_name$"),
        # back_to_start is handled within each state that needs it
    ]

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            TYPE_SELECTION: [
                CallbackQueryHandler(handle_type_selection, pattern=f"^({'|'.join(REQUEST_TYPES.keys())})$"),
                CallbackQueryHandler(start_command, pattern=r"^back_to_start$") # Allow restart here
            ],
            SENDER_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_sender_name),
                CallbackQueryHandler(start_command, pattern=r"^back_to_start$")
            ],
            RECIPIENT_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_recipient_name),
                CallbackQueryHandler(edit_sender_name, pattern=r"^edit_sender_name$"),
                CallbackQueryHandler(start_command, pattern=r"^back_to_start$")
            ],
            CONGRAT_HOLIDAY_CHOICE: [
                CallbackQueryHandler(handle_congrat_holiday_choice, pattern=f"^({'|'.join(list(HOLIDAYS.keys()) + ['custom', 'edit_sender_name', 'edit_recipient_name', 'back_to_start'])})$")
            ],
            CUSTOM_CONGRAT_MESSAGE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_custom_congrat_message),
                CallbackQueryHandler(back_to_holiday_choice, pattern=r"^back_to_holiday_choice$"),
                CallbackQueryHandler(start_command, pattern=r"^back_to_start$")
            ],
            CONGRAT_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_congrat_date),
                CallbackQueryHandler(back_to_custom_message, pattern=r"^back_to_custom_message$"),
                CallbackQueryHandler(back_to_holiday_choice, pattern=r"^back_to_holiday_choice$"),
                CallbackQueryHandler(start_command, pattern=r"^back_to_start$")
            ],
            ANNOUNCE_SUBTYPE_SELECTION: [
                CallbackQueryHandler(handle_announce_subtype_selection, pattern=f"^({'|'.join(list(ANNOUNCE_SUBTYPES.keys()) + ['back_to_start'])})$")
            ],
            ANNOUNCE_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_announce_news_text),
                CallbackQueryHandler(back_to_subtype, pattern=r"^back_to_subtype$"), # Only if type is announcement
                CallbackQueryHandler(start_command, pattern=r"^back_to_start$")
            ],
            WAIT_CENSOR_APPROVAL: [
                CallbackQueryHandler(handle_censor_choice, pattern=r"^(accept_censor|edit_censor|back_to_holiday_choice|back_to_subtype|back_to_start)$")
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_command), CommandHandler('start', start_command)],
        per_message=False # Allow navigation buttons within the same state message
    )

    application.add_handler(conv_handler)
    # Admin decision handler (outside conversation)
    application.add_handler(CallbackQueryHandler(handle_admin_decision, pattern=r"^(approve|reject)_\d+$"))

    # Setup and start the scheduler only if publishing is potentially needed
    if IS_PUBLISHING_ENABLED:
        try:
            scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
            # Check every 5 minutes
            scheduler.add_job(scheduled_publication_check, args=[application], trigger="interval", minutes=5, id="publication_check")
            scheduler.start()
            logger.info("APScheduler started for publication checks.")
        except Exception as e:
            logger.error(f"Failed to start APScheduler: {e}", exc_info=True)
    else:
        logger.info("APScheduler not started as publishing is disabled.")

    logger.info("Bot starting polling...")
    try:
        application.run_polling()
    except Exception as e:
        logger.critical(f"Bot polling failed critically: {e}", exc_info=True)
    finally:
        # Cleanup scheduler if it was started
        if IS_PUBLISHING_ENABLED and 'scheduler' in locals() and scheduler.running:
            scheduler.shutdown()
            logger.info("APScheduler shut down.")

if __name__ == "__main__":
    main()
