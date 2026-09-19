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
STATE_BACKUP_FILE = Path("state.backup.json")
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

    def read_state(path):
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = data.get("_meta", {})
        # State v2 уже содержит актуальный baseline, но не содержит флага
        # первой рассылки. Поэтому именно один раз отправляем все уже
        # сыгранные/идущие результаты, а затем переходим в обычный режим.
        if meta.get("version") not in (STATE_VERSION, 2):
            raise ValueError("неподдерживаемая версия state")
        return bool(meta.get("initial_notify_done", False)), {
            k: v for k, v in data.items() if k != "_meta"
        }

    if STATE_FILE.exists():
        try:
            INITIAL_NOTIFY_DONE, state = read_state(STATE_FILE)
            return state
        except Exception as e:
            logging.warning(
                "STATE: не удалось прочитать state.json: %s — пробуем резервную копию",
                e,
            )

    if STATE_BACKUP_FILE.exists():
        try:
            INITIAL_NOTIFY_DONE, state = read_state(STATE_BACKUP_FILE)
            logging.warning("STATE: восстановлено из state.backup.json")
            return state
        except Exception as e:
            logging.warning(
                "STATE: не удалось прочитать state.backup.json: %s",
                e,
            )

    return {}


def save_state(state):
    data = {
        "_meta": {
            "version": STATE_VERSION,
            "initial_notify_done": INITIAL_NOTIFY_DONE,
        },
        **state,
    }
    serialized = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)

    # Сначала сохраняем последнее корректное состояние в резервную копию.
    if STATE_FILE.exists():
        try:
            STATE_BACKUP_FILE.write_text(
                STATE_FILE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        except Exception as e:
            logging.warning(
                "STATE: не удалось обновить state.backup.json: %s",
                e,
            )

    STATE_FILE.write_text(serialized, encoding="utf-8")


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


def is_incomplete_response(state, current, khimik_ages):
    """Не даёт частичному ответу ФХМО перезаписать нормальное состояние."""
    if not state:
        return False

    previous_matches = [
        value for value in state.values()
        if isinstance(value, dict) and value.get("age")
    ]
    if not previous_matches:
        return False

    previous_ages = {str(value.get("age")) for value in previous_matches}
    current_ages = {str(age) for age in khimik_ages}

    # Если возраст, который раньше стабильно отслеживался, внезапно
    # полностью исчез — считаем ответ сайта неполным.
    missing_ages = sorted(previous_ages - current_ages)
    if missing_ages:
        logging.warning(
            "ФХМО: неполный ответ — исчезли возраста: %s; state не обновляем",
            ", ".join(missing_ages),
        )
        return True

    previous_count = len(previous_matches)
    current_count = len(current)

    # Резкое уменьшение количества матчей обычно означает неполную
    # выдачу сайта, а не реальное исчезновение матчей.
    minimum_count = max(1, int(previous_count * 0.70))
    if current_count < minimum_count:
        logging.warning(
            "ФХМО: неполный ответ — матчей %d вместо минимум %d из %d; state не обновляем",
            current_count,
            minimum_count,
            previous_count,
        )
        return True

    # Дополнительная защита: если по конкретному возрасту пропала большая
    # часть матчей, не принимаем такой ответ за нормальное состояние.
    previous_by_age = {}
    current_by_age = {}
    for value in previous_matches:
        age = str(value.get("age"))
        previous_by_age[age] = previous_by_age.get(age, 0) + 1
    for value in current.values():
        if isinstance(value, dict) and value.get("age"):
            age = str(value.get("age"))
            current_by_age[age] = current_by_age.get(age, 0) + 1

    for age, previous_age_count in previous_by_age.items():
        if previous_age_count >= 4:
            current_age_count = current_by_age.get(age, 0)
            if current_age_count < int(previous_age_count * 0.50):
                logging.warning(
                    "ФХМО: неполный ответ — возраст %s: матчей %d вместо %d; state не обновляем",
                    age,
                    current_age_count,
                    previous_age_count,
                )
                return True

    return False



def event_id(match, event):
    """Уникальный идентификатор уведомления для конкретного матча."""
    match_id = match.get("url", "").rstrip("/").split("/")[-1]
    score = match.get("score", "")
    if event in ("home_goal", "away_goal", "score"):
        return f"{match_id}:score:{score}"
    if event == "start":
        return f"{match_id}:start"
    if event == "finish":
        return f"{match_id}:finish:{score}"
    return f"{match_id}:{event}:{score}"



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
    logging.info("СТАТИСТИКА: найдено возрастов: %d", len(ages))

    current = {}
    khimik_ages = set()
    total_groups = 0
    total_group_matches = 0
    total_khimik_matches = 0

    for age, _label, age_url in ages:
        try:
            groups = discover_group_pages(age_url)
            total_groups += len(groups)
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
                total_group_matches += len(all_links)

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

                total_khimik_matches += len(links)

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

    if is_incomplete_response(state, current, khimik_ages):
        logging.warning(
            "ФХМО: текущий запуск пропущен из-за неполного ответа сайта"
        )
        return

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
                    home_delta = nh - oh
                    away_delta = na - oa
                    if home_delta == 1 and away_delta == 0:
                        event = "home_goal"
                    elif away_delta == 1 and home_delta == 0:
                        event = "away_goal"
                    # При нескольких голах между двумя проверками
                    # сообщаем изменение итогового счёта, не выдумывая
                    # отдельные события.
                    elif home_delta > 0 or away_delta > 0:
                        event = "score"
                except Exception:
                    pass
                changes.append((key, old, match, event))

            elif old_status == "⏳ Матч не начался" and new_status.startswith("⏱"):
                changes.append((key, old, match, "start"))

            elif old_status.startswith("⏱") and new_status == "🏁 Матч завершён":
                changes.append((key, old, match, "finish"))
            # Остальные изменения текста/периода внутри live-статуса
            # не считаются отдельным событием и уведомление не отправляют.

    logging.info("СТАТИСТИКА: найдено изменений: %d", len(changes))

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
    notifications_sent = 0

    sent_events = state.get("_sent_events", [])
    if not isinstance(sent_events, list):
        sent_events = []
    sent_events = set(str(x) for x in sent_events)

    for key, old, match, event in sorted(changes, key=sort_key):
        current_event_id = event_id(match, event)

        if not initial_mode and current_event_id in sent_events:
            logging.info(
                "EVENT: повторное событие пропущено: %s",
                current_event_id,
            )
            continue
        if event == "home_goal":
            if norm(match["home"]).lower() == "химик воскресенск":
                header = "🥅 ГОООЛ ХИМИКА!"
            else:
                header = "🥅 ГОЛ СОПЕРНИКА"

        elif event == "away_goal":
            if norm(match["away"]).lower() == "химик воскресенск":
                header = "🥅 ГОООЛ ХИМИКА!"
            else:
                header = "🥅 ГОЛ СОПЕРНИКА"

        elif event == "start":
            header = "🟢 МАТЧ НАЧАЛСЯ!"

        elif event == "finish":
            header = "🏁 МАТЧ ЗАВЕРШЁН"

        elif event == "score":
            header = "🥅 ИЗМЕНЕНИЕ СЧЁТА"

        else:
            header = None

        if event == "finish":
            body = (
                f"{match['home']} — {match['away']}\n"
                f"🥅 {match['score']}"
            )
        else:
            body = (
                f"🏒 Химик Воскресенск {match['age']}\n"
                f"🕒 {match['date_time']}\n"
                f"{match['home']} — {match['away']}\n"
                f"🔥 Счёт: {match['score']}\n"
                f"{match['status']}"
            )
        message = f"{header}\n\n{body}" if header else body

        if initial_mode:
            # Исторический backfill получает только владелец бота.
            telegram(message)
        else:
            # После первичной рассылки уведомления получают все активные подписчики.
            broadcast(message, users)

        if not initial_mode:
            sent_events.add(current_event_id)

        notifications_sent += 1

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

    # Храним ограниченную историю уведомлений, чтобы state не рос бесконечно.
    state["_sent_events"] = sorted(sent_events)[-500:]

    if not INITIAL_NOTIFY_DONE:
        INITIAL_NOTIFY_DONE = True
        logging.info("INITIAL_NOTIFY: первая рассылка завершена, дальше только изменения счёта")

    logging.info("СТАТИСТИКА: уведомлений отправлено: %d", notifications_sent)

    save_state(state)
    save_users(users)


if __name__ == "__main__":
    main()
