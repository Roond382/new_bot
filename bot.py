import os
import sys
import logging
from datetime import datetime, timedelta
import signal
import re
import traceback
from typing import Dict, Any, Optional, Tuple
import asyncio
import asyncpg # Изменено: импорт asyncpg
import pytz
from dotenv import load_dotenv
from telegram import __version__ as ptb_ver, Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Глобальная переменная для отслеживания состояния
BOT_STATE = {
    'running': False,
    'start_time': None,
    'last_activity': None
}

# Константы
# DB_FILE = 'db.sqlite' # Удалено: не используется для PostgreSQL
# BAD_WORDS_FILE = 'bad_words.txt' # Удалено: bad_words будут встроены
DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука", "номер", "телефон", "звоните", "пишите", "контакт", "минет", "минетчик", "минетчица", "мокрощелка", "мокрощёлка", "манда", "мандавошка", "мандавошки", "мандей", "мандень", "мандеть", "мандища", "мандой", "манду", "мандюк", "мандюга", "мудоеб", "мудозвон", "мудоклюй", "наебать", "наебет", "наебнуть", "наебывать", "обосрать", "обосцать", "обосцаться", "обдристаться", "объебос", "обьебать", "обьебос", "отпиздить", "отпиздячить", "отпороть", "отъебись", "остоебенить", "остопиздеть", "охуеть", "охуенно", "охуевать", "охуевающий", "паскуда", "падла", "паскудник", "нахер", "нахрен", "нахуй", "нехера", "нехрен", "нехуй", "никуя", "нихуя", "нихера", "непизда", "нипизда", "нипизду", "напиздел", "напиздили", "наебнулся", "наебался", "пердеж", "пердение", "пердеть", "пердильник", "пердун", "пердунец", "пердунья", "пердуха", "пердульки", "перднуть", "пёрнуть", "пернуть", "пердят", "пиздят", "пиздить", "пиздишь", "пиздится", "заебал", "заебло", "пидар", "пидарас", "пидор", "пидорас", "пидик", "педрик", "педераст", "пидрас", "пидры", "пидрилы", "пидрило", "педрило", "блядина", "блядь", "блядство", "блядствующие", "блядствует", "шалава", "шалавость", "шалавиться"]
MAX_NAME_LENGTH = 50
MAX_TEXT_LENGTH = 4000
MAX_CONGRAT_TEXT_LENGTH = 500
MAX_ANNOUNCE_NEWS_TEXT_LENGTH = 300
CHANNEL_NAME = "Небольшой Мир: Николаевск"

# Настройки из .env
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID")) if os.getenv("CHANNEL_ID") else None
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID")) if os.getenv("ADMIN_CHAT_ID") else None
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", 'Europe/Moscow')) # Сделано настраиваемым
WORKING_HOURS = (0, 23)  # 00:00-23:59 (круглосуточно)
WORK_ON_WEEKENDS = True

# Переменные окружения для PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")

# Проверка обязательных переменных окружения
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в переменных окружения")
if not CHANNEL_ID:
    logging.warning("CHANNEL_ID не задан в переменных окружения. Публикация в канал невозможна")
if not ADMIN_CHAT_ID:
    logging.warning("ADMIN_CHAT_ID не задан в переменных окружения. Уведомления администратора не будут отправляться")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан в переменных окружения. Невозможно подключиться к БД.")

# Типы запросов
REQUEST_TYPES = {
    "congrat": {"name": "Поздравление", "icon": "🎉"},
    "announcement": {"name": "Объявление", "icon": "📢"},
    "news": {"name": "Новость от жителя", "icon": "🗞️"}
}

# Подтипы объявлений
ANNOUNCE_SUBTYPES = {
    "ride": "🚗 Попутка",
    "offer": "💡 Предложение",
    "lost": "🔍 Потеряли/Нашли"
}

# Праздники
HOLIDAYS = {
    "🎄 Новый год": "01-01",
    "🪖 23 Февраля": "02-23",
    "💐 8 Марта": "03-08",
    "🏅 9 Мая": "05-09",
    "🇷🇺 12 Июня": "06-12",
    "🤝 4 Ноября": "11-04"
}

