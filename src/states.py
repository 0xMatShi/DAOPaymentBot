"""FSM состояния для диалогов бота."""

from aiogram.fsm.state import State, StatesGroup


class PaymentStates(StatesGroup):
    """Состояния процесса оплаты."""
    waiting_for_tx_hash = State()


class WithdrawalStates(StatesGroup):
    """Состояния процесса вывода реферального баланса."""
    waiting_for_network = State()
    waiting_for_amount = State()
    waiting_for_address = State()
