"""跨任务合并下载单个匿名检查（POST /jobs/merged-study/download）。

数据来源严格限定为“各已完成任务 output_dir 中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**，且只接受相同密钥指纹的任务：

1. 从各任务成功输出中选出属于该匿名 StudyInstanceUID 的全部实例作为初选集合；
2. 在所有指定任务的成功输出上建立全局匿名 SOPInstanceUID 索引，递归读取集合内
   匿名 DICOM 任意序列层级的 ReferencedSOPInstanceUID，把**跨任务**被引用的实例
   （可能属于其他检查）补入集合，循环至闭包稳定；
3. 合并按匿名 SOPInstanceUID 去重：同一 UID 的多份输出**逐字节一致**时只保留一份，
   **不一致**则收集全部冲突 UID 一并返回 409；引用目标在所有指定任务中均不存在时
   同样收集后返回 409，**禁止生成不完整 ZIP**。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from dataclasses import dataclass

import pydicom

from .anonymizer import FileReport, JobResult
from .study_subset import iter_referenced_sop_uids

# 409 问题原因
REASON_MISSING_IN_ALL_JOBS = "missing_in_all_jobs"
REASON_BYTE_CONFLICT = "byte_conflict"


class MergedStudyError(Exception):
    """合并解析失败；http_status 由路由层映射为 HTTPException。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        study_uid: str | None = None,
        problems: list[dict] | None = None,
        **extra,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.detail: dict = {"code": code, "message": message}
        if study_uid is not None:
            self.detail["study_instance_uid"] = study_uid
        if problems:
            self.detail["problems"] = problems
        self.detail.update(extra)


@dataclass(frozen=True)
class OutputEntry:
    """某任务成功输出中的一个匿名文件（跨任务全局索引的最小单元）。"""
    job_id: str
    result: JobResult
    report: FileReport

    @property
    def sop_uid(self) -> str:
        return self.report.sop_instance_uid

    @property
    def path(self) -> str:
        return os.path.join(self.result.output_dir, self.report.output_name)


@dataclass
class ClosureStats:
    initial_uids: int = 0
    added_uids: int = 0
    deduped_copies: int = 0


