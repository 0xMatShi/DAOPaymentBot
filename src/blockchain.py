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
