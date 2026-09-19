import os
import re
import json
import logging
import time
from pathlib import Path
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
STATE_FILE = Path("state.json")

# TEST_NOTIFY=true используется только для ручной проверки Telegram.
TEST_NOTIFY = os.getenv("TEST_NOTIFY", "").lower() in ("1", "true", "yes", "on")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 KhimikScoresBot/4.0",
})

retry = Retry(
    total=4,
    connect=4,
    read=4,
    backoff_factor=2,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=True,
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

last_get = 0.0


def get(url):
    global last_get

    # Не долбим ФХМО слишком быстро.
    wait = 0.4 - (time.monotonic() - last_get)
    if wait > 0:
        time.sleep(wait)

    r = session.get(url, timeout=30)
    last_get = time.monotonic()
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
        href = urljoin(COMPETITION_URL, a["href"]).split("?")[0]

        if age and "/competitions/com_" in href:
            result.append((age, label, href))

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
    Возвращает ссылки всех матчей группы и ссылки матчей Химика.

    Для определения конкретного матча не используем всю страницу:
    поднимаемся от ссылки на матч по DOM и выбираем самый маленький
    контейнер, в котором одновременно есть дата/время и название
    Химика. Если такой контейнер не найден, матч останется в all_links.
    """
    soup = BeautifulSoup(get(group_url), "html.parser")

    all_links = []
    khimik_links = []

    for a in soup.select('a[href*="/matches/m_"]'):
        href = urljoin(group_url, a["href"]).split("?")[0]
        if href in all_links:
            continue

        all_links.append(href)

        node = a
        found = False

        for _ in range(8):
            node = node.parent
            if node is None:
                break

            text = norm(node.get_text(" ", strip=True))
            if not text or len(text) > 1800:
                break

            has_khimik = "Химик Воскресенск" in text
            has_match_marker = (
                "Обзор матча" in text
                or re.search(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", text)
                or re.search(r"\b\d{1,2}:\d{2}\b", text)
            )

            if has_khimik and has_match_marker:
                khimik_links.append(href)
                found = True
                break

        if found:
            continue

    group_text = norm(soup.get_text(" ", strip=True))
    khimik_in_group = "Химик Воскресенск" in group_text

    return unique(all_links), unique(khimik_links), khimik_in_group

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
        # Дополнительный fallback.
        names = []

        for tag in soup.find_all(["h1", "h2", "h3"]):
            t = norm(tag.get_text(" ", strip=True))
            if t and t not in names:
                names.append(t)

        if len(names) >= 2:
            home, away = names[0], names[1]
        else:
            # На страницах ФХМО команды также можно найти по alt у картинок.
            image_names = []
            for img in soup.find_all("img"):
                alt = norm(img.get("alt", ""))
                if alt and alt not in image_names:
                    image_names.append(alt)

            candidates = [
                x for x in image_names
                if x and "Image" not in x and len(x) > 2
            ]

            if len(candidates) >= 2:
                home, away = candidates[:2]
            else:
                return None

    score_re = re.compile(r"(?<!\d)(\d{1,2}):(\d{1,2})(?!\d)")

    def valid_scores(text):
        result = []
        for a, b in score_re.findall(text or ""):
            a, b = int(a), int(b)
            if a <= 30 and b <= 30:
                result.append((a, b))
        return result

    # 1) Приоритетно ищем счёт в блоках событий матча.
    # ФХМО может не обновлять крупный итоговый счёт, поэтому берём
    # последнюю накопленную пару из блока с событиями/голами.
    event_scores = []

    event_words = (
        "гол", "шайб", "заброш", "взятие ворот",
        "вбрасыван", "удален", "удалён"
    )

    for tag in soup.find_all(["div", "li", "tr", "td", "p", "span"]):
        text = norm(tag.get_text(" ", strip=True))
        if not text or len(text) > 1200:
            continue
        lower = text.lower()
        if any(word in lower for word in event_words):
            vals = valid_scores(text)
            if vals:
                event_scores.extend(vals)

    if event_scores:
        current_score = f"{event_scores[-1][0]}:{event_scores[-1][1]}"
        score_source = "event_feed"
    else:
        # 2) Резерв: вся страница, но исключаем минуты/секунды матча.
        scores = valid_scores(page_text)
        if not scores:
            return None
        current_score = f"{scores[-1][0]}:{scores[-1][1]}"
        score_source = "page"

    logging.info(
        "MATCH %s | %s — %s | score=%s | source=%s",
        match_url.rsplit("/", 2)[-2] if "/matches/" in match_url else match_url,
        home,
        away,
        current_score,
        score_source,
    )

    dt = ""
    m = re.search(
        r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})",
        page_text,
    )
    if m:
        dt = f"{m.group(1)} {m.group(2)}"

    clocks = re.findall(
        r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)",
        page_text,
    )

    max_seconds = 0
    for c in clocks:
        mm, ss = map(int, c)
        max_seconds = max(max_seconds, mm * 60 + ss)

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

    # Отдельный тест Telegram. Никаких запросов к ФХМО и никаких изменений state.
    if TEST_NOTIFY:
        telegram(
            "🏒 ТЕСТ Khimik Live Scores\n"
            "Telegram-уведомления работают корректно.\n"
            "Это тестовое сообщение, состояние матчей не изменено."
        )
        logging.info("TEST_NOTIFY: тестовое сообщение отправлено в Telegram")
        return

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
    khimik_ages = []

    for age, group_label, group_url in all_groups:
        try:
            all_links, khimik_links, khimik_in_group = discover_match_links(group_url)

            logging.info(
                "%s %s: матчей/ссылок %d, матчей Химика по группе %d",
                age,
                group_label,
                len(all_links),
                len(khimik_links),
            )

            if khimik_in_group:
                khimik_ages.append(age)

            # Для 2017 — ВСЯ группа, в которой находится Химик.
            if age == "2017" and khimik_in_group:
                logging.info(
                    "2017 %s: группа Химика найдена — отслеживаем все %d матчей",
                    group_label,
                    len(all_links),
                )

                links_to_parse = all_links

            # Для остальных возрастов — только матчи Химика.
            else:
                links_to_parse = khimik_links

            for link in links_to_parse:
                try:
                    match = parse_match(link, age, group_label)
                    if not match:
                        continue

                    # Дополнительная защита: для не-2017 проверяем команды
                    # уже на странице конкретного матча.
                    if age != "2017":
                        if "Химик Воскресенск" not in (
                            match["home"],
                            match["away"],
                        ):
                            continue

                    current[link] = match

                except Exception as e:
                    logging.warning("Ошибка матча %s: %s", link, e)

        except Exception as e:
            logging.warning(
                "Ошибка группы %s %s: %s",
                age,
                group_label,
                e,
            )

    khimik_ages = sorted(set(khimik_ages))
    logging.info(
        "Возрастов с Химиком: %s",
        ", ".join(khimik_ages) if khimik_ages else "не найдено",
    )
    logging.info("Отслеживаемых матчей: %d", len(current))

    for key, match in current.items():
        old = state.get(key)

        # Уведомляем только об изменении счёта.
        if old and old.get("score") != match["score"]:
            telegram(
                f"🏒 Химик Воскресенск {match['age']}\n"
                f"{match['home']} — {match['away']}\n"
                f"{match['score']}\n"
                f"{match['status']}"
            )

            logging.info(
                "ОТПРАВЛЕНО: %s %s — %s: %s → %s",
                match["age"],
                match["home"],
                match["away"],
                old.get("score"),
                match["score"],
            )
        elif old:
            logging.info(
                "Без изменения: %s | %s — %s | %s",
                match["age"],
                match["home"],
                match["away"],
                match["score"],
            )
        else:
            logging.info(
                "Первичная запись: %s | %s — %s | %s",
                match["age"],
                match["home"],
                match["away"],
                match["score"],
            )

        state[key] = match

    save_state(state)


if __name__ == "__main__":
    main()
