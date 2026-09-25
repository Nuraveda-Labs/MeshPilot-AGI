"""Pluggable generation engines behind the shared `Engine` protocol: MUapi (image/video/text)
and HeyGen (avatar video). Resolve one by name with `get_engine`."""
from __future__ import annotations

from meshpilot.media.generation.engines.base import Engine, EngineError
from meshpilot.media.generation.engines.heygen import HeyGenEngine
from meshpilot.media.generation.engines.higgsfield import HiggsfieldEngine
from meshpilot.media.generation.engines.muapi import MuapiEngine

_ENGINES: dict[str, type] = {
    "muapi": MuapiEngine,
    "heygen": HeyGenEngine,
    "higgsfield": HiggsfieldEngine,
}


def get_engine(name: str) -> Engine:
    """Instantiate an engine by name (default 'muapi')."""
    cls = _ENGINES.get((name or "muapi").lower())
    if cls is None:
        raise EngineError(f"unknown engine {name!r}; known: {sorted(_ENGINES)}")
    return cls()  # type: ignore[return-value]


__all__ = ["Engine", "EngineError", "MuapiEngine", "HeyGenEngine", "HiggsfieldEngine", "get_engine"]
