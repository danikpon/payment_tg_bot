import asyncio
import os
from datetime import datetime, timedelta
import re

import pytz
from aiogram import Bot, Dispatcher
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    Update
)
from aiogram.filters import CommandStart, Command, Text, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ContentType
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import logging

# Импортируем функции работы с базой данных
from database import (
    init_db,
    add_user,
    get_user,
    get_user_by_username,
    update_expire_date,
    update_total_paid,
    get_all_users
)

# Загрузка переменных окружения из .env файла
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Получение переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не установлен BOT_TOKEN в переменных окружения.")

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID") or 370756745)  # Укажите ID админа или группы
PAYMENT_LINK = os.getenv("PAYMENT_LINK")  # Ссылка на оплату

if not PAYMENT_LINK:
    raise ValueError("Не установлена PAYMENT_LINK в переменных окружения.")

# Константы подписки
COST_PER_MONTH = 50
DAYS_PER_MONTH = 30

# Инициализация планировщика задач
scheduler = AsyncIOScheduler(timezone=pytz.timezone("Europe/Moscow"))

# Инициализация бота и диспетчера с FSM
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())

# Создание объекта Router **до** определения обработчиков
router = Router()
dp.include_router(router)

# ============ FSM States ============
class AdminStates(StatesGroup):
    waiting_for_reset_username = State()
    waiting_for_gift_subscription = State()
    waiting_for_send_message = State()
    waiting_for_broadcast_text = State()
    awaiting_file = State()
    awaiting_file_with_file = State()

class UserStates(StatesGroup):
    waiting_for_custom_amount = State()

# ============ Вспомогательные функции ============
def rub_to_days(amount: int) -> int:
    """
    Переводит сумму (рубли) в дни подписки, 50 руб = 30 дней.
    """
    full_months = amount // COST_PER_MONTH
    leftover_rub = amount % COST_PER_MONTH
    leftover_days = leftover_rub * DAYS_PER_MONTH // COST_PER_MONTH
    return full_months * DAYS_PER_MONTH + leftover_days

async def notify_admin(text: str, reply_markup=None):
    """
    Отправляет сообщение админу или группе через ADMIN_CHAT_ID.
    """
    try:
        await bot.send_message(ADMIN_CHAT_ID, text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления админу: {e}")

def get_mention(user) -> str:
    """
    Возвращает '@username', если есть, иначе 'Имя (id)'.
    """
    username = user.username
    return f"@{username}" if username else f"{user.full_name} (id={user.id})"

def normalize_username(username: str) -> str:
    """
    Удаляет символ '@' из начала имени пользователя и приводит к нижнему регистру.
    """
    return username.lstrip('@').lower()

def create_payment_keyboard(amount: int = None) -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру для оплаты.
    Если amount указан, создаёт кнопку "Оплатить {amount} руб".
    Если amount не указан, создаёт кнопку для ввода суммы.
    """
    if amount:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Оплатить {amount} руб", url=f"{PAYMENT_LINK}?amount={amount}")]
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить свою сумму", callback_data="pay_custom_amount")]
        ])
    return keyboard

def create_connect_vpn_keyboard() -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру с кнопкой "Подключить VPN".
    """
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подключить VPN", callback_data="connect_vpn")]
    ])
    return keyboard

def create_admin_decision_keyboard() -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру с кнопками "Принять" и "Отклонить".
    """
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Принять", callback_data="admin_accept"),
            InlineKeyboardButton(text="Отклонить", callback_data="admin_reject")
        ]
    ])
    return keyboard

def create_send_file_keyboard() -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру с кнопками "Да" и "Нет" для отправки файла.
    """
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да", callback_data="send_file_yes"),
            InlineKeyboardButton(text="Нет", callback_data="send_file_no")
        ]
    ])
    return keyboard

