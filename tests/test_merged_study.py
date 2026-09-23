"""跨任务合并下载某匿名检查：去重、跨任务引用闭包、字节冲突/缺失 409、密钥指纹校验。"""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.crypto import derive_uid
from tests.sample_data import _write_bytes
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

SECRET = "merged-secret-12345"
OTHER_SECRET = "another-secret-67890"


def _ct_bytes(sop_uid: str, series_uid: str, study_uid: str,
              top_refs: list[str] | None = None,
              deep_refs: list[str] | None = None,
              pixels: bytes | None = None) -> bytes:
    """构造带可选 SOP 引用的最小合法 DICOM（pixels 用于人为制造字节差异）。"""
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
    if pixels is not None:
        # 非空 PixelData 会进入匿名输出，用于构造同 SOP UID 但字节不同的两份文件
        ds.PixelData = pixels

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


def _download(client, job_ids: list[str], study_uid: str):
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


# --------------------------------------------------------------------------- #
def test_merged_overlapping_study_dedupes_identical_copies(client):
    # 同一检查 3.1 的实例 1.1.1 在两个批次中重复出现（字节一致），各另有一个独有实例
    files_a = {
        "a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "a/a2.dcm": _ct_bytes("1.1.2", "2.1.1", "3.1"),
    }
    files_b = {
        "b/a1-copy.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "b/a3.dcm": _ct_bytes("1.1.3", "2.1.1", "3.1"),
    }
    ja = _upload(client, files_a)
    jb = _upload(client, files_b)

    study = derive_uid(SECRET, "3.1")
    r = _download(client, [ja["id"], jb["id"]], study)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    # 初选 2 + 2 = 4 份；1.1.1 字节一致去重 1 份；补入 0；ZIP 共 3 个 UID
    assert r.headers["x-merged-initial-file-count"] == "4"
    assert r.headers["x-merged-added-uid-count"] == "0"
    assert r.headers["x-merged-deduped-file-count"] == "1"
    assert r.headers["x-merged-zip-file-count"] == "3"
    entries = _zip_entries(r.content)
    assert set(entries) == {
        derive_uid(SECRET, s) + ".dcm" for s in ("1.1.1", "1.1.2", "1.1.3")
    }
    # ZIP 条目扁平、不暴露任何来源路径
    assert all("/" not in n for n in entries)


def test_merged_closure_pulls_referenced_sops_from_other_jobs(client):
    # 任务 1：检查 3.1 的 b1 引用 1.3.1（在任务 2 中）并深层引用 1.4.1（在任务 3 中）
    files1 = {
        "j1/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "j1/b1.dcm": _ct_bytes("1.1.2", "2.1.1", "3.1",
                               top_refs=["1.3.1"], deep_refs=["1.4.1"]),
    }
    files2 = {"j2/c1.dcm": _ct_bytes("1.3.1", "2.3.1", "3.3")}
    files3 = {"j3/d1.dcm": _ct_bytes("1.4.1", "2.4.1", "3.4")}
    j1 = _upload(client, files1)
    j2 = _upload(client, files2)
    j3 = _upload(client, files3)

    study = derive_uid(SECRET, "3.1")
    r = _download(client, [j1["id"], j2["id"], j3["id"]], study)
    assert r.status_code == 200, r.text
    assert r.headers["x-merged-initial-file-count"] == "2"
    assert r.headers["x-merged-added-uid-count"] == "2"
    assert r.headers["x-merged-deduped-file-count"] == "0"
    assert r.headers["x-merged-zip-file-count"] == "4"
    assert set(_zip_entries(r.content)) == {
        derive_uid(SECRET, s) + ".dcm" for s in ("1.1.1", "1.1.2", "1.3.1", "1.4.1")
    }


def test_merged_closure_recurses_across_jobs_until_stable(client):
    # x1 -> y1（任务2）-> z1（任务3）的跨任务引用链
    files1 = {"j1/x.dcm": _ct_bytes("1.9.1", "2.9.1", "3.9", top_refs=["1.8.1"])}
    files2 = {"j2/y.dcm": _ct_bytes("1.8.1", "2.8.1", "3.8", top_refs=["1.7.1"])}
    files3 = {"j3/z.dcm": _ct_bytes("1.7.1", "2.7.1", "3.7")}
    j1 = _upload(client, files1)
    j2 = _upload(client, files2)
    j3 = _upload(client, files3)

    r = _download(client, [j1["id"], j2["id"], j3["id"]],
                  derive_uid(SECRET, "3.9"))
    assert r.status_code == 200, r.text
    assert r.headers["x-merged-added-uid-count"] == "2"
    assert set(_zip_entries(r.content)) == {
        derive_uid(SECRET, s) + ".dcm" for s in ("1.9.1", "1.8.1", "1.7.1")
    }


