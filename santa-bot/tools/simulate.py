"""A 12-person office game on FakeMaxApi, printed as a Russian chat transcript (§14).

    cd santa-bot && .venv/bin/python -m tools.simulate

It is the owner's demo and a smoke test. Every update goes through the same code as
the webhook (``process_update``); the payment arrives through the real ResultURL
route with a valid test-mode signature; the notices come from the scheduler's jobs.
Nothing leaves the computer: MAX is faked, the clock is fixed (Friday, 20 November
2026, 12:00 in Moscow) and the database lives in a temporary folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Collection, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import jobs, repo
from app.config import Config, load_config
from app.context import CTX_KEY, AppContext
from app.core import texts
from app.core.clock import FakeClock
from app.core.kb import LinkButton
from app.core.models import Game, Tier
from app.main import build_context
from app.updates import process_update
from app.web import routes
from tools.fake_max import Delivery, FakeMaxApi, FakeUser

START = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)  # 12:00 in Moscow
SEED = 2026
OWNER_ID = 1000
PEOPLE = {
    101: "Ольга Смирнова",
    102: "Иван Петров",
    103: "Мария Петрова",
    104: "Алексей Кузнецов",
    105: "Екатерина Соколова",
    106: "Дмитрий Попов",
    107: "Анна Лебедева",
    108: "Сергей Козлов",
    109: "Наталья Новикова",
    110: "Михаил Морозов",
    111: "Татьяна Волкова",
    112: "Павел Соловьёв",
}
OLGA, IVAN, MARIA, TATIANA, PAVEL = 101, 102, 103, 111, 112
COLLEAGUES = {  # joined by link after Иван and Мария, with their wishes
    104: "Хороший молотый кофе или френч-пресс",
    105: "Книгу Донны Тартт или красивый ежедневник",
    106: "Что-нибудь для велосипеда: фонарик, флягу",
    107: "Ароматическую свечу или набор чая",
    108: "Настольную игру для компании",
    109: "Тёплые носки с оленями :)",
    110: "Пазл на 1000 деталей",
}
RESULT_URL = "/pay/robokassa/result"


class SimulationError(Exception):
    """The game did not go as the scenario expects: something in the bot is broken."""


def simulation_env(data_dir: Path) -> dict[str, str]:
    return {
        "DOMAIN": "santa-v-chate.ru",
        "MAX_BOT_TOKEN": "simulation",
        "MAX_BOT_USERNAME": "se1234567_bot",
        "MAX_WEBHOOK_SECRET": "simulation-webhook-secret",
        "WEBHOOK_PATH_SECRET": "simulationpathsecret0000",
        "ADMIN_EXPORT_TOKEN": "simulation-export-token",
        "ADMIN_USER_IDS": str(OWNER_ID),
        "OWNER_FULL_NAME": "Петров Пётр Петрович",
        "OWNER_INN": "123456789012",
        "SUPPORT_EMAIL": "help@santa-v-chate.ru",
        "ROBOKASSA_MERCHANT_LOGIN": "santa-demo",
        "ROBOKASSA_TEST": "1",
        "ROBOKASSA_TEST_PASSWORD1": "demo-test-password-1",
        "ROBOKASSA_TEST_PASSWORD2": "demo-test-password-2",
        "DATA_DIR": str(data_dir),
    }


# --- the transcript --------------------------------------------------------------------------------


class Transcript:
    """Prints what people do and every message the bot shows them, in order."""

    def __init__(self, api: FakeMaxApi, write: Callable[[str], None]) -> None:
        self._api = api
        self._write = write
        self._seen = 0
        self._quiet = False

    def title(self, text: str) -> None:
        self._write(text)
        self._write("=" * len(text))

    def section(self, text: str) -> None:
        self._write("")
        self._write(f"--- {text} ---")

    def note(self, text: str) -> None:
        if not self._quiet:
            self._write(f"({text})")

    def action(self, who: str, what: str) -> None:
        if not self._quiet:
            self._write(f"{who}: {what}")

    def flush(self, only: Collection[int] | None = None) -> None:
        """Print the bot's new messages; with ``only``, just those to these people (others are counted)."""
        new = self._api.timeline[self._seen:]
        self._seen = len(self._api.timeline)
        if self._quiet:
            return
        shown = [d for d in new if only is None or d.record.target.id in only]
        for delivery in shown:
            self._write(render(delivery))
        if len(shown) < len(new):
            self.note(f"и ещё {len(new) - len(shown)} таких же сообщений другим участникам")

    @contextmanager
    def quietly(self, summary: str) -> Iterator[None]:
        """Run a repetitive part without printing it, then sum it up in one line."""
        self._quiet = True
        try:
            yield
        finally:
            self._quiet = False
        self.note(summary)


