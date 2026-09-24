"""The draw (§5.5): one random cycle through all participants.

A single cycle guarantees nobody gifts themselves and, with at least three
people, no two people gift each other. Excluded pairs may not be adjacent in
the cycle in either direction. Production passes ``random.SystemRandom()``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Collection, Sequence

MIN_PARTICIPANTS = 3
SHUFFLE_ATTEMPTS = 5000
SEARCH_BUDGET_SECONDS = 2.0
_DEADLINE_CHECK_EVERY = 1024


def draw_cycle(
    ids: Sequence[int],
    excluded: Collection[frozenset[int]],
    rng: random.Random,
    *,
    attempts: int = SHUFFLE_ATTEMPTS,
    time_budget: float = SEARCH_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[int, int] | None:
    """Return {giver: receiver} forming one cycle, or None if no valid cycle was found.

    Shuffle-and-test first (``attempts`` times); then a randomized backtracking
    search limited to ``time_budget`` seconds.
    """
    people = list(dict.fromkeys(ids))
    if len(people) < MIN_PARTICIPANTS:
        return None
    blocked_with = _blocked_neighbours(people, excluded)
    if any(len(people) - 1 - len(blocked_with[p]) < 2 for p in people):
        return None  # everyone needs a giver and a receiver among the allowed people
    for _ in range(attempts):
        rng.shuffle(people)
        if _is_valid_cycle(people, blocked_with):
            return _as_mapping(people)
    return _search(people, blocked_with, rng, deadline=clock() + time_budget, clock=clock)


def _blocked_neighbours(people: list[int], excluded: Collection[frozenset[int]]) -> dict[int, set[int]]:
    blocked_with: dict[int, set[int]] = {person: set() for person in people}
    for pair in excluded:
        if len(pair) != 2:
            continue
        first, second = tuple(pair)
        if first in blocked_with and second in blocked_with:
            blocked_with[first].add(second)
            blocked_with[second].add(first)
    return blocked_with


def _is_valid_cycle(order: list[int], blocked_with: dict[int, set[int]]) -> bool:
    return all(order[i - 1] not in blocked_with[order[i]] for i in range(len(order)))


def _as_mapping(order: list[int]) -> dict[int, int]:
    return {giver: order[(i + 1) % len(order)] for i, giver in enumerate(order)}


def _search(
    people: list[int],
    blocked_with: dict[int, set[int]],
    rng: random.Random,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> dict[int, int] | None:
    """Randomized depth-first search for a Hamiltonian cycle in the 'allowed' graph."""
    start = rng.choice(people)
    path = [start]
    used = {start}

    def options(last: int) -> list[int]:
        found = [p for p in people if p not in used and p not in blocked_with[last]]
        rng.shuffle(found)
        return found

    frames = [options(start)]
    steps = 0
    while frames:
        steps += 1
        if steps % _DEADLINE_CHECK_EVERY == 0 and clock() > deadline:
            return None
        remaining = frames[-1]
        if not remaining:
            frames.pop()
            used.discard(path.pop())
            continue
        candidate = remaining.pop()
        if len(path) + 1 == len(people):
            if start not in blocked_with[candidate]:
                return _as_mapping([*path, candidate])
            continue
        path.append(candidate)
        used.add(candidate)
        frames.append(options(candidate))
    return None
