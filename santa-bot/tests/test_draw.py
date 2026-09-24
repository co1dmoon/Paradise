from __future__ import annotations

import random
import time

import pytest

from app.core.draw import draw_cycle


def assert_single_cycle(pairs: dict[int, int], ids: list[int], excluded: set[frozenset[int]]) -> None:
    assert set(pairs) == set(ids)
    assert set(pairs.values()) == set(ids)
    seen, current = [], ids[0]
    for _ in ids:
        seen.append(current)
        current = pairs[current]
    assert current == ids[0] and len(set(seen)) == len(ids), "must be one cycle through everybody"
    for giver, receiver in pairs.items():
        assert giver != receiver
        assert pairs[receiver] != giver or len(ids) < 3, "no two people gift each other"
        assert frozenset((giver, receiver)) not in excluded


def random_exclusions(ids: list[int], rng: random.Random) -> set[frozenset[int]]:
    count = rng.randint(0, len(ids) // 3)
    return {frozenset(rng.sample(ids, 2)) for _ in range(count)}


@pytest.mark.parametrize("n", range(3, 61))
def test_single_valid_cycle_with_random_exclusions(n: int) -> None:
    rng = random.Random(n)
    ids = [1000 + i for i in range(n)]
    excluded = random_exclusions(ids, rng)
    pairs = draw_cycle(ids, excluded, rng)
    if n == 3 and excluded:
        assert pairs is None
        return
    assert pairs is not None
    assert_single_cycle(pairs, ids, excluded)


@pytest.mark.parametrize("pair", [(1, 2), (1, 3), (2, 3)])
def test_three_people_with_any_exclusion_is_impossible(pair: tuple[int, int]) -> None:
    assert draw_cycle([1, 2, 3], {frozenset(pair)}, random.Random(1)) is None


def test_fewer_than_three_people_cannot_draw() -> None:
    assert draw_cycle([1, 2], set(), random.Random(1)) is None
    assert draw_cycle([1], set(), random.Random(1)) is None


def test_three_hundred_people_under_one_second() -> None:
    rng = random.Random(300)
    ids = list(range(1, 301))
    excluded = random_exclusions(ids, rng)
    started = time.perf_counter()
    pairs = draw_cycle(ids, excluded, random.SystemRandom())
    assert time.perf_counter() - started < 1.0
    assert pairs is not None
    assert_single_cycle(pairs, ids, excluded)


def test_backtracking_fallback_finds_a_tight_solution() -> None:
    # Person 1 may only sit next to 2 and 3: shuffling rarely finds it, the search must.
    ids = list(range(1, 9))
    excluded = {frozenset((1, other)) for other in range(4, 9)}
    pairs = draw_cycle(ids, excluded, random.Random(5), attempts=0)
    assert pairs is not None
    assert_single_cycle(pairs, ids, excluded)
    assert {pairs[1], next(g for g, r in pairs.items() if r == 1)} == {2, 3}


def test_search_gives_up_when_the_time_budget_is_spent() -> None:
    ticks = iter(range(1_000_000))
    ids = list(range(1, 13))
    # Two groups that may only mix across a single bridge person: no Hamiltonian cycle exists.
    left, right = ids[:6], ids[6:]
    excluded = {frozenset((a, b)) for a in left[1:] for b in right}
    result = draw_cycle(ids, excluded, random.Random(3), attempts=0, time_budget=5, clock=lambda: next(ticks))
    assert result is None


def test_duplicate_ids_and_foreign_exclusions_are_ignored() -> None:
    pairs = draw_cycle([1, 2, 3, 4, 4], {frozenset((7, 8)), frozenset((1,))}, random.Random(2))
    assert pairs is not None
    assert_single_cycle(pairs, [1, 2, 3, 4], set())
