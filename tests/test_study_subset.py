"""按匿名 StudyInstanceUID 下载检查子集：引用闭包、404/409、复用已产出匿名文件。"""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.crypto import derive_uid
from tests.sample_data import _write_bytes  # 复用合成 DICOM 写出工具
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

SECRET = "subset-secret-123456"


def _ct_bytes(sop_uid: str, series_uid: str, study_uid: str,
              top_refs: list[str] | None = None,
              deep_refs: list[str] | None = None) -> bytes:
    """构造带可选 SOP 引用的最小合法 DICOM。

    top_refs：顶层 ReferencedSOPSequence[*].ReferencedSOPInstanceUID；
    deep_refs：深层 ReferencedFrameOfReferenceSequence[0]
               .RTReferencedStudySequence[0].ContourImageSequence[*] 中的引用。
    """
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = "P-1"
    ds.PatientName = "Zhang^San"
    ds.Modality = "CT"

    if top_refs:
        items = []
        for ref in top_refs:
            item = Dataset()
            item.ReferencedSOPClassUID = CTImageStorage
            item.ReferencedSOPInstanceUID = ref
            items.append(item)
        ds.ReferencedSOPSequence = items

    if deep_refs:
        rfors = Dataset()
        rfors.FrameOfReferenceUID = "1.9.9.3"
        rtrs = Dataset()
        rtrs.SeriesInstanceUID = series_uid
        contours = []
        for ref in deep_refs:
            cis = Dataset()
            cis.ReferencedSOPClassUID = CTImageStorage
            cis.ReferencedSOPInstanceUID = ref
            contours.append(cis)
        rtrs.ContourImageSequence = contours
        rfors.RTReferencedStudySequence = [rtrs]
        ds.ReferencedFrameOfReferenceSequence = [rfors]

    return _write_bytes(ds)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in files.items():
            zf.writestr(name, blob)
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(tmp_path / "jobs"))
    import importlib
    import app.config
    import app.jobs
    importlib.reload(app.config)
    importlib.reload(app.jobs)
    import app.main
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
        yield c


