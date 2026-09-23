"""基于用户稳定密钥的确定性派生。

同一把稳定密钥 + 同一原始标识，永远得到相同结果，因此：
* 同一患者跨批次得到相同伪名（患者级伪名）；
* 同一原始 UID 跨文件被重写为同一新 UID，检查/系列/SOP 关系得以保持。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Final

# DICOM 2.25 根弧用于“基于整数”的 UID（与 pydicom generate_uid 同一做法）
UID_ROOT: Final[str] = "2.25"
# 2.25 下整数取 128 bit，保证长度与唯一性
_UID_MODULUS: Final[int] = 1 << 128

_VERSION: Final[bytes] = b"dicom-anon-v1"


def _digest(secret: str, namespace: str, payload: str) -> bytes:
    key = _VERSION + b"|" + secret.encode("utf-8")
    msg = namespace.encode("utf-8") + b"|" + payload.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).digest()


def _b32(raw: bytes, length: int) -> str:
    text = base64.b32encode(raw).decode("ascii").rstrip("=")
    return text[:length]


def patient_token(secret: str, identity: str) -> str:
    """返回患者稳定令牌（用作伪 ID）。"""
    return _b32(_digest(secret, "patient", identity), 16)


def patient_pseudonyms(secret: str, identity: str) -> tuple[str, str]:
    """返回 (匿名 PatientID, 匿名 PatientName)，同一患者保持一致。"""
    token = patient_token(secret, identity)
    return token, f"ANON^{token[:10]}"


def derive_uid(secret: str, original_uid: str) -> str:
    """由原始 UID 确定性派生新 UID。"""
    raw = _digest(secret, "uid", original_uid.strip())
    value = int.from_bytes(raw, "big") % _UID_MODULUS
    # 避免 0
    if value == 0:
        value = 1
    return f"{UID_ROOT}.{value}"


def date_shift_days(
    secret: str, identity: str, shift_min: int = -365, shift_max: int = 365
) -> int:
    """为患者在闭区间 [shift_min, shift_max] 内派生一个稳定的日期平移天数。

    仅依赖 (secret, identity, shift_min, shift_max)，因此：
    * 同一患者在同一批次/跨批次间结果一致；
    * 范围相同时跨批次一致，范围不同时派生不同结果（区间是派生输入的一部分）；
    * 平移关闭时调用方直接使用 0，不经过本函数。

    默认区间 -365..365 刻意沿用 v1 的 ``date-shift`` 载荷，保证升级前已处理批次
    与升级后结果完全一致；自定义区间改用带范围的载荷以参与确定性。
    """
    span = shift_max - shift_min + 1
    if (shift_min, shift_max) == (-365, 365):
        raw = _digest(secret, "date-shift", identity)
        return int.from_bytes(raw[:2], "big") % 731 - 365
    raw = _digest(
        secret, "date-shift-range", f"{shift_min}:{shift_max}|{identity}"
    )
    return int.from_bytes(raw[:2], "big") % span + shift_min
