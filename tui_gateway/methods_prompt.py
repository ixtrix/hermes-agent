"""Prompt / attachment / respond JSON-RPC handlers (moved verbatim from server.py).

Handler bodies are byte-identical to their pre-split server.py form; they
are rebound onto server.py's globals at install time — see method_ctx.py.
"""

from .method_ctx import HandlerRegistry


_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped



def _pending_reaction_notes(
    session: dict,
    *,
    load_cfg,
    session_db,
    log,
) -> str:
    """Return unseen reaction notes using the owning server's helpers."""
    session_key = str(session.get("session_key") or "")
    if not session_key:
        return ""
    try:
        display = load_cfg().get("display")
        if not (isinstance(display, dict) and bool(display.get("message_reactions", False))):
            return ""
    except Exception:
        return ""
    try:
        with session_db(session) as db:
            if db is None:
                return ""
            pending = db.take_unseen_reactions(session_key, author="user")
    except Exception:
        log.debug("Failed to read pending reactions", exc_info=True)
        return ""
    if not pending:
        return ""
    notes = []
    for entry in pending:
        snippet = (entry.get("text") or "").strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        emoji = entry.get("emoji") or ""
        whose = "their own" if entry.get("role") == "user" else "your"
        if snippet:
            notes.append(f'[The user reacted {emoji} to {whose} message: "{snippet}"]')
        else:
            notes.append(f"[The user reacted {emoji} to {whose} earlier message]")
    return "\n".join(notes)


