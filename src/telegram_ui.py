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
from aiogram.filters import CommandStart

from src.logger import logger
from src.blockchain import get_current_block
from src.payments import (
    SUBSCRIPTION_PLANS,
    SUPPORTED_NETWORKS,
    SUPPORTED_TOKENS,
    cancel_user_payment_sessions,
    check_payment,
    create_payment_session,
    get_wallet_for_network,
    get_user_subscription,
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
        [InlineKeyboardButton(text="Проверить оплату", callback_data=f"check:{plan_id}:{network}:{token}")],
        [InlineKeyboardButton(text="< Назад", callback_data=f"net:{plan_id}:{network}")],
    ])


def back_kb(callback_data: str = "back_to_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="< Назад", callback_data=callback_data)],
    ])


# ── Хендлеры ────────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    logger.info(f"User {message.from_user.id} started the bot") # type: ignore
    cancel_user_payment_sessions(message.from_user.id)  # type: ignore
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_kb())


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
async def show_payment(callback: CallbackQuery) -> None:
    _, plan_id, network, token = callback.data.split(":")  # type: ignore
    plan = SUBSCRIPTION_PLANS.get(plan_id)
    net = SUPPORTED_NETWORKS.get(network)
    tok = SUPPORTED_TOKENS.get(token)
    if not plan or not net or not tok:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return

    user_id = callback.from_user.id
    wallet_address = get_wallet_for_network(user_id, network)

    # Фиксируем текущий блок для сканирования платежей
    try:
        current_block = await get_current_block(network)
    except Exception as e:
        logger.error(f"Failed to get current block for {network}: {e}")
        await callback.answer("Ошибка сети. Попробуйте позже.", show_alert=True)
        return

    create_payment_session(user_id, plan_id, network, token, current_block)

    text = (
        f"Оплата подписки: {plan['label']}\n\n"
        f"Переведите {plan['price']}$ {tok['name']} в сети {net['name']} "
        f"на адрес ниже для оплаты подписки на срок \"{plan['label']}\".\n\n"
        f"Адрес для оплаты:\n`{wallet_address}`\n\n"
        f"После перевода нажмите \"Проверить оплату\"."
    )

    logger.info(f"User {user_id} selected {tok['name']} on {net['name']}, wallet: {wallet_address}")
    await callback.message.edit_text(  # type: ignore
        text,
        reply_markup=payment_kb(plan_id, network, token),
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("check:"))
async def check_payment_handler(callback: CallbackQuery) -> None:
    _, plan_id, network, token = callback.data.split(":")  # type: ignore
    user_id = callback.from_user.id

    logger.info(f"User {user_id} checking payment for plan {plan_id}, {network}/{token}")

    result = await check_payment(user_id, plan_id, network, token)
    if result:
        plan = SUBSCRIPTION_PLANS.get(plan_id, {})
        tok = SUPPORTED_TOKENS.get(token, {})
        net = SUPPORTED_NETWORKS.get(network, {})
        text = (
            f"Оплата подтверждена!\n\n"
            f"Подписка: {plan.get('label', plan_id)}\n"
            f"Сумма: {result['amount']} {tok.get('name', token)}\n"
            f"Сеть: {net.get('name', network)}\n"
            f"Tx: `{result['tx_hash']}`"
        )
        await callback.message.edit_text(  # type: ignore
            text,
            reply_markup=back_kb(),
            parse_mode=ParseMode.MARKDOWN,
        )
        await callback.answer("Оплата подтверждена!", show_alert=True)
    else:
        await callback.answer("Оплата пока не найдена. Попробуйте позже.", show_alert=True)


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
    ])
    logger.info("Bot commands configured")
