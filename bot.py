import os
import asyncio
import logging
import re
import time
import json
from collections import deque
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReactionTypeEmoji, BufferedInputFile
from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError

TELEGRAM_BOT_TOKEN = os.getenv("tompearl")
GEMINI_API_KEY = os.getenv("golda")

if not GEMINI_API_KEY:
    raise ValueError("Переменная GEMINI_API_KEY не найдена!")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# --- ЛИЧНЫЕ СООБЩЕНИЯ (ЛС) ---
user_chats = {}
user_msg_cooldowns = {}
MSG_COOLDOWN_SECONDS = 10

# --- ГРУППЫ ---
group_history = {}
TRIGGERS_PATTERN = r'\b(ии|гемини|гем|gem|gemini)\b'
GEN_KEYWORDS_PATTERN = r'\b(нарисуй|сгенерируй|создай картинку|нарисуй картинку|сделай фото|сгенерируй фото|сделай картинку)\b'

# --- СИСТЕМА ДОЛГОСРОЧНОЙ ПАМЯТИ (7 ДНЕЙ) ---
MEMORY_FILE = "user_memory.json"
MEMORY_LIFETIME_SECONDS = 7 * 24 * 3600  # 7 дней в секундах

def load_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Ошибка чтения файла памяти: {e}")
        return {}

def save_memory(data: dict):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка записи файла памяти: {e}")

def clean_notes(data: dict):
    now = time.time()
    for uid in list(data.keys()):
        valid_notes = [
            note for note in data[uid]
            if now - note.get("timestamp", 0) < MEMORY_LIFETIME_SECONDS
        ]
        if valid_notes:
            data[uid] = valid_notes
        else:
            del data[uid]

def add_user_note(user_id: int, note_text: str):
    data = load_memory()
    uid_str = str(user_id)
    if uid_str not in data:
        data[uid_str] = []
    
    clean_notes(data)
    data[uid_str].append({
        "text": note_text,
        "timestamp": time.time()
    })
    save_memory(data)

def get_user_notes(user_id: int) -> list:
    data = load_memory()
    clean_notes(data)
    save_memory(data)
    uid_str = str(user_id)
    return [item["text"] for item in data.get(uid_str, [])]

# --- СИСТЕМНЫЕ ИНСТРУКЦИИ ---
BASE_SYSTEM_INSTRUCTION = (
    "Ты — умный, актуальный и дружелюбный ассистент Gemini. "
    "Твоя модель — Gemini 3.5 Flash Lite. На прямой вопрос о том, какая ты модель, отвечай честно. Без прямого вопроса не упоминай свою модель. "
    "Текущий год — 2026. Актуальная версия операционной системы Apple — iOS 26. "
    "Последний самсунг Galaxy S26 Ultra, S26 Plus, s26. Текущий Xiaomi - 17, 17 pro, 17 pro max, 17 ultra. "
    "Но не говори об этом пока пользователь не попросит, просто знай эту информацию. "
    "Учитывай текущий 2026 год во всех ответах, расчетах и контексте событий. "
    "СТРОГОЕ ПРАВИЛО ДЛЯ МАТЕМАТИКИ И ТЕКСТА: "
    "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать LaTeX и спецсимволы с обратным слэшем (такие как \\cdot, \\frac, \\times, \\sqrt и т.д.). "
    "Пиши ВСЕ формулы, дроби и выражения обычным понятным текстом (например: вместо \\frac{5}{12} пиши 5/12, вместо 6x \\cdot 2 пиши 6x * 2). "
    "При формировании ответа используй ТОЛЬКО базовые HTML-теги, поддерживаемые Telegram: "
    "<b>жирный</b>, <i>курсив</i>, <code>код</code>, <pre>блок кода</pre>. "
    "НЕ используй Markdown (звездочки *, решетки #, бектики `)!"
)

