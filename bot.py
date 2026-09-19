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
TEST_NOTIFY = os.getenv("TEST_NOTIFY", "").lower() in ("1", "true", "yes", "on")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 KhimikScoresBot/5.0",
})

# ФХМО периодически отвечает 503. Не делаем длинных серий повторов:
# максимум одна повторная попытка после короткой паузы.
retry = Retry(
    total=1,
    connect=1,
    read=1,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=False,
)
session.mount("https://", HTTPAdapter(max_retries=retry))
session.mount("http://", HTTPAdapter(max_retries=retry))

last_get = 0.0
GET_DELAY = 0.45


def get(url):
    global last_get
    wait = GET_DELAY - (time.monotonic() - last_get)
    if wait > 0:
        time.sleep(wait)
    r = session.get(url, timeout=18)
    last_get = time.monotonic()
    r.raise_for_status()
    return r.text


def telegram(text):
    r = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=15,
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
    by_age = {}
    for a in soup.select("a[href]"):
        label = norm(a.get_text(" ", strip=True))
        age = age_from(label)
        href = urljoin(COMPETITION_URL, a["href"]).split("?")[0]
        if age and "/competitions/com_" in href:
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


def _match_container(link):
    """Найти ближайший DOM-контейнер именно этой карточки матча."""
    node = link
    for _ in range(8):
        node = node.parent
        if node is None:
            return None
        teams = [norm(x.get_text(" ", strip=True)) for x in node.select(":scope .team-name-match")]
        # В карточке матча ровно две команды.
        if len(teams) == 2:
            return node, teams
    return None


def discover_match_links(group_url):
    """
    Один запрос к странице группы.
    Возвращает все матчи группы и только матчи Химика.

    Важно: Химик определяется внутри КОНКРЕТНОЙ карточки матча.
    Нельзя искать слово "Химик" в родительском блоке страницы группы —
    иначе один матч Химика ошибочно помечает всю группу.
    """
    soup = BeautifulSoup(get(group_url), "html.parser")
    all_links = []
    khimik_links = []

    for a in soup.select('a[href*="/matches/m_"]'):
        href = urljoin(group_url, a["href"]).split("?")[0]
        if href in all_links:
            continue
        all_links.append(href)

        found = _match_container(a)
        if found:
            _, teams = found
            if any("Химик Воскресенск" in t for t in teams):
                khimik_links.append(href)

    # Определяем участие Химика в группе по найденным карточкам.
    khimik_in_group = bool(khimik_links)
    return unique(all_links), unique(khimik_links), khimik_in_group


def parse_score(soup):
    """Разбирает реальный scoreboard ФХМО, а для live-матча — события голов."""
    # Основной источник: финальный/текущий scoreboard.
    scores = [norm(x.get_text(" ", strip=True)) for x in soup.select(".final-score .team-score")]
    scores = [x for x in scores if re.fullmatch(r"\d{1,2}", x)]
    if len(scores) >= 2:
        a, b = int(scores[0]), int(scores[1])
        # Если scoreboard уже содержит ненулевой счёт — это самый надёжный источник.
        if a != 0 or b != 0:
            return f"{a}:{b}"

    # На live-матче ФХМО scoreboard может временно оставаться 0:0,
    # пока события голов уже есть на странице. Считаем только события "Гол".
    home_goals = len(soup.select(".cub-event.team1-event .popup-title"))
    away_goals = len(soup.select(".cub-event.team2-event .popup-title"))

    # В каждом popup-title могут быть удаления и другие события,
    # поэтому учитываем только popup-title с текстом "Гол".
    def goal_count(selector):
        return sum(
            1 for x in soup.select(selector)
            if norm(x.get_text(" ", strip=True)).lower() == "гол"
        )

    home_goals = goal_count(".cub-event.team1-event .popup-title")
    away_goals = goal_count(".cub-event.team2-event .popup-title")

    if home_goals or away_goals:
        return f"{home_goals}:{away_goals}"

    # Нулевой счёт до первого гола.
    if len(scores) >= 2:
        return f"{int(scores[0])}:{int(scores[1])}"

    return None


def parse_status(soup, page_text):
    # Явные подписи периода на странице.
    if re.search(r"Третий период", page_text, re.I):
        return "⏱ 3 период"
    if re.search(r"Второй период", page_text, re.I):
        return "⏱ 2 период"
    if re.search(r"(?:Первый|1) период", page_text, re.I):
        return "⏱ 1 период"

    # Если есть финальный блок/завершённый матч, считаем завершённым.
    if soup.select_one(".final-score") and re.search(r"матч заверш|завершен|завершён|итог", page_text, re.I):
        return "🟢 Матч завершён"

    # По времени событий это live; точное определение периода не всегда есть.
    return "⏱ Матч идёт"


