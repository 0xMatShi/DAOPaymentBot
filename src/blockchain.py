import os

import aiohttp
from solders.pubkey import Pubkey

from src.logger import logger
from src.payments import SUPPORTED_NETWORKS

# keccak256("Transfer(address,address,uint256)")
TRANSFER_EVENT_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Solana program IDs
_SOL_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
_SOL_ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def _get_rpc_url(network: str) -> str:
    """Возвращает RPC URL для EVM-сети из .env или дефолтный."""
    net_info = SUPPORTED_NETWORKS[network]
    return os.getenv(net_info["rpc_url_env"], net_info["rpc_url_default"])


def _get_trongrid_url(network: str) -> str:
    """Возвращает TronGrid API URL из .env или дефолтный."""
    net_info = SUPPORTED_NETWORKS[network]
    return os.getenv(net_info["api_url_env"], net_info["api_url_default"])


async def _rpc_call(network: str, method: str, params: list) -> dict:
    """JSON-RPC вызов через aiohttp (EVM)."""
    url = _get_rpc_url(network)
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if "error" in data:
                logger.error(f"RPC error ({network}, {method}): {data['error']}")
                raise RuntimeError(f"RPC error: {data['error']}")
            return data


async def _solana_rpc_call(url: str, method: str, params: list) -> dict:
    """JSON-RPC вызов для Solana."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if "error" in data:
                logger.error(f"Solana RPC error ({method}): {data['error']}")
                raise RuntimeError(f"Solana RPC error: {data['error']}")
            return data


# ── EVM ────────────────────────────────────────────────────


async def _evm_get_current_block(network: str) -> int:
    data = await _rpc_call(network, "eth_blockNumber", [])
    block = int(data["result"], 16)
    logger.debug(f"Current block on {network}: {block}")
    return block


async def _evm_scan_transfers(
    network: str, token_address: str, wallet: str, from_block: int,
) -> list[dict]:
    padded_wallet = "0x" + wallet[2:].lower().zfill(64)
    filter_params = {
        "fromBlock": hex(from_block),
        "toBlock": "latest",
        "address": token_address,
        "topics": [TRANSFER_EVENT_TOPIC, None, padded_wallet],
    }

    try:
        data = await _rpc_call(network, "eth_getLogs", [filter_params])
    except RuntimeError:
        logger.warning(f"Failed to scan transfers on {network} for {wallet}")
        return []

    logs = data.get("result", [])
    transfers = []
    for log_entry in logs:
        raw_amount = int(log_entry["data"], 16)
        transfers.append({
            "tx_hash": log_entry["transactionHash"],
            "amount": raw_amount,
        })
    return transfers


# ── Tron ───────────────────────────────────────────────────


async def _tron_get_current_timestamp(network: str) -> int:
    """Возвращает текущий timestamp Tron-сети (мс)."""
    url = _get_trongrid_url(network)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{url}/wallet/getnowblock",
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
    ts = data["block_header"]["raw_data"]["timestamp"]
    logger.debug(f"Current Tron timestamp: {ts}")
    return ts


async def _tron_scan_transfers(
    network: str, token_address: str, wallet: str, from_timestamp: int,
) -> list[dict]:
    """Сканирует TRC-20 входящие переводы через TronGrid API."""
    url = _get_trongrid_url(network)
    api_key = os.getenv("TRONGRID_API_KEY", "")
    headers = {}
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key

    transfers: list[dict] = []
    fingerprint = None

    while True:
        params: dict = {
            "only_to": "true",
            "only_confirmed": "true",
            "limit": 200,
            "contract_address": token_address,
            "min_timestamp": from_timestamp,
        }
        if fingerprint:
            params["fingerprint"] = fingerprint

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                f"{url}/v1/accounts/{wallet}/transactions/trc20",
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

        for tx in data.get("data", []):
            transfers.append({
                "tx_hash": tx["transaction_id"],
                "amount": int(tx["value"]),
            })

        meta = data.get("meta", {})
        fingerprint = meta.get("fingerprint")
        if not fingerprint:
            break

    return transfers


# ── Solana ─────────────────────────────────────────────────


def _get_associated_token_address(wallet: str, mint: str) -> str:
    """Вычисляет Associated Token Account (ATA) для кошелька и токена."""
    wallet_pk = Pubkey.from_string(wallet)
    mint_pk = Pubkey.from_string(mint)
    ata, _ = Pubkey.find_program_address(
        [bytes(wallet_pk), bytes(_SOL_TOKEN_PROGRAM), bytes(mint_pk)],
        _SOL_ATA_PROGRAM,
    )
    return str(ata)


async def _solana_get_current_slot(network: str) -> int:
    url = _get_rpc_url(network)
    data = await _solana_rpc_call(url, "getSlot", [{"commitment": "confirmed"}])
    slot = data["result"]
    logger.debug(f"Current Solana slot: {slot}")
    return slot


async def _solana_scan_transfers(
    network: str, token_mint: str, wallet: str, from_slot: int,
) -> list[dict]:
    """Сканирует входящие SPL-токен переводы через Solana RPC."""
    url = _get_rpc_url(network)
    ata = _get_associated_token_address(wallet, token_mint)

    try:
        sigs_data = await _solana_rpc_call(url, "getSignaturesForAddress", [
            ata, {"commitment": "confirmed"},
        ])
    except RuntimeError:
        logger.warning(f"Failed to get signatures for {ata} on Solana")
        return []

    signatures = sigs_data.get("result", [])
    if not signatures:
        return []

    transfers = []
    for sig_info in signatures:
        if sig_info["slot"] < from_slot:
            continue
        if sig_info.get("err"):
            continue

        try:
            tx_data = await _solana_rpc_call(url, "getTransaction", [
                sig_info["signature"],
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
            ])
        except RuntimeError:
            continue

        tx = tx_data.get("result")
        if not tx:
            continue

        amount = _parse_solana_spl_transfer(tx, ata)
        if amount > 0:
            transfers.append({
                "tx_hash": sig_info["signature"],
                "amount": amount,
            })

    return transfers


def _parse_solana_spl_transfer(tx: dict, destination_ata: str) -> int:
    """Извлекает сумму входящих SPL-переводов из транзакции."""
    total = 0
    all_instructions = list(tx["transaction"]["message"]["instructions"])
    for inner in tx.get("meta", {}).get("innerInstructions", []):
        all_instructions.extend(inner.get("instructions", []))

    for ix in all_instructions:
        parsed = ix.get("parsed")
        if not parsed or not isinstance(parsed, dict):
            continue
        ix_type = parsed.get("type", "")
        info = parsed.get("info", {})
        if ix_type == "transfer" and info.get("destination") == destination_ata:
            total += int(info.get("amount", 0))
        elif ix_type == "transferChecked" and info.get("destination") == destination_ata:
            total += int(info.get("tokenAmount", {}).get("amount", 0))

    return total


# ── Публичный API ──────────────────────────────────────────


async def get_current_block(network: str) -> int:
    """Возвращает номер блока (EVM), timestamp мс (Tron) или slot (Solana)."""
    net_info = SUPPORTED_NETWORKS[network]
    net_type = net_info.get("type")
    if net_type == "tron":
        return await _tron_get_current_timestamp(network)
    if net_type == "solana":
        return await _solana_get_current_slot(network)
    return await _evm_get_current_block(network)


async def scan_incoming_transfers(
    network: str,
    token_address: str,
    wallet: str,
    from_block: int,
) -> list[dict]:
    """Сканирует входящие переводы. Возвращает [{"tx_hash": str, "amount": int (raw)}]."""
    net_info = SUPPORTED_NETWORKS[network]
    net_type = net_info.get("type")
    if net_type == "tron":
        result = await _tron_scan_transfers(network, token_address, wallet, from_block)
    elif net_type == "solana":
        result = await _solana_scan_transfers(network, token_address, wallet, from_block)
    else:
        result = await _evm_scan_transfers(network, token_address, wallet, from_block)

    if result:
        logger.info(f"Found {len(result)} transfer(s) to {wallet} on {network}")
    return result


# ── Проверка транзакций по хэшу ───────────────────────────────


def validate_tx_hash_format(network: str, tx_hash: str) -> tuple[bool, str]:
    """Проверяет формат хэша транзакции для конкретной сети.

    Returns:
        (is_valid, error_message)
    """
    from src.payments import SUPPORTED_NETWORKS

    net_info = SUPPORTED_NETWORKS.get(network)
    if not net_info:
        return False, "Неизвестная сеть"

    net_type = net_info.get("type")

    if net_type in ["evm", "tron"]:
        # EVM и Tron: 0x + 64 hex chars (32 bytes)
        if not tx_hash.startswith("0x"):
            return False, "Хэш должен начинаться с '0x'"
        if len(tx_hash) != 66:  # 0x + 64 chars
            return False, f"Неверная длина хэша (ожидается 66 символов, получено {len(tx_hash)})"
        try:
            int(tx_hash, 16)  # Проверка hex
        except ValueError:
            return False, "Хэш содержит недопустимые символы (только 0-9, a-f)"
        return True, ""

    elif net_type == "solana":
        # Solana: base58-encoded signature (обычно 87-88 символов)
        if len(tx_hash) < 80 or len(tx_hash) > 90:
            return False, f"Неверная длина подписи (ожидается 80-90 символов, получено {len(tx_hash)})"
        try:
            import base58
            base58.b58decode(tx_hash)
        except Exception:
            return False, "Неверный формат base58"
        return True, ""

    return False, "Неподдерживаемый тип сети"


async def verify_evm_transaction(
    network: str,
    tx_hash: str,
    expected_to: str,
    expected_token: str,
    min_amount: float,
    decimals: int,
) -> tuple[bool, str, float]:
    """Проверяет EVM транзакцию по хэшу.

    Returns:
        (is_valid, error_message, amount)
    """
    try:
        data = await _rpc_call(network, "eth_getTransactionReceipt", [tx_hash])
    except RuntimeError as e:
        logger.error(f"Failed to get tx receipt: {e}")
        return False, "Не удалось получить данные о транзакции из сети", 0.0

    receipt = data.get("result")
    if not receipt:
        return False, "Транзакция не найдена или ещё не подтверждена", 0.0

    # Проверяем статус (success)
    status = receipt.get("status")
    if status != "0x1":
        return False, "Транзакция завершилась с ошибкой", 0.0

    # Проверяем адрес контракта токена
    logs = receipt.get("logs", [])
    transfer_found = False
    total_amount = 0

    expected_to_padded = "0x" + expected_to[2:].lower().zfill(64)

    for log_entry in logs:
        if log_entry.get("address", "").lower() != expected_token.lower():
            continue
        topics = log_entry.get("topics", [])
        if len(topics) < 3:
            continue
        if topics[0] != TRANSFER_EVENT_TOPIC:
            continue
        # topics[2] - получатель (to)
        if topics[2].lower() != expected_to_padded.lower():
            continue

        # Парсим amount
        raw_amount = int(log_entry["data"], 16)
        total_amount += raw_amount
        transfer_found = True

    if not transfer_found:
        return False, f"Перевод на адрес {expected_to} не найден в транзакции", 0.0

    amount = total_amount / (10 ** decimals)

    if amount < min_amount:
        return False, f"Недостаточная сумма ({amount} < {min_amount})", amount

    return True, "", amount


async def verify_tron_transaction(
    network: str,
    tx_hash: str,
    expected_to: str,
    expected_token: str,
    min_amount: float,
    decimals: int,
) -> tuple[bool, str, float]:
    """Проверяет Tron TRC-20 транзакцию по хэшу.

    Returns:
        (is_valid, error_message, amount)
    """
    url = _get_trongrid_url(network)
    api_key = os.getenv("TRONGRID_API_KEY", "")
    headers = {}
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key

    # Убираем 0x prefix если есть
    tx_id = tx_hash[2:] if tx_hash.startswith("0x") else tx_hash

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                f"{url}/v1/transactions/{tx_id}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    return False, "Транзакция не найдена", 0.0
                data = await resp.json()
    except Exception as e:
        logger.error(f"Failed to get Tron tx: {e}")
        return False, "Ошибка при запросе к сети Tron", 0.0

    # Проверяем успешность
    ret = data.get("ret", [])
    if not ret or ret[0].get("contractRet") != "SUCCESS":
        return False, "Транзакция не успешна", 0.0

    # Парсим contract calls
    contract = data.get("raw_data", {}).get("contract", [])
    if not contract:
        return False, "Некорректная структура транзакции", 0.0

    parameter = contract[0].get("parameter", {}).get("value", {})
    to_address = parameter.get("to")
    amount_raw = parameter.get("amount", 0)

    if not to_address:
        return False, "Отсутствуют данные о получателе", 0.0

    # Сравниваем адреса (base58)
    if to_address != expected_to:
        return False, f"Получатель не совпадает (ожидается {expected_to})", 0.0

    amount = amount_raw / (10 ** decimals)

    if amount < min_amount:
        return False, f"Недостаточная сумма ({amount} < {min_amount})", amount

    return True, "", amount


async def verify_solana_transaction(
    network: str,
    signature: str,
    expected_to: str,
    expected_token_mint: str,
    min_amount: float,
    decimals: int,
) -> tuple[bool, str, float]:
    """Проверяет Solana SPL-токен транзакцию по подписи.

    Returns:
        (is_valid, error_message, amount)
    """
    url = _get_rpc_url(network)
    ata = _get_associated_token_address(expected_to, expected_token_mint)

    try:
        tx_data = await _solana_rpc_call(url, "getTransaction", [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed"
            },
        ])
    except RuntimeError as e:
        logger.error(f"Failed to get Solana tx: {e}")
        return False, "Не удалось получить транзакцию из сети Solana", 0.0

    tx = tx_data.get("result")
    if not tx:
        return False, "Транзакция не найдена или ещё не подтверждена", 0.0

    # Проверяем статус
    if tx.get("meta", {}).get("err"):
        return False, "Транзакция завершилась с ошибкой", 0.0

    # Парсим сумму
    amount_raw = _parse_solana_spl_transfer(tx, ata)

    if amount_raw == 0:
        return False, f"Перевод на кошелёк {expected_to} не найден", 0.0

    amount = amount_raw / (10 ** decimals)

    if amount < min_amount:
        return False, f"Недостаточная сумма ({amount} < {min_amount})", amount

    return True, "", amount


async def verify_transaction_by_hash(
    tx_hash: str,
    network: str,
    token: str,
    plan: str,
) -> tuple[bool, str, float]:
    """Универсальная функция проверки транзакции по хэшу.

    Args:
        tx_hash: Хэш транзакции (EVM/Tron) или подпись (Solana)
        network: ID сети ('base', 'arbitrum', 'tron', 'solana')
        token: ID токена ('usdc', 'usdt')
        plan: ID плана для проверки суммы

    Returns:
        (is_valid, error_message, amount)
    """
    from src.payments import (
        SUBSCRIPTION_PLANS,
        SUPPORTED_TOKENS,
        SUPPORTED_NETWORKS,
        get_master_wallet_address,
        check_tx_hash_already_used,
        eth_to_tron_address,
    )

    # Валидация формата
    is_valid_format, format_error = validate_tx_hash_format(network, tx_hash)
    if not is_valid_format:
        return False, format_error, 0.0

    # Проверка повторного использования
    if check_tx_hash_already_used(tx_hash):
        return False, "Этот хэш транзакции уже был использован", 0.0

    # Получаем параметры
    plan_info = SUBSCRIPTION_PLANS.get(plan)
    token_info = SUPPORTED_TOKENS.get(token)
    net_info = SUPPORTED_NETWORKS.get(network)

    if not plan_info or not token_info or not net_info:
        return False, "Неверные параметры плана/токена/сети", 0.0

    min_amount = plan_info["price"]
    decimals = token_info["decimals"]
    token_address = token_info["addresses"].get(network)

    if not token_address:
        return False, f"Токен {token} не поддерживается в сети {network}", 0.0

    # Получаем адрес мастер-кошелька
    master_wallet = get_master_wallet_address(network)
    if not master_wallet:
        return False, "Мастер-кошелёк не найден", 0.0

    # Вызываем специфичную функцию
    net_type = net_info.get("type")

    if net_type == "evm":
        return await verify_evm_transaction(
            network, tx_hash, master_wallet, token_address, min_amount, decimals
        )
    elif net_type == "tron":
        # Для Tron конвертируем адрес если нужно
        tron_address = eth_to_tron_address(master_wallet) if not master_wallet.startswith("T") else master_wallet
        return await verify_tron_transaction(
            network, tx_hash, tron_address, token_address, min_amount, decimals
        )
    elif net_type == "solana":
        return await verify_solana_transaction(
            network, tx_hash, master_wallet, token_address, min_amount, decimals
        )

    return False, "Неподдерживаемый тип сети", 0.0
