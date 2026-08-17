"""Shared test fixtures.

Every test that touches the store gets a freshly seeded database, because a test that inherits
another test's state is exactly the kind of hidden coupling this project is about.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from racejack.config import RunnerConfig
from racejack.demo.client import StorefrontClient
from racejack.seed import seed


@pytest.fixture(scope="session")
def config() -> RunnerConfig:
    return RunnerConfig.from_env()


@pytest.fixture
async def fresh_state(config: RunnerConfig) -> None:
    await seed(config.database_url, create=True)


@pytest.fixture
async def storefront(config: RunnerConfig, fresh_state: None) -> AsyncIterator[StorefrontClient]:
    async with StorefrontClient(
        config.replica_urls, timeout=config.request_timeout_seconds
    ) as client:
        await client.wait_until_ready()
        yield client