HOLIDAY_TEMPLATES = {
    "🎄 Новый год": "С Новым годом!\nПусть исполняются все ваши желания!",
    "🪖 23 Февраля": "С Днём защитника Отечества!\nМужества, отваги и мирного неба над головой!",
    "💐 8 Марта": "С 8 Марта!\nКрасоты, счастья и весеннего настроения!",
    "🏅 9 Мая": "С Днём Победы!\nВечная память героям!",
    "🇷🇺 12 Июня": "С Днём России!\nМира, благополучия и процветания нашей стране!",
    "🤝 4 Ноября": "С Днём народного единства!\nСогласия, мира и добра!"
}

# Состояния диалога
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

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        # logging.FileHandler('bot.log'), # Удалено: логи в stdout
        logging.StreamHandler(sys.stdout) # Изменено: логи в stdout
    ]
)
logger = logging.getLogger(__name__)

# Пул соединений с БД
db_pool = None

async def get_db_connection():
    """Получает соединение из пула."""
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
    return await db_pool.acquire()

async def release_db_connection(conn):
    """Возвращает соединение в пул."""
    global db_pool
    if db_pool:
        await db_pool.release(conn)

async def init_db():
    """Инициализирует таблицы базы данных с индексами для PostgreSQL."""
    conn = None
    try:
        conn = await get_db_connection()
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                type TEXT NOT NULL,
                subtype TEXT,
                from_name TEXT,
                to_name TEXT,
                text TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                publish_date DATE,
                published_at TIMESTAMP WITH TIME ZONE,
                congrat_type TEXT
            );
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_approved_unpublished
            ON applications(status, published_at)
            WHERE status = 'approved' AND published_at IS NULL;
        """)
        logger.info("База данных PostgreSQL успешно инициализирована.")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД PostgreSQL: {e}", exc_info=True)
        raise
    finally:
        if conn:
            await release_db_connection(conn)

async def add_application(data: Dict[str, Any]) -> Optional[int]:
    """Добавляет новую заявку в базу данных."""
    conn = None
    try:
        conn = await get_db_connection()
        app_id = await conn.fetchval("""
            INSERT INTO applications
            (user_id, username, type, subtype, from_name, to_name, text, publish_date, congrat_type)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id;
        """,
            data['user_id'],
            data.get('username'),
            data['type'],
            data.get('subtype'),
            data.get('from_name'),
            data.get('to_name'),
            data['text'],
            data.get('publish_date'),
            data.get('congrat_type')
        )
        return app_id
    except Exception as e:
        logger.error(f"Ошибка добавления заявки: {e}\nДанные: {data}", exc_info=True)
        return None
    finally:
        if conn:
            await release_db_connection(conn)

async def get_application_details(app_id: int) -> Optional[asyncpg.Record]:
    """Получает детали заявки по ID."""
    conn = None
    try:
        conn = await get_db_connection()
        return await conn.fetchrow("SELECT * FROM applications WHERE id = $1", app_id)
    except Exception as e:
        logger.error(f"Ошибка получения заявки #{app_id}: {e}", exc_info=True)
        return None
    finally:
        if conn:
            await release_db_connection(conn)

async def get_approved_unpublished_applications() -> list:
    """Получает одобренные, но не опубликованные заявки."""
    conn = None
    try:
        conn = await get_db_connection()
        return await conn.fetch("""
            SELECT id, user_id, username, type, subtype, from_name, to_name, text, publish_date, congrat_type
            FROM applications
            WHERE status = 'approved' AND published_at IS NULL
        """)
    except Exception as e:
        logger.error(f"Ошибка получения заявок: {e}", exc_info=True)
        return []
    finally:
        if conn:
            await release_db_connection(conn)

async def update_application_status(app_id: int, status: str) -> bool:
    """Обновляет статус заявки."""
    conn = None
    try:
        conn = await get_db_connection()
        await conn.execute("UPDATE applications SET status = $1 WHERE id = $2", status, app_id)
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления статуса заявки #{app_id}: {e}", exc_info=True)
        return False
    finally:
        if conn:
            await release_db_connection(conn)

async def mark_application_as_published(app_id: int) -> bool:
    """Помечает заявку как опубликованную."""
    conn = None
    try:
        conn = await get_db_connection()
        await conn.execute("""
            UPDATE applications
            SET published_at = CURRENT_TIMESTAMP, status = 'published'
            WHERE id = $1
        """, app_id)
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации заявки #{app_id}: {e}", exc_info=True)
        return False
    finally:
        if conn:
            await release_db_connection(conn)

# --- Валидация и цензура ---
def validate_name(name: str) -> bool:
    """Проверяет валидность имени."""
    if not name or not name.strip():
        return False

    name = name.strip()
    allowed_chars = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ -")
    return (2 <= len(name) <= MAX_NAME_LENGTH and
            all(c in allowed_chars for c in name) and
            not name.startswith('-') and
            not name.endswith('-') and
            '--' not in name)

def is_holiday_active(holiday_date_str: str) -> bool:
    """Проверяет, активен ли праздник (+/-5 дней от даты)."""
    try:
        current_year = datetime.now().year
        holiday_date = datetime.strptime(f"{current_year}-{holiday_date_str}", "%Y-%m-%d").date()
        today = datetime.now().date()
        start = holiday_date - timedelta(days=5)
        end = holiday_date + timedelta(days=5)
        return start <= today <= end
    except Exception as e:
        logger.error(f"Ошибка проверки праздника: {e}", exc_info=True)
        return False

async def load_bad_words() -> list:
    """Загружает список запрещенных слов из встроенной константы."""
    return DEFAULT_BAD_WORDS # Изменено: теперь из константы

async def censor_text(text: str) -> Tuple[str, bool]:
    """Цензурирует текст и возвращает результат."""
    bad_words = await load_bad_words()
    censored = text
    has_bad = False

    for word in bad_words:
        try:
            if re.search(re.escape(word), censored, re.IGNORECASE):
                has_bad = True
                censored = re.sub(re.escape(word), '***', censored, flags=re.IGNORECASE)
        except re.error as e:
            logger.error(f"Ошибка цензуры слова '{word}': {e}\nТекст: {text[:100]}...", exc_info=True)

    # Цензура контактной информации (уже включена в DEFAULT_BAD_WORDS)
    # try:
    #     censored = re.sub(
    #         r'(звоните|пишите|телефон|номер|тел\.?|т\.)[:;\s]*([\+\d\(\).\s-]{7,})',
    #         'Контактная информация скрыта (пишите в ЛС)',
    #         censored,
    #         flags=re.IGNORECASE
    #     )
    # except re.error as e:
    #     logger.error(f"Ошибка цензуры контактов: {e}", exc_info=True)

    return censored, has_bad
    # --- Вспомогательные функции ---
async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Отправляет сообщение с подробной обработкой ошибок."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except TelegramError as e:
        logger.warning(f"Ошибка отправки сообщения в {chat_id}: {e}\nТекст: {text[:100]}...")
    except Exception as e:
        logger.error(f"Неожиданная ошибка отправки в {chat_id}: {e}\nТекст: {text[:100]}...", exc_info=True)
    return False

async def safe_edit_message_text(query: Update.callback_query, text: str, **kwargs):
    """Редактирует сообщение с обработкой ошибок."""
    if not query or not query.message:
        return
    try:
        await query.edit_message_text(text=text, **kwargs)
    except TelegramError as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Ошибка редактирования сообщения: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка редактирования: {e}", exc_info=True)

async def safe_reply_text(update: Update, text: str, **kwargs):
    """Отвечает на сообщение с обработкой ошибок."""
    if update.callback_query:
        await safe_edit_message_text(update.callback_query, text, **kwargs)
    elif update.message:
        try:
            await update.message.reply_text(text=text, **kwargs)
        except TelegramError as e:
            logger.warning(f"Ошибка ответа на сообщение: {e}")
        except Exception as e:
            logger.error(f"Неожиданная ошибка ответа: {e}", exc_info=True)

async def send_bot_status(bot: Bot, status: str):
    """Отправляет статус бота администратору"""
    if ADMIN_CHAT_ID and bot:
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🤖 Статус бота: {status}\n"
                     f"• Время: {datetime.now(TIMEZONE).strftime('%H:%M %d.%m.%Y')}\n"
                     f"• Рабочее время: {'Да' if is_working_hours() else 'Нет'}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки статуса: {e}")

async def publish_to_channel(app_id: int, bot: Bot) -> bool:
    """Публикует заявку в канал с обработкой ошибок."""
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID не задан. Публикация невозможна.")
        return False

    app_details = await get_application_details(app_id) # Изменено: await
    if not app_details:
        logger.error(f"Заявка #{app_id} не найдена.")
        return False

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=app_details['text']
        )
        await mark_application_as_published(app_id) # Изменено: await
        logger.info(f"Заявка #{app_id} опубликована в канале {CHANNEL_ID}")
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации заявки #{app_id}: {str(e)}")
        return False

async def check_pending_applications(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет и публикует одобренные заявки."""
    applications = await get_approved_unpublished_applications() # Изменено: await
    for app in applications:
        if app['type'] in ['news', 'announcement'] or (
            app['publish_date'] and
            datetime.strptime(str(app['publish_date']), "%Y-%m-%d").date() <= datetime.now().date()
        ):
            await publish_to_channel(app['id'], context.bot)
            await asyncio.sleep(1)

