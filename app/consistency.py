"""跨任务一致性核对。

多个独立批次（任务）若使用同一把稳定密钥，则确定性派生保证：
* 同一患者身份（patient key）应得到相同伪名与日期偏移；
* 同一原始 Study/Series/SOP UID 应映射到同一新 UID；
* 不同原始 UID 不应碰撞到同一新 UID。

本模块对 2～5 个**已完成**任务做纯内存比对，输出差异报告：
* ``conflict`` —— 同一 key/原始 UID 在不同任务派生出不同结果（正常情况下不会发生，
  出现意味着版本/密钥派生逻辑不一致）；
* ``only_in_one_job`` —— 该标识仅出现在部分任务中，属于批次覆盖范围差异，不是冲突；
* ``uid_new_collision`` —— 不同原始 UID 在不同任务中派生出相同新 UID。
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from typing import Any, Optional

from .anonymizer import JobResult
from .jobs import Job
from . import pagination

# 每个 UID 层级差异明细条目上限（计数始终完整，仅截断明细，防止 JSON 过大）
_MAX_DIFF_ENTRIES_PER_LEVEL = 500

# 差异明细分页的默认/最大页长（请求未传 limit 时使用默认值）
DEFAULT_DIFF_PAGE_LIMIT = 50
MAX_DIFF_PAGE_LIMIT = 200

# 固定业务排序键中的类型段（决定同一层级内冲突/碰撞/覆盖差异的先后顺序）
_RANK_TYPE: dict[str, int] = {
    "conflict": 0,
    "date_shift_mismatch": 0,
    "uid_new_collision": 1,
    "only_in_one_job": 2,
}


def _job_counts(job: Job) -> dict[str, int]:
    result: JobResult = job.result
    return {
        "file_count": len(result.files),
        "output_file_count": sum(bool(r.output_name) for r in result.files),
        "error_count": sum(r.status == "error" for r in result.files),
        "warning_count": sum(r.status == "warning" for r in result.files),
        "patient_count": len(result.patients),
        "study_count": len(result.studies),
        "series_count": len(result.series),
        "sop_count": len(result.sops),
    }


def build_job_summary(job: Job) -> dict:
    counts = _job_counts(job)
    return {
        "id": job.id,
        "status": "ready",
        "secret_fingerprint": job.secret_fingerprint,
        "shift_dates": job.shift_dates,
        "date_shift_min": getattr(job, "date_shift_min", -365),
        "date_shift_max": getattr(job, "date_shift_max", 365),
        "source_zip": job.source_name,
        "created_at": job.created_at,
        "expires_at": job.expires_at,
        **counts,
    }


# --------------------------------------------------------------------------- #
# 患者映射
# --------------------------------------------------------------------------- #
def _compare_patients(jobs: list[Job]) -> dict:
    # patient_key -> job_id -> 伪名/偏移
    per_key: dict[str, dict[str, dict]] = defaultdict(dict)
    for job in jobs:
        for key, p in job.result.patients.items():
            per_key[key][job.id] = {
                "new_patient_id": p.new_patient_id,
                "new_patient_name": p.new_patient_name,
                "date_shift_days": p.date_shift_days,
            }

    job_ids = [j.id for j in jobs]
    differences = []
    checked_keys = 0
    conflict_count = 0
    shift_mismatch_count = 0
    only_count = 0

    for key in sorted(per_key):
        appearances = per_key[key]
        present = [jid for jid in job_ids if jid in appearances]
        missing = [jid for jid in job_ids if jid not in appearances]
        values = {jid: appearances[jid] for jid in present}

        if len(appearances) >= 2:
            checked_keys += 1
            ids = {v["new_patient_id"] for v in appearances.values()}
            names = {v["new_patient_name"] for v in appearances.values()}
            shifts = {v["date_shift_days"] for v in appearances.values()}
            if len(ids) > 1 or len(names) > 1:
                # 同一患者派生出不同伪名：确定性派生被破坏（真正的冲突）
                conflict_count += 1
                differences.append({
                    "level": "patient",
                    "patient_key": key,
                    "difference_type": "conflict",
                    "values": values,
                    "present_in": present,
                    "missing_in": [],
                })
            elif len(shifts) > 1:
                # 伪名一致但日期偏移不同：通常因各任务 shift_dates 开关不同
                shift_mismatch_count += 1
                differences.append({
                    "level": "patient",
                    "patient_key": key,
                    "difference_type": "date_shift_mismatch",
                    "values": values,
                    "present_in": present,
                    "missing_in": [],
                })
        else:
            only_count += 1
            differences.append({
                "level": "patient",
                "patient_key": key,
                "difference_type": "only_in_one_job",
                "values": values,
                "present_in": present,
                "missing_in": missing,
            })

    shift_consistent = len({j.shift_dates for j in jobs}) == 1
    return {
        "checked_keys": checked_keys,
        "conflict_count": conflict_count,
        "date_shift_mismatch_count": shift_mismatch_count,
        "only_in_one_job_count": only_count,
        "differences": differences,
        "shift_dates_consistent": shift_consistent,
        # 内部使用：完整、按固定业务键排序的差异列表（患者层不做截断）
        "_all_differences": differences,
    }


# --------------------------------------------------------------------------- #
# UID 映射
# --------------------------------------------------------------------------- #
def _uid_layer(jobs: list[Job], level: str) -> tuple[dict[str, dict[str, str]],
                                                      dict[str, dict[str, str]]]:
    """返回 (按原始 UID 索引, 按新 UID 索引) 的 {标识 -> {job_id: 对端 UID}}。"""
    by_old: dict[str, dict[str, str]] = defaultdict(dict)
    by_new: dict[str, dict[str, str]] = defaultdict(dict)
    for job in jobs:
        result: JobResult = job.result
        if level == "study":
            entries = (
                (v["old_study_instance_uid"], v["new_study_instance_uid"])
                for v in result.studies.values()
            )
        elif level == "series":
            entries = (
                (v["old_series_instance_uid"], v["new_series_instance_uid"])
                for v in result.series.values()
            )
        else:
            entries = result.sops.items()
        for old_uid, new_uid in entries:
            # 顶层 UID 缺失时会用确定性占位 UID 补齐，old 端为空/占位不具备跨批次可比性
            if not old_uid or old_uid.startswith("missing:"):
                continue
            by_old[old_uid][job.id] = new_uid
            by_new[new_uid][job.id] = old_uid
    return by_old, by_new


def _compare_uid_level(jobs: list[Job], level: str) -> dict:
    by_old, by_new = _uid_layer(jobs, level)
    job_ids = [j.id for j in jobs]
    conflicts: list[dict] = []
    collisions: list[dict] = []
    coverage: list[dict] = []
    checked = 0
    only_count = 0

    # 1) 同一原始 UID 的映射
    for old_uid in sorted(by_old):
        appearances = by_old[old_uid]
        present = [jid for jid in job_ids if jid in appearances]
        missing = [jid for jid in job_ids if jid not in appearances]

        if len(appearances) >= 2:
            checked += 1
            if len(set(appearances.values())) > 1:
                conflicts.append({
                    "level": level,
                    "old_uid": old_uid,
                    "difference_type": "conflict",
                    "mappings": {jid: appearances[jid] for jid in present},
                    "present_in": present,
                    "missing_in": [],
                })
        else:
            only_count += 1
            coverage.append({
                "level": level,
                "old_uid": old_uid,
                "difference_type": "only_in_one_job",
                "mappings": {jid: appearances[jid] for jid in present},
                "present_in": present,
                "missing_in": missing,
            })

    # 2) 新 UID 碰撞：同一新 UID 对应 >=2 个不同原始 UID
    collision_count = 0
    for new_uid in sorted(by_new):
        sources = by_new[new_uid]
        if len(set(sources.values())) > 1:
            collision_count += 1
            collisions.append({
                "level": level,
                "old_uid": None,
                "difference_type": "uid_new_collision",
                "new_uid": new_uid,
                "sources": dict(sources),
                "present_in": [jid for jid in job_ids if jid in sources],
                "missing_in": [jid for jid in job_ids if jid not in sources],
            })

    # 冲突/碰撞是关键差异，始终完整给出；覆盖范围差异可能成千上万，仅保留前若干条。
    # 完整列表先按固定业务键排序（见 _diff_sort_key），再在其上做截断，保证分页与
    # 截断使用同一个稳定顺序。
    all_differences = sorted(
        conflicts + collisions + coverage, key=lambda d: _diff_sort_key(level, d)
    )
    budget = _MAX_DIFF_ENTRIES_PER_LEVEL - len(conflicts) - len(collisions)
    kept_coverage = coverage[:max(budget, 0)]
    truncated = len(kept_coverage) < len(coverage)
    embedded = conflicts + collisions + kept_coverage
    return {
        "level": level,
        "checked_old_uids": checked,
        "conflict_count": len(conflicts),
        "only_in_one_job_count": only_count,
        "new_uid_collision_count": collision_count,
        "differences_truncated": truncated,
        "differences": embedded,
        # 内部使用：完整、按固定业务键排序的差异列表（供分页，不受截断影响）
        "_all_differences": all_differences,
    }


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def build_report(jobs: list[Job]) -> dict:
    """对已校验（存在、ready、同密钥指纹）的任务构建一致性报告。"""
    return report_without_private(build_full_report(jobs))


# --------------------------------------------------------------------------- #
# 差异明细分页
# --------------------------------------------------------------------------- #
def _diff_sort_key(level: str, diff: dict) -> tuple:
    """跨层级统一的固定业务排序键。

    先按差异类型（conflict/date_shift_mismatch -> uid_new_collision ->
    only_in_one_job），再按该条目的业务标识（患者 key / 原始 UID / 新 UID），
    保证页码顺序稳定、与比对执行顺序无关。
    """
    dtype = diff["difference_type"]
    type_rank = _RANK_TYPE.get(dtype, 9)
    if level == "patient":
        identity = diff.get("patient_key") or ""
    elif dtype == "uid_new_collision":
        identity = diff.get("new_uid") or ""
    else:
        identity = diff.get("old_uid") or ""
    return (type_rank, identity)


def _full_differences(report: dict, level: str) -> list[dict]:
    """从内部报告对象取出某层级的完整（未截断）差异列表。"""
    if level == "patient":
        return report["patient_mappings"]["_all_differences"]
    layer = next(l for l in report["uid_mappings"] if l["level"] == level)
    return layer["_all_differences"]


def build_full_report(jobs: list[Job]) -> dict:
    """同 build_report，但保留各层级 ``_all_differences`` 供分页使用。"""
    patients = _compare_patients(jobs)
    uid_layers = [_compare_uid_level(jobs, level) for level in ("study", "series", "sop")]

    uid_conflicts = sum(layer["conflict_count"] for layer in uid_layers)
    uid_collisions = sum(layer["new_uid_collision_count"] for layer in uid_layers)
    uid_only = sum(layer["only_in_one_job_count"] for layer in uid_layers)

    inconsistent = (
        patients["conflict_count"] > 0
        or patients["date_shift_mismatch_count"] > 0
        or uid_conflicts > 0
        or uid_collisions > 0
    )
    return {
        "overall_status": "inconsistent" if inconsistent else "consistent",
        "secret_fingerprint": jobs[0].secret_fingerprint,
        "jobs": [build_job_summary(j) for j in jobs],
        "patient_mappings": patients,
        "uid_mappings": uid_layers,
        "summary": {
            "patient_conflict": patients["conflict_count"],
            "patient_date_shift_mismatch": patients["date_shift_mismatch_count"],
            "uid_conflict": uid_conflicts,
            "uid_new_collision": uid_collisions,
            "patient_only_in_one_job": patients["only_in_one_job_count"],
            "uid_only_in_one_job": uid_only,
        },
    }


def report_without_private(report: dict) -> dict:
    """剥离内部的 ``_all_differences`` 键，得到可直接序列化的报告。"""
    patients_out = {k: v for k, v in report["patient_mappings"].items()
                    if not k.startswith("_")}
    uid_layers_out = [
        {k: v for k, v in layer.items() if not k.startswith("_")}
        for layer in report["uid_mappings"]
    ]
    return {**report, "patient_mappings": patients_out, "uid_mappings": uid_layers_out}


def page_differences(
    report: dict,
    *,
    job_ids: list[str],
    level: str,
    limit: int,
    after: Optional[list[Any]],
) -> dict:
    """在完整差异列表上按固定业务键做 keyset 分页。

    返回 {"items", "next_cursor", "has_more"}；游标绑定任务集合与 level。
    """
    differences = sorted(
        _full_differences(report, level),
        key=lambda d: _diff_sort_key(level, d),
    )
    sort_keys = [_diff_sort_key(level, d) for d in differences]
    if after is not None:
        # 游标来自同一作用域（已由 CursorCodec 校验）。底层任务结果若在翻页间
        # 发生变化导致该键消失，也能稳定定位到“大于游标键”的首条，不重不漏。
        start = bisect.bisect_right(sort_keys, tuple(after))
    else:
        start = 0

    window = differences[start:start + limit + 1]
    has_more = len(window) > limit
    page = window[:limit]
    next_cursor: Optional[str] = None
    if has_more and page:
        last_key = list(_diff_sort_key(level, page[-1]))
        next_cursor = pagination.codec.encode(
            job_ids=job_ids, level=level, after=last_key
        )
    return {"items": page, "next_cursor": next_cursor, "has_more": has_more}
