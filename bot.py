import os
import re
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
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

MOSCOW = ZoneInfo("Europe/Moscow")
REQUEST_DELAY = 0.55

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 KhimikScoresBot/7.0"
})

retry = Retry(
    total=1,
    connect=1,
    read=1,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=False,
)
session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2))

_last_request = 0.0


def get(url):
    global _last_request
    wait = REQUEST_DELAY - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)

    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        return r.text
    finally:
        _last_request = time.monotonic()


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


def unique(seq):
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def age_from(text):
    m = re.search(r"\b(20(?:1[0-7]))\s*г\.?\s*р\.?", text or "", re.I)
    return m.group(1) if m else None


def discover_age_pages():
    soup = BeautifulSoup(get(COMPETITION_URL), "html.parser")
    by_age = {}

    for a in soup.select("a[href]"):
        label = norm(a.get_text(" ", strip=True))
        href = urljoin(COMPETITION_URL, a["href"]).split("#")[0]
        age = age_from(label)
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


def discover_group(group_url):
    """
    Один запрос к группе.

    Возвращаем:
      - все матчи группы;
      - есть ли Химик в группе;
      - ссылку на страницу команды Химика, если она есть.
    """
    soup = BeautifulSoup(get(group_url), "html.parser")

    all_links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = re.search(r"/matches/(m_\d+)/?", href)
        if m:
            all_links.append(
                urljoin(group_url, f"/matches/{m.group(1)}/")
            )

    group_text = norm(soup.get_text(" ", strip=True))
    khimik_in_group = "Химик Воскресенск" in group_text

    team_url = None
    if khimik_in_group:
        for a in soup.select('a[href*="/teams/"]'):
            text = norm(a.get_text(" ", strip=True))
            alt_text = " ".join(
                norm(img.get("alt", ""))
                for img in a.select("img[alt]")
            )
            combined = f"{text} {alt_text}"
            if "Химик Воскресенск" in combined:
                team_url = urljoin(group_url, a["href"])
                break

    return unique(all_links), khimik_in_group, team_url


def discover_team_matches(team_url):
    """Страница команды содержит только матчи Химика."""
    soup = BeautifulSoup(get(team_url), "html.parser")
    links = []

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = re.search(r"/matches/(m_\d+)/?", href)
        if m:
            links.append(urljoin(team_url, f"/matches/{m.group(1)}/"))

    return unique(links)


def extract_teams(soup):
    title = norm(soup.title.get_text(" ", strip=True) if soup.title else "")
    title = re.sub(r"^Матч\s+", "", title, flags=re.I)
    title = re.sub(r"\s+Первенство Московской области.*$", "", title, flags=re.I)

    if " - " in title:
        a, b = title.split(" - ", 1)
        if norm(a) and norm(b):
            return norm(a), norm(b)

    names = []
    for x in soup.select(".team-name-match"):
        t = norm(x.get_text(" ", strip=True))
        if t and t not in names:
            names.append(t)

    if len(names) >= 2:
        return names[0], names[1]

    return None, None


def parse_score(soup):
    # 1. Нормальный scoreboard.
    scores = []
    for x in soup.select(".final-score .team-score"):
        t = norm(x.get_text(" ", strip=True))
        if re.fullmatch(r"\d{1,2}", t):
            scores.append(int(t))

    if len(scores) >= 2 and (scores[0] != 0 or scores[1] != 0):
        return f"{scores[0]}:{scores[1]}"

    # 2. Live: scoreboard может быть 0:0, а голы уже есть в событиях.
    def goal_count(selector):
        return sum(
            1
            for x in soup.select(selector)
            if norm(x.get_text(" ", strip=True)).lower() == "гол"
        )

    home = goal_count(".cub-event.team1-event .popup-title")
    away = goal_count(".cub-event.team2-event .popup-title")

    if home or away:
        return f"{home}:{away}"

    if len(scores) >= 2:
        return f"{scores[0]}:{scores[1]}"

    # 3. Запасной вариант.
    for selector in (".final-score", ".match-score"):
        node = soup.select_one(selector)
        if node:
            m = re.search(
                r"(?<!\d)(\d{1,2})\s*:\s*(\d{1,2})(?!\d)",
                norm(node.get_text(" ", strip=True)),
            )
            if m:
                return f"{int(m.group(1))}:{int(m.group(2))}"

    return None


def parse_datetime(soup, page_text):
    candidates = []

    meta = soup.select_one('meta[name="description"]')
    if meta:
        candidates.append(norm(meta.get("content", "")))

    candidates.append(page_text)

    for text in candidates:
        m = re.search(
            r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})",
            text,
        )
        if m:
            raw = f"{m.group(1)} {m.group(2)}"
            try:
                dt = datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(
                    tzinfo=MOSCOW
                )
                return raw, dt
            except ValueError:
                pass

    return "", None


