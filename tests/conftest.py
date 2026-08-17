"""Shared test fixtures.

Every test that touches the store gets a freshly seeded database, because a test that inherits
another test's state is exactly the kind of hidden coupling this project is about.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest

from racejack.config import RunnerConfig
from racejack.demo.client import StorefrontClient
from racejack.seed import prepare_secure_run, prepare_vulnerable_run


@pytest.fixture(scope="session")
def config() -> RunnerConfig:
    return RunnerConfig.from_env()


@pytest.fixture
async def fresh_state(config: RunnerConfig) -> None:
    """The secure baseline: fresh fixtures, both backstops in place, no instrumentation.

    Restoring rather than merely reseeding matters, because a vulnerable test earlier in the run
    will have deliberately removed the backstop constraints.
    """
    await prepare_secure_run(config.database_url)


@pytest.fixture
async def vulnerable_state(config: RunnerConfig) -> AsyncIterator[None]:
    """Fresh fixtures with the backstop removed, then the secure baseline put back afterwards."""
    await prepare_vulnerable_run(config.database_url)
    yield
    await prepare_secure_run(config.database_url)


@pytest.fixture
def vulnerable_urls(config: RunnerConfig) -> tuple[str, ...]:
    """The vulnerable replicas, or a skip when its opt-in profile is not running."""
    urls = config.vulnerable_replica_urls
    if not urls:
        pytest.skip("no vulnerable replicas configured")
    if not _vulnerable_is_reachable(urls[0]):
        if os.environ.get("RACEJACK_REQUIRE_VULNERABLE"):
            pytest.fail(
                f"RACEJACK_REQUIRE_VULNERABLE is set but {urls[0]} is not reachable; "
                "the vulnerable profile must be running for this suite"
            )
        pytest.skip("the vulnerable opt-in profile is not running")
    return urls


def _vulnerable_is_reachable(url: str) -> bool:
    try:
        with httpx.Client(timeout=2.0) as client:
            return client.get(f"{url}/healthz").status_code == httpx.codes.OK
    except httpx.HTTPError:
        return False


@pytest.fixture
async def storefront(config: RunnerConfig, fresh_state: None) -> AsyncIterator[StorefrontClient]:
    async with StorefrontClient(
        config.replica_urls, timeout=config.request_timeout_seconds
    ) as client:
        await client.wait_until_ready()
        yield client