# ============ Пользовательские команды ============
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = message.from_user
    normalized_username = normalize_username(user.username) if user.username else f"id_{user.id}"

    logger.info(f"Получен /start от пользователя {user_id} (@{normalized_username})")

    if user_id == ADMIN_CHAT_ID:
        # Администратор
        await message.answer(
            f"Здравствуйте, {get_mention(user)}! Вы администратор бота.\n"
            "Доступные команды: /check_users, /sum_payments, /reset_subscription, /gift_subscription, /send_message, /broadcast"
        )
        return

    # Проверяем, есть ли пользователь в базе данных
    user_record = await get_user(user_id)
    if not user_record:
        # Новый пользователь
        await add_user(user_id, normalized_username)
        await message.answer(
            f"Привет, {get_mention(user)}! Добро пожаловать в наш VPN-сервис.\n"
            "Ваш запрос на подключение отправлен администратору."
        )
        # Уведомляем администратора с кнопками "Принять" и "Отклонить"
        await notify_admin(
            f"Новый пользователь @{normalized_username} запросил подключение VPN.",
            reply_markup=create_admin_decision_keyboard()
        )
    else:
        # Пользователь уже существует (администратор его принял)
        expire_date_str = user_record[2]  # expire_date
        logger.info(f"Пользователь {user_id} имеет expire_date: {expire_date_str}")
        
        # Определяем, активна ли подписка
        subscription_active = False
        if expire_date_str:
            try:
                expire_date = datetime.fromisoformat(expire_date_str)
                if expire_date > datetime.now():
                    subscription_active = True
                    days_left = (expire_date - datetime.now()).days
                    await message.answer(f"Ваша подписка активна. Осталось {days_left} дней.")
            except ValueError:
                logger.error(f"Некорректный формат даты для пользователя {user_id}: {expire_date_str}")

        # Показываем кнопки оплаты независимо от expire_date
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Проверить подписку", callback_data="check_subscription"),
                InlineKeyboardButton(text="Оплатить 50 руб", callback_data="pay_standard")
            ],
            [
                InlineKeyboardButton(text="Оплатить свою сумму", callback_data="pay_custom_amount")
            ]
        ])
        await message.answer(
            "Выберите действие:",
            reply_markup=keyboard
        )

