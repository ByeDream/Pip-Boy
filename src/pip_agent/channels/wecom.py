"""WeCom (企业微信) smart-bot WebSocket channel.

Uses the official ``wecom-aibot-python-sdk`` package. Bundles the
media-upload pipeline (init/chunk/finish), the ``_pending_frames``
table that threads ``reply_stream`` calls back to the correct inbound,
and the background WS loop.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import uuid
from typing import Any

from pip_agent.channels.base import (
    Attachment,
    Channel,
    InboundMessage,
    _detect_image_mime,
)

log = logging.getLogger(__name__)

try:
    from aibot import WSClient, WSClientOptions, generate_req_id  # type: ignore[import-untyped]
    _HAS_WECOM_SDK = True
except ImportError:
    _HAS_WECOM_SDK = False


class WecomChannel(Channel):
    """WeCom (企业微信) smart-bot channel via official WebSocket SDK."""

    name = "wecom"

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        msg_queue: list[InboundMessage],
        q_lock: threading.Lock,
    ) -> None:
        if not _HAS_WECOM_SDK:
            raise RuntimeError(
                "WecomChannel requires wecom-aibot-python-sdk: "
                "pip install wecom-aibot-python-sdk"
            )
        self._bot_id = bot_id
        self._bot_secret = bot_secret
        self._msg_queue = msg_queue
        self._q_lock = q_lock

        self._ws_client: Any = None
        self._ws_loop: Any = None
        # Map of ``inbound_id -> frame``.  Keyed by a per-inbound uuid
        # (attached to ``InboundMessage.raw["_pip_inbound_id"]``) so two
        # concurrent messages from the same peer can't stomp each
        # other's reply target. See ``_enqueue`` / ``send`` /
        # ``release_inbound``.
        self._pending_frames: dict[str, Any] = {}
        self._pending_lock = threading.Lock()

    _DOWNLOAD_TIMEOUT = 30  # seconds

    def start(self, stop_event: threading.Event) -> None:
        """Create WSClient, register handlers, run in current thread's event loop."""
        import asyncio

        class _QuietLogger:
            def debug(self, msg: str, *args: object) -> None:
                pass
            def info(self, msg: str, *args: object) -> None:
                log.info("[AiBotSDK] %s", msg)
            def warn(self, msg: str, *args: object) -> None:
                log.warning("[AiBotSDK] %s", msg)
            def error(self, msg: str, *args: object) -> None:
                log.error("[AiBotSDK] %s", msg)

        self._ws_client = WSClient(
            WSClientOptions(
                bot_id=self._bot_id,
                secret=self._bot_secret,
                logger=_QuietLogger(),
            )
        )
        ws = self._ws_client

        # -- helpers (closures) --

        def _parse_frame(body: dict) -> tuple[str, str, bool, str, str]:
            """Return (sender_id, peer_id, is_group, guild_id, chat_type)."""
            sender = body.get("from", {})
            if isinstance(sender, dict):
                sender_id = (
                    sender.get("userid")
                    or sender.get("user_id")
                    or sender.get("open_id")
                    or ""
                )
            elif isinstance(sender, str):
                sender_id = sender
            else:
                sender_id = ""
            chat_id = body.get("chatid", "")
            chat_type = body.get("chattype", "") or body.get("chat_type", "")
            is_group = chat_type == "group"
            guild_id = chat_id if is_group else ""
            peer_id = chat_id if chat_id else sender_id
            return sender_id, peer_id, is_group, guild_id, chat_type

        async def _download_media(
            url: str, aeskey: str | None = None,
        ) -> tuple[bytes | None, str | None]:
            # ``aibot.WSClient.download_file`` returns ``(bytes, filename)``
            # where ``filename`` is parsed from the gateway's
            # Content-Disposition. WeCom's own ``file`` body dict rarely
            # carries ``filename``/``file_name``/``name`` for user
            # uploads, so if we drop this the agent sees ``[File: file]``
            # with no extension to work from. Plumb it through.
            # PROFILE
            from pip_agent import _profile

            async with _profile.span("wecom.media_download", channel="wecom"):
                try:
                    data, fname = await asyncio.wait_for(
                        ws.download_file(url, aeskey),
                        timeout=self._DOWNLOAD_TIMEOUT,
                    )
                    # PROFILE
                    _profile.event(
                        "wecom.media_bytes",
                        channel="wecom",
                        bytes=len(data) if data else 0,
                    )
                    return data, fname
                except Exception as exc:
                    log.warning("wecom media download failed: %s", exc)
                    return None, None

        async def _collect_quote_attachments(body: dict) -> list[Attachment]:
            """Download media from a quoted (reply-to) message.

            Field-based detection (like the main handler) so we don't
            depend on the quote's msgtype being accurate.
            """
            quote = body.get("quote")
            if not quote:
                return []
            atts: list[Attachment] = []

            log.debug(
                "wecom quote: msgtype=%s keys=%s",
                quote.get("msgtype", ""), sorted(quote.keys()),
            )

            if quote.get("image", {}).get("url"):
                img = quote["image"]
                data, _fname = await _download_media(
                    img["url"], img.get("aeskey"),
                )
                atts.append(Attachment(
                    type="image", data=data,
                    mime_type=_detect_image_mime(data) if data else "",
                    text="" if data else "[Quoted image]",
                ))

            if quote.get("file", {}).get("url"):
                fi = quote["file"]
                data, dl_fname = await _download_media(
                    fi["url"], fi.get("aeskey"),
                )
                # Prefer the gateway-supplied filename (from the
                # download response) over the body dict — WeCom's
                # file body usually lacks ``filename``/``file_name``.
                fname = (
                    dl_fname
                    or fi.get("filename") or fi.get("file_name")
                    or fi.get("name") or "file"
                )
                if data and _detect_image_mime(data):
                    atts.append(Attachment(
                        type="image", data=data,
                        mime_type=_detect_image_mime(data),
                    ))
                else:
                    text_content = ""
                    if data:
                        try:
                            text_content = data.decode("utf-8")
                        except (UnicodeDecodeError, ValueError):
                            pass
                    atts.append(Attachment(
                        type="file", data=data, filename=fname,
                        text=text_content,
                    ))

            if quote.get("voice", {}).get("content"):
                atts.append(Attachment(
                    type="voice", text=quote["voice"]["content"],
                ))

            if not atts and quote.get("text", {}).get("content"):
                quoted_text = quote["text"]["content"]
                sender = quote.get("from", {})
                who = sender.get("userid") or sender.get("name") or ""
                label = f"quoted message from {who}" if who else "quoted message"
                atts.append(Attachment(
                    type="file", filename=label, text=quoted_text,
                ))
            return atts

        def _enqueue(frame: dict, text: str, attachments: list[Attachment]) -> None:
            body = frame.get("body", {})
            sender_id, peer_id, is_group, guild_id, chat_type = _parse_frame(body)
            if not text and not attachments:
                return
            # Generate a unique id for this inbound frame and stash it
            # both in the frame (so the dispatcher can recover it from
            # ``InboundMessage.raw``) and in the pending-frames table.
            # Keying by ``peer_id`` used to lose the earlier frame when
            # a second message from the same peer landed before the
            # first reply was sent — reply_stream would then thread to
            # the wrong source message.
            inbound_id = uuid.uuid4().hex
            frame["_pip_inbound_id"] = inbound_id
            log.debug(
                "wecom enqueue: type=%s peer=%s sender=%s atts=%d inbound_id=%s",
                body.get("msgtype", "?"), peer_id, sender_id, len(attachments),
                inbound_id,
            )
            with self._pending_lock:
                self._pending_frames[inbound_id] = frame
            msg = InboundMessage(
                text=text,
                sender_id=sender_id,
                channel="wecom",
                peer_id=peer_id,
                guild_id=guild_id,
                account_id=self._bot_id,
                is_group=is_group,
                raw=frame,
                attachments=attachments,
            )
            # PROFILE
            from pip_agent import _profile

            _profile.event(
                "wecom.inbound_received",
                channel="wecom",
                text_len=len(text),
                atts=len(attachments),
                sender=sender_id,
                peer=peer_id,
                is_group=is_group,
                inbound_id=inbound_id,
            )
            with self._q_lock:
                self._msg_queue.append(msg)

        # -- event handlers --

        @ws.on("authenticated")
        def _on_auth():
            print("  [wecom] Authenticated")

        @ws.on("disconnected")
        def _on_disconnect(reason: str = ""):
            print(f"  [wecom] Disconnected: {reason}")

        @ws.on("error")
        def _on_error(err: Exception):
            log.warning("wecom error: %s", err)

        @ws.on("message")
        async def _on_message(frame: dict):
            """Unified handler — detect fields regardless of msgtype.

            WeCom may deliver image/file fields under unexpected msgtypes
            (e.g. "stream", or text+image without "mixed"), so we check
            all fields unconditionally, matching pipi's strategy.
            """
            body = frame.get("body", {})
            if not body:
                return
            msgtype = body.get("msgtype", "")
            # PROFILE
            from pip_agent import _profile

            async with _profile.span(
                "wecom.on_message", channel="wecom", msgtype=msgtype
            ):
                await _on_message_inner(frame, body, msgtype)

        async def _on_message_inner(  # PROFILE — split from _on_message
            frame: dict, body: dict, msgtype: str,
        ) -> None:
            text_parts: list[str] = []
            atts: list[Attachment] = []

            log.debug("wecom msg: msgtype=%s keys=%s", msgtype, sorted(body.keys()))

            # -- mixed: iterate msg_item explicitly --
            if msgtype == "mixed" and body.get("mixed", {}).get("msg_item"):
                for item in body["mixed"]["msg_item"]:
                    mt = item.get("msgtype", "")
                    if mt == "text":
                        t = item.get("text", {}).get("content", "")
                        if t:
                            text_parts.append(t)
                    elif mt == "image":
                        img = item.get("image", {})
                        url = img.get("url", "")
                        if url:
                            data, _fname = await _download_media(
                                url, img.get("aeskey"),
                            )
                            atts.append(Attachment(
                                type="image", data=data,
                                mime_type=_detect_image_mime(data) if data else "",
                                text="" if data else "[Image]",
                            ))
                        else:
                            atts.append(Attachment(type="image", text="[Image]"))
                    elif mt == "file":
                        # Mirror the non-mixed ``body["file"]`` branch:
                        # download, fall back to gateway-supplied
                        # filename over the body dict (WeCom often
                        # omits ``filename`` so without this the agent
                        # sees ``[File: file]``), and attempt a UTF-8
                        # decode so text-shaped files surface their
                        # content to the model alongside the attachment.
                        fi = item.get("file", {})
                        url = fi.get("url", "")
                        if not url:
                            continue
                        data, dl_fname = await _download_media(
                            url, fi.get("aeskey"),
                        )
                        fname = (
                            dl_fname
                            or fi.get("filename") or fi.get("file_name")
                            or fi.get("name") or "file"
                        )
                        text_content_f = ""
                        if data:
                            try:
                                text_content_f = data.decode("utf-8")
                            except (UnicodeDecodeError, ValueError):
                                pass
                        atts.append(Attachment(
                            type="file", data=data, filename=fname,
                            text=text_content_f,
                        ))
                    elif mt == "voice":
                        # Voice in a mixed bundle: WeCom attaches the
                        # ASR transcription under ``voice.content`` the
                        # same way as the top-level branch, so reuse
                        # the same fallback text when ASR is missing.
                        v = item.get("voice", {})
                        asr = v.get("content", "")
                        atts.append(Attachment(
                            type="voice",
                            text=asr or "[Voice message]",
                        ))
                    else:
                        # Unknown sub-type in a mixed frame is rare but
                        # we want operators to notice when WeCom adds a
                        # new kind — silent drops were the whole reason
                        # M6 existed.
                        log.debug(
                            "wecom mixed: ignoring unknown sub-msgtype=%s "
                            "keys=%s", mt, sorted(item.keys()),
                        )
            else:
                # -- field detection: check text, image, file, voice
                #    unconditionally (a message may carry multiple) --
                text_content = body.get("text", {}).get("content", "")
                if text_content:
                    text_parts.append(text_content)

                if body.get("image", {}).get("url"):
                    img = body["image"]
                    data, _fname = await _download_media(
                        img["url"], img.get("aeskey"),
                    )
                    atts.append(Attachment(
                        type="image", data=data,
                        mime_type=_detect_image_mime(data) if data else "",
                        text="" if data else "[Image]",
                    ))

                if body.get("file", {}).get("url"):
                    fi = body["file"]
                    data, dl_fname = await _download_media(
                        fi["url"], fi.get("aeskey"),
                    )
                    # Prefer the gateway-supplied filename over the body
                    # dict: WeCom usually omits ``filename`` on file
                    # uploads so without this the agent would see
                    # ``[File: file]`` with no extension.
                    fname = (
                        dl_fname
                        or fi.get("filename") or fi.get("file_name")
                        or fi.get("name") or "file"
                    )
                    text_content_f = ""
                    if data:
                        try:
                            text_content_f = data.decode("utf-8")
                        except (UnicodeDecodeError, ValueError):
                            pass
                    atts.append(Attachment(
                        type="file", data=data, filename=fname, text=text_content_f,
                    ))

                if body.get("voice", {}).get("content"):
                    asr = body["voice"]["content"]
                    atts.append(Attachment(type="voice", text=asr))
                elif msgtype == "voice":
                    atts.append(Attachment(
                        type="voice", text="[Voice message]",
                    ))

                # -- link messages (forwarded articles / shared links) --
                link = body.get("link") or {}
                if link:
                    parts = []
                    if link.get("title"):
                        parts.append(link["title"])
                    if link.get("desc"):
                        parts.append(link["desc"])
                    if link.get("url"):
                        parts.append(link["url"])
                    if parts:
                        text_parts.append("\n".join(parts))

                # -- chat_record / merged-forward messages --
                chat_record = body.get("chat_record") or {}
                if chat_record:
                    items = chat_record.get("item") or chat_record.get("items") or []
                    for rec in items:
                        title = rec.get("title", "")
                        content_r = rec.get("content", "")
                        if title or content_r:
                            text_parts.append(
                                f"{title}: {content_r}" if title else content_r
                            )
                    if not items:
                        desc = chat_record.get("title") or chat_record.get("desc") or ""
                        if desc:
                            text_parts.append(f"[Chat Record] {desc}")

                # -- news / miniprogram / markdown --
                news = body.get("news") or {}
                if news.get("articles"):
                    for art in news["articles"]:
                        parts = []
                        if art.get("title"):
                            parts.append(art["title"])
                        if art.get("description"):
                            parts.append(art["description"])
                        if art.get("url"):
                            parts.append(art["url"])
                        if parts:
                            text_parts.append("\n".join(parts))

                md = body.get("markdown") or {}
                if md.get("content"):
                    text_parts.append(md["content"])

            # -- quote (reply-to) attachments --
            atts.extend(await _collect_quote_attachments(body))

            text = "\n".join(text_parts)
            if not text and not atts:
                _body_keys = {
                    k: (list(v.keys()) if isinstance(v, dict) else type(v).__name__)
                    for k, v in body.items()
                }
                log.debug(
                    "wecom DROPPED msgtype=%s structure=%s",
                    msgtype,
                    json.dumps(_body_keys, ensure_ascii=False, default=str)[:2000],
                )
                return
            _enqueue(frame, text, atts)

        async def _run():
            self._ws_loop = asyncio.get_running_loop()
            await ws.connect()
            # Bridge the cross-thread ``threading.Event`` into asyncio by
            # blocking a single executor worker on ``Event.wait()``. The
            # worker wakes the instant ``stop_event.set()`` is called,
            # bounding shutdown latency to worker-scheduling (~ms) instead
            # of the old ``asyncio.sleep(0.5)`` poll tick (avg 250 ms, p99
            # 500 ms just to notice a shutdown signal).
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, stop_event.wait)
            # Drain in-flight replies before tearing the socket down. A
            # bare ``ws.disconnect()`` here would cancel any coroutines
            # that other threads scheduled via ``_run_async`` (typically
            # ``_reply_async`` / media uploads) while the user was
            # mid-sentence — the receiving side sees a truncated stream.
            # Wait up to 3 s for the loop's own task set to drain; after
            # that the disconnect goes through regardless (we'd rather
            # lose a hung task than stall Ctrl+C indefinitely).
            pending = {
                t for t in asyncio.all_tasks(loop)
                if t is not asyncio.current_task()
            }
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "wecom shutdown: %d task(s) still running after "
                        "3 s drain window; disconnecting anyway",
                        len(pending),
                    )
            ws.disconnect()

        asyncio.run(_run())

    def send(self, to: str, text: str, **kw: Any) -> bool:
        # PROFILE
        from pip_agent import _profile

        with _profile.span_sync(
            "wecom.send", channel="wecom", text_len=len(text),
        ):
            inbound_id = str(kw.get("inbound_id") or "")
            frame: Any = None
            if inbound_id:
                with self._pending_lock:
                    frame = self._pending_frames.get(inbound_id)
            if not frame or not self._ws_client:
                # No cached frame means this is either a proactive send
                # (cron / heartbeat / command response without an inbound)
                # or the frame was already released. Fall through to the
                # markdown push path rather than silently dropping.
                log.debug(
                    "wecom send: no frame for inbound_id=%s (peer=%s), "
                    "using send_message",
                    inbound_id or "<none>", to,
                )
                return self._send_proactive(to, text)
            ok, _ = self._run_async(self._reply_async(frame, text))
            return ok

    def release_inbound(self, inbound_id: str) -> None:
        if not inbound_id:
            return
        with self._pending_lock:
            self._pending_frames.pop(inbound_id, None)

    async def _reply_async(self, frame: dict, text: str) -> None:
        # PROFILE
        from pip_agent import _profile

        async with _profile.span(
            "wecom.reply_stream", channel="wecom", text_len=len(text),
        ):
            stream_id = generate_req_id("stream")
            await self._ws_client.reply_stream(frame, stream_id, text, True)

    # ------------------------------------------------------------------
    # Progressive-reply API (Channel.start/update/finish_stream override)
    # ------------------------------------------------------------------
    #
    # WeCom's ``reply_stream`` natively supports incremental snapshots
    # (each call replaces the prior body until ``is_finished=True``).
    # Two visual conventions worth knowing about — they're the WeCom
    # client doing the work, not us:
    #
    # * ``<think></think>`` as the very first body triggers the typing-
    #   dots animation. Sent unconditionally on stream open so the user
    #   sees a reply bubble appear within milliseconds of the request.
    # * Anything between ``<think>...</think>`` is rendered as a small
    #   cloud-icon italic block above the main reply — pipi uses this
    #   to surface ``thinking`` content live as the model emits it.
    #
    # The frame stays parked in ``_pending_frames`` for the entire
    # stream's lifetime; ``finish_stream`` releases it the same way the
    # one-shot ``send`` path does.

    def start_stream(
        self, to: str, *, inbound_id: str = "", account_id: str = "",
    ) -> str | None:
        if not self._ws_client or not inbound_id:
            return None
        with self._pending_lock:
            frame = self._pending_frames.get(inbound_id)
        if not frame:
            log.debug(
                "wecom start_stream: no frame for inbound_id=%s (peer=%s)",
                inbound_id, to,
            )
            return None
        stream_id = generate_req_id("stream")
        ok, _ = self._run_async(
            self._ws_client.reply_stream(
                frame, stream_id, "<think></think>", False,
            ),
        )
        if not ok:
            return None
        return stream_id

    def update_stream(
        self, to: str, handle: str, text: str,
        *, inbound_id: str = "", account_id: str = "",
    ) -> bool:
        if not self._ws_client or not handle or not inbound_id:
            return False
        with self._pending_lock:
            frame = self._pending_frames.get(inbound_id)
        if not frame:
            return False
        ok, _ = self._run_async(
            self._ws_client.reply_stream(frame, handle, text, False),
        )
        return ok

    def finish_stream(
        self, to: str, handle: str, text: str,
        *, inbound_id: str = "", account_id: str = "",
    ) -> bool:
        if not self._ws_client or not handle or not inbound_id:
            return False
        with self._pending_lock:
            frame = self._pending_frames.get(inbound_id)
        if not frame:
            return False
        ok, _ = self._run_async(
            self._ws_client.reply_stream(frame, handle, text, True),
        )
        # Mirror send_with_retry's release-on-completion contract so
        # the pending-frames table stays bounded.
        self.release_inbound(inbound_id)
        return ok

    # -- media upload pipeline (WebSocket protocol, no SDK uploadMedia needed) --

    _UPLOAD_CHUNK_SIZE = 512 * 1024  # 512 KB per chunk
    _MAX_UPLOAD_CHUNKS = 100

    async def _ws_command(self, body: dict, cmd: str) -> dict:
        """Send a raw WS command and return the response frame."""
        req_id = generate_req_id(cmd)
        frame: dict = {"headers": {"req_id": req_id}}
        return await self._ws_client.reply(frame, body, cmd)

    async def _upload_media_bytes(
        self, data: bytes, media_type: str, filename: str,
    ) -> str:
        """Upload media via init→chunk→finish and return media_id."""
        total_size = len(data)
        total_chunks = (total_size + self._UPLOAD_CHUNK_SIZE - 1) // self._UPLOAD_CHUNK_SIZE
        if total_chunks > self._MAX_UPLOAD_CHUNKS:
            raise ValueError(
                f"File too large: {total_chunks} chunks exceeds {self._MAX_UPLOAD_CHUNKS}"
            )

        init_resp = await self._ws_command({
            "type": media_type,
            "filename": filename,
            "total_size": total_size,
            "total_chunks": total_chunks,
            "md5": hashlib.md5(data).hexdigest(),
        }, "aibot_upload_media_init")

        init_body = init_resp.get("body", {})
        upload_id = str(init_body.get("upload_id", "")).strip()
        if not upload_id:
            raise RuntimeError(f"upload_media_init: no upload_id in {init_resp}")

        for idx, start in enumerate(range(0, total_size, self._UPLOAD_CHUNK_SIZE)):
            chunk = data[start:start + self._UPLOAD_CHUNK_SIZE]
            await self._ws_command({
                "upload_id": upload_id,
                "chunk_index": idx,
                "base64_data": base64.b64encode(chunk).decode("ascii"),
            }, "aibot_upload_media_chunk")

        finish_resp = await self._ws_command(
            {"upload_id": upload_id}, "aibot_upload_media_finish",
        )
        finish_body = finish_resp.get("body", {})
        media_id = str(finish_body.get("media_id", "")).strip()
        if not media_id:
            raise RuntimeError(f"upload_media_finish: no media_id in {finish_resp}")
        return media_id

    async def _send_media_msg(self, chat_id: str, media_type: str, media_id: str) -> None:
        await self._ws_client.send_message(chat_id, {
            "msgtype": media_type,
            media_type: {"media_id": media_id},
        })

    def _run_async(self, coro: Any) -> tuple[bool, Any]:
        """Schedule an async coroutine on the WS thread's event loop.

        Returns ``(ok, result)``. ``ok`` is ``False`` iff the loop was
        unavailable, the coroutine timed out, aibot raised a mid-send
        ``RuntimeError`` (socket torn down), or any other exception
        escaped. Callers that actually need to gate retries on success
        (``send()`` → ``send_with_retry``) should test ``ok``; callers
        that are fire-and-forget from the dispatcher's point of view
        (media uploads) can still ignore it.

        Historical note: before this split, ``send()`` returned a
        constant ``True`` regardless of whether the WS send succeeded,
        so ``send_with_retry`` could never actually retry a transient
        failure. Threading the success bit back up makes the retry
        contract real.
        """
        import asyncio
        import concurrent.futures

        # PROFILE
        from pip_agent import _profile

        loop = self._ws_loop
        if loop is None or loop.is_closed():
            log.warning("wecom _run_async: WS loop not available")
            return False, None
        with _profile.span_sync("wecom.run_async_wait", channel="wecom"):
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return True, future.result(timeout=30)
            except concurrent.futures.TimeoutError:
                log.warning("wecom _run_async: timed out")
                future.cancel()
                return False, None
            except RuntimeError as exc:
                # aibot raises RuntimeError("WebSocket not connected, ...")
                # when the socket is torn down mid-send (typical on Ctrl+C
                # while a reply is still in flight). Log without traceback.
                log.warning("wecom _run_async: %s", exc)
                return False, None
            except Exception:
                log.exception("wecom _run_async: coroutine failed")
                return False, None

    def send_image(self, to: str, image_data: bytes, caption: str = "", **kw: Any) -> bool:
        if not self._ws_client or not image_data:
            return False
        try:
            async def _do() -> None:
                media_id = await self._upload_media_bytes(image_data, "image", "image.jpg")
                await self._send_media_msg(to, "image", media_id)
                if caption:
                    await self._ws_client.send_message(to, {
                        "msgtype": "markdown",
                        "markdown": {"content": caption},
                    })
            ok, _ = self._run_async(_do())
            return ok
        except Exception as exc:
            log.warning("wecom send_image error: %s", exc)
            return False

    def send_file(
        self, to: str, file_data: bytes, filename: str = "",
        caption: str = "", **kw: Any,
    ) -> bool:
        if not self._ws_client or not file_data:
            return False
        try:
            async def _do() -> None:
                media_id = await self._upload_media_bytes(
                    file_data, "file", filename or "file",
                )
                await self._send_media_msg(to, "file", media_id)
                if caption:
                    await self._ws_client.send_message(to, {
                        "msgtype": "markdown",
                        "markdown": {"content": caption},
                    })
            ok, _ = self._run_async(_do())
            return ok
        except Exception as exc:
            log.warning("wecom send_file error: %s", exc)
            return False

    def _send_proactive(self, to: str, text: str) -> bool:
        """Fallback: proactive push via send_message (markdown)."""
        from pip_agent.fileutil import chunk_message

        if not self._ws_client:
            return False
        all_ok = True
        for chunk in chunk_message(text, "wecom"):
            try:
                ok, _ = self._run_async(self._ws_client.send_message(to, {
                    "msgtype": "markdown",
                    "markdown": {"content": chunk},
                }))
                if not ok:
                    all_ok = False
            except Exception as exc:
                log.warning("wecom send_message error: %s", exc)
                all_ok = False
        return all_ok

    def close(self) -> None:
        if self._ws_client:
            try:
                self._ws_client.disconnect()
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# Background WS loop
# ---------------------------------------------------------------------------

def wecom_ws_loop(
    wecom: WecomChannel,
    stop: threading.Event,
) -> None:
    """WebSocket loop for WeCom, runs in a daemon thread."""
    print("  [wecom] WebSocket loop starting")
    wecom.start(stop)
