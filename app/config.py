"""运行期配置，全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # 任务工作目录（上传的 ZIP、解压内容、结果 ZIP 均放在这里）
    work_dir: str = os.getenv("DICOM_ANON_WORK_DIR", "/tmp/dicom-anon")
    # 单个上传 ZIP 的大小上限（字节）
    max_upload_bytes: int = _int("DICOM_ANON_MAX_UPLOAD_BYTES", 512 * 1024 * 1024)
    # 解压后的总大小上限（字节），防御 zip 炸弹
    max_extracted_bytes: int = _int("DICOM_ANON_MAX_EXTRACTED_BYTES", 2 * 1024 * 1024 * 1024)
    # 解压后的最大文件数量
    max_extracted_files: int = _int("DICOM_ANON_MAX_EXTRACTED_FILES", 20000)
    # 任务保留时长（秒），过期由后台任务清理
    job_ttl_seconds: int = _int("DICOM_ANON_JOB_TTL_SECONDS", 3600)
    # 稳定密钥的最小长度
    min_secret_length: int = _int("DICOM_ANON_MIN_SECRET_LENGTH", 8)
    # 一致性核对分页游标的有效期（秒）；过期游标必须重新从第一页开始
    cursor_ttl_seconds: int = _int("DICOM_ANON_CURSOR_TTL_SECONDS", 900)


settings = Settings()
