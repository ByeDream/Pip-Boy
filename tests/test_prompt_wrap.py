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

    def test_file_with_saved_path_hands_model_a_path(self):
        # Once the host has materialized the bytes to disk, the model
        # should get the relative path + a prod to use its native
        # tools. That's what turns "opaque zip" into "agent unzips it".
        inbound = InboundMessage(
            text="look at this", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="逆转裁判.zip",
                data=b"PK\x03\x04" + b"\x00" * 100,
                saved_path="incoming/20260422-012509-逆转裁判.zip",
            )],
        )
        out = _format_prompt(inbound, None)
        assert isinstance(out, list)
        file_block = next(
            b for b in out
            if b.get("type") == "text" and "saved to" in b.get("text", "")
        )
        text = file_block["text"]
        assert "逆转裁判.zip" in text
        assert "incoming/20260422-012509-逆转裁判.zip" in text
        assert "unzip" in text.lower()

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


class TestMaterializeAttachments:
    """``_materialize_attachments`` drops binary bytes to disk so the
    LLM can follow them with its native Read/Bash tools. This is what
    makes zip/doc/pdf uploads actionable instead of opaque.

    The incoming dir lives under the agent's ``.pip/``, so saved paths
    look like ``.pip/incoming/<ts>-<name>`` for the root agent and
    ``<project>/.pip/incoming/<ts>-<name>`` for sub-agents.
    """

    def _incoming_dir(self, workdir):
        # Mirrors the real caller: paths.incoming_dir -> <pip_dir>/incoming
        return workdir / ".pip" / "incoming"

    def test_binary_file_written_and_saved_path_set(self, tmp_path):
        from pip_agent.agent_host import _materialize_attachments

        payload = b"PK\x03\x04" + b"\x00" * 256
        inbound = InboundMessage(
            text="", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="archive.zip", data=payload,
            )],
        )
        incoming = self._incoming_dir(tmp_path)
        _materialize_attachments(
            inbound, workdir=tmp_path, incoming_dir=incoming,
        )
        att = inbound.attachments[0]
        assert att.saved_path.startswith(".pip/incoming/")
        assert att.saved_path.endswith("-archive.zip")
        # POSIX separators even on Windows — the model feeds this
        # straight into Bash ``unzip``, which doesn't grok backslashes.
        assert "\\" not in att.saved_path
        written = tmp_path / att.saved_path
        assert written.exists()
        assert written.read_bytes() == payload

    def test_text_file_not_written_twice(self, tmp_path):
        # If ``.text`` is already populated the file inlines cheaply in
        # the prompt; no need to occupy disk with a second copy.
        from pip_agent.agent_host import _materialize_attachments

        inbound = InboundMessage(
            text="", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="readme.md",
                data=b"hello", text="hello",
            )],
        )
        incoming = self._incoming_dir(tmp_path)
        _materialize_attachments(
            inbound, workdir=tmp_path, incoming_dir=incoming,
        )
        assert inbound.attachments[0].saved_path == ""
        assert not incoming.exists()

    def test_image_gets_extension_from_mime(self, tmp_path):
        # Images arrive without a filename on WeChat; we synthesize one
        # so the model can tell a jpg from a png when it reads the dir.
        from pip_agent.agent_host import _materialize_attachments

        inbound = InboundMessage(
            text="", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="image", data=b"\x89PNG\r\n\x1a\n",
                mime_type="image/png",
            )],
        )
        incoming = self._incoming_dir(tmp_path)
        _materialize_attachments(
            inbound, workdir=tmp_path, incoming_dir=incoming,
        )
        att = inbound.attachments[0]
        assert att.saved_path.endswith(".png")
        assert (tmp_path / att.saved_path).exists()

    def test_path_traversal_filename_is_sanitized(self, tmp_path):
        # A malicious / bugged channel could hand us a filename with
        # path separators. We must land strictly under the agent's
        # incoming dir regardless.
        from pip_agent.agent_host import _materialize_attachments

        inbound = InboundMessage(
            text="", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="../../etc/passwd",
                data=b"nope",
            )],
        )
        incoming = self._incoming_dir(tmp_path)
        _materialize_attachments(
            inbound, workdir=tmp_path, incoming_dir=incoming,
        )
        att = inbound.attachments[0]
        assert att.saved_path.startswith(".pip/incoming/")
        assert ".." not in att.saved_path
        assert "passwd" in att.saved_path

    def test_per_agent_isolation(self, tmp_path):
        # Two sub-agents, same filename, same second — the per-project
        # partitioning (alpha/.pip/incoming/, beta/.pip/incoming/) is
        # what stops them from clobbering each other.
        from pip_agent.agent_host import _materialize_attachments

        inb_a = InboundMessage(
            text="", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="notes.bin", data=b"A" * 10,
            )],
        )
        inb_b = InboundMessage(
            text="", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="notes.bin", data=b"B" * 10,
            )],
        )
        dir_a = tmp_path / "alpha" / ".pip" / "incoming"
        dir_b = tmp_path / "beta" / ".pip" / "incoming"
        _materialize_attachments(
            inb_a, workdir=tmp_path, incoming_dir=dir_a,
        )
        _materialize_attachments(
            inb_b, workdir=tmp_path, incoming_dir=dir_b,
        )
        a_path = tmp_path / inb_a.attachments[0].saved_path
        b_path = tmp_path / inb_b.attachments[0].saved_path
        assert a_path.read_bytes() == b"A" * 10
        assert b_path.read_bytes() == b"B" * 10
        assert "alpha" in inb_a.attachments[0].saved_path
        assert "beta" in inb_b.attachments[0].saved_path

    def test_oversized_attachment_skipped(self, tmp_path):
        from pip_agent.agent_host import (
            _MAX_INCOMING_BYTES,
            _materialize_attachments,
        )

        inbound = InboundMessage(
            text="", sender_id="u1", channel="wecom", peer_id="p1",
            attachments=[Attachment(
                type="file", filename="huge.bin",
                data=b"x" * (_MAX_INCOMING_BYTES + 1),
            )],
        )
        incoming = self._incoming_dir(tmp_path)
        _materialize_attachments(
            inbound, workdir=tmp_path, incoming_dir=incoming,
        )
        assert inbound.attachments[0].saved_path == ""
        assert not incoming.exists()

    def test_no_attachments_is_noop(self, tmp_path):
        from pip_agent.agent_host import _materialize_attachments

        inbound = InboundMessage(
            text="just text", sender_id="u1",
            channel="cli", peer_id="cli-user",
        )
        incoming = self._incoming_dir(tmp_path)
        _materialize_attachments(
            inbound, workdir=tmp_path, incoming_dir=incoming,
        )
        assert not incoming.exists()
