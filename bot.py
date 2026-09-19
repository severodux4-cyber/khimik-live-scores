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
from urllib3.util import Retry

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

retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=3,
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
    wait = 2.0 - (time.monotonic() - last_get)
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
    """Возвращает ВСЕ матчи группы.

    Для возрастов 2010-2016 принадлежность матча Химику определяется
    только по конкретной странице матча. Никаких попыток искать
    "Химик" в родительском DOM-блоке группы.
    """
    soup = BeautifulSoup(get(group_url), "html.parser")
    links = []

    for a in soup.select('a[href*="/matches/m_"]'):
        href = urljoin(group_url, a["href"]).split("?")[0]
        links.append(href)

    return unique(links)


def parse_team_names(soup):
    teams = soup.select(".match-score .team-name-match")
    names = [norm(x.get_text(" ", strip=True)) for x in teams]
    names = [x for x in names if x]

    if len(names) >= 2:
        return names[0], names[1]

    # Надёжный fallback: alt логотипов внутри match-score.
    imgs = soup.select(".match-score img[alt]")
    alts = [norm(x.get("alt", "")) for x in imgs]
    alts = [x for x in alts if x]

    if len(alts) >= 2:
        return alts[0], alts[1]

    return None, None


def parse_score(soup):
    """Читает счёт из реального scoreboard ФХМО.

    На сайте scoreboard находится в:
      .match-score .final-score .team-score

    Если scoreboard ещё показывает 0:0, но в ленте уже есть голы,
    используем количество событий 'Гол' по сторонам. Это важно для
    live-матчей: в предоставленном HTML scoreboard был 0:0, тогда как
    лента уже содержала голевые события.
    """
    score_nodes = soup.select(".match-score .final-score .team-score")
    if len(score_nodes) >= 2:
        try:
            home = int(norm(score_nodes[0].get_text(" ", strip=True)))
            away = int(norm(score_nodes[1].get_text(" ", strip=True)))
            if 0 <= home <= 99 and 0 <= away <= 99:
                scoreboard = (home, away)
            else:
                scoreboard = None
        except ValueError:
            scoreboard = None
    else:
        scoreboard = None

    goal_home = 0
    goal_away = 0

    for event in soup.select(".cub-event.team1-event, .cub-event.team2-event"):
        title = norm(
            event.select_one(".popup-title").get_text(" ", strip=True)
            if event.select_one(".popup-title") else ""
        )
        if title.lower() == "гол":
            if "team1-event" in event.get("class", []):
                goal_home += 1
            elif "team2-event" in event.get("class", []):
                goal_away += 1

    # Если scoreboard не 0:0 — это основной источник.
    # Если 0:0, но лента уже содержит голы — scoreboard на странице
    # ещё не обновился, поэтому используем ленту.
    if scoreboard is not None:
        if scoreboard != (0, 0) or (goal_home == 0 and goal_away == 0):
            return f"{scoreboard[0]}:{scoreboard[1]}"

    if goal_home or goal_away:
        return f"{goal_home}:{goal_away}"

    if scoreboard is not None:
        return f"{scoreboard[0]}:{scoreboard[1]}"

    return None


def parse_status(soup):
    page_text = norm(soup.get_text(" ", strip=True))

    periods = [
        ("Завершение игры", "🟢 Матч завершён"),
        ("Матч завершен", "🟢 Матч завершён"),
        ("Матч завершён", "🟢 Матч завершён"),
        ("3 период", "⏱ 3 период"),
        ("3-й период", "⏱ 3 период"),
        ("Третий период", "⏱ 3 период"),
        ("2 период", "⏱ 2 период"),
        ("2-й период", "⏱ 2 период"),
        ("Второй период", "⏱ 2 период"),
        ("1 период", "⏱ 1 период"),
        ("1-й период", "⏱ 1 период"),
        ("Первый период", "⏱ 1 период"),
    ]

    for needle, status in periods:
        if needle.lower() in page_text.lower():
            return status

    # Для уже завершённого матча часто есть итоговый счёт и нет
    # активного периода. В таком случае не придумываем период.
    return "⏱ Матч идёт"


