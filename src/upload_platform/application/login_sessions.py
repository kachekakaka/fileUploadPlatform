"""Server-owned QR login. Durable metadata is separate from ephemeral sensitive images."""

import base64
from datetime import datetime, timezone
import json
import queue
import struct
import threading
import time
from typing import Literal
from uuid import UUID
import zlib

from pydantic import Field
from sqlalchemy import select, update

from upload_platform.application.accounts import MaintenanceRequest, ProcessIdentity
from upload_platform.application.contracts import Empty, StrictModel
from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.windows import JobHost, stop_verified


class LoginRequest(StrictModel):
    platform: Literal["bilibili"] = "bilibili"
    account_id: str | None = None


class LoginEvent(StrictModel):
    type: Literal["login_qr_ready", "login_progress"]
    protocol_version: Literal[1]
    interaction_version: Literal[1]
    engine_build_id: str
    operation_id: UUID
    seq: int = Field(strict=True, gt=0)
    challenge_id: UUID | None = None
    png_base64: str | None = Field(default=None, max_length=175000)
    deadline_at: int | None = Field(default=None, strict=True)
    expires_at: None = None
    expiry_source: Literal["local"] | None = None
    phase: Literal["waiting_confirmation", "verifying_identity"] | None = None


class LoginError(StrictModel):
    code: str
    message: str


class AccountSummary(StrictModel):
    id: str
    platform: str
    reference: str
    remote_user_id: str | None
    display_name: str
    enabled: bool
    availability: Literal["ready", "disabled", "blocked"]
    block_reasons: list[str]


class Challenge(StrictModel):
    challenge_id: str
    image_path: str
    expires_at: None = None
    deadline_at: str
    expiry_source: Literal["local"] = "local"


class LoginView(StrictModel):
    session_id: str
    operation_id: str
    platform: Literal["bilibili"] = "bilibili"
    account_id: str | None
    revision: int
    phase: Literal[
        "queued",
        "waiting_for_executions",
        "starting",
        "waiting_confirmation",
        "verifying_identity",
        "waiting_for_account",
        "finalizing",
        "cancelling",
        "recovering",
        "finished",
    ]
    outcome: Literal["succeeded", "cancelled", "expired", "failed"] | None
    created_at: str
    updated_at: str
    can_cancel: bool
    can_resume: bool
    can_retry: bool
    challenge: Challenge | None
    account_summary: AccountSummary | None
    error: LoginError | None


class LoginList(StrictModel):
    items: list[LoginView]


class GuiLoginCapability(StrictModel):
    version: Literal[1] = 1
    available: bool
    reason: str | None
    platforms: list[Literal["bilibili"]]


class SystemCapabilities(StrictModel):
    gui_login: GuiLoginCapability


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def summary(account, blocks):
    reasons = sorted({x["reason"] for x in blocks})
    allowed = {"maintenance", "credential_unverified", "credential_invalid"}
    return AccountSummary(
        **{
            k: account[k]
            for k in ("id", "platform", "reference", "remote_user_id", "display_name", "enabled")
        },
        availability="disabled" if not account["enabled"] else "blocked" if reasons else "ready",
        block_reasons=[r if r in allowed else "account_blocked" for r in reasons],
    ).model_dump()


def validate_png(encoded):
    """Validate the small grayscale PNG emitted by the engine, including bounded inflation."""
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 128 * 1024 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid image")
    offset, width, height, payload, ended = 8, 0, 0, bytearray(), False
    while offset < len(raw):
        size = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4 : offset + 8]
        data = raw[offset + 8 : offset + 8 + size]
        crc = struct.unpack_from(">I", raw, offset + 8 + size)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            raise ValueError("invalid image")
        if kind == b"IHDR" and offset == 8 and size == 13:
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if not (0 < width <= 1024 and 0 < height <= 1024) or (
                depth,
                color,
                compression,
                filtering,
                interlace,
            ) != (8, 0, 0, 0, 0):
                raise ValueError("invalid image")
        elif kind == b"IDAT" and width:
            payload.extend(data)
        elif kind == b"IEND" and size == 0 and offset + 12 == len(raw):
            ended = True
        else:
            raise ValueError("invalid image")
        offset += size + 12
    decoder = zlib.decompressobj()
    pixels = decoder.decompress(payload, (width + 1) * height + 1)
    if not ended or not decoder.eof or decoder.unused_data or len(pixels) != (width + 1) * height:
        raise ValueError("invalid image")
    if any(pixels[i] > 4 for i in range(0, len(pixels), width + 1)):
        raise ValueError("invalid image")
    return raw