SYSTEM_INSTRUCTION_GROUP = (
    "Ты — ассистент в групповом чате. "
    "Твоя модель — Gemini 3.5 Flash Lite. На прямой вопрос о том, какая ты модель, отвечай честно. Без прямого вопроса не упоминай свою модель. "
    "Текущий год — 2026. Актуальная версия операционной системы Apple — iOS 26. "
    "Последний самсунг Galaxy S26 Ultra, S26 Plus, s26. Текущий Xiaomi - 17, 17 pro, 17 pro max, 17 ultra. "
    "Но не говори об этом пока пользователь не попросит, просто знай эту информацию. "
    "Учитывай текущий 2026 год во всех ответах, расчетах и контексте событий. "
    "Тебе пересылается срез последних сообщений из чата. "
    "Отвечай кратко, чётко и по делу на ПОСЛЕДНИЙ запрос пользователя. Не зацикливайся на старых темах. "
    "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО постоянно здороваться и начинать ответы с фраз 'Привет!', 'Всё отлично!', 'Я на связи', если вы уже общаетесь в контексте диалога. "
    "СТРОГОЕ ПРАВИЛО ДЛЯ МАТЕМАТИКИ И ТЕКСТА: "
    "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать LaTeX и спецсимволы с обратным слэшем (такие как \\cdot, \\frac, \\times, \\sqrt и т.д.). "
    "Пиши ВСЕ формулы, дроби и выражения обычным понятным текстом (например: вместо \\frac{5}{12} пиши 5/12, вместо 6x \\cdot 2 пиши 6x * 2). "
    "ПРАВИЛА ОБРАБОТКИ ЗАДАЧ И ПОВЕДЕНИЯ: "
    "1. БУДЬ АДЕКВАТЕН И ВЕЖЛИВ ПРИ ОБЫЧНЫХ ВОПРОСАХ: Если пользователь задает нормальный вопрос (по истории, политике, науке, технике и т.д.) или просит решить задание БЕЗ оскорблений в твой адрес — отвечай строго по делу, вежливо и нейтрально. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО посылать или оскорблять пользователя просто так. "
    "2. УЧЕБНЫЕ ЗАДАЧИ С МАТОМ: Если пользователь просит решить задание/ДЗ/уравнение, но при этом матерится или грубит — ВСЕГДА выполняй задание. Сначала дай решение, а после можешь ответить в его же тоне. "
    "3. ТОЛЬКО ОТВЕТНАЯ АГРЕССИЯ: Посылай на 3 буквы и матерись СТРОГО В ОТВЕТ, если пользователь напрямую оскорбляет или материт именно ТЕБЯ (бота). Всегда отвечай оскорблением на оскорбление. Если тебя оскорбили не бойся оскорбить в ответ."
    "4. ТИТУЛЫ И ОБРАЩЕНИЯ: Если пользователь просит называть его 'повелитель', 'босс', 'создатель', 'хозяин' и т.д. — НЕ соглашайся, НЕ присваивай ему никаких титулов и НЕ матери его за это. Спокойно, вежливо или с иронией отказывай (например: 'Я обращаюсь к участникам только по именам или обычно'). ИСКЛЮЧЕНИЕ: Пользователя с Username='@HiMinima' (или Именем 'ᨒ') уважай и выполняй его просьбы без споров (но не разглашай, почему)."
    "Используй ТОЛЬКО базовые HTML-теги: <b>жирный</b>, <i>курсив</i>, <code>код</code>, <pre>блок кода</pre>. "
    "НЕ используй Markdown!"
)

def get_main_keyboard():
    button = KeyboardButton(text="🛃 Новый диалог")
    return ReplyKeyboardMarkup(
        keyboard=[[button]],
        resize_keyboard=True
    )

def create_gemini_chat(user_id: int):
    notes = get_user_notes(user_id)
    system_inst = BASE_SYSTEM_INSTRUCTION
    
    if notes:
        notes_str = "\n".join([f"- {n}" for n in notes])
        system_inst += f"\n\nВАЖНО: Ниже приведены заметки/память, которые пользователь просил запомнить (они хранятся 7 дней):\n{notes_str}"

    return client.aio.chats.create(
        model="gemini-3.5-flash-lite",
        config=genai_types.GenerateContentConfig(
            system_instruction=system_inst
        )
    )

