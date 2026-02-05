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
    """Получает всех пользователей из user_profiles."""
    conn = get_connection()
    cursor = conn.cursor()

    # Берем всех пользователей из user_profiles
    cursor.execute("""
        SELECT user_id, username, first_name, last_name
        FROM user_profiles
        ORDER BY user_id
    """)
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


def format_user_display(user: dict) -> str:
    """Форматирует отображение пользователя: ID | @username или ID | FirstName."""
    user_id = user["user_id"]
    username = user.get("username")
    first_name = user.get("first_name")

    if username:
        return f"{user_id} | @{username}"
    elif first_name:
        return f"{user_id} | {first_name}"
    else:
        return str(user_id)


def change_plan(user_id: int) -> None:
    plan_choices = [f"{pid} ({label})" for pid, label in PLAN_LABELS.items()]
    plan_choices.append("< Отмена")

    selected = inquirer.select(  # type: ignore
        message="Выберите новый план:",
        choices=plan_choices,
    ).execute()

    if selected == "< Отмена":
        return

    new_plan = selected.split(" (")[0]

    conn = get_connection()
    cursor = conn.cursor()

    if new_plan == "forever":
        expires_at = None
    else:
        days = {"1month": 30, "3months": 90}[new_plan]
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    cursor.execute(
        "INSERT INTO subscriptions (user_id, plan, status, expires_at) VALUES (?, ?, 'active', ?)",
        (user_id, new_plan, expires_at),
    )

    conn.commit()
    conn.close()
    print(f"\nПлан изменён на {PLAN_LABELS[new_plan]}.\n")


def add_days(user_id: int) -> None:
    sub = get_user_subscription(user_id)
    if not sub:
        print("\nУ пользователя нет активной подписки.\n")
        return
    if not sub["expires_at"]:
        print("\nПодписка бессрочная, добавление дней не требуется.\n")
        return

    days_str = inquirer.text(message="Количество дней:").execute()  # type: ignore
    try:
        days = int(days_str)
    except ValueError:
        print("\nНекорректное число.\n")
        return

    current_expires = datetime.fromisoformat(sub["expires_at"])
    new_expires = current_expires + timedelta(days=days)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE subscriptions SET expires_at = ? WHERE user_id = ? AND status = 'active'",
        (new_expires.isoformat(), user_id),
    )
    conn.commit()
    conn.close()
    print(f"\nДобавлено {days} дн. Новый срок: {format_expires(new_expires.isoformat())}\n")


def cancel_subscription(user_id: int) -> None:
    sub = get_user_subscription(user_id)
    if not sub:
        print("\nУ пользователя нет активной подписки.\n")
        return

    confirm = inquirer.select(  # type: ignore
        message=f"Отменить подписку \"{PLAN_LABELS.get(sub['plan'], sub['plan'])}\"?",
        choices=["Да", "Нет"],
    ).execute()

    if confirm != "Да":
        return

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    print("\nПодписка отменена.\n")


def show_user_detail(user: dict) -> None:
    import sys
    sys.path.insert(0, ".")
    from src.payments import get_user_referral_info, get_user_own_referral_code

    while True:
        clear()
        sub = get_user_subscription(user["user_id"])
        ref_info = get_user_referral_info(user["user_id"])

        print("=" * 50)
        print(f"  ID:          {user['user_id']}")
        if user.get("username"):
            print(f"  Username:    @{user['username']}")
        if user.get("first_name"):
            name = user['first_name']
            if user.get("last_name"):
                name += f" {user['last_name']}"
            print(f"  Имя:         {name}")
        if sub:
            print(f"  Подписка:    {PLAN_LABELS.get(sub['plan'], sub['plan'])}")
            print(f"  Истекает:    {format_expires(sub['expires_at'])}")
        else:
            print("  Подписка:    нет")

        # Показываем реферальную информацию (по какой ссылке пришёл)
        if ref_info:
            print(f"  Пришёл по:   {ref_info['code']}")
            if ref_info['name']:
                print(f"  Название:    {ref_info['name']}")
            else:
                print(f"  Название:    без названия")
        else:
            print("  Пришёл по:   нет")

        # Показываем личный реф. код пользователя
        own_code = get_user_own_referral_code(user["user_id"])
        if own_code:
            print(f"  Личный код:  {own_code}")
        else:
            print("  Личный код:  не создан")

        print("=" * 50 + "\n")

        action = inquirer.select(  # type: ignore
            message="Действие:",
            choices=["Изменить план", "Добавить дни", "Отменить подписку", "< Назад"],
        ).execute()

        if action == "< Назад":
            return
        elif action == "Изменить план":
            change_plan(user["user_id"])
        elif action == "Добавить дни":
            add_days(user["user_id"])
        elif action == "Отменить подписку":
            cancel_subscription(user["user_id"])


