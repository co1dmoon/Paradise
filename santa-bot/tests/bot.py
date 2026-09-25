"""End-to-end driver: updates go through ``process_update`` exactly as the webhook sends them.

    bot = Bot(ctx, api)
    olga = bot.person(101, "Ольга")
    await bot(olga.start())
    await bot(olga.press("Согласен"))

Handler errors are normally swallowed by ``process_update`` (logged and sent to
the admins); here any such error fails the test at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app import repo
from app.context import AppContext
from app.core import texts
from app.core.models import Game
from app.updates import process_update
from tools.fake_max import FakeMaxApi, FakeUser, message_callback

ADMIN_ID = 9000


@dataclass
class Bot:
    ctx: AppContext
    api: FakeMaxApi
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        alert = self.ctx.alerts.alert

        async def recording(key: str, text: str, **kwargs: Any) -> bool:
            if key == "error":
                self.errors.append(text)
            return await alert(key, text, **kwargs)

        self.ctx.alerts.alert = recording  # type: ignore[method-assign]

    async def __call__(self, update: dict[str, Any]) -> None:
        await process_update(self.ctx, update)
        assert not self.errors, self.errors
        if update.get("update_type") == "message_callback":
            assert self.api.answered(update["callback"]["callback_id"]), "callback was not answered"

    def person(self, user_id: int, name: str) -> FakeUser:
        return FakeUser(self.api, user_id, name)

    async def forge(self, user: FakeUser, payload: str) -> None:
        """A callback with an arbitrary payload, as a stale or tampered button would send."""
        if self.api.messages_to(user.user_id):
            await self(self.api.forge(user.user_id, payload, name=user.name))
        else:
            await self(message_callback(user.user_id, payload, name=user.name))

    async def drain(self) -> None:
        await self.ctx.outbox.drain()

    async def onboard(self, user: FakeUser, payload: str | None = None) -> None:
        await self(user.start(payload))
        await self(user.press(texts.BTN_CONSENT))

    async def create_game(self, organizer: FakeUser, title: str = "Семья Ивановых", *,
                          participates: bool = True) -> Game:
        """Run the §5.2 wizard: title, the first budget preset, 'Пока не знаю', participation."""
        await self(organizer.press(texts.BTN_CREATE_GAME))
        await self(organizer.say(title))
        await self(organizer.press(texts.BUDGET_PRESETS[0]))
        await self(organizer.press(texts.BTN_DATE_UNKNOWN))
        await self(organizer.press(texts.BTN_I_PARTICIPATE if participates else texts.BTN_ONLY_ORGANIZE))
        games = await repo.games_of_user(self.ctx.db, organizer.user_id)
        return games[0][0]

    async def join(self, user: FakeUser, game: Game, wishes: str | None = "Книгу и носки") -> None:
        """A new user opens the invite link, consents and (optionally) writes wishes."""
        await self.onboard(user, f"j_{game.code}")
        if wishes is not None:
            await self(user.say(wishes))

    async def game(self, game_id: int) -> Game:
        game = await repo.get_game(self.ctx.db, game_id)
        assert game is not None
        return game
