"""AstrBot plugin for the deepseek-sing gateway.

Thin client only — every generation happens on an OpenAI-compatible
``sing-song`` endpoint: either the hosted FlowBee gateway (per-call billing on
the user's own key) or a self-hosted sing-gateway (own Replicate token, own
voice whitelist file). No local audio stack is required.

Voice names are not enumerable: users pick names on the voice plaza
(``/音色`` shows the link) and pass them to ``/点歌`` or the LLM tools; the
gateway validates them.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import File, Record

SING_MODEL = "sing-song"
SEARCH_MODEL = "sing-search"
# Cold starts can take 2-4 minutes; the upstream relay itself allows ~15 minutes.
REQUEST_TIMEOUT = 960.0
PITCH_MODES = ("no-change", "male-to-female", "female-to-male")
# Mixing gains in dB: 0 keeps a stem (accompaniment / backing harmonies), -60
# mutes it. The gateway clamps to the same range.
VOLUME_MIN = -60
VOLUME_MAX = 0
# Trailing "/点歌 ... 伴奏[-dB]" token that turns the accompaniment on.
_MIX_TOKEN_RE = re.compile(r"\s+((?:带)?伴奏(-?\d+)?)\s*$")
FLOWBEE_KEY_URL = "https://flowbee.top"
PLAZA_URL = "https://voice-models.flowbee.top"
PLUGIN_NAME = "astrbot_plugin_deepseek_sing"

# Optional usage limits: a rolling 24h window, so quota frees up gradually
# instead of resetting at midnight.
LIMIT_WINDOW_SECONDS = 24 * 3600
_RESERVED_EXTRA = "deepseek_sing_quota_reserved"
JOB_POLL_INTERVAL = 3.0


class _JobUnavailable(RuntimeError):
    """The endpoint has no job API (old gateway, or new-api without the plugin)."""

# Config keys grouped into cards in _conf_schema.json (nested dicts since v0.2.0).
_CONFIG_GROUPS = {
    "flowbee_base_url": "flowbee",
    "flowbee_api_key": "flowbee",
    "gateway_base_url": "selfhosted",
    "gateway_token": "selfhosted",
    "voices": "selfhosted",
    "voices_file": "selfhosted",
    "voice": "singing",
    "duration": "singing",
    "start": "singing",
    "pitch": "singing",
    "pitch_mode": "singing",
    "instrumental_volume": "singing",
    "backup_vocals_volume": "singing",
    "transport": "singing",
    "daily_per_user": "limits",
    "daily_per_group": "limits",
    "cooldown_seconds": "limits",
    "daily_total": "limits",
}

USAGE_HINT = (
    "用法：/点歌 歌名 [音色名] [伴奏[-dB]]　·　/音色 音色广场　·　/点歌帮助\n"
    "音色名可省略：未指定时使用插件配置的默认音色，未配置则用网关默认。"
)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _limit_value(value: Any, default: int = -1) -> int:
    """Read a usage limit; any negative value means unlimited."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _timestamps(value: Any) -> list[float]:
    """Keep only usable numbers so a hand-edited ledger cannot break a decision."""
    if not isinstance(value, list):
        return []
    return [float(entry) for entry in value if isinstance(entry, (int, float))]


def _entity_map(value: Any) -> dict[str, list[float]]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _timestamps(entries) for key, entries in value.items()}


def _command_arg(event: AstrMessageEvent, command: str) -> str:
    """Strip the leading command token (with or without ``/``) from the text."""
    text = (event.message_str or "").strip()
    for prefix in (f"/{command}", command):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _describe_error(exc: BaseException) -> str:
    """Readable failure text; httpx timeouts stringify to an empty string."""
    text = str(exc).strip()
    if text:
        return text
    name = type(exc).__name__
    if "Timeout" in name:
        return f"网关在 {int(REQUEST_TIMEOUT)} 秒内没有返回结果，请稍后重试"
    return f"网关请求中断（{name}），请稍后重试"


