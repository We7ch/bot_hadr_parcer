import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp


RSS_URL = "https://career.habr.com/vacancies/rss"
RESUMES_URL = "https://career.habr.com/resumes"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

TITLE_RE = re.compile(r'^Требуется «(?P<title>.+?)»(?: \((?P<extra>.+?)\))?$')
SALARY_RE = re.compile(
    r"(?:от|до)?\s*\d[\d\s]*"
    r"(?:\s*до\s*\d[\d\s]*)?\s*₽",
    re.IGNORECASE,
)
SKILLS_RE = re.compile(r"Требуемые навыки:\s*(.+?)(?:\.\s*$|$)", re.IGNORECASE)
EMPLOYMENT_PHRASES = (
    "Полный рабочий день",
    "Неполный рабочий день",
    "Сменный график",
    "Гибкий график",
    "Удаленная работа",
    "Проектная работа",
    "Стажировка",
)

CANDIDATE_BLOCK_RE = re.compile(
    r'(<article class="grid grid-cols-1 justify-start gap-4">.*?</article>)',
    re.S,
)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _strip_tags(text: str) -> str:
    return _clean(re.sub(r"<[^>]+>", " ", text))


def _normalize_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://career.habr.com{url}"


def _extract_title(raw_title: str) -> tuple[str, str]:
    match = TITLE_RE.match(_clean(raw_title))
    if not match:
        return _clean(raw_title), ""
    return match.group("title"), _clean(match.group("extra"))


def _extract_salary(text: str) -> str:
    match = SALARY_RE.search(text)
    return _clean(match.group(0)) if match else ""


def _extract_skills(description: str) -> list[str]:
    match = SKILLS_RE.search(description)
    if not match:
        return []

    skills = []
    for item in match.group(1).split(","):
        skill = _clean(item).lstrip("#").strip()
        if skill:
            skills.append(skill)
    return skills


def _extract_employment(description: str) -> str:
    for phrase in EMPLOYMENT_PHRASES:
        if phrase.lower() in description.lower():
            return phrase
    return ""


