import html
import json
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from config import (
    CURRENCY,
    DESTINATION_NAMES,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    ORIGIN_NAMES,
    SEARCH_DAYS_AHEAD,
)


API_URL = "https://api.travelpayouts.com/aviasales/v3/search_by_price_range"
SPECIAL_OFFERS_URL = "https://api.travelpayouts.com/aviasales/v3/get_special_offers"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")

DEAL_MONITOR_ORIGINS = ["ALA", "TAS"]
DEAL_MONITOR_MIN_DISCOUNT = 0.30
DEAL_MONITOR_STRONG_DISCOUNT = 0.40
DEAL_MONITOR_LOOKAHEAD_DAYS = SEARCH_DAYS_AHEAD
DEAL_MONITOR_MAX_PRICE = 500000
DEAL_MONITOR_PAGE_SIZE = 100
DEAL_MONITOR_MAX_PAGES = 20
DEAL_MONITOR_MIN_REFERENCE_SAMPLES = 5
DEAL_MONITOR_HISTORY_DAYS = 90
DEAL_MONITOR_MAX_ALERTS = 20
STATE_PATH = Path("deal_monitor_state.json")
TODAY = datetime.now(timezone.utc).date()
END_DATE = TODAY + timedelta(days=DEAL_MONITOR_LOOKAHEAD_DAYS)


def require_secrets():
    missing = []

    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not TRAVELPAYOUTS_TOKEN:
        missing.append("TRAVELPAYOUTS_TOKEN")

    if missing:
        raise ValueError(
            "Не найдены секреты GitHub Actions: " + ", ".join(missing)
        )


def api_get(url: str, params: dict):
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"X-Access-Token": TRAVELPAYOUTS_TOKEN},
                timeout=30,
            )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    time.sleep(5 * attempt)
                    continue

            response.raise_for_status()
            payload = response.json()

            if payload.get("success") is False:
                raise RuntimeError(
                    "Travelpayouts вернул ошибку: "
                    + str(payload.get("error") or payload)[:500]
                )

            return payload

        except requests.exceptions.RequestException:
            if attempt == 3:
                raise
            time.sleep(5 * attempt)

    return {}