class LoginSessions:
    def __init__(self, accounts):
        self.accounts, self.service, self.database = accounts, accounts.service, accounts.database
        self.lock = threading.RLock()
        self.threads, self.images = {}, {}
        self.pending_resumes = set()
        self.stopping = threading.Event()
        self.owner = uid()

    def capabilities(self):
        reason = None
        try:
            capability = self.accounts.deployment().capabilities().get("login_interaction", {})
            if capability != {
                "version": 1,
                "modes": ["terminal", "machine"],
                "image_type": "image/png",
            }:
                reason = "engine_interaction_incompatible"
        except Problem as exc:
            reason = exc.code
        return {
            "gui_login": {
                "version": 1,
                "available": reason is None,
                "reason": reason,
                "platforms": ["bilibili"],
            }
        }

    def _operation(self, identifier):
        operation = self.accounts.get(identifier)
        metadata = operation["execution"].get("login_session")
        if not metadata:
            raise Problem("login_session_not_found", "扫码会话不存在", 404)
        if metadata.get("version") != 1:
            raise Problem("login_version_incompatible", "扫码会话版本不兼容", 409)
        return operation

    def _set(self, identifier, generation=None, **values):
        with self.database.transaction() as conn:
            operation = db.get(conn, db.account_maintenance, identifier)
            if generation is not None and operation["generation"] != generation:
                raise Problem("maintenance_fenced", "扫码会话已由新代次接管", 409)
            meta = operation["execution"]["login_session"]
            if all(meta.get(k) == v for k, v in values.items()):
                return
            meta = {**meta, **values, "revision": meta["revision"] + 1}
            conn.execute(
                update(db.account_maintenance)
                .where(db.account_maintenance.c.id == identifier)
                .values(
                    execution={**operation["execution"], "login_session": meta},
                    updated_at=self.service.clock(),
                )
            )

    def view(self, identifier):
        operation = self._operation(identifier)
        meta = operation["execution"]["login_session"]
        phase, outcome = meta["phase"], meta["outcome"]
        challenge = None
        with self.lock:
            image = self.images.get(identifier)
            if (
                image
                and time.time() < image[2]
                and phase == "waiting_confirmation"
                and not meta["cancel_requested"]
            ):
                challenge = Challenge(
                    challenge_id=image[0],
                    deadline_at=utc(image[2]),
                    image_path=f"/api/v1/account-login-sessions/{identifier}/qr?challenge_id={image[0]}",
                )
        account = None
        if operation["account_id"] and outcome is not None:
            with self.database.read() as conn:
                item = db.get(conn, db.accounts, operation["account_id"])
                blocks = db.many(
                    conn,
                    select(db.account_blocks).where(db.account_blocks.c.account_id == item["id"]),
                )
                account = summary(item, blocks)
        return LoginView(
            session_id=identifier,
            operation_id=identifier,
            account_id=operation["account_id"],
            revision=meta["revision"],
            phase=phase,
            outcome=outcome,
            created_at=utc(operation["created_at"]),
            updated_at=utc(operation["updated_at"]),
            can_cancel=outcome is None
            and not meta["cancel_requested"]
            and phase not in ("finalizing", "recovering", "cancelling"),
            can_resume=phase == "recovering",
            can_retry=outcome in ("failed", "expired", "cancelled"),
            challenge=challenge,
            account_summary=account,
            error=LoginError(code=meta["error"], message="登录未完成，请根据会话状态恢复或重试")
            if meta.get("error")
            else None,
        ).model_dump()

    def listing(self, active=True, account_id=None):
        with self.database.read() as conn:
            query = select(db.account_maintenance).where(
                db.account_maintenance.c.kind == "login",
                db.account_maintenance.c.execution["login_session"]["version"].as_integer() == 1,
            )
            # Finished metadata can lag the authoritative maintenance result across a crash.
            if active:
                query = query.where(
                    db.account_maintenance.c.execution["login_session"]["outcome"]
                    .as_string()
                    .is_(None)
                )
            if account_id:
                query = query.where(db.account_maintenance.c.account_id == account_id)
            rows = db.many(
                conn, query.order_by(db.account_maintenance.c.created_at.desc()).limit(100)
            )
        identifiers = [r["id"] for r in rows]
        return {"items": [self.view(i) for i in identifiers]}

    def create(self, key, body):
        if not self.capabilities()["gui_login"]["available"]:
            raise Problem(
                "engine_interaction_incompatible", "当前引擎不支持界面扫码，请升级对应版本", 409
            )
        status, receipt = self.accounts.create(
            key, MaintenanceRequest(account_id=body.account_id), login_session=True
        )
        self.kick(receipt["session_id"])
        return status, self.view(receipt["session_id"])

    def image(self, identifier, challenge_id):
        view = self.view(identifier)
        with self.lock:
            image = self.images.get(identifier)
            if image and view["challenge"] and image[0] == challenge_id:
                return image[1]
        operation = self._operation(identifier)
        old = operation["execution"]["login_session"].get("challenge_id")
        raise Problem(
            "login_challenge_expired" if old else "login_qr_not_ready",
            "二维码已失效" if old else "二维码尚未就绪",
            410 if old else 409,
        )

    def cancel(self, identifier, key):
        def action(conn, _):
            operation = db.get(conn, db.account_maintenance, identifier)
            meta = operation["execution"].get("login_session")
            if not meta or meta.get("version") != 1:
                raise Problem("login_session_not_found", "扫码会话不存在", 404)
            if meta["outcome"] is None:
                if meta["phase"] == "finalizing" or operation["status"] in (
                    "VERIFYING",
                    "SUCCEEDED",
                ):
                    raise Problem("login_finalizing", "登录结果正在核验，请查询原会话", 409)
                conn.execute(
                    update(db.account_maintenance)
                    .where(db.account_maintenance.c.id == identifier)
                    .values(
                        execution={
                            **operation["execution"],
                            "login_session": {
                                **meta,
                                "cancel_requested": True,
                                "phase": "cancelling",
                                "revision": meta["revision"] + 1,
                            },
                        },
                        updated_at=self.service.clock(),
                    )
                )
            return {"session_id": identifier}

        self.service.mutate(
            "login-session:cancel:" + identifier, key, Empty(), lambda: None, action, 202
        )
        with self.lock:
            self.images.pop(identifier, None)
        self.kick(identifier)
        value = self.view(identifier)
        return (200 if value["outcome"] else 202), value

    def resume(self, identifier, key):
        self._operation(identifier)
        self.service.mutate(
            "login-session:resume:" + identifier,
            key,
            Empty(),
            lambda: None,
            lambda conn, _: {"session_id": identifier},
            202,
        )
        with self.lock:
            if identifier in self.threads:
                self.pending_resumes.add(identifier)
            else:
                self.kick(identifier)
        return 202, self.view(identifier)

    def start(self):
        for item in self.listing()["items"]:
            self.kick(item["session_id"])

    def kick(self, identifier):
        with self.lock:
            if self.stopping.is_set() or identifier in self.threads:
                return
            thread = threading.Thread(target=self._run, args=(identifier,), daemon=True)
            self.threads[identifier] = thread
            thread.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            threads = list(self.threads.values())
            self.images.clear()
        deadline = time.monotonic() + 90
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

    def _mark_exit(self, identifier, generation):
        with self.database.transaction() as conn:
            operation = db.get(conn, db.account_maintenance, identifier)
            if operation["generation"] != generation:
                raise Problem("maintenance_fenced", "扫码会话代次已改变", 409)
            conn.execute(
                update(db.account_maintenance)
                .where(db.account_maintenance.c.id == identifier)
                .values(execution={**operation["execution"], "exit_confirmed": True})
            )

    def _unstarted(self, identifier, generation):
        with self.database.transaction() as conn:
            operation = db.get(conn, db.account_maintenance, identifier)
            if operation["status"] != "WAITING" or operation["generation"] != generation:
                raise Problem("maintenance_fenced", "扫码会话已经启动", 409)
            directory = self.database.config.data_dir / "maintenance" / identifier
            directory.mkdir(parents=True, exist_ok=True)
            conn.execute(
                update(db.account_maintenance)
                .where(db.account_maintenance.c.id == identifier)
                .values(
                    status="VERIFYING",
                    execution={
                        **operation["execution"],
                        "exit_confirmed": True,
                        "never_started": True,
                        "result_path": str(directory / "result.json"),
                    },
                )
            )

    def _finish(self, identifier, generation):
        with self.lock:
            self.images.pop(identifier, None)
        self._set(identifier, generation, phase="finalizing")
        _, result = self.accounts.complete(identifier, uid())
        if result["status"] not in ("SUCCEEDED", "FAILED"):
            raise Problem("login_pending_verification", "登录结果仍需核验", 409)
        current = self._operation(identifier)
        meta = current["execution"]["login_session"]
        code = (current.get("result") or {}).get("error")
        outcome = (
            "succeeded"
            if result["status"] == "SUCCEEDED"
            else (
                "cancelled"
                if meta["cancel_requested"]
                else "expired"
                if code == "LOGIN_LOCAL_TIMEOUT" or meta.get("error") == "login_local_timeout"
                else "failed"
            )
        )
        error = (
            None
            if outcome == "succeeded"
            else meta.get("error")
            or (
                code
                if code
                in {
                    "QR_REQUEST_FAILED",
                    "QR_UNAVAILABLE",
                    "LOGIN_PLATFORM_ERROR",
                    "LOGIN_NETWORK_FAILED",
                    "LOGIN_PROTOCOL_ERROR",
                    "LOGIN_LOCAL_TIMEOUT",
                    "MID_MISMATCH",
                    "IDENTITY_QUERY_FAILED",
                    "CREDENTIAL_INVALID",
                }
                else "login_not_verified"
            )
        )
        self._set(identifier, generation, phase="finished", outcome=outcome, error=error)

    def _recover(self, operation):
        identifier, generation = operation["id"], operation["generation"]
        execution = operation["execution"]
        meta = execution["login_session"]
        if meta.get("owner_active", True) and meta.get("owner") != self.owner:
            import pywintypes
            import win32api
            import win32event
            from upload_platform.infrastructure.windows import process_creation, boot_id

            owner_boot = meta.get("owner_boot", execution.get("boot_id"))
            owner_pid = meta.get("owner_pid", execution.get("coordinator_pid"))
            owner_creation = meta.get("owner_creation", execution.get("coordinator_creation_time"))
            if owner_boot == boot_id() and owner_pid:
                try:
                    handle = win32api.OpenProcess(0x1000 | 0x100000, False, owner_pid)
                except (OSError, pywintypes.error) as exc:
                    if getattr(exc, "winerror", None) != 87:
                        return
                else:
                    try:
                        if (
                            process_creation(handle) == owner_creation
                            and win32event.WaitForSingleObject(handle, 0)
                            != win32event.WAIT_OBJECT_0
                        ):
                            return
                    finally:
                        handle.Close()
        with self.database.transaction() as conn:
            current = db.get(conn, db.account_maintenance, identifier)
            if current["generation"] != generation:
                return
            generation += 1
            import os
            import win32api
            from upload_platform.infrastructure.windows import process_creation, boot_id

            claimed = {
                **current["execution"]["login_session"],
                "owner": self.owner,
                "owner_active": True,
                "owner_pid": os.getpid(),
                "owner_creation": process_creation(win32api.GetCurrentProcess()),
                "owner_boot": boot_id(),
            }
            conn.execute(
                update(db.account_maintenance)
                .where(db.account_maintenance.c.id == identifier)
                .values(
                    generation=generation,
                    execution={**current["execution"], "login_session": claimed},
                )
            )
        try:
            self._set(identifier, generation, phase="recovering")
            if operation["status"] in ("SUCCEEDED", "FAILED"):
                self._finish(identifier, generation)
                return
            proof = execution.get("exit_confirmed", False)
            if not proof:
                proof, _ = stop_verified(execution)
            if not proof:
                raise Problem("maintenance_exit_unconfirmed", "旧登录进程退出尚未确认", 409)
            self._mark_exit(identifier, generation)
            self._finish(identifier, generation)
        except Exception:
            self._set(identifier, generation, phase="recovering", error="login_recovery_required")
        finally:
            self._set(identifier, generation, owner_active=False)

    def _consume(self, identifier, generation, line, host, last_seq):
        value = json.loads(line)
        operation = self._operation(identifier)
        if value.get("type") == "account_identified":
            if (
                set(value)
                != {"type", "protocol_version", "engine_build_id", "operation_id", "verified_mid"}
                or type(value["protocol_version"]) is not int
                or value["protocol_version"] != 1
                or value["operation_id"] != identifier
                or value["engine_build_id"] != operation["execution"]["engine_build_id"]
            ):
                raise ValueError("wrong identity")
            # The durable identity file is verified by Accounts, never accepted from stdout alone.
            self._set(identifier, generation, phase="waiting_for_account")
            while True:
                if (
                    self.stopping.is_set()
                    or self._operation(identifier)["execution"]["login_session"]["cancel_requested"]
                ):
                    return last_seq
                _, answer = self.accounts.identified(identifier, uid())
                if answer["ready"]:
                    host.send(
                        {
                            "type": "maintenance_ready",
                            "operation_id": identifier,
                            "verified_mid": answer["verified_mid"],
                        }
                    )
                    self._set(identifier, generation, phase="verifying_identity")
                    return last_seq
                while self._operation(identifier)["waiting_for_executions"]:
                    if host.exited() and not host.active_count():
                        return last_seq
                    if (
                        self.stopping.wait(0.2)
                        or self._operation(identifier)["execution"]["login_session"][
                            "cancel_requested"
                        ]
                    ):
                        return last_seq
        if "type" not in value:
            # Terminal stdout is only a hint; protected disk evidence controls completion.
            if value.get("outcome") in ("succeeded", "failed") or set(value) == {
                "protocol_version",
                "error",
            }:
                return last_seq
            raise ValueError("invalid message")
        event = LoginEvent.model_validate_json(line)
        if (
            type(value["protocol_version"]) is not int
            or type(value["interaction_version"]) is not int
            or str(event.operation_id) != identifier
            or event.engine_build_id != operation["execution"]["engine_build_id"]
        ):
            raise ValueError("wrong identity")
        if event.seq <= last_seq:
            return last_seq
        if event.seq != last_seq + 1:
            raise ValueError("missing sequence")
        if event.type == "login_qr_ready":
            if (
                last_seq != 0
                or not event.challenge_id
                or not event.png_base64
                or event.phase is not None
                or event.expiry_source != "local"
                or event.deadline_at is None
                or not time.time() < event.deadline_at <= time.time() + 181
            ):
                raise ValueError("invalid challenge")
            raw = validate_png(event.png_base64)
            with self.lock:
                self.images[identifier] = (str(event.challenge_id), raw, event.deadline_at)
            self._set(
                identifier,
                generation,
                phase="waiting_confirmation",
                challenge_id=str(event.challenge_id),
                deadline_at=event.deadline_at,
            )
        else:
            if (
                not event.phase
                or event.png_base64 is not None
                or event.challenge_id is not None
                or event.deadline_at is not None
                or event.expiry_source is not None
            ):
                raise ValueError("invalid progress")
            if event.phase != "waiting_confirmation":
                with self.lock:
                    self.images.pop(identifier, None)
            self._set(identifier, generation, phase=event.phase)
        return event.seq

    def _run(self, identifier):
        host, generation, owned = None, None, False
        try:
            operation = self._operation(identifier)
            if operation["execution"]["login_session"]["outcome"] is not None:
                return
            generation = operation["generation"]
            if operation["status"] != "WAITING":
                self._recover(operation)
                return
            while operation["waiting_for_executions"]:
                if (
                    self.stopping.is_set()
                    or operation["execution"]["login_session"]["cancel_requested"]
                ):
                    break
                self._set(identifier, generation, phase="waiting_for_executions")
                self.stopping.wait(0.2)
                operation = self._operation(identifier)
            if (
                self.stopping.is_set()
                or operation["execution"]["login_session"]["cancel_requested"]
            ):
                self._unstarted(identifier, generation)
                self._finish(identifier, generation)
                return
            _, started = self.accounts.start(identifier, uid())
            owned = True
            self._set(identifier, generation, phase="starting", owner=self.owner, owner_active=True)

            def persist(value):
                self.accounts.process(
                    identifier,
                    uid(),
                    ProcessIdentity(**{k: value[k] for k in ("pid", "creation_time", "assigned")}),
                )

            host = JobHost(
                started["execution"],
                persist,
                command=started["command"],
                output_limit=192 * 1024,
                output_queue_size=8,
            )
            host.send({**started["engine_request"], "interaction_mode": "machine"})
            last_seq = 0
            startup_deadline = time.monotonic() + 75
            while not host.exited() or host.active_count() or not host.messages.empty():
                operation = self._operation(identifier)
                meta = operation["execution"]["login_session"]
                deadline = meta.get("deadline_at")
                timed_out = (last_seq == 0 and time.monotonic() > startup_deadline) or (
                    meta["phase"] == "waiting_confirmation" and deadline and time.time() > deadline
                )
                if host.protocol_overflow:
                    raise ValueError("protocol overflow")
                if self.stopping.is_set() or meta["cancel_requested"] or timed_out:
                    if timed_out:
                        self._set(identifier, generation, error="login_local_timeout")
                    if not host.stop():
                        raise Problem("maintenance_exit_unconfirmed", "登录进程退出未确认", 409)
                    break
                try:
                    line = host.messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is not None:
                    last_seq = self._consume(identifier, generation, line, host, last_seq)
            self._mark_exit(identifier, generation)
            self._finish(identifier, generation)
        except Exception:
            # Never persist/re-raise arbitrary protocol bodies, image bytes or third-party errors.
            if host is None and not owned:
                try:
                    current = self._operation(identifier)
                    if (
                        current["status"] == "WAITING"
                        and current["execution"]["login_session"]["cancel_requested"]
                    ):
                        self._unstarted(identifier, generation)
                        self._finish(identifier, generation)
                except Exception:
                    pass
            if (
                host is not None
                and self._operation(identifier)["execution"]["login_session"]["phase"]
                != "finalizing"
            ):
                try:
                    if host.stop():
                        self._mark_exit(identifier, generation)
                        self._set(identifier, generation, error="login_interrupted")
                        self._finish(identifier, generation)
                except Exception:
                    pass
            try:
                current = self._operation(identifier)
                if current["execution"]["login_session"]["outcome"] is None:
                    self._set(
                        identifier, generation, phase="recovering", error="login_recovery_required"
                    )
            except Exception:
                pass
        finally:
            if host:
                host.close()
            if owned:
                try:
                    self._set(identifier, generation, owner_active=False)
                except Exception:
                    pass
            with self.lock:
                self.images.pop(identifier, None)
                self.threads.pop(identifier, None)
                if identifier in self.pending_resumes:
                    self.pending_resumes.remove(identifier)
                    self.kick(identifier)