def parse_status(soup, page_text, scheduled_dt):
    now = datetime.now(MOSCOW)

    # Самое важное: будущий матч НИКОГДА не может быть "завершён".
    if scheduled_dt and scheduled_dt > now:
        return "⏳ Матч не начался"

    # Явно указан текущий период.
    if re.search(r"\bТретий период\b", page_text, re.I):
        return "⏱ 3 период"
    if re.search(r"\bВторой период\b", page_text, re.I):
        return "⏱ 2 период"
    if re.search(r"\b(?:Первый|1) период\b", page_text, re.I):
        return "⏱ 1 период"

    # Если счёт уже ненулевой и период на странице не указан,
    # НЕ называем матч завершённым автоматически.
    # Иначе 0:0 будущих матчей снова будут ошибочно завершены.
    if scheduled_dt and scheduled_dt <= now:
        return "⏱ Матч идёт / статус ФХМО не указан"

    return "ℹ️ Статус не определён"


def parse_match(url, age, group_label):
    soup = BeautifulSoup(get(url), "html.parser")
    page_text = norm(soup.get_text(" ", strip=True))

    home, away = extract_teams(soup)
    if not home or not away:
        raise ValueError("Не удалось определить команды")

    score = parse_score(soup)
    if score is None:
        raise ValueError("Не удалось определить счёт")

    date_time, scheduled_dt = parse_datetime(soup, page_text)

    return {
        "age": age,
        "group": group_label,
        "home": home,
        "away": away,
        "score": score,
        "date_time": date_time,
        "status": parse_status(soup, page_text, scheduled_dt),
        "url": url,
    }


def main():
    state = load_state()

    if TEST_NOTIFY:
        telegram(
            "🏒 ТЕСТ Khimik Live Scores\n"
            "Telegram-уведомления работают корректно.\n"
            "state.json не изменён."
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
        except requests.RequestException as e:
            logging.warning("Ошибка возраста %s: %s", age, e)
            continue

        for group_label, group_url in groups:
            try:
                all_links, khimik_group, team_url = discover_group(group_url)

                logging.info(
                    "%s %s: матчей %d, группа Химика=%s",
                    age,
                    group_label,
                    len(all_links),
                    khimik_group,
                )

                if not khimik_group:
                    continue

                khimik_ages.add(age)

                # Для 2010–2016 используем страницу команды:
                # она содержит только матчи Химика и резко уменьшает
                # количество обращений к страницам отдельных матчей.
                if age != "2017" and team_url:
                    links = discover_team_matches(team_url)
                    logging.info(
                        "%s %s: матчей Химика по странице команды %d",
                        age, group_label, len(links)
                    )
                else:
                    # Для 2017 — ВСЯ группа.
                    links = all_links

                # Для одного запуска не делаем параллельных запросов.
                # ФХМО отвечает 503 при агрессивном параллелизме.
                for link in links:
                    try:
                        match = parse_match(link, age, group_label)

                        if age != "2017":
                            if (
                                norm(match["home"]).lower() != "химик воскресенск"
                                and norm(match["away"]).lower() != "химик воскресенск"
                            ):
                                continue

                        current[link] = match

                        logging.info(
                            "MATCH %s | %s — %s | %s | %s | %s",
                            link.rstrip("/").split("/")[-1],
                            match["home"],
                            match["away"],
                            match["score"],
                            match["date_time"],
                            match["status"],
                        )

                    except requests.RequestException as e:
                        logging.warning("HTTP ошибка %s: %s", link, e)
                    except Exception as e:
                        logging.warning("Ошибка матча %s: %s", link, e)

            except requests.RequestException as e:
                logging.warning(
                    "HTTP ошибка группы %s %s: %s",
                    age, group_label, e
                )
            except Exception as e:
                logging.warning(
                    "Ошибка группы %s %s: %s",
                    age, group_label, e
                )

    logging.info(
        "Возрастов с Химиком: %s",
        ", ".join(sorted(khimik_ages)) if khimik_ages else "не найдено"
    )
    logging.info("Отслеживаемых матчей: %d", len(current))

    for key, match in current.items():
        old = state.get(key)

        if old and old.get("score") != match["score"]:
            telegram(
                f"🏒 Химик Воскресенск {match['age']}\n"
                f"📅 {match['date_time']}\n"
                f"{match['home']} — {match['away']}\n"
                f"🥅 {match['score']}\n"
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

        state[key] = match

    save_state(state)


if __name__ == "__main__":
    main()
