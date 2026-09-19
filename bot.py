import os
import re
import json
import logging
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Не долбим ФХМО десятками параллельных запросов.
MAX_WORKERS = 4

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 KhimikScoresBot/6.0"
})

retry = Retry(
    total=1,
    connect=1,
    read=1,
    backoff_factor=0.4,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=False,
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
session.mount("https://", adapter)
session.mount("http://", adapter)


def get(url):
    r = session.get(url, timeout=20)
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


def discover_match_links(group_url):
    """
    Получаем ВСЕ ссылки матчей группы.

    Ключевой момент:
    наличие Химика в группе определяем по ТЕКСТУ СТРАНИЦЫ ГРУППЫ.
    Это уже проверенный на ФХМО способ.

    Для конкретного матча команды определяются уже со страницы самого матча.
    """
    soup = BeautifulSoup(get(group_url), "html.parser")
    all_links = []

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = re.search(r"/matches/(m_\d+)/?", href)
        if m:
            full = urljoin(group_url, f"/matches/{m.group(1)}/")
            all_links.append(full)

    all_links = unique(all_links)

    # Именно так определяем группу Химика.
    group_text = norm(soup.get_text(" ", strip=True))
    khimik_in_group = "Химик Воскресенск" in group_text

    return all_links, khimik_in_group


def extract_teams(soup):
    # Самый надёжный источник — title:
    title = norm(soup.title.get_text(" ", strip=True) if soup.title else "")
    title = re.sub(r"^Матч\s+", "", title, flags=re.I)
    title = re.sub(r"\s+Первенство Московской области.*$", "", title, flags=re.I)

    if " - " in title:
        a, b = title.split(" - ", 1)
        if a.strip() and b.strip():
            return norm(a), norm(b)

    # Fallback: team-name-match.
    names = []
    for x in soup.select(".team-name-match"):
        t = norm(x.get_text(" ", strip=True))
        if t and t not in names:
            names.append(t)

    if len(names) >= 2:
        return names[0], names[1]

    # Fallback: alt у логотипов.
    names = []
    for img in soup.select("img[alt]"):
        t = norm(img.get("alt", ""))
        if t and t not in names and len(t) > 2 and "image" not in t.lower():
            names.append(t)

    if len(names) >= 2:
        return names[0], names[1]

    return None, None


def parse_score(soup):
    """
    Реальный scoreboard ФХМО.
    Сначала берём final-score/team-score.
    Если там 0:0, считаем события "Гол".
    """
    scores = []
    for x in soup.select(".final-score .team-score"):
        t = norm(x.get_text(" ", strip=True))
        if re.fullmatch(r"\d{1,2}", t):
            scores.append(int(t))

    if len(scores) >= 2:
        # Ненулевой scoreboard — используем его.
        if scores[0] != 0 or scores[1] != 0:
            return f"{scores[0]}:{scores[1]}"

    def goals(selector):
        n = 0
        for x in soup.select(selector):
            if norm(x.get_text(" ", strip=True)).lower() == "гол":
                n += 1
        return n

    home = goals(".cub-event.team1-event .popup-title")
    away = goals(".cub-event.team2-event .popup-title")

    if home or away:
        return f"{home}:{away}"

    if len(scores) >= 2:
        return f"{scores[0]}:{scores[1]}"

    # Дополнительный fallback для вариантов вёрстки.
    for selector in (".final-score", ".match-score"):
        node = soup.select_one(selector)
        if node:
            text = norm(node.get_text(" ", strip=True))
            m = re.search(r"(?<!\d)(\d{1,2})\s*[:\-]\s*(\d{1,2})(?!\d)", text)
            if m:
                return f"{int(m.group(1))}:{int(m.group(2))}"

    return None


def parse_status(soup, page_text):
    if re.search(r"\bТретий период\b", page_text, re.I):
        return "⏱ 3 период"
    if re.search(r"\bВторой период\b", page_text, re.I):
        return "⏱ 2 период"
    if re.search(r"\b(?:Первый|1) период\b", page_text, re.I):
        return "⏱ 1 период"

    # Если есть финальный счёт и нет признака текущего периода.
    final = soup.select(".final-score .team-score")
    vals = [
        norm(x.get_text(" ", strip=True))
        for x in final
        if re.fullmatch(r"\d{1,2}", norm(x.get_text(" ", strip=True)))
    ]
    if len(vals) >= 2 and not re.search(r"период", page_text, re.I):
        return "🟢 Матч завершён"

    return "⏱ Матч идёт"


def parse_match(url, age, group_label):
    soup = BeautifulSoup(get(url), "html.parser")
    page_text = norm(soup.get_text(" ", strip=True))

    home, away = extract_teams(soup)
    if not home or not away:
        raise ValueError("Не удалось определить команды")

    score = parse_score(soup)
    if score is None:
        raise ValueError("Не удалось определить счёт")

    dt = ""
    meta = soup.select_one('meta[name="description"]')
    meta_text = norm(meta.get("content", "")) if meta else ""

    for text in (meta_text, page_text):
        m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})", text)
        if m:
            dt = f"{m.group(1)} {m.group(2)}"
            break

    return {
        "age": age,
        "group": group_label,
        "home": home,
        "away": away,
        "score": score,
        "date_time": dt,
        "status": parse_status(soup, page_text),
        "url": url,
    }


def parse_many(urls, age, group_label):
    """
    Ограниченный пул из 4 потоков.
    Это существенно быстрее последовательного режима,
    но не создаёт лавину запросов к ФХМО.
    """
    result = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(parse_match, url, age, group_label): url
            for url in urls
        }

        for future in as_completed(futures):
            url = futures[future]
            try:
                match = future.result()
                result.append(match)
                logging.info(
                    "MATCH %s | %s — %s | %s",
                    url.rstrip("/").split("/")[-1],
                    match["home"],
                    match["away"],
                    match["score"],
                )
            except requests.RequestException as e:
                logging.warning("HTTP ошибка %s: %s", url, e)
            except Exception as e:
                logging.warning("Ошибка матча %s: %s", url, e)

    return result


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
        except Exception as e:
            logging.warning("Ошибка возраста %s: %s", age, e)
            continue

        for group_label, group_url in groups:
            try:
                all_links, khimik_group = discover_match_links(group_url)

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

                # 2017: ВСЯ группа.
                # Остальные возраста: сначала получаем команды матчей,
                # затем оставляем только Химик.
                matches = parse_many(all_links, age, group_label)

                for match in matches:
                    is_khimik = (
                        norm(match["home"]).lower() == "химик воскресенск"
                        or norm(match["away"]).lower() == "химик воскресенск"
                    )

                    if age == "2017":
                        # Для 2017 оставляем весь найденный group.
                        current[match["url"]] = match
                    elif is_khimik:
                        current[match["url"]] = match

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

    # Уведомление только при изменении счёта.
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