def test_merged_missing_reference_in_all_jobs_409(client):
    # 引用 1.9.999 不出现在任何任务中 -> 409，不产出 ZIP
    files1 = {"j1/d.dcm": _ct_bytes("1.4.1", "2.4.1", "3.4", top_refs=["1.9.999"])}
    files2 = {"j2/other.dcm": _ct_bytes("1.5.1", "2.5.1", "3.5")}
    j1 = _upload(client, files1)
    j2 = _upload(client, files2)

    r = _download(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.4"))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "MERGED_REFERENCE_CLOSURE_INCOMPLETE"
    assert detail["study_instance_uid"] == derive_uid(SECRET, "3.4")
    problem = detail["problems"][0]
    assert problem["sop_instance_uid"] == derive_uid(SECRET, "1.9.999")
    assert problem["reason"] == "not_in_output"
    assert problem["copies"] == []


def test_merged_byte_conflict_409_lists_uid_and_hashes(client):
    # 同一原始 SOP 1.1.1 经同一密钥匿名化后 UID 相同，但输入文件字节不同
    files_a = {"a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", pixels=b"AAAA")}
    files_b = {"b/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1", pixels=b"BBBB")}
    ja = _upload(client, files_a)
    jb = _upload(client, files_b)

    r = _download(client, [ja["id"], jb["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "MERGED_REFERENCE_CLOSURE_INCOMPLETE"
    problem = next(
        p for p in detail["problems"]
        if p["sop_instance_uid"] == derive_uid(SECRET, "1.1.1")
    )
    assert problem["reason"] == "byte_conflict"
    source_jobs = {c["job_id"] for c in problem["copies"]}
    assert source_jobs == {ja["id"], jb["id"]}
    hashes = {c["sha256"] for c in problem["copies"]}
    assert len(hashes) == 2 and all(hashes)
    # 只暴露扁平匿名文件名，不暴露来源路径
    assert all("/" not in c["output_name"] for c in problem["copies"])

    # 冲突发生在被引用实例上时同样 409：b1 引用字节冲突的 1.1.1
    files_c = {"c/b1.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2", top_refs=["1.1.1"])}
    jc = _upload(client, files_c)
    r2 = _download(client, [ja["id"], jb["id"], jc["id"]],
                   derive_uid(SECRET, "3.2"))
    assert r2.status_code == 409
    p2 = next(
        p for p in r2.json()["detail"]["problems"]
        if p["sop_instance_uid"] == derive_uid(SECRET, "1.1.1")
    )
    assert p2["reason"] == "byte_conflict"


def test_merged_ambiguous_sop_within_one_job_409(client):
    # 单个任务内同一 SOP 出现两次（引擎输出 <uid>.dcm / <uid>-2.dcm）
    dup_sop = "1.5.5.777"
    files = {
        "dup_a.dcm": _ct_bytes(dup_sop, "2.5.5", "3.55"),
        "dup_b.dcm": _ct_bytes(dup_sop, "2.5.5", "3.55"),
    }
    j1 = _upload(client, files)
    j2 = _upload(client, {"x/a3.dcm": _ct_bytes("1.1.3", "2.1.1", "3.1")})

    r = _download(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.55"))
    assert r.status_code == 409
    problem = next(
        p for p in r.json()["detail"]["problems"]
        if p["sop_instance_uid"] == derive_uid(SECRET, dup_sop)
    )
    assert problem["reason"] == "ambiguous_output"
    assert {c["output_name"] for c in problem["copies"]} == {
        f"{derive_uid(SECRET, dup_sop)}.dcm",
        f"{derive_uid(SECRET, dup_sop)}-2.dcm",
    }
    assert {c["job_id"] for c in problem["copies"]} == {j1["id"]}


def test_merged_secret_fingerprint_mismatch_409(client):
    ja = _upload(client, {"a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    jb = _upload(client, {"b/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")},
                 secret=OTHER_SECRET)

    r = _download(client, [ja["id"], jb["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SECRET_FINGERPRINT_MISMATCH"
    assert set(detail["fingerprints"]) == {ja["id"], jb["id"]}


def test_merged_job_not_found_404(client):
    ja = _upload(client, {"a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    r = _download(client, [ja["id"], "deadbeefdeadbeef"],
                  derive_uid(SECRET, "3.1"))
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["missing_job_ids"] == ["deadbeefdeadbeef"]


def test_merged_failed_job_409(client):
    ja = _upload(client, {"a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    broken = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": SECRET},
    ).json()
    assert broken["status"] == "failed"

    r = _download(client, [ja["id"], broken["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "JOBS_NOT_COMPLETED"
    assert detail["not_ready_jobs"][0]["id"] == broken["id"]


def test_merged_study_not_found_404(client):
    ja = _upload(client, {"a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    jb = _upload(client, {"b/b1.dcm": _ct_bytes("1.2.1", "2.2.1", "3.2")})
    r = _download(client, [ja["id"], jb["id"]], "2.25.unknown-anon-study")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "STUDY_NOT_FOUND"
    assert detail["study_instance_uid"] == "2.25.unknown-anon-study"


def test_merged_request_validation_422(client):
    ja = _upload(client, {"a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    base_url = "/jobs/merged-study/download"

    # 仅 1 个任务
    r = client.post(base_url, json={"job_ids": [ja["id"]],
                                    "study_instance_uid": "2.25.x"})
    assert r.status_code == 422

    # 6 个任务
    r = client.post(base_url, json={"job_ids": [ja["id"]] * 6,
                                    "study_instance_uid": "2.25.x"})
    assert r.status_code == 422

    # 重复任务 ID
    r = client.post(base_url, json={"job_ids": [ja["id"], ja["id"]],
                                    "study_instance_uid": "2.25.x"})
    assert r.status_code == 422

    # 空 study UID
    r = client.post(base_url, json={"job_ids": [ja["id"], ja["id"] + "x"],
                                    "study_instance_uid": "   "})
    assert r.status_code == 422


def test_merged_accepts_up_to_five_jobs(client):
    # 5 个任务各含同一检查的一个不同实例；同密钥下派生 UID 一致、字节各自独立
    jobs = []
    for i in range(5):
        jobs.append(_upload(client, {
            f"j{i}/f.dcm": _ct_bytes(f"1.1.{i + 1}", "2.1.1", "3.1")
        }))
    r = _download(client, [j["id"] for j in jobs], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200, r.text
    assert r.headers["x-merged-zip-file-count"] == "5"


def test_merged_bytes_match_single_job_download(client):
    # 合并包内文件与各任务全量包/子集包逐字节一致，且条目名就是匿名 SOP UID
    files1 = {
        "a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1"),
        "a/a2.dcm": _ct_bytes("1.1.2", "2.1.1", "3.1", top_refs=["1.3.1"]),
    }
    files2 = {"b/c1.dcm": _ct_bytes("1.3.1", "2.3.1", "3.3")}
    j1 = _upload(client, files1)
    j2 = _upload(client, files2)

    full1 = _zip_entries(client.get(f"/jobs/{j1['id']}/download").content)
    full2 = _zip_entries(client.get(f"/jobs/{j2['id']}/download").content)

    r = _download(client, [j1["id"], j2["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200
    entries = _zip_entries(r.content)
    for name, blob in entries.items():
        source = full1.get(name, full2.get(name))
        assert source is not None
        assert blob == source

    # 临时 ZIP 在响应结束后无残留
    leftovers = [
        n for n in os.listdir(_registry().work_dir)
        if n.startswith("merged-study-")
    ]
    assert leftovers == []


def test_merged_content_disposition_filename(client):
    ja = _upload(client, {"a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")})
    jb = _upload(client, {"b/x.dcm": _ct_bytes("1.9.9", "2.9.9", "3.9")})
    r = _download(client, [ja["id"], jb["id"]], derive_uid(SECRET, "3.1"))
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment; filename=\"merged-study-")
    assert cd.endswith(".zip\"")


def test_merged_request_order_does_not_change_result(client):
    files_a = {"a/a1.dcm": _ct_bytes("1.1.1", "2.1.1", "3.1")}
    files_b = {"b/a2.dcm": _ct_bytes("1.1.2", "2.1.1", "3.1")}
    ja = _upload(client, files_a)
    jb = _upload(client, files_b)
    study = derive_uid(SECRET, "3.1")

    r1 = _download(client, [ja["id"], jb["id"]], study)
    r2 = _download(client, [jb["id"], ja["id"]], study)
    assert r1.status_code == r2.status_code == 200
    assert _zip_entries(r1.content) == _zip_entries(r2.content)
