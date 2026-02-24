import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import base58
from aiogram import Bot
from eth_account import Account
from solders.keypair import Keypair

from src.logger import logger

DB_PATH = "data/bot.db"
PRIVATE_CHAT_ID = int(os.getenv("PRIVATE_CHAT_ID", "0"))
PRIVATE_GROUP_ID = int(os.getenv("PRIVATE_GROUP_ID", "0"))  # Используем имя переменной с опечаткой из .env

SUBSCRIPTION_PLANS = {
    "1month": {"label": "1 месяц", "price": 50, "duration_days": 30},
    "3months": {"label": "3 месяца", "price": 120, "duration_days": 90},
    "forever": {"label": "Навсегда", "price": 250, "duration_days": None},
}

SUPPORTED_NETWORKS = {
    "base": {
        "name": "Base",
        "type": "evm",
        "chain_id": 8453,
        "rpc_url_env": "BASE_RPC_URL",
        "rpc_url_default": "https://mainnet.base.org",
    },
    "arbitrum": {
        "name": "Arbitrum One",
        "type": "evm",
        "chain_id": 42161,
        "rpc_url_env": "ARBITRUM_RPC_URL",
        "rpc_url_default": "https://arb1.arbitrum.io/rpc",
    },
    "tron": {
        "name": "Tron (TRC-20)",
        "type": "tron",
        "api_url_env": "TRONGRID_API_URL",
        "api_url_default": "https://api.trongrid.io",
    },
    "solana": {
        "name": "Solana",
        "type": "solana",
        "rpc_url_env": "SOLANA_RPC_URL",
        "rpc_url_default": "https://api.mainnet-beta.solana.com",
    },
}

