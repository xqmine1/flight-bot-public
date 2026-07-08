import os
import html
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

from config import (
    CURRENCY,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    SEARCH_DAYS_AHEAD,
    DEDUP_KEEP_DAYS,
    SENT_DB_FILENAME,
    BEACH_DESTINATION_LIMITS,
    HUB_DESTINATION_LIMITS,
    DIRECT_ONLY_DESTINATIONS,
    BEACH_ORIGINS,
    HUB_ORIGINS,
    ORIGIN_NAMES,
    DESTINATION_NAMES,
    AIRPORT_CITY_NAMES,
)

print("RUNNING GITHUB ACTIONS FLIGHT BOT")

# ====== ТОКЕНЫ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ======

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")

if not TELEGRAM_TOKEN:
    raise ValueError("Не найден TELEGRAM_TOKEN в GitHub Secrets")

if not TELEGRAM_CHAT_ID:
    raise ValueError("Не найден TELEGRAM_CHAT_ID в GitHub Secrets")

if not TRAVELPAYOUTS_TOKEN:
    raise ValueError("Не найден TRAVELPAYOUTS_TOKEN в GitHub Secrets")


# ====== РЕЖИМ ПОИСКА ======

# Режимы:
# beach — пляжные направления
# hubs — хабы
SEARCH_MODE = (os.getenv("SEARCH_MODE") or "beach").lower().strip()

# Антидубль включён для обоих режимов.
DEDUP_ENABLED = SEARCH_MODE in ["beach", "hubs"]

SENT_DB_PATH = Path(SENT_DB_FILENAME)


# ====== ПЕРИОД ПОИСКА ======

SEARCH_START_DATE = datetime.now(timezone.utc).date()
SEARCH_END_DATE = SEARCH_START_DATE + timedelta(days=SEARCH_DAYS_AHEAD)


def generate_search_months(start_date: date, end_date: date):
    months = []

    year = start_date.year
    month = start_date.month

    while (year, month) <= (end_date.year, end_date.month):
        months.append(f"{year:04d}-{month:02d}")

        month += 1

        if month > 12:
            month = 1
            year += 1

    return months


MONTHS = generate_search_months(SEARCH_START_DATE, SEARCH_END_DATE)


# ====== РЕЖИМЫ И МАРШРУТЫ ======

def get_active_origins():
    if SEARCH_MODE == "beach":
        return BEACH_ORIGINS

    if SEARCH_MODE == "hubs":
        return HUB_ORIGINS

    raise ValueError(f"Неизвестный режим поиска: {SEARCH_MODE}")


def get_active_destination_limits():
    if SEARCH_MODE == "beach":
        return BEACH_DESTINATION_LIMITS

    if SEARCH_MODE == "hubs":
        return HUB_DESTINATION_LIMITS

    raise ValueError(f"Неизвестный режим поиска: {SEARCH_MODE}")


def get_mode_title():
    if SEARCH_MODE == "beach":
        return "🏖 <b>Дешёвые пляжные направления найдены</b>"

    if SEARCH_MODE == "hubs":
        return "🧭 <b>Дешёвые хабы найдены</b>"

    return "✈️ <b>Дешёвые билеты найдены</b>"


def build_routes():
    routes = []
    active_origins = get_active_origins()
    destination_limits = get_active_destination_limits()

    for origin in active_origins:
        for destination, price_limit in destination_limits.items():
            for month in MONTHS:
                direct_only = (
                    SEARCH_MODE == "hubs"
                    and destination in DIRECT_ONLY_DESTINATIONS
                )

                routes.append(
                    {
                        "origin": origin,
                        "destination": destination,
                        "month": month,
                        "price_limit": price_limit,
                        "direct_only": direct_only,
                    }
                )

    return routes


# ====== TELEGRAM ======

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Ошибка отправки в Telegram: {data}")


