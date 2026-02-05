import asyncio
import os

from dotenv import load_dotenv

# ВАЖНО: загружаем .env до импорта модулей, которые используют переменные окружения
load_dotenv()

from aiogram import Bot, Dispatcher

from src.logger import logger
from src.payments import init_db, deactivate_expired_subscriptions, SUBSCRIPTION_PLANS
from src.telegram_ui import router, setup_bot_commands

CHECK_INTERVAL = 5 * 60  # 5 минут


async def check_expired_subscriptions(bot: Bot) -> None:
    """Фоновая задача: раз в 5 минут проверяет и деактивирует просроченные подписки."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            expired = deactivate_expired_subscriptions()
            for sub in expired:
                plan_label = SUBSCRIPTION_PLANS.get(sub["plan"], {}).get("label", sub["plan"])
                try:
                    await bot.send_message(
                        sub["user_id"],
                        f"Ваша подписка \"{plan_label}\" истекла.\n"
                        f"Чтобы продлить, нажмите /start и выберите \"Оплатить подписку\".",
                    )
                    logger.info(f"Sent expiration notice to user {sub['user_id']}")
                except Exception as e:
                    logger.warning(f"Failed to notify user {sub['user_id']}: {e}")
        except Exception as e:
            logger.error(f"Error in expired subscriptions check: {e}")


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

    asyncio.create_task(check_expired_subscriptions(bot))

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
