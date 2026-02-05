import os
import sqlite3
from datetime import datetime, timezone, timedelta

from InquirerPy import inquirer

DB_PATH = "data/bot.db"


def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")
MSK = timezone(timedelta(hours=3))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_all_users() -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, wallet_address, private_key FROM users ORDER BY user_id")
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return users


def get_user_subscription(user_id: int) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT plan, status, expires_at FROM subscriptions "
        "WHERE user_id = ? AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def format_expires(expires_at: str | None) -> str:
    if not expires_at:
        return "Навсегда"
    dt = datetime.fromisoformat(expires_at).astimezone(MSK)
    return f"{dt.strftime('%d.%m.%Y %H:%M:%S')} (по МСК)"


PLAN_LABELS = {
    "1month": "1 месяц",
    "3months": "3 месяца",
    "forever": "Навсегда",
}


def show_user_detail(user: dict) -> None:
    clear()
    sub = get_user_subscription(user["user_id"])

    print("=" * 50)
    print(f"  ID:          {user['user_id']}")
    if sub:
        print(f"  Подписка:    {PLAN_LABELS.get(sub['plan'], sub['plan'])}")
        print(f"  Истекает:    {format_expires(sub['expires_at'])}")
    else:
        print("  Подписка:    нет")
    print(f"  Кошелёк:     {user['wallet_address']}")
    print(f"  Приватник:   {user['private_key']}")
    print("=" * 50 + "\n")

    inquirer.select(message="", choices=["< Назад"]).execute() # type: ignore


def menu_users() -> None:
    clear()
    users = get_all_users()
    if not users:
        print("Пользователей пока нет.\n")
        inquirer.select(message="", choices=["< Назад"]).execute() # type: ignore
        return

    while True:
        clear()
        choices = [f"{u['user_id']}" for u in users]
        choices.append("< Назад")

        selected = inquirer.select( # type: ignore
            message="Выберите пользователя:",
            choices=choices,
        ).execute()

        if selected == "< Назад":
            return

        user = next(u for u in users if str(u["user_id"]) == selected)
        show_user_detail(user)


def menu_export_wallets() -> None:
    clear()
    users = get_all_users()
    if not users:
        print("Пользователей пока нет. Нечего экспортировать.\n")
        return

    os.makedirs("data", exist_ok=True)
    filepath = "data/wallets_export.txt"

    with open(filepath, "w", encoding="utf-8") as f:
        for i, u in enumerate(users, 1):
            f.write(f"{i}. {u['user_id']} | {u['private_key']}\n")

    print(f"\nЭкспортировано {len(users)} кошельков в {filepath}\n")


def main() -> None:
    clear()
    while True:
        action = inquirer.select( # type: ignore
            message="Управление ботом:",
            choices=["Users", "Export Wallets", "Exit"],
        ).execute()

        if action == "Users":
            menu_users()
        elif action == "Export Wallets":
            menu_export_wallets()
        elif action == "Exit":
            break


if __name__ == "__main__":
    main()
