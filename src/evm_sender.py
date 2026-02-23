"""Отправка USDC ERC-20 на Base и Arbitrum."""

import os

import aiohttp
from eth_account import Account

from src.logger import logger

# Адреса контрактов USDC
USDC_ADDRESSES = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "arbitrum": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
}

USDC_DECIMALS = 6

# ERC-20 transfer(address,uint256) selector
_TRANSFER_SELECTOR = "a9059cbb"


def _encode_transfer(to_address: str, amount_units: int) -> str:
    """Кодирует вызов transfer(address,uint256) в ABI."""
    to_padded = to_address[2:].lower().zfill(64)
    amount_padded = hex(amount_units)[2:].zfill(64)
    return "0x" + _TRANSFER_SELECTOR + to_padded + amount_padded


async def _rpc(rpc_url: str, method: str, params: list):
    """Делает JSON-RPC вызов и возвращает result."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            data = await resp.json()
    if "error" in data:
        raise Exception(f"RPC error: {data['error']}")
    return data["result"]


async def send_erc20_usdc(
    private_key_hex: str,
    to_address: str,
    amount_usd: float,
    network: str,
) -> str:
    """Отправляет USDC ERC-20 на Base или Arbitrum.

    Args:
        private_key_hex: Приватный ключ отправителя (hex без 0x)
        to_address: EVM-адрес получателя
        amount_usd: Сумма в долларах (1 USD = 1 USDC)
        network: "base" или "arbitrum"

    Returns:
        TX Hash отправленной транзакции
    """
    rpc_urls = {
        "base": os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
        "arbitrum": os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"),
    }

    rpc_url = rpc_urls.get(network)
    if not rpc_url:
        raise ValueError(f"Неизвестная сеть: {network}")

    usdc_contract = USDC_ADDRESSES.get(network)
    if not usdc_contract:
        raise ValueError(f"USDC не поддерживается в сети: {network}")

    key_hex = private_key_hex.lstrip("0x")
    account = Account.from_key(f"0x{key_hex}")
    from_address = account.address

    amount_units = int(amount_usd * (10 ** USDC_DECIMALS))
    data = _encode_transfer(to_address, amount_units)

    # Nonce
    nonce_hex = await _rpc(rpc_url, "eth_getTransactionCount", [from_address, "latest"])
    nonce = int(nonce_hex, 16)

    # Gas price
    gas_price_hex = await _rpc(rpc_url, "eth_gasPrice", [])
    gas_price = int(gas_price_hex, 16)

    # Gas estimate
    gas_hex = await _rpc(rpc_url, "eth_estimateGas", [
        {"from": from_address, "to": usdc_contract, "data": data}
    ])
    gas_limit = int(int(gas_hex, 16) * 1.2)

    # Chain ID
    chain_id_hex = await _rpc(rpc_url, "eth_chainId", [])
    chain_id = int(chain_id_hex, 16)

    # Подписываем транзакцию
    tx = {
        "nonce": nonce,
        "gasPrice": gas_price,
        "gas": int(gas_limit),
        "to": usdc_contract,
        "value": 0,
        "data": data,
        "chainId": chain_id,
    }
    signed = account.sign_transaction(tx)

    raw_tx = signed.raw_transaction.hex()
    if not raw_tx.startswith("0x"):
        raw_tx = "0x" + raw_tx

    tx_hash = await _rpc(rpc_url, "eth_sendRawTransaction", [raw_tx])

    logger.info(f"Отправлено {amount_usd}$ USDC ({network}) → {to_address}, tx: {tx_hash}")
    return tx_hash
