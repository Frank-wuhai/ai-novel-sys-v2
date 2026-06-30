from __future__ import annotations

from collections import OrderedDict
from typing import Callable, TypeVar


T = TypeVar("T")
_CACHE: OrderedDict[tuple[object, ...], object] = OrderedDict()
_MAX_ITEMS = 64


def cached_production_value(key: tuple[object, ...], factory: Callable[[], T]) -> T:
    if key in _CACHE:
        value = _CACHE.pop(key)
        _CACHE[key] = value
        return value  # type: ignore[return-value]
    value = factory()
    _CACHE[key] = value
    while len(_CACHE) > _MAX_ITEMS:
        _CACHE.popitem(last=False)
    return value


def clear_production_cache() -> None:
    _CACHE.clear()