SUPPORTED_TOKENS = {
    "usdc": {
        "name": "USDC",
        "decimals": 6,
        "addresses": {
            "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "arbitrum": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        },
    },
    "usdt": {
        "name": "USDT",
        "decimals": 6,
        "addresses": {
            "base": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
            "arbitrum": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
            "tron": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            "solana": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        },
    },
}


def eth_to_tron_address(eth_address: str) -> str:
    """Конвертирует Ethereum-адрес в Tron-адрес (base58check с префиксом 0x41)."""
    addr_bytes = bytes.fromhex(eth_address[2:])
    prefixed = b'\x41' + addr_bytes
    h1 = hashlib.sha256(prefixed).digest()
    h2 = hashlib.sha256(h1).digest()
    return base58.b58encode(prefixed + h2[:4]).decode()




def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            private_key TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            plan TEXT NOT NULL,
            network TEXT,
            token TEXT,
            status TEXT DEFAULT 'pending',
            tx_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            network TEXT NOT NULL,
            token TEXT NOT NULL,
            from_block INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    # Создать таблицу master_wallets
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS master_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            network TEXT UNIQUE NOT NULL,
            wallet_address TEXT NOT NULL,
            private_key TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Миграция: добавляем колонки для Solana-кошелька
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    if "sol_wallet_address" not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN sol_wallet_address TEXT")
    if "sol_private_key" not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN sol_private_key TEXT")

    # Миграция: добавить tx_hash в payment_sessions
    existing_ps = {row[1] for row in cursor.execute("PRAGMA table_info(payment_sessions)").fetchall()}
    if "tx_hash" not in existing_ps:
        cursor.execute("ALTER TABLE payment_sessions ADD COLUMN tx_hash TEXT")

    # Создать таблицу user_profiles для хранения информации о всех пользователях
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            last_interaction TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Создать таблицу для реферальных ссылок
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referral_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            max_uses INTEGER,
            current_uses INTEGER DEFAULT 0,
            custom_prices TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Создать таблицу для отслеживания использования реферальных ссылок
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referral_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            referral_code TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id)
        )
    """)

    # Создать таблицу для отслеживания уведомлений об истечении подписки
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expiry_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subscription_id INTEGER NOT NULL,
            notification_type TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id),
            FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
        )
    """)

    # Миграция: добавить колонку name в referral_links
    existing_ref = {row[1] for row in cursor.execute("PRAGMA table_info(referral_links)").fetchall()}
    if "name" not in existing_ref:
        cursor.execute("ALTER TABLE referral_links ADD COLUMN name TEXT")

    # Миграция: добавить колонку owner_user_id в referral_links для пользовательских ссылок
    if "owner_user_id" not in existing_ref:
        cursor.execute("ALTER TABLE referral_links ADD COLUMN owner_user_id INTEGER")

    # Миграция: добавить колонки для бесплатных реферальных ссылок
    if "free_days" not in existing_ref:
        cursor.execute("ALTER TABLE referral_links ADD COLUMN free_days INTEGER")
    if "is_instant" not in existing_ref:
        cursor.execute("ALTER TABLE referral_links ADD COLUMN is_instant INTEGER DEFAULT 0")

    # Миграция: добавить реферальный баланс и счётчик в профили пользователей
    existing_profiles = {row[1] for row in cursor.execute("PRAGMA table_info(user_profiles)").fetchall()}
    if "referral_balance" not in existing_profiles:
        cursor.execute("ALTER TABLE user_profiles ADD COLUMN referral_balance REAL DEFAULT 0")
    if "referral_paid_count" not in existing_profiles:
        cursor.execute("ALTER TABLE user_profiles ADD COLUMN referral_paid_count INTEGER DEFAULT 0")

    # Таблица запросов на вывод реферального баланса
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS withdrawal_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            evm_address TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            tx_hash TEXT,
            notification_message_id INTEGER,
            notification_chat_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id)
        )
    """)

    # Миграция: добавить колонку network в withdrawal_requests
    existing_wr = {row[1] for row in cursor.execute("PRAGMA table_info(withdrawal_requests)").fetchall()}
    if "network" not in existing_wr:
        cursor.execute("ALTER TABLE withdrawal_requests ADD COLUMN network TEXT")

    # Миграция: переименовать trc20_address → evm_address в withdrawal_requests
    try:
        cursor.execute("ALTER TABLE withdrawal_requests RENAME COLUMN trc20_address TO evm_address")
    except Exception:
        pass

    # Таблица кошелька для отправки выплат пользователям
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS withdrawal_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address TEXT NOT NULL,
            encrypted_private_key TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

    # Генерируем мастер-кошельки если их нет
    generate_master_wallets()
    # Инициализируем кошелёк для вывода из .env если он не в БД
    _init_withdrawal_wallet()

    logger.info("Database initialized")


def generate_master_wallets() -> None:
    """Генерирует мастер-кошельки для всех поддерживаемых сетей при первом запуске."""
    conn = _get_connection()
    cursor = conn.cursor()

    # Создаём один EVM кошелёк
    evm_account = Account.create()
    evm_address = evm_account.address
    evm_private_key = evm_account.key.hex()

    # Создаём Solana кошелёк
    sol_kp = Keypair()
    sol_address = str(sol_kp.pubkey())
    sol_private_key = base58.b58encode(bytes(sol_kp)).decode()

    # Сохраняем EVM кошельки (один ключ для base, arbitrum)
    for network in ["base", "arbitrum"]:
        cursor.execute("SELECT id FROM master_wallets WHERE network = ?", (network,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO master_wallets (network, wallet_address, private_key) VALUES (?, ?, ?)",
                (network, evm_address, evm_private_key),
            )
            logger.info(f"Generated master wallet for {network}: {evm_address}")

    # Сохраняем Tron (тот же ключ, конвертированный адрес)
    tron_address = eth_to_tron_address(evm_address)
    cursor.execute("SELECT id FROM master_wallets WHERE network = ?", ("tron",))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO master_wallets (network, wallet_address, private_key) VALUES (?, ?, ?)",
            ("tron", tron_address, evm_private_key),
        )
        logger.info(f"Generated master wallet for tron: {tron_address}")

    # Сохраняем Solana
    cursor.execute("SELECT id FROM master_wallets WHERE network = ?", ("solana",))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO master_wallets (network, wallet_address, private_key) VALUES (?, ?, ?)",
            ("solana", sol_address, sol_private_key),
        )
        logger.info(f"Generated master wallet for solana: {sol_address}")

    conn.commit()
    conn.close()


def _init_withdrawal_wallet() -> None:
    """Сохраняет публичный EVM-адрес кошелька вывода в БД (приватник только в .env)."""
    wallet_key = os.getenv("WITHDRAWAL_WALLET_KEY", "").strip()
    if not wallet_key:
        return  # Не настроено — пропускаем

    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM withdrawal_wallets LIMIT 1")
    if cursor.fetchone():
        conn.close()
        return  # Уже инициализирован

    key_hex = wallet_key.lstrip("0x")
    try:
        evm_address = Account.from_key(f"0x{key_hex}").address
    except Exception as e:
        logger.error(f"Не удалось вычислить EVM-адрес из WITHDRAWAL_WALLET_KEY: {e}")
        conn.close()
        return

    # Приватный ключ в БД не хранится — только публичный EVM-адрес
    cursor.execute(
        "INSERT INTO withdrawal_wallets (wallet_address, encrypted_private_key) VALUES (?, '')",
        (evm_address,),
    )
    conn.commit()
    conn.close()
    logger.info(f"Публичный EVM-адрес кошелька вывода сохранён в БД: {evm_address}")


def get_withdrawal_wallet() -> dict | None:
    """Возвращает EVM-адрес и приватный ключ кошелька для вывода.

    Приватный ключ всегда берётся из .env (WITHDRAWAL_WALLET_KEY).
    """
    wallet_key = os.getenv("WITHDRAWAL_WALLET_KEY", "").strip()
    if not wallet_key:
        logger.error("WITHDRAWAL_WALLET_KEY не задан в .env")
        return None

    key_hex = wallet_key.lstrip("0x")
    try:
        evm_address = Account.from_key(f"0x{key_hex}").address
    except Exception as e:
        logger.error(f"Не удалось вычислить EVM-адрес из WITHDRAWAL_WALLET_KEY: {e}")
        return None

    return {"address": evm_address, "private_key": key_hex}


def get_referral_balance(user_id: int) -> float:
    """Возвращает реферальный баланс пользователя в долларах."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT referral_balance FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return float(row["referral_balance"]) if row and row["referral_balance"] else 0.0


def add_referral_balance(user_id: int, amount: float) -> None:
    """Начисляет сумму на реферальный баланс пользователя."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE user_profiles SET referral_balance = referral_balance + ? WHERE user_id = ?",
        (amount, user_id),
    )
    conn.commit()
    conn.close()
    logger.info(f"Начислено {amount}$ на реферальный баланс пользователя {user_id}")


def get_referral_paid_count(user_id: int) -> int:
    """Возвращает количество рефералов, которые совершили оплату."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT referral_paid_count FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return int(row["referral_paid_count"]) if row and row["referral_paid_count"] else 0


