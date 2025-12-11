import os
import json
import logging
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

# НОВЫЕ ИМПОРТЫ ДЛЯ FIREBASE И WEBHOOKS
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request, jsonify 
import asyncio # Для асинхронных операций в синхронной среде Flask


# --- 1. НАСТРОЙКА ---

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise EnvironmentError("❌ TELEGRAM_BOT_TOKEN не задан.")
try:
    MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID"))
except (ValueError, TypeError):
    # Если MANAGER_CHAT_ID не задан или не число, присваиваем 0, чтобы код не падал, но логика будет ограничена
    MANAGER_CHAT_ID = 0 
    logger.warning("⚠️ MANAGER_CHAT_ID не задан. Функции менеджера будут недоступны.")

# --- 2. ИНИЦИАЛИЗАЦИЯ FIREBASE ---

FIREBASE_CONFIG = os.getenv("FIREBASE_CONFIG_JSON") 

if not FIREBASE_CONFIG:
    # --- ЛОКАЛЬНОЕ ТЕСТИРОВАНИЕ ---
    try:
        # ⚠️ ИЗМЕНИТЕ ПУТЬ К ВАШЕМУ JSON-КЛЮЧУ
        path_to_key = 'serviceAccountKey.json' 
        cred = credentials.Certificate(path_to_key)
    except Exception as e:
         raise EnvironmentError("❌ FIREBASE_CONFIG_JSON не задан, и локальный файл ключа Firebase не существует: " + str(e))
else:
    # --- ДЕПЛОЙ НА VERCEL ---
    try:
        cred_dict = json.loads(FIREBASE_CONFIG)
        cred = credentials.Certificate(cred_dict)
    except Exception as e:
        raise ValueError(f"❌ Ошибка парсинга JSON Firebase из переменной окружения: {e}")

try:
    firebase_admin.initialize_app(cred)
except ValueError:
    pass
    
db = firestore.client()
submissions_collection = db.collection('submissions')

# --- 3. ФУНКЦИИ ДЛЯ РАБОТЫ С FIREBASE ---

# Все функции здесь должны быть асинхронными, если не требуют обхода event loop
# Однако, firebase-admin - это синхронный SDK. Мы будем вызывать его в асинхронных функциях
# и полагаться на асинхронную природу python-telegram-bot и Flask.

async def save_submission_to_db(submission_data):
    """Сохранение новой заявки в Firestore."""
    doc_ref = submissions_collection.document()
    submission_data['doc_id'] = doc_ref.id 
    doc_ref.set(submission_data) 
    return submission_data

async def get_submissions_list(filter_key=None):
    """Получение всех, избранных или отобранных заявок."""
    query = submissions_collection
    
    if filter_key == 'favorite':
        query = query.where('favorite', '==', True)
    elif filter_key == 'selected':
        query = query.where('selected', '==', True)
        
    query = query.order_by('createdAt', direction=firestore.Query.DESCENDING)
        
    docs = query.stream()
    # Добавляем doc_id в каждый документ
    return [{**doc.to_dict(), 'doc_id': doc.id} for doc in docs]

async def update_submission_status(doc_id, updates):
    """Обновление статуса (favorite/selected) по doc_id."""
    submissions_collection.document(doc_id).update(updates)

async def get_submission_by_doc_id(doc_id):
    """Поиск заявки по ID документа."""
    doc = submissions_collection.document(doc_id).get()
    if doc.exists:
        data = doc.to_dict()
        data['doc_id'] = doc.id 
        return data
    return None

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БОТА ---

def build_keyboard(submission, index=None, total=None):
    """Построение inline-клавиатуры для заявки."""
    fav_label = '⭐ Убрать из избранного' if submission.get('favorite') else '⭐ В избранное'
    sel_label = '🏁 Убрать из отбора' if submission.get('selected') else '🏁 В отбор'
    
    doc_id = submission.get('doc_id') 

    keyboard = [
        [
            InlineKeyboardButton(fav_label, callback_data=f"fav:{doc_id}"),
            InlineKeyboardButton(sel_label, callback_data=f"sel:{doc_id}"),
        ]
    ]

    if index is not None and total is not None:
        nav_row = [
            InlineKeyboardButton('← Назад', callback_data=f"prev:{index}"),
            InlineKeyboardButton(f"{index}/{total}", callback_data='noop'),
            InlineKeyboardButton('Вперёд →', callback_data=f"next:{index}")
        ]
        keyboard.append(nav_row)

    return InlineKeyboardMarkup(keyboard)

async def send_submission(context: ContextTypes.DEFAULT_TYPE, chat_id, sub, index=None, total=None, reply_message_id=None):
    """Отправка заявки (фото или текст) с клавиатурой."""
    keyboard = build_keyboard(sub, index, total)
    caption = sub['text']
    
    if sub['photo']:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=sub['photo'],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            reply_to_message_id=reply_message_id
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            reply_to_message_id=reply_message_id
        )


