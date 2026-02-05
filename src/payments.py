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


def get_wallet_for_network(user_id: int, network: str) -> str:
    """Возвращает адрес кошелька в формате нужной сети."""
    _ensure_all_wallets(user_id)
    net = SUPPORTED_NETWORKS.get(network, {})
    net_type = net.get("type", "evm")

    if net_type == "solana":
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT sol_wallet_address FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row["sol_wallet_address"]

    eth_address = get_or_create_wallet(user_id)
    if net_type == "tron":
        return eth_to_tron_address(eth_address)
    return eth_address


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


def get_or_create_wallet(user_id: int) -> str:
    """Возвращает EVM-адрес, создавая все кошельки при необходимости."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT wallet_address FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if row:
        conn.close()
        return row["wallet_address"]

    account = Account.create()
    sol_kp = Keypair()

    cursor.execute(
        "INSERT INTO users (user_id, wallet_address, private_key, sol_wallet_address, sol_private_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, account.address, account.key.hex(),
         str(sol_kp.pubkey()), base58.b58encode(bytes(sol_kp)).decode()),
    )
    conn.commit()
    conn.close()
    logger.info(f"Created wallets for user {user_id}: EVM={account.address}, SOL={sol_kp.pubkey()}")
    return account.address


def _ensure_all_wallets(user_id: int) -> None:
    """Догенерирует недостающие кошельки для существующего пользователя."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT sol_wallet_address FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        get_or_create_wallet(user_id)
        return

    if not row["sol_wallet_address"]:
        sol_kp = Keypair()
        cursor.execute(
            "UPDATE users SET sol_wallet_address = ?, sol_private_key = ? WHERE user_id = ?",
            (str(sol_kp.pubkey()), base58.b58encode(bytes(sol_kp)).decode(), user_id),
        )
        conn.commit()
        logger.info(f"Generated Solana wallet for existing user {user_id}: {sol_kp.pubkey()}")

    conn.close()


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


async def check_payment(user_id: int, plan: str, network: str, token: str) -> dict | None:
    """Проверка поступления оплаты через сканирование Transfer-событий.

    Возвращает {"tx_hash": str, "amount": float} при успехе или None.
    """
    from src.blockchain import scan_incoming_transfers

    plan_info = SUBSCRIPTION_PLANS.get(plan)
    token_info = SUPPORTED_TOKENS.get(token)
    if not plan_info or not token_info:
        logger.warning(f"check_payment: unknown plan={plan} or token={token}")
        return None

    session = get_pending_session(user_id, plan, network, token)
    if not session:
        logger.warning(f"check_payment: no pending session for user {user_id}, plan={plan}, {network}/{token}")
        return None

    # Получаем адрес кошелька пользователя
    _ensure_all_wallets(user_id)
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT wallet_address, sol_wallet_address FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        logger.warning(f"check_payment: no wallet for user {user_id}")
        return None

    net_info = SUPPORTED_NETWORKS.get(network, {})
    net_type = net_info.get("type", "evm")
    if net_type == "solana":
        wallet = row["sol_wallet_address"]
    elif net_type == "tron":
        wallet = eth_to_tron_address(row["wallet_address"])
    else:
        wallet = row["wallet_address"]
    token_address = token_info["addresses"].get(network)
    if not token_address:
        logger.warning(f"check_payment: no token address for {token} on {network}")
        return None

    logger.info(f"Checking payment for user {user_id}: scanning transfers to {wallet} on {network}/{token} from block {session['from_block']}")

    transfers = await scan_incoming_transfers(
        network=network,
        token_address=token_address,
        wallet=wallet,
        from_block=session["from_block"],
    )

    if not transfers:
        return None

    # Суммируем все входящие переводы
    decimals = token_info["decimals"]
    total_amount = sum(t["amount"] / (10 ** decimals) for t in transfers)
    required_amount = plan_info["price"]

    logger.info(f"User {user_id}: received {total_amount} {token_info['name']}, required {required_amount}")

    if total_amount >= required_amount:
        # Используем хеш последнего трансфера как основной
        tx_hash = transfers[-1]["tx_hash"]

        record_payment(user_id, total_amount, plan, network, token, tx_hash)
        activate_subscription(user_id, plan)
        complete_payment_session(session["id"])

        return {"tx_hash": tx_hash, "amount": total_amount}

    return None


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