def parse_departure_date(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def normalize_candidate(raw: dict, origin: str):
    if raw.get("return_at"):
        return None

    destination = str(
        raw.get("destination_code")
        or raw.get("destination")
        or raw.get("destination_airport")
        or ""
    ).strip().upper()
    departure = parse_departure_date(
        raw.get("departure_at") or raw.get("depart_date")
    )

    try:
        price = int(float(raw.get("price") or raw.get("value")))
    except (TypeError, ValueError):
        return None

    if not destination or destination == origin or not departure or price <= 0:
        return None

    if not TODAY <= departure <= END_DATE:
        return None

    transfers = raw.get("transfers", raw.get("number_of_changes"))
    try:
        transfers = int(transfers)
        transfers_key = str(transfers)
    except (TypeError, ValueError):
        transfers = None
        transfers_key = "?"

    destination_name = (
        DESTINATION_NAMES.get(destination)
        or raw.get("destination_name")
        or destination
    )

    return {
        "origin": origin,
        "destination": destination,
        "destination_name": str(destination_name),
        "departure_at": departure.isoformat(),
        "price": price,
        "transfers": transfers,
        "transfers_key": transfers_key,
        "link": raw.get("link") or "",
        "flight_number": raw.get("flight_number") or "",
    }


def get_all_destination_candidates(origin: str):
    candidates = []

    for page in range(1, DEAL_MONITOR_MAX_PAGES + 1):
        payload = api_get(
            API_URL,
            {
                "origin": origin,
                "destination": "-",
                "value_min": 1,
                "value_max": DEAL_MONITOR_MAX_PRICE,
                "one_way": "true",
                "direct": "false",
                "locale": "ru",
                "currency": CURRENCY,
                "market": "ru",
                "limit": DEAL_MONITOR_PAGE_SIZE,
                "page": page,
            },
        )

        page_data = payload.get("data") or []
        if not isinstance(page_data, list) or not page_data:
            break

        candidates.extend(
            candidate
            for candidate in (
                normalize_candidate(item, origin)
                for item in page_data
                if isinstance(item, dict)
            )
            if candidate
        )

        if len(page_data) < DEAL_MONITOR_PAGE_SIZE:
            break

        if page == DEAL_MONITOR_MAX_PAGES:
            print(
                f"{origin}: достигнут предел страниц поиска "
                f"({DEAL_MONITOR_MAX_PAGES})"
            )

    return candidates


def get_special_offer_candidates(origin: str):
    try:
        payload = api_get(
            SPECIAL_OFFERS_URL,
            {
                "origin": origin,
                "locale": "ru",
                "currency": CURRENCY,
                "market": "ru",
            },
        )
    except Exception as error:
        print(f"{origin}: не удалось получить спецпредложения: {error}")
        return []

    data = payload.get("data") or []
    if not isinstance(data, list):
        return []

    return [
        candidate
        for candidate in (
            normalize_candidate(item, origin)
            for item in data
            if isinstance(item, dict)
        )
        if candidate
    ]


def deduplicate_candidates(candidates: list):
    cheapest_by_key = {}

    for candidate in candidates:
        key = "|".join(
            [
                candidate["origin"],
                candidate["destination"],
                candidate["departure_at"],
                candidate["transfers_key"],
            ]
        )
        existing = cheapest_by_key.get(key)

        if not existing or candidate["price"] < existing["price"]:
            cheapest_by_key[key] = candidate

    return list(cheapest_by_key.values())


def get_reference_key(candidate: dict):
    departure = date.fromisoformat(candidate["departure_at"])
    return "|".join(
        [
            candidate["origin"],
            candidate["destination"],
            departure.strftime("%m"),
            candidate["transfers_key"],
        ]
    )


def get_daily_route_prices(candidates: list):
    groups = {}

    for candidate in candidates:
        route_key = get_reference_key(candidate)
        daily_prices = groups.setdefault(route_key, {})
        departure = candidate["departure_at"]
        current = daily_prices.get(departure)

        if current is None or candidate["price"] < current:
            daily_prices[departure] = candidate["price"]

    return groups


def load_state():
    empty_state = {"version": 1, "history": {}, "sent": {}}

    if not STATE_PATH.exists():
        return empty_state

    try:
        with STATE_PATH.open("r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError):
        print("Файл состояния не прочитан; создаю новое состояние")
        return empty_state

    if not isinstance(state, dict):
        return empty_state

    if not isinstance(state.get("history"), dict):
        state["history"] = {}
    if not isinstance(state.get("sent"), dict):
        state["sent"] = {}
    state["version"] = 1
    return state


def save_state(state: dict):
    temp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    temp_path.replace(STATE_PATH)


def cleanup_state(state: dict):
    cutoff = TODAY - timedelta(days=DEAL_MONITOR_HISTORY_DAYS)

    for route_key, samples in list(state["history"].items()):
        if not isinstance(samples, dict):
            del state["history"][route_key]
            continue

        for observed_on in list(samples):
            try:
                observed_date = date.fromisoformat(observed_on)
            except ValueError:
                del samples[observed_on]
                continue

            if observed_date < cutoff:
                del samples[observed_on]

        if not samples:
            del state["history"][route_key]

    for ticket_key, record in list(state["sent"].items()):
        if not isinstance(record, dict):
            del state["sent"][ticket_key]
            continue

        departure = parse_departure_date(record.get("departure_at"))
        if departure and departure < TODAY:
            del state["sent"][ticket_key]


def build_reference_prices(state: dict, daily_route_prices: dict):
    references = {}
    today_key = TODAY.isoformat()

    for route_key, departure_prices in daily_route_prices.items():
        if len(departure_prices) < DEAL_MONITOR_MIN_REFERENCE_SAMPLES:
            continue

        current_median = int(statistics.median(departure_prices.values()))
        samples = state["history"].setdefault(route_key, {})

        prior_prices = []
        for observed_on, price in samples.items():
            if observed_on == today_key:
                continue
            try:
                prior_date = date.fromisoformat(observed_on)
                price = int(price)
            except (ValueError, TypeError):
                continue
            if TODAY - timedelta(days=DEAL_MONITOR_HISTORY_DAYS) <= prior_date < TODAY:
                prior_prices.append(price)

        if len(prior_prices) >= DEAL_MONITOR_MIN_REFERENCE_SAMPLES:
            references[route_key] = {
                "price": int(statistics.median(prior_prices)),
                "source": "история цен бота за последние 90 дней",
            }
        else:
            references[route_key] = {
                "price": current_median,
                "source": "цены на похожие даты в текущей выдаче",
            }

        samples.setdefault(today_key, current_median)

    return references


def make_aviasales_link(candidate: dict):
    link = str(candidate.get("link") or "")
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if link.startswith("/"):
        return "https://www.aviasales.ru" + link
    return (
        "https://www.aviasales.ru/search/"
        f"{candidate['origin']}{candidate['destination']}1"
    )


def format_transfers(candidate: dict):
    count = candidate.get("transfers")
    if count == 0:
        return "без пересадок"
    if count == 1:
        return "1 пересадка"
    if count is not None and 2 <= count <= 4:
        return f"{count} пересадки"
    if count is not None:
        return f"{count} пересадок"
    return "пересадки не указаны"


def format_money(value):
    return f"{int(value):,}".replace(",", " ")


def build_alert(candidate: dict, reference: dict, discount: float):
    origin_name = html.escape(ORIGIN_NAMES.get(candidate["origin"], candidate["origin"]))
    destination_name = html.escape(candidate["destination_name"])
    departure_date = date.fromisoformat(candidate["departure_at"]).strftime("%d.%m.%Y")
    link = html.escape(make_aviasales_link(candidate), quote=True)
    discount_percent = round(discount * 100)
    strength = (
        "🔥 Сильная цена"
        if discount >= DEAL_MONITOR_STRONG_DISCOUNT
        else "💸 Цена заметно ниже обычного уровня"
    )
    flight_number = candidate.get("flight_number")
    flight_line = (
        f"Рейс: {html.escape(str(flight_number))}\n" if flight_number else ""
    )

    return (
        f"<b>{origin_name} → {destination_name}</b>\n"
        f"{candidate['origin']} → {candidate['destination']} | {departure_date}\n"
        f"<b>{format_money(candidate['price'])} ₽</b> — "
        f"ниже ориентира на {discount_percent}%\n"
        f"Ориентир по направлению: {format_money(reference['price'])} ₽\n"
        f"Пересадки: {html.escape(format_transfers(candidate))}\n"
        f"{flight_line}"
        f"<a href=\"{link}\">Открыть билет</a>\n"
        f"<i>{html.escape(reference['source'])}</i>\n"
        f"{strength}"
    )


def send_telegram_message(message: str):
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Telegram вернул HTTP {response.status_code}")
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Ошибка отправки в Telegram: {payload}")


def send_long_message(message: str):
    if len(message) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        send_telegram_message(message)
        return

    sections = []
    current = ""
    for section in message.split("\n\n--------------------\n\n"):
        proposed = (
            section
            if not current
            else current + "\n\n--------------------\n\n" + section
        )
        if current and len(proposed) > MAX_TELEGRAM_MESSAGE_LENGTH:
            sections.append(current)
            current = section
        else:
            current = proposed
    if current:
        sections.append(current)

    for index, section in enumerate(sections, start=1):
        send_telegram_message(f"{section}\n\nЧасть {index}/{len(sections)}")


def ticket_key(candidate: dict):
    return "|".join(
        [
            candidate["origin"],
            candidate["destination"],
            candidate["departure_at"],
            candidate["transfers_key"],
        ]
    )


def main():
    require_secrets()
    state = load_state()
    cleanup_state(state)

    candidates = []
    for origin in DEAL_MONITOR_ORIGINS:
        print(f"Ищу направления из {origin} по всем доступным городам")
        candidates.extend(get_all_destination_candidates(origin))
        candidates.extend(get_special_offer_candidates(origin))

    candidates = deduplicate_candidates(candidates)
    daily_route_prices = get_daily_route_prices(candidates)
    references = build_reference_prices(state, daily_route_prices)

    deals = []
    for candidate in candidates:
        route_key = get_reference_key(candidate)
        reference = references.get(route_key)
        if not reference or reference["price"] <= 0:
            continue

        discount = 1 - candidate["price"] / reference["price"]
        if discount < DEAL_MONITOR_MIN_DISCOUNT:
            continue

        stable_key = ticket_key(candidate)
        prior = state["sent"].get(stable_key)
        if prior:
            try:
                if candidate["price"] >= int(prior.get("best_price")):
                    continue
            except (TypeError, ValueError):
                pass

        deals.append(
            {
                "candidate": candidate,
                "reference": reference,
                "discount": discount,
                "key": stable_key,
            }
        )

    deals.sort(key=lambda item: (-item["discount"], item["candidate"]["price"]))
    deals = deals[:DEAL_MONITOR_MAX_ALERTS]

    # Сохраняем накопленные рыночные наблюдения даже в дни без уведомлений.
    save_state(state)

    if not deals:
        print(
            f"Проверено цен: {len(candidates)}. "
            "На 30% ниже доступного ценового ориентира билетов не найдено."
        )
        return

    blocks = []
    for index, deal in enumerate(deals, start=1):
        blocks.append(
            f"<b>{index})</b> "
            + build_alert(deal["candidate"], deal["reference"], deal["discount"])
        )

    message = (
        "🌍 <b>Аномально дешёвые билеты из Алматы и Ташкента</b>\n\n"
        + "\n\n--------------------\n\n".join(blocks)
    )
    send_long_message(message)

    for deal in deals:
        candidate = deal["candidate"]
        old_record = state["sent"].get(deal["key"], {})
        try:
            best_price = min(
                int(old_record.get("best_price", candidate["price"])),
                candidate["price"],
            )
        except (TypeError, ValueError):
            best_price = candidate["price"]
        state["sent"][deal["key"]] = {
            "best_price": best_price,
            "last_sent": datetime.now(timezone.utc).isoformat(),
            "departure_at": candidate["departure_at"],
        }

    save_state(state)
    print(f"Отправлено уведомлений: {len(deals)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Ошибка монитора дешёвых билетов: {error}")
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            try:
                send_telegram_message(
                    "⚠️ <b>Ошибка монитора дешёвых билетов</b>\n"
                    + html.escape(str(error))[:1500]
                )
            except Exception:
                pass
        raise
