import os
import re
import time
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

COMPETITION_URL = os.getenv(
    "COMPETITION_URL",
    "https://фхмо.рф/competitions/2026-2027/pervenstvo-moskovskoy-oblasti-po-khokkeyu-sredi-yunoshey-2026-2027/",
)
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

REQUEST_DELAY = 0.65
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 KhimikTablesTest/1.0"
})

retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=False,
)
session.mount(
    "https://",
    HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2),
)

_last_request = 0.0


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def get(url):
    global _last_request
    wait = REQUEST_DELAY - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)

    try:
        response = session.get(url, timeout=20)
        response.raise_for_status()
        return response.text
    finally:
        _last_request = time.monotonic()


def telegram(text):
    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": str(CHAT_ID), "text": text},
        timeout=15,
    )
    response.raise_for_status()


def age_from(text):
    match = re.search(r"\b(20(?:1[0-7]))\s*г\.?\s*р\.?", text or "", re.I)
    return match.group(1) if match else None


def discover_age_pages():
    soup = BeautifulSoup(get(COMPETITION_URL), "html.parser")
    by_age = {}

    for a in soup.select("a[href]"):
        label = norm(a.get_text(" ", strip=True))
        href = urljoin(COMPETITION_URL, a["href"]).split("#")[0]
        age = age_from(label)
        if age and "/competitions/com_" in href:
            by_age[age] = (age, label, href)

    return [by_age[age] for age in sorted(by_age)]


def discover_group_pages(age_url):
    soup = BeautifulSoup(get(age_url), "html.parser")
    result = []

    for a in soup.select("a[href]"):
        label = norm(a.get_text(" ", strip=True))
        href = urljoin(age_url, a["href"])
        if "cgroup=" in href and label in ("Первая группа", "Вторая группа"):
            result.append((label, href))

    # Удаляем дубли, сохраняя порядок.
    seen = set()
    unique = []
    for item in result:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def parse_standings(soup):
    """Разбирает таблицу результатов группы ФХМО."""
    candidates = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        parsed = []

        for row in rows:
            cells = [
                norm(cell.get_text(" ", strip=True))
                for cell in row.find_all(["th", "td"], recursive=False)
            ]
            cells = [cell for cell in cells if cell]

            if len(cells) < 12:
                continue

            stats_cells = cells[-11:]
            if not all(re.fullmatch(r"-?\d+", cell) for cell in stats_cells):
                continue

            prefix = norm(" ".join(cells[:-11]))
            match = re.match(r"^(\d+)\s*(.*)$", prefix)
            if not match:
                continue

            place = int(match.group(1))
            team = norm(match.group(2))
            if not team or team.lower() in {"команда", "место"}:
                continue

            stats = [int(value) for value in stats_cells]
            parsed.append(
                {
                    "place": place,
                    "team": team,
                    "games": stats[0],
                    "wins": stats[1],
                    "ot_wins": stats[2],
                    "losses": stats[3],
                    "ot_losses": stats[4],
                    "so_wins": stats[5],
                    "so_losses": stats[6],
                    "scored": stats[7],
                    "conceded": stats[8],
                    "diff": stats[9],
                    "points": stats[10],
                }
            )

        if parsed:
            candidates.append(parsed)

    # Берём таблицу, в которой явно есть Химик. Если точного совпадения
    # нет из-за оформления названия, допускаем строку, содержащую "химик".
    for parsed in candidates:
        if any("химик воскресенск" in row["team"].lower() for row in parsed):
            return sorted(parsed, key=lambda row: row["place"])

    return []


def send_test_tables():
    ages = discover_age_pages()
    logging.info("Найдено возрастов на странице соревнования: %d", len(ages))

    sent = 0
    found_ages = []

    for age, _label, age_url in ages:
        try:
            groups = discover_group_pages(age_url)
        except requests.RequestException as exc:
            logging.warning("TABLE TEST: ошибка возраста %s: %s", age, exc)
            continue

        for group_label, group_url in groups:
            try:
                soup = BeautifulSoup(get(group_url), "html.parser")
                group_text = norm(soup.get_text(" ", strip=True)).lower()
                if "химик воскресенск" not in group_text:
                    continue

                standings = parse_standings(soup)
                if not standings:
                    logging.warning(
                        "TABLE TEST: Химик найден, но таблица не разобралась: %s %s",
                        age,
                        group_label,
                    )
                    continue

                found_ages.append(age)

                lines = [
                    f"🏒 ХИМИК ВОСКРЕСЕНСК {age}",
                    f"📊 ТАБЛИЦА — {group_label}",
                    "",
                    "Место | Команда | И | В | ОТВ | П | ОТП | БВ | БП | ШЗ | ШП | Р | О",
                ]

                for row in standings:
                    marker = "🏒 " if "химик воскресенск" in row["team"].lower() else ""
                    lines.append(
                        f"{row['place']}. {marker}{row['team']} | "
                        f"{row['games']} | {row['wins']} | {row['ot_wins']} | "
                        f"{row['losses']} | {row['ot_losses']} | {row['so_wins']} | "
                        f"{row['so_losses']} | {row['scored']} | {row['conceded']} | "
                        f"{row['diff']:+d} | {row['points']}"
                    )

                message = "\n".join(lines)
                telegram(message)
                sent += 1
                logging.info("TABLE TEST: отправлена %s %s", age, group_label)

            except requests.RequestException as exc:
                logging.warning(
                    "TABLE TEST: HTTP ошибка %s %s: %s",
                    age,
                    group_label,
                    exc,
                )
            except Exception as exc:
                logging.warning(
                    "TABLE TEST: ошибка %s %s: %s",
                    age,
                    group_label,
                    exc,
                )

    telegram(
        "🏒 ХИМИК LIVE — ТЕСТ ТАБЛИЦ\n\n"
        f"Найдено возрастов с Химиком: {len(sorted(set(found_ages)))}\n"
        f"Отправлено таблиц: {sent}"
    )
    logging.info("TABLE TEST: найдено возрастов %s", ", ".join(sorted(set(found_ages))) or "нет")
    logging.info("TABLE TEST: отправлено таблиц %d", sent)


if __name__ == "__main__":
    send_test_tables()