def short_name(user_id: int) -> str:
    return PEOPLE[user_id].split()[0] if user_id in PEOPLE else "Владелец бота"


def render(delivery: Delivery) -> str:
    target = delivery.record.target
    header = f"Бот → {short_name(target.id)}" + (" (обновил сообщение)" if delivery.edited else "") + ":"
    lines = [header, *(f"    {line}" for line in delivery.body.text.split("\n"))]
    if delivery.body.keyboard is not None:
        for row in delivery.body.keyboard.rows:
            labels = [f"[{b.text} — ссылка]" if isinstance(b, LinkButton) else f"[{b.text}]" for b in row]
            lines.append("    " + " ".join(labels))
    return "\n".join(lines)


# --- driving the bot ---------------------------------------------------------------------------------------


class Simulation:
    def __init__(self, ctx: AppContext, api: FakeMaxApi, clock: FakeClock, client: TestClient[Any, Any],
                 transcript: Transcript) -> None:
        self.ctx = ctx
        self.api = api
        self.clock = clock
        self.client = client
        self.show = transcript
        self.people = {user_id: FakeUser(api, user_id, name) for user_id, name in PEOPLE.items()}

    async def _deliver(self, update: dict[str, Any], only: Collection[int] | None = None) -> None:
        await process_update(self.ctx, update)
        await self.ctx.outbox.drain()
        self.show.flush(only)

    async def start(self, user_id: int, payload: str | None = None) -> None:
        what = "нажимает «Начать» в боте" if payload is None else f"открывает ссылку …?start={payload}"
        self.show.action(short_name(user_id), what)
        await self._deliver(self.people[user_id].start(payload))

    async def say(self, user_id: int, text: str) -> None:
        self.show.action(short_name(user_id), text)
        await self._deliver(self.people[user_id].say(text))

    async def press(self, user_id: int, label: str, only: Collection[int] | None = None) -> None:
        _, button = self.api.find_button(user_id, label)
        self.show.action(short_name(user_id), f"нажимает [{button.text}]")
        await self._deliver(self.people[user_id].press(label), only)

    async def run_job(self, name: str, job: Callable[[AppContext], Any], only: Collection[int] | None = None) -> None:
        self.show.note(f"планировщик: {name}")
        await job(self.ctx)
        await self.ctx.outbox.drain()
        self.show.flush(only)

    def wait(self, delta: timedelta, text: str) -> None:
        self.clock.advance(delta.total_seconds())
        self.show.note(text)

    async def game(self) -> Game:
        entries = await repo.games_of_user(self.ctx.db, OLGA)
        if not entries:
            raise SimulationError("Ольга не создала игру")
        return entries[0][0]

    async def join(self, user_id: int, code: str, wishes: str) -> None:
        """Open the invite, consent, keep the MAX name and write wishes."""
        await self.start(user_id, f"j_{code}")
        await self.press(user_id, texts.BTN_CONSENT)
        await self.press(user_id, texts.BTN_KEEP_NAME)
        await self.say(user_id, wishes)


# --- the scenario ---------------------------------------------------------------------------------------------