@method("prompt.submit")
def _(rid, params: dict) -> dict:
    from hermes_cli.input_sanitize import sanitize_user_prompt_text

    sid = params.get("session_id", "")
    raw_text = params.get("text", "")
    text = sanitize_user_prompt_text(raw_text) if isinstance(raw_text, str) else raw_text
    # Typed bare stop phrase while backend voice mode is active ends the
    # voice chat instead of sending "stop" to the agent — the typed twin of
    # the spoken stop phrase (PR #73106), applied at the ONE server-side
    # choke point every TUI submit passes through. Guarded on voice mode
    # being ON: typed "stop" outside a voice chat is a normal message.
    # (The desktop's voice conversation is renderer-owned and never flips
    # the backend flag, so it handles its own typed stop client-side.)
    if isinstance(text, str) and _voice_mode_enabled():
        try:
            from tools.voice_mode import is_voice_stop_phrase

            typed_stop = is_voice_stop_phrase(text)
        except Exception:
            typed_stop = False
        if typed_stop:
            os.environ["HERMES_VOICE"] = "0"
            os.environ["HERMES_VOICE_TTS"] = "0"
            try:
                from hermes_cli.voice import stop_continuous

                stop_continuous()
            except Exception:
                pass
            try:
                _tts_stream_stop(user_barge=False)
            except Exception:
                pass
            _voice_emit("voice.transcript", {"stop_phrase": True, "typed": True})
            logger.info("prompt.submit: typed stop phrase — voice chat ended")
            return _ok(rid, {"voice_stopped": True})
    truncate_user_ordinal = params.get("truncate_before_user_ordinal")
    if params.get("interrupted"):
        # Client-side barge-in (desktop VAD / typing over playback) — latch it
        # so this turn's model message carries the interruption note.
        from tools.tts_streaming import mark_speech_interrupted

        mark_speech_interrupted()
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    if _is_managed_staff_mode():
        try:
            session["_managed_prompt_attachments"] = _managed_prompt_attachments(
                session, params.get("attachments")
            )
        except Exception:
            return _err(rid, 4015, "invalid managed attachment descriptors")
    if (limit_message := _ensure_active_session_slot(sid, session)) is not None:
        return _err(rid, 4090, limit_message)
    if truncate_user_ordinal is not None and isinstance(text, str):
        # A rewind/regenerate replays a turn from what the transcript shows. A
        # skill turn shows its invocation, so re-expand it here — otherwise
        # re-running `/work fix it` sends the agent nine literal characters
        # instead of the skill it originally loaded.
        text = _expand_skill_invocation_for_replay(
            text, str(session.get("session_key") or "")
        )
    isolation_cfg = _load_dashboard_process_isolation_config()
    turn_isolation = _session_uses_compute_host(session, isolation_cfg)
    # Re-bind to the current client transport for this request. This keeps
    # streaming events on the active websocket even if an earlier disconnect
    # or fallback moved the session transport to stdio.
    if (t := current_transport()) is not None:
        session["transport"] = t
    while True:
        busy_transport = None
        with session["history_lock"]:
            if session.get("running"):
                # Don't reject a mid-turn prompt — queue it (and, by default,
                # interrupt the live turn) so it runs as the next turn. The
                # provider interrupt itself must happen after this lock is
                # released: a non-interruptible tool may keep it waiting.
                busy_transport = t or session.get("transport")
            else:
                break
        busy_response = _handle_busy_submit(
            rid, sid, session, text, busy_transport,
            queued=bool(params.get("queued")),
        )
        if busy_response is not None:
            return busy_response
        # The old turn finished between the two lock acquisitions. Retry the
        # claim so this prompt starts normally instead of being stranded in a
        # queue whose drain already ran.

    with session["history_lock"]:
        # A watch session's run lives in the PARENT turn, so its own running
        # flag is False — without this, typing mid-run builds a second agent
        # racing the in-flight child on the same stored session (interleaved
        # transcript, stale fork). After the run completes, submitting is fine:
        # the upgrade resumes the child's transcript as a normal conversation.
        if session.get("lazy") and _child_run_active(str(session.get("session_key") or "")):
            return _err(rid, 4009, "subagent still running — wait for it to finish")
        if truncate_user_ordinal is not None:
            try:
                ordinal = int(truncate_user_ordinal)
            except (TypeError, ValueError):
                return _err(rid, 4004, "truncate_before_user_ordinal must be an integer")
            history = session.get("history", [])
            user_indices = [
                i for i, m in enumerate(history)
                if m.get("role") == "user" and not m.get("display_kind")
            ]
            # Reject out-of-range ordinals on BOTH ends. A negative value would
            # otherwise sail past the upper-bound check and hit Python's negative
            # indexing below (user_indices[-1] -> the LAST user turn), silently
            # truncating history to everything before it and persisting that loss
            # via replace_messages — an unrecoverable overwrite of the session DB.
            if ordinal < 0 or ordinal >= len(user_indices):
                return _err(rid, 4018, "target user message is no longer in session history")
            truncated = history[: user_indices[ordinal]]
            # Stale clients can attach truncate_before_user_ordinal=0 to an
            # ordinary submit. That resolves to history[:0] == [] and
            # replace_messages() DELETEs every durable row — silent total
            # transcript loss. Refuse the empty-truncation edge unless the
            # client explicitly opts in (legitimate restore/regenerate of the
            # first user turn).
            if (
                not truncated
                and history
                and not is_truthy_value(params.get("confirm_empty_truncate"))
            ):
                logger.warning(
                    "prompt.submit: REFUSED empty truncation of session %s "
                    "(%d messages would be wiped; ordinal=%d).",
                    sid,
                    len(history),
                    ordinal,
                )
                return _err(
                    rid,
                    4028,
                    "truncation would erase the entire session transcript; "
                    "resubmit with confirm_empty_truncate=true if this is intended",
                )
            # Info for routine rewind/edit cuts; warning only when the client
            # explicitly opts into wiping the whole transcript.
            log_fn = logger.warning if not truncated else logger.info
            log_fn(
                "prompt.submit: truncating session %s history %d -> %d messages "
                "(ordinal=%d)",
                sid,
                len(history),
                len(truncated),
                ordinal,
            )
            # Write-before-memory (mirrors gateway hygiene / manual /compress):
            # persist the truncated transcript first. If replace_messages fails
            # after we already rewrote session["history"], the turn still runs
            # against the short list while state.db keeps the old tail. The
            # agent flush is append-only for history-dict identities, so the
            # new exchange is appended on top of the "undone" turns — durable
            # zombie history on resume, and the edit/regenerate never sticks.
            # Fail closed: refuse the turn and leave memory/DB unchanged.
            if (db := _get_db()) is not None:
                try:
                    db.replace_messages(session["session_key"], truncated)
                except Exception as exc:
                    logger.error(
                        "prompt.submit: replace_messages failed for session %s "
                        "(ordinal=%d); refusing turn so memory and DB stay "
                        "aligned: %s",
                        sid,
                        ordinal,
                        exc,
                        exc_info=True,
                    )
                    return _err(
                        rid,
                        5008,
                        f"failed to persist history truncation: {exc}",
                    )
            session["history"] = truncated
            session["history_version"] = int(session.get("history_version", 0)) + 1
        session["running"] = True
        session["_turn_cancel_requested"] = False
        session["last_active"] = time.time()
        _start_inflight_turn(session, text)

    if turn_isolation:
        isolated_response = _submit_prompt_to_compute_host(rid, sid, session, text)
        if not isolated_response.get("error"):
            return isolated_response
        logger.warning(
            "compute-host dispatch failed for session %s; falling back inline: %s",
            sid,
            isolated_response["error"].get("message", "unknown error"),
        )

    # Persist the DB row lazily, now that the user has actually sent a message.
    # Disk-full must fail the RPC (not stream silently): desktop maps the error
    # string to a "disk full" toast so the user knows why the send vanished.
    try:
        _ensure_session_db_row(session)
        # A branch becomes real here: copy its parent's transcript into the row so it
        # resumes with full context (the agent won't persist the seed itself).
        _persist_branch_seed(session)
    except Exception as exc:
        from hermes_state import is_disk_full_error

        with session["history_lock"]:
            session["running"] = False
            session["last_active"] = time.time()
            _clear_inflight_turn(session)
        if is_disk_full_error(exc):
            return _err(
                rid,
                5070,
                "disk full: session storage could not be written — free some disk space and try again",
            )
        logger.warning("prompt.submit: session persist failed: %s", exc, exc_info=True)
        return _err(
            rid,
            5071,
            f"session storage could not be written: {exc}",
        )
    _start_agent_build(sid, session)

    def run_after_agent_ready() -> None:
        # Patient wait (#63078): the user's message is already the accepted
        # in-flight turn, so a slow deferred build must not eat it. The wait
        # delivers the prompt when the still-running build completes, honors a
        # cancel promptly, notices the user once past the slow threshold, and
        # only errors when the build itself fails or the bounded cap expires.
        err = _wait_agent_for_prompt(session, rid, sid)
        if err:
            # Terminal frame + retained snapshot (not a bare "error" event +
            # cleared inflight): if the client is disconnected right now, the
            # retained snapshot is the only way resume can show this failure.
            _emit_terminal_turn_error(
                sid,
                session,
                (err.get("error") or {}).get("message", "agent initialization failed"),
            )
            with session["history_lock"]:
                session["running"] = False
                session["last_active"] = time.time()
            _emit("session.info", sid, _session_info(session.get("agent"), session))
            return
        with session["history_lock"]:
            if session.get("_turn_cancel_requested") or not session.get("running"):
                session["running"] = False
                _clear_inflight_turn(session)
                # Surface the cancellation to the client. Without this emit the
                # turn vanishes silently — the Desktop sees `prompt.submit`
                # return `{"status": "streaming"}` but never receives a
                # `message.start` or `error` event, so the composer shows no
                # feedback (issue #63078 server-side half). Match the
                # `_wait_agent` error branch above: emit, then bail.
                _emit(
                    "error",
                    sid,
                    {
                        "message": "Turn cancelled before the agent was ready"
                        if session.get("_turn_cancel_requested")
                        else "Session no longer running before the agent was ready"
                    },
                )
                return
        _run_prompt_submit(rid, sid, session, text)

    run_thread = threading.Thread(target=run_after_agent_ready, daemon=True)
    # Keep a handle so session.interrupt can tell a live turn from a stuck
    # `running` flag (a turn that died without clearing it) and recover the latter.
    session["_run_thread"] = run_thread
    run_thread.start()
    return _ok(rid, {"status": "streaming"})



