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
PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID", "0"))

SUBSCRIPTION_PLANS = {
    "1month": {"label": "1 месяц", "price": 0.1, "duration_days": 30},
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

    conn.commit()
    conn.close()

    # Генерируем мастер-кошельки если их нет
    generate_master_wallets()

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


def create_referral_link(max_uses: int | None, custom_prices: str | None) -> str:
    """Создает реферальную ссылку с уникальным кодом."""
    import secrets

    # Генерируем уникальный код
    code = secrets.token_urlsafe(8)

    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO referral_links (code, max_uses, custom_prices) VALUES (?, ?, ?)",
        (code, max_uses, custom_prices)
    )
    conn.commit()
    conn.close()

    logger.info(f"Created referral link: code={code}, max_uses={max_uses}, custom_prices={custom_prices}")
    return code


def get_referral_link(code: str) -> dict | None:
    """Получает информацию о реферальной ссылке."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, code, max_uses, current_uses, custom_prices, is_active FROM referral_links WHERE code = ?",
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
        conn.close()
        return False  # Уже использовал

    # Получаем информацию о ссылке
    cursor.execute(
        "SELECT max_uses, current_uses, is_active FROM referral_links WHERE code = ?",
        (code,)
    )
    row = cursor.fetchone()

    if not row or not row["is_active"]:
        conn.close()
        return False  # Ссылка не найдена или неактивна

    max_uses = row["max_uses"]
    current_uses = row["current_uses"]

    # Проверяем лимит
    if max_uses is not None and current_uses >= max_uses:
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

    logger.info(f"Referral link used: user={user_id}, code={code}")
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


def get_all_referral_links() -> list[dict]:
    """Возвращает все реферальные ссылки."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, code, max_uses, current_uses, custom_prices, is_active, created_at "
        "FROM referral_links ORDER BY created_at DESC"
    )
    links = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return links


def get_plan_price_for_user(user_id: int, plan_id: str) -> int:
    """Возвращает цену плана для пользователя с учетом реферальной ссылки."""
    # Дефолтная цена
    default_price = SUBSCRIPTION_PLANS.get(plan_id, {}).get("price", 0)

    # Проверяем реферальный код
    referral_code = get_user_referral_code(user_id)
    if not referral_code:
        return default_price

    ref_link = get_referral_link(referral_code)
    if not ref_link or not ref_link["custom_prices"]:
        return default_price

    # Парсим кастомные цены
    try:
        custom_prices = [int(p.strip()) for p in ref_link["custom_prices"].split(",")]
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


def activate_subscription(user_id: int, plan: str) -> None:
    plan_info = SUBSCRIPTION_PLANS.get(plan)
    if not plan_info:
        return

    now = datetime.now(timezone.utc)
    expires_at = None
    if plan_info["duration_days"]:
        expires_at = now + timedelta(days=plan_info["duration_days"])

    conn = _get_connection()
    cursor = conn.cursor()

    # Удаляем все старые записи, оставляем одну актуальную
    cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    cursor.execute(
        "INSERT INTO subscriptions (user_id, plan, status, expires_at) VALUES (?, ?, 'active', ?)",
        (user_id, plan, expires_at.isoformat() if expires_at else None),
    )

    conn.commit()
    conn.close()
    logger.info(f"Activated subscription '{plan}' for user {user_id}")


async def create_invite_link(bot: Bot, user_id: int, plan_name: str) -> str:
    """Создать одноразовую ссылку-приглашение в приватный канал.

    Args:
        bot: Экземпляр Bot для API-вызовов
        user_id: ID пользователя Telegram
        plan_name: Название плана подписки

    Returns:
        URL одноразовой пригласительной ссылки
    """
    logger.info(f"Attempting to create invite link: chat_id={PRIVATE_CHANNEL_ID} (type: {type(PRIVATE_CHANNEL_ID)}), user={user_id}, plan={plan_name}")

    if PRIVATE_CHANNEL_ID == 0:
        logger.error("PRIVATE_CHANNEL_ID not set in environment variables!")
        raise ValueError("PRIVATE_CHANNEL_ID not configured")

    try:
        invite_link = await bot.create_chat_invite_link(
            chat_id=PRIVATE_CHANNEL_ID,
            member_limit=1,  # Одноразовая ссылка
            name=f"{plan_name} - User {user_id}"  # Для удобства в логах канала
        )
        logger.info(f"Created invite link for user {user_id}, plan {plan_name}")
        return invite_link.invite_link
    except Exception as e:
        logger.error(f"Failed to create invite link for user {user_id}: {e}")
        raise