def send_long_telegram_message(text: str):
    if len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        send_telegram_message(text)
        return

    parts = []
    current_part = ""

    for line in text.split("\n"):
        if len(current_part) + len(line) + 1 > MAX_TELEGRAM_MESSAGE_LENGTH:
            if current_part:
                parts.append(current_part)
            current_part = line
        else:
            current_part += "\n" + line if current_part else line

    if current_part:
        parts.append(current_part)

    total_parts = len(parts)

    for index, part in enumerate(parts, start=1):
        send_telegram_message(f"{part}\n\nЧасть {index}/{total_parts}")


def send_error_message(error: Exception):
    error_type = html.escape(type(error).__name__)
    error_text = html.escape(str(error))[:1500]

    message = (
        "⚠️ <b>Ошибка Flight Bot</b>\n\n"
        f"<b>Режим:</b> {html.escape(SEARCH_MODE)}\n"
        f"<b>Тип:</b> {error_type}\n"
        f"<b>Описание:</b>\n<code>{error_text}</code>"
    )

    send_telegram_message(message)


# ====== TRAVELPAYOUTS / AVIASALES ======

def get_flight_prices(origin: str, destination: str, month: str, direct_only: bool):
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": month,
        "one_way": "true",
        "direct": "true" if direct_only else "false",
        "sorting": "price",
        "currency": CURRENCY,
        "limit": 10,
        "page": 1,
        "market": "ru",
        "token": TRAVELPAYOUTS_TOKEN,
    }

    direct_text = "direct only" if direct_only else "with transfers"

    for attempt in range(1, 4):
        try:
            response = requests.get(url, params=params, timeout=30)

            print(
                f"{origin} → {destination}, {month}: "
                f"status {response.status_code}, {direct_text}, attempt {attempt}"
            )

            if response.status_code != 200:
                print(
                    f"Ошибка API по маршруту {origin} → {destination}, {month}: "
                    f"{response.text[:300]}"
                )
                return []

            data = response.json()

            if not data.get("success"):
                print(
                    f"Travelpayouts вернул ошибку по маршруту "
                    f"{origin} → {destination}, {month}: {data}"
                )
                return []

            return data.get("data", [])

        except requests.exceptions.RequestException as error:
            print(
                f"Сетевая ошибка по маршруту {origin} → {destination}, {month}. "
                f"Попытка {attempt}/3: {error}"
            )

            if attempt < 3:
                time.sleep(5)

    print(
        f"{origin} → {destination}, {month}: "
        f"маршрут пропущен после 3 неудачных попыток"
    )

    return []


# ====== ФОРМАТИРОВАНИЕ ======

def format_date(value):
    if not value:
        return "не указано"

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


def format_date_only(value):
    if not value:
        return "не указано"

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).strftime("%d.%m.%Y")
    except Exception:
        return str(value)


def get_ticket_departure_date(ticket: dict):
    value = ticket.get("departure_at")

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).date()
    except Exception:
        return None


def is_ticket_inside_search_window(ticket: dict) -> bool:
    departure_date = get_ticket_departure_date(ticket)

    if not departure_date:
        return False

    return SEARCH_START_DATE <= departure_date <= SEARCH_END_DATE


def format_money(value):
    try:
        return f"{int(value):,}".replace(",", " ")
    except Exception:
        return str(value)


def get_city_name(code: str, mapping: dict) -> str:
    return mapping.get(code, code)


def build_aviasales_link(ticket, origin: str, destination: str):
    link = ticket.get("link")

    if link:
        if link.startswith("http"):
            return link
        return "https://www.aviasales.ru" + link

    return f"https://www.aviasales.ru/search/{origin}{destination}1"


def make_compact_link(link: str) -> str:
    if not link:
        return ""

    return link.split("?")[0]


# ====== АНТИДУБЛЬ ======

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_sent_db():
    if not SENT_DB_PATH.exists():
        return {
            "version": 1,
            "items": {}
        }

    try:
        with SENT_DB_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {
                "version": 1,
                "items": {}
            }

        if "items" not in data or not isinstance(data["items"], dict):
            data["items"] = {}

        return data

    except Exception:
        return {
            "version": 1,
            "items": {}
        }