def _managed_attachment_receipt(
    session: dict, params: dict, *, default_mime: str = ""
) -> dict:
    import base64
    import binascii
    from hermes_cli.managed_staff import STAFF_PURPOSES
    declared_mime = ""

    encoded = params.get("bytes")
    if encoded is None:
        encoded = params.get("content_base64") or params.get("data") or params.get("data_url")
    if isinstance(encoded, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in encoded):
        data = bytes(encoded)
    elif isinstance(encoded, str):
        raw = encoded.strip()
        if raw.startswith("data:") and "," in raw:
            header, raw = raw.split(",", 1)
            declared_mime = header[5:].split(";", 1)[0].strip().lower()
        else:
            declared_mime = ""
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("attachment bytes must be base64") from exc
    else:
        raise ValueError("attachment bytes are required")
    if not data:
        raise ValueError("attachment is empty")
    claimed_size = params.get("size")
    if claimed_size is not None and int(claimed_size) != len(data):
        raise ValueError("attachment size does not match bytes")
    purpose = str(params.get("purpose") or "").strip()
    if purpose not in STAFF_PURPOSES:
        raise ValueError("attachment purpose is not approved")
    supplier_id = str(params.get("supplier_id") or "").strip()
    supplier_domain = str(params.get("supplier_domain") or "").strip()
    if purpose == "supplier" and (not supplier_id or not supplier_domain):
        raise ValueError("supplier attachment supplier metadata is required")
    declared_mime = str(
        params.get("mime_type") or default_mime or declared_mime
    ).strip().lower()
    filename = str(params.get("name") or params.get("filename") or "attachment").strip()
    admit = _staff_admit_attachment
    receipt = admit(
        session,
        data=data,
        filename=filename,
        purpose=purpose,
        supplier_id=supplier_id,
        supplier_domain=supplier_domain,
        declared_mime=declared_mime,
    )
    if isinstance(receipt, dict):
        metadata = dict(receipt)
    elif hasattr(receipt, "__dict__"):
        metadata = dict(vars(receipt))
    else:
        metadata = {}
        for key in (
            "attachment_id", "name", "size", "size_bytes", "mime_type",
            "declared_mime", "detected_mime", "purpose", "supplier_id",
            "supplier_domain", "expires_at", "state",
        ):
            if hasattr(receipt, key):
                metadata[key] = getattr(receipt, key)
    if any(key in metadata for key in ("path", "file_path", "ref_path", "namespace")):
        raise ValueError("attachment admission returned forbidden fields")
    receipt_metadata = metadata.get("metadata")
    if not isinstance(receipt_metadata, dict):
        receipt_metadata = {}
    observed_purpose = metadata.get("purpose", receipt_metadata.get("purpose"))
    if observed_purpose != purpose:
        raise ValueError("attachment admission purpose mismatch")
    for field, expected in (
        ("supplier_id", supplier_id),
        ("supplier_domain", supplier_domain),
    ):
        if expected and metadata.get(field, receipt_metadata.get(field)) != expected:
            raise ValueError(f"attachment admission {field} mismatch")
    attachment_id = metadata.get("attachment_id") or metadata.get("receipt_id") or metadata.get("id")
    if not attachment_id:
        raise ValueError("attachment admission returned no receipt")
    name = str(metadata.get("name") or filename).replace("\\", "/").rsplit("/", 1)[-1]
    size = metadata.get("size", metadata.get("size_bytes", len(data)))
    mime_type = str(
        metadata.get("mime_type")
        or metadata.get("detected_mime")
        or metadata.get("declared_mime")
        or declared_mime
    ).strip().lower()
    allowed_metadata = {
        "purpose", "supplier_id", "supplier_domain", "expires_at", "state",
        "declared_mime", "detected_mime",
    }
    safe_metadata = {
        key: value for key, value in metadata.items()
        if key in allowed_metadata and value is not None
    }
    if "/" not in mime_type:
        raise ValueError("attachment MIME type is required")
    return {
        "attached": True,
        "attachment_id": str(attachment_id),
        "name": name,
        "size": int(size),
        "mime_type": mime_type,
        "metadata": safe_metadata,
    }

