"""FSM состояния для диалогов бота."""

from aiogram.fsm.state import State, StatesGroup


class PaymentStates(StatesGroup):
    """Состояния процесса оплаты."""
    waiting_for_tx_hash = State()