def save_sent_db(sent_db: dict):
    with SENT_DB_PATH.open("w", encoding="utf-8") as file:
        json.dump(sent_db, file, ensure_ascii=False, indent=2)


def cleanup_sent_db(sent_db: dict):
    items = sent_db.get("items", {})
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_KEEP_DAYS)

    keys_to_delete = []

    for key, value in items.items():
        first_seen = value.get("first_seen")

        if not first_seen:
            continue

        try:
            first_seen_dt = datetime.fromisoformat(first_seen)

            if first_seen_dt < cutoff:
                keys_to_delete.append(key)

        except Exception:
            continue

    for key in keys_to_delete:
        del items[key]


def make_ticket_stable_key(route: dict, ticket: dict):
    """
    Стабильный ключ билета для антидубля.

    Цена НЕ включается в ключ.
    Поэтому если тот же билет станет дешевле, бот пришлёт его снова.
    """
    origin = route["origin"]
    destination = route["destination"]
    direct_only = route["direct_only"]

    departure_at = str(ticket.get("departure_at") or "")
    transfers = str(ticket.get("transfers") or "")
    flight_number = str(ticket.get("flight_number") or "")

    link = build_aviasales_link(ticket, origin, destination)
    compact_link = make_compact_link(link)

    key_parts = [
        SEARCH_MODE,
        origin,
        destination,
        departure_at,
        transfers,
        flight_number,
        "direct" if direct_only else "any",
        compact_link,
    ]

    return "|".join(key_parts)


def should_skip_ticket(sent_db: dict, ticket_key: str, ticket: dict):
    """
    True — билет уже был отправлен и не стал дешевле.
    False — билет новый или стал дешевле.
    """
    record = sent_db.get("items", {}).get(ticket_key)

    if not record:
        return False

    current_price = ticket.get("price")
    previous_best_price = record.get("best_price")

    try:
        current_price = int(current_price)
        previous_best_price = int(previous_best_price)
    except Exception:
        return True

    if current_price < previous_best_price:
        return False

    return True


def mark_tickets_as_sent(sent_db: dict, sent_records: list):
    items = sent_db.setdefault("items", {})

    for record in sent_records:
        key = record["key"]
        route = record["route"]
        ticket = record["ticket"]

        current_price = ticket.get("price")
        existing = items.get(key)

        if existing:
            first_seen = existing.get("first_seen", now_iso())

            try:
                previous_best_price = int(existing.get("best_price", current_price))
                current_price_int = int(current_price)
                best_price = min(previous_best_price, current_price_int)
            except Exception:
                best_price = current_price
        else:
            first_seen = now_iso()
            best_price = current_price

        items[key] = {
            "first_seen": first_seen,
            "last_sent": now_iso(),
            "mode": SEARCH_MODE,
            "origin": route["origin"],
            "destination": route["destination"],
            "month": route["month"],
            "best_price": best_price,
            "last_price": current_price,
            "departure_at": ticket.get("departure_at"),
            "transfers": ticket.get("transfers"),
            "flight_number": ticket.get("flight_number"),
            "direct_only": route.get("direct_only", False),
        }


# ====== ПЕРЕСАДКИ И ФИЛЬТРЫ ======

def airport_code_to_city(code: str) -> str:
    if not code:
        return ""

    code = str(code).upper().strip()
    return AIRPORT_CITY_NAMES.get(code, code)


def unique_keep_order(items):
    result = []

    for item in items:
        if item and item not in result:
            result.append(item)

    return result