def menu_users() -> None:
    clear()
    users = get_all_users()
    if not users:
        print("Пользователей пока нет.\n")
        inquirer.select(message="", choices=["< Назад"]).execute() # type: ignore
        return

    while True:
        clear()
        choices = [format_user_display(u) for u in users]
        choices.append("< Назад")

        selected = inquirer.select( # type: ignore
            message="Выберите пользователя:",
            choices=choices,
        ).execute()

        if selected == "< Назад":
            return

        # Извлекаем user_id из выбранной строки (первое число до " |")
        user_id_str = selected.split(" |")[0] if " |" in selected else selected
        user = next(u for u in users if str(u["user_id"]) == user_id_str)
        show_user_detail(user)


def menu_view_master_wallets() -> None:
    """Просмотр мастер-кошельков без экспорта."""
    clear()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT network, wallet_address FROM master_wallets ORDER BY network"
    )
    wallets = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if not wallets:
        print("Мастер-кошельки не найдены.\n")
        inquirer.select(message="", choices=["< Назад"]).execute()  # type: ignore
        return

    print("=" * 80)
    print("МАСТЕР-КОШЕЛЬКИ (адреса)")
    print("=" * 80)
    for w in wallets:
        print(f"{w['network']:12} | {w['wallet_address']}")
    print("=" * 80 + "\n")

    inquirer.select(message="", choices=["< Назад"]).execute()  # type: ignore


def menu_export_wallets() -> None:
    """Экспортирует только мастер-кошельки."""
    clear()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT network, wallet_address, private_key FROM master_wallets ORDER BY network"
    )
    wallets = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if not wallets:
        print("Мастер-кошельки не найдены.\n")
        inquirer.select(message="", choices=["< Назад"]).execute()  # type: ignore
        return

    os.makedirs("data", exist_ok=True)
    filepath = "data/master_wallets_export.txt"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("=== MASTER WALLETS ===\n")
        f.write("ВНИМАНИЕ: Храните этот файл в безопасном месте!\n\n")
        for w in wallets:
            f.write(f"Сеть: {w['network']}\n")
            f.write(f"Адрес: {w['wallet_address']}\n")
            f.write(f"Приватный ключ: {w['private_key']}\n")
            f.write("-" * 80 + "\n")

    print(f"\nЭкспортировано {len(wallets)} мастер-кошельков в {filepath}\n")
    print("⚠️  ВАЖНО: Этот файл содержит приватные ключи. Удалите его после сохранения в безопасное место!")
    inquirer.select(message="", choices=["< Назад"]).execute()  # type: ignore