async def check_shutdown_time(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет, не вышло ли время работы бота (23:00) и корректно останавливает его"""
    if not is_working_hours():
        logger.info("Рабочее время закончилось. Останавливаем бота.")
        await send_bot_status(context.bot, "Остановка (рабочее время закончилось)") # Изменено: context.bot
        
        try:
            await context.application.stop()
            # os._exit(0) # Удалено: некорректное завершение
        except Exception as e:
            logger.error(f"Ошибка при остановке бота: {e}")
            # os._exit(1) # Удалено: некорректное завершение

def check_environment():
    """Проверяет наличие всех необходимых файлов и переменных"""
    # required_files = [DB_FILE, BAD_WORDS_FILE] # Изменено: не нужны локальные файлы
    # missing_files = [f for f in required_files if not os.path.exists(f)]

    # if missing_files:
    #     logger.error(f"Отсутствуют необходимые файлы: {', '.join(missing_files)}")
    #     sys.exit(1)

    if not TOKEN or not CHANNEL_ID or not ADMIN_CHAT_ID or not DATABASE_URL:
        logger.error("Отсутствуют необходимые переменные окружения. Проверьте .env файл или настройки Render.")
        sys.exit(1)

# --- Обработчики команд и сообщений ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог и предлагает выбрать тип заявки."""
    user = update.effective_user
    logger.info(f"Пользователь {user.full_name} ({user.id}) начал диалог.")
    await safe_reply_text(
        update,
        f"Привет, {user.full_name}! Я бот для приёма заявок в канал \"{CHANNEL_NAME}\".\n\n"
        "Выберите тип заявки:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{REQUEST_TYPES['congrat']['icon']} {REQUEST_TYPES['congrat']['name']}", callback_data="type_congrat")],
            [InlineKeyboardButton(f"{REQUEST_TYPES['announcement']['icon']} {REQUEST_TYPES['announcement']['name']}", callback_data="type_announcement")],
            [InlineKeyboardButton(f"{REQUEST_TYPES['news']['icon']} {REQUEST_TYPES['news']['name']}", callback_data="type_news")]
        ])
    )
    return TYPE_SELECTION

