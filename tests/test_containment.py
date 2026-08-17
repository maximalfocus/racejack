"""The demonstration is contained: no egress, no published port, no persistent state.

Containment is a safety property, so it is asserted rather than asserted-in-prose. Two of these
tests run inside the container network and prove the runtime boundary; the rest read
the Compose file that defines it, so a future edit that publishes a port or opens the network fails
here instead of in review.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


@pytest.fixture(scope="session")
def compose() -> dict[str, Any]:
    if not COMPOSE_FILE.is_file():
        pytest.skip(f"compose file is not present in this image: {COMPOSE_FILE}")
    loaded = yaml.safe_load(COMPOSE_FILE.read_text())
    assert isinstance(loaded, dict)
    return loaded


async def test_the_network_has_no_egress() -> None:
    """A TCP connection to a public address must not succeed from inside the demo network."""
    with pytest.raises(OSError):
        _, writer = await asyncio.wait_for(asyncio.open_connection("1.1.1.1", 443), timeout=5)
        writer.close()


async def test_public_names_do_not_resolve() -> None:
    loop = asyncio.get_running_loop()
    with pytest.raises((OSError, socket.gaierror)):
        await asyncio.wait_for(loop.getaddrinfo("example.invalid.racejack-demo", 443), timeout=5)


def test_the_demo_network_is_internal(compose: dict[str, Any]) -> None:
    assert compose["networks"]["demo"]["internal"] is True


def test_no_service_publishes_a_port(compose: dict[str, Any]) -> None:
    published = {
        name: service["ports"]
        for name, service in compose["services"].items()
        if service.get("ports")
    }
    assert published == {}, f"services publish ports: {published}"


def test_every_service_is_hardened(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        assert service.get("cap_drop") == ["ALL"], f"{name} does not drop all capabilities"
        assert "no-new-privileges:true" in service.get("security_opt", []), (
            f"{name} allows privilege escalation"
        )
        assert service.get("read_only") is True, f"{name} has a writable root filesystem"


def test_every_service_runs_non_root(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        user = str(service.get("user", ""))
        assert user, f"{name} does not pin a non-root user"
        assert not user.startswith("0:"), f"{name} runs as root"


def test_the_only_host_path_the_demo_touches_is_its_own_artifacts_directory(
    compose: dict[str, Any],
) -> None:
    """A bind mount is a hole in the containment boundary; the only one is our own scratch dir."""
    mounts = {
        name: service["volumes"]
        for name, service in compose["services"].items()
        if service.get("volumes")
    }
    assert mounts == {
        "harness": ["./artifacts:/artifacts"],
        "compare": ["./artifacts:/artifacts"],
    }, f"unexpected host mounts: {mounts}"


def test_the_database_holds_no_persistent_state(compose: dict[str, Any]) -> None:
    db = compose["services"]["db"]
    assert not db.get("volumes"), "the database must not be backed by a persistent volume"
    tmpfs = " ".join(db["tmpfs"])
    assert "/var/lib/postgresql/data" in tmpfs, "the data directory must be disposable"