def get_referral_percent(paid_count: int) -> int:
    """Возвращает процент вознаграждения в зависимости от количества оплативших рефералов.

    0 рефералов → 20%, 1 реферал → 25%, 2+ рефералов → 30%.
    """
    if paid_count == 0:
        return 20
    elif paid_count == 1:
        return 25
    else:
        return 30


def increment_referral_paid_count(user_id: int) -> None:
    """Увеличивает счётчик оплативших рефералов на 1."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE user_profiles SET referral_paid_count = referral_paid_count + 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def deduct_referral_balance(user_id: int, amount: float) -> bool:
    """Списывает сумму с реферального баланса. Возвращает True если успешно."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT referral_balance FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    row = cursor.fetchone()
    balance = float(row["referral_balance"]) if row and row["referral_balance"] else 0.0

    if balance < amount:
        conn.close()
        return False

    cursor.execute(
        "UPDATE user_profiles SET referral_balance = referral_balance - ? WHERE user_id = ?",
        (amount, user_id),
    )
    conn.commit()
    conn.close()
    logger.info(f"Списано {amount}$ с реферального баланса пользователя {user_id}")
    return True


def create_withdrawal_request(user_id: int, amount: float, evm_address: str, network: str) -> int | None:
    """Создаёт запрос на вывод и списывает сумму с баланса.

    Returns:
        ID запроса или None если ошибка (недостаточно средств или уже есть pending-запрос)
    """
    conn = _get_connection()
    cursor = conn.cursor()

    # Проверяем баланс
    cursor.execute(
        "SELECT referral_balance FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    row = cursor.fetchone()
    balance = float(row["referral_balance"]) if row and row["referral_balance"] else 0.0

    if balance < amount:
        conn.close()
        logger.warning(f"Пользователь {user_id} запросил вывод {amount}$ при балансе {balance}$")
        return None

    # Проверяем нет ли уже pending-запроса
    cursor.execute(
        "SELECT id FROM withdrawal_requests WHERE user_id = ? AND status IN ('pending', 'confirming')",
        (user_id,),
    )
    if cursor.fetchone():
        conn.close()
        logger.warning(f"Пользователь {user_id} уже имеет активный запрос на вывод")
        return None

    # Списываем баланс
    cursor.execute(
        "UPDATE user_profiles SET referral_balance = referral_balance - ? WHERE user_id = ?",
        (amount, user_id),
    )

    # Создаём запрос
    cursor.execute(
        "INSERT INTO withdrawal_requests (user_id, amount, evm_address, network) VALUES (?, ?, ?, ?)",
        (user_id, amount, evm_address, network),
    )
    request_id = cursor.lastrowid
    conn.commit()
    conn.close()

    logger.info(f"Создан запрос на вывод #{request_id} от пользователя {user_id}: {amount}$ → {evm_address} ({network})")
    return request_id  # type: ignore


def get_withdrawal_request(request_id: int) -> dict | None:
    """Возвращает данные запроса на вывод."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, amount, evm_address, network, status, tx_hash, "
        "notification_message_id, notification_chat_id "
        "FROM withdrawal_requests WHERE id = ?",
        (request_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_withdrawal_request(
    request_id: int,
    status: str | None = None,
    tx_hash: str | None = None,
    notification_message_id: int | None = None,
    notification_chat_id: int | None = None,
) -> None:
    """Обновляет поля запроса на вывод."""
    updates = []
    values = []

    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if tx_hash is not None:
        updates.append("tx_hash = ?")
        values.append(tx_hash)
    if notification_message_id is not None:
        updates.append("notification_message_id = ?")
        values.append(notification_message_id)
    if notification_chat_id is not None:
        updates.append("notification_chat_id = ?")
        values.append(notification_chat_id)

    if not updates:
        return

    conn = _get_connection()
    cursor = conn.cursor()
    values.append(request_id)
    cursor.execute(
        f"UPDATE withdrawal_requests SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    conn.commit()
    conn.close()


def get_master_wallet_address(network: str) -> str | None:
    """Возвращает адрес мастер-кошелька для указанной сети."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT wallet_address FROM master_wallets WHERE network = ?", (network,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row["wallet_address"]
    return None


def get_master_wallet(network: str) -> dict | None:
    """Возвращает полные данные мастер-кошелька (address + private_key)."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT wallet_address, private_key FROM master_wallets WHERE network = ?",
        (network,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"address": row["wallet_address"], "private_key": row["private_key"]}
    return None


def update_user_profile(user_id: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
    """Обновляет или создает профиль пользователя."""
    from datetime import datetime, timezone

    conn = _get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM user_profiles WHERE user_id = ?", (user_id,))
    exists = cursor.fetchone()

    now = datetime.now(timezone.utc).isoformat()

    if exists:
        cursor.execute(
            "UPDATE user_profiles SET username = ?, first_name = ?, last_name = ?, last_interaction = ? WHERE user_id = ?",
            (username, first_name, last_name, now, user_id),
        )
    else:
        cursor.execute(
            "INSERT INTO user_profiles (user_id, username, first_name, last_name, last_interaction) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, last_name, now),
        )

    conn.commit()
    conn.close()

    # Создаём личную реферальную ссылку при первом создании профиля
    if not exists:
        ensure_user_referral_link(user_id)
        logger.info(f"Created personal referral link for new user {user_id}")


def get_user_profile(user_id: int) -> dict | None:
    """Возвращает профиль пользователя."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, username, first_name, last_name FROM user_profiles WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def create_referral_link(
    max_uses: int | None,
    custom_prices: str | None,
    name: str | None = None,
    free_days: int | None = None,
    is_instant: bool = False
) -> str:
    """Создает реферальную ссылку с уникальным кодом.

    Args:
        max_uses: Лимит использований (None = безлимит)
        custom_prices: Кастомные цены ("35,90,200" или None)
        name: Название ссылки для идентификации
        free_days: Количество бесплатных дней подписки (None = обычная платная ссылка)
        is_instant: Флаг мгновенной активации (True = активировать сразу при переходе)

    Returns:
        Уникальный код ссылки
    """
    import secrets

    # Генерируем уникальный код
    code = secrets.token_urlsafe(8)

    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO referral_links (code, max_uses, custom_prices, name, free_days, is_instant) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (code, max_uses, custom_prices, name, free_days, 1 if is_instant else 0)
    )
    conn.commit()
    conn.close()

    logger.info(f"Created referral link: code={code}, name={name}, max_uses={max_uses}, "
                f"custom_prices={custom_prices}, free_days={free_days}, is_instant={is_instant}")
    return code