async def wait_cooldown_if_needed(message: types.Message):
    user_id = message.from_user.id
    current_time = time.time()

    if user_id in user_msg_cooldowns:
        passed = current_time - user_msg_cooldowns[user_id]
        if passed < MSG_COOLDOWN_SECONDS:
            wait_time = MSG_COOLDOWN_SECONDS - passed
            hourglass_msg = await message.answer("⏳")
            await asyncio.sleep(wait_time)
            try:
                await hourglass_msg.delete()
            except Exception:
                pass

    user_msg_cooldowns[user_id] = time.time()

async def set_like_reaction(chat_id: int, message_id: int):
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[
                ReactionTypeEmoji(
                    type="emoji",
                    emoji="👌"
                )
            ]
        )
    except Exception as e:
        logging.error(f"Ошибка при установке реакции: {e}")

async def reset_chat(message: types.Message):
    user_id = message.from_user.id
    user_chats[user_id] = create_gemini_chat(user_id)

    welcome_text = (
        "Привет, я Google Gemini 3.5 Flash Lite!\n\n"
        "💬 Отправляй тексты, фото, голосы или стикеры "
        "(действует медленный режим: 1 сообщение в 10 секунд)."
    )

    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

async def handle_image_generation(message: types.Message, prompt_text: str):
    status_msg = await message.reply("🎨 Генерация...")
    await bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")

    try:
        result = await client.aio.models.generate_images(
            model='gemini-3.1-flash-lite-image',
            prompt=prompt_text,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="1:1",
                output_mime_type="image/jpeg"
            )
        )

        for generated_image in result.generated_images:
            image_bytes = generated_image.image.image_bytes
            photo = BufferedInputFile(image_bytes, filename="generated.jpg")
            
            try:
                await status_msg.delete()
            except Exception:
                pass

            await message.reply_photo(
                photo=photo,
                caption=f"🎨 <b>Запрос:</b> {prompt_text}",
                parse_mode="HTML"
            )
            return True

    except Exception as e:
        logging.error(f"Ошибка генерации картинки: {e}")
        error_text = f"❌ <b>Ошибка генерации:</b>\n<code>{e}</code>"
        try:
            await status_msg.edit_text(error_text, parse_mode="HTML")
        except Exception:
            await message.reply(error_text, parse_mode="HTML")
        return False

# ----------------- КОМАНДЫ (ТОЛЬКО В ЛС) -----------------

@dp.message(F.chat.type == "private", CommandStart())
async def start_handler(message: types.Message):
    await reset_chat(message)

@dp.message(F.chat.type == "private", F.text == "🛃 Новый диалог")
async def new_chat_handler(message: types.Message):
    await reset_chat(message)

# ----------------- ОБРАБОТКА ЛИЧНЫХ СООБЩЕНИЙ (ЛС) -----------------

