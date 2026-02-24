from aiogram import Bot, Router, F
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import TelegramForbiddenError
from datetime import datetime, timezone, timedelta
from aiogram.enums import ParseMode
import os
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext

from src.logger import logger
from src.blockchain import get_current_block, verify_transaction_by_hash
from src.states import PaymentStates, WithdrawalStates
from src.payments import (
    SUBSCRIPTION_PLANS,
    SUPPORTED_NETWORKS,
    SUPPORTED_TOKENS,
    cancel_user_payment_sessions,
    create_invite_links,
    create_payment_session,
    get_user_subscription,
    get_master_wallet_address,
    update_payment_session_tx_hash,
    complete_payment_session,
    record_payment,
    activate_subscription,
    activate_free_subscription,
    update_user_profile,
    get_user_profile,
    get_referral_link,
    use_referral_link,
    get_user_referral_code,
    get_user_referral_info,
    get_plan_price_for_user,
    get_user_own_referral_code,
    get_referral_balance,
    get_referral_paid_count,
    get_referral_percent,
    add_referral_balance,
    deduct_referral_balance,
    create_withdrawal_request,
    get_withdrawal_request,
    update_withdrawal_request,
    get_withdrawal_wallet,
)

router = Router()


# ── Вспомогательные функции ─────────────────────────────────────


async def safe_send_message(message: Message, text: str, **kwargs):
    """Безопасная отправка сообщения с обработкой блокировки бота."""
    try:
        return await message.answer(text, **kwargs)
    except TelegramForbiddenError:
        user = message.from_user
        if user:
            logger.warning(f"User {user.id} has blocked the bot")
        return None


async def safe_edit_message(callback: CallbackQuery, text: str, **kwargs):
    """Безопасное редактирование сообщения с обработкой блокировки бота."""
    try:
        # Проверяем что message доступно и не является InaccessibleMessage
        if callback.message and hasattr(callback.message, 'edit_text'):
            return await callback.message.edit_text(text, **kwargs) # type: ignore
    except TelegramForbiddenError:
        user = callback.from_user
        if user:
            logger.warning(f"User {user.id} has blocked the bot")
        return None


MAIN_MENU_TEXT = (
    "Добро пожаловать! Ты почти участник DAO 😈\n\n"
    "Выбирай подписку и получай доступ ко всем ресурсам\n"
    "Личный кабинет даст информацию о подписке\n"
    "Есть вопрос? Пиши в ЛС\n\n"
    "Выбирай нужное действие 👇🏻"
)


# ── Клавиатуры ──────────────────────────────────────────────


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить подписку", callback_data="subscribe")],
        [InlineKeyboardButton(text="Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton(text="Задать вопрос", url="https://t.me/aNd3x")],
    ])