# Команда для проверки подписки
@router.message(Command("check"))
async def cmd_check(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    logger.info(f"Пользователь {user_id} запросил проверку подписки")
    if not user:
        await message.answer("Вы не зарегистрированы. Отправьте /start для регистрации.")
        return
    if user_id == ADMIN_CHAT_ID:
        await message.answer("Вы администратор бота.")
        return
    expire_date_str = user[2]  # expire_date
    if expire_date_str:
        try:
            expire_date = datetime.fromisoformat(expire_date_str)
            if expire_date > datetime.now():
                days_left = (expire_date - datetime.now()).days
                await message.answer(f"Ваша подписка активна. Осталось {days_left} дней.")
                return
        except ValueError:
            logger.error(f"Некорректный формат даты для пользователя {user_id}: {expire_date_str}")
    # Проверяем подписку родителя
    parent_user_id = user[4] if len(user) > 4 else None  # parent_user_id
    if parent_user_id:
        parent_user = await get_user(parent_user_id)
        if parent_user and parent_user[2]:
            try:
                parent_expire_date = datetime.fromisoformat(parent_user[2])
                if parent_expire_date > datetime.now():
                    days_left = (parent_expire_date - datetime.now()).days
                    await message.answer(f"Ваша подписка активна через родительскую подписку. Осталось {days_left} дней.")
                    return
            except ValueError:
                logger.error(f"Некорректный формат даты для родителя пользователя {user_id}: {parent_user[2]}")
    await message.answer("Ваша подписка истекла или отсутствует.")

# ============ Обработчики CallbackQuery ============
@router.callback_query(Text(text="connect_vpn"))
async def handle_connect_vpn(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = normalize_username(callback.from_user.username) if callback.from_user.username else f"id_{user_id}"
    logger.info(f"Пользователь {user_id} (@{username}) запросил подключение VPN")
    # Уведомляем администратора о запросе пользователя
    await notify_admin(
        f"Пользователь @{username} хочет подключить VPN.",
        reply_markup=create_admin_decision_keyboard()
    )
    await callback.answer("Ваш запрос отправлен администратору.", show_alert=True)

@router.callback_query(Text(text="pay_standard"))
async def handle_pay_standard(callback: CallbackQuery):
    user_id = callback.from_user.id
    logger.info(f"Пользователь {user_id} выбрал оплату 50 руб")
    amount = 50  # Стандартная сумма
    await callback.message.answer(
        "Вы выбрали оплату 50 руб. Вы можете перевести средства по номеру +79788030694 или перейти по ссылке для оплаты через Т-Банк:",
        reply_markup=create_payment_keyboard(amount=amount)
    )
    await callback.answer()

    # Здесь должна быть логика подтверждения оплаты через платежный вебхук
    # Для примера обновим сразу после нажатия кнопки
    new_expire_date = datetime.now() + timedelta(days=DAYS_PER_MONTH)  # 30 дней за 50 руб
    await update_expire_date(user_id, new_expire_date.isoformat())
    await update_total_paid(user_id, amount)
    logger.info(f"Пользователь {user_id} оплатил {amount} руб. Подписка обновлена до {new_expire_date.isoformat()}")
    await callback.message.answer("Оплата подтверждена. Подписка обновлена до " +
                                  new_expire_date.strftime('%Y-%m-%d') + ".", show_alert=True)

@router.callback_query(Text(text="pay_custom_amount"))
async def handle_pay_custom_amount(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    logger.info(f"Пользователь {user_id} выбрал оплату своей суммы")
    await callback.message.answer("Введите сумму оплаты в рублях:")
    await state.set_state(UserStates.waiting_for_custom_amount)
    await callback.answer()

@router.callback_query(Text(text="check_subscription"))
async def handle_check_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    logger.info(f"Пользователь {user_id} запросил проверку подписки через кнопку")
    user = await get_user(user_id)
    if not user:
        await callback.message.answer("Вы не зарегистрированы. Отправьте /start для регистрации.")
        await callback.answer()
        return

    expire_date_str = user[2]
    if expire_date_str:
        try:
            expire_date = datetime.fromisoformat(expire_date_str)
            if expire_date > datetime.now():
                days_left = (expire_date - datetime.now()).days
                await callback.message.answer(f"Ваша подписка активна. Осталось {days_left} дней.")
                await callback.answer()
                return
        except ValueError:
            logger.error(f"Некорректный формат даты для пользователя {user_id}: {expire_date_str}")

    # Проверяем подписку родителя
    parent_user_id = user[4] if len(user) > 4 else None  # parent_user_id
    if parent_user_id:
        parent_user = await get_user(parent_user_id)
        if parent_user and parent_user[2]:
            try:
                parent_expire_date = datetime.fromisoformat(parent_user[2])
                if parent_expire_date > datetime.now():
                    days_left = (parent_expire_date - datetime.now()).days
                    await callback.message.answer(f"Ваша подписка активна через родительскую подписку. Осталось {days_left} дней.")
                    await callback.answer()
                    return
            except ValueError:
                logger.error(f"Некорректный формат даты для родителя пользователя {user_id}: {parent_user[2]}")
    await callback.message.answer("Ваша подписка истекла или отсутствует.")
    await callback.answer()

# Обработчики кнопок принятия и отклонения
@router.callback_query(Text(text="admin_accept"))
async def handle_admin_accept(callback: CallbackQuery, state: FSMContext):
    # Извлекаем username из текста сообщения
    message_text = callback.message.text
    logger.info(f"Администратор принял запрос: {message_text}")
    try:
        # Используем регулярное выражение для извлечения username
        match = re.search(r'@(\w+)', message_text)
        if not match:
            raise IndexError
        username = match.group(1).lower()
    except IndexError:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        logger.error("Не удалось извлечь username из сообщения")
        return

    user = await get_user_by_username(username)
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        logger.error(f"Пользователь @{username} не найден в базе данных")
        return

    user_id = user[0]
    # Сохраняем данные в FSM
    await state.update_data(action="send_message", target_user_id=user_id)
    await state.set_state(AdminStates.waiting_for_send_message)
    await callback.message.answer("Введите сообщение для пользователя:")
    
    # Уведомляем пользователя, чтобы он нажал /start
    try:
        await bot.send_message(user_id, "Ваш запрос был одобрен администратором. Пожалуйста, нажмите /start для завершения настройки.")
        logger.info(f"Отправлено уведомление пользователю @{username} для нажатия /start")
    except TelegramBadRequest:
        logger.error(f"Не удалось отправить уведомление пользователю @{username}.")
    await callback.answer()

@router.callback_query(Text(text="admin_reject"))
async def handle_admin_reject(callback: CallbackQuery):
    # Извлекаем username из текста сообщения
    message_text = callback.message.text
    logger.info(f"Администратор отклонил запрос: {message_text}")
    try:
        # Используем регулярное выражение для извлечения username
        match = re.search(r'@(\w+)', message_text)
        if not match:
            raise IndexError
        username = match.group(1).lower()
    except IndexError:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        logger.error("Не удалось извлечь username из сообщения")
        return

    user = await get_user_by_username(username)
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        logger.error(f"Пользователь @{username} не найден в базе данных")
        return

    user_id = user[0]
    try:
        await bot.send_message(user_id, "Ваш запрос на подключение VPN был отклонён.")
        logger.info(f"Отправлено уведомление об отклонении пользователю {user_id}")
    except TelegramBadRequest:
        logger.error(f"Не удалось отправить уведомление пользователю @{username}.")

    await callback.message.answer(f"Запрос пользователя @{username} отклонён.")
    await callback.answer()

# Обработчик отправки сообщения администратором
@router.message(StateFilter(AdminStates.waiting_for_send_message))
async def admin_send_message(message: Message, state: FSMContext):
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    text = message.text.strip()
    if not text:
        await message.answer("Сообщение не может быть пустым. Пожалуйста, введите сообщение:")
        return
    # Сохраняем текст в FSM
    await state.update_data(message_text=text)
    # Предлагаем отправить файл
    keyboard = create_send_file_keyboard()
    await message.answer("Хотите отправить файл вместе с сообщением?", reply_markup=keyboard)
    await state.set_state(AdminStates.awaiting_file)
    logger.info(f"Администратор подготовился отправить сообщение пользователю {target_user_id}")

# Обработчик кнопки "Да" для отправки файла
@router.callback_query(Text(text="send_file_yes"))
async def handle_send_file_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    action = data.get("action")
    if action != "send_message":
        await callback.answer("Нет действия для выполнения.", show_alert=True)
        return
    await state.set_state(AdminStates.awaiting_file_with_file)
    await callback.message.answer("Пожалуйста, отправьте файл для пользователя.")
    await callback.answer()

# Обработчик кнопки "Нет" для отправки файла
@router.callback_query(Text(text="send_file_no"))
async def handle_send_file_no(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    action = data.get("action")
    if action != "send_message":
        await callback.answer("Нет действия для выполнения.", show_alert=True)
        return
    target_user_id = data.get("target_user_id")
    message_text = data.get("message_text")
    try:
        await bot.send_message(target_user_id, message_text)
        await callback.message.answer("Сообщение отправлено пользователю.")
        logger.info(f"Администратор отправил сообщение пользователю {target_user_id}")
    except TelegramBadRequest:
        await callback.message.answer("Не удалось отправить сообщение пользователю.")
        logger.error(f"Не удалось отправить сообщение пользователю {target_user_id}")
    await state.clear()
    await callback.answer()

# Обработчик получения файла от администратора
@router.message(StateFilter(AdminStates.awaiting_file_with_file), F.content_type.in_([ContentType.DOCUMENT, ContentType.PHOTO]))
async def handle_admin_file(message: Message, state: FSMContext):
    data = await state.get_data()
    action = data.get("action")
    if action != "send_message":
        await message.answer("Нет действия для выполнения.")
        return
    target_user_id = data.get("target_user_id")
    message_text = data.get("message_text")
    document = message.document.file_id if message.document else message.photo[-1].file_id

    try:
        # Отправляем файл и сообщение пользователю
        await bot.send_document(target_user_id, document, caption=message_text)
        await message.answer("Файл и сообщение отправлены пользователю.")
        logger.info(f"Администратор отправил файл и сообщение пользователю {target_user_id}")
    except Exception as e:
        logger.error(f"Не удалось отправить файл пользователю {target_user_id}: {e}")
        await message.answer("Не удалось отправить файл пользователю.")
    await state.clear()

# Обработчик пользовательских сообщений для оплаты своей суммы
@router.message(StateFilter(UserStates.waiting_for_custom_amount), F.content_type == ContentType.TEXT)
async def process_user_custom_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        amount = int(message.text.strip())
        if amount < 1:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите корректную сумму оплаты в рублях:")
        logger.warning(f"Пользователь {user_id} ввёл некорректную сумму: {message.text}")
        return

    # Сохраняем сумму в состоянии
    await state.update_data(custom_amount=amount)

    # Отправляем кнопку оплаты с указанной суммой
    payment_url = f"{PAYMENT_LINK}?amount={amount}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Оплатить {amount} руб", url=payment_url)]
    ])
    await message.answer(
        f"Вы выбрали оплату {amount} руб. Вы можете перевести средства по номеру +79788030694 или перейти по ссылке для оплаты через Т-Банк:",
        reply_markup=keyboard
    )
    await message.answer("После оплаты нажмите /check для подтверждения.")
    
    # Здесь должна быть логика подтверждения оплаты через платёжный вебхук
    # Для примера обновим сразу после отправки кнопки
    days = rub_to_days(amount)
    new_expire_date = datetime.now() + timedelta(days=days)
    await update_expire_date(user_id, new_expire_date.isoformat())
    await update_total_paid(user_id, amount)
    logger.info(f"Пользователь {user_id} оплатил {amount} руб. Подписка обновлена до {new_expire_date.isoformat()}")
    await message.answer(f"Оплата {amount} руб. подтверждена. Подписка обновлена до {new_expire_date.strftime('%Y-%m-%d')}.")
    await state.clear()