async def scenario(sim: Simulation) -> None:
    show = sim.show
    show.section("1. Ольга создаёт игру для отдела")
    await sim.start(OLGA)
    await sim.press(OLGA, texts.BTN_CONSENT)
    await sim.press(OLGA, texts.BTN_CREATE_GAME)
    await sim.say(OLGA, "Отдел продаж")
    await sim.press(OLGA, "до 1000 ₽")
    await sim.press(OLGA, "25.12")
    await sim.press(OLGA, texts.BTN_I_PARTICIPATE)
    await sim.say(OLGA, "Хороший зелёный чай и кружку с котом")
    code = (await sim.game()).code

    show.section("2. Коллеги вступают по ссылке из общего чата")
    await sim.join(IVAN, code, "Шахматы в дорогу или книгу про космос")
    await sim.start(MARIA, f"j_{code}")
    await sim.press(MARIA, texts.BTN_CONSENT)
    await sim.press(MARIA, texts.BTN_CHANGE_NAME)
    await sim.say(MARIA, "Мария (бухгалтерия)")
    await sim.say(MARIA, "Плед или что-нибудь для дачи")
    names = ", ".join(short_name(user_id) for user_id in COLLEAGUES)
    with show.quietly(f"так же вступили и написали пожелания: {names}"):
        for user_id, wishes in COLLEAGUES.items():
            await sim.join(user_id, code, wishes)

    show.section("3. Организатору приходит сводка")
    sim.wait(timedelta(minutes=10), "прошло 10 минут")
    await sim.run_job("уведомления организатору", jobs.organizer_notices)

    show.section("4. Мест нет: в бесплатной игре до 10 человек")
    await sim.start(TATIANA, f"j_{code}")
    await sim.press(TATIANA, texts.BTN_CONSENT)
    with show.quietly("Павел открыл бота раньше, без ссылки, и дал согласие"):
        await sim.start(PAVEL)
        await sim.press(PAVEL, texts.BTN_CONSENT)
    await sim.say(PAVEL, f"код {code.lower()}")
    await sim.run_job("уведомления организатору", jobs.organizer_notices)

    show.section("5. Татьяна оплачивает расширение (Robokassa в тестовом режиме)")
    await sim.press(TATIANA, texts.btn_pay(490))
    await robokassa_pays(sim, sim.api.link_url(TATIANA, texts.btn_pay_link(490)))
    await sim.start(TATIANA, f"p_{await latest_inv_id(sim)}")
    await sim.say(TATIANA, "Сертификат в книжный")
    await sim.say(PAVEL, "Что-нибудь к чаю")

    show.section("6. Исключение: Иван и Мария — супруги")
    await sim.press(OLGA, texts.BTN_PANEL)
    await sim.press(OLGA, texts.BTN_EXCLUSIONS)
    await sim.press(OLGA, texts.BTN_ADD_PAIR)
    await sim.press(OLGA, "Иван Петров")
    await sim.press(OLGA, "Мария (бухгалтерия)")

    show.section("7. Жеребьёвка")
    await sim.press(OLGA, texts.BTN_PANEL)
    await sim.press(OLGA, texts.BTN_DRAW)
    await sim.press(OLGA, texts.BTN_CONFIRM_DRAW, only={OLGA, IVAN})

    show.section("8. Анонимные вопросы")
    receiver = await receiver_of(sim, IVAN)
    await sim.press(IVAN, texts.BTN_ASK_RECEIVER)
    await sim.say(IVAN, "Привет! Ты больше любишь чай или кофе?")
    await sim.press(receiver, texts.BTN_REPLY_TO_SANTA)
    await sim.say(receiver, "Кофе, и побольше! Спасибо, Санта :)")

    show.section("9. За день до обмена")
    sim.wait(datetime(2026, 12, 24, 9, 0, tzinfo=timezone.utc) - sim.clock.now(), "24 декабря, 12:00")
    await sim.run_job("напоминание за день до обмена", jobs.pre_exchange_reminders, only={IVAN})
    await check(sim)