def get_referral_link(code: str) -> dict | None:
    """Получает информацию о реферальной ссылке."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, code, max_uses, current_uses, custom_prices, is_active, name, free_days, is_instant "
        "FROM referral_links WHERE code = ?",
        (code,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def use_referral_link(user_id: int, code: str) -> bool:
    """Регистрирует использование реферальной ссылки. Возвращает True если успешно."""
    conn = _get_connection()
    cursor = conn.cursor()

    # Проверяем, не использовал ли уже пользователь эту ссылку
    cursor.execute(
        "SELECT id FROM referral_usage WHERE user_id = ? AND referral_code = ?",
        (user_id, code)
    )
    if cursor.fetchone():
        logger.info(f"User {user_id} already used referral code {code}")
        conn.close()
        return False  # Уже использовал

    # Получаем информацию о ссылке
    cursor.execute(
        "SELECT max_uses, current_uses, is_active FROM referral_links WHERE code = ?",
        (code,)
    )
    row = cursor.fetchone()

    if not row:
        logger.warning(f"Referral link {code} not found in database")
        conn.close()
        return False

    if not row["is_active"]:
        logger.warning(f"Referral link {code} is inactive")
        conn.close()
        return False

    max_uses = row["max_uses"]
    current_uses = row["current_uses"]

    # Проверяем лимит
    if max_uses is not None and current_uses >= max_uses:
        logger.warning(f"Referral link {code} limit reached: {current_uses}/{max_uses}")
        conn.close()
        return False  # Лимит исчерпан

    # Регистрируем использование
    cursor.execute(
        "INSERT INTO referral_usage (user_id, referral_code) VALUES (?, ?)",
        (user_id, code)
    )

    # Увеличиваем счетчик
    cursor.execute(
        "UPDATE referral_links SET current_uses = current_uses + 1 WHERE code = ?",
        (code,)
    )

    conn.commit()
    conn.close()

    logger.info(f"✓ Referral link used: user={user_id}, code={code}")
    return True


def get_user_referral_code(user_id: int) -> str | None:
    """Возвращает реферальный код, по которому пришел пользователь."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT referral_code FROM referral_usage WHERE user_id = ? LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return row["referral_code"]
    return None


def get_user_referral_info(user_id: int) -> dict | None:
    """Возвращает полную информацию о реферальной ссылке пользователя (код + название).

    Returns:
        {"code": str, "name": str} или None если пользователь не по реферальной ссылке
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ru.referral_code, rl.name "
        "FROM referral_usage ru "
        "JOIN referral_links rl ON ru.referral_code = rl.code "
        "WHERE ru.user_id = ? LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"code": row["referral_code"], "name": row["name"]}
    return None


def get_user_own_referral_code(user_id: int) -> str | None:
    """Возвращает личный реферальный код пользователя.

    Returns:
        Код реферальной ссылки или None если ссылка ещё не создана
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT code FROM referral_links WHERE owner_user_id = ? LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return row["code"]
    return None


def ensure_user_referral_link(user_id: int) -> str:
    """Создаёт личную реферальную ссылку для пользователя, если её ещё нет.

    Args:
        user_id: ID пользователя Telegram

    Returns:
        Код реферальной ссылки (существующий или новый)
    """
    # Проверяем, есть ли уже ссылка
    existing_code = get_user_own_referral_code(user_id)
    if existing_code:
        return existing_code

    # Создаём новую ссылку с дефолтными настройками
    import secrets

    code = secrets.token_urlsafe(8)

    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO referral_links (code, max_uses, custom_prices, name, owner_user_id) "
        "VALUES (?, NULL, NULL, NULL, ?)",
        (code, user_id)
    )
    conn.commit()
    conn.close()

    logger.info(f"Created personal referral link for user {user_id}: {code}")
    return code


