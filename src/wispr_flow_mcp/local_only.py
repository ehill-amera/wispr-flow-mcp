"""Refuses outbound network connections in this process."""

from __future__ import annotations

import socket
from typing import Any


def refuse_network() -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("wispr-flow-mcp is local only: network connections are disabled")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
