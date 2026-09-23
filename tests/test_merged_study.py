"""跨任务合并下载匿名检查（POST /jobs/merged-study/download）。

覆盖：跨任务引用闭包补全、字节一致去重、字节冲突 409、跨任务缺失引用 409、
同密钥指纹门槛、任务不存在/未完成、404 检查缺失、扁平匿名文件名（不暴露来源）。
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.crypto import derive_uid
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian
from tests.sample_data import _write_bytes

SECRET = "merged-secret-123456"


def _ct_bytes(sop_uid: str, series_uid: str, study_uid: str,
              top_refs: list[str] | None = None,
              deep_refs: list[str] | None = None,
              patient_id: str = "P-1") -> bytes:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = patient_id
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


def _upload(client, files: dict[str, bytes], secret: str = SECRET):
    r = client.post(
        "/jobs",
        files={"file": ("studies.zip", _zip_bytes(files), "application/zip")},
        data={"secret": secret, "shift_dates": "false"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _merge(client, job_ids, study_uid):
    return client.post(
        "/jobs/merged-study/download",
        json={"job_ids": job_ids, "study_instance_uid": study_uid},
    )


def _zip_entries(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _registry():
    from app.main import registry
    return registry


def _output_bytes(job_id: str, output_name: str) -> bytes:
    job = _registry().get(job_id)
    with open(os.path.join(job.result.output_dir, output_name), "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
def test_merged_selects_study_across_jobs_with_cross_job_closure(client):
    # 任务 A：检查 3.1 的实例 a1，引用检查 3.9 的 x1（不在 A 中）
    j1 = _upload(client, {
        "a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", top_refs=["1.9.1"]),
    })
    # 任务 B：检查 3.2 的 b1 -> c1(检查 3.3，任务 C）；并提供被 A 引用的 x1
    j2 = _upload(client, {
        "b/b1.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2", top_refs=["1.3.1"]),
        "b/x1.dcm": _ct_bytes("1.9.1", "2.9.1", "3.9"),
    })
    # 任务 C：提供 c1
    j3 = _upload(client, {
        "c/c1.dcm": _ct_bytes("1.3.1", "2.3.1", "3.3"),
    })

    study = derive_uid(SECRET, "3.1")
    r = _merge(client, [j1["id"], j2["id"], j3["id"]], study)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["x-merged-job-count"] == "3"
    # 初选：仅 a1；递归补入 x1（任务 B）、b1（同文件含该引用？否）……
    # a1 引用 x1；x1 无外发引用。闭包 = {a1, x1}
    assert r.headers["x-merged-study-initial-file-count"] == "1"
    assert r.headers["x-merged-study-added-file-count"] == "1"
    assert r.headers["x-merged-study-file-count"] == "2"
    assert r.headers["x-merged-study-deduped-copy-count"] == "0"

    names = set(_zip_entries(r.content))
    assert names == {derive_uid(SECRET, s) + ".dcm" for s in ("1.1.1", "1.9.1")}
    # 扁平匿名文件名：无目录、无来源任务路径
    assert all("/" not in n for n in names)

    # 以检查 3.2 为初选：b1 -> c1（跨到任务 C）
    r2 = _merge(client, [j1["id"], j2["id"], j3["id"]], derive_uid(SECRET, "3.2"))
    assert r2.status_code == 200
    assert r2.headers["x-merged-study-initial-file-count"] == "1"
    assert r2.headers["x-merged-study-added-file-count"] == "1"
    assert set(_zip_entries(r2.content)) == {
        derive_uid(SECRET, s) + ".dcm" for s in ("1.2.1", "1.3.1")
    }


def test_merged_closure_chains_across_multiple_jobs(client):
    j1 = _upload(client, {
        "s1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", top_refs=["1.2.1"]),
    })
    j2 = _upload(client, {
        "s2.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2", top_refs=["1.3.1"]),
    })
    j3 = _upload(client, {
        "s3.dcm": _ct_bytes("1.3.1", "2.3.1", "3.3"),
    })
    r = _merge(client, [j1["id"], j2["id"], j3["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200, r.text
    assert r.headers["x-merged-study-added-file-count"] == "2"
    assert set(_zip_entries(r.content)) == {
        derive_uid(SECRET, s) + ".dcm" for s in ("1.1.1", "1.2.1", "1.3.1")
    }


def test_merged_deduplicates_identical_sop_byte_for_byte(client):
    # 同一原始 SOP 在两个同密钥任务中各出现一次：匿名输出逐字节相同 -> 只保留一份
    common = _ct_bytes("1.1.1", "2.1.1", "3.1")
    j1 = _upload(client, {"dirA/common.dcm": common})
    j2 = _upload(client, {"totally/different/path/common.dcm": common})

    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200, r.text
    entries = _zip_entries(r.content)
    assert set(entries) == {derive_uid(SECRET, "1.1.1") + ".dcm"}
    # 初选 UID 去重后为 1 份；两个任务中的重复物理副本计入 deduped
    assert r.headers["x-merged-study-initial-file-count"] == "1"
    assert r.headers["x-merged-study-added-file-count"] == "0"
    assert r.headers["x-merged-study-file-count"] == "1"
    assert r.headers["x-merged-study-deduped-copy-count"] == "1"
    # 保留内容与任一任务输出逐字节一致
    assert entries[derive_uid(SECRET, "1.1.1") + ".dcm"] == _output_bytes(
        j1["id"], derive_uid(SECRET, "1.1.1") + ".dcm"
    )


def test_merged_deduplicates_identical_within_one_job(client):
    # 单个任务内重复 SOP（输出 <uid>.dcm 与 <uid>-2.dcm），内容相同 -> 合并去重
    dup = _ct_bytes("1.5.5.777", "2.5.5", "3.55")
    j1 = _upload(client, {"dup_a.dcm": dup, "dup_b.dcm": dup})
    j2 = _upload(client, {"other.dcm": _ct_bytes("1.7.1", "2.7.1", "3.77")})

    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.55"))
    assert r.status_code == 200, r.text
    assert set(_zip_entries(r.content)) == {derive_uid(SECRET, "1.5.5.777") + ".dcm"}
    assert r.headers["x-merged-study-deduped-copy-count"] == "1"


def test_merged_byte_conflict_returns_409_with_uid(client):
    # 同密钥、同原始 SOP，但文件内容不同（患者不同 -> 匿名输出字节不同）
    j1 = _upload(client, {
        "a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", patient_id="P-A"),
    })
    j2 = _upload(client, {
        "b.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", patient_id="P-B"),
    })
    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "MERGED_REFERENCE_CLOSURE_INCOMPLETE"
    conflict_uid = derive_uid(SECRET, "1.1.1")
    assert detail["conflicting_sop_instance_uids"] == [conflict_uid]
    assert detail["missing_sop_instance_uids"] == []
    problem = detail["problems"][0]
    assert problem["sop_instance_uid"] == conflict_uid
    assert problem["reason"] == "byte_conflict"
    assert set(problem["job_ids"]) == {j1["id"], j2["id"]}
    hashes = {c["sha256"] for c in problem["copies"]}
    assert len(hashes) == 2  # 两份输出字节确实不同
    # 不暴露绝对路径：output_file 仅为扁平匿名文件名
    for copy in problem["copies"]:
        assert "/" not in copy["output_file"]
        assert j1["id"] not in copy["output_file"]

    # 拒绝输出：不产生残留临时 ZIP
    work = _registry().work_dir
    leftovers = [
        n for jn in os.listdir(work)
        for n in os.listdir(os.path.join(work, jn))
        if n.startswith("merged-study-")
    ] if os.path.isdir(work) else []
    assert leftovers == []


def test_merged_missing_reference_in_all_jobs_409(client):
    j1 = _upload(client, {
        "d1.dcm": _ct_bytes("1.4.1", "2.4.1", "3.4", top_refs=["1.4.999"]),
    })
    j2 = _upload(client, {
        "other.dcm": _ct_bytes("1.7.1", "2.7.1", "3.7"),
    })
    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.4"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "MERGED_REFERENCE_CLOSURE_INCOMPLETE"
    missing_uid = derive_uid(SECRET, "1.4.999")
    assert detail["missing_sop_instance_uids"] == [missing_uid]
    problem = detail["problems"][0]
    assert problem["sop_instance_uid"] == missing_uid
    assert problem["reason"] == "missing_in_all_jobs"
    assert problem["job_ids"] == []


def test_merged_reports_conflict_and_missing_together(client):
    # 同时暴露字节冲突 + 缺失引用，一次性全部列出
    j1 = _upload(client, {
        # 冲突 SOP（初选成员）
        "a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", patient_id="P-A",
                           top_refs=["1.4.999"]),
    })
    j2 = _upload(client, {
        "b.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", patient_id="P-B"),
    })
    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["conflicting_sop_instance_uids"] == [derive_uid(SECRET, "1.1.1")]
    assert detail["missing_sop_instance_uids"] == [derive_uid(SECRET, "1.4.999")]
    reasons = {p["reason"] for p in detail["problems"]}
    assert reasons == {"byte_conflict", "missing_in_all_jobs"}


def test_merged_conflict_only_when_within_closure(client):
    # 字节冲突的 SOP 不属于闭包（无任何引用指向它）时不应导致 409
    j1 = _upload(client, {
        "a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", patient_id="P-A"),
        # 另一检查中与 j2 字节冲突的实例
        "x.dcm": _ct_bytes("1.9.1", "2.9.1", "3.9", patient_id="P-A"),
    })
    j2 = _upload(client, {
        "b.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", patient_id="P-A"),
        "y.dcm": _ct_bytes("1.9.1", "2.9.1", "3.9", patient_id="P-B"),
    })
    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200, r.text
    assert set(_zip_entries(r.content)) == {derive_uid(SECRET, "1.1.1") + ".dcm"}


def test_merged_different_secret_fingerprint_409(client):
    j1 = _upload(client, {"a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")},
                 secret="merged-secret-AAA")
    j2 = _upload(client, {"b.dcm": _ct_bytes("1.2.1", "2.2.1", "3.1")},
                 secret="merged-secret-BBB")
    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SECRET_FINGERPRINT_MISMATCH"
    assert len(set(detail["fingerprints"].values())) == 2
    assert detail["study_instance_uid"] == derive_uid(SECRET, "3.1")


def test_merged_missing_job_404(client):
    j1 = _upload(client, {"a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    r = _merge(client, [j1["id"], "does-not-exist"], derive_uid(SECRET, "3.1"))
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["missing_job_ids"] == ["does-not-exist"]


def test_merged_failed_job_409(client):
    failed = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    ).json()
    ok = _upload(client, {"a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    r = _merge(client, [failed["id"], ok["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "JOBS_NOT_COMPLETED"
    assert {j["id"] for j in detail["not_ready_jobs"]} == {failed["id"]}


def test_merged_study_not_in_any_job_404(client):
    j1 = _upload(client, {"a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    j2 = _upload(client, {"b.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2")})
    r = _merge(client, [j1["id"], j2["id"]], "2.25.unknown-anon-study")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "STUDY_NOT_FOUND"
    assert detail["study_instance_uid"] == "2.25.unknown-anon-study"


def test_merged_validation_cardinality_and_duplicates(client):
    j = _upload(client, {"a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    body = {"study_instance_uid": derive_uid(SECRET, "3.1")}
    assert client.post("/jobs/merged-study/download",
                       json={**body, "job_ids": [j["id"]]}).status_code == 422
    assert client.post("/jobs/merged-study/download",
                       json={**body, "job_ids": [j["id"]] * 6}).status_code == 422
    assert client.post("/jobs/merged-study/download",
                       json={**body, "job_ids": [j["id"], j["id"]]}).status_code == 422
    assert client.post("/jobs/merged-study/download",
                       json={"job_ids": [j["id"], j["id"]],
                             "study_instance_uid": "  "}).status_code == 422
    assert client.post("/jobs/merged-study/download",
                       json={"job_ids": [j["id"], "  "],
                             "study_instance_uid": "2.25.x"}).status_code == 422


def test_merged_accepts_up_to_five_jobs(client):
    jobs = [
        _upload(client, {f"d{i}.dcm": _ct_bytes(f"1.1.{i}", "2.1.1", "3.1")})
        for i in range(5)
    ]
    # 5 个不同原始 SOP 的实例属于同一检查：初选 5 份、无去重
    r = _merge(client, [j["id"] for j in jobs], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200, r.text
    assert r.headers["x-merged-job-count"] == "5"
    assert r.headers["x-merged-study-file-count"] == "5"


def test_merged_zip_filename_and_content_disposition(client):
    j1 = _upload(client, {"a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    j2 = _upload(client, {"b.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2")})
    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment; filename=\"merged-study-")
    assert cd.endswith(".zip\"")


def test_merged_temp_zip_cleaned_up_after_response(client):
    j1 = _upload(client, {"a.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    j2 = _upload(client, {"b.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2")})
    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200
    leftovers = [
        n for n in os.listdir(_registry().work_dir)
        if n.startswith("merged-study-")
    ]
    assert leftovers == []


def test_merged_does_not_expose_source_paths_in_zip(client):
    j1 = _upload(client, {
        "patient_A/secret_dir/CT0001.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
    })
    j2 = _upload(client, {
        "batch_B/patient_A/CT0001.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
    })
    r = _merge(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200, r.text
    names = list(_zip_entries(r.content))
    assert names == [derive_uid(SECRET, "1.1.1") + ".dcm"]
    for name in names:
        assert "patient_A" not in name and "secret_dir" not in name
        assert "batch_B" not in name and not name.endswith("/")
