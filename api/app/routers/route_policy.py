"""Read-only route policy checks (order/trade path guard)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, WebSocketRoute

FORBIDDEN_PATH_SUBSTRINGS = (
    'order',
    'trade',
    'position',
    'execut',
    'buy',
    'sell',
    'submit',
    'place',
    'leverage',
    'margin',
    'withdraw',
    'transfer',
)


def _flatten_routes(routes: Iterable[BaseRoute]) -> list[BaseRoute]:
    out: list[BaseRoute] = []
    for route in routes:
        if type(route).__name__ == '_IncludedRouter':
            out.extend(_flatten_routes(route.original_router.routes))  # type: ignore[attr-defined]
        elif hasattr(route, 'routes'):
            out.extend(_flatten_routes(route.routes))  # type: ignore[attr-defined]
        else:
            out.append(route)
    return out


def forbidden_substrings_in_path(path: str) -> list[str]:
    lower = path.lower()
    return [s for s in FORBIDDEN_PATH_SUBSTRINGS if s in lower]


def audit_app_routes(app: Any) -> list[str]:
    """Return human-readable violations; empty means OK."""
    problems: list[str] = []
    http_seen = 0
    ws_seen = 0
    for route in _flatten_routes(app.routes):
        path = getattr(route, 'path', None) or ''
        if isinstance(route, APIRoute):
            http_seen += 1
            methods = set(route.methods or set()) - {'HEAD', 'OPTIONS'}
            if not methods <= {'GET'}:
                problems.append(f'{path}: disallowed methods {sorted(methods)}')
            for sub in forbidden_substrings_in_path(path):
                problems.append(f'{path}: forbidden substring {sub!r}')
        elif isinstance(route, WebSocketRoute):
            ws_seen += 1
            for sub in forbidden_substrings_in_path(path):
                problems.append(f'{path}: forbidden substring {sub!r}')
    if ws_seen != 1:
        problems.append(f'expected exactly one WebSocket route, found {ws_seen}')
    schema = app.openapi()
    for path, item in schema.get('paths', {}).items():
        for sub in forbidden_substrings_in_path(path):
            problems.append(f'openapi {path}: forbidden substring {sub!r}')
        for method, op in item.items():
            if method.startswith('x-'):
                continue
            if method.lower() != 'get':
                problems.append(f'openapi {method.upper()} {path} is not GET')
            if 'requestBody' in op:
                problems.append(f'openapi {method.upper()} {path} has requestBody')
    if http_seen == 0:
        problems.append('no HTTP APIRoute instances found')
    return problems
