"""Отправка USDT TRC-20 через сеть Tron."""

import asyncio

from src.logger import logger

USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


async def send_trc20_usdt(private_key_hex: str, to_address: str, amount_usd: float) -> str:
    """Отправляет USDT TRC-20 на указанный адрес.

    Args:
        private_key_hex: Приватный ключ кошелька в hex-формате (без 0x)
        to_address: TRC-20 адрес получателя (начинается с T, 34 символа)
        amount_usd: Сумма в долларах США (1 USD = 1 USDT)

    Returns:
        TX Hash отправленной транзакции

    Raises:
        Exception: При любой ошибке отправки
    """

    def _send() -> str:
        from tronpy import Tron
        from tronpy.keys import PrivateKey

        key_hex = private_key_hex.lstrip("0x")
        priv_key = PrivateKey(bytes.fromhex(key_hex))
        from_address = priv_key.public_key.to_base58check_address()

        client = Tron()
        contract = client.get_contract(USDT_TRC20_CONTRACT)

        # USDT TRC-20 имеет 6 знаков после запятой
        amount_units = int(amount_usd * 1_000_000)

        txn = (
            contract.functions.transfer(to_address, amount_units)
            .with_owner(from_address)
            .fee_limit(30_000_000)  # Лимит комиссии: 30 TRX
            .build()
            .sign(priv_key)
        )

        txn.broadcast().wait()
        return txn.txid

    loop = asyncio.get_running_loop()
    tx_hash = await loop.run_in_executor(None, _send)
    logger.info(f"Отправлено {amount_usd}$ USDT TRC-20 → {to_address}, tx: {tx_hash}")
    return tx_hash
