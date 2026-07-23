# TODO(anguelos): move to a general didip flask module
from typing import Tuple


def create_pagers(result_length: int, skip: int, item_count: int) -> Tuple[Tuple[int,int], Tuple[int,int], Tuple[int,int], Tuple[int,int], Tuple[int,int]]:
    """Creates REST API pagers.

    Args:
        result_length (int): total number of results
        skip (int): _description_
        item_count (int): _description_

    Returns:
        tuple[tuple[int, int], ...]: A tuple of tuples of the form (skip, item_count) for first, previous, current, following, and last pagers.
    """
    last_item = result_length - 1
    first = (0, item_count)
    prev = (max(skip - item_count, 0), item_count)
    current = (skip, item_count)
    following = (min(skip + item_count, last_item), item_count)
    last = (max(last_item - item_count, 0), item_count)

    return first, prev, current, following, last
