"""Tests for ``agent_host._format_prompt`` — user / cron / heartbeat wrapping
plus multimodal (image / file / voice) attachment rendering.
"""

from __future__ import annotations

import base64

from pip_agent.agent_host import _format_prompt
from pip_agent.channels import Attachment, InboundMessage


class TestFormatPrompt:
    def test_cli_user_passes_through(self):
        inbound = InboundMessage(
            text="hello", sender_id="cli-user", channel="cli", peer_id="cli-user",
        )
        assert _format_prompt(inbound, None) == "hello"

    def test_cron_sentinel_wraps_regardless_of_channel(self):
        inbound = InboundMessage(
            text="summarize news", sender_id="__cron__",
            channel="cli", peer_id="cli-user", agent_id="pip-boy",
        )
        out = _format_prompt(inbound, None)
        assert out.startswith("<cron_task>")
        assert out.endswith("</cron_task>")
        assert "summarize news" in out

    def test_heartbeat_sentinel_wraps_regardless_of_channel(self):
        inbound = InboundMessage(
            text="still alive", sender_id="__heartbeat__",
            channel="cli", peer_id="cli-user", agent_id="pip-boy",
        )
        out = _format_prompt(inbound, None)
        assert out.startswith("<heartbeat>")
        assert out.endswith("</heartbeat>")
        assert "still alive" in out

    def test_remote_channel_wraps_user_query(self):
        inbound = InboundMessage(
            text="hi bot", sender_id="u123", channel="wechat",
            peer_id="u123", is_group=False,
        )
        out = _format_prompt(inbound, None)
        assert "<user_query" in out
        assert 'from="wechat:u123"' in out
        assert 'status="unverified"' in out

    def test_remote_group_includes_group_attr(self):
        inbound = InboundMessage(
            text="hi bot", sender_id="u123", channel="wecom",
            peer_id="g1", guild_id="g1", is_group=True,
        )
        out = _format_prompt(inbound, None)
        assert 'group="true"' in out

    def test_leading_at_mention_stripped(self):
        inbound = InboundMessage(
            text="@Pip hey", sender_id="u1", channel="wechat",
            peer_id="p1",
        )
        out = _format_prompt(inbound, None)
        assert "@Pip" not in out
        assert "hey" in out


# ---------------------------------------------------------------------------
# Multimodal (Phase 7): attachments flip the return type from str to
# list[dict]. The blocks must follow the Anthropic content-block shape
# so the SDK's AsyncIterable path accepts them.
# ---------------------------------------------------------------------------