def parse_match(match_url, age, group_label):
    soup = BeautifulSoup(get(match_url), "html.parser")
    page_text = norm(soup.get_text(" ", strip=True))

    title = norm(soup.title.get_text(" ", strip=True) if soup.title else "")
    title = re.sub(r"^Матч\s+", "", title, flags=re.I)
    title = re.sub(r"\s+Первенство Московской области.*$", "", title, flags=re.I)

    if " - " in title:
        home, away = [norm(x) for x in title.split(" - ", 1)]
    else:
        names = [norm(x.get_text(" ", strip=True)) for x in soup.select(".team-name-match")]
        names = unique([x for x in names if x])
        if len(names) < 2:
            return None
        home, away = names[:2]

    score = parse_score(soup)
    if score is None:
        logging.warning("Не удалось определить счёт: %s", match_url)
        return None

    # Дата/время берём из meta description или текста страницы.
    dt = ""
    meta = soup.select_one('meta[name="description"]')
    meta_text = norm(meta.get("content", "")) if meta else ""
    m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})", meta_text)
    if not m:
        m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})", page_text)
    if m:
        dt = f"{m.group(1)} {m.group(2)}"

    # Если на странице есть явный final-score с ненулевым счётом,
    # матч считаем завершённым; иначе смотрим период.
    status = parse_status(soup, page_text)
    final_scores = [norm(x.get_text(" ", strip=True)) for x in soup.select(".final-score .team-score")]
    if len(final_scores) >= 2 and all(re.fullmatch(r"\d{1,2}", x) for x in final_scores[:2]):
        if int(final_scores[0]) != 0 or int(final_scores[1]) != 0:
            # Ненулевой scoreboard может быть текущим live-score, поэтому
            # сначала проверяем наличие явного периода.
            if not re.search(r"(?:Первый|Второй|Третий|1|2|3) период", page_text, re.I):
                status = "🟢 Матч завершён"

    return {
        "age": age,
        "group": group_label,
        "home": home,
        "away": away,
        "score": score,
        "date_time": dt,
        "status": status,
        "url": match_url,
    }


def main():
    state = load_state()

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

    current = {}
    khimik_ages = set()

    for age, _label, age_url in ages:
        try:
            groups = discover_group_pages(age_url)
            logging.info("%s: групп %d", age, len(groups))
        except Exception as e:
            logging.warning("Ошибка возраста %s: %s", age, e)
            continue

        for group_label, group_url in groups:
            try:
                all_links, khimik_links, khimik_in_group = discover_match_links(group_url)
                logging.info(
                    "%s %s: матчей %d, матчей Химика %d",
                    age, group_label, len(all_links), len(khimik_links)
                )

                if not khimik_in_group:
                    continue
                khimik_ages.add(age)

                # 2017: вся группа Химика.
                # Остальные возраста: только матчи Химика.
                links_to_parse = all_links if age == "2017" else khimik_links

                for link in links_to_parse:
                    try:
                        match = parse_match(link, age, group_label)
                        if not match:
                            continue

                        # Финальная защита для возрастов 2010–2016.
                        if age != "2017" and "Химик Воскресенск" not in (match["home"], match["away"]):
                            continue

                        current[link] = match
                    except requests.RequestException as e:
                        logging.warning("HTTP ошибка матча %s: %s", link, e)
                    except Exception as e:
                        logging.warning("Ошибка матча %s: %s", link, e)

            except requests.RequestException as e:
                logging.warning("HTTP ошибка группы %s %s: %s", age, group_label, e)
            except Exception as e:
                logging.warning("Ошибка группы %s %s: %s", age, group_label, e)

    logging.info("Возрастов с Химиком: %s", ", ".join(sorted(khimik_ages)) or "не найдено")
    logging.info("Отслеживаемых матчей: %d", len(current))

    for key, match in current.items():
        old = state.get(key)
        if old and old.get("score") != match["score"]:
            telegram(
                f"🏒 Химик Воскресенск {match['age']}\n"
                f"{match['home']} — {match['away']}\n"
                f"{match['score']}\n"
                f"{match['status']}"
            )
            logging.info(
                "ОТПРАВЛЕНО: %s %s — %s: %s → %s",
                match["age"], match["home"], match["away"],
                old.get("score"), match["score"]
            )
        state[key] = match

    save_state(state)


if __name__ == "__main__":
    main()
