"""构造合成 DICOM ZIP 的测试夹具：CT + RTSTRUCT（深层引用）+ 非法文件。"""
from __future__ import annotations

import io
import zipfile

from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    RTStructureSetStorage,
)


def _write_bytes(ds: FileDataset) -> bytes:
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _ct_like(sop_uid: str, series_uid: str, study_uid: str, patient_id: str,
             patient_name: str, frame_uid: str, pixels: bytes | None = None) -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)

    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.FrameOfReferenceUID = frame_uid
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.PatientBirthDate = "19800115"
    ds.PatientSex = "M"
    ds.StudyDate = "20240301"
    ds.SeriesDate = "20240301"
    ds.ContentDate = "20240301"
    ds.StudyTime = "101500"
    ds.AccessionNumber = "ACC001"
    ds.StudyID = "STUDY1"
    ds.StudyDescription = "Head CT"
    ds.SeriesDescription = "Axial 5mm"
    ds.InstitutionName = "Secret Hospital"
    ds.ReferringPhysicianName = "Dr^House"
    ds.OperatorsName = "Op^One"
    ds.StationName = "CT-STATION-1"
    ds.DeviceSerialNumber = "SN-12345"
    ds.PatientAddress = "1 Secret Street"
    ds.ImageComments = "free text PHI"
    ds.Manufacturer = "Vendor"
    ds.Modality = "CT"
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    if pixels is None:
        pixels = bytes(range(32))
    ds.PixelData = pixels
    # 私有标签
    block = ds.private_block(0x0009, "PHI CREATOR", create=True)
    block.add_new(0x10, "LO", "Private PHI value")
    return _write_bytes(ds)


def _rtstruct_like(sop_uid: str, series_uid: str, study_uid: str, patient_id: str,
                   patient_name: str, frame_uid: str, ref_sop: str,
                   dangling_sop: str) -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = RTStructureSetStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = RTStructureSetStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.PatientBirthDate = "19800115"
    ds.StudyDate = "20240302"
    ds.AccessionNumber = "ACC001"
    ds.InstitutionName = "Secret Hospital"
    ds.StructureSetLabel = "RTSTRUCT"
    ds.Modality = "RTSTRUCT"

    # RT ReferencedFrameOfReferenceSequence（深层嵌套引用）
    rfors = Dataset()
    rfors.FrameOfReferenceUID = frame_uid
    rtrs = Dataset()
    rtrs.SeriesInstanceUID = series_uid
    cis = Dataset()
    cis.ReferencedSOPClassUID = CTImageStorage
    cis.ReferencedSOPInstanceUID = ref_sop
    rtrs.ContourImageSequence = [cis]
    rfors.RTReferencedStudySequence = [rtrs]
    ds.ReferencedFrameOfReferenceSequence = [rfors]

    # 引用一个压缩包内不存在的 SOP（dangling reference）
    dangling_item = Dataset()
    dangling_item.ReferencedSOPClassUID = CTImageStorage
    dangling_item.ReferencedSOPInstanceUID = dangling_sop
    ds.ReferencedSOPSequence = [dangling_item]
    return _write_bytes(ds)


def build_sample_zip(path: str, seed: int = 1) -> dict:
    def uid(i: int) -> str:
        return f"1.2.3.4.5.6.7.8.9.{seed}.{i}"

    study_uid = uid(1)
    series_uid = uid(2)
    frame_uid = uid(3)
    sops = [uid(4), uid(5)]
    rt_sop = uid(6)
    dangling = uid(99)

    data = {
        "patient_1/CT0001.dcm": _ct_like(sops[0], series_uid, study_uid, "P-001", "Zhang^San", frame_uid),
        "patient_1/CT0002.dcm": _ct_like(sops[1], series_uid, study_uid, "P-001", "Zhang^San", frame_uid),
        "patient_1/rt/RS0001.dcm": _rtstruct_like(rt_sop, uid(7), study_uid,
                                                  "P-001", "Zhang^San", frame_uid,
                                                  sops[0], dangling),
        "patient_2/CT0001.dcm": _ct_like(uid(10), uid(11), uid(12),
                                         "P-002", "Li^Si", uid(13)),
        "notes/not-a-dicom.txt": b"hello, this is not dicom",
        "broken/bad.dcm": b"\x00\x01\x02not dicom at all\xff\xfe",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in data.items():
            zf.writestr(name, blob)
    return {
        "study_uid": study_uid,
        "series_uid": series_uid,
        "frame_uid": frame_uid,
        "sops": sops,
        "rt_sop": rt_sop,
        "dangling": dangling,
    }