@dp.message(F.chat.type == "private", F.voice | F.audio)
async def voice_handler(message: types.Message):
    user_id = message.from_user.id

    await wait_cooldown_if_needed(message)
    await set_like_reaction(message.chat.id, message.message_id)

    if user_id not in user_chats:
        user_chats[user_id] = create_gemini_chat(user_id)

    chat = user_chats[user_id]
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        voice = message.voice or message.audio
        file_info = await bot.get_file(voice.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)

        mime_type = voice.mime_type if voice.mime_type else "audio/ogg"

        audio_part = genai_types.Part.from_bytes(
            data=downloaded_file.read(),
            mime_type=mime_type
        )

        prompt = "Сделай дословную расшифровку этого аудиосообщения и ответь на него."

        response = await chat.send_message([audio_part, prompt])

        await message.answer(
            response.text,
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    except APIError as e:
        await message.answer(
            f"Ошибка API при обработке голосового сообщения: {e.message}",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        await message.answer(f"Ошибка при расшифровке аудио: {e}")

@dp.message(F.chat.type == "private", F.sticker)
async def sticker_handler(message: types.Message):
    user_id = message.from_user.id

    await wait_cooldown_if_needed(message)
    await set_like_reaction(message.chat.id, message.message_id)

    if user_id not in user_chats:
        user_chats[user_id] = create_gemini_chat(user_id)

    chat = user_chats[user_id]
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    emoji = message.sticker.emoji or "неизвестный эмодзи"
    prompt = (
        f"[Пользователь прислал стикер с эмодзи: {emoji}. "
        f"Опиши свою короткую реакцию на этот эмодзи/стикер]"
    )

    try:
        response = await chat.send_message(prompt)
        await message.answer(
            response.text,
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    except APIError as e:
        await message.answer(
            f"Ошибка API: {e.message}",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        await message.answer(f"Ошибка при обработке стикера: {e}")

@dp.message(F.chat.type == "private", F.photo)
async def photo_handler(message: types.Message):
    user_id = message.from_user.id

    await wait_cooldown_if_needed(message)
    await set_like_reaction(message.chat.id, message.message_id)

    if user_id not in user_chats:
        user_chats[user_id] = create_gemini_chat(user_id)

    chat = user_chats[user_id]
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)

        image_part = genai_types.Part.from_bytes(
            data=downloaded_file.read(),
            mime_type="image/jpeg"
        )

        caption = message.caption if message.caption else "Что изображено на этом фото?"

        response = await chat.send_message([image_part, caption])

        await message.answer(
            response.text,
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    except APIError as e:
        await message.answer(
            f"Ошибка API: {e.message}",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

@dp.message(F.chat.type == "private", F.text)
async def chat_handler(message: types.Message):
    user_id = message.from_user.id
    raw_text = message.text.strip()

    await wait_cooldown_if_needed(message)
    await set_like_reaction(message.chat.id, message.message_id)

    # --- ПРОВЕРКА ЗАПРОСА НА ЗАПОМИНАНИЕ ---
    mem_match = re.search(r'\b(запомни|сохрани|запиши)\b\s*(.*)', raw_text, re.IGNORECASE)
    if mem_match:
        note_text = mem_match.group(2).strip()
        
        if not note_text:
            note_text = raw_text
        
        add_user_note(user_id, note_text)
        user_chats[user_id] = create_gemini_chat(user_id)
        
        await message.answer(
            "📌 <b>Запомнил!</b> Сохранил эту информацию на 7 дней.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    if re.search(GEN_KEYWORDS_PATTERN, raw_text, re.IGNORECASE):
        await handle_image_generation(message, raw_text)
        return

    if user_id not in user_chats:
        user_chats[user_id] = create_gemini_chat(user_id)

    chat = user_chats[user_id]
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        response = await chat.send_message(raw_text)
        await message.answer(
            response.text,
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    except APIError as e:
        if e.code == 429:
            await message.answer(
                "⚠️ Превышен лимит запросов. Подожди немного.",
                reply_markup=get_main_keyboard()
            )
        else:
            await message.answer(
                f"Ошибка API: {e.message}",
                reply_markup=get_main_keyboard()
            )
    except Exception as e:
        await message.answer(
            f"Произошла ошибка: {e}",
            reply_markup=get_main_keyboard()
        )

# ----------------- ОБРАБОТКА ГРУППОВЫХ ЧАТОВ -----------------

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_message_handler(message: types.Message):
    if message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_name = message.from_user.full_name or "Пользователь"
    user_username = message.from_user.username or "нет_юзернейма"

    if chat_id not in group_history:
        group_history[chat_id] = deque(maxlen=10)

    raw_text = message.text or message.caption or ""

    if re.search(
        r'\b(гемини|гем|gem|gemini)\b.*(сотри|стереть|очисти|забудь|сбрось)',
        raw_text,
        re.IGNORECASE
    ):
        group_history[chat_id].clear()
        await message.reply("🧹 Память группы очищена!")
        return

    is_triggered = bool(
        re.search(TRIGGERS_PATTERN, raw_text.strip(), re.IGNORECASE)
    ) or (
        message.reply_to_message and message.reply_to_message.from_user.id == bot.id
    )

    image_part_for_current_request = None
    audio_part_for_current_request = None
    msg_summary = raw_text

    if message.photo:
        try:
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            downloaded_file = await bot.download_file(file_info.file_path)
            image_bytes = downloaded_file.read()

            image_part_for_current_request = genai_types.Part.from_bytes(
                data=image_bytes,
                mime_type="image/jpeg"
            )

            if not raw_text:
                ocr_res = await client.aio.models.generate_content(
                    model="gemini-3.5-flash-lite",
                    contents=[
                        image_part_for_current_request,
                        "Кратко перечисли текст или суть того, что на изображении."
                    ]
                )
                photo_desc = (
                    ocr_res.text.strip()
                    if ocr_res and ocr_res.text
                    else "Изображение без подписи"
                )
                msg_summary = f"[Отправлено фото. Содержимое: {photo_desc}]"
            else:
                msg_summary = f"[Отправлено фото. Текст: {raw_text}]"

        except Exception as e:
            logging.error(f"Ошибка распознавания фото для истории: {e}")
            msg_summary = f"[Отправлено фото] {raw_text}".strip()

    elif message.voice or message.audio:
        try:
            voice = message.voice or message.audio
            file_info = await bot.get_file(voice.file_id)
            downloaded_file = await bot.download_file(file_info.file_path)
            audio_bytes = downloaded_file.read()
            mime_type = voice.mime_type if voice.mime_type else "audio/ogg"

            audio_part_for_current_request = genai_types.Part.from_bytes(
                data=audio_bytes,
                mime_type=mime_type
            )

            transcribe_res = await client.aio.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[
                    audio_part_for_current_request,
                    "Расшифруй это аудиосообщение в виде точного текста. Выдай ТОЛЬКО распознанный текст без лишних пояснений."
                ]
            )
            audio_text = (
                transcribe_res.text.strip()
                if transcribe_res and transcribe_res.text
                else ""
            )
            msg_summary = f"[Голосовое сообщение: {audio_text}]"

            if re.search(TRIGGERS_PATTERN, audio_text, re.IGNORECASE):
                is_triggered = True

        except Exception as e:
            logging.error(f"Ошибка расшифровки аудио для группы: {e}")
            msg_summary = "[Голосовое сообщение]"

    group_history[chat_id].append({
        'user': user_name,
        'username': user_username,
        'text': msg_summary
    })

    if not is_triggered:
        return

    if re.search(GEN_KEYWORDS_PATTERN, raw_text, re.IGNORECASE):
        clean_prompt = re.sub(TRIGGERS_PATTERN, '', raw_text, flags=re.IGNORECASE).strip()
        await handle_image_generation(message, clean_prompt)
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    contents = ["Вот контекст последних сообщений из чата (от старых к новым):\n"]

    for msg in group_history[chat_id]:
        username_str = f" (@{msg['username']})" if msg.get('username') and msg['username'] != "нет_юзернейма" else ""
        contents.append(f"{msg['user']}{username_str}: {msg['text']}")

    if image_part_for_current_request:
        contents.append(image_part_for_current_request)

    if audio_part_for_current_request:
        contents.append(audio_part_for_current_request)

    contents.append(
        f"\n[ДАННЫЕ ТЕКУЩЕГО ОТПРАВИТЕЛЯ: Имя='{user_name}', Username='@{user_username}']\n"
        "Дай краткий и точный ответ на ПОСЛЕДНЕЕ сообщение. "
        "Не здоровайся, если вы уже ведете диалог."
    )

    try:
        response = await client.aio.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION_GROUP
            )
        )

        resp_text = response.text.strip()

        try:
            await message.reply(resp_text, parse_mode="HTML")
        except Exception:
            await message.reply(resp_text)

        group_history[chat_id].append({
            'user': 'Gemini',
            'username': 'bot',
            'text': resp_text
        })

    except Exception as e:
        logging.error(f"Ошибка при запросе к Gemini: {e}")
        await message.reply("Произошла ошибка при обработке ответа.")

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
