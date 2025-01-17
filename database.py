# database.py
import aiosqlite
import logging

DB_PATH = "users.db"
logger = logging.getLogger(__name__)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT UNIQUE,
                expire_date TEXT,
                total_paid REAL DEFAULT 0,
                parent_user_id INTEGER
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована.")

async def add_user(user_id: int, username: str, parent_user_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO users (user_id, username, parent_user_id)
                VALUES (?, ?, ?)
            """, (user_id, username, parent_user_id))
            await db.commit()
            logger.info(f"Пользователь @{username} добавлен в базу данных.")
        except aiosqlite.IntegrityError:
            logger.warning(f"Пользователь @{username} уже существует в базе данных.")

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, username, expire_date, total_paid, parent_user_id
            FROM users
            WHERE user_id = ?
        """, (user_id,))
        user = await cursor.fetchone()
        return user

async def get_user_by_username(username: str):
    # === Добавляем отладочные логи для диагностики ===
    logger.info(f"[DEBUG] Пытаемся найти пользователя по username='{username}'")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, username, expire_date, total_paid, parent_user_id
            FROM users
            WHERE username = ?
        """, (username,))

        user = await cursor.fetchone()

        # Логируем, что вернул запрос
        logger.info(f"[DEBUG] Результат поиска get_user_by_username('{username}'): {user}")

        return user

async def update_expire_date(user_id: int, expire_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET expire_date = ?
            WHERE user_id = ?
        """, (expire_date, user_id))
        await db.commit()
    logger.info(f"Дата истечения подписки пользователя {user_id} обновлена до {expire_date}")

async def update_total_paid(user_id: int, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET total_paid = total_paid + ?
            WHERE user_id = ?
        """, (amount, user_id))
        await db.commit()
    logger.info(f"Общая сумма оплат пользователя {user_id} обновлена на {amount} руб.")

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, username, expire_date, total_paid, parent_user_id
            FROM users
        """)
        users = await cursor.fetchall()
        return users