class _UsageLog:
    """Rolling 24h ledger of generations, keyed by user / group / whole instance.

    Volume here is tens of rows a day, so it stays a JSON file rather than a
    database: expired timestamps are pruned on every decision and the file is
    rewritten when a slot is taken. Persistence is best-effort — a failed write
    only warns, because a broken ledger must never take singing down with it.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.users: dict[str, list[float]] = {}
        self.groups: dict[str, list[float]] = {}
        self.total: list[float] = []
        if path is not None:
            self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning(f"[deepseek-sing] 用量账本读取失败，从空账本开始：{self.path}", exc_info=True)
            return
        if not isinstance(raw, dict):
            return
        self.users = _entity_map(raw.get("users"))
        self.groups = _entity_map(raw.get("groups"))
        self.total = _timestamps(raw.get("total"))

    def _save(self) -> None:
        if self.path is None:
            return
        payload = {"users": self.users, "groups": self.groups, "total": self.total}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            logger.warning(f"[deepseek-sing] 用量账本写入失败，限制仅在内存生效：{self.path}", exc_info=True)

    def _prune(self, now: float) -> None:
        cutoff = now - LIMIT_WINDOW_SECONDS
        for bucket in (self.users, self.groups):
            for key in list(bucket):
                kept = [ts for ts in bucket[key] if ts > cutoff]
                if kept:
                    bucket[key] = kept
                else:
                    del bucket[key]
        self.total = [ts for ts in self.total if ts > cutoff]

    async def consume(
        self,
        *,
        user_key: str,
        group_key: str,
        per_user: int,
        per_group: int,
        per_total: int,
        cooldown: int,
    ) -> str | None:
        """Book one generation; returns the text to show the user when blocked."""
        now = time.time()
        async with self.lock:
            self._prune(now)
            user_used = self.users.get(user_key, [])
            if cooldown >= 0 and user_used and now - user_used[-1] < cooldown:
                wait = max(1, int(cooldown - (now - user_used[-1])))
                return f"⏱️ 点歌太快啦，请 {wait} 秒后再试。"
            if 0 <= per_user <= len(user_used):
                return f"🚫 你最近 24 小时点歌已达上限（{per_user} 首），稍后再来～"
            if 0 <= per_group <= len(self.groups.get(group_key, [])):
                return f"🚫 本群最近 24 小时点歌已达上限（{per_group} 首），稍后再来～"
            if 0 <= per_total <= len(self.total):
                return "🚫 机器人最近 24 小时的点歌额度已用完，稍后再来。"
            self.users.setdefault(user_key, []).append(now)
            self.groups.setdefault(group_key, []).append(now)
            self.total.append(now)
            self._save()
        return None

    async def release(self, *, user_key: str, group_key: str) -> None:
        """Return a booked slot after a failed generation."""
        async with self.lock:
            for bucket, key in ((self.users, user_key), (self.groups, group_key)):
                entries = bucket.get(key)
                if entries:
                    entries.pop()
                    if not entries:
                        del bucket[key]
            if self.total:
                self.total.pop()
            self._save()


@register(
    PLUGIN_NAME,
    "leicanxi",
    "AI 唱歌：B站点歌 + RVC 翻唱，走 sing-song 网关（FlowBee 免部署或自托管），音色白名单可自配",
    "0.3.0",
    "https://github.com/leicanxi/astrbot_plugin_deepseek_sing",
)
class DeepSeekSingPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._usage = _UsageLog(self._usage_path())
        self._sync_voices_file()

    def _usage_path(self) -> Path | None:
        """Ledger location inside the plugin data dir; None keeps accounting in memory only."""
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "usage.json"
        except (OSError, RuntimeError, ValueError):
            logger.warning("[deepseek-sing] 无法定位插件数据目录，使用限制仅在内存生效", exc_info=True)
            return None

    # ---- configuration -----------------------------------------------------

    def _get(self, key: str, default: Any = None) -> Any:
        """Read the grouped (v0.2.0 nested) config first, then legacy flat keys.

        Flattening the schema orphans values saved by older plugin versions;
        falling back keeps pre-upgrade configs working until re-saved.
        """
        group = _CONFIG_GROUPS.get(key)
        if group:
            sub = self.config.get(group)
            if isinstance(sub, dict):
                value = sub.get(key)
                if value not in (None, ""):
                    return value
        return self.config.get(key, default)

    @property
    def default_voice(self) -> str:
        return str(self._get("voice") or "").strip()

    @property
    def mode(self) -> str:
        return str(self.config.get("mode") or "flowbee").strip().lower()

    @property
    def base_url(self) -> str:
        key = "gateway_base_url" if self.mode == "self-hosted" else "flowbee_base_url"
        return str(self._get(key) or "").strip().rstrip("/")

    @property
    def transport(self) -> str:
        """``auto`` prefers the job API, ``job`` requires it, ``sync`` never uses it."""
        mode = str(self._get("transport") or "auto").strip().lower()
        return mode if mode in {"auto", "job", "sync"} else "auto"

    @property
    def api_root(self) -> str:
        """Base URL without the OpenAI ``/v1`` suffix, for job routes."""
        base = self.base_url
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base.rstrip("/")

    @property
    def api_key(self) -> str:
        key = "gateway_token" if self.mode == "self-hosted" else "flowbee_api_key"
        return str(self._get(key) or "").strip()

    def _require_key(self) -> str:
        key = self.api_key
        if key:
            return key
        if self.mode == "self-hosted":
            raise RuntimeError("未配置网关令牌，请在插件配置里填写 gateway_token（即网关的 SING_GATEWAY_TOKEN）")
        raise RuntimeError(f"未配置 FlowBee API Key，请在插件配置里填写（{FLOWBEE_KEY_URL} 获取）")

    def _gen_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "duration": _clamp_int(self._get("duration"), 25, 5, 120),
            "start": _clamp_int(self._get("start"), 60, 0, 3600),
            "pitch": _clamp_int(self._get("pitch"), 0, -24, 24),
            "instrumental_volume": _clamp_int(
                self._get("instrumental_volume"), VOLUME_MIN, VOLUME_MIN, VOLUME_MAX
            ),
            "backup_vocals_volume": _clamp_int(
                self._get("backup_vocals_volume"), VOLUME_MIN, VOLUME_MIN, VOLUME_MAX
            ),
        }
        pitch_mode = str(self._get("pitch_mode") or "no-change").strip()
        if pitch_mode in PITCH_MODES:
            params["pitch_mode"] = pitch_mode
        return params

    # ---- optional usage limits ----------------------------------------------

    @staticmethod
    def _entity_keys(event: AstrMessageEvent) -> tuple[str, str]:
        """(user, group) ledger keys.

        Private chats have no group id; scoping their group quota to ``dm:<user>``
        keeps unrelated DMs from sharing a single pool.
        """
        user_key = event.get_sender_id() or event.unified_msg_origin
        group_key = event.get_group_id() or f"dm:{user_key}"
        return user_key, group_key

    async def _enforce_limits(self, event: AstrMessageEvent) -> str | None:
        """Book one generation against the configured limits.

        Returns the text to answer with when the caller must not generate. Admins
        are exempt and consume nothing.
        """
        per_user = _limit_value(self._get("daily_per_user"))
        per_group = _limit_value(self._get("daily_per_group"))
        per_total = _limit_value(self._get("daily_total"))
        cooldown = _limit_value(self._get("cooldown_seconds"))
        if per_user < 0 and per_group < 0 and per_total < 0 and cooldown < 0:
            return None
        if event.is_admin():
            return None
        user_key, group_key = self._entity_keys(event)
        refusal = await self._usage.consume(
            user_key=user_key,
            group_key=group_key,
            per_user=per_user,
            per_group=per_group,
            per_total=per_total,
            cooldown=cooldown,
        )
        if refusal is None:
            event.set_extra(_RESERVED_EXTRA, True)
        return refusal

    async def _refund_limits(self, event: AstrMessageEvent) -> None:
        """Hand back the booked slot after a failed generation — nothing was billed."""
        if not event.get_extra(_RESERVED_EXTRA):
            return
        event.set_extra(_RESERVED_EXTRA, False)
        user_key, group_key = self._entity_keys(event)
        await self._usage.release(user_key=user_key, group_key=group_key)

    # ---- gateway client ----------------------------------------------------

    async def _chat(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("未配置网关地址")
        key = self._require_key()
        request = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
            ],
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=request,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"网关请求失败：HTTP {response.status_code} {self._error_detail(response)}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("网关返回格式不正确") from exc
        if isinstance(content, str):
            try:
                block = json.loads(content)
            except ValueError as exc:
                raise RuntimeError("网关返回内容不是 JSON") from exc
        else:
            block = content
        if not isinstance(block, dict):
            raise RuntimeError("网关返回内容格式不正确")
        return block

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text[:300]
        if isinstance(data, dict):
            detail = data.get("detail") or data.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message")
            if detail:
                return str(detail)[:300]
        return response.text[:300]

    # ---- voices file sync (self-hosted) ------------------------------------

    def _sync_voices_file(self) -> None:
        """Write the configured voices JSON into the gateway voices file."""
        if self.mode != "self-hosted":
            return
        raw = self._get("voices")
        if not isinstance(raw, dict) or not raw:
            return
        text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
        target = Path(
            str(self._get("voices_file") or "~/.octop/sing-voices.json")
        ).expanduser()
        try:
            if target.exists() and target.read_text(encoding="utf-8") == text:
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            logger.info(f"[deepseek-sing] 已同步音色白名单到 {target}")
        except OSError:
            logger.warning(f"[deepseek-sing] 音色白名单同步失败：{target}", exc_info=True)

    # ---- generation ---------------------------------------------------------

    def _job_payload(
        self, song: str, voice: str, mix: dict[str, int] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = dict(self._gen_params())
        # Per-request gains (tool args, /点歌 伴奏 token) override the defaults.
        payload.update(mix or {})
        if song.startswith(("http://", "https://")):
            payload["url"] = song
        else:
            payload["song"] = song
        voice = (voice or "").strip() or self.default_voice
        if voice:
            payload["voice"] = voice
        return payload

    async def _sing_sync(
        self, song: str, voice: str = "", mix: dict[str, int] | None = None
    ) -> dict[str, Any]:
        block = await self._chat(SING_MODEL, self._job_payload(song, voice, mix))
        source = block.get("source")
        if block.get("type") != "audio" or not isinstance(source, dict) or not source.get("url"):
            raise RuntimeError(str(block.get("error") or "网关没有返回音频"))
        return block

    async def _sing_job(
        self, song: str, voice: str = "", mix: dict[str, int] | None = None
    ) -> dict[str, Any]:
        payload = self._job_payload(song, voice, mix)
        request = {"model": SING_MODEL, **payload}
        async with httpx.AsyncClient(timeout=30.0) as client:
            job = await self._submit_job(client, request)
            data = await self._poll_job(client, str(job["id"]), job)
        block = self._job_block(data)
        if not block["source"]["url"]:
            raise RuntimeError(str(data.get("error") or "网关没有返回音频"))
        return block

    async def _submit_job(
        self, client: httpx.AsyncClient, request: dict[str, Any]
    ) -> dict[str, Any]:
        response = await client.post(
            f"{self.api_root}/sing/v1/jobs",
            headers={"Authorization": f"Bearer {self._require_key()}"},
            json=request,
        )
        if response.status_code in (404, 405, 501):
            raise _JobUnavailable(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise RuntimeError(
                f"网关请求失败：HTTP {response.status_code} {self._error_detail(response)}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("网关返回格式不正确") from exc
        job_id = str(data.get("id") or "").strip() if isinstance(data, dict) else ""
        if not job_id:
            raise RuntimeError("网关没有返回任务 ID")
        return {**data, "id": job_id}

    async def _poll_job(
        self, client: httpx.AsyncClient, job_id: str, first: dict[str, Any]
    ) -> dict[str, Any]:
        status = str(first.get("status") or "")
        if status == "succeeded":
            return first
        if status == "failed":
            raise RuntimeError(str(first.get("error") or "生成失败"))
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(JOB_POLL_INTERVAL)
            response = await client.get(
                f"{self.api_root}/sing/v1/jobs/{job_id}",
                headers={"Authorization": f"Bearer {self._require_key()}"},
            )
            if response.status_code == 404:
                raise RuntimeError("任务不存在或已过期")
            if response.status_code >= 400:
                raise RuntimeError(
                    f"查询任务失败：HTTP {response.status_code} {self._error_detail(response)}"
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("网关返回格式不正确") from exc
            status = str(data.get("status") or "") if isinstance(data, dict) else ""
            if status == "succeeded":
                return data
            if status == "failed":
                raise RuntimeError(str(data.get("error") or "生成失败"))
        raise TimeoutError(f"任务 {job_id} 在 {int(REQUEST_TIMEOUT)} 秒内没有完成")

    @staticmethod
    def _job_block(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "audio",
            "source": {"type": "url", "url": str(data.get("audio_url") or "")},
            "media_type": "audio/mpeg",
            "filename": str(data.get("filename") or ""),
            "title": str(data.get("title") or ""),
            "artist": str(data.get("artist") or ""),
            "duration_seconds": int(data.get("duration_seconds") or 0),
            "cached": bool(data.get("cached")),
        }

    async def _generate(
        self, song: str, voice: str = "", mix: dict[str, int] | None = None
    ) -> dict[str, Any]:
        mode = self.transport
        if mode == "sync":
            return await self._sing_sync(song, voice, mix)
        try:
            return await self._sing_job(song, voice, mix)
        except _JobUnavailable as exc:
            if mode == "job":
                raise RuntimeError("网关没有提供任务接口，请升级网关或把 transport 改成 auto/sync") from exc
            logger.info("[deepseek-sing] 任务接口不可用，回退同步：%s", exc)
            return await self._sing_sync(song, voice, mix)

    async def _download_audio(self, url: str) -> Path:
        path = Path(tempfile.gettempdir()) / f"deepseek_sing_{uuid.uuid4().hex}.mp3"
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            path.write_bytes(response.content)
        return path

    async def _send_audio(self, event: AstrMessageEvent, url: str) -> None:
        try:
            await event.send(event.chain_result([Record(file=url)]))
            return
        except Exception:
            logger.warning("[deepseek-sing] 语音（URL）发送失败，尝试本地文件", exc_info=True)
        path: Path | None = None
        try:
            path = await self._download_audio(url)
            try:
                await event.send(event.chain_result([Record(file=str(path))]))
                return
            except Exception:
                logger.warning("[deepseek-sing] 语音（文件）发送失败，改发文件附件", exc_info=True)
            await event.send(
                event.chain_result([File(name=f"deepseek_sing_{int(time.time())}.mp3", file=str(path))])
            )
        except Exception:
            logger.warning("[deepseek-sing] 音频发送全部失败", exc_info=True)
            await event.send(event.plain_result(f"⚠️ 语音发送失败，音频链接：{url}"))
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    # ---- generation tasks ---------------------------------------------------

    def _start_generation(
        self,
        event: AstrMessageEvent,
        song: str,
        voice: str,
        *,
        mix: dict[str, int] | None = None,
        fallback_song: str | None = None,
    ) -> None:
        """Run one generation in the background so no tool/command timeout applies."""
        task = asyncio.create_task(
            self._generate_and_send(
                event,
                song,
                voice,
                mix=mix,
                fallback_song=song if fallback_song is None else fallback_song,
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _generate_and_send(
        self,
        event: AstrMessageEvent,
        song: str,
        voice: str,
        *,
        mix: dict[str, int] | None = None,
        fallback_song: str,
    ) -> None:
        try:
            try:
                block = await self._generate(song, voice, mix)
            except Exception as exc:
                if not voice or "unknown voice" not in str(exc):
                    raise
                block = await self._generate(fallback_song, "", mix)
        except Exception as exc:
            logger.warning(
                "[deepseek-sing] 点歌失败：%s: %r",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            await self._refund_limits(event)
            await self._send_text(event, f"❌ 点歌失败：{_describe_error(exc)}")
            return
        title = str(block.get("title") or "").strip()
        artist = str(block.get("artist") or "").strip()
        if title:
            # Label the version before the audio: that is what lets the user tell
            # a wrong pick without listening all the way through.
            label = f"🎤 《{title}》" + (f" — {artist}" if artist else "")
            await self._send_text(event, f"{label}\n不对就说一声，可以换一版。")
        await self._send_audio(event, str(block["source"]["url"]))

    async def _send_text(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(event.plain_result(text))
        except Exception:
            logger.warning("[deepseek-sing] 失败提示发送失败", exc_info=True)

    # ---- commands -----------------------------------------------------------

    @filter.command("点歌")
    async def diange(self, event: AstrMessageEvent):
        """点一首歌让 AI 唱：/点歌 歌名 [音色名]"""
        args = _command_arg(event, "点歌")
        if not args:
            await event.send(event.plain_result(USAGE_HINT))
            return
        self._sync_voices_file()
        # 音色名不再可枚举，由网关校验：末尾词先按音色尝试，网关报 unknown voice
        # 时回退为整串歌名重新搜索。结尾的「伴奏[-dB]」token 先消费掉，表示
        # 本次带伴奏（0 原始音量，-6 轻一些）。
        text = args
        mix: dict[str, int] = {}
        trailing = _MIX_TOKEN_RE.search(text)
        if trailing:
            mix["instrumental_volume"] = _clamp_int(
                trailing.group(2) or 0, 0, VOLUME_MIN, VOLUME_MAX
            )
            text = text[: trailing.start()].strip()
        song, voice = text, ""
        parts = text.rsplit(maxsplit=1)
        if len(parts) == 2:
            song, voice = parts[0].strip(), parts[1].strip().casefold()
        refusal = await self._enforce_limits(event)
        if refusal:
            await event.send(event.plain_result(refusal))
            return
        await event.send(event.plain_result("🎤 正在生成，请稍候（冷启动最长约 4 分钟）..."))
        self._start_generation(event, song, voice, mix=mix, fallback_song=text)

    @filter.command("音色")
    async def list_voices(self, event: AstrMessageEvent):
        """音色广场指引：/音色"""
        configured = self.default_voice
        head = (
            f"🎵 音色广场：{PLAZA_URL}\n"
            "在广场挑好音色后：/点歌 歌名 音色名（音色名可省略，用默认）"
        )
        if self.mode != "self-hosted":
            head += "\n有自己的音色模型？登录后在广场页「上传音色」提交 zip 或链接，自动审核通过后即可使用。"
        if configured:
            head += f"\n当前默认音色：{configured}（/点歌 不带音色名时使用）"
        custom = (
            "自托管模式：编辑音色白名单（配置页「音色白名单 JSON」或网关音色文件）即可。"
            if self.mode == "self-hosted"
            else f"登录 {PLAZA_URL} 后到「上传音色」页自助提交（自动审核），通过后会得到专属音色名。"
        )
        await event.send(
            event.plain_result(
                head
                + "\n💡 想固定音色？在插件配置「演唱默认参数 → 音色」里填音色名，点歌就不用每次带上。"
                + f"\n🎤 自定义音色：{custom}"
            )
        )

    @filter.command("点歌帮助")
    async def sing_help(self, event: AstrMessageEvent):
        """查看点歌帮助"""
        await event.send(
            event.plain_result(
                "🎤 DeepSeek 唱歌\n"
                "/点歌 歌名 [音色名]　AI 唱歌（音色名可省略）\n"
                "/点歌 歌名 伴奏[-dB]　带伴奏翻唱，如 /点歌 晴天 伴奏、/点歌 晴天 伴奏-6（伴奏轻 6 dB）\n"
                "/音色　音色广场链接\n\n"
                "要和声可对 AI 说「加和声」；音色省略时使用插件配置的默认音色，未配置则用网关默认；自有音色可登录音色广场后在「上传音色」页自助提交（自动审核），详见 /音色。\n"
                "也可以直接对 LLM 说「唱一首《歌名》」，LLM 会自动调用工具。\n"
                "生成按次计费或由你的 Replicate Token 承担，详见插件 README。"
            )
        )

    # ---- LLM tool -----------------------------------------------------------

    @staticmethod
    def _mix_from_tool(instrumental_volume: Any, backup_vocals_volume: Any) -> dict[str, int]:
        """Collect tool-provided mixing gains; absent or invalid values are omitted."""
        mix: dict[str, int] = {}
        for key, value in (
            ("instrumental_volume", instrumental_volume),
            ("backup_vocals_volume", backup_vocals_volume),
        ):
            if value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            mix[key] = max(VOLUME_MIN, min(VOLUME_MAX, parsed))
        return mix

    @filter.llm_tool(name="sing_song")
    async def sing_song_tool(
        self,
        event: AstrMessageEvent,
        song: str,
        voice: str = "",
        instrumental_volume: int | None = None,
        backup_vocals_volume: int | None = None,
    ) -> str:
        '''唱一首歌（AI 翻唱），会直接把音频发到当前会话。

        音频前会标明唱的是哪个版本；如果用户听完说不是这首，用 confirm_song
        拿到候选让他重选。

        Args:
            song(string): 歌曲名；用户明确确认过的 B 站歌曲链接也可以直接传。
            voice(string, optional): 音色名（用户在音色广场挑好的名字），可先调用 list_sing_voices 获取广场链接。
            instrumental_volume(int, optional): 伴奏增益（dB）：0 带原始音量伴奏，负数更轻（最小 -60）；仅当用户明确要伴奏时传 0，没提就不传（默认纯人声）。
            backup_vocals_volume(int, optional): 和声增益（dB）：0 带和声（和声也会被转成所选音色），负数更轻；仅当用户明确要和声时传。
        Returns:
            已受理提示；音频生成完成后会主动发送到当前会话。失败时以消息形式告知。
        '''
        self._sync_voices_file()
        song = (song or "").strip()
        if not song:
            return "点歌失败：缺少歌曲名"
        refusal = await self._enforce_limits(event)
        if refusal:
            return refusal
        # Return immediately: generation takes minutes and AstrBot caps tool calls
        # (tool_call_timeout, 60-120s by default). The audio is pushed when ready.
        self._start_generation(
            event,
            song,
            (voice or "").strip(),
            mix=self._mix_from_tool(instrumental_volume, backup_vocals_volume),
        )
        return "已提交生成，完成后会直接把音频发到当前会话，请稍候。"

    @filter.llm_tool(name="confirm_song")
    async def confirm_song_tool(self, event: AstrMessageEvent, song: str, limit: int = 3) -> str:
        '''重新确认要点的是哪首歌：返回网关按匹配度排好序的候选列表（免费，不生成音频）。

        第 1 条就是网关的推荐，先看它；不要自己按标题重排或挑版本——标题上的
        「深情版 / 女声版 / 原唱」这类装饰很容易误判。只有用户觉得唱的不是这首、
        需要重新确认歌曲时才调用本工具；正常点歌直接 sing_song，不要预先调用，
        一次点歌最多确认一次。

        Args:
            song(string): 用户说的歌名。
            limit(int, optional): 候选数量，默认 3。
        Returns:
            候选列表（含 title/artist/duration/url）；用户选定后把那条的 url 传给 sing_song。
        '''
        query = (song or "").strip()
        if not query:
            return "请先告诉我要确认哪首歌（歌名）。"
        try:
            block = await self._chat(
                SEARCH_MODEL, {"query": query, "limit": _clamp_int(limit, 3, 1, 5)}
            )
        except Exception as exc:
            logger.warning("[deepseek-sing] confirm_song 失败：%r", exc, exc_info=True)
            return f"没拿到候选：{_describe_error(exc)}"
        candidates = block.get("data") or []
        if not candidates:
            return f"没搜到《{query}》的候选，换个歌名再试。"
        return json.dumps(
            {
                "candidates": candidates,
                "next": (
                    "第 1 条是网关排序后的推荐，先把它报给用户确认；用户选了哪条，"
                    "就把那条的 url 传给 sing_song。不要自己按标题挑版本。"
                ),
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="list_sing_voices")
    async def list_voices_tool(self, event: AstrMessageEvent) -> str:
        """返回音色广场链接与音色使用说明。

        Returns:
            音色广场地址；用户在广场挑好音色名后，调用 sing_song 时传 voice=音色名。
        """
        return (
            f"音色目录已迁移到音色广场：{PLAZA_URL}。请指引：用户在广场挑好音色后，"
            "点歌时把音色名传给 voice；未指定音色时自动使用配置的默认音色。"
            "用户有自己的 RVC 模型时，登录后在广场页「上传音色」提交 zip 或模型直链，"
            "自动审核通过后会得到专属音色名，也可在「我的音色」里管理。点歌时不能传自定义链接。"
        )
