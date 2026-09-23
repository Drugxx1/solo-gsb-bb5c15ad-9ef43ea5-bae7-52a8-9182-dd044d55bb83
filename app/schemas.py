"""API 响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_serializer


class DiagnosticOut(BaseModel):
    code: str
    severity: str = Field(description="info | warning | error")
    message: str


class ChangeOut(BaseModel):
    path: str = ""
    tag: Optional[str] = None
    keyword: str
    action: str = Field(description="REPLACE | EMPTY | REMOVE")
    old: Optional[str] = None
    new: Optional[str] = None


class FileOut(BaseModel):
    name: str
    status: str = Field(description="ok | warning | error | skipped")
    output_name: Optional[str] = None
    patient_key: Optional[str] = None
    study_instance_uid: Optional[str] = None
    series_instance_uid: Optional[str] = None
    sop_instance_uid: Optional[str] = None
    max_severity: Optional[str] = None
    diagnostics: list[DiagnosticOut] = []
    changes: list[ChangeOut] = []
    changes_truncated: bool = False


class PatientOut(BaseModel):
    key: str
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int
    file_count: int


class StudyOut(BaseModel):
    old_study_instance_uid: str
    new_study_instance_uid: str
    patient_key: str
    file_count: int


class SeriesOut(BaseModel):
    old_series_instance_uid: str
    new_series_instance_uid: str
    new_study_instance_uid: str
    file_count: int


class JobOut(BaseModel):
    id: str
    status: str = Field(description="ready | failed")
    secret_fingerprint: str
    shift_dates: bool
    date_shift_min: int = Field(
        default=-365,
        ge=-365,
        le=365,
        description="本任务日期平移使用的闭区间下界（与 date_shift_max 成对）",
    )
    date_shift_max: int = Field(
        default=365,
        ge=-365,
        le=365,
        description="本任务日期平移使用的闭区间上界（与 date_shift_min 成对）",
    )
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    changes_capped: bool
    patients: list[PatientOut]
    studies: list[StudyOut]
    series: list[SeriesOut]
    files: list[FileOut]
    error: Optional[str] = None


class JobSummary(BaseModel):
    id: str
    status: str
    source_zip: str
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    created_at: float
    expires_at: float


# --------------------------------------------------------------------------- #
# 跨任务一致性核对（POST /jobs/consistency-check）
# --------------------------------------------------------------------------- #
class ConsistencyCheckRequest(BaseModel):
    job_ids: list[str] = Field(
        description="已完成（status=ready）任务 ID，2～5 个；不得重复",
        min_length=2,
        max_length=5,
    )
    level: Optional[Literal["patient", "study", "series", "sop"]] = Field(
        default=None,
        description=(
            "差异明细分页层级。不传：返回原完整计数报告（向后兼容）；"
            "传入时在顶层额外返回该层级按固定业务键排序的 items / next_cursor / has_more，"
            "报告内的完整 *_count 与 overall_status 仍然给出。"
        ),
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=200,
        description="每页差异明细条数，1～200；level 存在且未传时使用服务端默认值 50",
    )
    cursor: Optional[str] = Field(
        default=None,
        description=(
            "上一页响应返回的不透明 next_cursor；游标绑定本次任务集合与 level，"
            "篡改、过期或跨任务集合/层级复用将返回 400"
        ),
    )

    @field_validator("job_ids")
    @classmethod
    def _strip_and_reject_duplicates(cls, value: list[str]) -> list[str]:
        ids = [str(jid).strip() for jid in value]
        if any(not jid for jid in ids):
            raise ValueError("任务 ID 不能为空字符串")
        if len(set(ids)) != len(ids):
            raise ValueError("任务 ID 不得重复")
        return ids


class ConsistencyJobSummary(BaseModel):
    id: str
    status: str = Field(description="固定为 ready（未完成任务会直接返回 409）")
    secret_fingerprint: str
    shift_dates: bool
    date_shift_min: int = Field(default=-365, ge=-365, le=365)
    date_shift_max: int = Field(default=365, ge=-365, le=365)
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    patient_count: int
    study_count: int
    series_count: int
    sop_count: int


class PatientMappingValue(BaseModel):
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int


class PatientMappingDifference(BaseModel):
    patient_key: str
    difference_type: str = Field(
        description="conflict（同 key 在不同任务派生出不同伪名） | "
        "date_shift_mismatch（伪名一致但日期偏移不同，通常因 shift_dates 开关不同） | "
        "only_in_one_job（该患者仅出现在部分任务中，属于覆盖范围差异，不是冲突）"
    )
    values: dict[str, PatientMappingValue] = Field(
        description="任务 ID -> 该任务下的稳定伪名/日期偏移（仅包含出现该患者的任务）"
    )
    present_in: list[str] = Field(description="出现该患者的任务 ID")
    missing_in: list[str] = Field(default=[], description="未出现该患者的任务 ID")


class PatientMappingReport(BaseModel):
    checked_keys: int = Field(description="在 >=2 个任务中共同出现、实际比对过的患者 key 数")
    conflict_count: int
    date_shift_mismatch_count: int
    only_in_one_job_count: int
    differences: list[PatientMappingDifference]
    shift_dates_consistent: bool = Field(
        description="所有参与任务的 shift_dates 开关是否一致（开关不同会导致日期偏移策略不一致）"
    )


class UidMappingDifference(BaseModel):
    level: str = Field(description="study | series | sop")
    old_uid: Optional[str] = Field(
        default=None, description="原始 UID；uid_new_collision 时可能为空"
    )
    difference_type: str = Field(
        description="conflict（同一原始 UID 映射到不同新 UID） | "
        "only_in_one_job（覆盖范围差异） | "
        "uid_new_collision（同一新 UID 在不同任务中对应不同原始 UID）"
    )
    mappings: dict[str, str] = Field(
        default={}, description="任务 ID -> 新 UID（conflict / only_in_one_job 时给出）"
    )
    new_uid: Optional[str] = Field(default=None, description="uid_new_collision 时的新 UID")
    sources: dict[str, str] = Field(
        default={}, description="uid_new_collision 时：任务 ID -> 各自的原始 UID"
    )
    present_in: list[str] = Field(default=[])
    missing_in: list[str] = Field(default=[])


class UidLevelReport(BaseModel):
    level: str = Field(description="study | series | sop")
    checked_old_uids: int = Field(description="在 >=2 个任务中共同出现、实际比对过的原始 UID 数")
    conflict_count: int
    only_in_one_job_count: int
    new_uid_collision_count: int
    differences_truncated: bool = Field(
        description="差异条目过多时已截断（计数仍然完整）"
    )
    differences: list[UidMappingDifference]


class ConsistencyDifferenceItem(BaseModel):
    """统一的差异明细条目（患者层 / UID 层），按类型只填充对应字段。"""
    level: str = Field(description="patient | study | series | sop")
    difference_type: str = Field(
        description="患者层：conflict | date_shift_mismatch | only_in_one_job；"
        "UID 层：conflict | only_in_one_job | uid_new_collision"
    )
    patient_key: Optional[str] = Field(default=None, description="患者层条目的业务键")
    old_uid: Optional[str] = Field(
        default=None, description="UID 层原始 UID；uid_new_collision 时为空"
    )
    new_uid: Optional[str] = Field(default=None, description="uid_new_collision 时的新 UID")
    values: Optional[dict[str, PatientMappingValue]] = Field(
        default=None, description="患者层：任务 ID -> 该任务下的稳定伪名/日期偏移"
    )
    mappings: Optional[dict[str, str]] = Field(
        default=None, description="UID 层 conflict / only_in_one_job：任务 ID -> 新 UID"
    )
    sources: Optional[dict[str, str]] = Field(
        default=None, description="uid_new_collision：任务 ID -> 各自的原始 UID"
    )
    present_in: list[str] = []
    missing_in: list[str] = []


class ConsistencyCheckOut(BaseModel):
    overall_status: str = Field(
        description="consistent（无任何冲突） | inconsistent（存在伪名或 UID 冲突）"
    )
    secret_fingerprint: str = Field(description="参与任务共同的密钥指纹（SHA-256 前 12 位）")
    jobs: list[ConsistencyJobSummary]
    patient_mappings: PatientMappingReport
    uid_mappings: list[UidLevelReport] = Field(
        description="按 Study / Series / SOP 三个层级给出的映射差异"
    )
    summary: dict[str, int] = Field(
        description="patient_conflict / patient_date_shift_mismatch / "
        "uid_conflict / uid_new_collision / "
        "patient_only_in_one_job / uid_only_in_one_job 计数"
    )
    # 仅当请求传入 level 时出现（model_serializer 会在未分页时剔除这三个键）
    items: Optional[list[ConsistencyDifferenceItem]] = Field(
        default=None,
        description="请求 level 对应层级的差异明细，按固定业务键排序（见层级内排序规则）",
    )
    next_cursor: Optional[str] = Field(
        default=None,
        description="下一页不透明游标；has_more=false 时为 null。游标绑定任务集合与 level",
    )
    has_more: Optional[bool] = Field(
        default=None, description="该层级是否还有后续差异明细"
    )

    @model_serializer(mode="plain")
    def _serialize(self):
        data = {
            "overall_status": self.overall_status,
            "secret_fingerprint": self.secret_fingerprint,
            "jobs": [j.model_dump() for j in self.jobs],
            "patient_mappings": self.patient_mappings.model_dump(),
            "uid_mappings": [u.model_dump() for u in self.uid_mappings],
            "summary": self.summary,
        }
        # 未请求分页（level 缺省）时保持与旧版响应完全一致，不输出分页键
        if self.items is not None or self.next_cursor is not None or self.has_more is not None:
            data["items"] = (
                [i.model_dump(exclude_none=True) for i in self.items]
                if self.items is not None else None
            )
            data["next_cursor"] = self.next_cursor
            data["has_more"] = self.has_more
        return data


class ConsistencyErrorDetail(BaseModel):
    code: str = Field(
        description="JOB_NOT_FOUND | JOBS_NOT_COMPLETED | SECRET_FINGERPRINT_MISMATCH"
    )
    message: str
    missing_job_ids: list[str] = []
    not_ready_jobs: list[dict[str, str]] = []
    fingerprints: dict[str, str] = {}


class CursorErrorDetail(BaseModel):
    code: str = Field(
        description=(
            "INVALID_CURSOR（游标格式/签名非法、已被篡改，或服务重启后旧游标失效） | "
            "CURSOR_EXPIRED（游标超过有效期） | "
            "CURSOR_SCOPE_MISMATCH（游标被复用到其他任务集合或其他 level）"
        )
    )
    message: str


class ErrorOut(BaseModel):
    error: str
    detail: Optional[str] = None


# --------------------------------------------------------------------------- #
# 按匿名 StudyInstanceUID 下载检查子集（GET /jobs/{id}/studies/{uid}/download）
# --------------------------------------------------------------------------- #
class StudySubsetProblemItem(BaseModel):
    sop_instance_uid: str = Field(description="出问题的匿名（已重写）SOPInstanceUID")
    reason: str = Field(
        description="not_in_output（引用目标不在本任务成功输出中） | "
        "ambiguous_output（同一匿名 SOP UID 对应多个输出文件）"
    )
    output_files: list[str] = Field(
        default=[], description="ambiguous_output 时给出相互冲突的扁平输出文件名"
    )
    message: str


class StudySubsetErrorDetail(BaseModel):
    code: str = Field(
        description="STUDY_NOT_FOUND（404） | REFERENCE_CLOSURE_INCOMPLETE（409） | "
        "JOB_NOT_READY（409） | ANONYMIZED_OUTPUT_UNREADABLE（500）"
    )
    message: str
    study_instance_uid: Optional[str] = None
    problems: list[StudySubsetProblemItem] = []