def get_referral_link_owner(referral_code: str) -> int | None:
    """Возвращает ID владельца реферальной ссылки.

    Returns:
        user_id владельца или None если это административная ссылка или ссылка не найдена
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT owner_user_id FROM referral_links WHERE code = ?",
        (referral_code,)
    )
    row = cursor.fetchone()
    conn.close()
    if row and row["owner_user_id"]:
        return row["owner_user_id"]
    return None


def add_days_to_subscription(user_id: int, days: int) -> bool:
    """Добавляет дни к активной подписке пользователя.

    Args:
        user_id: ID пользователя
        days: Количество дней для добавления

    Returns:
        True если дни добавлены успешно, False если нет активной подписки
    """
    conn = _get_connection()
    cursor = conn.cursor()

    # Получаем активную подписку
    cursor.execute(
        "SELECT id, plan, expires_at FROM subscriptions "
        "WHERE user_id = ? AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()

    if not row:
        conn.close()
        logger.warning(f"Cannot add days to user {user_id}: no active subscription")
        return False

    current_expires = row["expires_at"]

    # Если подписка бессрочная, не добавляем дни
    if not current_expires:
        conn.close()
        logger.info(f"User {user_id} has lifetime subscription, skipping days addition")
        return True

    # Вычисляем новую дату истечения
    expires_dt = datetime.fromisoformat(current_expires)
    new_expires = expires_dt + timedelta(days=days)

    # Обновляем подписку
    cursor.execute(
        "UPDATE subscriptions SET expires_at = ? WHERE id = ?",
        (new_expires.isoformat(), row["id"])
    )
    conn.commit()
    conn.close()

    logger.info(f"Added {days} days to user {user_id} subscription (new expires: {new_expires.isoformat()})")
    return True


def get_all_referral_links() -> list[dict]:
    """Возвращает все реферальные ссылки."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, code, max_uses, current_uses, custom_prices, is_active, name, created_at "
        "FROM referral_links ORDER BY created_at DESC"
    )
    links = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return links


def get_plan_price_for_user(user_id: int, plan_id: str) -> float:
    """Возвращает цену плана для пользователя с учетом реферальной ссылки.

    Returns:
        float: Цена в долларах (может быть с плавающей точкой)
    """
    # Дефолтная цена
    default_price = float(SUBSCRIPTION_PLANS.get(plan_id, {}).get("price", 0))

    # Проверяем реферальный код
    referral_code = get_user_referral_code(user_id)
    if not referral_code:
        return default_price

    ref_link = get_referral_link(referral_code)
    if not ref_link or not ref_link["custom_prices"]:
        return default_price

    # Парсим кастомные цены (поддержка float)
    try:
        custom_prices = [float(p.strip()) for p in ref_link["custom_prices"].split(",")]
        plan_ids = list(SUBSCRIPTION_PLANS.keys())
        plan_index = plan_ids.index(plan_id)

        if plan_index < len(custom_prices):
            return custom_prices[plan_index]
    except (ValueError, IndexError):
        logger.warning(f"Failed to get custom price for user {user_id}, plan {plan_id}")

    return default_price


def update_payment_session_tx_hash(session_id: int, tx_hash: str) -> None:
    """Сохраняет хэш транзакции в payment_session."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE payment_sessions SET tx_hash = ? WHERE id = ?",
        (tx_hash, session_id),
    )
    conn.commit()
    conn.close()
    logger.info(f"Updated payment session {session_id} with tx_hash {tx_hash}")


def check_tx_hash_already_used(tx_hash: str) -> bool:
    """Проверяет, был ли хэш уже использован для оплаты."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) as cnt FROM payments WHERE tx_hash = ?",
        (tx_hash,)
    )
    count = cursor.fetchone()["cnt"]
    conn.close()
    return count > 0






def create_payment_session(user_id: int, plan: str, network: str, token: str, from_block: int) -> int:
    """Создаёт или переиспользует pending-сессию для проверки оплаты."""
    conn = _get_connection()
    cursor = conn.cursor()

    # Переиспользуем существующую pending-сессию с теми же параметрами
    cursor.execute(
        "SELECT id FROM payment_sessions "
        "WHERE user_id = ? AND plan = ? AND network = ? AND token = ? AND status = 'pending'",
        (user_id, plan, network, token),
    )
    row = cursor.fetchone()
    if row:
        conn.close()
        logger.debug(f"Reusing payment session {row['id']} for user {user_id}")
        return row["id"]

    cursor.execute(
        "INSERT INTO payment_sessions (user_id, plan, network, token, from_block) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, plan, network, token, from_block),
    )
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"Created payment session {session_id} for user {user_id}, plan={plan}, {network}/{token}, from_block={from_block}")
    return session_id # type: ignore


def get_pending_session(user_id: int, plan: str, network: str, token: str) -> dict | None:
    """Возвращает активную pending-сессию или None."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, from_block FROM payment_sessions "
        "WHERE user_id = ? AND plan = ? AND network = ? AND token = ? AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id, plan, network, token),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row["id"], "from_block": row["from_block"]}
    return None


def cancel_user_payment_sessions(user_id: int) -> int:
    """Отменяет все pending-сессии пользователя. Возвращает кол-во отменённых."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE payment_sessions SET status = 'cancelled' "
        "WHERE user_id = ? AND status = 'pending'",
        (user_id,),
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    if count:
        logger.info(f"Cancelled {count} payment session(s) for user {user_id}")
    return count


def complete_payment_session(session_id: int) -> None:
    """Помечает сессию как completed."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE payment_sessions SET status = 'completed' WHERE id = ?",
        (session_id,),
    )
    conn.commit()
    conn.close()
    logger.info(f"Payment session {session_id} completed")


def record_payment(user_id: int, amount: float, plan: str, network: str, token: str, tx_hash: str) -> None:
    """Записывает подтверждённый платёж."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO payments (user_id, amount, plan, network, token, status, tx_hash) "
        "VALUES (?, ?, ?, ?, ?, 'confirmed', ?)",
        (user_id, amount, plan, network, token, tx_hash),
    )
    conn.commit()
    conn.close()
    logger.info(f"Recorded payment for user {user_id}: {amount} {token} on {network}, tx={tx_hash}")