def extract_layover_codes_from_ticket(ticket: dict, origin: str, destination: str):
    possible_keys = [
        "transfer_airports",
        "transfers_airports",
        "layover_airports",
        "stopover_airports",
        "stops",
        "stopovers",
    ]

    found_codes = []

    for key in possible_keys:
        value = ticket.get(key)

        if not value:
            continue

        if isinstance(value, str):
            for part in value.replace(";", ",").split(","):
                code = part.strip().upper()
                if len(code) == 3:
                    found_codes.append(code)

        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    code = item.strip().upper()
                    if len(code) == 3:
                        found_codes.append(code)

                elif isinstance(item, dict):
                    for inner_key in ["airport", "airport_code", "iata", "code"]:
                        code = item.get(inner_key)
                        if code and len(str(code).strip()) == 3:
                            found_codes.append(str(code).strip().upper())

    segment_keys = ["segments", "segment", "route", "routes", "itinerary"]

    for key in segment_keys:
        segments = ticket.get(key)

        if not segments or not isinstance(segments, list):
            continue

        route_codes = []

        for segment in segments:
            if not isinstance(segment, dict):
                continue

            for airport_key in [
                "origin",
                "destination",
                "origin_airport",
                "destination_airport",
                "departure_airport",
                "arrival_airport",
                "from",
                "to",
            ]:
                code = segment.get(airport_key)

                if isinstance(code, str) and len(code.strip()) == 3:
                    route_codes.append(code.strip().upper())

        route_codes = unique_keep_order(route_codes)

        for code in route_codes:
            if code not in [origin, destination]:
                found_codes.append(code)

    found_codes = unique_keep_order(found_codes)

    found_codes = [
        code for code in found_codes
        if code not in [origin.upper(), destination.upper()]
    ]

    return found_codes


def get_transfer_word(count: int) -> str:
    if count == 1:
        return "пересадка"

    if 2 <= count <= 4:
        return "пересадки"

    return "пересадок"


def format_transfers(ticket: dict, origin: str, destination: str):
    transfers = ticket.get("transfers")

    try:
        transfers_count = int(transfers)
    except Exception:
        transfers_count = None

    if transfers_count == 0:
        return "без пересадок"

    layover_codes = extract_layover_codes_from_ticket(ticket, origin, destination)
    layover_cities = [airport_code_to_city(code) for code in layover_codes]
    layover_cities = unique_keep_order(layover_cities)

    if transfers_count == 1:
        if layover_cities:
            return f"1 пересадка в {layover_cities[0]}"
        return "1 пересадка"

    if transfers_count and transfers_count >= 2:
        transfer_word = get_transfer_word(transfers_count)

        if layover_cities:
            return f"{transfers_count} {transfer_word}: {', '.join(layover_cities)}"

        return f"{transfers_count} {transfer_word}"

    if layover_cities:
        if len(layover_cities) == 1:
            return f"пересадка в {layover_cities[0]}"

        return f"пересадки: {', '.join(layover_cities)}"

    return "пересадки не указаны"


def is_direct_ticket(ticket: dict) -> bool:
    transfers = ticket.get("transfers")

    try:
        return int(transfers) == 0
    except Exception:
        return False


# ====== СООБЩЕНИЕ ======

def build_alert_item(route: dict, ticket: dict):
    origin = route["origin"]
    destination = route["destination"]

    price = ticket.get("price")
    flight_number = ticket.get("flight_number", "не указано")
    departure_raw = ticket.get("departure_at")

    departure_date = format_date_only(departure_raw)

    link = build_aviasales_link(ticket, origin, destination)
    compact_link = make_compact_link(link)
    safe_link = html.escape(compact_link, quote=True)

    origin_name = html.escape(get_city_name(origin, ORIGIN_NAMES))
    destination_name = html.escape(get_city_name(destination, DESTINATION_NAMES))

    flight_number_safe = html.escape(str(flight_number))
    transfers_text = html.escape(format_transfers(ticket, origin, destination))

    block = (
        f"<b>{origin_name} → {destination_name}</b>\n"
        f"{origin} → {destination} | {departure_date}\n"
        f"<b>{format_money(price)} ₽</b>\n"
        f"Рейс: {flight_number_safe}\n"
        f"Пересадки: {transfers_text}\n"
        f'<a href="{safe_link}">Открыть билет</a>'
    )

    return {
        "price": price,
        "origin": origin,
        "destination": destination,
        "block": block,
    }


