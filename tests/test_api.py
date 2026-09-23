"""FastAPI 端到端测试：上传、预览、诊断、下载、错误处理。"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.sample_data import build_sample_zip


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 必须在导入应用配置前设置工作目录
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("DICOM_ANON_JOB_TTL_SECONDS", "3600")
    # 清理已导入模块以让配置重新生效
    import importlib
    import app.config
    import app.jobs
    importlib.reload(app.config)
    importlib.reload(app.jobs)
    import app.main
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
        yield c


@pytest.fixture()
def sample_zip_bytes(tmp_path):
    p = tmp_path / "sample.zip"
    build_sample_zip(str(p))
    return p.read_bytes()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_full_flow_upload_preview_download(client, sample_zip_bytes):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890", "shift_dates": "true"},
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "ready"
    assert job["file_count"] == 6
    assert job["output_file_count"] == 4
    assert job["error_count"] == 2
    assert len(job["patients"]) == 2
    # 预览中包含逐文件变更
    ct = next(f for f in job["files"] if f["name"] == "patient_1/CT0001.dcm")
    assert ct["status"] == "ok"
    keywords = {c["keyword"] for c in ct["changes"]}
    assert "PatientID" in keywords and "StudyInstanceUID" in keywords

    job_id = job["id"]
    # GET /jobs/{id}
    r2 = client.get(f"/jobs/{job_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == job_id

    # /files 子资源
    r3 = client.get(f"/jobs/{job_id}/files")
    assert r3.status_code == 200
    assert len(r3.json()) == 6

    # 下载
    r4 = client.get(f"/jobs/{job_id}/download")
    assert r4.status_code == 200
    assert r4.headers["content-type"] == "application/zip"
    body = io.BytesIO(r4.content)
    with zipfile.ZipFile(body) as zf:
        assert len(zf.namelist()) == 4
        assert all(n.endswith(".dcm") for n in zf.namelist())


def test_dangling_reference_diagnostic_visible(client, sample_zip_bytes):
    job = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890"},
    ).json()
    rt = next(f for f in job["files"] if f["name"].endswith("RS0001.dcm"))
    assert rt["status"] == "warning"
    codes = {d["code"] for d in rt["diagnostics"]}
    assert "DANGLING_REFERENCE" in codes


def test_invalid_dicom_diagnostic_visible(client, sample_zip_bytes):
    job = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890"},
    ).json()
    bad = next(f for f in job["files"] if f["name"] == "broken/bad.dcm")
    assert bad["status"] == "error"
    assert any(d["code"] == "INVALID_DICOM" and d["severity"] == "error"
               for d in bad["diagnostics"])


def test_short_secret_rejected(client, sample_zip_bytes):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "short"},
    )
    assert r.status_code == 422


def test_non_zip_rejected(client):
    r = client.post(
        "/jobs",
        files={"file": ("study.txt", b"hello", "text/plain")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 422


def test_empty_upload_rejected(client):
    r = client.post(
        "/jobs",
        files={"file": ("empty.zip", b"", "application/zip")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 422


def test_bad_zip_reports_failed_job(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"]


def test_job_not_found(client):
    assert client.get("/jobs/nope/download").status_code == 404
    assert client.get("/jobs/nope").status_code == 404


def test_same_secret_cross_batch_same_pseudonym(client, sample_zip_bytes):
    def upload():
        return client.post(
            "/jobs",
            files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
            data={"secret": "cross-batch-secret-123"},
        ).json()

    j1 = upload()
    j2 = upload()
    p1 = {p["key"]: p["new_patient_id"] for p in j1["patients"]}
    p2 = {p["key"]: p["new_patient_id"] for p in j2["patients"]}
    assert p1 == p2


# --------------------------------------------------------------------------- #
# date_shift_min / date_shift_max
# --------------------------------------------------------------------------- #
def _upload_with_range(client, sample_zip_bytes, secret, **form):
    data = {"secret": secret}
    data.update(form)
    return client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data=data,
    )


def test_date_shift_range_defaults_in_response(client, sample_zip_bytes):
    r = _upload_with_range(client, sample_zip_bytes, "range-secret-12345")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["date_shift_min"] == -365
    assert body["date_shift_max"] == 365
    assert body["shift_dates"] is True
    # 患者偏移全部落在缺省闭区间内
    assert all(-365 <= p["date_shift_days"] <= 365 for p in body["patients"])


@pytest.mark.parametrize("form", [
    {"date_shift_min": "-10"},          # 只给 min
    {"date_shift_max": "10"},           # 只给 max
    {"date_shift_min": "-366", "date_shift_max": "0"},   # min 越下界
    {"date_shift_min": "0", "date_shift_max": "366"},    # max 越上界
    {"date_shift_min": "10", "date_shift_max": "-10"},   # min > max
    {"date_shift_min": "30", "date_shift_max": "10"},
])
def test_date_shift_range_invalid_422(client, sample_zip_bytes, form):
    r = _upload_with_range(client, sample_zip_bytes, "range-secret-12345", **form)
    assert r.status_code == 422, form


def test_date_shift_range_non_integer_422(client, sample_zip_bytes):
    r = _upload_with_range(
        client, sample_zip_bytes, "range-secret-12345",
        date_shift_min="abc", date_shift_max="10",
    )
    assert r.status_code == 422


def test_date_shift_range_boundary_values_accepted(client, sample_zip_bytes):
    # 闭区间边界：-365/-365、-365/365、365/365 均合法
    for lo, hi in ((-365, -365), (-365, 365), (365, 365)):
        r = _upload_with_range(
            client, sample_zip_bytes, "range-secret-12345",
            date_shift_min=str(lo), date_shift_max=str(hi),
        )
        assert r.status_code == 201, (lo, hi, r.text)
        body = r.json()
        assert body["date_shift_min"] == lo
        assert body["date_shift_max"] == hi
        assert all(lo <= p["date_shift_days"] <= hi for p in body["patients"])


def test_custom_range_stable_across_batches(client, sample_zip_bytes):
    def upload():
        return _upload_with_range(
            client, sample_zip_bytes, "range-cross-batch-1",
            shift_dates="true", date_shift_min="-5", date_shift_max="-5",
        ).json()

    j1, j2 = upload(), upload()
    s1 = {p["key"]: p["date_shift_days"] for p in j1["patients"]}
    s2 = {p["key"]: p["date_shift_days"] for p in j2["patients"]}
    assert s1 == s2 == {"id:P-001": -5, "id:P-002": -5}


def test_custom_range_applied_to_dates(client, sample_zip_bytes):
    body = _upload_with_range(
        client, sample_zip_bytes, "range-secret-12345",
        date_shift_min="-5", date_shift_max="-5",
    ).json()
    ct = next(f for f in body["files"] if f["name"] == "patient_1/CT0001.dcm")
    dates = {
        c["keyword"]: (c["old"], c["new"])
        for c in ct["changes"] if c["keyword"] in ("StudyDate", "PatientBirthDate")
    }
    assert dates["StudyDate"] == ("20240301", "20240225")
    assert dates["PatientBirthDate"] == ("19800115", "19800110")


def test_range_ignored_when_shift_disabled(client, sample_zip_bytes):
    body = _upload_with_range(
        client, sample_zip_bytes, "range-secret-12345",
        shift_dates="false", date_shift_min="-5", date_shift_max="5",
    ).json()
    assert body["shift_dates"] is False
    assert {p["date_shift_days"] for p in body["patients"]} == {0}
    ct = next(f for f in body["files"] if f["name"] == "patient_1/CT0001.dcm")
    # 关闭平移：DA 不出现在 REPLACE 变更中
    assert not any(
        c["keyword"] == "StudyDate" and c["action"] == "REPLACE"
        for c in ct["changes"]
    )


def test_consistency_summary_echoes_shift_range(client, sample_zip_bytes):
    j1 = _upload_with_range(
        client, sample_zip_bytes, "range-cc-secret-123",
        date_shift_min="-10", date_shift_max="10",
    ).json()
    j2 = _upload_with_range(
        client, sample_zip_bytes, "range-cc-secret-123",
        date_shift_min="-10", date_shift_max="10",
    ).json()
    r = client.post(
        "/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]}
    )
    assert r.status_code == 200, r.text
    for job in r.json()["jobs"]:
        assert job["date_shift_min"] == -10
        assert job["date_shift_max"] == 10


def test_different_ranges_produce_date_shift_mismatch(client, sample_zip_bytes):
    # 同密钥、同患者、不同范围：确定性偏移可能不同 -> date_shift_mismatch
    j1 = _upload_with_range(
        client, sample_zip_bytes, "range-mismatch-secret",
        date_shift_min="0", date_shift_max="0",
    ).json()
    j2 = _upload_with_range(
        client, sample_zip_bytes, "range-mismatch-secret",
        date_shift_min="-10", date_shift_max="-10",
    ).json()
    r = client.post(
        "/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["patient_date_shift_mismatch"] == 2
    assert body["overall_status"] == "inconsistent"


# --------------------------------------------------------------------------- #
# POST /jobs/consistency-check
# --------------------------------------------------------------------------- #
def _upload(client, sample_zip_bytes, secret, *, shift_dates="true"):
    return client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": secret, "shift_dates": shift_dates},
    ).json()


def test_consistency_check_same_secret_same_batch(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "consistency-secret-1")
    j2 = _upload(client, sample_zip_bytes, "consistency-secret-1")

    r = client.post("/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall_status"] == "consistent"
    assert body["secret_fingerprint"] == j1["secret_fingerprint"] == j2["secret_fingerprint"]
    assert {j["id"] for j in body["jobs"]} == {j1["id"], j2["id"]}
    # 两批数据完全相同：患者/UID 全部共同出现且映射一致
    pm = body["patient_mappings"]
    assert pm["checked_keys"] == 2
    assert pm["conflict_count"] == 0
    assert pm["only_in_one_job_count"] == 0
    assert pm["shift_dates_consistent"] is True
    for layer in body["uid_mappings"]:
        assert layer["conflict_count"] == 0
        assert layer["new_uid_collision_count"] == 0
        assert layer["only_in_one_job_count"] == 0
        assert layer["checked_old_uids"] > 0
    assert body["summary"]["patient_conflict"] == 0
    assert body["summary"]["uid_conflict"] == 0


def test_consistency_check_overlapping_batches_reports_coverage(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "overlap-secret-123")
    j2 = _upload(client, sample_zip_bytes, "overlap-secret-123")
    # 删除第二个任务里的一名患者及其 UID，模拟两个批次覆盖范围不同
    victim = next(p for p in j2["patients"] if p["key"] == "id:P-002")
    j2_obj = _registry().get(j2["id"])
    victim_studies = {
        v["new_study_instance_uid"]
        for v in j2_obj.result.studies.values() if v["patient_key"] == victim["key"]
    }
    j2_obj.result.patients.pop(victim["key"], None)
    j2_obj.result.studies = {
        k: v for k, v in j2_obj.result.studies.items()
        if v["patient_key"] != victim["key"]
    }
    j2_obj.result.series = {
        k: v for k, v in j2_obj.result.series.items()
        if v["new_study_instance_uid"] not in victim_studies
    }
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:4])

    r = client.post("/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    # 覆盖范围差异不构成冲突
    assert body["overall_status"] == "consistent"
    pm = body["patient_mappings"]
    diff = next(d for d in pm["differences"] if d["patient_key"] == "id:P-002")
    assert diff["difference_type"] == "only_in_one_job"
    assert diff["present_in"] == [j1["id"]]
    assert diff["missing_in"] == [j2["id"]]
    assert pm["only_in_one_job_count"] == 1
    assert body["summary"]["patient_only_in_one_job"] == 1
    assert body["summary"]["uid_only_in_one_job"] > 0


def test_consistency_check_different_secret_409(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "consistency-secret-A")
    j2 = _upload(client, sample_zip_bytes, "consistency-secret-B")

    r = client.post("/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SECRET_FINGERPRINT_MISMATCH"
    assert set(detail["fingerprints"]) == {j1["id"], j2["id"]}
    assert len(set(detail["fingerprints"].values())) == 2


def test_consistency_check_missing_job_404(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "consistency-secret-1")
    r = client.post(
        "/jobs/consistency-check", json={"job_ids": [j1["id"], "does-not-exist"]}
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["missing_job_ids"] == ["does-not-exist"]


def test_consistency_check_failed_job_409(client):
    # 构造一个 failed 任务
    failed = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": "consistency-secret-1"},
    ).json()
    # 另一个 ready 任务通过篡改 failed 任务的前一步不易构造，直接手工在注册表中造一个
    jid = failed["id"]
    r = client.post("/jobs/consistency-check", json={"job_ids": [jid, jid]})
    # 重复 ID 先被 422 拦截
    assert r.status_code == 422

    # failed + 不存在的组合：不存在优先返回 404
    r2 = client.post(
        "/jobs/consistency-check", json={"job_ids": [jid, "nope"]}
    )
    assert r2.status_code == 404

    # 需要两个 failed 任务触发 not-ready 分支
    failed2 = client.post(
        "/jobs",
        files={"file": ("broken2.zip", b"PK\x03\x04 garbage2", "application/zip")},
        data={"secret": "consistency-secret-1"},
    ).json()
    r3 = client.post(
        "/jobs/consistency-check", json={"job_ids": [jid, failed2["id"]]}
    )
    assert r3.status_code == 409
    detail = r3.json()["detail"]
    assert detail["code"] == "JOBS_NOT_COMPLETED"
    assert {j["id"] for j in detail["not_ready_jobs"]} == {jid, failed2["id"]}
    assert all(j["status"] == "failed" for j in detail["not_ready_jobs"])


def test_consistency_check_validation_cardinality(client, sample_zip_bytes):
    j = _upload(client, sample_zip_bytes, "consistency-secret-1")

    assert client.post("/jobs/consistency-check", json={"job_ids": [j["id"]]}).status_code == 422
    assert client.post(
        "/jobs/consistency-check",
        json={"job_ids": [j["id"]] * 6},
    ).status_code == 422
    assert client.post("/jobs/consistency-check", json={"job_ids": []}).status_code == 422
    assert client.post("/jobs/consistency-check", json={}).status_code == 422
    # 重复 ID
    assert client.post(
        "/jobs/consistency-check", json={"job_ids": [j["id"], j["id"]]}
    ).status_code == 422
    # 空白 ID
    assert client.post(
        "/jobs/consistency-check", json={"job_ids": [j["id"], "   "]}
    ).status_code == 422


def test_consistency_check_accepts_up_to_five_jobs(client, sample_zip_bytes):
    ids = [_upload(client, sample_zip_bytes, "five-jobs-secret-12")["id"] for _ in range(5)]
    r = client.post("/jobs/consistency-check", json={"job_ids": ids})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["jobs"]) == 5
    assert body["overall_status"] == "consistent"


def _registry():
    # 测试夹具 reload 过 app.jobs，必须经由 app.main 拿到同一份 registry
    from app.main import registry
    return registry


# --------------------------------------------------------------------------- #
# POST /jobs/consistency-check 差异明细分页（level / limit / cursor）
# --------------------------------------------------------------------------- #
def _cc(client, job_ids, **extra):
    return client.post(
        "/jobs/consistency-check", json={"job_ids": job_ids, **extra}
    )


def test_consistency_check_without_level_unchanged(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    body = _cc(client, [j1["id"], j2["id"]]).json()
    # 不传 level：响应中不出现任何分页键（向后兼容）
    assert "items" not in body
    assert "next_cursor" not in body
    assert "has_more" not in body
    # 传入 cursor 但不传 level 时也不分页（cursor 被忽略，等同旧行为）
    body2 = _cc(client, [j1["id"], j2["id"]], cursor="unused").json()
    assert "items" not in body2


def test_consistency_check_level_patient_first_page(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    r = _cc(client, [j1["id"], j2["id"]], level="patient", limit=1)
    assert r.status_code == 200, r.text
    body = r.json()
    # 完整计数与 overall_status 仍然给出
    assert body["overall_status"] == "consistent"
    assert body["patient_mappings"]["checked_keys"] == 2
    assert "summary" in body
    # 两批完全相同：患者层无差异
    assert body["items"] == []
    assert body["has_more"] is False
    assert body["next_cursor"] is None

    # 删除 j2 的一名患者后，患者层出现 1 条覆盖差异
    j2_obj = _registry().get(j2["id"])
    j2_obj.result.patients.pop("id:P-002", None)
    body = _cc(client, [j1["id"], j2["id"]], level="patient", limit=10).json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["level"] == "patient"
    assert item["patient_key"] == "id:P-002"
    assert item["difference_type"] == "only_in_one_job"
    assert item["present_in"] == [j1["id"]]
    assert item["missing_in"] == [j2["id"]]
    assert body["has_more"] is False
    # 完整计数仍然给出
    assert body["patient_mappings"]["only_in_one_job_count"] == 1


def test_consistency_check_pagination_walks_all_pages(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    # 让 j2 只保留 4 个 SOP（见 overlapping 用例的做法），制造覆盖差异
    j2_obj = _registry().get(j2["id"])
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:1])

    cursor = None
    keys: list[tuple] = []
    pages = 0
    while True:
        payload = {"job_ids": [j1["id"], j2["id"]], "level": "sop", "limit": 2}
        if cursor:
            payload["cursor"] = cursor
        body = client.post(
            "/jobs/consistency-check", json=payload
        ).json()
        pages += 1
        for item in body["items"]:
            keys.append((item["difference_type"],
                         item.get("old_uid") or item.get("new_uid")))
        # 完整计数不随分页变化
        assert body["uid_mappings"][2]["level"] == "sop"
        assert body["summary"]["uid_only_in_one_job"] >= 3
        if not body["has_more"]:
            assert body["next_cursor"] is None
            break
        cursor = body["next_cursor"]
        assert cursor and cursor != "null"
        assert pages < 20

    # 顺序符合固定业务键（本例无冲突/碰撞，全部为覆盖差异，按 old_uid 升序）
    assert all(t == "only_in_one_job" for t, _ in keys)
    assert keys == sorted(keys, key=lambda k: k[1])
    assert pages >= 2


def test_consistency_check_default_limit_50(client, sample_zip_bytes, monkeypatch):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2_obj = _registry().get(j2["id"])
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:1])
    # 不传 limit 也应返回分页结构（默认页长 50，差异不足一页）
    body = _cc(client, [j1["id"], j2["id"]], level="series").json()
    assert body["has_more"] is False
    assert "items" in body
    assert "next_cursor" in body


def test_consistency_check_limit_bounds_422(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    assert _cc(client, [j1["id"], j2["id"]], level="sop", limit=0).status_code == 422
    assert _cc(client, [j1["id"], j2["id"]], level="sop", limit=201).status_code == 422
    assert _cc(client, [j1["id"], j2["id"]], level="sop",
               limit="abc").status_code == 422
    assert _cc(client, [j1["id"], j2["id"]], level="modality").status_code == 422


def test_consistency_check_limit_respected(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2_obj = _registry().get(j2["id"])
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:1])
    body = _cc(client, [j1["id"], j2["id"]], level="sop", limit=1).json()
    assert len(body["items"]) == 1
    assert body["has_more"] is True
    assert body["next_cursor"]


def test_consistency_check_cursor_tampered_400(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2_obj = _registry().get(j2["id"])
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:1])
    body = _cc(client, [j1["id"], j2["id"]], level="sop", limit=1).json()
    cursor = body["next_cursor"]

    for bad in (cursor[:-3] + "xxx", "garbage", "a.b", "....", cursor.upper()):
        r = _cc(client, [j1["id"], j2["id"]], level="sop", limit=1, cursor=bad)
        assert r.status_code == 400, bad
        assert r.json()["detail"]["code"] == "INVALID_CURSOR"


def test_consistency_check_cursor_cross_scope_400(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j3 = _upload(client, sample_zip_bytes, "page-secret-12345")
    for job in (j2, j3):
        obj = _registry().get(job["id"])
        obj.result.sops = dict(list(obj.result.sops.items())[:1])

    cursor = _cc(client, [j1["id"], j2["id"]], level="sop", limit=1).json()["next_cursor"]

    # 跨任务集合复用
    r = _cc(client, [j1["id"], j3["id"]], level="sop", limit=1, cursor=cursor)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "CURSOR_SCOPE_MISMATCH"

    # 跨 level 复用
    r = _cc(client, [j1["id"], j2["id"]], level="study", limit=1, cursor=cursor)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "CURSOR_SCOPE_MISMATCH"

    # 任务集合相同但请求顺序不同：属于同一集合，允许复用
    r = _cc(client, [j2["id"], j1["id"]], level="sop", limit=1, cursor=cursor)
    assert r.status_code == 200, r.text


def test_consistency_check_cursor_expired_400(client, sample_zip_bytes, monkeypatch):
    from app.main import pagination

    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2_obj = _registry().get(j2["id"])
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:1])

    real_time = pagination.time.time
    # 签发时间固定在“过去”
    monkeypatch.setattr(pagination.time, "time", lambda: real_time() - 10000)
    cursor = _cc(client, [j1["id"], j2["id"]], level="sop", limit=1).json()["next_cursor"]
    monkeypatch.undo()
    r = _cc(client, [j1["id"], j2["id"]], level="sop", limit=1, cursor=cursor)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "CURSOR_EXPIRED"


def test_consistency_check_cursor_without_level_ignored(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    # 未请求分页时即便携带非法 cursor 也不报错（cursor 不参与）
    r = _cc(client, [j1["id"], j2["id"]], cursor="garbage")
    assert r.status_code == 200
    assert "items" not in r.json()


def test_consistency_check_cursor_job_errors_take_precedence(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2_obj = _registry().get(j2["id"])
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:1])
    cursor = _cc(client, [j1["id"], j2["id"]], level="sop", limit=1).json()["next_cursor"]
    # 任务不存在优先 404，密钥不一致优先 409，均早于游标校验
    r = _cc(client, [j1["id"], "missing"], level="sop", cursor=cursor)
    assert r.status_code == 404

    j_other = _upload(client, sample_zip_bytes, "page-secret-DIFFERENT")
    r = _cc(client, [j1["id"], j_other["id"]], level="sop", cursor=cursor)
    assert r.status_code == 409


def _registry():
    # 测试夹具 reload 过 app.jobs，必须经由 app.main 拿到同一份 registry
    from app.main import registry
    return registry
