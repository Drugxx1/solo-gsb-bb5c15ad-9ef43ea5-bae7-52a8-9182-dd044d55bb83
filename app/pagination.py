"""一致性核对差异明细的不透明分页游标。

游标为不透明字符串，对外不暴露任何业务数据；内部用进程级随机密钥做 HMAC 签名，
并把**任务集合（排序后的 job_ids）、筛选层级 level、签发时间**绑定进载荷：

* 签名被篡改 / 载荷无法解析 -> ``CursorInvalid``（400）；
* 超过 TTL（默认 15 分钟）-> ``CursorExpired``（400）；
* 复用到另一个任务集合或另一个 level -> ``CursorScopeMismatch``（400）。

进程级签名密钥在每次启动时随机生成，因此服务重启后旧游标一律失效（按篡改处理）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

from .config import settings

_VERSION = "cc-page-v1"
# 游标是不透明令牌，超长输入只可能是伪造/粘贴错误，直接拒绝
_MAX_CURSOR_BYTES = 2048


class CursorError(ValueError):
    """游标不可用（统一映射为 HTTP 400）。"""

    code = "INVALID_CURSOR"

    def __init__(self, message: str, *, expired: bool = False,
                 scope_mismatch: bool = False) -> None:
        super().__init__(message)
        if expired:
            self.code = "CURSOR_EXPIRED"
        elif scope_mismatch:
            self.code = "CURSOR_SCOPE_MISMATCH"


class CursorCodec:
    """签名、解析并校验分页游标。"""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._key = secrets.token_bytes(32)

    # ------------------------------------------------------------------ #
    def _b64encode(self, raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _b64decode(self, text: str) -> bytes:
        try:
            padding = "=" * (-len(text) % 4)
            return base64.urlsafe_b64decode(text + padding)
        except (ValueError, TypeError) as exc:
            raise CursorError("游标编码非法（base64url 解码失败）") from exc

    def _sign(self, payload_b64: str) -> str:
        mac = hmac.new(self._key, payload_b64.encode("ascii"), hashlib.sha256)
        return self._b64encode(mac.digest())

    @staticmethod
    def scope(job_ids: list[str], level: str) -> dict[str, Any]:
        """游标绑定的查询作用域（任务集合与筛选条件）。"""
        return {"level": level, "job_ids": sorted(job_ids)}

    def encode(
        self,
        *,
        job_ids: list[str],
        level: str,
        after: Optional[list[Any]],
        now: Optional[float] = None,
    ) -> str:
        payload = {
            "v": _VERSION,
            "scope": self.scope(job_ids, level),
            "after": after,
            "iat": time.time() if now is None else now,
        }
        payload_b64 = self._b64encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        return f"{payload_b64}.{self._sign(payload_b64)}"

    def decode(
        self,
        token: str,
        *,
        job_ids: list[str],
        level: str,
        now: Optional[float] = None,
    ) -> Optional[list[Any]]:
        """校验游标并返回上一页最后一条的排序键（无游标时返回 None）。

        依次校验：编码/签名 -> 结构 -> 过期 -> 作用域（任务集合 + level）。
        """
        raw = (token or "").strip()
        if not raw:
            return None
        if len(raw) > _MAX_CURSOR_BYTES or "." not in raw:
            raise CursorError("游标格式非法")
        payload_b64, sig = raw.rsplit(".", 1)
        expected = self._sign(payload_b64)
        if not hmac.compare_digest(sig, expected):
            raise CursorError("游标签名无效（已篡改或来自其他服务实例）")

        try:
            payload = json.loads(self._b64decode(payload_b64).decode("utf-8"))
        except Exception as exc:
            raise CursorError("游标载荷无法解析") from exc
        if not isinstance(payload, dict) or payload.get("v") != _VERSION:
            raise CursorError("游标版本不支持")
        scope = payload.get("scope")
        after = payload.get("after")
        issued_at = payload.get("iat")
        if not isinstance(scope, dict) or not isinstance(issued_at, (int, float)):
            raise CursorError("游标字段缺失")
        if after is not None and not isinstance(after, list):
            raise CursorError("游标字段缺失")

        current = time.time() if now is None else now
        if current - float(issued_at) > self.ttl_seconds:
            raise CursorError("游标已过期，请从第一页重新开始", expired=True)

        if scope != self.scope(job_ids, level):
            raise CursorError(
                "游标与当前任务集合或筛选条件（level）不一致，不可跨条件复用",
                scope_mismatch=True,
            )
        return after


# 单例：进程级签名密钥；TTL 从配置读取，测试中可直接替换 codec.ttl_seconds
codec = CursorCodec(settings.cursor_ttl_seconds)