def _upload(client, files: dict[str, bytes]):
    r = client.post(
        "/jobs",
        files={"file": ("studies.zip", _zip_bytes(files), "application/zip")},
        data={"secret": SECRET, "shift_dates": "false"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _download(client, job_id: str, study_uid: str):
    return client.get(f"/jobs/{job_id}/studies/{study_uid}/download")


def _zip_names(content: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return set(zf.namelist())


def _registry():
    from app.main import registry
    return registry


# --------------------------------------------------------------------------- #
def test_subset_initial_study_without_outgoing_refs(client):
    files = {
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "b1.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2", top_refs=["1.1.1"]),
    }
    job = _upload(client, files)
    new_study_a = derive_uid(SECRET, "3.1")

    r = _download(client, job["id"], new_study_a)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["x-study-initial-file-count"] == "1"
    assert r.headers["x-study-added-file-count"] == "0"
    assert r.headers["x-study-subset-file-count"] == "1"
    names = _zip_names(r.content)
    assert names == {derive_uid(SECRET, "1.1.1") + ".dcm"}
    assert all("/" not in n for n in names)


def test_subset_closure_cross_study_and_deep_nested(client):
    files = {
        # 检查 A：无外发引用
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        # 检查 B：b1 深层嵌套引用 a1；b2 顶层跨检查引用 c1
        "b1.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2", deep_refs=["1.1.1"]),
        "b2.dcm": _ct_bytes("1.2.2", "2.2.1", "3.2", top_refs=["1.3.1"]),
        # 检查 C
        "c1.dcm": _ct_bytes("1.3.1", "2.3.1", "3.3"),
    }
    job = _upload(client, files)
    study_b = derive_uid(SECRET, "3.2")

    r = _download(client, job["id"], study_b)
    assert r.status_code == 200, r.text
    assert r.headers["x-study-initial-file-count"] == "2"
    assert r.headers["x-study-added-file-count"] == "2"
    assert r.headers["x-study-subset-file-count"] == "4"
    assert _zip_names(r.content) == {
        derive_uid(SECRET, s) + ".dcm" for s in ("1.1.1", "1.2.1", "1.2.2", "1.3.1")
    }

    # 引用是有方向的：请求 A / C 都不会反向带入 B
    r_a = _download(client, job["id"], derive_uid(SECRET, "3.1"))
    assert r_a.headers["x-study-initial-file-count"] == "1"
    assert r_a.headers["x-study-added-file-count"] == "0"
    r_c = _download(client, job["id"], derive_uid(SECRET, "3.3"))
    assert r_c.headers["x-study-added-file-count"] == "0"
    assert _zip_names(r_c.content) == {derive_uid(SECRET, "1.3.1") + ".dcm"}


def test_subset_closure_follows_chains_until_stable(client):
    files = {
        "x1.dcm": _ct_bytes("1.9.1", "2.9.1", "3.9", top_refs=["1.8.1"]),
        "y1.dcm": _ct_bytes("1.8.1", "2.8.1", "3.8", top_refs=["1.7.1"]),
        "z1.dcm": _ct_bytes("1.7.1", "2.7.1", "3.7"),
    }
    job = _upload(client, files)
    r = _download(client, job["id"], derive_uid(SECRET, "3.9"))
    assert r.status_code == 200, r.text
    assert r.headers["x-study-initial-file-count"] == "1"
    assert r.headers["x-study-added-file-count"] == "2"
    assert _zip_names(r.content) == {
        derive_uid(SECRET, s) + ".dcm" for s in ("1.9.1", "1.8.1", "1.7.1")
    }


def test_subset_dangling_reference_409(client):
    files = {
        "d1.dcm": _ct_bytes("1.4.1", "2.4.1", "3.4", top_refs=["1.4.999"]),
    }
    job = _upload(client, files)
    r = _download(client, job["id"], derive_uid(SECRET, "3.4"))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "REFERENCE_CLOSURE_INCOMPLETE"
    assert detail["study_instance_uid"] == derive_uid(SECRET, "3.4")
    problem = detail["problems"][0]
    # 返回的必须是匿名后的 UID 与原因
    assert problem["sop_instance_uid"] == derive_uid(SECRET, "1.4.999")
    assert problem["reason"] == "not_in_output"
    assert problem["message"]

    # 全量下载不受影响（原有行为不变）
    full = client.get(f"/jobs/{job['id']}/download")
    assert full.status_code == 200
    assert len(_zip_names(full.content)) == 1


def test_subset_ambiguous_sop_409(client):
    dup_sop = "1.5.5.777"
    files = {
        # 两个文件共享同一 SOPInstanceUID（引擎输出为 <uid>.dcm / <uid>-2.dcm）
        "dup_a.dcm": _ct_bytes(dup_sop, "2.5.5", "3.55"),
        "dup_b.dcm": _ct_bytes(dup_sop, "2.5.5", "3.55"),
        # 检查 E 引用该重复 SOP
        "e1.dcm": _ct_bytes("1.6.6.1", "2.6.6", "3.66", top_refs=[dup_sop]),
    }
    job = _upload(client, files)

    r = _download(client, job["id"], derive_uid(SECRET, "3.66"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "REFERENCE_CLOSURE_INCOMPLETE"
    problem = next(
        p for p in detail["problems"]
        if p["sop_instance_uid"] == derive_uid(SECRET, dup_sop)
    )
    assert problem["reason"] == "ambiguous_output"
    new_sop = derive_uid(SECRET, dup_sop)
    assert problem["output_files"] == sorted([f"{new_sop}.dcm", f"{new_sop}-2.dcm"])

    # 直接请求重复 SOP 所在检查同样拒绝：一个 SOP UID 对应多个输出文件即 409
    r2 = _download(client, job["id"], derive_uid(SECRET, "3.55"))
    assert r2.status_code == 409, r2.text
    p2 = next(
        p for p in r2.json()["detail"]["problems"]
        if p["sop_instance_uid"] == new_sop
    )
    assert p2["reason"] == "ambiguous_output"

    # 全量下载不受影响（原有行为不变），重复 SOP 的两个文件都保留
    full = client.get(f"/jobs/{job['id']}/download")
    assert full.status_code == 200
    assert set(_zip_names(full.content)) == {f"{new_sop}.dcm", f"{new_sop}-2.dcm",
                                             derive_uid(SECRET, "1.6.6.1") + ".dcm"}


def test_subset_study_not_found_404(client):
    files = {"a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")}
    job = _upload(client, files)
    r = _download(client, job["id"], "2.25.unknown-anon-study-uid")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "STUDY_NOT_FOUND"
    assert detail["study_instance_uid"] == "2.25.unknown-anon-study-uid"


def test_subset_job_not_found_404(client):
    r = _download(client, "deadbeef", "2.25.whatever")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "JOB_NOT_FOUND"


def test_subset_failed_job_409(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    )
    job = r.json()
    assert job["status"] == "failed"
    resp = _download(client, job["id"], "2.25.whatever")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "JOB_NOT_READY"


def test_subset_reuses_anonymized_outputs_byte_identical(client):
    files = {
        "a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "b1.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2", top_refs=["1.1.1"]),
    }
    job = _upload(client, files)

    full = client.get(f"/jobs/{job['id']}/download")
    assert full.status_code == 200
    with zipfile.ZipFile(io.BytesIO(full.content)) as zf:
        full_bytes = {n: zf.read(n) for n in zf.namelist()}

    sub = _download(client, job["id"], derive_uid(SECRET, "3.2"))
    assert sub.status_code == 200
    with zipfile.ZipFile(io.BytesIO(sub.content)) as zf:
        for name in zf.namelist():
            assert zf.read(name) == full_bytes[name]  # 与全量包内文件逐字节一致

    # 子集 ZIP 为一次性临时文件：响应结束后任务目录无残留
    job_obj = _registry().get(job["id"])
    base = os.path.dirname(job_obj.result.output_dir)
    leftovers = [n for n in os.listdir(base) if n.startswith("study-subset-")]
    assert leftovers == []


def test_subset_filename_and_content_disposition(client):
    files = {"a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")}
    job = _upload(client, files)
    r = _download(client, job["id"], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment; filename=\"study-")
    assert r.headers["content-disposition"].endswith(".zip\"")


def test_subset_with_canonical_sample_zip(client, tmp_path):
    """标准样例：患者 1 的 RTSTRUCT 带悬空引用 -> 子集 409；患者 2 正常下载。"""
    from tests.sample_data import build_sample_zip

    zip_path = tmp_path / "sample.zip"
    meta = build_sample_zip(str(zip_path), seed=1)
    with open(zip_path, "rb") as fh:
        r = client.post(
            "/jobs",
            files={"file": ("study.zip", fh, "application/zip")},
            data={"secret": SECRET, "shift_dates": "false"},
        )
    assert r.status_code == 201, r.text
    job = r.json()

    # 患者 2 的检查没有任何外发引用：恰好 1 个文件，补入 0
    # sample_data 中患者 2 的 StudyInstanceUID 为 uid(12)
    study_p2 = derive_uid(SECRET, "1.2.3.4.5.6.7.8.9.1.12")
    ok = _download(client, job["id"], study_p2)
    assert ok.status_code == 200, ok.text
    assert ok.headers["x-study-initial-file-count"] == "1"
    assert ok.headers["x-study-added-file-count"] == "0"

    # 患者 1 的检查：RTSTRUCT 引用了包外 SOP（dangling=...9.1.99）-> 409
    bad = _download(client, job["id"], derive_uid(SECRET, meta["study_uid"]))
    assert bad.status_code == 409
    detail = bad.json()["detail"]
    assert detail["code"] == "REFERENCE_CLOSURE_INCOMPLETE"
    problem = next(
        p for p in detail["problems"]
        if p["sop_instance_uid"] == derive_uid(SECRET, meta["dangling"])
    )
    assert problem["reason"] == "not_in_output"

    # 同一任务的全量下载依旧可用（原有行为不变）
    full = client.get(f"/jobs/{job['id']}/download")
    assert full.status_code == 200
    assert len(_zip_names(full.content)) == job["output_file_count"] == 4
