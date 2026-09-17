import os
import re
import json
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

COMPETITION_URL = os.getenv(
    "COMPETITION_URL",
    "https://фхмо.рф/competitions/2026-2027/pervenstvo-moskovskoy-oblasti-po-khokkeyu-sredi-yunoshey-2026-2027/"
)
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
STATE_FILE = Path("state.json")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 KhimikScoresBot/3.0"
})

def get(url):
    r = session.get(url, timeout=30)
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
    # One page per age
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
    soup = BeautifulSoup(get(group_url), "html.parser")
    links = []
    for a in soup.select('a[href*="/matches/m_"]'):
        href = urljoin(group_url, a["href"]).split("?")[0]
        links.append(href)
    return unique(links)

def parse_match(match_url, age, group_label):
    soup = BeautifulSoup(get(match_url), "html.parser")
    page_text = norm(soup.get_text(" ", strip=True))

    # The match title is the most reliable source for team names.
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
        # Fallback: team headings in the match page.
        names = []
        for tag in soup.find_all(["h1", "h2", "h3"]):
            t = norm(tag.get_text(" ", strip=True))
            if t and t not in names:
                names.append(t)
        if len(names) >= 2:
            home, away = names[0], names[1]
        else:
            return None

    # Score is represented in the event feed as cumulative scores after goals.
    # The last score pair is therefore the current/final score.
    score_candidates = re.findall(r"(?<!\d)(\d{1,2}):(\d{1,2})(?!\d)", page_text)
    # Match pages also contain clock values such as 45:00 and 00:00.
    # A hockey score is normally far below the game clock, so exclude
    # clock-like values with a first component above 30.
    scores = [(int(a), int(b)) for a, b in score_candidates if int(a) <= 30 and int(b) <= 30]
    if not scores:
        return None

    current_score = f"{scores[-1][0]}:{scores[-1][1]}"

    # Extract scheduled date/time from the match page text.
    dt = ""
    m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})", page_text)
    if m:
        dt = f"{m.group(1)} {m.group(2)}"

    # Current status: finished games expose a 45:00 (or longer OT) endpoint.
    clocks = re.findall(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", page_text)
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
    first_run = not bool(state)

    ages = discover_age_pages()
    logging.info("Найдено возрастов: %d", len(ages))

    all_groups = []
    for age, label, age_url in ages:
        groups = discover_group_pages(age_url)
        logging.info("%s: групп %d", age, len(groups))
        for group_label, group_url in groups:
            all_groups.append((age, group_label, group_url))

    # Find which 2017 group actually contains Khimik.
    khimik_2017_groups = set()
    group_matches = {}

    for age, group_label, group_url in all_groups:
        try:
            links = discover_match_links(group_url)
            group_matches[(age, group_label, group_url)] = links
            if age == "2017":
                for link in links:
                    try:
                        html = get(link)
                        if "Химик Воскресенск" in norm(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)):
                            khimik_2017_groups.add(group_url)
                            break
                    except Exception as e:
                        logging.warning("Не удалось проверить 2017 матч %s: %s", link, e)
            logging.info("%s %s: матчей/ссылок %d", age, group_label, len(links))
        except Exception as e:
            logging.warning("Ошибка группы %s %s: %s", age, group_label, e)

    logging.info("Групп 2017 с Химиком: %d", len(khimik_2017_groups))

    current = {}
    for (age, group_label, group_url), links in group_matches.items():
        track_all = (age == "2017" and group_url in khimik_2017_groups)

        for link in links:
            try:
                match = parse_match(link, age, group_label)
                if not match:
                    continue

                if track_all or "Химик Воскресенск" in (match["home"], match["away"]):
                    # Match URL is the stable unique identifier.
                    current[link] = match
            except Exception as e:
                logging.warning("Ошибка матча %s: %s", link, e)

    logging.info("Отслеживаемых матчей: %d", len(current))

    for key, match in current.items():
        old = state.get(key)

        # If the match has no score yet, do not send anything, but remember it.
        if old and old.get("score") != match["score"]:
            telegram(
                f"🏒 {match['age']} г.р. — {match['group']}\n"
                f"{match['home']} — {match['away']}\n"
                f"🔴 {old.get('score')} → 🟢 {match['score']}\n"
                f"{match['status']}\n"
                f"🕒 {match['date_time']}\n"
                f"🔗 {match['url']}"
            )

        state[key] = match

    save_state(state)

if __name__ == "__main__":
    main()
