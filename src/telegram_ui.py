from aiogram import Bot, Router, F
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from datetime import datetime, timezone, timedelta
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext

from src.logger import logger
from src.blockchain import get_current_block, verify_transaction_by_hash
from src.states import PaymentStates
from src.payments import (
    SUBSCRIPTION_PLANS,
    SUPPORTED_NETWORKS,
    SUPPORTED_TOKENS,
    cancel_user_payment_sessions,
    create_invite_link,
    create_payment_session,
    get_user_subscription,
    get_master_wallet_address,
    update_payment_session_tx_hash,
    complete_payment_session,
    record_payment,
    activate_subscription,
)

router = Router()

MAIN_MENU_TEXT = "Главное меню\n\nДобро пожаловать! Выберите действие:"

FAQ_TEXT = (
    "FAQ\n\n"
    "Здесь будет информация о часто задаваемых вопросах.\n\n"
    "(Текст будет дополнен позже)"
)


# ── Клавиатуры ──────────────────────────────────────────────


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить подписку", callback_data="subscribe")],
        [InlineKeyboardButton(text="Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton(text="FAQ", callback_data="faq")],
    ])


def plans_kb() -> InlineKeyboardMarkup:
    buttons = []
    for plan_id, plan in SUBSCRIPTION_PLANS.items():
        buttons.append([InlineKeyboardButton(
            text=f"{plan['label']} - {plan['price']}$",
            callback_data=f"plan:{plan_id}",
        )])
    buttons.append([InlineKeyboardButton(text="< Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def network_kb(plan_id: str) -> InlineKeyboardMarkup:
    buttons = []
    for net_id, net in SUPPORTED_NETWORKS.items():
        buttons.append([InlineKeyboardButton(
            text=net["name"],
            callback_data=f"net:{plan_id}:{net_id}",
        )])
    buttons.append([InlineKeyboardButton(text="< Назад", callback_data="subscribe")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def token_kb(plan_id: str, network: str) -> InlineKeyboardMarkup:
    buttons = []
    for token_id, token in SUPPORTED_TOKENS.items():
        if network not in token["addresses"]:
            continue
        buttons.append([InlineKeyboardButton(
            text=token["name"],
            callback_data=f"token:{plan_id}:{network}:{token_id}",
        )])
    buttons.append([InlineKeyboardButton(text="< Назад", callback_data=f"plan:{plan_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payment_kb(plan_id: str, network: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить оплату", callback_data=f"confirm:{plan_id}:{network}:{token}")],
        [InlineKeyboardButton(text="< Назад", callback_data=f"net:{plan_id}:{network}")],
    ])


def back_kb(callback_data: str = "back_to_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="< Назад", callback_data=callback_data)],
    ])


# ── Хендлеры ────────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    logger.info(f"User {message.from_user.id} started the bot") # type: ignore

    # Очищаем FSM при рестарте
    await state.clear()

    cancel_user_payment_sessions(message.from_user.id)  # type: ignore
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_kb())


@router.message(Command("chatid"))
async def get_chat_id_command(message: Message) -> None:
    """Получить ID текущего чата (для настройки канала/группы)"""
    chat_info = (
        f"📋 Информация о чате:\n\n"
        f"ID: `{message.chat.id}`\n"
        f"Тип: {message.chat.type}\n"
        f"Название: {message.chat.title or 'Личные сообщения'}"
    )
    logger.info(f"Chat ID request: {message.chat.id}, type: {message.chat.type}, title: {message.chat.title}")
    await message.answer(chat_info, parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery) -> None:
    cancel_user_payment_sessions(callback.from_user.id)
    await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=main_menu_kb()) # type: ignore
    await callback.answer()


@router.callback_query(F.data == "subscribe")
async def show_plans(callback: CallbackQuery) -> None:
    logger.info(f"User {callback.from_user.id} opened subscription plans")
    await callback.message.edit_text( # type: ignore
        "Выберите одну из предложенных подписок:",
        reply_markup=plans_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("plan:"))
async def show_network_selection(callback: CallbackQuery) -> None:
    plan_id = callback.data.split(":")[1]  # type: ignore
    plan = SUBSCRIPTION_PLANS.get(plan_id)
    if not plan:
        await callback.answer("Неизвестный план", show_alert=True)
        return

    PLAN_RANK = {"1month": 1, "3months": 2, "forever": 3}
    sub = get_user_subscription(callback.from_user.id)
    if sub:
        current_rank = PLAN_RANK.get(sub["plan"], 0)
        selected_rank = PLAN_RANK.get(plan_id, 0)
        if selected_rank == current_rank:
            await callback.answer("Этот тип подписки уже активирован.", show_alert=True)
            return
        if selected_rank < current_rank:
            await callback.answer("У вас уже приобретён план лучше.", show_alert=True)
            return

    logger.info(f"User {callback.from_user.id} selected plan {plan_id}")
    await callback.message.edit_text(  # type: ignore
        f"Оплата подписки: {plan['label']} - {plan['price']}$\n\n"
        f"Выберите сеть для перевода:",
        reply_markup=network_kb(plan_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("net:"))
async def show_token_selection(callback: CallbackQuery) -> None:
    _, plan_id, network = callback.data.split(":")  # type: ignore
    plan = SUBSCRIPTION_PLANS.get(plan_id)
    net = SUPPORTED_NETWORKS.get(network)
    if not plan or not net:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return

    cancel_user_payment_sessions(callback.from_user.id)
    logger.info(f"User {callback.from_user.id} selected network {network}")
    await callback.message.edit_text(  # type: ignore
        f"Оплата подписки: {plan['label']} - {plan['price']}$\n"
        f"Сеть: {net['name']}\n\n"
        f"Выберите токен для оплаты:",
        reply_markup=token_kb(plan_id, network),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("token:"))
async def show_payment(callback: CallbackQuery, state: FSMContext) -> None:
    _, plan_id, network, token = callback.data.split(":")  # type: ignore
    plan = SUBSCRIPTION_PLANS.get(plan_id)
    net = SUPPORTED_NETWORKS.get(network)
    tok = SUPPORTED_TOKENS.get(token)
    if not plan or not net or not tok:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return

    user_id = callback.from_user.id

    # ИЗМЕНЕНИЕ: получаем мастер-кошелек вместо персонального
    wallet_address = get_master_wallet_address(network)
    if not wallet_address:
        await callback.answer("Ошибка: кошелёк не настроен", show_alert=True)
        return

    # Создаем сессию (from_block = 0, т.к. проверка будет вручную по хэшу)
    session_id = create_payment_session(user_id, plan_id, network, token, 0)

    # Сохраняем данные в FSM context
    await state.update_data(
        plan_id=plan_id,
        network=network,
        token=token,
        session_id=session_id,
    )

    text = (
        f"Оплата подписки: {plan['label']}\n\n"
        f"Переведите {plan['price']}$ {tok['name']} в сети {net['name']} "
        f"на адрес ниже:\n\n"
        f"`{wallet_address}`\n\n"
        f"После перевода нажмите \"Подтвердить оплату\" и отправьте хэш транзакции."
    )

    logger.info(f"User {user_id} selected {tok['name']} on {net['name']}, master wallet: {wallet_address}")
    await callback.message.edit_text(  # type: ignore
        text,
        reply_markup=payment_kb(plan_id, network, token),
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_payment_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Хендлер кнопки 'Подтвердить оплату' - переводит в режим ожидания хэша."""
    _, plan_id, network, token = callback.data.split(":")  # type: ignore
    user_id = callback.from_user.id

    plan = SUBSCRIPTION_PLANS.get(plan_id, {})
    net = SUPPORTED_NETWORKS.get(network, {})
    tok = SUPPORTED_TOKENS.get(token, {})

    # Сохраняем контекст в FSM
    await state.update_data(
        plan_id=plan_id,
        network=network,
        token=token,
    )

    # Получаем тип сети для подсказки формата
    net_type = net.get("type", "")
    if net_type == "solana":
        format_hint = "Формат: base58-подпись (обычно 87-88 символов)"
    else:
        format_hint = "Формат: 0x + 64 символа (например: 0x1234...abcd)"

    text = (
        f"Отправьте хэш транзакции для подтверждения оплаты.\n\n"
        f"План: {plan.get('label', plan_id)}\n"
        f"Сеть: {net.get('name', network)}\n"
        f"Токен: {tok.get('name', token)}\n\n"
        f"{format_hint}\n\n"
        f"Отмена: /start"
    )

    await callback.message.edit_text(text, reply_markup=None)  # type: ignore
    await state.set_state(PaymentStates.waiting_for_tx_hash)
    await callback.answer()
    logger.info(f"User {user_id} entered tx hash input mode for {plan_id}/{network}/{token}")


@router.message(PaymentStates.waiting_for_tx_hash, F.text)
async def process_tx_hash(message: Message, state: FSMContext, bot: Bot) -> None:
    """Обработка хэша транзакции от пользователя."""
    import os

    user_id = message.from_user.id  # type: ignore
    tx_hash = message.text.strip()  # type: ignore

    # Получаем данные из FSM
    data = await state.get_data()
    plan_id = data.get("plan_id")
    network = data.get("network")
    token = data.get("token")
    session_id = data.get("session_id")

    if not plan_id or not network or not token:
        await message.answer(
            "Ошибка: данные сессии потеряны. Начните заново: /start",
            reply_markup=back_kb(),
        )
        await state.clear()
        return

    # Показываем индикатор "бот печатает"
    await bot.send_chat_action(message.chat.id, "typing")

    # Проверяем транзакцию
    logger.info(f"User {user_id} submitted tx_hash: {tx_hash} for {network}/{token}")

    is_valid, error_msg, amount = await verify_transaction_by_hash(
        tx_hash=tx_hash,
        network=network,
        token=token,
        plan=plan_id,
    )

    if not is_valid:
        # Ошибка - даём возможность повторить
        await message.answer(
            f"❌ Ошибка проверки транзакции:\n{error_msg}\n\n"
            f"Попробуйте ещё раз или отмените: /start"
        )
        logger.warning(f"Transaction verification failed for user {user_id}: {error_msg}")
        return

    # Успех - активируем подписку
    plan = SUBSCRIPTION_PLANS.get(plan_id, {})
    tok = SUPPORTED_TOKENS.get(token, {})
    net = SUPPORTED_NETWORKS.get(network, {})

    # Записываем платёж
    record_payment(user_id, amount, plan_id, network, token, tx_hash)

    # Активируем подписку
    activate_subscription(user_id, plan_id)

    # Обновляем tx_hash в сессии и помечаем completed
    if session_id:
        update_payment_session_tx_hash(session_id, tx_hash)
        complete_payment_session(session_id)

    # Генерируем пригласительную ссылку
    try:
        invite_link = await create_invite_link(bot, user_id, plan.get('label', plan_id))
        link_text = f"\n\nВаша одноразовая ссылка для вступления в канал:\n{invite_link}\n\n⚠️ Ссылка станет недействительной после присоединения одного человека!"
    except Exception as e:
        logger.error(f"Failed to create invite link for user {user_id}: {e}")
        link_text = "\n\n⚠️ Не удалось создать пригласительную ссылку. Обратитесь в поддержку."

    success_text = (
        f"✅ Оплата подтверждена!\n\n"
        f"Подписка: {plan.get('label', plan_id)}\n"
        f"Сумма: {amount} {tok.get('name', token)}\n"
        f"Сеть: {net.get('name', network)}\n"
        f"Tx: `{tx_hash}`"
        f"{link_text}"
    )

    await message.answer(
        success_text,
        reply_markup=back_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )

    # Нотификация администратора
    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    if admin_chat_id:
        try:
            await bot.send_message(
                int(admin_chat_id),
                f"💰 Новая оплата:\n"
                f"User ID: {user_id}\n"
                f"План: {plan.get('label', plan_id)}\n"
                f"Сумма: {amount} {tok.get('name', token)}\n"
                f"Сеть: {net.get('name', network)}\n"
                f"Tx: {tx_hash}"
            )
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")

    # Очищаем FSM
    await state.clear()

    logger.info(f"Payment confirmed for user {user_id}: {amount} {token} on {network}, tx={tx_hash}")


@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    subscription = get_user_subscription(user_id)

    if subscription:
        plan_info = SUBSCRIPTION_PLANS.get(subscription["plan"], {})
        status_text = f"Активная подписка: {plan_info.get('label', subscription['plan'])}"
        if subscription["expires_at"]:
            expires_utc = datetime.fromisoformat(subscription["expires_at"])
            expires_msk = expires_utc.astimezone(timezone(timedelta(hours=3)))
            status_text += f"\nДействует до: {expires_msk.strftime('%d.%m.%Y %H:%M:%S')} (по МСК)"
    else:
        status_text = "У вас нет активной подписки."

    text = (
        f"Личный кабинет\n\n"
        f"ID: {user_id}\n"
        f"{status_text}"
    )

    logger.info(f"User {user_id} opened profile")
    await callback.message.edit_text(text, reply_markup=back_kb()) # type: ignore
    await callback.answer()


@router.callback_query(F.data == "faq")
async def show_faq(callback: CallbackQuery) -> None:
    logger.info(f"User {callback.from_user.id} opened FAQ")
    await callback.message.edit_text(FAQ_TEXT, reply_markup=back_kb()) # type: ignore
    await callback.answer()


# ── Настройка команд бота ───────────────────────────────────


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Перезапустить бота"),
        BotCommand(command="chatid", description="Получить ID чата"),
    ])
    logger.info("Bot commands configured")