async def select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор типа заявки."""
    query = update.callback_query
    await query.answer()
    context.user_data['request_type'] = query.data.replace("type_", "")
    logger.info(f"Выбран тип заявки: {context.user_data['request_type']}")

    if context.user_data['request_type'] == "congrat":
        keyboard = [
            [InlineKeyboardButton(name, callback_data=f"holiday_{date}")] for name, date in HOLIDAYS.items()
        ]
        keyboard.append([InlineKeyboardButton("Свой текст поздравления", callback_data="custom_congrat_text")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")])

        await safe_edit_message_text(
            query,
            "Вы выбрали \"Поздравление\". Выберите праздник или введите свой текст:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CONGRAT_HOLIDAY_CHOICE
    elif context.user_data['request_type'] == "announcement":
        keyboard = [
            [InlineKeyboardButton(name, callback_data=f"subtype_{key}")] for key, name in ANNOUNCE_SUBTYPES.items()
        ]
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")])
        await safe_edit_message_text(
            query,
            "Вы выбрали \"Объявление\". Выберите подтип объявления:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ANNOUNCE_SUBTYPE_SELECTION
    elif context.user_data['request_type'] == "news":
        context.user_data['subtype'] = None # Для новостей подтип не нужен
        await safe_edit_message_text(
            query,
            "Вы выбрали \"Новость от жителя\".\n\n"
            "Введите текст новости (до 300 символов)."
        )
        return ANNOUNCE_TEXT_INPUT

async def select_congrat_holiday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор праздника для поздравления."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "custom_congrat_text":
        context.user_data['congrat_type'] = "custom"
        await safe_edit_message_text(
            query,
            "Введите свой текст поздравления (до 500 символов)."
        )
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    elif data.startswith("holiday_"):
        holiday_date_str = data.replace("holiday_", "")
        holiday_name = next((name for name, date in HOLIDAYS.items() if date == holiday_date_str), "")
        context.user_data['congrat_type'] = holiday_name

        if is_holiday_active(holiday_date_str):
            context.user_data['text'] = HOLIDAY_TEMPLATES.get(holiday_name, "")
            await safe_edit_message_text(
                query,
                f"Вы выбрали {holiday_name}. Текст поздравления будет сгенерирован автоматически.\n\n"
                "Введите имя отправителя (например, \"Семья Ивановых\")."
            )
            return SENDER_NAME_INPUT
        else:
            await safe_edit_message_text(
                query,
                f"Праздник {holiday_name} неактивен для публикации. Выберите другой праздник или введите свой текст.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(name, callback_data=f"holiday_{date}")] for name, date in HOLIDAYS.items()
                ] + [[InlineKeyboardButton("Свой текст поздравления", callback_data="custom_congrat_text")],
                     [InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")]])
            )
            return CONGRAT_HOLIDAY_CHOICE
    return ConversationHandler.END # Should not happen

async def process_custom_congrat_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает введенный пользователем текст поздравления."""
    text = update.message.text
    if not text or not text.strip():
        await safe_reply_text(update, "Пожалуйста, введите текст поздравления.")
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    if len(text) > MAX_CONGRAT_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст поздравления слишком длинный. Максимум {MAX_CONGRAT_TEXT_LENGTH} символов.")
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    context.user_data['text'] = text
    await safe_reply_text(
        update,
        "Введите имя отправителя (например, \"Семья Ивановых\")."
    )
    return SENDER_NAME_INPUT