def get_user_subscription(user_id: int) -> dict | None:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, plan, status, expires_at FROM subscriptions "
        "WHERE user_id = ? AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None

    # Ленивая проверка: если подписка истекла — деактивируем
    if row["expires_at"]:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires <= datetime.now(timezone.utc):
            cursor.execute(
                "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            conn.close()
            return None

    conn.close()
    return {"plan": row["plan"], "status": row["status"], "expires_at": row["expires_at"]}


def deactivate_expired_subscriptions() -> list[dict]:
    """Находит все просроченные активные подписки, помечает их expired.

    Возвращает список {"user_id": int, "plan": str} деактивированных подписок.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, plan FROM subscriptions "
        "WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
        (now,),
    )
    rows = [dict(r) for r in cursor.fetchall()]

    if rows:
        ids = [r["id"] for r in rows]
        cursor.execute(
            f"UPDATE subscriptions SET status = 'expired' WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.commit()
        logger.info(f"Deactivated {len(rows)} expired subscription(s)")

    conn.close()
    return [{"user_id": r["user_id"], "plan": r["plan"]} for r in rows]


def activate_subscription(user_id: int, plan: str, payment_amount: float = 0.0) -> dict:
    """Активирует подписку для пользователя.

    Если у пользователя уже есть активная подписка с датой истечения:
    - Добавляет дни к существующей дате истечения

    Если нет активной подписки или она бессрочная:
    - Удаляет старую и создаёт новую

    Returns:
        dict: {"action": "created"|"extended", "expires_at": str|None, "days_added": int|None}
    """
    plan_info = SUBSCRIPTION_PLANS.get(plan)
    if not plan_info:
        return {"action": "error", "expires_at": None, "days_added": None}

    conn = _get_connection()
    cursor = conn.cursor()

    # Проверяем, есть ли активная подписка
    cursor.execute(
        "SELECT id, plan, expires_at FROM subscriptions "
        "WHERE user_id = ? AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()

    now = datetime.now(timezone.utc)
    duration_days = plan_info["duration_days"]

    if row and row["expires_at"] and duration_days:
        # Есть активная подписка с датой истечения - добавляем дни
        current_expires = datetime.fromisoformat(row["expires_at"])
        new_expires = current_expires + timedelta(days=duration_days)

        cursor.execute(
            "UPDATE subscriptions SET expires_at = ?, plan = ? WHERE id = ?",
            (new_expires.isoformat(), plan, row["id"])
        )

        # ВАЖНО: Очищаем все старые уведомления для этой подписки
        # Чтобы система могла отправить новые уведомления для новой даты истечения
        cursor.execute(
            "DELETE FROM expiry_notifications WHERE subscription_id = ?",
            (row["id"],)
        )
        deleted_notifications = cursor.rowcount

        conn.commit()
        conn.close()

        logger.info(f"Extended subscription for user {user_id}: added {duration_days} days from {plan} (new expires: {new_expires.isoformat()})")
        logger.info(f"Cleared {deleted_notifications} old notification records for subscription {row['id']}")

        # Начисляем реферальное вознаграждение владельцу ссылки
        referral_code = get_user_referral_code(user_id)
        if referral_code:
            owner_id = get_referral_link_owner(referral_code)
            if owner_id:
                paid_count = get_referral_paid_count(owner_id)
                percent = get_referral_percent(paid_count)
                reward = round(payment_amount * percent / 100, 2)
                increment_referral_paid_count(owner_id)
                add_referral_balance(owner_id, reward)
                logger.info(
                    f"✓ Начислено {reward}$ ({percent}%) на реферальный баланс пользователя {owner_id} "
                    f"(пригласил {user_id}, сумма платежа {payment_amount}$)"
                )

        return {
            "action": "extended",
            "expires_at": new_expires.isoformat(),
            "days_added": duration_days
        }

    else:
        # Нет активной подписки или она бессрочная - создаём новую
        expires_at = None
        if duration_days:
            expires_at = now + timedelta(days=duration_days)

        # Удаляем все старые уведомления для пользователя
        cursor.execute("DELETE FROM expiry_notifications WHERE user_id = ?", (user_id,))
        deleted_notifications = cursor.rowcount

        # Удаляем все старые подписки
        cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        cursor.execute(
            "INSERT INTO subscriptions (user_id, plan, status, expires_at) VALUES (?, ?, 'active', ?)",
            (user_id, plan, expires_at.isoformat() if expires_at else None),
        )

        conn.commit()
        conn.close()
        logger.info(f"Activated subscription '{plan}' for user {user_id}")
        if deleted_notifications > 0:
            logger.info(f"Cleared {deleted_notifications} old notification records for user {user_id}")

        # Начисляем реферальное вознаграждение владельцу ссылки
        referral_code = get_user_referral_code(user_id)
        if referral_code:
            owner_id = get_referral_link_owner(referral_code)
            if owner_id:
                paid_count = get_referral_paid_count(owner_id)
                percent = get_referral_percent(paid_count)
                reward = round(payment_amount * percent / 100, 2)
                increment_referral_paid_count(owner_id)
                add_referral_balance(owner_id, reward)
                logger.info(
                    f"✓ Начислено {reward}$ ({percent}%) на реферальный баланс пользователя {owner_id} "
                    f"(пригласил {user_id}, сумма платежа {payment_amount}$)"
                )

        return {
            "action": "created",
            "expires_at": expires_at.isoformat() if expires_at else None,
            "days_added": duration_days
        }


def activate_free_subscription(user_id: int, days: int) -> dict:
    """Активирует бесплатную подписку на указанное количество дней.

    Если у пользователя уже есть активная подписка:
    - Если она бессрочная - не добавляет дни
    - Если есть срок истечения - добавляет к нему указанное количество дней

    Если нет активной подписки - создаёт новую на указанное количество дней.

    Args:
        user_id: ID пользователя
        days: Количество дней подписки

    Returns:
        dict: {"action": "created"|"extended"|"skipped", "expires_at": str|None}
    """
    conn = _get_connection()
    cursor = conn.cursor()

    # Проверяем, есть ли активная подписка
    cursor.execute(
        "SELECT id, plan, expires_at FROM subscriptions "
        "WHERE user_id = ? AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()

    now = datetime.now(timezone.utc)

    if row:
        # Есть активная подписка
        current_expires = row["expires_at"]

        if not current_expires:
            # Бессрочная подписка - не добавляем дни
            conn.close()
            logger.info(f"User {user_id} has lifetime subscription, skipping free days addition")
            return {"action": "skipped", "expires_at": None}

        # Добавляем дни к существующей подписке
        expires_dt = datetime.fromisoformat(current_expires)
        new_expires = expires_dt + timedelta(days=days)

        cursor.execute(
            "UPDATE subscriptions SET expires_at = ? WHERE id = ?",
            (new_expires.isoformat(), row["id"])
        )
        conn.commit()
        conn.close()

        logger.info(f"Extended subscription for user {user_id}: added {days} days (new expires: {new_expires.isoformat()})")
        return {"action": "extended", "expires_at": new_expires.isoformat()}

    else:
        # Нет активной подписки - создаём новую
        expires_at = now + timedelta(days=days)

        cursor.execute(
            "INSERT INTO subscriptions (user_id, plan, status, expires_at) VALUES (?, ?, 'active', ?)",
            (user_id, f"free_{days}d", expires_at.isoformat()),
        )
        conn.commit()
        conn.close()

        logger.info(f"Activated free {days}-day subscription for user {user_id} (expires: {expires_at.isoformat()})")
        return {"action": "created", "expires_at": expires_at.isoformat()}


async def create_invite_link_for_chat(bot: Bot, chat_id: int, user_id: int, plan_name: str, chat_type: str) -> str:
    """Создать одноразовую ссылку-приглашение для конкретного чата.

    Args:
        bot: Экземпляр Bot для API-вызовов
        chat_id: ID чата/группы
        user_id: ID пользователя Telegram
        plan_name: Название плана подписки
        chat_type: Тип чата для логирования ("chat" или "group")

    Returns:
        URL одноразовой пригласительной ссылки
    """
    logger.info(f"Attempting to create invite link for {chat_type}: chat_id={chat_id}, user={user_id}, plan={plan_name}")

    if chat_id == 0:
        logger.error(f"{chat_type.upper()}_ID not set in environment variables!")
        raise ValueError(f"{chat_type} ID not configured")

    try:
        invite_link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            member_limit=1,  # Одноразовая ссылка
            name=f"{plan_name} - User {user_id}"  # Для удобства в логах канала
        )
        logger.info(f"Created invite link for {chat_type} for user {user_id}, plan {plan_name}")
        return invite_link.invite_link
    except Exception as e:
        logger.error(f"Failed to create invite link for {chat_type} for user {user_id}: {e}")
        raise


async def create_invite_links(bot: Bot, user_id: int, plan_name: str) -> dict[str, str]:
    """Создать одноразовые ссылки-приглашения для чата и группы.

    Args:
        bot: Экземпляр Bot для API-вызовов
        user_id: ID пользователя Telegram
        plan_name: Название плана подписки

    Returns:
        Словарь с ключами "chat" и "group", содержащий пригласительные ссылки
    """
    links = {}

    # Создаём ссылку для чата
    try:
        links["chat"] = await create_invite_link_for_chat(bot, PRIVATE_CHAT_ID, user_id, plan_name, "chat")
    except Exception as e:
        logger.error(f"Failed to create chat invite link: {e}")
        links["chat"] = None

    # Создаём ссылку для группы
    try:
        links["group"] = await create_invite_link_for_chat(bot, PRIVATE_GROUP_ID, user_id, plan_name, "group")
    except Exception as e:
        logger.error(f"Failed to create group invite link: {e}")
        links["group"] = None

    return links


# ── Система уведомлений об истечении подписки ──────────────────────


def get_subscriptions_requiring_notification() -> list[dict]:
    """Возвращает подписки, которым требуется отправить уведомление.

    Проверяет подписки на следующие временные метки:
    - За 3 дня до истечения
    - За 1 день до истечения
    - За 1 час до истечения
    - Истекла (+ grace period 1 час)

    Returns:
        Список словарей с информацией о подписках и типе уведомления
    """
    conn = _get_connection()
    cursor = conn.cursor()

    now = datetime.now(timezone.utc)
    three_days = now + timedelta(days=3)
    one_day = now + timedelta(days=1)
    one_hour = now + timedelta(hours=1)

    results = []

    # Получаем все активные подписки с датой истечения
    cursor.execute(
        "SELECT id, user_id, plan, expires_at FROM subscriptions "
        "WHERE status = 'active' AND expires_at IS NOT NULL"
    )
    subscriptions = [dict(row) for row in cursor.fetchall()]

    for sub in subscriptions:
        sub_id = sub["id"]
        user_id = sub["user_id"]
        expires_at = datetime.fromisoformat(sub["expires_at"])

        # Проверяем каждый тип уведомления
        notifications_to_check = [
            ("3days", three_days),
            ("1day", one_day),
            ("1hour", one_hour),
        ]

        for notif_type, threshold in notifications_to_check:
            if expires_at <= threshold:
                # Проверяем, не отправляли ли уже это уведомление
                cursor.execute(
                    "SELECT id FROM expiry_notifications "
                    "WHERE user_id = ? AND subscription_id = ? AND notification_type = ?",
                    (user_id, sub_id, notif_type)
                )
                if not cursor.fetchone():
                    results.append({
                        "user_id": user_id,
                        "subscription_id": sub_id,
                        "plan": sub["plan"],
                        "expires_at": sub["expires_at"],
                        "notification_type": notif_type
                    })

        # Проверяем истекшие подписки (с grace period)
        if expires_at <= now:
            # Проверяем, не отправляли ли уведомление об истечении
            cursor.execute(
                "SELECT id FROM expiry_notifications "
                "WHERE user_id = ? AND subscription_id = ? AND notification_type = 'expired'",
                (user_id, sub_id)
            )
            if not cursor.fetchone():
                results.append({
                    "user_id": user_id,
                    "subscription_id": sub_id,
                    "plan": sub["plan"],
                    "expires_at": sub["expires_at"],
                    "notification_type": "expired"
                })

    conn.close()
    return results


def mark_notification_sent(user_id: int, subscription_id: int, notification_type: str) -> None:
    """Записывает факт отправки уведомления."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO expiry_notifications (user_id, subscription_id, notification_type) "
        "VALUES (?, ?, ?)",
        (user_id, subscription_id, notification_type)
    )
    conn.commit()
    conn.close()
    logger.info(f"Marked notification as sent: user={user_id}, type={notification_type}")


