"""跨任务合并下载某匿名检查（引用闭包递归补全 + 按匿名 SOP UID 去重）。

数据来源严格限定为“多个已完成任务 output_dir 中的匿名 DICOM 文件”，
**不重新解压或处理任何原始 ZIP**：

1. 以匿名 StudyInstanceUID 在各任务成功输出中的全部实例作为初选集合；
2. 在所有指定任务上建立统一的 匿名 SOPInstanceUID -> 输出文件 索引，
   递归读取集合内匿名 DICOM 任意序列层级的 ReferencedSOPInstanceUID，
   把跨任务被引用的实例（可能属于其他检查）补入集合，循环至闭包稳定；
3. 同一匿名 SOPInstanceUID 对应多份文件时：字节完全一致只保留一份，
   字节不一致则 409 并列出冲突 UID；引用目标在所有指定任务中均不存在时，
   同样 409 并列出缺失 UID；任何情况下都不产生残缺 ZIP；
4. 只接受密钥指纹相同的任务（由路由层校验），ZIP 条目名只使用匿名 UID，
   不暴露来源任务的原始路径信息。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from dataclasses import dataclass

import pydicom

from .anonymizer import FileReport, JobResult
from .study_subset import (
    REASON_AMBIGUOUS_OUTPUT,
    REASON_NOT_IN_OUTPUT,
    _output_path,
    _successful_outputs,
    iter_referenced_sop_uids,
)

# 合并场景下 409 的问题原因
REASON_BYTE_CONFLICT = "byte_conflict"


class MergedStudyError(Exception):
    """跨任务合并解析失败；http_status 由路由层映射为 HTTPException。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        study_uid: str | None = None,
        problems: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.detail: dict = {"code": code, "message": message}
        if study_uid is not None:
            self.detail["study_instance_uid"] = study_uid
        if problems is not None:
            self.detail["problems"] = problems


@dataclass(frozen=True)
class Entry:
    """某匿名 SOPInstanceUID 在一个任务成功输出中的一份具体文件。"""

    job_id: str
    path: str
    report: FileReport

    # FileReport 为可变 dataclass 不可哈希；条目的身份只由 (任务, 路径) 决定
    def __hash__(self) -> int:
        return hash((self.job_id, self.path))

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Entry)
            and self.job_id == other.job_id
            and self.path == other.path
        )


@dataclass
class MergedClosure:
    study_uid: str
    job_ids: list[str]
    initial_uids: list[str]
    chosen: dict[str, Entry]            # uid -> 去重后保留的唯一文件
    total_copies: int                   # 去重前闭包内的文件份数
    initial_copies: int                 # 初选集合的文件份数（跨任务重复各算一份）

    @property
    def added_uid_count(self) -> int:
        """递归补入的匿名 SOP UID 数。"""
        return len(self.chosen) - len(set(self.initial_uids))

    @property
    def deduped_count(self) -> int:
        """因字节一致而被去掉的重复文件份数。"""
        return self.total_copies - len(self.chosen)

    @property
    def file_count(self) -> int:
        """ZIP 内最终条目数（按匿名 SOP UID 去重后）。"""
        return len(self.chosen)


def _entry_sort_key(entry: Entry) -> tuple[str, str]:
    return (entry.job_id, entry.report.output_name or "")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _problem_item(
    uid: str, reason: str, copies: list[Entry], hashes: dict[Entry, str]
) -> dict:
    if reason == REASON_NOT_IN_OUTPUT:
        message = "引用目标在所有指定任务的成功输出中均不存在"
    elif reason == REASON_BYTE_CONFLICT:
        message = "同一匿名 SOPInstanceUID 在不同任务中的输出字节不一致"
    else:
        message = "一个匿名 SOPInstanceUID 在同一任务内对应多个输出文件，无法唯一补入"
    return {
        "sop_instance_uid": uid,
        "reason": reason,
        "copies": [
            {
                "job_id": e.job_id,
                "output_name": e.report.output_name,
                "sha256": hashes.get(e, ""),
            }
            for e in copies
        ],
        "message": message,
    }