async def process_sender_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает имя отправителя."""
    sender_name = update.message.text
    if not validate_name(sender_name):
        await safe_reply_text(update, f"Некорректное имя отправителя. Используйте только буквы, пробелы и дефисы (2-{MAX_NAME_LENGTH} символов).")
        return SENDER_NAME_INPUT

    context.user_data['from_name'] = sender_name
    await safe_reply_text(
        update,
        "Введите имя получателя (например, \"Ивану Ивановичу\")."
    )
    return RECIPIENT_NAME_INPUT

async def process_recipient_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает имя получателя и переходит к выбору даты."""
    recipient_name = update.message.text
    if not validate_name(recipient_name):
        await safe_reply_text(update, f"Некорректное имя получателя. Используйте только буквы, пробелы и дефисы (2-{MAX_NAME_LENGTH} символов).")
        return RECIPIENT_NAME_INPUT

    context.user_data['to_name'] = recipient_name

    # Формируем текст поздравления, если он не был задан (для праздников)
    if 'text' not in context.user_data or not context.user_data['text']:
        holiday_name = context.user_data.get('congrat_type', '')
        template = HOLIDAY_TEMPLATES.get(holiday_name, "")
        if template:
            context.user_data['text'] = f"{template}\n\n"
        else:
            context.user_data['text'] = ""

    # Добавляем имена отправителя и получателя к тексту
    final_text = context.user_data['text']
    if context.user_data.get('from_name'):
        final_text += f"\nОт: {context.user_data['from_name']}"
    if context.user_data.get('to_name'):
        final_text += f"\nДля: {context.user_data['to_name']}"

    context.user_data['text'] = final_text

    await safe_reply_text(
        update,
        "Введите дату публикации поздравления в формате ДД.ММ.ГГГГ (например, 01.01.2024)."
    )
    return CONGRAT_DATE_INPUT

