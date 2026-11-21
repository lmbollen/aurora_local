"""Library tests use real sockets on 127.0.0.1 (fake heat pump modules).

The Home Assistant test plugin, when installed, blocks sockets for every
test; allow them here."""

from __future__ import annotations

import pytest

try:
    import pytest_socket  # noqa: F401  (comes with the Home Assistant test harness)
except ImportError:  # plain library environment: sockets are allowed anyway
    pass
else:
    @pytest.fixture(autouse=True)
    def _allow_sockets(socket_enabled):  # noqa: PT004
        yield
