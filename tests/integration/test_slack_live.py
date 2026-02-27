"""Live Slack integration tests.

These tests actually interact with the Slack API and require:
- SLACK_BOT_TOKEN: Bot token with appropriate scopes
- SLACK_TEST_CHANNEL: Channel ID where test messages will be posted

Run with: pytest tests/ --live
"""

import pytest
from slack_sdk.web.async_client import AsyncWebClient


@pytest.mark.live
@pytest.mark.asyncio
async def test_post_message(slack_client: AsyncWebClient, slack_test_channel: str):
    """Test posting a simple message to Slack."""
    response = await slack_client.chat_postMessage(
        channel=slack_test_channel,
        text="[Live Test] Simple message test",
    )

    assert response["ok"] is True
    assert response["message"]["text"] == "[Live Test] Simple message test"
    message_ts = response["ts"]

    # Cleanup: delete the test message
    await slack_client.chat_delete(channel=slack_test_channel, ts=message_ts)


@pytest.mark.live
@pytest.mark.asyncio
async def test_update_message(slack_client: AsyncWebClient, slack_test_channel: str):
    """Test posting and then updating a message."""
    # Post initial message
    response = await slack_client.chat_postMessage(
        channel=slack_test_channel,
        text="[Live Test] Original message",
    )
    assert response["ok"] is True
    message_ts = response["ts"]

    # Update the message
    update_response = await slack_client.chat_update(
        channel=slack_test_channel,
        ts=message_ts,
        text="[Live Test] Updated message",
    )
    assert update_response["ok"] is True
    assert update_response["message"]["text"] == "[Live Test] Updated message"

    # Cleanup
    await slack_client.chat_delete(channel=slack_test_channel, ts=message_ts)


@pytest.mark.live
@pytest.mark.asyncio
async def test_post_thread_reply(slack_client: AsyncWebClient, slack_test_channel: str):
    """Test posting a reply in a thread."""
    # Post parent message
    parent_response = await slack_client.chat_postMessage(
        channel=slack_test_channel,
        text="[Live Test] Parent message for thread test",
    )
    assert parent_response["ok"] is True
    parent_ts = parent_response["ts"]

    # Post reply in thread
    reply_response = await slack_client.chat_postMessage(
        channel=slack_test_channel,
        thread_ts=parent_ts,
        text="[Live Test] Thread reply",
    )
    assert reply_response["ok"] is True
    assert reply_response["message"]["thread_ts"] == parent_ts

    # Cleanup: delete both messages
    await slack_client.chat_delete(channel=slack_test_channel, ts=reply_response["ts"])
    await slack_client.chat_delete(channel=slack_test_channel, ts=parent_ts)


@pytest.mark.live
@pytest.mark.asyncio
async def test_post_blocks(slack_client: AsyncWebClient, slack_test_channel: str):
    """Test posting a message with Block Kit blocks."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "[Live Test] Block Kit Message",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Bold text* and _italic text_",
            },
        },
        {
            "type": "divider",
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Field 1:*\nValue 1"},
                {"type": "mrkdwn", "text": "*Field 2:*\nValue 2"},
            ],
        },
    ]

    response = await slack_client.chat_postMessage(
        channel=slack_test_channel,
        text="[Live Test] Block Kit Message",  # Fallback text
        blocks=blocks,
    )

    assert response["ok"] is True
    assert len(response["message"]["blocks"]) == 4
    message_ts = response["ts"]

    # Cleanup
    await slack_client.chat_delete(channel=slack_test_channel, ts=message_ts)


@pytest.mark.live
@pytest.mark.asyncio
async def test_file_upload(slack_client: AsyncWebClient, slack_test_channel: str):
    """Test uploading a file snippet."""
    content = "print('Hello from live test!')\n# This is a test file"

    response = await slack_client.files_upload_v2(
        channel=slack_test_channel,
        content=content,
        filename="live_test.py",
        title="[Live Test] Code Snippet",
        initial_comment="Testing file upload functionality",
    )

    assert response["ok"] is True
    file_info = response["file"]
    assert file_info["name"] == "live_test.py"
    assert file_info["title"] == "[Live Test] Code Snippet"

    # Cleanup: delete the uploaded file
    await slack_client.files_delete(file=file_info["id"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_post_message_with_context_blocks(
    slack_client: AsyncWebClient, slack_test_channel: str
):
    """Test posting a message with context blocks (used for metadata display)."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "[Live Test] Message with context",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": ":clock1: Duration: 1.5s"},
                {"type": "mrkdwn", "text": ":moneybag: Cost: $0.05"},
            ],
        },
    ]

    response = await slack_client.chat_postMessage(
        channel=slack_test_channel,
        text="[Live Test] Message with context",
        blocks=blocks,
    )

    assert response["ok"] is True
    message_ts = response["ts"]

    # Cleanup
    await slack_client.chat_delete(channel=slack_test_channel, ts=message_ts)


@pytest.mark.live
@pytest.mark.asyncio
async def test_auth_test(slack_client: AsyncWebClient):
    """Test that the Slack client can authenticate successfully."""
    response = await slack_client.auth_test()

    assert response["ok"] is True
    assert "user_id" in response
    assert "team_id" in response
    assert "bot_id" in response