def menu_new_referral() -> None:
    """Создание новой реферальной ссылки."""
    import sys
    sys.path.insert(0, ".")
    from src.payments import create_referral_link

    clear()
    print("=" * 80)
    print("СОЗДАНИЕ РЕФЕРАЛЬНОЙ ССЫЛКИ")
    print("=" * 80 + "\n")

    # 1. Лимит пользователей
    max_uses_str = inquirer.text(  # type: ignore
        message="Лимит использований (оставьте пустым для безлимита):",
        default=""
    ).execute()

    max_uses = None
    if max_uses_str.strip():
        try:
            max_uses = int(max_uses_str)
        except ValueError:
            print("\n❌ Некорректное число. Отмена.\n")
            inquirer.select(message="", choices=["< Назад"]).execute()  # type: ignore
            return

    # 2. Кастомные цены
    custom_prices_str = inquirer.text(  # type: ignore
        message="Кастомные цены через запятую (например: 35.5,90,200.99) или пустое для дефолтных:",
        default=""
    ).execute()

    custom_prices = None
    if custom_prices_str.strip():
        try:
            # Проверяем формат (поддержка float)
            prices = [float(p.strip()) for p in custom_prices_str.split(",")]
            if len(prices) != 3:
                print("\n❌ Нужно указать ровно 3 цены (для 1мес, 3мес, навсегда). Отмена.\n")
                inquirer.select(message="", choices=["< Назад"]).execute()  # type: ignore
                return
            custom_prices = custom_prices_str.strip()
        except ValueError:
            print("\n❌ Некорректный формат цен. Отмена.\n")
            inquirer.select(message="", choices=["< Назад"]).execute()  # type: ignore
            return

    # 3. Название ссылки
    name = inquirer.text(  # type: ignore
        message="Название ссылки (для идентификации, например 'VK реклама'):",
        default=""
    ).execute()

    name = name.strip() if name.strip() else None

    # 4. Подтверждение
    clear()
    print("=" * 80)
    print("ПОДТВЕРЖДЕНИЕ")
    print("=" * 80)
    print(f"  Название:            {name if name else 'без названия'}")
    print(f"  Лимит использований: {max_uses if max_uses else 'безлимит'}")
    print(f"  Кастомные цены:      {custom_prices if custom_prices else 'дефолтные'}")
    print("=" * 80 + "\n")

    confirm = inquirer.select(  # type: ignore
        message="Создать ссылку?",
        choices=["Да", "Нет"]
    ).execute()

    if confirm != "Да":
        return

    # 5. Создание и сохранение
    code = create_referral_link(max_uses, custom_prices, name)

    # Формируем ссылку
    referral_url = f"https://t.me/DAOPayment_Bot?start={code}"

    # Сохраняем в файл
    os.makedirs("data", exist_ok=True)
    filepath = f"data/referral_{code}.txt"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"Реферальная ссылка\n")
        f.write("=" * 80 + "\n\n")
        if name:
            f.write(f"Название: {name}\n")
        f.write(f"Код: {code}\n")
        f.write(f"Ссылка: {referral_url}\n\n")
        f.write(f"Лимит использований: {max_uses if max_uses else 'безлимит'}\n")
        f.write(f"Кастомные цены: {custom_prices if custom_prices else 'дефолтные'}\n")

    # Показываем результат
    clear()
    print("=" * 80)
    print("OK REFERALNAYA SSYLKA SOZDANA")
    print("=" * 80)
    if name:
        print(f"\nNazvanie: {name}")
    print(f"Kod: {code}")
    print(f"Ssylka: {referral_url}\n")
    print(f"Sohraneno v: {filepath}\n")
    print("=" * 80 + "\n")

    inquirer.select(message="", choices=["< Nazad"]).execute()  # type: ignore


def main() -> None:
    clear()
    while True:
        action = inquirer.select( # type: ignore
            message="Управление ботом:",
            choices=["Users", "View Master Wallets", "Export Master Wallets", "New Referral", "Exit"],
        ).execute()

        if action == "Users":
            menu_users()
        elif action == "View Master Wallets":
            menu_view_master_wallets()
        elif action == "Export Master Wallets":
            menu_export_wallets()
        elif action == "New Referral":
            menu_new_referral()
        elif action == "Exit":
            break


if __name__ == "__main__":
    main()
