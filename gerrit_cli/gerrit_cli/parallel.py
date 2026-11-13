"""Run independent Gerrit fetches a few at a time.

Each REST call to review.whamcloud.com is a round trip of 70ms or
more, and much of the CLI's time went to making them one after another:
one per patch of a series, one per file a comment is on.
"""

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

# pygerrit2's connection pool holds 10.
WORKERS = 8


def parallel_map(fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
    """fn over items, up to WORKERS at once, results in input order.

    fn handles its own errors (an exception propagates when its result
    is reached) and must not share mutable state between calls; a
    client and its HTTP session may be shared.
    """
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))