def _managed_prompt_attachments(session: dict, value: object) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("attachments must be a list")
    identity_lookup = _staff_identity_for_session
    identity_lookup(session)
    result: list[dict] = []
    required = {"attachment_id", "name", "size", "mime_type", "metadata"}
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("attachment descriptors have an invalid shape")
        if any(key in item for key in ("path", "ref_path", "namespace", "identity")):
            raise ValueError("attachment descriptor contains forbidden fields")
        attachment_id = str(item["attachment_id"]).strip()
        name = str(item["name"]).replace("\\", "/").rsplit("/", 1)[-1].strip()
        mime_type = str(item["mime_type"]).strip().lower()
        if not attachment_id or not name or "/" not in mime_type:
            raise ValueError("attachment descriptor is incomplete")
        if not isinstance(item["size"], int) or item["size"] < 0:
            raise ValueError("attachment descriptor size is invalid")
        metadata = item["metadata"]
        if not isinstance(metadata, dict) or any(
            key in metadata for key in ("path", "file_path", "namespace", "identity")
        ):
            raise ValueError("attachment metadata contains forbidden fields")
        result.append(
            {
                "attachment_id": attachment_id,
                "name": name,
                "size": item["size"],
                "mime_type": mime_type,
                "metadata": dict(metadata),
            }
        )
    return result

@method("clipboard.paste")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    if _is_managed_staff_mode():
        if params.get("bytes") is None and not params.get("content_base64") and not params.get("data"):
            return _err(rid, 4015, "clipboard image bytes are required in managed staff mode")
        try:
            receipt = _managed_attachment_receipt(
                session,
                {**params, "name": params.get("name") or "clipboard.png", "mime_type": "image/png"},
                default_mime="image/png",
            )
        except Exception:
            return _err(rid, 5028, "attachment admission failed")
        return _ok(rid, receipt)
    try:
        from hermes_cli.clipboard import has_clipboard_image, save_clipboard_image
    except Exception as e:
        return _err(rid, 5027, f"clipboard unavailable: {e}")

    session["image_counter"] = session.get("image_counter", 0) + 1
    img_dir = _session_images_dir(session)
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = (
        img_dir
        / f"clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{session['image_counter']}.png"
    )

    # Save-first: mirrors CLI keybinding path; more robust than has_image() precheck
    if not save_clipboard_image(img_path):
        session["image_counter"] = max(0, session["image_counter"] - 1)
        msg = (
            "Clipboard has image but extraction failed"
            if has_clipboard_image()
            else "No image found in clipboard"
        )
        return _ok(rid, {"attached": False, "message": msg})

    session.setdefault("attached_images", []).append(str(img_path))
    return _ok(
        rid,
        {
            "attached": True,
            "path": str(img_path),
            "count": len(session["attached_images"]),
            **_image_meta(img_path),
        },
    )


