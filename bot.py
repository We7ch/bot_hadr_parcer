import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup
from dotenv import load_dotenv

import database as db
import habr_parser as habr
import llm_analyzer


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")


class HabrSearch(StatesGroup):
    waiting_query = State()


class CandidateSearch(StatesGroup):
    waiting_query = State()


START_LABEL = "Запустить бота"
HABR_LABEL = "Найти вакансии"
CANDIDATE_LABEL = "Найти кандидатов"
DBTEST_LABEL = "Проверка подключения к Базе Данных"
HELP_LABEL = "Помощь"
CANCEL_LABEL = "Отмена / Вернуться назад"


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=START_LABEL), KeyboardButton(text=HABR_LABEL)],
        [KeyboardButton(text=CANDIDATE_LABEL), KeyboardButton(text=HELP_LABEL)],
        [KeyboardButton(text=CANCEL_LABEL)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите команду",
)


def main_menu_text() -> str:
    return (
        "Привет! Я бот для анализа резюме.\n\n"
        "Доступные команды:\n"
        "/habr — поиск вакансий на Habr Career\n"
        "/candidates — поиск кандидатов на Habr Career\n"
        "/help — помощь\n"
    )


async def init_bot() -> Bot:
    return Bot(token=BOT_TOKEN)


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать"),
            BotCommand(command="habr", description="Найти вакансии"),
            BotCommand(command="candidates", description="Найти кандидатов"),
            BotCommand(command="help", description="Помощь"),
        ]
    )


dp = Dispatcher()


@dp.message(Command("start"))
@dp.message(F.text == START_LABEL)
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(main_menu_text(), reply_markup=MAIN_MENU)


@dp.message(Command("help"))
@dp.message(F.text == HELP_LABEL)
async def cmd_help(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Я помогаю анализировать резюме с hh.ru на соответствие вакансиям.\n"
        "По всем вопросам обращаться: @we7ch\n"
        "/candidates — поиск кандидатов на Habr Career\n"
        "Скоро здесь появится полный функционал.",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("cancel"))
@dp.message(F.text == CANCEL_LABEL)
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(main_menu_text(), reply_markup=MAIN_MENU)


async def _send_dbtest_result(message: types.Message, state: FSMContext):
    await state.clear()
    try:
        await db.save_resume(
            None,
            hh_resume_id="test_123",
            title="Python Developer",
            skills=["Python", "FastAPI", "PostgreSQL", "aiogram"],
            experience="3 года коммерческой разработки",
            salary=200000,
            raw_data={"test": "data", "source": "hh.ru"},
        )

        await db.save_user_request(
            None,
            telegram_id=message.from_user.id,
            vacancy_description="Ищем Python-разработчика",
            result_summary="Резюме подходит на 85%",
        )

        resume = await db.get_resume(None, "test_123")
        history = await db.get_user_history(None, message.from_user.id)

        await message.answer(
            f"БД работает.\n\n"
            f"Сохранённое резюме:\n"
            f"ID: {resume['hh_resume_id']}\n"
            f"Должность: {resume['title']}\n"
            f"Зарплата: {resume['salary']} руб.\n\n"
            f"История запросов: {len(history)}",
            reply_markup=MAIN_MENU,
        )
    except Exception as e:
        await message.answer(
            f"Ошибка при работе с БД:\n{type(e).__name__}: {e}",
            reply_markup=MAIN_MENU,
        )


@dp.message(F.text == DBTEST_LABEL)
async def menu_dbtest(message: types.Message, state: FSMContext):
    await _send_dbtest_result(message, state)


@dp.message(Command("habr"))
@dp.message(F.text == HABR_LABEL)
async def cmd_habr(message: types.Message, state: FSMContext):
    await state.set_state(HabrSearch.waiting_query)
    await message.answer("Какую вакансию хотите найти?", reply_markup=MAIN_MENU)


@dp.message(HabrSearch.waiting_query, F.text)
async def habr_query_handler(message: types.Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer(
            "Напишите название вакансии, например: Python developer",
            reply_markup=MAIN_MENU,
        )
        return

    try:
        vacancies = await habr.fetch_habr_vacancies(query=query, per_page=5)
        await state.clear()

        if not vacancies:
            await message.answer(
                f"По запросу «{query}» вакансий не нашёл.",
                reply_markup=MAIN_MENU,
            )
            return

        chunks: list[str] = []
        current = f"Вакансии Habr Career по запросу «{query}»:\n\n"

        for vacancy in vacancies:
            block = habr.format_vacancy(vacancy)
            if len(current) + len(block) + 2 > 3500:
                chunks.append(current.rstrip())
                current = ""
            current += block + "\n\n"

        if current.strip():
            chunks.append(current.rstrip())

        for chunk in chunks:
            await message.answer(chunk, reply_markup=MAIN_MENU)

    except Exception as e:
        await state.clear()
        await message.answer(
            f"Ошибка при поиске вакансий:\n{type(e).__name__}: {e}",
            reply_markup=MAIN_MENU,
        )


@dp.message(Command("candidates"))
@dp.message(F.text == CANDIDATE_LABEL)
async def cmd_candidates(message: types.Message, state: FSMContext):
    await state.set_state(CandidateSearch.waiting_query)
    await message.answer("На какую вакансию нужно найти кандидатов?", reply_markup=MAIN_MENU)


@dp.message(CandidateSearch.waiting_query, F.text)
async def candidate_query_handler(message: types.Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer(
            "Напишите название вакансии, по которой искать кандидатов. Например python разработчик",
            reply_markup=MAIN_MENU,
        )
        return

    try:
        await message.answer(
            f"Начал поиск кандидатов по вакансии «{query}».",
            reply_markup=MAIN_MENU,
        )

        candidates = await habr.fetch_habr_candidates(query=query, page=1)

        if not candidates:
            await state.clear()
            await message.answer(
                f"По запросу «{query}» кандидатов не нашёл.",
                reply_markup=MAIN_MENU,
            )
            return

        await state.clear()

        await message.answer(
            f"Нашёл кандидатов: {len(candidates)}. Сохранил их в памяти бота для текущего запроса.",
            reply_markup=MAIN_MENU,
        )

        await message.answer(
            "Отправляю найденных кандидатов в ChatGPT через OpenRouter на оценку...",
            reply_markup=MAIN_MENU,
        )

        try:
            analysis_result = await llm_analyzer.analyze_candidates(
                vacancy=query,
                candidates=candidates[:10],
            )
            analysis_chunks = llm_analyzer.format_analysis_result_chunks(analysis_result)
            for chunk_index, chunk in enumerate(analysis_chunks, start=1):
                await message.answer(
                    chunk,
                    parse_mode="HTML",
                    reply_markup=MAIN_MENU if chunk_index == len(analysis_chunks) else None,
                )
        except Exception as e:
            logging.exception("Не удалось оценить кандидатов через Qwen")
            await message.answer(
                f"Кандидаты найдены, но оценка через ChatGPT не удалась:\n{type(e).__name__}: {e}",
                reply_markup=MAIN_MENU,
            )

    except Exception as e:
        await state.clear()
        await message.answer(
            f"Ошибка при поиске кандидатов:\n{type(e).__name__}: {e}",
            reply_markup=MAIN_MENU,
        )


@dp.message(Command("dbtest"))
async def cmd_dbtest(message: types.Message, state: FSMContext):
    await _send_dbtest_result(message, state)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    bot = await init_bot()
    await setup_bot_commands(bot)

    logging.info("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
