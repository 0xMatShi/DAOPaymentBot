import os

import aiohttp

from src.logger import logger
from src.payments import SUPPORTED_NETWORKS

# keccak256("Transfer(address,address,uint256)")
TRANSFER_EVENT_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _get_rpc_url(network: str) -> str:
    """Возвращает RPC URL для сети из .env или дефолтный."""
    net_info = SUPPORTED_NETWORKS[network]
    return os.getenv(net_info["rpc_url_env"], net_info["rpc_url_default"])


async def _rpc_call(network: str, method: str, params: list) -> dict:
    """JSON-RPC вызов через aiohttp."""
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


async def get_current_block(network: str) -> int:
    """Возвращает номер текущего блока."""
    data = await _rpc_call(network, "eth_blockNumber", [])
    block = int(data["result"], 16)
    logger.debug(f"Current block on {network}: {block}")
    return block


async def scan_incoming_transfers(
    network: str,
    token_address: str,
    wallet: str,
    from_block: int,
) -> list[dict]:
    """Сканирует Transfer-события ERC-20 для указанного получателя.

    Возвращает список {"tx_hash": str, "amount": int (raw)} отсортированных по блоку.
    """
    # topic[0] = Transfer event signature
    # topic[2] = получатель (address, дополненный нулями до 32 байт)
    padded_wallet = "0x" + wallet[2:].lower().zfill(64)

    filter_params = {
        "fromBlock": hex(from_block),
        "toBlock": "latest",
        "address": token_address,
        "topics": [
            TRANSFER_EVENT_TOPIC,
            None,  # topic[1] = отправитель (любой)
            padded_wallet,  # topic[2] = получатель
        ],
    }

    try:
        data = await _rpc_call(network, "eth_getLogs", [filter_params])
    except RuntimeError:
        logger.warning(f"Failed to scan transfers on {network} for {wallet}")
        return []

    logs = data.get("result", [])
    if not logs:
        return []

    transfers = []
    for log_entry in logs:
        tx_hash = log_entry["transactionHash"]
        # data содержит uint256 amount
        raw_amount = int(log_entry["data"], 16)
        transfers.append({
            "tx_hash": tx_hash,
            "amount": raw_amount,
        })

    logger.info(f"Found {len(transfers)} transfer(s) to {wallet} on {network}")
    return transfers
