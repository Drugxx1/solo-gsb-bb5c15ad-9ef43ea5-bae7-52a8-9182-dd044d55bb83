"""匿名化标签策略。

依据 DICOM PS3.15 Annex E（Basic Application Confidentiality Profile）整理，
并在此项目中额外做 UID 一致性重写与（可选）日期平移。
"""
from __future__ import annotations

from pydicom.tag import Tag

# 患者身份主标签（特殊处理：替换为稳定伪名，序列内递归替换）
PATIENT_ID = Tag(0x00100020)
PATIENT_NAME = Tag(0x00100010)
PATIENT_BIRTH_DATE = Tag(0x00100030)

# 审计标签
PATIENT_IDENTITY_REMOVED = Tag(0x00120062)
DEIDENTIFICATION_METHOD = Tag(0x00120063)

# 文件元信息
SOURCE_APPLICATION_ENTITY_TITLE = Tag(0x00020016)
MEDIA_STORAGE_SOP_INSTANCE_UID = Tag(0x00020003)

# 需要置空的标签关键字（leaf 元素；序列请放 REMOVE_KEYWORDS）
EMPTY_KEYWORDS: frozenset[str] = frozenset(
    {
        # 0008 检查/机构/人员
        "AccessionNumber",
        "InstitutionName",
        "InstitutionAddress",
        "InstitutionalDepartmentName",
        "StationName",
        "StudyID",
        "StudyDescription",
        "SeriesDescription",
        "AdmittingDiagnosesDescription",
        "ReferringPhysicianName",
        "PhysiciansOfRecord",
        "NameOfPhysiciansReadingStudy",
        "PerformingPhysicianName",
        "OperatorsName",
        "NamesOfIntendedRecipientsOfResults",
        # 0018 设备
        "DeviceSerialNumber",
        "SoftwareVersions",
        "ProtocolName",
        # 0010 患者
        "IssuerOfPatientID",
        "PatientAge",
        "PatientBirthTime",
        "EthnicGroup",
        "PatientReligiousPreference",
        "Occupation",
        "CountryOfResidence",
        "RegionOfResidence",
        "MilitaryRank",
        "BranchOfService",
        # 0032 / 0038 / 0040 申请与位置
        "RequestingPhysician",
        "RequestingService",
        "RequestedProcedureDescription",
        "RequestedProcedureLocation",
        "ScheduledProcedureStepDescription",
        "ScheduledPerformingPhysicianName",
        "ScheduledStationAETitle",
        "ScheduledStationName",
        "PerformedStationAETitle",
        "PerformedStationName",
        "PerformedLocation",
        "CurrentPatientLocation",
        "PatientInstitutionResidence",
        "PersonAddress",
        "PersonTelephoneNumbers",
    }
)

# 需要整体删除的标签关键字（自由文本、身份序列等）
REMOVE_KEYWORDS: frozenset[str] = frozenset(
    {
        # 患者直接身份信息
        "OtherPatientIDs",
        "OtherPatientNames",
        "PatientBirthName",
        "PatientMotherBirthName",
        "PatientAddress",
        "PatientTelephoneNumbers",
        "MedicalRecordLocator",
        "PatientComments",
        "AdditionalPatientHistory",
        # 自由文本
        "ImageComments",
        "StudyComments",
        "ReferringPhysicianAddress",
        "ReferringPhysicianTelephoneNumbers",
        # 身份/机构序列
        "ReferencedPatientSequence",
        "ReferencedPatientPhotoSequence",
        "OtherPatientIDsSequence",
        "IssuerOfPatientIDQualifiersSequence",
        "ReferringPhysicianIdentificationSequence",
        "PerformingPhysicianIdentificationSequence",
        "PhysiciansOfRecordIdentificationSequence",
        "PhysiciansReadingStudyIdentificationSequence",
        "RequestingPhysicianIdentificationSequence",
        "OperatorIdentificationSequence",
        "InstitutionCodeSequence",
        "PatientInsurancePlanCodeSequence",
        "PersonIdentificationCodeSequence",
        # 某些设备会保存“原始属性”备份，必须清除
        "OriginalAttributesSequence",
    }
)

# 所有需要参与一致性重写的 UID 标签关键字。
# 顶层实例/检查/系列/坐标系 UID 与序列内引用使用同一张全局映射表，
# 因此无论出现在顶层还是嵌套序列都会被一致替换；
# SOP Class UID 不在此列（它描述类型，不能改）。
UID_KEYWORDS: frozenset[str] = frozenset(
    {
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "SOPInstanceUID",
        "AcquisitionUID",
        "FrameOfReferenceUID",
        "SynchronizationFrameOfReferenceUID",
        "TargetFrameOfReferenceUID",
        "SourceFrameOfReferenceUID",
        "ConcatenationUID",
        "SOPInstanceUIDOfConcatenationSource",
        "MultiFrameSourceSOPInstanceUID",
        "DeviceUID",
        # 序列内引用（0008,1155；RT 3006,0024/00C2 等）
        "ReferencedSOPInstanceUID",
        "ReferencedFrameOfReferenceUID",
        "RelatedFrameOfReferenceUID",
    }
)
