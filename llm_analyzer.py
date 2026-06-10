import asyncio
import html
import json
import os
import re
import textwrap
from typing import Any

import aiohttp
from dotenv import load_dotenv


load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen/qwen-2.5-72b-instruct:free")
OPENROUTER_FREE_MODELS = os.getenv("OPENROUTER_FREE_MODELS", "")
CHAT_COMPLETIONS_URL = f"{OPENROUTER_BASE_URL}/chat/completions"
DEFAULT_FREE_FALLBACK_MODELS = [
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "moonshotai/kimi-k2.6:free",
    "nex-agi/nex-n2-pro:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "poolside/laguna-m.1:free",
    "poolside/laguna-xs.2:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "qwen/qwen3-coder:free",
]
RETRYABLE_MODEL_STATUSES = {404, 429, 500, 502, 503, 504}
MAX_RETRY_AFTER_SECONDS = 25
TABLE_WIDTHS = [3, 13, 24, 30, 30]
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_SAFE_LIMIT = 3800


def _as_text(value: Any, default: str = "Не указано") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item) or default
    text = str(value).strip()
    return text or default


def _candidate_title(candidate: dict[str, Any]) -> str:
    return _as_text(candidate.get("title") or candidate.get("role"), "Не указана")


def _candidate_experience(candidate: dict[str, Any]) -> str:
    return _as_text(candidate.get("experience") or candidate.get("status") or candidate.get("updated_text"))


def _candidate_salary(candidate: dict[str, Any]) -> str:
    return _as_text(candidate.get("salary") or candidate.get("salary_text"), "Не указана")


def _format_candidates_for_llm(candidates: list[dict[str, Any]]) -> str:
    candidates_text = ""
    for i, res in enumerate(candidates, start=1):
        candidates_text += f"Кандидат {i}:\n"
        candidates_text += f"- Имя: {_as_text(res.get('name'), 'Не указано')}\n"
        candidates_text += f"- Должность: {_candidate_title(res)}\n"
        candidates_text += f"- Опыт: {_candidate_experience(res)}\n"
        candidates_text += f"- Навыки: {_as_text(res.get('skills'))}\n"
        candidates_text += f"- Зарплата: {_candidate_salary(res)}\n"
        candidates_text += f"- Ссылка: {_as_text(res.get('profile_url'), 'Не указана')}\n\n"
    return candidates_text


def _retry_after_seconds(response: aiohttp.ClientResponse) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return 0
    try:
        return max(0, min(MAX_RETRY_AFTER_SECONDS, float(value)))
    except ValueError:
        return 0


def _model_names() -> list[str]:
    model_names = [LLM_MODEL]
    env_models = [
        model.strip()
        for model in OPENROUTER_FREE_MODELS.replace("\n", ",").split(",")
        if model.strip()
    ]
    for fallback_model in env_models or DEFAULT_FREE_FALLBACK_MODELS:
        if fallback_model not in model_names:
            model_names.append(fallback_model)
    return model_names


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "/").replace("\n", " ").strip()


def _local_score(vacancy: str, candidate: dict[str, Any]) -> int:
    haystack = " ".join(
        [
            _candidate_title(candidate),
            _candidate_experience(candidate),
            _as_text(candidate.get("skills")),
        ]
    ).lower()
    words = [
        word.strip(".,;:()[]{}\"'").lower()
        for word in vacancy.split()
        if len(word.strip(".,;:()[]{}\"'")) >= 3
    ]
    matches = sum(1 for word in set(words) if word in haystack)
    skill_count = len(candidate.get("skills") or [])
    score = 45 + matches * 15 + min(skill_count, 6) * 3
    if candidate.get("profile_url"):
        score += 4
    if candidate.get("salary_text") or candidate.get("salary"):
        score += 3
    return max(20, min(88, score))


def _local_markdown_table(vacancy: str, candidates: list[dict[str, Any]]) -> str:
    rows = []
    for index, candidate in enumerate(candidates, start=1):
        score = _local_score(vacancy, candidate)
        name = _as_text(candidate.get("name"), f"Кандидат {index}")
        link = _as_text(candidate.get("profile_url"), "")
        candidate_cell = f"[{name}]({link})" if link else name
        skills = _as_text(candidate.get("skills"), "данные по навыкам ограничены")
        strengths = f"Совпадения по профилю: {_candidate_title(candidate)}; навыки: {skills}"
        risks = "Оценка резервная: LLM временно недоступна, нужна ручная проверка резюме"
        rows.append((score, candidate_cell, strengths, risks))

    rows.sort(key=lambda item: item[0], reverse=True)
    lines = [
        "| № | Рейтинг (0-100) | Кандидат | Сильные стороны | Риски |",
        "|---|---:|---|---|---|",
    ]
    for index, (score, candidate_cell, strengths, risks) in enumerate(rows, start=1):
        lines.append(
            "| "
            f"{index} | {score} | {_escape_table_cell(candidate_cell)} | "
            f"{_escape_table_cell(strengths)} | {_escape_table_cell(risks)} |"
        )
    return "\n".join(lines)


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return all(cell and set(cell) <= {"-", ":"} for cell in cells)