def _successful_entries(job) -> list[OutputEntry]:
    """任务内真正写出且磁盘文件仍存在的匿名输出（error/无输出名者剔除）。"""
    entries: list[OutputEntry] = []
    result: JobResult = job.result
    for report in result.files:
        if (
            report.status == "error"
            or not report.output_name
            or not report.sop_instance_uid
        ):
            continue
        entry = OutputEntry(job_id=job.id, result=result, report=report)
        if not os.path.isfile(entry.path):
            continue
        entries.append(entry)
    return entries


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_merged_closure(
    jobs: list, study_uid: str
) -> tuple[dict[str, OutputEntry], ClosureStats]:
    """在多任务成功输出上解析匿名检查的跨任务引用闭包。

    返回 (uid -> 选中的唯一输出条目, 统计)；无法闭合或字节冲突时抛 MergedStudyError。
    """
    # 全局匿名 SOP UID -> 全部任务中的输出条目（同任务重复 SOP 也会有多个）
    index: dict[str, list[OutputEntry]] = {}
    for job in jobs:
        for entry in _successful_entries(job):
            index.setdefault(entry.sop_uid, []).append(entry)
    for targets in index.values():
        targets.sort(key=lambda e: (e.job_id, e.report.output_name))

    initial = [
        entry
        for entry in sorted(
            (e for targets in index.values() for e in targets),
            key=lambda e: (e.job_id, e.report.output_name),
        )
        if entry.report.study_instance_uid == study_uid
    ]
    # 同一物理文件（同任务重复 SOP）只在初选集合中出现一次
    initial_keys = {(e.job_id, e.report.output_name) for e in initial}
    if not initial_keys:
        raise MergedStudyError(
            "STUDY_NOT_FOUND",
            404,
            "该匿名 StudyInstanceUID 在任一指定任务的成功输出中均不存在",
            study_uid=study_uid,
        )

    initial_uids = {e.sop_uid for e in initial}
    stats = ClosureStats(initial_uids=len(initial_uids))
    queue: list[OutputEntry] = list(initial)
    queued = set(initial_keys)
    # uid -> 该闭包内出现过的全部物理输出
    members: dict[str, list[OutputEntry]] = {
        uid: [e for e in initial if e.sop_uid == uid] for uid in initial_uids
    }
    missing: set[str] = set()

    while queue:
        entry = queue.pop()
        try:
            ds = pydicom.dcmread(entry.path)
        except Exception as exc:  # 自己写出的匿名文件不可读属于服务端异常
            raise MergedStudyError(
                "ANONYMIZED_OUTPUT_UNREADABLE",
                500,
                f"无法读取匿名输出文件（任务 {entry.job_id}）：{exc}",
                study_uid=study_uid,
            ) from exc

        for ref_uid in iter_referenced_sop_uids(ds):
            targets = index.get(ref_uid, [])
            if not targets:
                missing.add(ref_uid)
                continue
            bucket = members.setdefault(ref_uid, [])
            seen_keys = {(e.job_id, e.report.output_name) for e in bucket}
            for target in targets:
                key = (target.job_id, target.report.output_name)
                if key not in seen_keys:
                    seen_keys.add(key)
                    bucket.append(target)
                if key not in queued:
                    queued.add(key)
                    queue.append(target)

    problems: list[dict] = []

    # 引用目标在所有指定任务的成功输出中均不存在
    for uid in sorted(missing):
        problems.append({
            "sop_instance_uid": uid,
            "reason": REASON_MISSING_IN_ALL_JOBS,
            "job_ids": [],
            "message": "引用目标在所有指定任务的成功输出中均不存在",
        })

    # 同一匿名 SOP UID 的多份物理输出必须逐字节一致；不一致则禁止合并
    chosen: dict[str, OutputEntry] = {}
    for uid in sorted(members):
        entries = sorted(
            members[uid], key=lambda e: (e.job_id, e.report.output_name)
        )
        hashes: dict[str, tuple[str, str]] = {}  # sha256 -> (job_id, output_name)
        copies: list[dict] = []
        for entry in entries:
            try:
                digest = _sha256(entry.path)
            except OSError as exc:
                raise MergedStudyError(
                    "ANONYMIZED_OUTPUT_UNREADABLE",
                    500,
                    f"无法读取匿名输出文件（任务 {entry.job_id}）：{exc}",
                    study_uid=study_uid,
                ) from exc
            copies.append({
                "job_id": entry.job_id,
                "output_file": entry.report.output_name,
                "sha256": digest,
            })
            hashes.setdefault(digest, (entry.job_id, entry.report.output_name))

        if len(hashes) > 1:
            problems.append({
                "sop_instance_uid": uid,
                "reason": REASON_BYTE_CONFLICT,
                "job_ids": sorted({e.job_id for e in entries}),
                "copies": copies,
                "message": (
                    "同一匿名 SOPInstanceUID 在不同任务（或重复输出）中的文件字节不一致，"
                    "无法确定唯一内容"
                ),
            })
            continue

        # 内容等价时任选一份写出：优先规范命名的 <uid>.dcm，其次按 (任务, 文件名) 最小
        canonical = f"{uid}.dcm"
        winner = next(
            (e for e in entries if e.report.output_name == canonical), entries[0]
        )
        chosen[uid] = winner
        stats.deduped_copies += len(entries) - 1

    if problems:
        raise MergedStudyError(
            "MERGED_REFERENCE_CLOSURE_INCOMPLETE",
            409,
            "跨任务引用闭包无法在指定任务的成功输出内闭合（存在缺失引用或字节冲突），"
            "已拒绝输出合并 ZIP",
            study_uid=study_uid,
            problems=problems,
            conflicting_sop_instance_uids=sorted(
                p["sop_instance_uid"]
                for p in problems if p["reason"] == REASON_BYTE_CONFLICT
            ),
            missing_sop_instance_uids=sorted(
                p["sop_instance_uid"]
                for p in problems if p["reason"] == REASON_MISSING_IN_ALL_JOBS
            ),
        )

    stats.added_uids = len(chosen) - stats.initial_uids
    return chosen, stats


def build_merged_zip(entries_by_uid: dict[str, OutputEntry], dest_zip: str) -> str:
    """以匿名 SOPInstanceUID.dcm 扁平打包；同名（任务内重复 SOP）自动去重。"""
    used_names: set[str] = set()
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for uid in sorted(entries_by_uid):
            entry = entries_by_uid[uid]
            arcname = f"{uid}.dcm"
            # 正常情况下匿名 UID 唯一；防御同名冲突（不同 UID 不应发生）
            if arcname in used_names:
                continue
            used_names.add(arcname)
            zf.write(entry.path, arcname=arcname)
    return dest_zip


def write_merged_study_zip(
    jobs: list, study_uid: str, work_dir: str
) -> tuple[str, ClosureStats]:
    """构建合并 ZIP 到工作目录下的临时文件，返回 (路径, 统计)。"""
    entries_by_uid, stats = resolve_merged_closure(jobs, study_uid)

    fd, tmp_path = tempfile.mkstemp(
        prefix="merged-study-",
        suffix=".zip",
        dir=work_dir,
    )
    os.close(fd)
    try:
        build_merged_zip(entries_by_uid, tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path, stats
