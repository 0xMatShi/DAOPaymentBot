import os
import sqlite3
from datetime import datetime, timedelta, timezone

from eth_account import Account

from src.logger import logger

DB_PATH = "data/bot.db"

SUBSCRIPTION_PLANS = {
    "1month": {"label": "1 месяц", "price": 1, "duration_days": 30},
    "3months": {"label": "3 месяца", "price": 2, "duration_days": 90},
    "forever": {"label": "Навсегда", "price": 3, "duration_days": None},
}

SUPPORTED_NETWORKS = {
    "base": {
        "name": "Base",
        "chain_id": 8453,
        "rpc_url_env": "BASE_RPC_URL",
        "rpc_url_default": "https://mainnet.base.org",
    },
    "arbitrum": {
        "name": "Arbitrum One",
        "chain_id": 42161,
        "rpc_url_env": "ARBITRUM_RPC_URL",
        "rpc_url_default": "https://arb1.arbitrum.io/rpc",
    },
}

SUPPORTED_TOKENS = {
    "usdc": {
        "name": "USDC",
        "decimals": 6,
        "addresses": {
            "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "arbitrum": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        },
    },
    "usdt": {
        "name": "USDT",
        "decimals": 6,
        "addresses": {
            "base": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
            "arbitrum": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        },
    },
}


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

    conn.commit()
    conn.close()
    logger.info("Database initialized")


def get_or_create_wallet(user_id: int) -> str:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT wallet_address FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if row:
        conn.close()
        return row["wallet_address"]

    account = Account.create()
    wallet_address = account.address
    private_key = account.key.hex()

    cursor.execute(
        "INSERT INTO users (user_id, wallet_address, private_key) VALUES (?, ?, ?)",
        (user_id, wallet_address, private_key),
    )
    conn.commit()
    conn.close()
    logger.info(f"Created wallet {wallet_address} for user {user_id}")
    return wallet_address


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
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT wallet_address FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        logger.warning(f"check_payment: no wallet for user {user_id}")
        return None

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
    cursor.execute(
        "INSERT INTO subscriptions (user_id, plan, status, expires_at) VALUES (?, ?, 'active', ?)",
        (user_id, plan, expires_at.isoformat() if expires_at else None),
    )
    conn.commit()
    conn.close()
    logger.info(f"Activated subscription '{plan}' for user {user_id}")