class TestAttachmentBlocks:
    def test_no_attachments_returns_str(self):
        inbound = InboundMessage(
            text="hi", sender_id="u1", channel="wechat", peer_id="p1",
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, str)

    def test_single_image_produces_blocks(self):
        img_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        inbound = InboundMessage(
            text="look", sender_id="u1", channel="wechat", peer_id="p1",
            attachments=[Attachment(
                type="image", data=img_bytes, mime_type="image/png",
            )],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        assert len(out) == 2
        # Text block first — the LLM should see the caption before
        # being asked to interpret the pixels.
        assert out[0]["type"] == "text"
        assert "look" in out[0]["text"]
        # Image block shape must exactly match Anthropic's contract.
        assert out[1]["type"] == "image"
        src = out[1]["source"]
        assert src["type"] == "base64"
        assert src["media_type"] == "image/png"
        assert base64.b64decode(src["data"]) == img_bytes

    def test_image_without_mime_defaults_to_jpeg(self):
        # Fallback exists because some WeCom payloads lose mime-type
        # during the quote-normalisation pass upstream. Better than
        # dropping the image.
        inbound = InboundMessage(
            text="", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(type="image", data=b"\xff\xd8\xff\xe0fake")],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        image_blocks = [b for b in out if b.get("type") == "image"]
        assert image_blocks
        assert image_blocks[0]["source"]["media_type"] == "image/jpeg"

    def test_image_without_bytes_becomes_text_placeholder(self):
        # Channel saw an image but failed to pull bytes — we must
        # still signal "there was an image" to the LLM, not silently
        # drop it.
        inbound = InboundMessage(
            text="see this", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(type="image", data=None, text="[Image]")],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        assert any("[Image]" in b.get("text", "") for b in out)
        assert not any(b.get("type") == "image" for b in out)

    def test_file_with_text_becomes_attached_file_wrapper(self):
        inbound = InboundMessage(
            text="review this", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="spec.md", text="# Spec\nContent.",
            )],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        file_block = next(
            b for b in out
            if b.get("type") == "text" and "<attached-file" in b.get("text", "")
        )
        assert 'name="spec.md"' in file_block["text"]
        assert "# Spec" in file_block["text"]

    def test_binary_file_without_text_becomes_placeholder(self):
        inbound = InboundMessage(
            text="got it", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="binary.zip", data=b"PK\x03\x04",
            )],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        assert any(
            "[File: binary.zip]" in b.get("text", "") for b in out
        )

    def test_voice_transcription_becomes_text(self):
        inbound = InboundMessage(
            text="", sender_id="u1", channel="wechat", peer_id="p1",
            attachments=[Attachment(
                type="voice", text="remind me at 3 pm",
            )],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        assert any(
            "[Voice transcription]: remind me at 3 pm" in b.get("text", "")
            for b in out
        )

    def test_voice_without_transcription_still_marks_presence(self):
        inbound = InboundMessage(
            text="", sender_id="u1", channel="wechat", peer_id="p1",
            attachments=[Attachment(type="voice", text="")],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        assert any("[Voice message]" in b.get("text", "") for b in out)

    def test_mixed_attachments_preserve_order(self):
        img_bytes = b"\x89PNGfake"
        inbound = InboundMessage(
            text="batch", sender_id="u1", channel="wechat", peer_id="p1",
            attachments=[
                Attachment(
                    type="image", data=img_bytes, mime_type="image/png",
                ),
                Attachment(
                    type="file", filename="notes.txt", text="hello",
                ),
            ],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        # First: caption text, then: image, then: attached-file.
        assert out[0]["type"] == "text" and "batch" in out[0]["text"]
        assert out[1]["type"] == "image"
        assert out[2]["type"] == "text" and "<attached-file" in out[2]["text"]

    def test_empty_text_with_image_only_omits_text_block(self):
        # No caption means no leading text block — we should NOT emit
        # an empty ``{"type": "text", "text": ""}`` because that's an
        # invalid Anthropic block and an unnecessary payload.
        inbound = InboundMessage(
            text="", sender_id="u1", channel="cli", peer_id="cli-user",
            attachments=[Attachment(
                type="image", data=b"\x89PNG", mime_type="image/png",
            )],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        assert all(b.get("text", "non-empty") != "" for b in out)
        assert out[0]["type"] == "image"

    def test_unrenderable_attachment_preserves_text_prompt(self):
        # Unknown attachment type should not take down the inbound —
        # the text prompt must still reach the LLM. Either shape is
        # acceptable (list with a single text block, or bare str);
        # what matters is the LLM sees "hello".
        inbound = InboundMessage(
            text="hello", sender_id="u1", channel="cli", peer_id="cli-user",
            attachments=[Attachment(type="mystery")],
        )
        out = _format_prompt(inbound, None)
        if isinstance(out, list):
            joined = "".join(b.get("text", "") for b in out if b.get("type") == "text")
            assert "hello" in joined
        else:
            assert "hello" in out

    def test_only_unrenderable_with_no_text_falls_back_to_str(self):
        # Edge case the code handles defensively: zero text AND zero
        # renderable attachments → blocks is empty → we must return
        # the (possibly empty) text instead of handing the SDK an
        # invalid empty block list.
        inbound = InboundMessage(
            text="", sender_id="u1", channel="cli", peer_id="cli-user",
            attachments=[Attachment(type="mystery")],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, str)