async def process_congrat_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает дату публикации поздравления."""
    date_str = update.message.text
    try:
        publish_date = datetime.strptime(date_str, "%d.%m.%Y").date()
        today = datetime.now().date()
        if publish_date < today:
            await safe_reply_text(update, "Дата публикации не может быть в прошлом. Пожалуйста, введите корректную дату.")
            return CONGRAT_DATE_INPUT
        context.user_data['publish_date'] = publish_date.strftime("%Y-%m-%d")
    except ValueError:
        await safe_reply_text(update, "Неверный формат даты. Пожалуйста, введите дату в формате ДД.ММ.ГГГГ.")
        return CONGRAT_DATE_INPUT

    # Проверка цензуры и подтверждение
    original_text = context.user_data['text']
    censored_text, has_bad_words = await censor_text(original_text)
    context.user_data['censored_text'] = censored_text
    context.user_data['has_bad_words'] = has_bad_words

    if has_bad_words:
        keyboard = [
            [InlineKeyboardButton("✅ Отправить на модерацию (с цензурой)", callback_data="approve_censor")],
            [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_text")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")]
        ]
        await safe_reply_text(
            update,
            f"Ваш текст содержит нецензурные выражения или контактную информацию. Он будет опубликован в следующем виде:\n\n"
            f"```\n{censored_text}\n```\n\n"
            "Вы можете отправить его на модерацию с цензурой или отредактировать.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_CENSOR_APPROVAL
    else:
        return await complete_request(update, context)

async def select_announce_subtype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор подтипа объявления."""
    query = update.callback_query
    await query.answer()
    context.user_data['subtype'] = query.data.replace("subtype_", "")
    logger.info(f"Выбран подтип объявления: {context.user_data['subtype']}")

    await safe_edit_message_text(
        query,
        f"Вы выбрали \"{ANNOUNCE_SUBTYPES[context.user_data['subtype']]}\".\n\n"
        "Введите текст объявления (до 500 символов)."
    )
    return ANNOUNCE_TEXT_INPUT

async def process_announce_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает введенный пользователем текст объявления/новости."""
    text = update.message.text
    if not text or not text.strip():
        await safe_reply_text(update, "Пожалуйста, введите текст.")
        return ANNOUNCE_TEXT_INPUT

    max_len = MAX_ANNOUNCE_NEWS_TEXT_LENGTH if context.user_data['request_type'] == 'news' else MAX_CONGRAT_TEXT_LENGTH
    if len(text) > max_len:
        await safe_reply_text(update, f"Текст слишком длинный. Максимум {max_len} символов.")
        return ANNOUNCE_TEXT_INPUT

    context.user_data['text'] = text

    # Проверка цензуры и подтверждение
    original_text = context.user_data['text']
    censored_text, has_bad_words = await censor_text(original_text)
    context.user_data['censored_text'] = censored_text
    context.user_data['has_bad_words'] = has_bad_words

    if has_bad_words:
        keyboard = [
            [InlineKeyboardButton("✅ Отправить на модерацию (с цензурой)", callback_data="approve_censor")],
            [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_text")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")]
        ]
        await safe_reply_text(
            update,
            f"Ваш текст содержит нецензурные выражения или контактную информацию. Он будет опубликован в следующем виде:\n\n"
            f"```\n{censored_text}\n```\n\n"
            "Вы можете отправить его на модерацию с цензурой или отредактировать.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_CENSOR_APPROVAL
    else:
        return await complete_request(update, context)

async def handle_censor_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает решение пользователя по цензуре."""
    query = update.callback_query
    await query.answer()

    if query.data == "approve_censor":
        context.user_data['text'] = context.user_data['censored_text'] # Используем цензурированный текст
        return await complete_request(update, context)
    elif query.data == "edit_text":
        request_type = context.user_data['request_type']
        if request_type == "congrat":
            await safe_edit_message_text(query, "Введите свой текст поздравления заново (до 500 символов).")
            return CUSTOM_CONGRAT_MESSAGE_INPUT
        elif request_type in ["announcement", "news"]:
            max_len = MAX_ANNOUNCE_NEWS_TEXT_LENGTH if request_type == 'news' else MAX_CONGRAT_TEXT_LENGTH
            await safe_edit_message_text(query, f"Введите текст заново (до {max_len} символов).")
            return ANNOUNCE_TEXT_INPUT
    elif query.data == "back_to_start":
        return await start(update, context)
    return ConversationHandler.END