# --- 5. ОБРАБОТЧИКИ ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    if update.effective_chat.id != MANAGER_CHAT_ID:
        # Для пользователей
        await update.message.reply_text("Добро пожаловать! Пришлите вашу заявку, начинающуюся с 🎄.")
        return 

    # Для менеджера
    keyboard = [
        [KeyboardButton('📋 Все заявки')],
        [KeyboardButton('⭐ Избранные'), KeyboardButton('🏁 Отобранные')]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text('Панель менеджера', reply_markup=reply_markup)


async def handle_new_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящих сообщений как заявок."""
    msg = update.message
    content = msg.caption or msg.text or ''

    # Проверка: заявка должна начинаться с 🎄 или содержать фото
    is_submission = content.startswith('🎄') or (msg.photo and len(msg.photo) > 0)
    if not is_submission:
        return

    photo_file_id = msg.photo[-1].file_id if msg.photo else None

    submission = {
        'chatId': msg.chat.id,
        'text': content.strip() or '📷 Фото без описания',
        'photo': photo_file_id,
        'favorite': False,
        'selected': False,
        'createdAt': firestore.SERVER_TIMESTAMP,
    }

    saved_submission = await save_submission_to_db(submission)
    doc_id = saved_submission['doc_id']
    
    logger.info(f"✅ Заявка сохранена в Firestore, ID: {doc_id}")
    
    # Отправка менеджеру
    await send_submission(context, MANAGER_CHAT_ID, saved_submission)


async def handle_manager_hears(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок меню менеджера."""
    if update.effective_chat.id != MANAGER_CHAT_ID:
        return

    text = update.message.text
    filter_key = None
    
    if text == '📋 Все заявки':
        pass
    elif text == '⭐ Избранные':
        filter_key = 'favorite'
    elif text == '🏁 Отобранные':
        filter_key = 'selected'
    else:
        return

    list_to_show = await get_submissions_list(filter_key)
    
    if not list_to_show:
        await update.message.reply_text(f"❌ {text} пока нет")
        return

    sub = list_to_show[0]
    total = len(list_to_show)
    
    await send_submission(context, update.effective_chat.id, sub, index=1, total=total)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка Inline-кнопок (fav, sel, next, prev)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split(':')
    query_type = parts[0]
    
    if query_type == 'noop':
        return

    # 🔁 Переключение (next/prev)
    if query_type in ['next', 'prev']:
        submissions = await get_submissions_list() 
        current_index_one_based = int(parts[1]) 
        current_index = current_index_one_based - 1
        total = len(submissions)
        
        if not total: return await query.answer("Список пуст")
        
        if query_type == 'next':
            new_index = (current_index + 1) % total
        else:
            new_index = (current_index - 1 + total) % total

        sub = submissions[new_index]
        try:
            await query.delete_message()
            await send_submission(context, query.message.chat_id, sub, new_index + 1, total)
        except Exception as e:
            logger.error(f"❌ Ошибка при переключении: {e}")
        return

    # ✅ Обработка fav / sel
    if query_type in ['fav', 'sel']:
        doc_id = parts[1] 
        
        sub = await get_submission_by_doc_id(doc_id)
        if not sub:
            return await query.answer("Заявка не найдена")

        updates = {}
        if query_type == 'fav':
            new_status = not sub.get('favorite', False)
            updates['favorite'] = new_status
        elif query_type == 'sel':
            new_status = not sub.get('selected', False)
            updates['selected'] = new_status
        
        if not updates:
            return await query.answer("Нечего обновлять")

        await update_submission_status(doc_id, updates)
        
        # Обновляем объект для клавиатуры
        sub.update(updates) 
        
        # Пересчитываем индекс для правильной навигации
        submissions = await get_submissions_list()
        total = len(submissions)
        index = next((i for i, s in enumerate(submissions) if s.get('doc_id') == doc_id), -1)
        
        if index != -1:
            await query.edit_message_reply_markup(
                reply_markup=build_keyboard(sub, index + 1, total)
            )
            await query.answer("✅ Сохранено")
        else:
             await query.answer("⚠️ Не удалось обновить")


# --- 6. НАСТРОЙКА WEBHOOK (для Vercel) ---

def init_application():
    """Инициализация и настройка обработчиков."""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Добавление обработчиков
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(
        filters.ALL & ~filters.Chat(MANAGER_CHAT_ID), 
        handle_new_submission
    ))
    application.add_handler(MessageHandler(
        filters.Chat(MANAGER_CHAT_ID) & filters.Text(['📋 Все заявки', '⭐ Избранные', '🏁 Отобранные']),
        handle_manager_hears
    ))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    return application

# Создание Flask-приложения и инициализация Telegram Application
app = Flask(__name__)
application = init_application()

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
async def telegram_webhook(path):
    """Основной Webhook-эндпоинт для Telegram."""
    if request.method == "POST":
        # Обработка обновления от Telegram
        update = Update.de_json(request.get_json(force=True), application.bot)
        
        # ⚠️ ВАЖНО: Flask по умолчанию синхронный. Мы явно запускаем асинхронный метод
        # application.process_update в event loop.
        asyncio.run(application.process_update(update))
        
        return jsonify({"status": "ok"})
    
    # GET-запрос (для проверки работы Vercel)
    return jsonify({"status": "Bot is running on Vercel"})


# --- ЛОКАЛЬНЫЙ ЗАПУСК (для тестирования) ---
# Для запуска локально используйте: python bot.py
if __name__ == '__main__':
    logger.info("🤖 Запуск локально (Polling)...")
    
    # Если вы хотите тестировать Webhooks локально, вам нужно использовать ngrok:
    # from telegram.ext import CallbackContext
    # application.run_polling(poll_interval=1.0)
    
    # Для простоты тестируем Flask, но вам нужно настроить Webhook в Telegram
    app.run(port=os.environ.get("PORT", 5000))