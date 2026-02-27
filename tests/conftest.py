"""Pytest fixtures for Slack Claude Code tests."""

import asyncio
import os

import pytest
import pytest_asyncio
from slack_sdk.web.async_client import AsyncWebClient

from src.hooks import HookRegistry


def pytest_addoption(parser):
    """Add custom command line options."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run live integration tests that require Slack credentials",
    )


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "live: marks tests as live integration tests (require Slack credentials)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip live tests unless --live flag is passed."""
    if config.getoption("--live"):
        return

    skip_live = pytest.mark.skip(reason="Need --live option to run live tests")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def clean_hook_registry():
    """Clear hook registry before and after each test."""
    HookRegistry.clear()
    yield
    HookRegistry.clear()


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def slack_bot_token() -> str:
    """Get Slack bot token from environment."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        pytest.skip("SLACK_BOT_TOKEN environment variable not set")
    return token


@pytest.fixture
def slack_user_token() -> str:
    """Get Slack user token from environment (required for live app endpoint tests)."""
    token = os.environ.get("SLACK_USER_TOKEN", "")
    if not token:
        pytest.skip("SLACK_USER_TOKEN environment variable not set")
    return token


@pytest.fixture
def slack_test_channel() -> str:
    """Get test channel ID from environment."""
    channel = os.environ.get("SLACK_TEST_CHANNEL", "")
    if not channel:
        pytest.skip("SLACK_TEST_CHANNEL environment variable not set")
    return channel


@pytest.fixture
def slack_client(slack_bot_token: str) -> AsyncWebClient:
    """Create an async Slack WebClient for live tests."""
    return AsyncWebClient(token=slack_bot_token)


@pytest.fixture
def slack_user_client(slack_user_token: str) -> AsyncWebClient:
    """Create an async Slack WebClient using a real user token."""
    return AsyncWebClient(token=slack_user_token)


@pytest_asyncio.fixture
async def slack_bot_user_id(slack_client: AsyncWebClient) -> str:
    """Resolve the bot user ID for mention tests."""
    response = await slack_client.auth_test()
    return response["user_id"]