async def complete_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Завершает процесс создания заявки и сохраняет ее в БД."""
    user_data = context.user_data
    user = update.effective_user

    app_data = {
        'user_id': user.id,
        'username': user.username or user.full_name,
        'type': user_data['request_type'],
        'subtype': user_data.get('subtype'),
        'from_name': user_data.get('from_name'),
        'to_name': user_data.get('to_name'),
        'text': user_data['text'],
        'publish_date': user_data.get('publish_date')
    }

    app_id = await add_application(app_data) # Изменено: await

    if app_id:
        await safe_reply_text(update,
            "✅ Ваша заявка принята и отправлена на модерацию. Мы уведомим вас о решении.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
        await notify_admin_new_application(context.bot, app_id, app_data) # Изменено: await
    else:
        await safe_reply_text(update,
            "❌ Произошла ошибка при сохранении заявки. Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )

    context.user_data.clear() # Очищаем данные пользователя после завершения заявки
    return ConversationHandler.END

async def notify_admin_new_application(bot: Bot, app_id: int, app_data: Dict[str, Any]):
    """Уведомляет администратора о новой заявке."""
    if not ADMIN_CHAT_ID:
        return

    app_type_name = REQUEST_TYPES.get(app_data['type'], {}).get('name', app_data['type'])
    message_text = f"🆕 Новая заявка #{app_id} от @{app_data['username'] or app_data['user_id']}:\n"
    message_text += f"Тип: {app_type_name}"
    if app_data.get('subtype'):
        message_text += f" ({ANNOUNCE_SUBTYPES.get(app_data['subtype'], app_data['subtype'])})"
    if app_data.get('from_name'):
        message_text += f"\nОт: {app_data['from_name']}"
    if app_data.get('to_name'):
        message_text += f"\nДля: {app_data['to_name']}"
    if app_data.get('publish_date'):
        message_text += f"\nДата публикации: {app_data['publish_date']}"
    message_text += f"\n\nТекст:\n```\n{app_data['text']}\n```"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_{app_id}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{app_id}")]
    ])

    await safe_send_message(bot, ADMIN_CHAT_ID, message_text, reply_markup=keyboard)

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает действия администратора (одобрение/отклонение)."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if not str(query.from_user.id) == str(ADMIN_CHAT_ID): # Проверка, что это админ
        await safe_reply_text(update, "У вас нет прав для выполнения этого действия.")
        return

    action, app_id_str = data.split("_")[1], data.split("_")[2]
    app_id = int(app_id_str)

    app_details = await get_application_details(app_id) # Изменено: await
    if not app_details:
        await safe_edit_message_text(query, f"Заявка #{app_id} не найдена или уже обработана.")
        return

    if app_details['status'] != 'pending':
        await safe_edit_message_text(query, f"Заявка #{app_id} уже имеет статус '{app_details['status']}'.")
        return

    user_id = app_details['user_id']
    username = app_details['username'] or str(user_id)

    if action == "approve":
        success = await update_application_status(app_id, 'approved') # Изменено: await
        if success:
            await safe_edit_message_text(query, f"✅ Заявка #{app_id} одобрена.")
            await safe_send_message(context.bot, user_id, f"✅ Ваша заявка #{app_id} одобрена и будет опубликована в ближайшее время.")
        else:
            await safe_edit_message_text(query, f"❌ Ошибка при одобрении заявки #{app_id}.")
    elif action == "reject":
        success = await update_application_status(app_id, 'rejected') # Изменено: await
        if success:
            await safe_edit_message_text(query, f"❌ Заявка #{app_id} отклонена.")
            await safe_send_message(context.bot, user_id, f"❌ Ваша заявка #{app_id} отклонена. Причина: не соответствует правилам канала. Пожалуйста, ознакомьтесь с правилами и попробуйте снова.")
        else:
            await safe_edit_message_text(query, f"❌ Ошибка при отклонении заявки #{app_id}.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущий диалог."""
    user = update.effective_user
    logger.info(f"Пользователь {user.full_name} ({user.id}) отменил диалог.")
    await safe_reply_text(update, "Действие отменено. Чем могу еще помочь?",
                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])) # Добавлена кнопка
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует ошибки, вызванные обработчиками."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if update.effective_chat:
            await safe_send_message(context.bot, update.effective_chat.id,
                                    "Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже.")
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения об ошибке: {e}")

