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
USERS_FILE = Path("users.json")
TEST_NOTIFY = os.getenv("TEST_NOTIFY", "").lower() in ("1", "true", "yes", "on")

MOSCOW = ZoneInfo("Europe/Moscow")
REQUEST_DELAY = 0.65
STATE_VERSION = 3
INITIAL_NOTIFY_DONE = False
# После начала матча считаем его завершённым, если страница не даёт
# признаков текущей игры и прошло достаточно времени для полного матча.
MATCH_DURATION_GRACE_MINUTES = 120

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


def telegram_to(chat_id, text):
    r = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": str(chat_id), "text": text},
        timeout=15,
    )
    r.raise_for_status()


def load_users():
    data = {"offset": 0, "users": {}}

    if USERS_FILE.exists():
        try:
            raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data["offset"] = int(raw.get("offset", 0))
                data["users"] = raw.get("users", {}) if isinstance(raw.get("users", {}), dict) else {}
        except Exception as e:
            logging.warning("USERS: не удалось прочитать users.json: %s", e)

    # Владелец бота всегда получает уведомления.
    data["users"][str(CHAT_ID)] = {"active": True, "admin": True}
    return data


def save_users(data):
    USERS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def process_telegram_commands(users):
    """Обрабатывает команды Telegram при очередном запуске GitHub Actions."""
    offset = int(users.get("offset", 0))

    try:
        r = session.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={
                "offset": offset,
                "timeout": 0,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logging.warning("TELEGRAM UPDATES: ошибка получения команд: %s", e)
        return

    if not payload.get("ok"):
        logging.warning("TELEGRAM UPDATES: API вернул ошибку: %s", payload)
        return

    for update in payload.get("result", []):
        update_id = update.get("update_id")
        if update_id is not None:
            users["offset"] = max(int(users.get("offset", 0)), int(update_id) + 1)

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue

        raw_command = (message.get("text") or "").strip()
        if not raw_command:
            continue

        command = raw_command.split()[0].lower().split("@")[0]

        if command == "/start":
            users["users"][str(chat_id)] = {"active": True}
            try:
                telegram_to(
                    chat_id,
                    "🏒 Химик Live Scores\n\n"
                    "🔔 Уведомления включены.\n"
                    "Я буду присылать изменения счёта матчей Химика."
                )
                logging.info("USERS: подключён chat_id=%s", chat_id)
            except Exception as e:
                logging.warning("USERS: не удалось отправить приветствие %s: %s", chat_id, e)

        elif command == "/stop":
            existing = users["users"].get(str(chat_id), {})
            existing["active"] = False
            users["users"][str(chat_id)] = existing
            try:
                telegram_to(
                    chat_id,
                    "🔕 Уведомления отключены.\n"
                    "Для повторного включения отправьте /start."
                )
                logging.info("USERS: отключён chat_id=%s", chat_id)
            except Exception as e:
                logging.warning("USERS: не удалось отправить /stop %s: %s", chat_id, e)

        elif command == "/help":
            try:
                telegram_to(
                    chat_id,
                    "🏒 Химик Live Scores\n\n"
                    "/start — включить уведомления\n"
                    "/stop — отключить уведомления\n"
                    "/help — помощь"
                )
            except Exception as e:
                logging.warning("USERS: не удалось отправить /help %s: %s", chat_id, e)

    save_users(users)


def telegram(text):
    # Старое имя оставляем для тестовой отправки администратору.
    telegram_to(CHAT_ID, text)


def broadcast(text, users):
    sent = 0

    for chat_id, info in list(users.get("users", {}).items()):
        if not isinstance(info, dict) or not info.get("active", False):
            continue

        try:
            telegram_to(chat_id, text)
            sent += 1
            time.sleep(0.15)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                info["active"] = False
                logging.warning(
                    "BROADCAST: chat_id=%s недоступен (403), отключаем пользователя",
                    chat_id,
                )
            else:
                logging.warning("BROADCAST: ошибка chat_id=%s: %s", chat_id, e)
        except Exception as e:
            logging.warning("BROADCAST: ошибка chat_id=%s: %s", chat_id, e)

    logging.info("BROADCAST: отправлено пользователям: %d", sent)


def load_state():
    global INITIAL_NOTIFY_DONE
    INITIAL_NOTIFY_DONE = False
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        meta = data.get("_meta", {})
        # State v2 уже содержит актуальный baseline, но не содержит флага
        # первой рассылки. Поэтому именно один раз отправляем все уже
        # сыгранные/идущие результаты, а затем переходим в обычный режим.
        if meta.get("version") not in (STATE_VERSION, 2):
            logging.info("STATE: старый формат, создаём новый baseline")
            return {}
        INITIAL_NOTIFY_DONE = bool(meta.get("initial_notify_done", False))
        return {k: v for k, v in data.items() if k != "_meta"}
    except Exception:
        return {}


def save_state(state):
    data = {
        "_meta": {
            "version": STATE_VERSION,
            "initial_notify_done": INITIAL_NOTIFY_DONE,
        },
        **state,
    }
    STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
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


def is_suspicious_update(old, new):
    """
    Защищает state.json от временных/неполных данных ФХМО.

    Подозрительными считаем:
      - уменьшение счёта одной из команд;
      - возврат ненулевого счёта в 0:0;
      - возврат завершённого матча обратно в live/другой статус.

    Нормальные изменения (например 4:0 -> 5:0 или 4:0 -> 4:1)
    пропускаются.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False

    old_score = old.get("score", "")
    new_score = new.get("score", "")

    try:
        old_home, old_away = map(int, old_score.split(":"))
        new_home, new_away = map(int, new_score.split(":"))
    except (ValueError, AttributeError):
        return False

    # Счёт не может уменьшиться в обычном ходе матча.
    if new_home < old_home or new_away < old_away:
        return True

    # Сайт не должен возвращать уже ненулевой счёт к 0:0.
    if (old_home > 0 or old_away > 0) and new_home == 0 and new_away == 0:
        return True

    old_status = old.get("status", "")
    new_status = new.get("status", "")

    # Уже завершённый матч не должен снова становиться текущим.
    if old_status == "🏁 Матч завершён" and new_status.startswith("⏱"):
        return True

    return False


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
    """Определяет статус без ложного '3 период' из названий вкладок.

    На странице ФХМО текст '3 период' может присутствовать просто как
    название вкладки ленты, даже после окончания матча. Поэтому сначала
    смотрим на фактические события игры, а затем на время от начала.
    """
    now = datetime.now(MOSCOW)

    if scheduled_dt and scheduled_dt > now:
        return "⏳ Матч не начался"

    # Берём период из фактических событий, а НЕ из текста кнопок-вкладок.
    event_periods = []
    for node in soup.select(".feed-period-name"):
        value = norm(node.get_text(" ", strip=True)).lower()
        if value in ("1 период", "2 период", "3 период"):
            event_periods.append(value)

    latest_period = event_periods[-1] if event_periods else None

    # Если матч начался недавно, считаем его идущим.
    # Это покрывает, например, матч 13:15 в момент 14:04.
    if scheduled_dt:
        elapsed = (now - scheduled_dt).total_seconds() / 60
        if elapsed < MATCH_DURATION_GRACE_MINUTES:
            if latest_period == "3 период":
                return "⏱ 3 период"
            if latest_period == "2 период":
                return "⏱ 2 период"
            if latest_period == "1 период":
                return "⏱ 1 период"
            return "⏱ Матч идёт"

        # После двух часов после стартового времени при наличии результата
        # считаем матч завершённым. В отличие от старой версии, наличие
        # текста '3 период' в вкладке больше не мешает этому.
        return "🏁 Матч завершён"

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
    global INITIAL_NOTIFY_DONE

    state = load_state()
    users = load_users()
    process_telegram_commands(users)

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

                        old_match = state.get(link)
                        if old_match and is_suspicious_update(old_match, match):
                            logging.warning(
                                "STATE: подозрительный откат %s | %s → %s | %s → %s — сохраняем старое состояние",
                                link.rstrip("/").split("/")[-1],
                                old_match.get("score"),
                                match.get("score"),
                                old_match.get("status"),
                                match.get("status"),
                            )
                            current[link] = old_match
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

    # Первый запуск после установки v5: отправляем ВСЕ уже сыгранные
    # и текущие матчи. Будущие матчи не отправляем. После успешной
    # рассылки включается обычный режим "только изменение счёта".
    changes = []
    if not INITIAL_NOTIFY_DONE:
        logging.info("INITIAL_NOTIFY: отправляем все уже сыгранные и текущие результаты")
        for key, match in current.items():
            if match.get("status") == "⏳ Матч не начался":
                continue
            changes.append((key, {"score": "—"}, match, "initial"))
    else:
        for key, match in current.items():
            old = state.get(key)
            if not old:
                continue

            old_score = old.get("score")
            new_score = match.get("score")
            old_status = old.get("status", "")
            new_status = match.get("status", "")

            if old_score != new_score:
                event = "score"
                try:
                    oh, oa = map(int, old_score.split(":"))
                    nh, na = map(int, new_score.split(":"))
                    if nh == oh + 1 and na == oa:
                        event = "home_goal"
                    elif na == oa + 1 and nh == oh:
                        event = "away_goal"
                except Exception:
                    pass
                changes.append((key, old, match, event))

            elif old_status == "⏳ Матч не начался" and new_status.startswith("⏱"):
                changes.append((key, old, match, "start"))

            elif old_status.startswith("⏱") and new_status == "🏁 Матч завершён":
                changes.append((key, old, match, "finish"))

    def sort_key(row):
        _key, _old, match, _event = row
        try:
            age_key = int(match["age"])
        except Exception:
            age_key = 9999
        try:
            dt_key = datetime.strptime(
                match.get("date_time", "31.12.9999 23:59"),
                "%d.%m.%Y %H:%M",
            )
        except Exception:
            dt_key = datetime.max
        return age_key, dt_key

    initial_mode = not INITIAL_NOTIFY_DONE

    for key, old, match, event in sorted(changes, key=sort_key):
        if event == "home_goal":
            if norm(match["home"]).lower() == "химик воскресенск":
                header = "🥅 ГОООЛ ХИМИКА!"
            else:
                header = f"🥅 ГОЛ — {match['home']}"

        elif event == "away_goal":
            if norm(match["away"]).lower() == "химик воскресенск":
                header = "🥅 ГОООЛ ХИМИКА!"
            else:
                header = f"🥅 ГОЛ — {match['away']}"

        elif event == "start":
            header = "🟢 МАТЧ НАЧАЛСЯ"

        elif event == "finish":
            header = "🏁 МАТЧ ЗАВЕРШЁН"

        else:
            header = None

        body = (
            f"🏒 Химик Воскресенск {match['age']}\n"
            f"🕒 {match['date_time']}\n"
            f"{match['home']} — {match['away']}\n"
            f"🥅 {match['score']}\n"
            f"{match['status']}"
        )
        message = f"{header}\n\n{body}" if header else body

        if initial_mode:
            # Исторический backfill получает только владелец бота.
            telegram(message)
        else:
            # После первичной рассылки уведомления получают все активные подписчики.
            broadcast(message, users)

        logging.info(
            "ОТПРАВЛЕНО: %s %s — %s: %s → %s (%s)",
            match["age"],
            match["home"],
            match["away"],
            old.get("score"),
            match["score"],
            event,
        )

    # State обновляем после успешной отправки всех сообщений.
    for key, match in current.items():
        state[key] = match

    if not INITIAL_NOTIFY_DONE:
        INITIAL_NOTIFY_DONE = True
        logging.info("INITIAL_NOTIFY: первая рассылка завершена, дальше только изменения счёта")

    save_state(state)
    save_users(users)


if __name__ == "__main__":
    main()
