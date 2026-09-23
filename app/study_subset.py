"""按匿名 StudyInstanceUID 下载检查子集（引用闭包自动补全）。

数据来源严格限定为“已完成任务 output_dir 中的匿名 DICOM 文件”，
**不重新解压或处理原始 ZIP**：

1. 以匿名 StudyInstanceUID 对应的全部成功输出实例作为初选集合；
2. 递归读取集合内匿名 DICOM 任意序列层级的 ReferencedSOPInstanceUID，
   把同任务内被引用的实例（可能属于其他检查）补入集合，循环至闭包稳定；
3. 引用目标不在成功输出中、或一个匿名 SOP UID 对应多个输出文件时，
   一律拒绝输出（交由路由层返回 409 与具体匿名 UID/原因），不产生残缺 ZIP。
"""
from __future__ import annotations

import os
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Optional

import pydicom
from pydicom.sequence import Sequence

from .anonymizer import FileReport, JobResult

REFERENCED_SOP_KEYWORD = "ReferencedSOPInstanceUID"

# 409 问题原因
REASON_NOT_IN_OUTPUT = "not_in_output"
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output"


class StudySubsetError(Exception):
    """子集解析失败；http_status 由路由层映射为 HTTPException。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        study_uid: Optional[str] = None,
        problems: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.detail: dict = {"code": code, "message": message}
        if study_uid is not None:
            self.detail["study_instance_uid"] = study_uid
        if problems:
            self.detail["problems"] = problems


@dataclass
class StudyClosure:
    initial_reports: list[FileReport]
    included_reports: list[FileReport]

    @property
    def initial_count(self) -> int:
        return len(self.initial_reports)

    @property
    def added_count(self) -> int:
        initial_names = {r.output_name for r in self.initial_reports}
        return sum(r.output_name not in initial_names for r in self.included_reports)

    @property
    def included_output_names(self) -> list[str]:
        return sorted(r.output_name for r in self.included_reports)


# --------------------------------------------------------------------------- #
# 索引与引用遍历
# --------------------------------------------------------------------------- #
def _output_path(result: JobResult, report: FileReport) -> str:
    return os.path.join(result.output_dir, report.output_name)


def _successful_outputs(result: JobResult) -> list[FileReport]:
    """任务内真正写出且磁盘文件仍存在的匿名输出（error/无输出名者剔除）。"""
    outputs: list[FileReport] = []
    for report in result.files:
        if (
            report.status == "error"
            or not report.output_name
            or not report.sop_instance_uid
        ):
            continue
        if not os.path.isfile(_output_path(result, report)):
            continue
        outputs.append(report)
    return outputs


def _build_sop_index(result: JobResult) -> dict[str, list[FileReport]]:
    """匿名 SOPInstanceUID -> 成功输出文件报告（通常 1 个；重复 SOP 时多个）。"""
    index: dict[str, list[FileReport]] = {}
    for report in _successful_outputs(result):
        index.setdefault(report.sop_instance_uid, []).append(report)
    return index


def _split_ui_values(value: object):
    """展开 ReferencedSOPInstanceUID 的单值 / VM>1（反斜杠分隔）/ 多值形式。"""
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value).split("\\")
    for part in parts:
        text = str(part).strip()
        if text:
            yield text


def iter_referenced_sop_uids(ds) -> list[str]:
    """收集匿名 DICOM 中任意序列层级的全部 ReferencedSOPInstanceUID。"""
    refs: list[str] = []
    stack = [ds]
    while stack:
        current = stack.pop()
        for tag_ in list(current.keys()):
            elem = current[tag_]
            if elem.VR == "SQ" and isinstance(elem.value, Sequence):
                stack.extend(elem.value)
                continue
            if (getattr(elem, "keyword", "") or "") == REFERENCED_SOP_KEYWORD:
                refs.extend(_split_ui_values(elem.value))
    return refs


# --------------------------------------------------------------------------- #
# 引用闭包
# --------------------------------------------------------------------------- #
def resolve_study_closure(result: JobResult, study_uid: str) -> StudyClosure:
    """返回某匿名检查在本任务成功输出上的引用闭包；无法闭合时抛 StudySubsetError。"""
    index = _build_sop_index(result)

    initial = [
        report
        for report in _successful_outputs(result)
        if report.study_instance_uid == study_uid
    ]
    if not initial:
        raise StudySubsetError(
            "STUDY_NOT_FOUND",
            404,
            "该匿名 StudyInstanceUID 在本任务的成功输出中不存在",
            study_uid=study_uid,
        )

    chosen: dict[str, FileReport] = {r.output_name: r for r in initial}
    queue: list[FileReport] = list(initial)
    queued = {r.output_name for r in initial}
    # uid -> (reason, output_files)；同一 UID 只报一次，按“缺失优先”保留首次判定
    problems: dict[str, tuple[str, list[str]]] = {}

    while queue:
        report = queue.pop()
        path = _output_path(result, report)
        try:
            ds = pydicom.dcmread(path)
        except Exception as exc:  # 自己写出的匿名文件不可读属于服务端异常
            raise StudySubsetError(
                "ANONYMIZED_OUTPUT_UNREADABLE",
                500,
                f"无法读取匿名输出文件 {report.output_name}：{exc}",
                study_uid=study_uid,
            ) from exc

        # 闭包成员自身的匿名 SOP UID 若对应多个输出文件，同样属于歧义：
        # 不挑选、不猜测，直接 409（全量下载行为不受影响）
        own_targets = index.get(report.sop_instance_uid, [])
        if len(own_targets) > 1:
            problems.setdefault(
                report.sop_instance_uid,
                (
                    REASON_AMBIGUOUS_OUTPUT,
                    sorted(t.output_name for t in own_targets),
                ),
            )

        for ref_uid in iter_referenced_sop_uids(ds):
            targets = index.get(ref_uid, [])
            if not targets:
                problems.setdefault(ref_uid, (REASON_NOT_IN_OUTPUT, []))
                continue
            if len(targets) > 1:
                problems.setdefault(
                    ref_uid,
                    (
                        REASON_AMBIGUOUS_OUTPUT,
                        sorted(t.output_name for t in targets),
                    ),
                )
            # 即使存在歧义也继续向下扫描，尽量一次性暴露全部闭包问题；
            # 有 problems 时最终必定 409，chosen 仅用于遍历，不会被输出。
            for target in targets:
                chosen.setdefault(target.output_name, target)
                if target.output_name not in queued:
                    queued.add(target.output_name)
                    queue.append(target)

    if problems:
        problem_items = [
            {
                "sop_instance_uid": uid,
                "reason": reason,
                "output_files": output_files,
                "message": (
                    "引用目标不在本任务的成功输出中"
                    if reason == REASON_NOT_IN_OUTPUT
                    else "一个匿名 SOPInstanceUID 对应多个输出文件，无法唯一补入"
                ),
            }
            for uid, (reason, output_files) in sorted(problems.items())
        ]
        raise StudySubsetError(
            "REFERENCE_CLOSURE_INCOMPLETE",
            409,
            "引用闭包无法在本任务的成功输出内闭合，已拒绝输出子集 ZIP",
            study_uid=study_uid,
            problems=problem_items,
        )

    return StudyClosure(
        initial_reports=initial,
        included_reports=[chosen[name] for name in sorted(chosen)],
    )


# --------------------------------------------------------------------------- #
# 扁平 ZIP（复用匿名输出文件，不再触碰原始 ZIP / 解压目录）
# --------------------------------------------------------------------------- #
def build_subset_zip(output_dir: str, output_names: list[str], dest_zip: str) -> str:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in output_names:  # 调用方已排序；arcname 扁平、即匿名文件名
            zf.write(os.path.join(output_dir, name), arcname=name)
    return dest_zip


def write_study_subset_zip(job, study_uid: str) -> tuple[str, int, int]:
    """构建子集 ZIP 到任务目录下的临时文件，返回 (路径, 初选数, 补入数)。"""
    result: JobResult = job.result
    closure = resolve_study_closure(result, study_uid)

    fd, tmp_path = tempfile.mkstemp(
        prefix=f"study-subset-{job.id[:8]}-",
        suffix=".zip",
        dir=os.path.dirname(result.output_dir),
    )
    os.close(fd)
    try:
        build_subset_zip(result.output_dir, closure.included_output_names, tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path, closure.initial_count, closure.added_count