async def post_init(application: Application) -> None:
    """Вызывается после инициализации приложения."""
    BOT_STATE['running'] = True
    BOT_STATE['start_time'] = datetime.now(TIMEZONE)
    BOT_STATE['last_activity'] = datetime.now(TIMEZONE)
    logger.info("Бот запущен и готов к работе.")
    await send_bot_status(application.bot, "Запущен")
    await init_db() # Инициализация БД при запуске

async def pre_shutdown(application: Application) -> None:
    """Вызывается перед завершением работы приложения."""
    BOT_STATE['running'] = False
    logger.info("Бот завершает работу.")
    await send_bot_status(application.bot, "Завершение работы")
    global db_pool
    if db_pool:
        await db_pool.close() # Закрытие пула соединений
        logger.info("Пул соединений с БД закрыт.")

def main() -> None:
    """Запускает бота."""
    check_environment()

    # Удалено: Проверка на уже запущенный экземпляр через сокет
    # try:
    #     lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    #     lock_socket.bind('\0' + 'bot_lock')
    # except socket.error:
    #     print("Бот уже запущен")
    #     sys.exit(0)

    application = Application.builder().token(TOKEN).build()

    # Добавляем обработчики сигналов для graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown_handler(s, application)))

    # Conversation Handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            TYPE_SELECTION: [
                CallbackQueryHandler(select_type, pattern="^type_")
            ],
            CONGRAT_HOLIDAY_CHOICE: [
                CallbackQueryHandler(select_congrat_holiday, pattern="^holiday_"),
                CallbackQueryHandler(select_congrat_holiday, pattern="^custom_congrat_text"),
                CallbackQueryHandler(start, pattern="^back_to_start")
            ],
            CUSTOM_CONGRAT_MESSAGE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_custom_congrat_text)
            ],
            SENDER_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_sender_name)
            ],
            RECIPIENT_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_recipient_name)
            ],
            CONGRAT_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_congrat_date)
            ],
            ANNOUNCE_SUBTYPE_SELECTION: [
                CallbackQueryHandler(select_announce_subtype, pattern="^subtype_"),
                CallbackQueryHandler(start, pattern="^back_to_start")
            ],
            ANNOUNCE_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_announce_text)
            ],
            WAIT_CENSOR_APPROVAL: [
                CallbackQueryHandler(handle_censor_approval, pattern="^(approve_censor|edit_text|back_to_start)$")
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^back_to_start$") # Обработка кнопки "Назад" в любом состоянии
        ]
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
    application.add_error_handler(error_handler)

    # APScheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(check_pending_applications, 'interval', minutes=1, args=(application,))
    # scheduler.add_job(check_shutdown_time, 'cron', hour=23, minute=0, args=(application.job_queue,)) # Удалено: не нужно для Render
    scheduler.start()

    # Добавляем post_init и pre_shutdown колбэки
    application.post_init = post_init
    application.pre_shutdown = pre_shutdown

    logger.info("Запуск бота...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

async def shutdown_handler(signum, application: Application):
    logger.info(f"Получен сигнал {signum}, инициирую graceful shutdown...")
    await application.shutdown()
    logger.info("Приложение Telegram Bot API завершило работу.")
    # sys.exit(0) # Не вызываем sys.exit здесь, чтобы позволить run_polling завершиться

if __name__ == '__main__':
    main()