async def robokassa_pays(sim: Simulation, payment_url: str) -> None:
    """What Robokassa does after the test payment: a signed notice to ResultURL (§7)."""
    query = parse_qs(urlsplit(payment_url).query)
    inv_id, out_sum = query["InvId"][0], query["OutSum"][0]
    config = sim.ctx.config
    _, password2 = config.robokassa_passwords
    signature = hashlib.new(config.robokassa_hash, f"{out_sum}:{inv_id}:{password2}".encode()).hexdigest()
    sim.show.note(f"Татьяна платит {out_sum} ₽ по СБП на тестовой странице Robokassa")
    sim.show.action("Robokassa → сайт", f"POST {RESULT_URL} OutSum={out_sum} InvId={inv_id} IsTest=1, подпись верна")
    response = await sim.client.post(RESULT_URL, data={
        "OutSum": out_sum, "InvId": inv_id, "SignatureValue": signature, "IsTest": "1", "PaymentMethod": "SBP",
    })
    answer = await response.text()
    sim.show.action("сайт → Robokassa", answer)
    if (response.status, answer) != (200, f"OK{inv_id}"):
        raise SimulationError(f"ResultURL answered {response.status} {answer!r}")
    await sim.ctx.outbox.drain()
    sim.show.flush()


async def latest_inv_id(sim: Simulation) -> int:
    game = await sim.game()
    payments = await repo.payments_of_game(sim.ctx.db, game.id)
    return payments[-1].inv_id


async def receiver_of(sim: Simulation, giver: int) -> int:
    game = await sim.game()
    receiver = await repo.receiver_of(sim.ctx.db, game.id, giver)
    if receiver is None:
        raise SimulationError("жеребьёвка не записала пары")
    return receiver


async def check(sim: Simulation) -> None:
    """The final state the scenario promises; print it as the summary."""
    game = await sim.game()
    pairs = await repo.assignments(sim.ctx.db, game.id)
    payments = await repo.payments_of_game(sim.ctx.db, game.id)
    problems = []
    if (game.tier, game.participant_limit) != (Tier.S, 30):
        problems.append(f"тариф {game.tier}, лимит {game.participant_limit}")
    if set(pairs) != set(PEOPLE) or set(pairs.values()) != set(PEOPLE):
        problems.append("не все 12 человек получили пару")
    if pairs.get(IVAN) == MARIA or pairs.get(MARIA) == IVAN:
        problems.append("исключение не соблюдено")
    if sim.api.messages_to(OWNER_ID):
        problems.append("администратору пришли сообщения об ошибках")
    if problems:
        raise SimulationError("; ".join(problems))
    sim.show.section("Итог")
    sim.show.action("Игра", f"«{game.title}», код {game.code}, тариф {game.tier}, до {game.participant_limit} человек")
    sim.show.action("Оплата", f"InvId {payments[-1].inv_id}, {payments[-1].amount_rub} ₽, статус {payments[-1].status}")
    sim.show.action("Пары", f"{len(pairs)} из 12, Иван и Мария друг другу не дарят")


# --- running it ------------------------------------------------------------------------------------------------


@asynccontextmanager
async def result_url_client(ctx: AppContext) -> AsyncIterator[TestClient[Any, Any]]:
    """The website's routes on a local port, for the ResultURL call."""
    app = web.Application()
    app[CTX_KEY] = ctx
    routes.register(app)
    async with TestClient(TestServer(app)) as client:
        yield client


async def run(write: Callable[[str], None] = print) -> None:
    with tempfile.TemporaryDirectory(prefix="santa-simulation-") as data_dir:
        config: Config = load_config(simulation_env(Path(data_dir)), announce=lambda _: None)
        clock = FakeClock(START)
        api = FakeMaxApi(clock=clock, bot_username=config.max_bot_username)
        ctx = await build_context(config, api=api, clock=clock, rng=random.Random(SEED))
        transcript = Transcript(api, write)
        transcript.title("«Санта в чате»: офисная игра на 12 человек (демонстрация, всё понарошку)")
        write("Пятница, 20 ноября 2026, 12:00 по Москве. Бесплатно — до 10 участников.")
        try:
            async with result_url_client(ctx) as client:
                await scenario(Simulation(ctx, api, clock, client, transcript))
        finally:
            await ctx.wait_background()
            await ctx.db.close()


def main() -> None:
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    try:
        asyncio.run(run())
    except SimulationError as error:
        print(f"\nСИМУЛЯЦИЯ НЕ УДАЛАСЬ: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