@method("image.attach")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    if _is_managed_staff_mode():
        if str(params.get("path", "") or "").strip():
            return _err(rid, 4015, "path is not accepted in managed staff mode")
        try:
            receipt = _managed_attachment_receipt(
                session, params, default_mime="image/octet-stream"
            )
        except Exception:
            return _err(rid, 5028, "attachment admission failed")
        return _ok(rid, receipt)
    raw = str(params.get("path", "") or "").strip()
    if not raw:
        return _err(rid, 4015, "path required")
    try:
        from cli import (
            _IMAGE_EXTENSIONS,
            _detect_file_drop,
            _resolve_attachment_path,
            _split_path_input,
        )

        dropped = _detect_file_drop(raw)
        if dropped:
            image_path = dropped["path"]
            remainder = dropped["remainder"]
        else:
            path_token, remainder = _split_path_input(raw)
            image_path = _resolve_attachment_path(path_token)
            if image_path is None:
                return _err(rid, 4016, f"image not found: {path_token}")
        if image_path.suffix.lower() not in _IMAGE_EXTENSIONS:
            return _err(rid, 4016, f"unsupported image: {image_path.name}")
        session.setdefault("attached_images", []).append(str(image_path))
        return _ok(
            rid,
            {
                "attached": True,
                "path": str(image_path),
                "count": len(session["attached_images"]),
                "remainder": remainder,
                "text": remainder or f"[User attached image: {image_path.name}]",
                **_image_meta(image_path),
            },
        )
    except Exception as e:
        return _err(rid, 5027, str(e))


@method("image.attach_bytes")
def _(rid, params: dict) -> dict:
    """Attach an image to the session from base64 bytes (remote-client path).

    A desktop app or web dashboard running on a DIFFERENT machine than the
    gateway can't hand us a local path — that file only exists on the client's
    disk. So it uploads the raw image bytes (base64) and we write them into the
    gateway's own images dir. The response shape mirrors ``image.attach`` so the
    client treats both identically.

    Params:
      content_base64 / data (str, required): base64 image bytes. Accepts a
        ``data:image/...;base64,`` prefix and embedded whitespace. ``data`` is
        an accepted alias for older desktop builds.
      filename / ext (str, optional): extension hint. Without it, magic bytes
        identify PNG/JPEG/GIF/WebP/BMP, falling back to ``.png``.
    """
    session, err = _sess(params, rid)
    if err:
        return err
    if _is_managed_staff_mode():
        if str(params.get("path", "") or "").strip():
            return _err(rid, 4015, "path is not accepted in managed staff mode")
        try:
            receipt = _managed_attachment_receipt(
                session, params, default_mime="image/octet-stream"
            )
        except Exception:
            return _err(rid, 5028, "attachment admission failed")
        return _ok(rid, receipt)

    raw_b64 = str(params.get("content_base64") or params.get("data") or "").strip()
    if not raw_b64:
        return _err(rid, 4015, "content_base64 required")

    img_bytes = _decode_attach_base64(raw_b64, mime_prefix="image/")
    if img_bytes is None:
        return _err(rid, 4017, "data is not valid base64")
    if not img_bytes:
        return _err(rid, 4017, "image is empty")
    if len(img_bytes) > _ATTACH_BYTES_MAX_BYTES:
        mb = _ATTACH_BYTES_MAX_BYTES // (1024 * 1024)
        return _err(rid, 4018, f"image too large ({len(img_bytes)} bytes; cap is {mb} MB)")

    filename = str(params.get("filename", "") or "")
    ext_hint = str(params.get("ext", "") or "").strip().lower()
    if ext_hint and not ext_hint.startswith("."):
        ext_hint = "." + ext_hint
    ext = _sniff_image_ext(img_bytes, filename or (f"x{ext_hint}" if ext_hint else ""))
    if ext not in _allowed_image_extensions():
        return _err(rid, 4016, f"unsupported image extension: {ext}")

    try:
        img_path = _queue_attached_image(session, img_bytes, ext, prefix="upload")
    except Exception as e:
        return _err(rid, 5027, f"write failed: {e}")

    return _ok(
        rid,
        {
            "attached": True,
            "path": str(img_path),
            "count": len(session["attached_images"]),
            "remainder": "",
            "text": f"[User attached image: {img_path.name}]",
            "bytes": len(img_bytes),
            **_image_meta(img_path),
        },
    )


