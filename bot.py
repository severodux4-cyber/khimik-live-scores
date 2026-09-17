import os
import re
import json
import time
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

COMPETITION_URL = os.getenv(
    "COMPETITION_URL",
    "https://фхмо.рф/competitions/2026-2027/pervenstvo-moskovskoy-oblasti-po-khokkeyu-sredi-yunoshey-2026-2027/"
)
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
STATE_FILE = Path("state.json")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ФХМО начинает отвечать 503, если сделать слишком много запросов подряд.
# Поэтому используем повторные попытки + небольшую паузу между запросами.
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 KhimikScoresBot/4.0"
})

retry = Retry(
    total=4,
    connect=4,
    read=4,
    status=4,
    backoff_factor=2,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=True,
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

LAST_REQUEST = 0.0
REQUEST_DELAY = 1.0


def get(url):
    global LAST_REQUEST

    # Не долбим сайт десятками запросов подряд.
    wait = REQUEST_DELAY - (time.monotonic() - LAST_REQUEST)
    if wait > 0:
        time.sleep(wait)

    r = session.get(url, timeout=40)
    LAST_REQUEST = time.monotonic()
    r.raise_for_status()
    return r.text


def telegram(text):
    r = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    r.raise_for_status()


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def age_from(text):
    m = re.search(r"\b(20(?:1[0-7]))\s*г\.?\s*р\.?", text or "", re.I)
    return m.group(1) if m else None


def unique(seq):
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def discover_age_pages():
    soup = BeautifulSoup(get(COMPETITION_URL), "html.parser")
    result = []

    for a in soup.select("a[href]"):
        label = norm(a.get_text(" ", strip=True))
        age = age_from(label)
        href = urljoin(COMPETITION_URL, a["href"])

        if age and "/competitions/com_" in href:
            result.append((age, label, href.split("?")[0]))

    by_age = {}
    for age, label, href in result:
        by_age[age] = (age, label, href)

    return [by_age[a] for a in sorted(by_age)]


def discover_group_pages(age_url):
    soup = BeautifulSoup(get(age_url), "html.parser")
    result = []

    for a in soup.select("a[href]"):
        label = norm(a.get_text(" ", strip=True))
        href = urljoin(age_url, a["href"])

        if "cgroup=" in href and label in ("Первая группа", "Вторая группа"):
            result.append((label, href))

    return unique(result)


def discover_match_links(group_url):
    """
    Возвращает:
      all_links — все матчи группы;
      khimik_links — только ссылки, у которых ближайший контейнер/строка
                      содержит 'Химик Воскресенск'.

    Это позволяет не открывать сотни страниц матчей.
    """
    soup = BeautifulSoup(get(group_url), "html.parser")

    all_links = []
    khimik_links = []

    for a in soup.select('a[href*="/matches/m_"]'):
        href = urljoin(group_url, a["href"]).split("?")[0]

        if href in all_links:
            continue

        all_links.append(href)

        # Сначала пробуем ближайшую строку таблицы.
        context = ""
        parent = a.find_parent("tr")
        if parent:
            context = norm(parent.get_text(" ", strip=True))

        # Если это не таблица — ищем ближайший разумный контейнер.
        if not context:
            for tag_name in ("li", "article", "div"):
                parent = a.find_parent(tag_name)
                if parent:
                    text = norm(parent.get_text(" ", strip=True))
                    if 20 <= len(text) <= 800:
                        context = text
                        break

        if "Химик Воскресенск" in context:
            khimik_links.append(href)

    return unique(all_links), unique(khimik_links)


def parse_match(match_url, age, group_label):
    soup = BeautifulSoup(get(match_url), "html.parser")
    page_text = norm(soup.get_text(" ", strip=True))

    title = norm(soup.title.get_text(" ", strip=True) if soup.title else "")
    title = re.sub(r"^Матч\s+", "", title, flags=re.I)
    title = re.sub(
        r"\s+Первенство Московской области.*$",
        "",
        title,
        flags=re.I,
    )

    if " - " in title:
        home, away = [norm(x) for x in title.split(" - ", 1)]
    else:
        names = []
        for tag in soup.find_all(["h1", "h2", "h3"]):
            t = norm(tag.get_text(" ", strip=True))
            if t and t not in names:
                names.append(t)

        if len(names) >= 2:
            home, away = names[0], names[1]
        else:
            return None

    # Последняя накопленная пара счёта в ленте событий.
    # Значения вида 45:00 / 00:00 являются временем, а не счётом.
    score_candidates = re.findall(
        r"(?<!\d)(\d{1,2}):(\d{1,2})(?!\d)",
        page_text
    )

    scores = [
        (int(a), int(b))
        for a, b in score_candidates
        if int(a) <= 30 and int(b) <= 30
    ]

    if not scores:
        return None

    current_score = f"{scores[-1][0]}:{scores[-1][1]}"

    dt = ""
    m = re.search(
        r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})",
        page_text
    )
    if m:
        dt = f"{m.group(1)} {m.group(2)}"

    clocks = re.findall(
        r"(?<!\d)(\d{1,3}):(\d{2})(?!\d)",
        page_text
    )

    max_seconds = 0
    for mm, ss in clocks:
        max_seconds = max(max_seconds, int(mm) * 60 + int(ss))

    if max_seconds >= 45 * 60:
        status = "🟢 Матч завершён"
    elif "Третий период" in page_text:
        status = "⏱ 3 период"
    elif "Второй период" in page_text:
        status = "⏱ 2 период"
    elif "1 период" in page_text:
        status = "⏱ 1 период"
    else:
        status = "⏱ Матч идёт"

    return {
        "age": age,
        "group": group_label,
        "home": home,
        "away": away,
        "score": current_score,
        "date_time": dt,
        "status": status,
        "url": match_url,
    }


