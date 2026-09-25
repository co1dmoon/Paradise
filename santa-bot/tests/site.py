"""HTTP helpers for website, payment callback and webhook tests.

``site_client(ctx)`` serves ``web.routes`` over the test ``AppContext`` without
starting the outbox worker, so tests drain the outbox themselves and count exactly.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import repo
from app.context import CTX_KEY, AppContext
from app.core import games, texts
from app.core.clock import FakeClock
from app.core.models import Game, JoinVia, ParticipantStatus, Payment
from app.web import routes
from tests.bot import Bot
from tests.helpers import consented_user
from tools.fake_max import FakeUser

TEST_PASSWORD2 = "test-pass-2"
ORGANIZER = 100


@asynccontextmanager
async def site_client(ctx: AppContext) -> AsyncIterator[TestClient[web.Request, web.Application]]:
    app = web.Application()
    app[CTX_KEY] = ctx
    routes.register(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def result_signature(out_sum: str, inv_id: int | str, password2: str = TEST_PASSWORD2, algorithm: str = "md5") -> str:
    """The vector computed independently of the code under test: hash('OutSum:InvId:Password#2')."""
    return hashlib.new(algorithm, f"{out_sum}:{inv_id}:{password2}".encode()).hexdigest()


def result_form(payment: Payment, out_sum: str | None = None, **extra: str) -> dict[str, str]:
    """What Robokassa posts to ResultURL for ``payment`` (live format: six decimals)."""
    out_sum = out_sum or f"{payment.amount_rub}.000000"
    return {
        "OutSum": out_sum,
        "InvId": str(payment.inv_id),
        "SignatureValue": result_signature(out_sum, payment.inv_id),
        "Fee": "17.15",
        "EMail": "payer@example.ru",
        "PaymentMethod": "SBP",
        "IncCurrLabel": "SBPR",
        **extra,
    }


async def fill(bot: Bot, clock: FakeClock, game: Game, ids: Sequence[int]) -> None:
    """Consented users joined through the core (fast)."""
    for user_id in ids:
        await consented_user(bot.ctx.db, clock, user_id, f"Гость {user_id}")
        clock.advance(1)
        await games.join_game(bot.ctx.db, game.id, user_id, JoinVia.LINK, clock.now())


async def full_game_with_waiting(bot: Bot, clock: FakeClock, *late: FakeUser) -> Game:
    """Organizer Ольга plus 9 guests fill the free game; each ``late`` user joins the queue by link."""
    olga = bot.person(ORGANIZER, "Ольга")
    await bot.onboard(olga)
    game = await bot.create_game(olga, "Отдел продаж")
    await fill(bot, clock, game, range(201, 210))
    for person in late:
        await bot.join(person, game, wishes=None)
    return game


async def payment_of(ctx: AppContext, inv_id: int) -> Payment:
    payment = await repo.get_payment(ctx.db, inv_id)
    assert payment is not None
    return payment


async def status_in(ctx: AppContext, game: Game, user_id: int) -> ParticipantStatus:
    participant = await repo.get_participant(ctx.db, game.id, user_id)
    assert participant is not None
    return participant.status


async def pay_click(bot: Bot, person: FakeUser, game: Game) -> Payment:
    """``person`` taps [Расширить за 490 ₽]; returns their payment row."""
    await bot(person.press(texts.btn_pay(490)))
    payments = [p for p in await repo.payments_of_game(bot.ctx.db, game.id) if p.payer_id == person.user_id]
    assert len(payments) == 1
    return payments[0]
