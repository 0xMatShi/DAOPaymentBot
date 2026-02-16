import asyncio
import os

from dotenv import load_dotenv

# ВАЖНО: загружаем .env до импорта модулей, которые используют переменные окружения
load_dotenv()

from aiogram import Bot, Dispatcher

from src.logger import logger
from src.payments import (
    init_db,
    deactivate_expired_subscriptions,
    get_subscriptions_requiring_notification,
    mark_notification_sent,
    get_users_to_kick,
    kick_and_unban_user,
    SUBSCRIPTION_PLANS,
)
from src.telegram_ui import router, setup_bot_commands

CHECK_INTERVAL = 5 * 60  # 5 минут


async def check_subscription_notifications(bot: Bot) -> None:
    """Фоновая задача: проверяет подписки и отправляет уведомления."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            # 1. Проверяем уведомления об истечении
            notifications = get_subscriptions_requiring_notification()
            for notif in notifications:
                user_id = notif["user_id"]
                sub_id = notif["subscription_id"]
                notif_type = notif["notification_type"]
                expires_at = notif["expires_at"]

                # Формируем текст уведомления
                from datetime import datetime, timezone, timedelta
                expires_dt = datetime.fromisoformat(expires_at).astimezone(timezone(timedelta(hours=3)))
                expires_str = expires_dt.strftime('%d.%m.%Y %H:%M')

                if notif_type == "3days":
                    message = (
                        f"⏰ Ваша подписка истекает через 3 дня!\n\n"
                        f"Дата окончания: {expires_str} (МСК)\n\n"
                        f"Продлите подписку, чтобы не потерять доступ.\n"
                        f"Нажмите /start → \"Оплатить подписку\""
                    )
                elif notif_type == "1day":
                    message = (
                        f"⏰ Ваша подписка истекает завтра!\n\n"
                        f"Дата окончания: {expires_str} (МСК)\n\n"
                        f"Продлите подписку, чтобы не потерять доступ.\n"
                        f"Нажмите /start → \"Оплатить подписку\""
                    )
                elif notif_type == "1hour":
                    message = (
                        f"⚠️ Ваша подписка истекает через час!\n\n"
                        f"Дата окончания: {expires_str} (МСК)\n\n"
                        f"Продлите подписку прямо сейчас!\n"
                        f"Нажмите /start → \"Оплатить подписку\""
                    )
                elif notif_type == "expired":
                    message = (
                        f"❌ Ваша подписка истекла!\n\n"
                        f"У вас есть 1 час для продления.\n"
                        f"Если не продлите, доступ будет закрыт.\n\n"
                        f"Нажмите /start → \"Оплатить подписку\""
                    )
                else:
                    continue

                try:
                    await bot.send_message(user_id, message)
                    mark_notification_sent(user_id, sub_id, notif_type)
                    logger.info(f"Sent {notif_type} notification to user {user_id}")
                except Exception as e:
                    logger.warning(f"Failed to send {notif_type} notification to user {user_id}: {e}")

            # 2. Кикаем пользователей с истекшим grace period
            users_to_kick = get_users_to_kick()
            for user_info in users_to_kick:
                user_id = user_info["user_id"]
                sub_id = user_info["subscription_id"]
                try:
                    results = await kick_and_unban_user(bot, user_id)
                    logger.info(f"Kicked user {user_id} from chats: {results}")

                    # Деактивируем подписку
                    deactivate_expired_subscriptions()

                    # Отмечаем, что пользователь был кикнут (чтобы не кикать повторно)
                    mark_notification_sent(user_id, sub_id, "kicked")

                    # Отправляем финальное уведомление
                    await bot.send_message(
                        user_id,
                        "❌ Ваша подписка истекла. Вы были удалены из ДАО.\n\n"
                        "Чтобы вернуться, оформите новую подписку:\n"
                        "/start → \"Оплатить подписку\""
                    )
                except Exception as e:
                    logger.error(f"Failed to kick user {user_id}: {e}")

        except Exception as e:
            logger.error(f"Error in subscription notifications check: {e}")


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in .env")
        return

    chat_id = os.getenv("PRIVATE_CHAT_ID")
    group_id = os.getenv("PRIVATE_GROUP_ID")
    logger.info(f"Loaded PRIVATE_CHAT_ID from .env: {chat_id}")
    logger.info(f"Loaded PRIVATE_GROUP_ID from .env: {group_id}")

    init_db()

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    await setup_bot_commands(bot)

    asyncio.create_task(check_subscription_notifications(bot))

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
