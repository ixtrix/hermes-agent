"""Runtime seam for handlers moved out of ``server.py``.

The split modules keep their own function globals.  A registered handler
refreshes the names it uses from the owning server module immediately before
each call, so monkeypatched callbacks and live runtime hooks remain visible
without rebuilding handlers against a copied globals dictionary.
"""

from __future__ import annotations

import dis
from functools import wraps
from types import CodeType
from typing import Any, Callable


def _code_tree(code: CodeType):
    """Yield a function code object and nested function code objects."""
    yield code
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield from _code_tree(value)


class HandlerRegistry:
    """Deferred ``@method`` registrar used by the ``methods_*`` modules."""

    def __init__(self) -> None:
        self._pending: list[tuple[str, Callable[..., Any]]] = []

    def method(self, name: str):
        """Drop-in for the server's ``@method`` decorator."""

        def dec(fn):
            self._pending.append((name, fn))
            return fn

        return dec

    def profile_scoped(self, fn):
        """Drop-in for the server's ``@_profile_scoped`` decorator."""
        fn._hermes_profile_scoped = True
        return fn

    @staticmethod
    def _global_names(fn: Callable[..., Any]) -> tuple[set[str], set[str]]:
        names: set[str] = set()
        writes: set[str] = set()
        for code in _code_tree(fn.__code__):
            names.update(code.co_names)
            writes.update(
                str(instruction.argval)
                for instruction in dis.get_instructions(code)
                if instruction.opname in {"STORE_GLOBAL", "DELETE_GLOBAL"}
            )
        return names, writes

    def _refresh(
        self, server, fn: Callable[..., Any]
    ) -> tuple[dict[str, Any], set[str]]:
        """Expose current server globals to a split handler module."""
        module_globals = fn.__globals__
        server_globals = vars(server)
        pending = [fn]
        seen: set[int] = set()
        names: set[str] = set()
        writes: set[str] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            current_names, current_writes = self._global_names(current)
            names.update(current_names)
            writes.update(current_writes)
            for name in current_names:
                if name.startswith("__"):
                    continue
                if name in server_globals:
                    module_globals[name] = server_globals[name]
                candidate = module_globals.get(name)
                if (
                    callable(candidate)
                    and getattr(candidate, "__globals__", None) is module_globals
                ):
                    pending.append(candidate)
        bound_writes = {name for name in writes if name in server_globals}
        return module_globals, bound_writes

    def refresh(self, server, fn: Callable[..., Any]) -> None:
        """Refresh globals for a helper called outside a registered method."""
        self._refresh(server, fn)

    @staticmethod
    def _sync_writes(
        server, module_globals: dict[str, Any], writes: set[str]
    ) -> None:
        for name in writes:
            if name in module_globals:
                setattr(server, name, module_globals[name])

    def _bound(self, server, fn: Callable[..., Any]):
        @wraps(fn)
        def invoke(*args, **kwargs):
            module_globals, writes = self._refresh(server, fn)
            try:
                return fn(*args, **kwargs)
            finally:
                self._sync_writes(server, module_globals, writes)

        return invoke

    def install(self, server) -> None:
        """Register wrappers that resolve server globals at call time."""
        for name, fn in self._pending:
            real = self._bound(server, fn)
            if getattr(fn, "_hermes_profile_scoped", False):
                real = server._profile_scoped(real)
            server._methods[name] = real