def get_users_to_kick() -> list[dict]:
    """Возвращает пользователей, которых нужно кикнуть из группы.

    Это пользователи, у которых:
    - Подписка истекла более часа назад
    - Уже отправлено уведомление 'expired'
    - Ещё не были кикнуты (нет уведомления 'kicked')
    """
    conn = _get_connection()
    cursor = conn.cursor()

    now = datetime.now(timezone.utc)
    grace_period_end = now - timedelta(hours=1)

    # Находим истекшие подписки с отправленным уведомлением 'expired'
    # Ищем как active, так и expired подписки (подписка может быть деактивирована до кика)
    # Исключаем тех, кто уже был кикнут (есть уведомление 'kicked')
    cursor.execute(
        "SELECT DISTINCT s.user_id, s.id as subscription_id, s.expires_at, s.plan "
        "FROM subscriptions s "
        "JOIN expiry_notifications en ON s.id = en.subscription_id "
        "WHERE s.status IN ('active', 'expired') "
        "AND s.expires_at IS NOT NULL "
        "AND s.expires_at <= ? "
        "AND en.notification_type = 'expired' "
        "AND NOT EXISTS ("
        "    SELECT 1 FROM expiry_notifications "
        "    WHERE subscription_id = s.id AND notification_type = 'kicked'"
        ")",
        (grace_period_end.isoformat(),)
    )

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results