def parse_date_time(soup):
    meta = soup.select_one('meta[name="description"]')
    if meta and meta.get("content"):
        m = re.search(
            r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})",
            meta["content"],
        )
        if m:
            return f"{m.group(1)} {m.group(2)}"

    page_text = norm(soup.get_text(" ", strip=True))
    m = re.search(
        r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})",
        page_text,
    )
    return f"{m.group(1)} {m.group(2)}" if m else ""


def parse_match(match_url, age, group_label):
    soup = BeautifulSoup(get(match_url), "html.parser")

    home, away = parse_team_names(soup)
    if not home or not away:
        logging.warning("Не удалось определить команды: %s", match_url)
        return None

    score = parse_score(soup)
    if score is None:
        logging.warning("Не удалось определить счёт: %s", match_url)
        return None

    return {
        "age": age,
        "group": group_label,
        "home": home,
        "away": away,
        "score": score,
        "date_time": parse_date_time(soup),
        "status": parse_status(soup),
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
    khimik_ages = set()

    for age, group_label, group_url in all_groups:
        try:
            all_links = discover_match_links(group_url)
            logging.info(
                "%s %s: найдено матчей %d",
                age, group_label, len(all_links)
            )

            # Для 2017 нужна вся группа, где находится Химик.
            # Для остальных возрастов фильтруем по командам на самой
            # странице каждого конкретного матча.
            for link in all_links:
                try:
                    match = parse_match(link, age, group_label)
                    if not match:
                        continue

                    is_khimik = (
                        match["home"] == "Химик Воскресенск"
                        or match["away"] == "Химик Воскресенск"
                    )

                    if is_khimik:
                        khimik_ages.add(age)

                    if age == "2017":
                        # 2017: вся группа Химика, поэтому сюда попадут
                        # все матчи выбранной группы. Группу определяем ниже.
                        pass
                    elif not is_khimik:
                        continue

                    current[link] = match

                except Exception as e:
                    logging.warning("Ошибка матча %s: %s", link, e)

        except Exception as e:
            logging.warning(
                "Ошибка группы %s %s: %s",
                age, group_label, e
            )

    # 2017 нужно ограничить именно группой, где есть Химик.
    # Повторно групповые страницы не скачиваем: уже определяем группу
    # по наличию матча Химика среди успешно разобранных матчей.
    # Если вторая группа не содержит Химик, она автоматически отпадёт.
    # Поэтому текущий проход оставляем только для групп, где найден Химик.
    if "2017" in khimik_ages:
        filtered = {}
        for key, match in current.items():
            if match["age"] == "2017":
                filtered[key] = match

        # В current сейчас присутствуют все 2017-группы. Чтобы не
        # отслеживать вторую группу, оставляем группу(ы), где найден Химик.
        khimik_groups_2017 = {
            (m["group"],)
            for m in current.values()
            if m["age"] == "2017" and (
                m["home"] == "Химик Воскресенск"
                or m["away"] == "Химик Воскресенск"
            )
        }
        group_names = {x[0] for x in khimik_groups_2017}

        for key, match in list(filtered.items()):
            if match["group"] not in group_names:
                current.pop(key, None)

    logging.info(
        "Возрастов с Химиком: %s",
        ", ".join(sorted(khimik_ages)) if khimik_ages else "не найдено",
    )
    logging.info("Отслеживаемых матчей: %d", len(current))

    changed = False

    for key, match in current.items():
        old = state.get(key)

        if old and old.get("score") != match["score"]:
            try:
                telegram(
                    f"🏒 Химик Воскресенск {match['age']}\n"
                    f"{match['home']} — {match['away']}\n"
                    f"{match['score']}\n"
                    f"{match['status']}"
                )
            except Exception as e:
                # Не обновляем state, если Telegram не принял сообщение.
                logging.error("Ошибка Telegram для %s: %s", key, e)
                continue

            logging.info(
                "ОТПРАВЛЕНО: %s %s — %s: %s → %s",
                match["age"],
                match["home"],
                match["away"],
                old.get("score"),
                match["score"],
            )

        state[key] = match
        changed = True

    if changed:
        save_state(state)
    else:
        logging.info("State unchanged")


if __name__ == "__main__":
    main()
