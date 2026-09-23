"""DICOM 批量匿名化核心引擎。

职责：
1. 安全解压 ZIP（路径穿越 / zip 炸弹 / 符号链接防护）；
2. 扫描全部文件，识别非法 DICOM，按患者身份分组并建立 UID 全局映射；
3. 重写 Patient / Study / Series / SOP / FrameOfReference UID 及身份字段，
   递归处理所有序列内的 UID 引用，保证检查关系不变；
4. 逐文件产出“标签变更预览”和诊断信息（非法文件、引用缺失、UID 缺失等）；
5. 将结果以扁平文件名重新打包为匿名 ZIP（避免原始路径泄露 PHI）。
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.errors import InvalidDicomError
from pydicom.sequence import Sequence
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian

from . import crypto, tags

# 预览中单个文件 / 整体的变更条目上限，防止 JSON 过大
MAX_FILE_CHANGES = 400
MAX_TOTAL_CHANGES = 6000
_VALUE_PREVIEW_LEN = 80

_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(.*)$", re.DOTALL)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Diagnostic:
    code: str
    severity: str  # info | warning | error
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass
class Change:
    tag: Optional[str]
    keyword: str
    action: str  # REPLACE | EMPTY | REMOVE
    old: Optional[str]
    new: Optional[str]
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "tag": self.tag,
            "keyword": self.keyword,
            "action": self.action,
            "old": self.old,
            "new": self.new,
        }


@dataclass
class FileReport:
    name: str
    status: str  # ok | warning | error | skipped
    output_name: Optional[str] = None
    sop_instance_uid: Optional[str] = None
    study_instance_uid: Optional[str] = None
    series_instance_uid: Optional[str] = None
    patient_key: Optional[str] = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    changes_truncated: bool = False

    def to_dict(self, include_changes: bool = True) -> dict:
        severity_order = {"error": 3, "warning": 2, "info": 1}
        worst = max(
            (severity_order.get(d.severity, 0) for d in self.diagnostics), default=0
        )
        return {
            "name": self.name,
            "status": self.status,
            "output_name": self.output_name,
            "patient_key": self.patient_key,
            "study_instance_uid": self.study_instance_uid,
            "series_instance_uid": self.series_instance_uid,
            "sop_instance_uid": self.sop_instance_uid,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "max_severity": {3: "error", 2: "warning", 1: "info"}.get(worst),
            "changes": ([c.to_dict() for c in self.changes] if include_changes else []),
            "changes_truncated": self.changes_truncated,
        }


@dataclass
class PatientInfo:
    key: str
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int
    file_count: int = 0


@dataclass
class JobResult:
    files: list[FileReport]
    patients: dict[str, PatientInfo]
    studies: dict[str, dict]
    series: dict[str, dict]
    sops: dict[str, str]
    output_dir: str
    total_changes: bool = False  # 是否触达整体变更上限


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _preview(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\n", " ").replace("\r", " ")
    if len(text) > _VALUE_PREVIEW_LEN:
        return text[:_VALUE_PREVIEW_LEN] + "…"
    return text


def _shift_date_text(value: str, delta: timedelta) -> Optional[str]:
    if not isinstance(value, str) or len(value) < 8 or not value[:8].isdigit():
        return None
    try:
        d = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None
    return (d + delta).strftime("%Y%m%d") + value[8:]


def _shift_dt_text(value: str, delta: timedelta) -> Optional[str]:
    if not isinstance(value, str):
        return None
    m = _DT_RE.match(value.strip())
    if not m:
        return None
    y, mo, da, tail = m.groups()
    try:
        d = date(int(y), int(mo), int(da))
    except ValueError:
        return None
    return (d + delta).strftime("%Y%m%d") + tail


def _file_sha16(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# ZIP 安全解压
# --------------------------------------------------------------------------- #
def safe_extract_zip(
    zip_path: str,
    dest: str,
    max_total_bytes: int,
    max_files: int,
) -> list[str]:
    """解压 ZIP，返回解压出的文件相对路径列表；带完整的安全校验。"""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"无法打开 ZIP：{exc}") from exc

    members: list[zipfile.ZipInfo] = []
    total = 0
    with zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise ValueError(f"压缩包内文件数量超过上限 {max_files}")
        dest_abs = os.path.abspath(dest)
        for info in infos:
            # 规范化后必须仍在目标目录内，防止 ../ 路径穿越与绝对路径
            target = os.path.abspath(os.path.join(dest, info.filename))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                raise ValueError(f"压缩包包含非法路径：{info.filename}")
            # 统一为 / 的相对路径，去掉前导 ./
            rel = os.path.relpath(target, dest_abs).replace(os.sep, "/")
            if rel.startswith("../") or rel == "..":
                raise ValueError(f"压缩包包含非法路径：{info.filename}")
            # Unix 权限中的文件类型：只拒绝明确的非常规类型（符号链接/设备/FIFO/socket）；
            # 类型位为 0（未知，常见于 Windows 打包的 ZIP）视为普通文件
            mode = info.external_attr >> 16
            if mode:
                ftype = mode & 0o170000
                if ftype not in (0, 0o100000, 0o040000):
                    raise ValueError(
                        f"压缩包包含非常规文件（如符号链接/设备）：{info.filename}"
                    )
            if info.is_dir():
                continue
            total += info.file_size
            if total > max_total_bytes:
                raise ValueError("解压后总大小超过上限（可能为 zip 炸弹）")
            members.append(info)

        for info in members:
            zf.extract(info, dest)
            target = os.path.abspath(os.path.join(dest, info.filename))
            # 二次确认：解压后若磁盘上是符号链接，拒绝
            if os.path.islink(target):
                raise ValueError(f"检测到符号链接：{info.filename}")
            os.chmod(target, 0o600)

    rels = [
        os.path.relpath(
            os.path.abspath(os.path.join(dest, m.filename)), os.path.abspath(dest)
        ).replace(os.sep, "/")
        for m in members
    ]
    return sorted(rels)


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class Anonymizer:
    def __init__(
        self,
        secret: str,
        shift_dates: bool = True,
        date_shift_min: int = -365,
        date_shift_max: int = 365,
    ):
        self.secret = secret
        self.shift_dates = shift_dates
        self.date_shift_min = date_shift_min
        self.date_shift_max = date_shift_max
        self._uid_map: dict[str, str] = {}
        self._sop_originals: set[str] = set()
        self._reported_missing: set[tuple[str, str]] = set()
        self._missing_seed = ""
        self.total_change_count = 0
        self.changes_capped = False

    # ----- UID 映射 -------------------------------------------------------- #
    def mapped_uid(self, original: str, report: Optional[FileReport] = None,
                   warn_missing: bool = False, location: str = "") -> str:
        original = (original or "").strip()
        if not original:
            raise ValueError("empty UID")
        new = self._uid_map.get(original)
        if new is None:
            new = crypto.derive_uid(self.secret, original)
            self._uid_map[original] = new
        if warn_missing and original not in self._sop_originals:
            key = (original, location)
            if key not in self._reported_missing:
                self._reported_missing.add(key)
                if report is not None:
                    report.diagnostics.append(
                        Diagnostic(
                            "DANGLING_REFERENCE",
                            "warning",
                            f"{location or '序列引用'} 指向的 SOP 实例不在压缩包内："
                            f"{original[:48]}{'…' if len(original) > 48 else ''}（仍会被一致重写）",
                        )
                    )
        return new

    # ----- 患者身份 -------------------------------------------------------- #
    @staticmethod
    def _patient_identity(ds: Dataset, file_path: str) -> tuple[str, Optional[str]]:
        """返回 (identity, 诊断消息)。身份相同的文件归为同一患者。"""
        pid = str(getattr(ds, "PatientID", "") or "").strip()
        if pid:
            return f"id:{pid}", None
        name = str(getattr(ds, "PatientName", "") or "").strip()
        if name:
            return f"name:{name.upper()}", None
        study_uid = str(getattr(ds, "StudyInstanceUID", "") or "").strip()
        if study_uid:
            return f"study:{study_uid}", "缺少 PatientID/PatientName，按 StudyInstanceUID 归组"
        digest = _file_sha16(file_path)
        return f"file:{digest}", "缺少任何患者身份字段，按文件内容单独归组"

    # ----- 扫描 ------------------------------------------------------------ #
    def _read_dataset(self, path: str) -> Dataset:
        try:
            return pydicom.dcmread(path)
        except InvalidDicomError:
            ds = pydicom.dcmread(path, force=True)
            if not (getattr(ds, "SOPClassUID", None) or getattr(ds, "SOPInstanceUID", None)):
                raise
            return ds

    def _collect_uids(self, ds: Dataset) -> None:
        datasets: list[tuple[str, Dataset]] = [("", ds)]
        if ds.file_meta is not None:
            datasets.append(("file_meta", ds.file_meta))
        while datasets:
            path, current = datasets.pop()
            for tag_ in list(current.keys()):
                elem = current[tag_]  # 触发 RawDataElement -> DataElement 解析
                if elem.VR == "SQ" and isinstance(elem.value, Sequence):
                    for i, item in enumerate(elem.value):
                        datasets.append((f"{path}.{elem.keyword}[{i}]", item))
                elif elem.VR == "UI":
                    keyword = elem.keyword
                    value = str(elem.value or "").strip()
                    if not value:
                        continue
                    if keyword in tags.UID_KEYWORDS:
                        self._uid_map.setdefault(value, crypto.derive_uid(self.secret, value))
        # 顶层 SOP 集合（用于 dangling reference 判断）
        sop = str(getattr(ds, "SOPInstanceUID", "") or "").strip()
        if sop:
            self._sop_originals.add(sop)

    # ----- 变更记录 -------------------------------------------------------- #
    def _record(self, report: FileReport, change: Change) -> None:
        if len(report.changes) >= MAX_FILE_CHANGES:
            report.changes_truncated = True
            return
        if self.total_change_count >= MAX_TOTAL_CHANGES:
            self.changes_capped = True
            report.changes_truncated = True
            return
        report.changes.append(change)
        self.total_change_count += 1

    # ----- 单文件匿名化 ----------------------------------------------------- #
    def _ensure_top_level_uids(self, ds: Dataset, report: FileReport) -> None:
        """补齐顶层必需 UID，缺失时生成确定性 UID 并给出诊断。"""
        for keyword, tag_, code in (
            ("StudyInstanceUID", Tag(0x0020000D), "MISSING_STUDY_UID"),
            ("SeriesInstanceUID", Tag(0x0020000E), "MISSING_SERIES_UID"),
            ("SOPInstanceUID", Tag(0x00080018), "MISSING_SOP_UID"),
        ):
            if str(ds.get(tag_, "") or "").strip():
                continue
            placeholder = f"missing:{keyword.lower()}:{self._missing_seed}"
            new_uid = crypto.derive_uid(self.secret, placeholder)
            ds.add_new(tag_, "UI", new_uid)
            # 生成的新 UID 映射到自身，保持后续映射表一致
            self._uid_map[placeholder] = new_uid
            self._uid_map[new_uid] = new_uid
            report.diagnostics.append(
                Diagnostic(
                    code,
                    "warning",
                    f"缺少 {keyword}，已生成确定性替代 UID：{new_uid}",
                )
            )

    def _anonymize_dataset(
        self,
        current: Dataset,
        path: str,
        report: FileReport,
        patient: PatientInfo,
        delta: timedelta,
        is_file_meta: bool,
        new_sop_uid: str,
    ) -> None:
        # 1) 清除私有元素（每个 dataset 单独聚合成一条变更）
        private_before = sum(1 for el in current.elements() if el.tag.is_private)
        if private_before:
            current.remove_private_tags()
            self._record(
                report,
                Change(None, "<private tags>", "REMOVE",
                       f"{private_before} 个私有元素", None, path or "/"),
            )

        for tag_ in list(current.keys()):
            elem = current[tag_]  # 触发 RawDataElement -> DataElement 解析
            tag_ = elem.tag
            keyword = elem.keyword or ""
            loc = f"{path}.{keyword}" if path else keyword

            # 2) 嵌套序列：整段删除或递归处理
            if elem.VR == "SQ" and isinstance(elem.value, Sequence):
                if keyword in tags.REMOVE_KEYWORDS:
                    self._record(report, Change(str(tag_), keyword, "REMOVE",
                                                f"{len(elem.value)} 个数据项", None,
                                                path or "/"))
                    del current[tag_]
                    continue
                for i, item in enumerate(elem.value):
                    self._anonymize_dataset(
                        item, f"{loc}[{i}]", report, patient, delta, False, new_sop_uid
                    )
                continue

            if is_file_meta:
                # 3) 文件元信息特殊处理
                if keyword == "SourceApplicationEntityTitle":
                    self._record(report, Change(str(tag_), keyword, "EMPTY",
                                                _preview(elem.value), None, path))
                    elem.value = ""
                elif keyword == "MediaStorageSOPInstanceUID":
                    self._record(report, Change(str(tag_), keyword, "REPLACE",
                                                _preview(elem.value), new_sop_uid, path))
                    elem.value = new_sop_uid
                # MediaStorageSOPClassUID / Implementation* / TransferSyntaxUID 保留
                continue

            # 4) UID：全局一致性重写（顶层与序列引用同一张映射表）
            if elem.VR == "UI":
                value = elem.value
                if value is None:
                    continue
                # 兼容 VM>1 的 UI（极少数标签），逐分量映射
                if isinstance(value, (list, tuple)) or "\\" in str(value):
                    parts = list(value) if isinstance(value, (list, tuple)) else str(value).split("\\")
                    new_parts = []
                    changed = False
                    for part in parts:
                        part = str(part).strip()
                        if part and keyword in tags.UID_KEYWORDS:
                            new_part = self.mapped_uid(
                                part, report,
                                warn_missing=(keyword == "ReferencedSOPInstanceUID"),
                                location=loc,
                            )
                            new_parts.append(new_part)
                            changed |= new_part != part
                        else:
                            new_parts.append(part)
                    if changed:
                        self._record(report, Change(str(tag_), keyword, "REPLACE",
                                                    _preview(value), "\\".join(new_parts),
                                                    path or "/"))
                        elem.value = "\\".join(new_parts)
                    continue
                value = str(value).strip()
                if value and keyword in tags.UID_KEYWORDS:
                    warn = keyword == "ReferencedSOPInstanceUID"
                    new_uid = self.mapped_uid(
                        value, report, warn_missing=warn, location=loc
                    )
                    if new_uid != value:
                        self._record(report, Change(str(tag_), keyword, "REPLACE",
                                                    _preview(value), new_uid, path or "/"))
                    elem.value = new_uid
                continue

            # 5) 患者主标识：替换为稳定伪名（值已一致时跳过，避免重复记录）
            if tag_ == tags.PATIENT_ID:
                if str(elem.value or "") != patient.new_patient_id:
                    self._record(report, Change(str(tag_), keyword, "REPLACE",
                                                _preview(elem.value), patient.new_patient_id,
                                                path or "/"))
                elem.value = patient.new_patient_id
                continue
            if tag_ == tags.PATIENT_NAME:
                if str(elem.value or "") != patient.new_patient_name:
                    self._record(report, Change(str(tag_), keyword, "REPLACE",
                                                _preview(elem.value), patient.new_patient_name,
                                                path or "/"))
                elem.value = patient.new_patient_name
                continue

            # 6) 出生日期：平移或清空（其余 DA/DT 仅在开启日期平移时处理）
            if tag_ == tags.PATIENT_BIRTH_DATE:
                if self.shift_dates and elem.value:
                    shifted = _shift_date_text(str(elem.value), delta)
                    new_val = shifted if shifted is not None else ""
                    if shifted is None:
                        report.diagnostics.append(
                            Diagnostic("INVALID_DATE", "warning",
                                       f"{loc} 日期无法解析，已清空：{_preview(elem.value)}"))
                    self._record(report, Change(str(tag_), keyword, "REPLACE",
                                                _preview(elem.value), new_val, path or "/"))
                    elem.value = new_val
                else:
                    self._record(report, Change(str(tag_), keyword, "EMPTY",
                                                _preview(elem.value), None, path or "/"))
                    elem.value = ""
                continue

            # 7) 其余 PN：全部清空（如 ReferencedPatientSequence 以外的人名）
            if elem.VR == "PN" and elem.value not in (None, ""):
                self._record(report, Change(str(tag_), keyword, "EMPTY",
                                            _preview(elem.value), None, path or "/"))
                elem.value = ""
                continue

            # 8) 日期平移（DA/DT），保留时间部分；非法日期告警并清空
            if self.shift_dates and elem.VR in ("DA", "DT") and elem.value not in (None, ""):
                raw = str(elem.value)
                shifted = (
                    _shift_dt_text(raw, delta)
                    if elem.VR == "DT"
                    else _shift_date_text(raw, delta)
                )
                if shifted is None:
                    report.diagnostics.append(
                        Diagnostic("INVALID_DATE", "warning",
                                   f"{loc} 日期无法解析，已清空：{_preview(elem.value)}"))
                    self._record(report, Change(str(tag_), keyword, "EMPTY",
                                                _preview(elem.value), None, path or "/"))
                    elem.value = ""
                elif shifted != raw:
                    self._record(report, Change(str(tag_), keyword, "REPLACE",
                                                _preview(elem.value), shifted, path or "/"))
                    elem.value = shifted
                continue

            # 9) 明确要求删除 / 清空的标签
            if keyword in tags.REMOVE_KEYWORDS:
                self._record(report, Change(str(tag_), keyword, "REMOVE",
                                            _preview(elem.value), None, path or "/"))
                del current[tag_]
                continue
            if keyword in tags.EMPTY_KEYWORDS and elem.value not in (None, ""):
                self._record(report, Change(str(tag_), keyword, "EMPTY",
                                            _preview(elem.value), None, path or "/"))
                elem.value = ""
                continue

    def _normalize_file_meta(self, ds: Dataset, new_sop_uid: str) -> None:
        """保证写出文件具备合规的 File Meta Information 与传输语法。"""
        meta = ds.file_meta
        if meta is None:
            meta = FileMetaDataset()
            ds.file_meta = meta
        sop_class = getattr(ds, "SOPClassUID", None)
        if not getattr(meta, "MediaStorageSOPClassUID", None) and sop_class:
            meta.MediaStorageSOPClassUID = str(sop_class)
        meta.MediaStorageSOPInstanceUID = new_sop_uid
        if not getattr(meta, "TransferSyntaxUID", None):
            meta.TransferSyntaxUID = ExplicitVRLittleEndian

    # ----- 公共入口 -------------------------------------------------------- #
    def run(
        self,
        extracted_dir: str,
        relative_paths: list[str],
        output_dir: str,
    ) -> JobResult:
        os.makedirs(output_dir, exist_ok=True)
        self._reported_missing = set()
        self._missing_seed = ""

        reports: list[FileReport] = []
        valid: list[str] = []
        report_by_path: dict[str, FileReport] = {}
        identities: dict[str, list[FileReport]] = {}

        # ---- 第一遍：识别 DICOM、收集 UID、患者分组 -------------------- #
        for rel in relative_paths:
            path = os.path.join(extracted_dir, rel)
            report = FileReport(name=rel, status="ok")
            reports.append(report)
            if os.path.basename(rel).upper() == "DICOMDIR":
                report.status = "skipped"
                report.diagnostics.append(
                    Diagnostic("DICOMDIR_SKIPPED", "info",
                               "DICOMDIR 索引文件不会写入匿名包（路径会暴露 PHI）"))
                continue
            try:
                ds = self._read_dataset(path)
            except InvalidDicomError as exc:
                report.status = "error"
                report.diagnostics.append(
                    Diagnostic("INVALID_DICOM", "error", f"不是合法的 DICOM 文件：{exc}"))
                continue
            except Exception as exc:  # 截断 / 编码错误等
                report.status = "error"
                report.diagnostics.append(
                    Diagnostic("READ_ERROR", "error", f"读取 DICOM 失败：{exc}"))
                continue

            identity, msg = self._patient_identity(ds, path)
            report.patient_key = identity
            if msg:
                report.diagnostics.append(Diagnostic("PATIENT_IDENTITY_FALLBACK", "warning", msg))
            identities.setdefault(identity, []).append(report)
            try:
                self._collect_uids(ds)
            except Exception as exc:
                report.status = "error"
                report.diagnostics.append(
                    Diagnostic("READ_ERROR", "error", f"解析 DICOM 标签失败：{exc}"))
                continue
            valid.append(path)
            report_by_path[path] = report

        # ---- 建立患者映射 --------------------------------------------- #
        patients: dict[str, PatientInfo] = {}
        for identity in sorted(identities):
            token, pname = crypto.patient_pseudonyms(self.secret, identity)
            patients[identity] = PatientInfo(
                key=identity,
                new_patient_id=token,
                new_patient_name=pname,
                date_shift_days=(
                    crypto.date_shift_days(
                        self.secret,
                        identity,
                        self.date_shift_min,
                        self.date_shift_max,
                    )
                    if self.shift_dates else 0
                ),
                file_count=len(identities[identity]),
            )

        # ---- 第二遍：重写 + 写出 --------------------------------------- #
        studies: dict[str, dict] = {}
        series: dict[str, dict] = {}
        sops: dict[str, str] = {}

        for path in valid:
            report = report_by_path[path]
            try:
                ds = self._read_dataset(path)
                self._missing_seed = _file_sha16(path)
                self._ensure_top_level_uids(ds, report)

                orig_sop = str(ds.SOPInstanceUID).strip()
                orig_study = str(getattr(ds, "StudyInstanceUID", "") or "").strip()
                orig_series = str(getattr(ds, "SeriesInstanceUID", "") or "").strip()

                patient = patients[report.patient_key]
                delta = timedelta(days=patient.date_shift_days)

                # 输入可能完全没有患者标识元素：先补齐为伪名，保证输出一定匿名
                old_pid = getattr(ds.get(tags.PATIENT_ID, None), "value", None)
                old_pname = getattr(ds.get(tags.PATIENT_NAME, None), "value", None)
                ds.add_new(tags.PATIENT_ID, "LO", patient.new_patient_id)
                ds.add_new(tags.PATIENT_NAME, "PN", patient.new_patient_name)
                self._record(report, Change(str(tags.PATIENT_ID), "PatientID",
                                            "REPLACE", _preview(old_pid),
                                            patient.new_patient_id, "/"))
                self._record(report, Change(str(tags.PATIENT_NAME), "PatientName",
                                            "REPLACE", _preview(old_pname),
                                            patient.new_patient_name, "/"))

                new_sop = self.mapped_uid(orig_sop)
                if orig_study:
                    new_study = self.mapped_uid(orig_study)
                else:
                    new_study = str(ds.StudyInstanceUID)
                if orig_series:
                    new_series = self.mapped_uid(orig_series)
                else:
                    new_series = str(ds.SeriesInstanceUID)

                # 文件元信息（先处理；与主数据集走同一套映射）
                if ds.file_meta is not None:
                    self._anonymize_dataset(
                        ds.file_meta, "file_meta", report, patient, delta, True, new_sop
                    )
                self._anonymize_dataset(ds, "", report, patient, delta, False, new_sop)

                # 审计标签（Annex E 要求）
                old_removed_el = ds.get(tags.PATIENT_IDENTITY_REMOVED, None)
                old_method_el = ds.get(tags.DEIDENTIFICATION_METHOD, None)
                old_removed = getattr(old_removed_el, "value", None)
                old_method = getattr(old_method_el, "value", None)
                method_value = (
                    "DICOM-ANON-API; PS3.15 BASIC APPLICATION CONFIDENTIALITY PROFILE"
                )
                ds.add_new(tags.PATIENT_IDENTITY_REMOVED, "CS", "YES")
                ds.add_new(tags.DEIDENTIFICATION_METHOD, "LO", method_value)
                self._record(
                    report,
                    Change(str(tags.PATIENT_IDENTITY_REMOVED),
                           "PatientIdentityRemoved", "REPLACE",
                           _preview(old_removed), "YES", "/"),
                )
                self._record(
                    report,
                    Change(str(tags.DEIDENTIFICATION_METHOD),
                           "DeidentificationMethod", "REPLACE",
                           _preview(old_method), method_value, "/"),
                )

                self._normalize_file_meta(ds, new_sop)

                buf = io.BytesIO()
                ds.save_as(buf, enforce_file_format=True)
                output_name = f"{new_sop}.dcm"
                out_path = os.path.join(output_dir, output_name)
                # 不同文件共享同一 SOPInstanceUID（重复实例）时避免互相覆盖
                if os.path.exists(out_path):
                    dup_idx = 2
                    while os.path.exists(
                        os.path.join(output_dir, f"{new_sop}-{dup_idx}.dcm")
                    ):
                        dup_idx += 1
                    output_name = f"{new_sop}-{dup_idx}.dcm"
                    out_path = os.path.join(output_dir, output_name)
                    report.diagnostics.append(
                        Diagnostic(
                            "DUPLICATE_SOP_INSTANCE",
                            "warning",
                            f"与压缩包内另一文件共享同一 SOPInstanceUID，"
                            f"输出已重命名为 {output_name}",
                        )
                    )
                with open(out_path, "wb") as fh:
                    fh.write(buf.getvalue())

                report.output_name = output_name
                report.sop_instance_uid = new_sop
                report.study_instance_uid = new_study
                report.series_instance_uid = new_series
                sops[orig_sop] = new_sop
                studies.setdefault(
                    new_study,
                    {"new_study_instance_uid": new_study,
                     "old_study_instance_uid": orig_study,
                     "patient_key": report.patient_key, "file_count": 0},
                )["file_count"] += 1
                series.setdefault(
                    new_series,
                    {"new_series_instance_uid": new_series,
                     "old_series_instance_uid": orig_series,
                     "new_study_instance_uid": new_study, "file_count": 0},
                )["file_count"] += 1
            except Exception as exc:
                report.status = "error"
                report.diagnostics.append(
                    Diagnostic("WRITE_ERROR", "error", f"匿名化写出失败：{exc}"))

        # ---- 状态汇总 -------------------------------------------------- #
        for report in reports:
            if report.status == "skipped":
                continue
            if any(d.severity == "error" for d in report.diagnostics):
                report.status = "error"
            elif any(d.severity == "warning" for d in report.diagnostics):
                report.status = "warning"
            else:
                report.status = "ok"

        return JobResult(
            files=reports,
            patients=patients,
            studies=studies,
            series=series,
            sops=sops,
            output_dir=output_dir,
            total_changes=self.changes_capped,
        )


def build_zip(result: JobResult, dest_zip: str) -> str:
    """将匿名化后的文件以扁平结构打包（不保留原始路径/文件名）。"""
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for report in result.files:
            if report.status == "error" or not report.output_name:
                continue
            src = os.path.join(result.output_dir, report.output_name)
            if os.path.exists(src):
                zf.write(src, arcname=report.output_name)
    return dest_zip