async def kick_and_unban_user(bot: Bot, user_id: int) -> dict[str, bool]:
    """Кикает пользователя из чата и группы, затем разбанивает.

    Args:
        bot: Экземпляр Bot для API-вызовов
        user_id: ID пользователя Telegram

    Returns:
        Словарь с результатами: {"chat": bool, "group": bool}
    """
    results = {"chat": False, "group": False}

    # Кик из чата
    if PRIVATE_CHAT_ID != 0:
        try:
            await bot.ban_chat_member(chat_id=PRIVATE_CHAT_ID, user_id=user_id)
            logger.info(f"Kicked user {user_id} from chat {PRIVATE_CHAT_ID}")
            # Сразу разбаниваем, чтобы не попал в ЧС
            await bot.unban_chat_member(chat_id=PRIVATE_CHAT_ID, user_id=user_id, only_if_banned=True)
            logger.info(f"Unbanned user {user_id} from chat {PRIVATE_CHAT_ID}")
            results["chat"] = True
        except Exception as e:
            logger.error(f"Failed to kick/unban user {user_id} from chat: {e}")

    # Кик из группы
    if PRIVATE_GROUP_ID != 0:
        try:
            await bot.ban_chat_member(chat_id=PRIVATE_GROUP_ID, user_id=user_id)
            logger.info(f"Kicked user {user_id} from group {PRIVATE_GROUP_ID}")
            # Сразу разбаниваем, чтобы не попал в ЧС
            await bot.unban_chat_member(chat_id=PRIVATE_GROUP_ID, user_id=user_id, only_if_banned=True)
            logger.info(f"Unbanned user {user_id} from group {PRIVATE_GROUP_ID}")
            results["group"] = True
        except Exception as e:
            logger.error(f"Failed to kick/unban user {user_id} from group: {e}")

    return results


def get_active_users() -> list[dict]:
    """Возвращает всех пользователей с активной подпиской.

    Returns:
        Список словарей с информацией о пользователе и подписке
    """
    conn = _get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            up.user_id,
            up.username,
            up.first_name,
            up.last_name,
            s.plan,
            s.expires_at
        FROM user_profiles up
        JOIN subscriptions s ON up.user_id = s.user_id
        WHERE s.status = 'active'
        ORDER BY s.expires_at ASC NULLS LAST
    """)

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results
