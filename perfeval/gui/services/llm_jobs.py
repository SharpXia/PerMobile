from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Sequence
from uuid import uuid4

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from ..models.llm import InterpretationDepth, InterpretationSource, InterpretationState, LlmProfile
from .llm_artifacts import (
    MAX_RESPONSE_BYTES,
    PromptBundle,
    build_interpretation_record,
    build_prompt,
    write_interpretation,
    write_status,
)
from .llm_profiles import LlmProfileService
from .llm_protocols import (
    build_request,
    parse_response,
    redact_sensitive_text,
    safe_error_message,
)


@dataclass
class _Job:
    job_id: str
    source: InterpretationSource
    profile: LlmProfile
    api_key: str
    bundle: PromptBundle
    retries: int = 0
    reply: QNetworkReply | None = None
    timeout_timer: QTimer | None = None
    cancelled: bool = False
    timed_out: bool = False


@dataclass
class _Probe:
    request_id: str
    profile: LlmProfile
    api_key: str
    reply: QNetworkReply
    timer: QTimer


class InterpretationJobManager(QObject):
    job_state_changed = Signal(str, object, object)
    job_completed = Signal(str, object, object)
    job_failed = Signal(str, object, str)
    notification_requested = Signal(str, str, bool)
    connection_test_finished = Signal(str, bool, str)
    active_count_changed = Signal(int)

    def __init__(
        self,
        profiles: LlmProfileService,
        parent: QObject | None = None,
        *,
        network_manager: QNetworkAccessManager | None = None,
        max_concurrent: int = 2,
        timeout_ms: int = 120_000,
    ) -> None:
        super().__init__(parent)
        self.profiles = profiles
        self.network = network_manager or QNetworkAccessManager(self)
        self.max_concurrent = max(1, int(max_concurrent))
        self.timeout_ms = max(1_000, int(timeout_ms))
        self._jobs: dict[str, _Job] = {}
        self._source_jobs: dict[str, str] = {}
        self._queue: list[str] = []
        self._probes: dict[str, _Probe] = {}

    @property
    def active_count(self) -> int:
        return len(self._jobs)

    def job_for_source(self, source: InterpretationSource | str) -> str | None:
        key = source.source_key if isinstance(source, InterpretationSource) else str(source)
        return self._source_jobs.get(key)

    def is_active_for(self, source: InterpretationSource | str) -> bool:
        return self.job_for_source(source) is not None

    def start(
        self,
        source: InterpretationSource,
        profile_id: str | None = None,
        *,
        depth: InterpretationDepth | str = InterpretationDepth.STANDARD,
    ) -> str:
        if not source.root.is_dir() or source.root.is_symlink():
            raise ValueError("结果目录不可用，无法生成 LLM 解读")
        if self.is_active_for(source):
            raise RuntimeError("该结果已有 LLM 解读任务正在运行")
        profile = self.profiles.profile(profile_id) if profile_id else self.profiles.default_profile
        if profile is None:
            raise ValueError("尚未配置可用的 LLM 模型")
        api_key = self.profiles.api_key(profile.profile_id)
        if not api_key:
            raise ValueError(f"模型“{profile.name}”尚未配置 API Key")
        resolved_depth = InterpretationDepth(depth)
        try:
            bundle = build_prompt(source, resolved_depth)
        except Exception as exc:
            _write_status_safely(
                source,
                InterpretationState.FAILED,
                message=str(exc),
                profile=profile,
                depth=resolved_depth,
            )
            raise
        job_id = uuid4().hex
        job = _Job(job_id, source, profile, api_key, bundle)
        write_status(
            source,
            InterpretationState.QUEUED,
            message="等待发送 LLM 请求。",
            profile=profile,
            depth=bundle.depth,
            job_id=job_id,
        )
        self._jobs[job_id] = job
        self._source_jobs[source.source_key] = job_id
        self._queue.append(job_id)
        self.job_state_changed.emit(job_id, source, InterpretationState.QUEUED)
        self.active_count_changed.emit(self.active_count)
        QTimer.singleShot(0, self._start_queued)
        return job_id

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.cancelled = True
        if job_id in self._queue:
            self._queue.remove(job_id)
        if job.reply is not None and job.reply.isRunning():
            job.reply.abort()
        else:
            self._finish_cancelled(job)
        return True

    def cancel_source(self, source: InterpretationSource | str) -> bool:
        job_id = self.job_for_source(source)
        return self.cancel(job_id) if job_id else False

    def shutdown(self) -> bool:
        for job_id in tuple(self._jobs):
            self.cancel(job_id)
        for request_id, probe in tuple(self._probes.items()):
            probe.reply.abort()
            self._finish_probe(request_id, False, "连接测试已取消。")
        return True

    def test_connection(self, profile_id: str) -> str:
        profile = self.profiles.profile(profile_id)
        if profile is None:
            raise ValueError("请选择已保存的 LLM 模型配置")
        api_key = self.profiles.api_key(profile_id)
        if not api_key:
            raise ValueError("当前模型配置没有 API Key")
        http_request = build_request(
            profile,
            api_key,
            system_prompt="你是连接测试助手。",
            user_prompt="只回复 OK。",
            max_output_tokens=32,
        )
        request_id = uuid4().hex
        reply = self._post(http_request.url, http_request.headers, http_request.body)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(20_000)
        probe = _Probe(request_id, profile, api_key, reply, timer)
        self._probes[request_id] = probe
        timer.timeout.connect(lambda value=request_id: self._timeout_probe(value))
        reply.finished.connect(lambda value=request_id: self._on_probe_finished(value))
        timer.start()
        return request_id

    def _start_queued(self) -> None:
        while self._running_job_count() < self.max_concurrent and self._queue:
            job_id = self._queue.pop(0)
            job = self._jobs.get(job_id)
            if job is None or job.cancelled:
                continue
            self._start_job(job)

    def _start_job(self, job: _Job) -> None:
        request = build_request(
            job.profile,
            job.api_key,
            system_prompt=job.bundle.system_prompt,
            user_prompt=job.bundle.user_prompt,
            max_output_tokens=job.bundle.max_output_tokens,
        )
        job.timed_out = False
        job.reply = self._post(request.url, request.headers, request.body)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(self.timeout_ms)
        timer.timeout.connect(lambda value=job.job_id: self._timeout_job(value))
        job.timeout_timer = timer
        job.reply.finished.connect(lambda value=job.job_id: self._on_job_finished(value))
        timer.start()
        _write_status_safely(
            job.source,
            InterpretationState.RUNNING,
            message="正在等待 LLM 返回解读结果。",
            profile=job.profile,
            depth=job.bundle.depth,
            job_id=job.job_id,
        )
        self.job_state_changed.emit(job.job_id, job.source, InterpretationState.RUNNING)

    def _post(self, url: str, headers: Mapping[str, str], body: bytes) -> QNetworkReply:
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy,
        )
        for name, value in headers.items():
            request.setRawHeader(name.encode("ascii"), value.encode("utf-8"))
        return self.network.post(request, QByteArray(body))

    def _timeout_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.reply is None:
            return
        job.timed_out = True
        job.reply.abort()

    def _on_job_finished(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.reply is None:
            return
        reply = job.reply
        if job.timeout_timer is not None:
            job.timeout_timer.stop()
            job.timeout_timer.deleteLater()
            job.timeout_timer = None
        job.reply = None
        status_value = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        status_code = int(status_value) if isinstance(status_value, int) else None
        retry_after_ms = _retry_after_ms(reply)
        payload = bytes(reply.readAll())
        reply.deleteLater()

        if job.cancelled:
            self._finish_cancelled(job)
            return
        if job.timed_out:
            self._finish_failed(job, "LLM 请求超时，请检查网络或调整服务端配置。")
            return
        if status_code in {429, 500, 502, 503, 504} and job.retries < 1:
            job.retries += 1
            delay_ms = min(30_000, max(1_000, retry_after_ms))
            _write_status_safely(
                job.source,
                InterpretationState.QUEUED,
                message="服务暂时不可用，等待一次自动重试。",
                profile=job.profile,
                depth=job.bundle.depth,
                job_id=job.job_id,
            )
            self.job_state_changed.emit(job.job_id, job.source, InterpretationState.QUEUED)
            QTimer.singleShot(delay_ms, lambda value=job.job_id: self._requeue(value))
            self._start_queued()
            return
        if status_code is None or not 200 <= status_code < 300:
            self._finish_failed(
                job,
                safe_error_message(
                    payload,
                    status_code=status_code,
                    secrets=(job.api_key,),
                ),
            )
            return
        if len(payload) > MAX_RESPONSE_BYTES:
            self._finish_failed(job, "LLM 响应超过 1 MiB 安全上限。")
            return
        try:
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("LLM 响应顶层不是 JSON object")
            parsed = parse_response(job.profile.api_format, value)
            response_text = redact_sensitive_text(parsed.text, secrets=(job.api_key,))
            usage = _redact_sensitive_value(parsed.usage, secrets=(job.api_key,))
            record = build_interpretation_record(
                job.source,
                job.profile,
                job.bundle,
                response_text,
                usage if isinstance(usage, Mapping) else {},
            )
            descriptor = write_interpretation(job.source, job.profile, record)
        except Exception as exc:
            self._finish_failed(job, _safe_local_error(exc, secrets=(job.api_key,)))
            return
        self._remove_job(job)
        self.job_state_changed.emit(job.job_id, job.source, InterpretationState.COMPLETED)
        self.job_completed.emit(job.job_id, job.source, descriptor)
        self.notification_requested.emit(
            "LLM 解读已完成",
            f"“{job.source.display_name}”的解读报告已保存。",
            True,
        )

    def _requeue(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.cancelled:
            return
        self._queue.append(job_id)
        self._start_queued()

    def _finish_failed(self, job: _Job, message: str) -> None:
        safe_message = str(message).strip()[:1000] or "LLM 解读失败。"
        _write_status_safely(
            job.source,
            InterpretationState.FAILED,
            message=safe_message,
            profile=job.profile,
            depth=job.bundle.depth,
            job_id=job.job_id,
        )
        self._remove_job(job)
        self.job_state_changed.emit(job.job_id, job.source, InterpretationState.FAILED)
        self.job_failed.emit(job.job_id, job.source, safe_message)
        self.notification_requested.emit(
            "LLM 解读失败",
            f"“{job.source.display_name}”：{safe_message}",
            False,
        )

    def _finish_cancelled(self, job: _Job) -> None:
        _write_status_safely(
            job.source,
            InterpretationState.CANCELLED,
            message="用户已取消 LLM 解读。",
            profile=job.profile,
            depth=job.bundle.depth,
            job_id=job.job_id,
        )
        self._remove_job(job)
        self.job_state_changed.emit(job.job_id, job.source, InterpretationState.CANCELLED)

    def _remove_job(self, job: _Job) -> None:
        if job.job_id in self._queue:
            self._queue.remove(job.job_id)
        self._jobs.pop(job.job_id, None)
        self._source_jobs.pop(job.source.source_key, None)
        self.active_count_changed.emit(self.active_count)
        QTimer.singleShot(0, self._start_queued)

    def _running_job_count(self) -> int:
        return sum(job.reply is not None for job in self._jobs.values())

    def _timeout_probe(self, request_id: str) -> None:
        probe = self._probes.get(request_id)
        if probe is not None:
            probe.reply.abort()
            self._finish_probe(request_id, False, "连接测试超时。")

    def _on_probe_finished(self, request_id: str) -> None:
        probe = self._probes.get(request_id)
        if probe is None:
            return
        reply = probe.reply
        status_value = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        status_code = int(status_value) if isinstance(status_value, int) else None
        payload = bytes(reply.readAll())
        if status_code is not None and 200 <= status_code < 300:
            try:
                value = json.loads(payload.decode("utf-8"))
                if not isinstance(value, Mapping):
                    raise ValueError
                parse_response(probe.profile.api_format, value)
            except Exception:
                self._finish_probe(request_id, False, "服务已响应，但返回格式与所选 API 格式不匹配。")
                return
            self._finish_probe(request_id, True, "连接、鉴权和模型调用均成功。")
            return
        self._finish_probe(
            request_id,
            False,
            safe_error_message(
                payload,
                status_code=status_code,
                secrets=(probe.api_key,),
            ),
        )

    def _finish_probe(self, request_id: str, success: bool, message: str) -> None:
        probe = self._probes.pop(request_id, None)
        if probe is None:
            return
        probe.timer.stop()
        probe.timer.deleteLater()
        probe.reply.deleteLater()
        self.connection_test_finished.emit(request_id, success, message)


def _retry_after_ms(reply: QNetworkReply) -> int:
    raw = bytes(reply.rawHeader("Retry-After")).decode("ascii", errors="ignore").strip()
    try:
        return int(float(raw) * 1000)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return 1_000
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds() * 1000))


def _safe_local_error(exc: Exception, *, secrets: Sequence[str] = ()) -> str:
    message = redact_sensitive_text(exc, secrets=secrets).strip().replace("\n", " ")[:800]
    return message or exc.__class__.__name__


def _redact_sensitive_value(value: Any, *, secrets: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redact_sensitive_value(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_value(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_value(item, secrets=secrets) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value, secrets=secrets)
    return value


def _write_status_safely(*args: Any, **kwargs: Any) -> None:
    try:
        write_status(*args, **kwargs)
    except (OSError, UnicodeError, ValueError):
        pass


__all__ = ["InterpretationJobManager"]