@method("pdf.attach")
def _(rid, params: dict) -> dict:
    """Attach a PDF by rendering each page to PNG and queuing the pages.

    Anthropic's vision pipeline accepts images, not PDFs, so this runs
    ``pdftoppm`` (poppler-utils) at 150 DPI per page and queues each rendered
    page as an attached image. Accepts either a host ``path`` (local mode) or
    base64 ``content_base64`` (remote upload). Caps at 50 MB / 25 pages per call.

    Requires ``pdftoppm`` on $PATH (``apt install poppler-utils``); returns 5028
    if missing.
    """
    import shutil
    import subprocess
    import tempfile

    session, err = _sess(params, rid)
    if err:
        return err
    if _is_managed_staff_mode():
        if str(params.get("path", "") or "").strip():
            return _err(rid, 4015, "path is not accepted in managed staff mode")
        try:
            receipt = _managed_attachment_receipt(
                session, params, default_mime="application/pdf"
            )
        except Exception:
            return _err(rid, 5028, "attachment admission failed")
        return _ok(rid, receipt)

    if shutil.which("pdftoppm") is None:
        return _err(rid, 5028, "pdftoppm not installed (poppler-utils package required)")

    raw_path = str(params.get("path", "") or "").strip()
    raw_b64 = str(params.get("content_base64") or params.get("data") or "").strip()
    if not raw_path and not raw_b64:
        return _err(rid, 4015, "path or content_base64 required")

    with tempfile.TemporaryDirectory(prefix="pdf_attach_") as td:
        td_path = Path(td)
        if raw_b64:
            pdf_bytes = _decode_attach_base64(raw_b64, mime_prefix="application/pdf")
            if pdf_bytes is None:
                return _err(rid, 4017, "data is not valid base64")
            if not pdf_bytes:
                return _err(rid, 4017, "decoded PDF is empty")
            if len(pdf_bytes) > _PDF_ATTACH_MAX_BYTES:
                mb = _PDF_ATTACH_MAX_BYTES // (1024 * 1024)
                return _err(rid, 4018, f"PDF too large ({len(pdf_bytes)} bytes; cap is {mb} MB)")
            if pdf_bytes[:5] != b"%PDF-":
                return _err(rid, 4017, "payload is not a PDF (missing %PDF- magic bytes)")
            pdf_path = td_path / "input.pdf"
            pdf_path.write_bytes(pdf_bytes)
            display_name = str(params.get("filename", "") or "uploaded.pdf")
        else:
            try:
                from cli import _resolve_attachment_path

                resolved = _resolve_attachment_path(raw_path)
            except Exception:
                resolved = None
            if resolved is None or not Path(resolved).is_file():
                return _err(rid, 4016, f"PDF not found: {raw_path}")
            if Path(resolved).suffix.lower() != ".pdf":
                return _err(rid, 4016, f"not a PDF: {Path(resolved).name}")
            if Path(resolved).stat().st_size > _PDF_ATTACH_MAX_BYTES:
                mb = _PDF_ATTACH_MAX_BYTES // (1024 * 1024)
                return _err(rid, 4018, f"PDF too large; cap is {mb} MB")
            pdf_path = Path(resolved)
            display_name = pdf_path.name

        try:
            first_page = int(params.get("first_page") or 1)
            last_page_param = params.get("last_page")
            last_page = int(last_page_param) if last_page_param is not None else None
        except (TypeError, ValueError):
            return _err(rid, 4015, "first_page/last_page must be integers")

        if first_page < 1:
            return _err(rid, 4015, "first_page must be >= 1")
        if last_page is None:
            last_page = first_page + _PDF_ATTACH_MAX_PAGES - 1
        if last_page < first_page:
            return _err(rid, 4015, "last_page must be >= first_page")
        if last_page - first_page + 1 > _PDF_ATTACH_MAX_PAGES:
            return _err(rid, 4019, f"page range exceeds cap of {_PDF_ATTACH_MAX_PAGES} pages per attach call")

        out_prefix = td_path / "page"
        argv = [
            "pdftoppm", "-png", "-r", "150",
            "-f", str(first_page), "-l", str(last_page),
            str(pdf_path), str(out_prefix),
        ]
        from hermes_cli._subprocess_compat import windows_hide_flags

        try:
            res = subprocess.run(
                argv, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                # Force UTF-8 + lossy decode so non-UTF-8 child output can't
                # crash the gateway thread on locale-mismatched Windows (#53137).
                encoding="utf-8", errors="replace",
                creationflags=windows_hide_flags(),
            )
        except subprocess.TimeoutExpired:
            return _err(rid, 5028, "pdftoppm timed out (>120s)")
        if res.returncode != 0:
            tail = (res.stderr or res.stdout or "").strip().splitlines()[-3:]
            return _err(rid, 5028, "pdftoppm failed: " + " | ".join(tail))

        rendered = sorted(td_path.glob("page-*.png"))
        if not rendered:
            return _err(rid, 5028, "pdftoppm produced no pages (corrupt PDF?)")

        attached_pages = []
        for src in rendered:
            page_num = src.stem.split("-", 1)[-1]
            try:
                page_int = int(page_num)
            except ValueError:
                page_int = first_page + len(attached_pages)
            dst = _queue_attached_image(session, src.read_bytes(), ".png", prefix=f"pdf_p{page_num}")
            attached_pages.append({"path": str(dst), "page": page_int, **_image_meta(dst)})

        return _ok(
            rid,
            {
                "attached": True,
                "filename": display_name,
                "pages_attached": len(attached_pages),
                "pages": attached_pages,
                "count": len(session["attached_images"]),
                "text": f"[User attached PDF: {display_name} ({len(attached_pages)} page(s))]",
            },
        )