def resolve_merged_closure(jobs: list, study_uid: str) -> MergedClosure:
    """在多个任务的成功输出上解析匿名检查的跨任务引用闭包。

    jobs 已由路由层保证：全部存在、status=ready、密钥指纹一致。
    无法闭合（字节冲突 / 引用缺失 / 任务内歧义）时抛 MergedStudyError（409）。
    """
    # 匿名 SOP UID -> 该 UID 在所有任务中的全部输出文件
    index: dict[str, list[Entry]] = {}
    for job in jobs:
        result: JobResult = job.result
        for report in _successful_outputs(result):
            index.setdefault(report.sop_instance_uid, []).append(
                Entry(
                    job_id=job.id,
                    path=_output_path(result, report),
                    report=report,
                )
            )

    initial_set: set[str] = set()
    for job in jobs:
        for report in _successful_outputs(job.result):
            if report.study_instance_uid == study_uid:
                initial_set.add(report.sop_instance_uid)
    if not initial_set:
        raise MergedStudyError(
            "STUDY_NOT_FOUND",
            404,
            "该匿名 StudyInstanceUID 在所有指定任务的成功输出中均不存在",
            study_uid=study_uid,
        )
    initial_uids = sorted(initial_set)
    initial_copies = sum(len(index[uid]) for uid in initial_uids)

    chosen: dict[str, Entry] = {}
    total_copies = 0
    processed: set[str] = set()
    # uid -> (reason, copies)；同一 UID 只报一次，按“缺失优先”保留首次判定
    problems: dict[str, tuple[str, list[Entry]]] = {}
    hashes: dict[Entry, str] = {}
    queue: list[str] = list(initial_uids)

    def _resolve_uid(uid: str) -> list[Entry]:
        """确定一个 UID 保留的唯一文件；记录冲突/歧义；返回可继续扫描的副本。"""
        nonlocal total_copies
        if uid in processed:
            return []
        processed.add(uid)
        entries = index.get(uid, [])
        if not entries:
            problems.setdefault(uid, (REASON_NOT_IN_OUTPUT, []))
            return []
        total_copies += len(entries)

        # 同一任务内一个匿名 SOP UID 对应多个输出文件（输入 ZIP 重复实例）：
        # 不挑选、不猜测，直接视为歧义
        per_job: dict[str, list[Entry]] = {}
        for entry in entries:
            per_job.setdefault(entry.job_id, []).append(entry)
        ambiguous = next((es for es in per_job.values() if len(es) > 1), None)
        if ambiguous is not None:
            problems.setdefault(
                uid, (REASON_AMBIGUOUS_OUTPUT, sorted(entries, key=_entry_sort_key))
            )

        # 跨任务字节一致性：先比大小，再逐份算 SHA-256；全部一致只保留一份
        sizes = {entry: os.path.getsize(entry.path) for entry in entries}
        byte_conflict = len(set(sizes.values())) > 1
        for entry in entries:
            digest = _sha256_file(entry.path)
            hashes[entry] = digest
        if not byte_conflict and len(set(hashes[e] for e in entries)) > 1:
            byte_conflict = True
        if byte_conflict:
            problems.setdefault(
                uid, (REASON_BYTE_CONFLICT, sorted(entries, key=_entry_sort_key))
            )

        # 无冲突时选确定性的一份（按 job_id、output_name 排序取首），保证结果稳定
        if uid not in problems:
            chosen[uid] = sorted(entries, key=_entry_sort_key)[0]
        return entries

    while queue:
        uid = queue.pop()
        entries = _resolve_uid(uid)
        # 即使存在冲突也继续扫描各副本，尽量一次性暴露全部闭包问题；
        # 有 problems 时最终必定 409，chosen 不会被输出。
        for entry in entries:
            try:
                ds = pydicom.dcmread(entry.path)
            except Exception as exc:  # 自己写出的匿名文件不可读属于服务端异常
                raise MergedStudyError(
                    "ANONYMIZED_OUTPUT_UNREADABLE",
                    500,
                    f"无法读取匿名输出文件（任务 {entry.job_id}）"
                    f"{entry.report.output_name}：{exc}",
                    study_uid=study_uid,
                ) from exc
            for ref_uid in iter_referenced_sop_uids(ds):
                if ref_uid not in processed and ref_uid not in queue:
                    queue.append(ref_uid)

    if problems:
        problem_items = [
            _problem_item(uid, reason, copies, hashes)
            for uid, (reason, copies) in sorted(problems.items())
        ]
        raise MergedStudyError(
            "MERGED_REFERENCE_CLOSURE_INCOMPLETE",
            409,
            "跨任务引用闭包无法闭合（SOP 字节冲突或引用缺失），已拒绝输出合并 ZIP",
            study_uid=study_uid,
            problems=problem_items,
        )

    return MergedClosure(
        study_uid=study_uid,
        job_ids=sorted(job.id for job in jobs),
        initial_uids=initial_uids,
        chosen=chosen,
        total_copies=total_copies,
        initial_copies=initial_copies,
    )


# --------------------------------------------------------------------------- #
# 扁平 ZIP（条目名仅为匿名 SOPInstanceUID，不含任务 ID / 原始路径）
# --------------------------------------------------------------------------- #
def build_merged_zip(closure: MergedClosure, dest_zip: str) -> str:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for uid in sorted(closure.chosen):
            entry = closure.chosen[uid]
            zf.write(entry.path, arcname=entry.report.output_name)
    return dest_zip


def write_merged_study_zip(jobs: list, study_uid: str, work_dir: str) -> tuple[str, MergedClosure]:
    """构建合并 ZIP 到工作目录下的临时文件，返回 (路径, 闭包)。"""
    closure = resolve_merged_closure(jobs, study_uid)

    fd, tmp_path = tempfile.mkstemp(
        prefix="merged-study-",
        suffix=".zip",
        dir=work_dir,
    )
    os.close(fd)
    try:
        build_merged_zip(closure, tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path, closure