def plans_kb(custom_prices: list[float] | None = None) -> InlineKeyboardMarkup:
    """Создает клавиатуру с планами подписок.

    Args:
        custom_prices: список из 3 цен [price_1month, price_3months, price_forever]
                      Если None - используются дефолтные цены
                      Поддерживаются float значения (35.5, 90.99)
    """
    buttons = []
    plan_ids = list(SUBSCRIPTION_PLANS.keys())

    for idx, (plan_id, plan) in enumerate(SUBSCRIPTION_PLANS.items()):
        if custom_prices and idx < len(custom_prices):
            price = custom_prices[idx]
        else:
            price = plan['price']

        # Форматируем цену: если целое число, показываем без .0
        price_str = f"{price:.2f}".rstrip('0').rstrip('.') if isinstance(price, float) else str(price)

        buttons.append([InlineKeyboardButton(
            text=f"{plan['label']} - {price_str}$",
            callback_data=f"plan:{plan_id}",
        )])
    buttons.append([InlineKeyboardButton(text="< Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def token_kb(plan_id: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора токена (монеты)."""
    buttons = []
    for token_id, token in SUPPORTED_TOKENS.items():
        buttons.append([InlineKeyboardButton(
            text=token["name"],
            callback_data=f"token:{plan_id}:{token_id}",
        )])
    buttons.append([InlineKeyboardButton(text="< Назад", callback_data="subscribe")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def network_kb(plan_id: str, token: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора сети с учетом выбранного токена."""
    buttons = []
    token_info = SUPPORTED_TOKENS.get(token)
    if token_info:
        for net_id, net in SUPPORTED_NETWORKS.items():
            # Показываем только сети, в которых доступен выбранный токен
            if net_id in token_info["addresses"]:
                buttons.append([InlineKeyboardButton(
                    text=net["name"],
                    callback_data=f"net:{plan_id}:{token}:{net_id}",
                )])
    buttons.append([InlineKeyboardButton(text="< Назад", callback_data=f"plan:{plan_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payment_kb(plan_id: str, token: str, network: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить оплату", callback_data=f"confirm:{plan_id}:{token}:{network}")],
        [InlineKeyboardButton(text="< Назад", callback_data=f"token:{plan_id}:{token}")],
    ])


def back_kb(callback_data: str = "back_to_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="< Назад", callback_data=callback_data)],
    ])


def profile_kb(can_withdraw: bool) -> InlineKeyboardMarkup:
    buttons = []
    if can_withdraw:
        buttons.append([InlineKeyboardButton(text="💸 Вывод", callback_data="withdrawal_start")])
    buttons.append([InlineKeyboardButton(text="< Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Хендлеры ────────────────────────────────────────────────


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_referral(message: Message, state: FSMContext) -> None:
    """Обработка старта с реферальным кодом."""
    user = message.from_user  # type: ignore
    bot = message.bot  # type: ignore

    # Получаем реферальный код из deep link параметра
    args = message.text.split(maxsplit=1)  # type: ignore
    referral_code = args[1] if len(args) > 1 else None

    logger.info(f"User {user.id} started the bot with referral code: {referral_code}")  # type: ignore

    # Сохраняем/обновляем профиль пользователя
    update_user_profile(user.id, user.username, user.first_name, user.last_name) # type: ignore

    # Пытаемся использовать реферальную ссылку
    if referral_code:
        ref_link = get_referral_link(referral_code)
        logger.info(f"Referral link lookup: {ref_link}")

        if ref_link and ref_link["is_active"]:
            # Проверяем, является ли это мгновенной бесплатной ссылкой
            is_instant = ref_link.get("is_instant", 0) == 1
            free_days = ref_link.get("free_days")

            if is_instant and free_days:
                # Пытаемся использовать ссылку (она одноразовая)
                success = use_referral_link(user.id, referral_code) # type: ignore

                if success:
                    logger.info(f"✓ User {user.id} successfully used instant referral code {referral_code}") # type: ignore

                    # Активируем бесплатную подписку (или продлеваем существующую)
                    result = activate_free_subscription(user.id, free_days) # type: ignore
                    action = result.get("action")

                    if action == "skipped":
                        # У пользователя бессрочная подписка
                        await safe_send_message(
                            message,
                            "У вас уже есть бессрочная подписка! 🎉\n\n"
                            "Бесплатные дни не могут быть добавлены к бессрочной подписке."
                        )
                        await state.clear()
                        cancel_user_payment_sessions(user.id) # type: ignore
                        return

                    # Генерируем пригласительные ссылки для чата и группы
                    try:
                        invite_links = await create_invite_links(bot, user.id, f"Бесплатная подписка ({free_days} дней)") # type: ignore

                        link_text = "\n\n📱 Ваши одноразовые ссылки для вступления:"

                        if invite_links.get("chat"):
                            link_text += f"\n\n🔹 Чат:\n{invite_links['chat']}"
                        else:
                            link_text += f"\n\n🔹 Чат:\n⚠️ Не удалось создать ссылку"

                        if invite_links.get("group"):
                            link_text += f"\n\n🔹 Группа:\n{invite_links['group']}"
                        else:
                            link_text += f"\n\n🔹 Группа:\n⚠️ Не удалось создать ссылку"

                        link_text += "\n\n⚠️ Каждая ссылка станет недействительной после присоединения одного человека!"

                    except Exception as e:
                        logger.error(f"Failed to create invite links for user {user.id}: {e}") # type: ignore
                        link_text = "\n\n⚠️ Не удалось создать пригласительные ссылки. Обратитесь в поддержку."

                    # Формируем сообщение в зависимости от действия
                    if action == "created":
                        success_text = (
                            f"🎉 Поздравляем!\n\n"
                            f"Вам активирована бесплатная подписка на {free_days} дней!"
                            f"{link_text}"
                        )
                    elif action == "extended":
                        # Вычисляем новую дату истечения для отображения
                        expires_at = result.get("expires_at")
                        if expires_at:
                            expires_dt = datetime.fromisoformat(expires_at).astimezone(timezone(timedelta(hours=3)))
                            expires_str = expires_dt.strftime('%d.%m.%Y %H:%M')
                        else:
                            expires_str = "не определена"

                        success_text = (
                            f"🎉 Отлично!\n\n"
                            f"К вашей подписке добавлено {free_days} дней!\n\n"
                            f"Новая дата истечения: {expires_str} (МСК)"
                            f"{link_text}"
                        )
                    else:
                        success_text = (
                            f"🎉 Поздравляем!\n\n"
                            f"Вам активирована бесплатная подписка на {free_days} дней!"
                            f"{link_text}"
                        )

                    await safe_send_message(message, success_text)

                    # Очищаем FSM и сессии
                    await state.clear()
                    cancel_user_payment_sessions(user.id) # type: ignore
                    return  # Не показываем главное меню, т.к. уже отправили сообщение с доступом
                else:
                    logger.warning(f"✗ User {user.id} failed to use instant referral code {referral_code} (already used or limit reached)") # type: ignore
                    await safe_send_message(
                        message,
                        "❌ Эта реферальная ссылка уже была использована или недействительна."
                    )
                    await state.clear()
                    cancel_user_payment_sessions(user.id) # type: ignore
                    return
            else:
                # Обычная реферальная ссылка (не мгновенная)
                success = use_referral_link(user.id, referral_code) # type: ignore
                if success:
                    logger.info(f"✓ User {user.id} successfully used referral code {referral_code}") # type: ignore
                else:
                    logger.warning(f"✗ User {user.id} failed to use referral code {referral_code} (already used or limit reached)") # type: ignore
        else:
            logger.warning(f"✗ Referral link {referral_code} not found or inactive")

    # Очищаем FSM при рестарте
    await state.clear()

    cancel_user_payment_sessions(user.id) # type: ignore
    await safe_send_message(message, MAIN_MENU_TEXT, reply_markup=main_menu_kb())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Обработка обычного старта без параметров."""
    user = message.from_user  # type: ignore
    logger.info(f"User {user.id} started the bot") # type: ignore

    # Сохраняем/обновляем профиль пользователя
    update_user_profile(user.id, user.username, user.first_name, user.last_name) # type: ignore

    # Очищаем FSM при рестарте
    await state.clear()

    cancel_user_payment_sessions(user.id) # type: ignore
    await safe_send_message(message, MAIN_MENU_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery) -> None:
    user = callback.from_user
    update_user_profile(user.id, user.username, user.first_name, user.last_name)
    cancel_user_payment_sessions(user.id)
    await safe_edit_message(callback, MAIN_MENU_TEXT, reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "subscribe")
async def show_plans(callback: CallbackQuery) -> None:
    user = callback.from_user
    update_user_profile(user.id, user.username, user.first_name, user.last_name)
    logger.info(f"User {user.id} opened subscription plans")

    # Проверяем активную подписку
    subscription = get_user_subscription(user.id)
    if subscription and subscription["expires_at"]:
        # Есть активная подписка с датой истечения
        expires_dt = datetime.fromisoformat(subscription["expires_at"])
        now = datetime.now(timezone.utc)
        time_until_expiry = expires_dt - now

        # Если до истечения больше 3 дней - блокируем продление
        if time_until_expiry > timedelta(days=3):
            expires_msk = expires_dt.astimezone(timezone(timedelta(hours=3)))
            await safe_edit_message(
                callback,
                f"У вас уже есть активная подписка!\n\n"
                f"Дата истечения: {expires_msk.strftime('%d.%m.%Y %H:%M')} (МСК)\n\n"
                f"Продление станет доступно за 3 дня до истечения подписки.",
                reply_markup=back_kb(),
            )
            await callback.answer()
            return

    # Проверяем, есть ли у пользователя реферальный код с кастомными ценами
    custom_prices = None
    referral_code = get_user_referral_code(user.id)
    logger.info(f"Checking referral code for user {user.id}: {referral_code}")

    if referral_code:
        ref_link = get_referral_link(referral_code)
        logger.info(f"Referral link data: {ref_link}")

        if ref_link and ref_link["custom_prices"]:
            # Парсим кастомные цены из строки "35.5,90,200.99" (поддержка float)
            try:
                custom_prices = [float(p.strip()) for p in ref_link["custom_prices"].split(",")]
                logger.info(f"✓ Applying custom prices for user {user.id}: {custom_prices}")
            except ValueError:
                logger.warning(f"Failed to parse custom prices: {ref_link['custom_prices']}")
        else:
            logger.info(f"No custom prices for referral code {referral_code}")
    else:
        logger.info(f"No referral code for user {user.id}")

    # Если можем продлять - показываем информацию об этом
    message_text = "Выберите одну из предложенных подписок:"
    if subscription and subscription["expires_at"]:
        expires_dt = datetime.fromisoformat(subscription["expires_at"])
        expires_msk = expires_dt.astimezone(timezone(timedelta(hours=3)))
        message_text = (
            f"📝 У вас активная подписка до {expires_msk.strftime('%d.%m.%Y %H:%M')} (МСК)\n\n"
            f"При оплате новой подписки дни будут добавлены к текущей!\n\n"
            f"Выберите подписку для продления:"
        )

    await safe_edit_message(
        callback,
        message_text,
        reply_markup=plans_kb(custom_prices),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("plan:"))
async def show_token_selection(callback: CallbackQuery) -> None:
    """Показывает выбор токена (монеты) после выбора плана."""
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

    user_id = callback.from_user.id

    # Получаем цену с учетом реферальной ссылки
    price = get_plan_price_for_user(user_id, plan_id)
    price_str = f"{price:.2f}".rstrip('0').rstrip('.') if isinstance(price, float) else str(price)

    logger.info(f"User {user_id} selected plan {plan_id}, price: {price}")
    await safe_edit_message(
        callback,
        f"Оплата подписки: {plan['label']} - {price_str}$\n\n"
        f"Выберите монету для оплаты:",
        reply_markup=token_kb(plan_id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^token:[^:]+:[^:]+$"))
async def show_network_selection(callback: CallbackQuery) -> None:
    """Показывает выбор сети после выбора токена."""
    _, plan_id, token = callback.data.split(":")  # type: ignore
    plan = SUBSCRIPTION_PLANS.get(plan_id)
    tok = SUPPORTED_TOKENS.get(token)
    if not plan or not tok:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return

    user_id = callback.from_user.id
    cancel_user_payment_sessions(user_id)

    # Получаем цену с учетом реферальной ссылки
    price = get_plan_price_for_user(user_id, plan_id)
    price_str = f"{price:.2f}".rstrip('0').rstrip('.') if isinstance(price, float) else str(price)

    logger.info(f"User {user_id} selected token {token}")
    await safe_edit_message(
        callback,
        f"Оплата подписки: {plan['label']} - {price_str}$\n"
        f"Монета: {tok['name']}\n\n"
        f"Выберите сеть для перевода:",
        reply_markup=network_kb(plan_id, token),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("net:"))
async def show_payment(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает платежную информацию после выбора сети."""
    parts = callback.data.split(":")  # type: ignore
    if len(parts) != 4:  # net:plan_id:token:network
        await callback.answer("Неизвестный параметр", show_alert=True)
        return

    _, plan_id, token, network = parts
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

    # Получаем цену с учетом реферальной ссылки
    price = get_plan_price_for_user(user_id, plan_id)
    price_str = f"{price:.2f}".rstrip('0').rstrip('.') if isinstance(price, float) else str(price)

    text = (
        f"Оплата подписки: {plan['label']}\n\n"
        f"Переведите {price_str}$ {tok['name']} в сети {net['name']} "
        f"на адрес ниже:\n\n"
        f"<code>{wallet_address}</code>\n\n"
        f"После перевода нажмите \"Подтвердить оплату\" и отправьте хэш транзакции."
    )

    logger.info(f"User {user_id} selected {tok['name']} on {net['name']}, master wallet: {wallet_address}")
    await safe_edit_message(
        callback,
        text,
        reply_markup=payment_kb(plan_id, token, network),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_payment_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Хендлер кнопки 'Подтвердить оплату' - переводит в режим ожидания хэша."""
    parts = callback.data.split(":")  # type: ignore
    if len(parts) != 4:  # confirm:plan_id:token:network
        await callback.answer("Ошибка формата данных", show_alert=True)
        return

    _, plan_id, token, network = parts
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

    text = (
        f"Отправьте хэш транзакции для подтверждения оплаты.\n\n"
        f"План: {plan.get('label', plan_id)}\n"
        f"Монета: {tok.get('name', token)}\n"
        f"Сеть: {net.get('name', network)}"
    )

    await safe_edit_message(callback, text, reply_markup=None)
    await state.set_state(PaymentStates.waiting_for_tx_hash)
    await callback.answer()
    logger.info(f"User {user_id} entered tx hash input mode for {plan_id}/{token}/{network}")


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
        await safe_send_message(
            message,
            "Ошибка: данные сессии потеряны. Начните заново.",
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
        user_id=user_id,
    )

    if not is_valid:
        # Ошибка - даём возможность повторить
        await safe_send_message(
            message,
            f"❌ Ошибка проверки транзакции:\n{error_msg}\n\n"
            f"Попробуйте ещё раз."
        )
        logger.warning(f"Transaction verification failed for user {user_id}: {error_msg}")
        return

    # Успех - активируем подписку
    plan = SUBSCRIPTION_PLANS.get(plan_id, {})
    tok = SUPPORTED_TOKENS.get(token, {})
    net = SUPPORTED_NETWORKS.get(network, {})

    # Записываем платёж
    record_payment(user_id, amount, plan_id, network, token, tx_hash)

    # Активируем подписку (или продлеваем существующую)
    result = activate_subscription(user_id, plan_id, amount)
    action = result.get("action")
    expires_at = result.get("expires_at")
    days_added = result.get("days_added")

    # Обновляем tx_hash в сессии и помечаем completed
    if session_id:
        update_payment_session_tx_hash(session_id, tx_hash)
        complete_payment_session(session_id)

    # Генерируем пригласительные ссылки для чата и группы
    try:
        invite_links = await create_invite_links(bot, user_id, plan.get('label', plan_id))

        link_text = "\n\n📱 Ваши одноразовые ссылки для вступления:"

        if invite_links.get("chat"):
            link_text += f"\n\n🔹 Чат:\n{invite_links['chat']}"
        else:
            link_text += f"\n\n🔹 Чат:\n⚠️ Не удалось создать ссылку"

        if invite_links.get("group"):
            link_text += f"\n\n🔹 Группа:\n{invite_links['group']}"
        else:
            link_text += f"\n\n🔹 Группа:\n⚠️ Не удалось создать ссылку"

        link_text += "\n\n⚠️ Каждая ссылка станет недействительной после присоединения одного человека!"

    except Exception as e:
        logger.error(f"Failed to create invite links for user {user_id}: {e}")
        link_text = "\n\n⚠️ Не удалось создать пригласительные ссылки. Обратитесь в поддержку."

    # Формируем сообщение в зависимости от действия
    if action == "extended" and expires_at and days_added:
        # Продление существующей подписки
        expires_dt = datetime.fromisoformat(expires_at).astimezone(timezone(timedelta(hours=3)))
        expires_str = expires_dt.strftime('%d.%m.%Y %H:%M')

        success_text = (
            f"✅ Оплата подтверждена!\n\n"
            f"К вашей подписке добавлено {days_added} дней!\n\n"
            f"Новая дата истечения: {expires_str} (МСК)\n\n"
            f"Сумма: {amount} {tok.get('name', token)}\n"
            f"Сеть: {net.get('name', network)}\n"
            f"Tx: <code>{tx_hash}</code>"
            f"{link_text}"
        )
    else:
        # Новая подписка
        success_text = (
            f"✅ Оплата подтверждена!\n\n"
            f"Подписка: {plan.get('label', plan_id)}\n"
            f"Сумма: {amount} {tok.get('name', token)}\n"
            f"Сеть: {net.get('name', network)}\n"
            f"Tx: <code>{tx_hash}</code>"
            f"{link_text}"
        )

    await safe_send_message(
        message,
        success_text,
        parse_mode=ParseMode.HTML,
    )

    # Нотификация администратора
    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    if admin_chat_id:
        try:
            # Получаем профиль пользователя для отображения username
            profile = get_user_profile(user_id)
            user_display = f"{user_id}"
            if profile:
                if profile.get("username"):
                    user_display += f" | @{profile['username']}"
                elif profile.get("first_name"):
                    user_display += f" | {profile['first_name']}"

            # Получаем реферальную информацию
            ref_info = get_user_referral_info(user_id)
            ref_text = ""
            if ref_info:
                ref_name = ref_info.get('name') or "без названия"
                ref_text = f"\nРеф. ссылка: {ref_name} ({ref_info['code']})"

            await bot.send_message(
                int(admin_chat_id),
                f"💰 Новая оплата:\n"
                f"Пользователь: {user_display}\n"
                f"План: {plan.get('label', plan_id)}\n"
                f"Сумма: {amount} {tok.get('name', token)}\n"
                f"Сеть: {net.get('name', network)}\n"
                f"Tx: {tx_hash}"
                f"{ref_text}"
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

    # Реферальный баланс
    balance = get_referral_balance(user_id)
    balance_text = f"\n\n💰 Реферальный баланс: {balance:.2f}$"
    if balance >= 10:
        balance_text += "\n(доступен вывод)"
    else:
        balance_text += f"\n(минимум для вывода: 10$)"

    # Личная реферальная ссылка (для всех пользователей, у кого она есть)
    referral_text = ""
    own_referral_code = get_user_own_referral_code(user_id)
    if own_referral_code:
        bot_info = await callback.bot.get_me()  # type: ignore
        bot_username = bot_info.username if bot_info and bot_info.username else "PaymentDAOBot"
        referral_url = f"https://t.me/{bot_username}?start={own_referral_code}"
        paid_count = get_referral_paid_count(user_id)
        current_percent = get_referral_percent(paid_count)
        next_percent_info = ""
        if paid_count == 0:
            next_percent_info = " → после 1-го станет 25%"
        elif paid_count == 1:
            next_percent_info = " → после 2-го станет 30%"
        referral_text = (
            f"\n\n🔗 Ваша реферальная ссылка:\n<code>{referral_url}</code>\n\n"
            f"👥 Приведено рефералов: {paid_count}\n"
            f"💹 Ваш процент: {current_percent}%{next_percent_info}\n\n"
            f"Приглашайте друзей и получайте {current_percent}% от суммы их оплаты ежемесячно!"
        )

    text = (
        f"Личный кабинет\n\n"
        f"ID: {user_id}\n"
        f"{status_text}"
        f"{balance_text}"
        f"{referral_text}"
    )

    logger.info(f"User {user_id} opened profile, balance={balance:.2f}$")
    await safe_edit_message(
        callback, text, reply_markup=profile_kb(balance >= 10), parse_mode=ParseMode.HTML
    )
    await callback.answer()




# ── Вывод реферального баланса ──────────────────────────────


_WITHDRAWAL_NETWORKS = {
    "base": "Base",
    "arbitrum": "Arbitrum One",
}


@router.callback_query(F.data == "withdrawal_start")
async def withdrawal_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало процесса вывода: выбор сети."""
    user_id = callback.from_user.id
    balance = get_referral_balance(user_id)

    if balance < 10:
        await callback.answer("Недостаточно средств. Минимум для вывода: 10$", show_alert=True)
        return

    await state.update_data(withdrawal_balance=balance)
    await state.set_state(WithdrawalStates.waiting_for_network)

    network_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Base", callback_data="withdrawal_net:base")],
        [InlineKeyboardButton(text="Arbitrum One", callback_data="withdrawal_net:arbitrum")],
        [InlineKeyboardButton(text="< Назад", callback_data="profile")],
    ])

    text = (
        f"💸 Вывод средств\n\n"
        f"Ваш баланс: {balance:.2f}$\n\n"
        f"Минимальная сумма вывода: 10$\n\n"
        f"Выберите сеть для получения USDC:"
    )
    await safe_edit_message(callback, text, reply_markup=network_kb)
    await callback.answer()


@router.callback_query(WithdrawalStates.waiting_for_network, F.data.startswith("withdrawal_net:"))
async def withdrawal_network_selected(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал сеть — просим ввести сумму."""
    network = callback.data.split(":")[1]  # type: ignore
    if network not in _WITHDRAWAL_NETWORKS:
        await callback.answer("Неизвестная сеть", show_alert=True)
        return

    data = await state.get_data()
    balance = data.get("withdrawal_balance", 0.0)

    await state.update_data(withdrawal_network=network)
    await state.set_state(WithdrawalStates.waiting_for_amount)

    net_name = _WITHDRAWAL_NETWORKS[network]
    await safe_edit_message(
        callback,
        f"💸 Вывод средств\n\n"
        f"Сеть: {net_name}\n"
        f"Ваш баланс: {balance:.2f}$\n\n"
        f"Введите сумму для вывода (от 10$ до {balance:.2f}$):",
        reply_markup=None,
    )
    await callback.answer()


@router.message(WithdrawalStates.waiting_for_amount, F.text)
async def process_withdrawal_amount(message: Message, state: FSMContext) -> None:
    """Обработка введённой суммы вывода."""
    user_id = message.from_user.id  # type: ignore
    data = await state.get_data()
    network = data.get("withdrawal_network", "")
    net_name = _WITHDRAWAL_NETWORKS.get(network, network)

    try:
        amount = float(message.text.strip().replace(",", "."))  # type: ignore
    except ValueError:
        await safe_send_message(message, "Введите корректную сумму числом. Например: 30")
        return

    if amount < 10:
        await safe_send_message(message, "Минимальная сумма вывода: 10$. Введите сумму ещё раз:")
        return

    actual_balance = get_referral_balance(user_id)
    if amount > actual_balance:
        await safe_send_message(
            message,
            f"Недостаточно средств. Ваш баланс: {actual_balance:.2f}$\nВведите сумму ещё раз:"
        )
        return

    await state.update_data(withdrawal_amount=amount, withdrawal_balance=actual_balance)
    await state.set_state(WithdrawalStates.waiting_for_address)

    await safe_send_message(
        message,
        f"Сумма: {amount:.2f}$\nСеть: {net_name}\n\n"
        f"Введите EVM-адрес кошелька (0x...) для получения USDC:"
    )


@router.message(WithdrawalStates.waiting_for_address, F.text)
async def process_withdrawal_address(message: Message, state: FSMContext, bot: Bot) -> None:
    """Обработка EVM-адреса — создаём запрос и отправляем уведомление админам."""
    import re

    user_id = message.from_user.id  # type: ignore
    evm_address = message.text.strip()  # type: ignore

    # Валидация EVM-адреса (0x + 40 hex-символов)
    if not re.match(r"^0x[0-9a-fA-F]{40}$", evm_address):
        await safe_send_message(
            message,
            "Неверный EVM-адрес. Адрес должен начинаться с 0x и содержать 42 символа.\nПопробуйте ещё раз:"
        )
        return

    data = await state.get_data()
    amount = data.get("withdrawal_amount", 0.0)
    network = data.get("withdrawal_network", "")
    net_name = _WITHDRAWAL_NETWORKS.get(network, network)

    # Создаём запрос (списывает баланс)
    request_id = create_withdrawal_request(user_id, amount, evm_address, network)
    if request_id is None:
        await safe_send_message(
            message,
            "Ошибка при создании запроса. Возможно, недостаточно средств или уже есть активный запрос.\n"
            "Нажмите /start и попробуйте позже."
        )
        await state.clear()
        return

    # Формируем уведомление для администраторов
    profile = get_user_profile(user_id)
    username = profile.get("username") if profile else None
    user_display = f"@{username}" if username else f"id:{user_id}"

    now_msk = datetime.now(timezone(timedelta(hours=3)))
    time_str = now_msk.strftime("%H:%M:%S")

    notification_text = (
        f"Пользователь {user_display} оставил запрос на вывод.\n"
        f"Сумма: {amount:.0f}$\n"
        f"Сеть: {net_name}\n"
        f"Адрес: {evm_address}\n"
        f"Время: {time_str}(МСК)"
    )

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data=f"confirm_withdrawal:{request_id}")]
    ])

    withdrawal_chat_id_str = os.getenv("WITHDRAWAL_CHAT_ID", "").strip()
    if not withdrawal_chat_id_str:
        logger.error("WITHDRAWAL_CHAT_ID не задан в .env")
        add_referral_balance(user_id, amount)
        update_withdrawal_request(request_id, status="failed")
        await safe_send_message(
            message,
            "Ошибка конфигурации бота. Обратитесь в поддержку.\nБаланс восстановлен."
        )
        await state.clear()
        return

    withdrawal_chat_id = int(withdrawal_chat_id_str)

    try:
        sent_msg = await bot.send_message(
            withdrawal_chat_id, notification_text, reply_markup=confirm_kb
        )
        update_withdrawal_request(
            request_id,
            notification_message_id=sent_msg.message_id,
            notification_chat_id=withdrawal_chat_id,
        )
        logger.info(f"Запрос на вывод #{request_id} отправлен в чат {withdrawal_chat_id}")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление о выводе: {e}")
        add_referral_balance(user_id, amount)
        update_withdrawal_request(request_id, status="failed")
        await safe_send_message(
            message,
            "Не удалось отправить запрос администратору. Баланс восстановлен.\nПопробуйте позже."
        )
        await state.clear()
        return

    await safe_send_message(
        message,
        f"✅ Запрос на вывод {amount:.0f}$ отправлен на подтверждение.\n\n"
        f"Сеть: {net_name}\n"
        f"Адрес: {evm_address}\n\n"
        f"Ожидайте — средства будут переведены в течение 24-х часов."
    )
    await state.clear()
    logger.info(f"Пользователь {user_id} оформил вывод #{request_id}: {amount}$ → {evm_address} ({network})")


@router.callback_query(F.data.startswith("confirm_withdrawal:"))
async def confirm_withdrawal_admin(callback: CallbackQuery) -> None:
    """Администратор подтверждает вывод — отправляем USDC пользователю."""
    raw_id = callback.data.split(":")[1]  # type: ignore
    if not raw_id.isdigit():
        await callback.answer("Неверный формат запроса", show_alert=True)
        return

    request_id = int(raw_id)
    request = get_withdrawal_request(request_id)

    if not request:
        await callback.answer("Запрос не найден", show_alert=True)
        return

    if request["status"] != "pending":
        await callback.answer("Запрос уже обработан", show_alert=True)
        return

    # Меняем сообщение на "ожидание"
    if callback.message and hasattr(callback.message, "edit_text"):
        try:
            await callback.message.edit_text(  # type: ignore
                "Запрос на вывод подтверждается - ожидание...",
                reply_markup=None,
            )
        except Exception:
            pass

    await callback.answer()

    update_withdrawal_request(request_id, status="confirming")

    withdrawal_wallet = get_withdrawal_wallet()
    if not withdrawal_wallet:
        logger.error("WITHDRAWAL_WALLET_KEY не задан в .env")
        add_referral_balance(request["user_id"], request["amount"])
        update_withdrawal_request(request_id, status="failed")
        if callback.message and hasattr(callback.message, "edit_text"):
            try:
                await callback.message.edit_text(  # type: ignore
                    "❌ Ошибка: кошелёк для вывода не настроен. Баланс пользователя восстановлен."
                )
            except Exception:
                pass
        return

    network = request.get("network") or "base"
    net_name = _WITHDRAWAL_NETWORKS.get(network, network)
    evm_address = request["evm_address"]

    try:
        from src.evm_sender import send_erc20_usdc

        tx_hash = await send_erc20_usdc(
            withdrawal_wallet["private_key"],
            evm_address,
            request["amount"],
            network,
        )

        update_withdrawal_request(request_id, status="completed", tx_hash=tx_hash)

        profile = get_user_profile(request["user_id"])
        username = profile.get("username") if profile else None
        user_display = f"@{username}" if username else f"id:{request['user_id']}"

        if callback.message and hasattr(callback.message, "edit_text"):
            try:
                await callback.message.edit_text(  # type: ignore
                    f"Запрос на вывод для {user_display} успешно подтвержден - средства отправлены!\n"
                    f"Адрес получателя: {evm_address}\n"
                    f"Сеть: {net_name}\n"
                    f"Tx_Hash: {tx_hash}"
                )
            except Exception:
                pass

        try:
            await callback.bot.send_message(  # type: ignore
                request["user_id"],
                f"✅ Вывод средств выполнен!\n\n"
                f"Сумма: {request['amount']:.0f}$\n"
                f"Сеть: {net_name}\n"
                f"Адрес: {evm_address}\n"
                f"Tx Hash: {tx_hash}",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {request['user_id']} о выводе: {e}")

        logger.info(f"Вывод #{request_id} выполнен: {request['amount']}$ → {evm_address} ({network}), tx={tx_hash}")

    except Exception as e:
        logger.error(f"Ошибка при отправке USDC для вывода #{request_id}: {e}")

        add_referral_balance(request["user_id"], request["amount"])
        update_withdrawal_request(request_id, status="failed")

        if callback.message and hasattr(callback.message, "edit_text"):
            try:
                await callback.message.edit_text(  # type: ignore
                    f"❌ Ошибка при отправке средств!\n"
                    f"Адрес: {evm_address}\n"
                    f"Сеть: {net_name}\n"
                    f"Сумма: {request['amount']:.0f}$\n\n"
                    f"Баланс пользователя восстановлен.\n"
                    f"Ошибка: {e}"
                )
            except Exception:
                pass


# ── Настройка команд бота ───────────────────────────────────


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Перезапустить бота"),
    ])
    logger.info("Bot commands configured")