def _plain_links(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1\n\2", text)


def _extract_markdown_link(text: str) -> tuple[str, str]:
    match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", text)
    if not match:
        return text.strip(), ""
    return match.group(1).strip(), match.group(2).strip()


def _parse_markdown_table(table: str) -> list[list[str]]:
    rows = []
    for line in table.splitlines():
        if "|" not in line:
            continue
        cells = _split_markdown_row(line)
        if len(cells) < 5 or _is_separator_row(cells):
            continue
        rows.append([_plain_links(cell) for cell in cells[:5]])
    return rows


def _wrap_cell(value: str, width: int) -> list[str]:
    text = value.strip() or "-"
    lines = []
    for raw_line in text.splitlines():
        lines.extend(
            textwrap.wrap(
                raw_line,
                width=width,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    return lines or ["-"]


def _format_pretty_table(markdown_table: str) -> str:
    rows = _parse_markdown_table(markdown_table)
    if len(rows) < 2:
        return markdown_table.strip()

    border = "+" + "+".join("-" * (width + 2) for width in TABLE_WIDTHS) + "+"
    pretty_lines = [border]
    for row_index, row in enumerate(rows):
        wrapped_cells = [_wrap_cell(cell, width) for cell, width in zip(row, TABLE_WIDTHS)]
        height = max(len(cell_lines) for cell_lines in wrapped_cells)
        for line_index in range(height):
            pretty_lines.append(
                "| "
                + " | ".join(
                    (wrapped_cells[column][line_index] if line_index < len(wrapped_cells[column]) else "").ljust(width)
                    for column, width in enumerate(TABLE_WIDTHS)
                )
                + " |"
            )
        pretty_lines.append(border)
    return "\n".join(pretty_lines)


def _format_mobile_result(markdown_table: str) -> str:
    rows = _parse_markdown_table(markdown_table)
    if len(rows) < 2:
        return markdown_table.strip()

    blocks = ["Рейтинг кандидатов:"]
    for row in rows[1:]:
        number, rating, candidate, strengths, risks = row
        candidate_name, candidate_url = _extract_markdown_link(candidate)
        if not candidate_url:
            candidate_lines = candidate.splitlines()
            candidate_name = candidate_lines[0].strip() if candidate_lines else candidate.strip()
            candidate_url = next(
                (line.strip() for line in candidate_lines[1:] if line.strip().startswith(("http://", "https://"))),
                "",
            )

        block_lines = [
            f"{number}. {candidate_name}",
            f"Рейтинг: {rating}/100",
        ]
        if candidate_url:
            block_lines.append(f"Ссылка: {candidate_url}")
        block_lines.extend(
            [
                f"Сильные стороны: {strengths}",
                f"Риски: {risks}",
            ]
        )
        blocks.append("\n".join(block_lines))

    return "\n\n".join(blocks)


async def analyze_candidates(
    *,
    vacancy: str,
    candidates: list[dict[str, Any]],
    timeout: float = 60.0,
) -> str:
    """Send candidate data to the LLM and return a Markdown rating table."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY не указан в .env")

    candidates_text = _format_candidates_for_llm(candidates)
    system_prompt = """Ты — профессиональный IT-рекрутер. Твоя задача — оценить кандидатов на соответствие вакансии и составить рейтинг.

ПРАВИЛА ОТВЕТА:
1. Верни ответ СТРОГО в формате Markdown-таблицы.
2. Таблица должна содержать столбцы: | № | Рейтинг (0-100) | Кандидат | Сильные стороны | Риски |
3. В столбце "Кандидат" укажи имя кандидата и ссылку на резюме, если ссылка есть.
4. Отсортируй строки по убыванию рейтинга (лучшие сверху).
5. НЕ пиши никаких вступлений (типа "Вот таблица..."), НЕ пиши заключений. Только сама таблица.
6. Если кандидат явно не подходит, ставь ему рейтинг ниже 40.
7. Пиши коротко: в "Сильные стороны" и "Риски" не больше 8-10 слов.
"""
    user_prompt = (
        f"Вакансия: {vacancy}\n\n"
        "Список кандидатов:\n"
        f"{candidates_text}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/",
        "X-Title": "Telegram Resume Bot",
    }

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        last_error = None
        for model_name in _model_names():
            for attempt in range(2):
                payload = {
                    "model": model_name,
                    "temperature": 0.2,
                    "messages": messages,
                }
                async with session.post(CHAT_COMPLETIONS_URL, headers=headers, json=payload) as response:
                    response_text = await response.text()
                    if response.status < 400:
                        data = json.loads(response_text)
                        return data["choices"][0]["message"]["content"].strip()

                    last_error = RuntimeError(
                        f"OpenRouter API error {response.status} for model {model_name}: {response_text[:500]}"
                    )
                    if response.status == 429 and attempt == 0:
                        wait_seconds = _retry_after_seconds(response)
                        if wait_seconds:
                            await asyncio.sleep(wait_seconds)
                            continue
                    if response.status not in RETRYABLE_MODEL_STATUSES:
                        break
                    break

        return _local_markdown_table(vacancy, candidates)


def format_analysis_result(result: str) -> str:
    table = result.strip()
    if not table:
        return "Qwen не вернул таблицу с оценкой кандидатов."
    return format_analysis_result_chunks(table)[0]


def format_analysis_result_chunks(result: str, max_length: int = TELEGRAM_SAFE_LIMIT) -> list[str]:
    table = result.strip()
    if not table:
        return ["Qwen не вернул таблицу с оценкой кандидатов."]

    pretty_table = _format_mobile_result(table)
    chunks: list[str] = []
    current_blocks: list[str] = []

    def wrapped_length(blocks: list[str]) -> int:
        return len(html.escape("\n\n".join(blocks)))

    for block in pretty_table.split("\n\n"):
        candidate_blocks = current_blocks + [block]
        if current_blocks and wrapped_length(candidate_blocks) > max_length:
            chunks.append(html.escape("\n\n".join(current_blocks)))
            current_blocks = [block]
        else:
            current_blocks = candidate_blocks

    if current_blocks:
        chunks.append(html.escape("\n\n".join(current_blocks)))

    return chunks or ["Qwen не вернул таблицу с оценкой кандидатов."]