@method("file.attach")
def _(rid, params: dict) -> dict:
    """Attach a file through the active local or managed admission path."""
    session, err = _sess(params, rid)
    if err:
        return err

    if _is_managed_staff_mode():
        if str(params.get("path", "") or "").strip():
            return _err(rid, 4015, "path is not accepted in managed staff mode")
        try:
            receipt = _managed_attachment_receipt(session, params)
        except Exception:
            return _err(rid, 5028, "attachment admission failed")
        return _ok(rid, receipt)

    raw = str(params.get("path", "") or "").strip()
    data_url = str(params.get("data_url", "") or "").strip()
    name = str(params.get("name", "") or "").strip()
    if not raw and not data_url:
        return _err(rid, 4015, "path or data_url required")
    try:
        stored_path, uploaded = _stage_session_file_attachment(
            session, raw_path=raw, data_url=data_url, name=name
        )
        ref_path = _attachment_ref_path(session, stored_path)
        return _ok(
            rid,
            {
                "attached": True,
                "name": stored_path.name,
                "path": str(stored_path),
                "ref_path": ref_path,
                "ref_text": f"@file:{_format_ref_value(ref_path)}",
                "uploaded": uploaded,
            },
        )
    except Exception as e:
        return _err(rid, 5028, str(e))


@method("image.detach")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    if _is_managed_staff_mode():
        return _err(rid, 4015, "path is not accepted in managed staff mode")
    raw = str(params.get("path", "") or "").strip()
    if not raw:
        return _err(rid, 4015, "path required")
    images = session.setdefault("attached_images", [])
    before = len(images)
    session["attached_images"] = [path for path in images if path != raw]
    return _ok(
        rid,
        {
            "detached": len(session["attached_images"]) != before,
            "count": len(session["attached_images"]),
        },
    )


@method("input.detect_drop")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    if _is_managed_staff_mode():
        return _err(rid, 4015, "path drops are not accepted in managed staff mode")
    try:
        from cli import _detect_file_drop

        raw = str(params.get("text", "") or "")
        dropped = _detect_file_drop(raw)
        if not dropped:
            return _ok(rid, {"matched": False})

        drop_path = dropped["path"]
        remainder = dropped["remainder"]
        if dropped["is_image"]:
            session.setdefault("attached_images", []).append(str(drop_path))
            text = remainder or f"[User attached image: {drop_path.name}]"
            return _ok(
                rid,
                {
                    "matched": True,
                    "is_image": True,
                    "path": str(drop_path),
                    "count": len(session["attached_images"]),
                    "text": text,
                    **_image_meta(drop_path),
                },
            )

        text = f"[User attached file: {drop_path}]" + (
            f"\n{remainder}" if remainder else ""
        )
        return _ok(
            rid,
            {
                "matched": True,
                "is_image": False,
                "path": str(drop_path),
                "name": drop_path.name,
                "text": text,
            },
        )
    except Exception as e:
        return _err(rid, 5027, str(e))


@method("prompt.background")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    text, parent = params.get("text", ""), params.get("session_id", "")
    if not text:
        return _err(rid, 4012, "text required")
    task_id = f"bg_{uuid.uuid4().hex[:6]}"

    def run():
        session_tokens = _set_session_context(task_id, cwd=_session_cwd(session))
        try:
            from run_agent import AIAgent

            result = AIAgent(
                **_background_agent_kwargs(session["agent"], task_id)
            ).run_conversation(
                user_message=text,
                task_id=task_id,
            )
            _emit(
                "background.complete",
                parent,
                {
                    "task_id": task_id,
                    "text": (
                        result.get("final_response", str(result))
                        if isinstance(result, dict)
                        else str(result)
                    ),
                },
            )
        except Exception as e:
            _emit(
                "background.complete",
                parent,
                {"task_id": task_id, "text": f"error: {e}"},
            )
        finally:
            _clear_session_context(session_tokens)

    threading.Thread(target=run, daemon=True).start()
    return _ok(rid, {"task_id": task_id})


