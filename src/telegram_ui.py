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
    create_invite_links,
    create_payment_session,
    get_user_subscription,
    get_master_wallet_address,
    update_payment_session_tx_hash,
    complete_payment_session,
    record_payment,
    activate_subscription,
    update_user_profile,
    get_user_profile,
    get_referral_link,
    use_referral_link,
    get_user_referral_code,
    get_user_referral_info,
    get_plan_price_for_user,
    get_user_own_referral_code,
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
            return await callback.message.edit_text(text, **kwargs)
    except TelegramForbiddenError:
        user = callback.from_user
        if user:
            logger.warning(f"User {user.id} has blocked the bot")
        return None


MAIN_MENU_TEXT = (
    "Приветствую, боец. Ты уже участник DAO, осталась маленькая формальность в виде оплаты\n\n"
    "Выбирай подписку и получай доступ ко всем ресурсам\n"
    "Личный кабинет даст информацию о подписке\n"
    "А на любой вопрос я готов ответить в личке\n\n"
    "Ждем тебя среди сильнейших 💪🏻\n\n"
    "Выбирай нужное действие в меню ниже 👇🏻"
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


# ── Хендлеры ────────────────────────────────────────────────


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_referral(message: Message, state: FSMContext) -> None:
    """Обработка старта с реферальным кодом."""
    user = message.from_user  # type: ignore

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

    await safe_edit_message(
        callback,
        "Выберите одну из предложенных подписок:",
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

    # Активируем подписку
    activate_subscription(user_id, plan_id)

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

    # Получаем личную реферальную ссылку (только для пользователей с активной подпиской)
    referral_text = ""
    if subscription:
        own_referral_code = get_user_own_referral_code(user_id)
        if own_referral_code:
            bot_info = await callback.bot.get_me() # type: ignore
            bot_username = bot_info.username if bot_info and bot_info.username else "PaymentDAOBot"
            referral_url = f"https://t.me/{bot_username}?start={own_referral_code}"
            referral_text = f"\n\n🔗 Ваша реферальная ссылка:\n<code>{referral_url}</code>\n\nПриглашайте друзей и получайте +7 дней к подписке за каждую оплату!"

    text = (
        f"Личный кабинет\n\n"
        f"ID: {user_id}\n"
        f"{status_text}"
        f"{referral_text}"
    )

    logger.info(f"User {user_id} opened profile")
    await safe_edit_message(callback, text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()




# ── Настройка команд бота ───────────────────────────────────


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Перезапустить бота"),
    ])
    logger.info("Bot commands configured")
