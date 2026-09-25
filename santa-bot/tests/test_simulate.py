"""§14: the simulator plays the whole office game (the owner's demo doubles as a smoke test)."""

from __future__ import annotations

from tools import simulate


async def test_simulator_plays_the_whole_office_game_deterministically() -> None:
    first: list[str] = []
    await simulate.run(write=first.append)
    transcript = "\n".join(first)
    for step in (
        "Тайный Санта «Отдел продаж»",
        "В игру «Отдел продаж» вступили: Иван Петров, Мария (бухгалтерия)",
        "Мест нет: в бесплатной игре до 10 участников.",
        "В игру «Отдел продаж» хотят вступить ещё 2 чел.",
        "сайт → Robokassa: OK1",
        "Оплата получена, спасибо! Игра «Отдел продаж» теперь до 30 участников.",
        "Записал: Иван Петров и Мария (бухгалтерия) не будут дарить друг другу.",
        "Готово! Пары отправлены 12 из 12.",
        "Сообщение от вашего Тайного Санты (игра «Отдел продаж»): «Привет! Ты больше любишь чай или кофе?»",
        "— человека, которому вы дарите: «Кофе, и побольше! Спасибо, Санта :)»",
        "Завтра обмен подарками в игре «Отдел продаж».",
        "Пары: 12 из 12, Иван и Мария друг другу не дарят",
    ):
        assert step in transcript, step
    assert "Я понимаю кнопки" not in transcript, "every typed message was understood"

    second: list[str] = []
    await simulate.run(write=second.append)
    assert second == first