@method("preview.restart")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err

    url = str(params.get("url") or "").strip()
    cwd = str(params.get("cwd") or "").strip()
    context = str(params.get("context") or "").strip()

    if not url:
        return _err(rid, 4012, "url required")

    task_id = f"preview_{uuid.uuid4().hex[:6]}"
    parent = params.get("session_id", "")
    parent_history = _preview_restart_history(session)
    has_history = bool(parent_history)
    prompt = "\n".join(
        line
        for line in [
            "The desktop preview pane cannot load a local server URL.",
            "",
            f"Preview URL: {url}",
            f"Current working directory: {cwd or '(unknown)'}",
            "",
            f"Preview console:\n{context}" if context else "",
            "" if context else "",
            (
                "The conversation history above is from the user's main session — including the commands you (the assistant) previously ran to start servers, edit files, or check ports. Use it to figure out exactly which server should be running at this Preview URL. The user did not start a brand new task; recover what they had working."
                if has_history
                else None
            ),
            "Restart exactly the app intended for the Preview URL, not Hermes Desktop itself.",
            "The Preview URL and port are the target. Preserve that target unless you conclude it is impossible.",
            "If the prior conversation shows a specific command that bound this URL/port, prefer re-running THAT exact command (in the same cwd) over guessing a new one.",
            "First inspect what process, if any, owns the Preview URL port. If a stale server exists, inspect its cwd and prefer that cwd over the Hermes/Desktop process cwd.",
            "The Current working directory is only a hint. Do not assume it is the preview app root when the port owner or files indicate another root.",
            "If the console shows a module-script MIME error for src/main.tsx or similar, a static server is serving source files. Do not restart python -m http.server or any dumb static server for that app.",
            "For module-script MIME failures, inspect package.json/vite config in the candidate app root and start the real dev server/bundler (for example npm/pnpm/yarn dev) so module transforms happen.",
            "Before declaring success, verify the Preview URL responds with the intended app, not Hermes Desktop. If it serves Hermes/Desktop UI or another unrelated app, stop that process and report failure.",
            "Do not modify files. Do not ask the user unless blocked.",
            "Prefer existing project scripts or commands when they are clear.",
            "If a stale process owns the needed port, handle it safely.",
            "Start long-running servers detached/in the background, then return immediately.",
            "Do not run a foreground dev server command that blocks this background task.",
            "Keep the final response short: what command/server was started, or why it could not be restarted.",
        ]
        if line
    )

    # Normalize defensively: a malformed client path (embedded NUL, etc.) must
    # not blow up the whole restart — treat it as "no validated cwd".
    try:
        preview_cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else ""
        if preview_cwd and not os.path.isdir(preview_cwd):
            preview_cwd = ""
    except Exception:
        preview_cwd = ""

    def run():
        # Pin the validated preview cwd, else the parent workspace — never an
        # invalid client path, which would silently fall back to the launch dir.
        session_tokens = _set_session_context(task_id, cwd=(preview_cwd or _session_cwd(session)))
        try:
            from run_agent import AIAgent
            from tools.terminal_tool import register_task_env_overrides

            if preview_cwd:
                register_task_env_overrides(task_id, {"cwd": preview_cwd})

            history_note = (
                f" (with {len(parent_history)} parent-session messages of context)"
                if parent_history
                else ""
            )
            _emit(
                "preview.restart.progress",
                parent,
                {"task_id": task_id, "text": f"Starting hidden restart agent{history_note}"},
            )
            result = AIAgent(
                **_ephemeral_preview_agent_kwargs(session["agent"], task_id),
                **_preview_restart_callbacks(parent, task_id),
            ).run_conversation(
                user_message=prompt,
                task_id=task_id,
                conversation_history=parent_history or None,
            )
            text = (
                result.get("final_response", str(result))
                if isinstance(result, dict)
                else str(result)
            )
            _emit("preview.restart.complete", parent, {"task_id": task_id, "text": text})
        except Exception as e:
            _emit(
                "preview.restart.complete",
                parent,
                {"task_id": task_id, "text": f"error: {e}"},
            )
        finally:
            try:
                from tools.terminal_tool import clear_task_env_overrides

                clear_task_env_overrides(task_id)
            except Exception:
                pass
            _clear_session_context(session_tokens)

    threading.Thread(target=run, daemon=True).start()
    return _ok(rid, {"task_id": task_id})


@method("clarify.respond")
def _(rid, params: dict) -> dict:
    # allow_expired=True: a clarify can time out server-side (its entry is popped
    # from _pending) while the card is still visible — common when a WebSocket
    # reconnect during the wait drops tool.complete. A late answer must resolve
    # gracefully instead of hitting the raw 4009 "no pending answer request".
    return _respond(rid, params, "answer", allow_expired=True)


@method("terminal.read.respond")
def _(rid, params: dict) -> dict:
    # `text` is a JSON string of the serialized terminal buffer + line metadata.
    # allow_expired=True: the read_terminal tool's _block() uses a short 30s
    # timeout, so a slow renderer losing the race is the common case — a late
    # response must not error after the tool already returned empty.
    return _respond(rid, params, "text", allow_expired=True)


@method("sudo.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "password", allow_expired=True)


@method("secret.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "value", allow_expired=True)


@method("approval.respond")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    try:
        from tools.approval import resolve_gateway_approval

        return _ok(
            rid,
            {
                "resolved": resolve_gateway_approval(
                    session["session_key"],
                    params.get("choice", "deny"),
                    resolve_all=params.get("all", False),
                )
            },
        )
    except Exception as e:
        return _err(rid, 5004, str(e))


def register(server) -> None:
    """Bind handlers and server callbacks used by managed attachment helpers."""
    _registry.install(server)

    def pending_reaction_notes(session: dict) -> str:
        return _pending_reaction_notes(
            session,
            load_cfg=server._load_cfg,
            session_db=server._session_db,
            log=server.logger,
        )

    def managed_prompt_attachments(session: dict, value: object) -> list[dict]:
        _registry.refresh(server, _managed_prompt_attachments)
        return _managed_prompt_attachments(session, value)

    server._pending_reaction_notes = pending_reaction_notes
    server._managed_prompt_attachments = managed_prompt_attachments