# ============ Администраторские команды ============
@router.message(Command("check_users"))
async def admin_check_users(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("Эта команда доступна только администратору.")
        logger.warning(f"Пользователь {message.from_user.id} попытался использовать admin команду /check_users")
        return

    logger.info("Администратор запросил список пользователей")
    users = await get_all_users()
    active_users = []
    expired_users = []
    now = datetime.now()

    for user in users:
        user_id, username, expire_date_str, _, parent_user_id = user
        username_display = f"@{username}" if username else f"id_{user_id}"
        if expire_date_str:
            try:
                expire_date = datetime.fromisoformat(expire_date_str)
                if expire_date > now:
                    active_users.append(f"{username_display}: {expire_date.strftime('%Y-%m-%d')}")
                else:
                    expired_users.append(f"{username_display}: подписка истекла")
            except ValueError:
                expired_users.append(f"{username_display}: неверная дата подписки")
        else:
            expired_users.append(f"{username_display}: подписка отсутствует")

    active_text = "\n".join(active_users) if active_users else "Нет активных подписчиков."
    expired_text = "\n".join(expired_users) if expired_users else "Нет пользователей с истёкшей подпиской."

    await message.answer(f"📋 <b>Активные пользователи:</b>\n{active_text}\n\n⛔ <b>Истёкшие подписки:</b>\n{expired_text}")
    logger.info("Отправлен список пользователей администратору")

@router.message(Command("sum_payments"))
async def admin_sum_payments(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("Эта команда доступна только администратору.")
        logger.warning(f"Пользователь {message.from_user.id} попытался использовать admin команду /sum_payments")
        return

    logger.info("Администратор запросил сумму оплат")
    users = await get_all_users()
    total_sum = sum(user[3] for user in users if user[3] is not None)  # total_paid
    await message.answer(f"💰 <b>Общая сумма оплат:</b> {total_sum} руб.")
    logger.info(f"Общая сумма оплат: {total_sum} руб.")

@router.message(Command("reset_subscription"))
async def admin_reset_subscription_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("Эта команда доступна только администратору.")
        logger.warning(f"Пользователь {message.from_user.id} попытался использовать admin команду /reset_subscription")
        return

    await message.answer("Введите @username пользователя, чью подписку нужно сбросить.")
    await state.set_state(AdminStates.waiting_for_reset_username)
    logger.info("Администратор инициировал сброс подписки")

@router.message(Command("gift_subscription"))
async def admin_gift_subscription_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("Эта команда доступна только администратору.")
        logger.warning(f"Пользователь {message.from_user.id} попытался использовать admin команду /gift_subscription")
        return

    await message.answer(
        "Введите @username пользователя, которому хотите подарить подписку, и количество дней через пробел (например: @danikpon 30)."
    )
    await state.set_state(AdminStates.waiting_for_gift_subscription)
    logger.info("Администратор инициировал подарочную подписку")

@router.message(Command("send_message"))
async def admin_send_message_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("Эта команда доступна только администратору.")
        logger.warning(f"Пользователь {message.from_user.id} попытался использовать admin команду /send_message")
        return

    await message.answer("Введите @username и сообщение через пробел...")
    await state.set_state(AdminStates.waiting_for_send_message)
    logger.info("Администратор инициировал отправку сообщения пользователю")

@router.message(StateFilter(AdminStates.waiting_for_send_message))
async def process_send_message(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) != 2:
            ...
        username_input, text = parts
        username = normalize_username(username_input)
        logger.info(f"[DEBUG] Сейчас будем вызывать get_user_by_username('{username}')")  # <-- добавили
    except ValueError:
        ...
    user = await get_user_by_username(username)

@router.message(Command("broadcast"))
async def admin_broadcast_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("Эта команда доступна только администратору.")
        logger.warning(f"Пользователь {message.from_user.id} попытался использовать admin команду /broadcast")
        return

    await message.answer("Введите текст для рассылки всем активным подписчикам.")
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    logger.info("Администратор инициировал рассылку")

# ============ Обработчики сообщений для администратора ============
@router.message(StateFilter(AdminStates.waiting_for_send_message))
async def process_send_message(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError
        username_input, text = parts
        username = normalize_username(username_input)
    except ValueError:
        await message.answer("Неверный формат. Используйте: @username сообщение.")
        logger.warning(f"Администратор ввёл неверный формат для отправки сообщения: {message.text}")
        return
    user = await get_user_by_username(username)
    if not user:
        await message.answer(f"Пользователь @{username} не найден.")
        logger.warning(f"Администратор попытался отправить сообщение несуществующему пользователю @{username}")
        await state.clear()
        return
    user_id = user[0]
    # Сохраняем текст в FSM
    await state.update_data(message_text=text, target_user_id=user_id)
    # Предлагаем отправить файл
    keyboard = create_send_file_keyboard()
    await message.answer("Хотите отправить файл вместе с сообщением?", reply_markup=keyboard)
    await state.set_state(AdminStates.awaiting_file)
    logger.info(f"Администратор подготовился отправить сообщение пользователю {user_id}")

@router.message(StateFilter(AdminStates.waiting_for_reset_username))
async def process_reset_subscription(message: Message, state: FSMContext):
    username_input = message.text.strip()
    username = normalize_username(username_input)
    user = await get_user_by_username(username)
    if not user:
        await message.answer(f"Пользователь @{username} не найден.")
        logger.warning(f"Администратор попытался сбросить подписку для несуществующего пользователя @{username}")
        await state.clear()
        return
    user_id = user[0]
    await update_expire_date(user_id, None)
    await update_total_paid(user_id, 0)  # Опционально сбросить общую сумму оплат
    await message.answer(f"Подписка пользователя @{username} сброшена.")
    await notify_admin(f"Администратор сбросил подписку пользователя @{username}.")
    # Уведомляем пользователя о сбросе подписки
    try:
        await bot.send_message(user_id, "⚠️ Ваша подписка была сброшена администратором.")
        logger.info(f"Отправлено уведомление о сбросе подписки пользователю {user_id}")
    except TelegramBadRequest:
        logger.error(f"Не удалось отправить уведомление пользователю @{username}.")
    await state.clear()

@router.message(StateFilter(AdminStates.waiting_for_gift_subscription))
async def process_gift_subscription(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            raise ValueError
        username_input, days = parts
        days = int(days)
        if days < 1:
            raise ValueError
    except ValueError:
        await message.answer("Неверный формат. Используйте: @username количество_дней.")
        logger.warning(f"Администратор ввёл неверный формат для подарочной подписки: {message.text}")
        return
    username = normalize_username(username_input)
    user = await get_user_by_username(username)
    if not user:
        await message.answer(f"Пользователь @{username} не найден.")
        logger.warning(f"Администратор попытался подарить подписку несуществующему пользователю @{username}")
        await state.clear()
        return
    user_id = user[0]
    new_expire_date = datetime.now() + timedelta(days=days)
    await update_expire_date(user_id, new_expire_date.isoformat())
    # Опционально можно учесть стоимость, если подарок не бесплатный
    await update_total_paid(user_id, 0)
    await message.answer(f"Подарена подписка пользователю @{username} на {days} дней.")
    await notify_admin(f"Администратор подарил {days} дней подписки пользователю @{username}.")
    # Уведомляем пользователя о подарке
    try:
        await bot.send_message(user_id, f"🎁 Вам подарена подписка на {days} дней!")
        logger.info(f"Отправлено уведомление о подарке пользователю {user_id}")
    except TelegramBadRequest:
        logger.error(f"Не удалось отправить уведомление пользователю @{username}.")
    await state.clear()

@router.message(StateFilter(AdminStates.waiting_for_broadcast_text))
async def process_broadcast_text(message: Message, state: FSMContext):
    text = message.text.strip()
    users = await get_all_users()
    active_users = [user for user in users if user[2] and datetime.fromisoformat(user[2]) > datetime.now()]
    success = 0
    for user in active_users:
        user_id = user[0]
        try:
            await bot.send_message(user_id, text)
            await bot.send_message(user_id, "📢 Вы получили общее сообщение от администратора.")
            success += 1
            logger.info(f"Отправлено сообщение пользователю {user_id}")
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
    await message.answer(f"Рассылка завершена. Уведомлено пользователей: {success}.")
    await notify_admin(f"Администратор сделал рассылку. Уведомлено пользователей: {success}.")
    await state.clear()
    logger.info(f"Рассылка завершена. Уведомлено пользователей: {success}.")

# ============ Обработчик
# === ИЗМЕНЕНИЕ НАЧАЛО ===
# Добавляем функцию проверки, у кого подписка истекает ровно через 1 день
async def check_subscriptions_expiring():
    """
    Ежедневная проверка: если у пользователя подписка заканчивается завтра,
    отправить уведомление ему и администратору.
    """
    now = datetime.now()
    users = await get_all_users()
    for user in users:
        user_id, username, expire_date_str, total_paid, parent_user_id = user
        if expire_date_str:
            try:
                expire_date = datetime.fromisoformat(expire_date_str)
                # Если до окончания подписки ровно 1 день
                if (expire_date - now).days == 1:
                    # Уведомляем пользователя (если это не админ)
                    if user_id != ADMIN_CHAT_ID:
                        try:
                            await bot.send_message(
                                user_id,
                                f"⚠️ Ваша подписка заканчивается завтра!\n"
                                f"Дата окончания: {expire_date.strftime('%Y-%m-%d')}.\n"
                                f"Продлите подписку, чтобы VPN не отключился."
                            )
                        except Exception as e:
                            logger.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

                    # Уведомляем администратора
                    username_display = f"@{username}" if username else f"id_{user_id}"
                    await bot.send_message(
                        ADMIN_CHAT_ID,
                        f"⚠️ У пользователя {username_display} подписка истекает завтра ({expire_date.strftime('%Y-%m-%d')})."
                    )
            except ValueError:
                logger.error(f"Некорректный формат даты у пользователя {user_id}: {expire_date_str}")
# === ИЗМЕНЕНИЕ КОНЕЦ ===
# ============ Обработчик ошибок ============
@router.errors()
async def global_error_handler(update: Update, exception: Exception):
    await notify_admin(f"Произошла ошибка: {exception}")
    logger.error(f"Ошибка: {exception}")
    return True  # Ошибка обработана

# ============ Запуск бота ============
async def main():
    # Инициализация базы данных
    await init_db()

    # Настройка команд бота (только для пользователей, админ-команды скрыты)
    user_commands = [
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="check", description="Проверить подписку"),
    ]
    await bot.set_my_commands(user_commands)

    # === ИЗМЕНЕНИЕ НАЧАЛО: добавляем задачу в планировщик ===
    scheduler.add_job(
        check_subscriptions_expiring,
        "cron", 
        hour=14,      # запуск в полночь
        minute=4,    # можно менять на любое удобное время
    )
    # === ИЗМЕНЕНИЕ КОНЕЦ ===

    # Запуск планировщика задач
    scheduler.start()

    # Запуск бота
    await dp.start_polling(bot, router=router)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")
