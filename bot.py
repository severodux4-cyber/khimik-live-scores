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
s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0 KhimikScoresBot/2.0"})

def tg(text):
    r = s.post(
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

def norm(x):
    return re.sub(r"\s+", " ", x or "").strip()

def age_in(text):
    m = re.search(r"\b(20(?:1[0-7]))\s*г\.?\s*р\.?", text, re.I)
    return m.group(1) if m else None

def links_on(url):
    r = s.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("a[href]"):
        href = urljoin(url, a.get("href"))
        label = norm(a.get_text(" ", strip=True))
        if "cgroup=" in href or "/competitions/com_" in href:
            out.append((label, href))
    seen = set()
    result = []
    for x in out:
        if x[1] not in seen:
            seen.add(x[1])
            result.append(x)
    return result

def score(s):
    m = re.search(r"(?<!\d)(\d{1,2})\s*[:\-]\s*(\d{1,2})(?!\d)", s)
    return f"{m.group(1)}:{m.group(2)}" if m else None

def date_time(s):
    m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})", s)
    return f"{m.group(1)} {m.group(2)}" if m else ""

def parse_group(url, label=""):
    r = s.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    page_text = norm(soup.get_text(" ", strip=True))
    age = age_in(page_text) or age_in(label)

    # The federation page is card/table based. Start from elements containing
    # a score, then walk up a few levels to find the complete match card.
    nodes = []
    for el in soup.find_all(string=re.compile(r"\b\d{1,2}\s*[:\-]\s*\d{1,2}\b")):
        p = el.parent
        for _ in range(4):
            if p is None:
                break
            txt = norm(p.get_text(" ", strip=True))
            if 30 <= len(txt) <= 900:
                nodes.append(txt)
            p = p.parent

    matches = {}
    for txt in nodes:
        sc = score(txt)
        if not sc:
            continue

        # We only need matches involving Khimik for all ages, and ALL matches
        # in the 2017 group. The caller decides the latter.
        dt = date_time(txt)
        if not dt:
            continue

        # Try to recover team names from common page wording.
        clean = re.sub(r"\b\d{1,2}\s*[:\-]\s*\d{1,2}\b", " ", txt)
        clean = re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}\b", " ", clean)
        clean = norm(clean)

        # Split on repeated whitespace is often enough for the two team names;
        # otherwise retain the whole card as a stable identifier.
        chunks = [norm(x) for x in re.split(r"\s{2,}", clean) if norm(x)]
        teams = []
        for c in chunks:
            if 2 < len(c) < 100 and not re.search(r"\b(?:г\.р\.|группа|первенство|юношей)\b", c, re.I):
                if c not in teams:
                    teams.append(c)

        key = f"{url}|{dt}|{sc}|{clean[:300]}"
        matches[key] = {
            "age": age,
            "date_time": dt,
            "score": sc,
            "text": clean[:500],
            "url": url,
        }

    return list(matches.values())

def discover():
    groups = links_on(COMPETITION_URL)
    logging.info("group links: %d", len(groups))
    found = []
    for label, url in groups:
        try:
            age = age_in(label) or age_in(url)
            found.append((age, label, url, parse_group(url, label)))
        except Exception as e:
            logging.warning("failed %s: %s", url, e)
    return found

def is_khimik(m):
    return "Химик" in m["text"]

def main():
    state = load_state()
    first = not state
    groups = discover()

    current = {}
    for age, label, url, matches in groups:
        # 2017: every match in the first group.
        # Other years: only matches containing Khimik.
        is_2017 = age == "2017" and ("первая" in (label or "").lower() or "1" in (label or "").lower())
        for m in matches:
            if is_2017 or is_khimik(m):
                # Remove score from key so score changes compare correctly.
                stable = re.sub(r"\|[^|]+$", "", m["text"])
                key = f"{url}|{m['date_time']}|{stable}"
                current[key] = m

    logging.info("tracked matches: %d", len(current))

    changed = False
    for key, m in current.items():
        old = state.get(key)
        if old and old.get("score") != m["score"]:
            tg(
                f"🏒 Химик / группа {m.get('age') or 'неизвестно'}\n"
                f"{m['text']}\n"
                f"🔴 {old.get('score')} → 🟢 {m['score']}\n"
                f"🕒 {m['date_time']}\n"
                f"🔗 {m['url']}"
            )
        # On first run establish baseline silently.
        state[key] = m
        changed = True

    if changed:
        save_state(state)

if __name__ == "__main__":
    main()