def main():
    state = load_state()

    ages = discover_age_pages()
    logging.info("Найдено возрастов: %d", len(ages))

    all_groups = []

    for age, label, age_url in ages:
        try:
            groups = discover_group_pages(age_url)
            logging.info("%s: групп %d", age, len(groups))

            for group_label, group_url in groups:
                all_groups.append((age, group_label, group_url))

        except Exception as e:
            logging.warning("Ошибка возраста %s: %s", age, e)

    current = {}

    for age, group_label, group_url in all_groups:
        try:
            all_links, khimik_links = discover_match_links(group_url)

            logging.info(
                "%s %s: матчей/ссылок %d, матчей Химика по группе %d",
                age,
                group_label,
                len(all_links),
                len(khimik_links),
            )

            # Для 2017 нужны ВСЕ матчи группы Химика.
            if age == "2017":
                if khimik_links:
                    logging.info(
                        "2017 %s: группа Химика найдена",
                        group_label,
                    )

                    for link in all_links:
                        try:
                            match = parse_match(link, age, group_label)
                            if match:
                                current[link] = match
                        except Exception as e:
                            logging.warning(
                                "Ошибка матча %s: %s",
                                link,
                                e,
                            )

            # Для остальных годов нужны только матчи самого Химика.
            else:
                for link in khimik_links:
                    try:
                        match = parse_match(link, age, group_label)
                        if match:
                            current[link] = match
                    except Exception as e:
                        logging.warning(
                            "Ошибка матча %s: %s",
                            link,
                            e,
                        )

        except Exception as e:
            logging.warning(
                "Ошибка группы %s %s: %s",
                age,
                group_label,
                e,
            )

    logging.info("Отслеживаемых матчей: %d", len(current))

    for key, match in current.items():
        old = state.get(key)

        # На первом обнаружении просто запоминаем счёт.
        # Telegram отправляем только при изменении счёта.
        if old and old.get("score") != match["score"]:
            telegram(
                f"🏒 Химик Воскресенск {match['age']}\n"
                f"{match['home']} — {match['away']}\n"
                f"{match['score']}\n"
                f"{match['status']}"
            )

        state[key] = match

    save_state(state)


if __name__ == "__main__":
    main()