def _extract_location(extra: str, description: str) -> str:
    if extra:
        cleaned = extra
        cleaned = SALARY_RE.sub("", cleaned)
        for phrase in EMPLOYMENT_PHRASES:
            cleaned = re.sub(re.escape(phrase), "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace("Можно удалённо", "")
        cleaned = re.sub(r"\s*,\s*,", ",", cleaned)
        return _clean(cleaned.strip(" ,.-"))

    # Fallback: the description usually starts with the city before "(Россия)".
    desc_head = description.split(". ", 1)[0]
    if desc_head.startswith("Компания "):
        return ""
    return ""


def _parse_rss_item(item: ET.Element) -> dict[str, Any]:
    title_raw = item.findtext("title", default="")
    description_raw = item.findtext("description", default="")
    company = _clean(item.findtext("author", default=""))
    link = _clean(item.findtext("link", default=""))
    guid = _clean(item.findtext("guid", default=""))
    image = _clean(item.findtext("image", default=""))
    pub_date_raw = _clean(item.findtext("pubDate", default=""))

    vacancy_title, extra = _extract_title(title_raw)
    salary_text = _extract_salary(f"{extra} {description_raw}")
    location = _extract_location(extra, description_raw)
    skills = _extract_skills(description_raw)
    employment = _extract_employment(description_raw)

    published_at = ""
    if pub_date_raw:
        try:
            published_at = parsedate_to_datetime(pub_date_raw).isoformat()
        except Exception:
            published_at = pub_date_raw

    return {
        "habr_vacancy_id": f"habr_{guid}" if guid else "",
        "title": vacancy_title,
        "company": company,
        "location": location,
        "salary_text": salary_text,
        "employment": employment,
        "remote": "Можно удалённо" in description_raw,
        "skills": skills,
        "description": _clean(description_raw),
        "published_at": published_at,
        "link": link,
        "image": image,
        "raw_title": _clean(title_raw),
        "raw_description": _clean(description_raw),
    }


def _parse_rss(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    return [_parse_rss_item(item) for item in root.findall("./channel/item")]


def _parse_candidate_block(block: str) -> dict[str, Any]:
    name_match = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S)
    name = _strip_tags(name_match.group(2)) if name_match else ""
    profile_url = ""
    if name_match:
        profile_url = _normalize_url(name_match.group(1))

    updated_match = re.search(r'<time[^>]*datetime="([^"]+)"[^>]*>(.*?)</time>', block, flags=re.S)
    updated_at = updated_match.group(1) if updated_match else ""
    updated_text = _strip_tags(updated_match.group(2)) if updated_match else ""

    role_match = re.search(
        r'<div class="text-body-m text-font-black">(.*?)</div>',
        block,
        flags=re.S,
    )
    role_text = _strip_tags(role_match.group(1)) if role_match else ""

    salary_match = re.search(
        r'<div class="text-body-l font-semibold text-ui-green">(.*?)</div>',
        block,
        flags=re.S,
    )
    salary_text = _strip_tags(salary_match.group(1)) if salary_match else ""
    status = ""
    if salary_text:
        status_match = re.search(r"\s*•\s*(Ищу работу|Не ищу работу)\s*$", salary_text)
        if status_match:
            status = status_match.group(1)
            salary_text = _clean(salary_text[: status_match.start()])

    skill_titles = [
        _strip_tags(item)
        for item in re.findall(r'<span class="skill-chip__title"[^>]*>(.*?)</span>', block, flags=re.S)
    ]
    skills = [item for item in skill_titles if item]

    avatar_match = re.search(r'<img src="([^"]+)" class="base-avatar__img"', block)
    avatar = avatar_match.group(1) if avatar_match else ""

    if not status and "Ищу работу" in block:
        status = "Ищу работу"
    elif not status and "Не ищу работу" in block:
        status = "Не ищу работу"

    return {
        "name": name,
        "profile_url": profile_url,
        "updated_at": updated_at,
        "updated_text": updated_text,
        "role": role_text,
        "salary_text": salary_text,
        "status": status,
        "skills": skills,
        "avatar": avatar,
        "raw_html": block,
    }


def _parse_candidates(html_text: str) -> list[dict[str, Any]]:
    blocks = CANDIDATE_BLOCK_RE.findall(html_text)
    return [_parse_candidate_block(block) for block in blocks]


async def fetch_habr_vacancies(
    query: str = "python",
    *,
    page: int = 1,
    per_page: int = 10,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "page": str(page),
        "per_page": str(per_page),
    }

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    headers = {"User-Agent": USER_AGENT}

    async with aiohttp.ClientSession(timeout=client_timeout, headers=headers) as session:
        async with session.get(RSS_URL, params=params, proxy=proxy) as response:
            response.raise_for_status()
            xml_text = await response.text()

    return _parse_rss(xml_text)


async def fetch_habr_candidates(
    query: str,
    *,
    page: int = 1,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "page": str(page),
    }

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    headers = {"User-Agent": USER_AGENT}

    async with aiohttp.ClientSession(timeout=client_timeout, headers=headers) as session:
        async with session.get(RESUMES_URL, params=params, proxy=proxy) as response:
            response.raise_for_status()
            html_text = await response.text()

    return _parse_candidates(html_text)


def format_vacancy(vacancy: dict[str, Any]) -> str:
    parts = []

    title = vacancy.get("title") or "Без названия"
    company = vacancy.get("company") or "Компания не указана"
    parts.append(f"• {title}")
    parts.append(f"  {company}")

    meta = []
    if vacancy.get("location"):
        meta.append(vacancy["location"])
    if vacancy.get("salary_text"):
        meta.append(vacancy["salary_text"])
    if vacancy.get("employment"):
        meta.append(vacancy["employment"])
    if vacancy.get("remote"):
        meta.append("удалённо")

    if meta:
        parts.append(f"  {', '.join(meta)}")

    skills = vacancy.get("skills") or []
    if skills:
        parts.append(f"  Навыки: {', '.join(skills[:6])}")

    link = vacancy.get("link")
    if link:
        parts.append(f"  {link}")

    return "\n".join(parts)


def format_candidate(candidate: dict[str, Any]) -> str:
    parts = []

    name = candidate.get("name") or "Без имени"
    parts.append(f"• {name}")

    if candidate.get("role"):
        parts.append(f"  {candidate['role']}")

    meta = []
    if candidate.get("salary_text"):
        meta.append(candidate["salary_text"])
    if candidate.get("status"):
        meta.append(candidate["status"])
    if candidate.get("updated_text"):
        meta.append(candidate["updated_text"])

    if meta:
        parts.append(f"  {', '.join(meta)}")

    skills = candidate.get("skills") or []
    if skills:
        parts.append(f"  Навыки: {', '.join(skills[:6])}")

    link = candidate.get("profile_url")
    if link:
        parts.append(f"  {link}")

    return "\n".join(parts)