# ====== ОСНОВНАЯ ЛОГИКА ======

def main():
    checked_count = 0
    found_count = 0
    duplicate_count = 0
    alert_items = []
    pending_sent_records = []

    sent_db = None

    if DEDUP_ENABLED:
        sent_db = load_sent_db()
        cleanup_sent_db(sent_db)

    active_origins = get_active_origins()
    active_limits = get_active_destination_limits()

    print(f"Режим поиска: {SEARCH_MODE}")
    print(f"Антидубль включён: {DEDUP_ENABLED}")
    print(f"Период поиска: {SEARCH_START_DATE} — {SEARCH_END_DATE}")
    print(f"Месяцы поиска: {MONTHS}")
    print(f"Активные города вылета: {active_origins}")
    print(f"Активные направления: {list(active_limits.keys())}")

    for route in build_routes():
        origin = route["origin"]
        destination = route["destination"]
        month = route["month"]
        price_limit = route["price_limit"]
        direct_only = route["direct_only"]

        checked_count += 1

        tickets = get_flight_prices(origin, destination, month, direct_only)

        if not tickets:
            continue

        tickets = [
            ticket for ticket in tickets
            if is_ticket_inside_search_window(ticket)
        ]

        if not tickets:
            print(
                f"{origin} → {destination}, {month}: "
                f"билетов в период {SEARCH_START_DATE} — {SEARCH_END_DATE} нет"
            )
            continue

        if direct_only:
            tickets = [ticket for ticket in tickets if is_direct_ticket(ticket)]

            if not tickets:
                print(
                    f"{origin} → {destination}, {month}: "
                    f"прямых билетов нет"
                )
                continue

        cheapest = min(tickets, key=lambda item: item.get("price", 10**9))

        price = cheapest.get("price")

        if price is None:
            continue

        print(
            f"{origin} → {destination}, {month}: "
            f"найдена цена {price} ₽, порог {price_limit} ₽"
        )

        if price <= price_limit:
            if DEDUP_ENABLED:
                ticket_key = make_ticket_stable_key(route, cheapest)

                if should_skip_ticket(sent_db, ticket_key, cheapest):
                    duplicate_count += 1
                    print(
                        f"{origin} → {destination}, {month}: "
                        f"дубль для {SEARCH_MODE}, уже отправлялся "
                        f"за последние {DEDUP_KEEP_DAYS} дней"
                    )
                    continue

                pending_sent_records.append(
                    {
                        "key": ticket_key,
                        "route": route,
                        "ticket": cheapest,
                    }
                )

            found_count += 1
            alert_items.append(build_alert_item(route, cheapest))

    if alert_items:
        alert_items.sort(key=lambda item: item["price"])

        blocks = []

        for index, item in enumerate(alert_items, start=1):
            blocks.append(f"<b>{index})</b> {item['block']}")

        message = (
            f"{get_mode_title()}\n\n"
            + "\n\n--------------------\n\n".join(blocks)
        )

        send_long_telegram_message(message)

        if DEDUP_ENABLED:
            mark_tickets_as_sent(sent_db, pending_sent_records)
            save_sent_db(sent_db)

        print(f"Отправлено новых находок: {found_count}")
        print(f"Дублей пропущено: {duplicate_count}")

    else:
        if DEDUP_ENABLED:
            save_sent_db(sent_db)

        print(
            f"Проверка завершена. "
            f"Режим: {SEARCH_MODE}. "
            f"Проверено маршрутов: {checked_count}. "
            f"Новых билетов ниже лимитов нет. "
            f"Дублей пропущено: {duplicate_count}."
        )


if __name__ == "__main__":
    try:
        print("Запуск проверки...")
        main()
        print("Проверка завершена.")
    except Exception as error:
        print(f"Ошибка во время проверки: {error}")
        try:
            send_error_message(error)
        except Exception:
            pass
