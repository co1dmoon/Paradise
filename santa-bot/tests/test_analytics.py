from __future__ import annotations

import random
from datetime import date, timedelta

from app import repo
from app.core import analytics, billing, games, texts, users
from app.core.clock import FakeClock
from app.core.dates import MOSCOW
from app.core.games import GameDraft
from app.core.models import JoinVia, Tier
from app.core.pricing import PriceList
from app.db import Database


async def test_stats_follow_the_viral_loop(db: Database, clock: FakeClock, make_user) -> None:
    rng = random.Random(1)
    settings = await repo.get_settings(db)
    prices = PriceList.from_settings(settings)
    await make_user(100, "Ольга")
    game = await games.create_game(db, 100, GameDraft("Офис", "до 1000 ₽", None, True), settings,
                                   now=clock.now(), today=date(2026, 11, 20), rng=rng)
    for user_id in range(1, 12):
        clock.advance(1)
        await make_user(user_id, f"Коллега {user_id}", source=f"j:{game.code}")
        await games.join_game(db, game.id, user_id, JoinVia.LINK, clock.now())
    payment = await billing.request_upgrade(db, game.id, 11, Tier.S, prices, clock.now())
    await billing.confirm_payment(db, payment.inv_id, prices, raw="", now=clock.now())
    await games.run_draw(db, game.id, 100, now=clock.now(), rng=rng)
    clock.advance(60)
    ref = await games.create_game(db, 5, GameDraft("Семья", "до 500 ₽", None, True, source_game_id=game.id),
                                  settings, now=clock.now(), today=date(2026, 11, 20), rng=rng)
    await games.record_result_delivery(db, game.id, 3, False, clock.now())
    await users.ensure_user(db, 777, "s:yd", clock.now())

    block = await analytics.collect_stats(db, clock.now() - timedelta(hours=1), clock.now() + timedelta(seconds=1))
    assert (block.new_users, block.consents) == (13, 12)
    assert (block.games_created, block.games_3plus, block.draws) == (2, 1, 1)
    assert block.draw_size_avg == 12 and block.draw_size_median == 12
    assert (block.games_hit_limit, block.paid_games, block.revenue_rub, block.conversion) == (1, 1, 490, 1.0)
    assert block.ref_games == 1 and ref.source == "ref"
    assert block.participant_to_organizer == 1 / 11
    assert block.result_dm_failure_rate == 1 / 12

    report = await analytics.build_stats_report(db, clock.now(), MOSCOW)
    assert [period.label for period, _ in report.blocks] == ["Сегодня", "7 дней", "Сезон"]
    assert dict(report.organizer_sources) == {"direct": 1, "ref": 1}
    rendered = texts.stats(report)
    assert "выручка 490 ₽" in rendered and "Открытых жалоб: 0" in rendered


def test_season_start() -> None:
    assert analytics.season_start(date(2026, 11, 20)) == date(2026, 9, 1)
    assert analytics.season_start(date(2027, 1, 5)) == date(2026, 9, 1)
