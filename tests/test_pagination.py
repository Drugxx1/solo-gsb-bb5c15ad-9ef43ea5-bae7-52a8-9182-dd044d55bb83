"""差异明细分页与不透明游标的单元测试。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.anonymizer import JobResult, PatientInfo
from app import consistency, pagination
from app.schemas import ConsistencyCheckOut
from app.pagination import CursorCodec, CursorError


def _make_job(
    job_id: str,
    *,
    fingerprint: str = "fp0000000001",
    shift_dates: bool = True,
    patients: dict | None = None,
    studies: dict | None = None,
    series: dict | None = None,
    sops: dict | None = None,
    files: list | None = None,
    status: str = "ready",
) -> SimpleNamespace:
    result = JobResult(
        files=files or [],
        patients=patients or {},
        studies=studies or {},
        series=series or {},
        sops=sops or {},
        output_dir="/tmp/unused",
    )
    return SimpleNamespace(
        id=job_id,
        status=status,
        source_name=f"{job_id}.zip",
        secret_fingerprint=fingerprint,
        shift_dates=shift_dates,
        created_at=1000.0,
        expires_at=1000.0 + 3600,
        ttl=3600,
        result=result,
        error=None,
    )


def _patient(pid: str, name: str, shift: int) -> PatientInfo:
    return PatientInfo(
        key=f"id:{pid}", new_patient_id=pid, new_patient_name=name,
        date_shift_days=shift, file_count=1,
    )


# --------------------------------------------------------------------------- #
# CursorCodec
# --------------------------------------------------------------------------- #
def test_cursor_roundtrip_and_opaque_payload():
    codec = CursorCodec(ttl_seconds=60)
    token = codec.encode(job_ids=["j2", "j1"], level="sop", after=[2, "1.2.3"])
    # 不透明：不应直接暴露 job id / level / 排序键
    assert "j1" not in token and "j2" not in token and "sop" not in token
    after = codec.decode(token, job_ids=["j1", "j2"], level="sop")
    assert after == [2, "1.2.3"]


def test_cursor_empty_is_first_page():
    codec = CursorCodec(ttl_seconds=60)
    assert codec.decode("", job_ids=["j1"], level="study") is None
    assert codec.decode("   ", job_ids=["j1"], level="study") is None


def test_cursor_scope_is_order_independent():
    codec = CursorCodec(ttl_seconds=60)
    token = codec.encode(job_ids=["j1", "j2"], level="study", after=None)
    # 任务集合是集合语义：请求顺序不同仍可复用
    assert codec.decode(token, job_ids=["j2", "j1"], level="study") is None


def test_cursor_tampered_rejected():
    codec = CursorCodec(ttl_seconds=60)
    token = codec.encode(job_ids=["j1", "j2"], level="study", after=[1, "x"])
    with pytest.raises(CursorError) as exc:
        codec.decode(token[:-2] + "AA", job_ids=["j1", "j2"], level="study")
    assert exc.value.code == "INVALID_CURSOR"
    with pytest.raises(CursorError):
        codec.decode("not-a-cursor", job_ids=["j1", "j2"], level="study")
    with pytest.raises(CursorError):
        codec.decode("aaa.bbb", job_ids=["j1", "j2"], level="study")


def test_cursor_expired_rejected():
    codec = CursorCodec(ttl_seconds=10)
    token = codec.encode(
        job_ids=["j1", "j2"], level="study", after=None, now=1000.0
    )
    # 边界内仍有效
    codec.decode(token, job_ids=["j1", "j2"], level="study", now=1010.0)
    with pytest.raises(CursorError) as exc:
        codec.decode(token, job_ids=["j1", "j2"], level="study", now=1010.0 + 1)
    assert exc.value.code == "CURSOR_EXPIRED"


def test_cursor_cross_job_set_rejected():
    codec = CursorCodec(ttl_seconds=60)
    token = codec.encode(job_ids=["j1", "j2"], level="study", after=[0, "u"])
    with pytest.raises(CursorError) as exc:
        codec.decode(token, job_ids=["j1", "j3"], level="study")
    assert exc.value.code == "CURSOR_SCOPE_MISMATCH"
    with pytest.raises(CursorError) as exc:
        codec.decode(token, job_ids=["j1", "j2", "j3"], level="study")
    assert exc.value.code == "CURSOR_SCOPE_MISMATCH"


def test_cursor_cross_level_rejected():
    codec = CursorCodec(ttl_seconds=60)
    token = codec.encode(job_ids=["j1", "j2"], level="study", after=[0, "u"])
    with pytest.raises(CursorError) as exc:
        codec.decode(token, job_ids=["j1", "j2"], level="series")
    assert exc.value.code == "CURSOR_SCOPE_MISMATCH"


# --------------------------------------------------------------------------- #
# page_differences：固定业务键排序 + keyset 分页
# --------------------------------------------------------------------------- #
def _report_with_uid_diffs():
    # conflict(1.1.1) + 2 collision + 3 coverage，覆盖各排序段
    j1 = _make_job("j1", sops={
        "1.1.1": "2.25.A",            # 与 j2 冲突
        "1.1.3": "2.25.same1",        # 碰撞 1
        "1.1.5": "2.25.same2",        # 碰撞 2
        "1.1.7": "2.25.7",            # j1 独有
        "1.1.8": "2.25.8",            # j1 独有
        "1.1.9": "2.25.9",            # j1 独有
    })
    j2 = _make_job("j2", sops={
        "1.1.1": "2.25.B",
        "1.1.4": "2.25.same1",
        "1.1.6": "2.25.same2",
    })
    return consistency.build_full_report([j1, j2]), ["j1", "j2"]


def test_page_differences_stable_order_and_keyset():
    report, job_ids = _report_with_uid_diffs()
    seen: list[tuple] = []
    after = None
    pages = 0
    while True:
        page = consistency.page_differences(
            report, job_ids=job_ids, level="sop", limit=2, after=after
        )
        pages += 1
        keys = [(d["difference_type"], d.get("old_uid") or d.get("new_uid"))
                for d in page["items"]]
        seen.extend(keys)
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"]
        after = pagination.codec.decode(
            page["next_cursor"], job_ids=job_ids, level="sop"
        )
        assert after is not None
        assert pages < 20

    # 固定业务键：conflict -> uid_new_collision(new_uid 排序) -> only_in_one_job(old_uid 排序)
    types = [t for t, _ in seen]
    assert types == (
        ["conflict"]
        + ["uid_new_collision"] * 2
        + ["only_in_one_job"] * 7
    )
    assert pages == 5  # ceil(10 / 2)
    # 总条目数：1 conflict + 2 collision + 7 coverage
    # （碰撞来源 UID 1.1.3/1.1.4/1.1.5/1.1.6 各自也只出现在一个任务，同样算覆盖差异）
    assert len(seen) == 10
    # 末页恰好放完时 has_more 为 False
    assert seen[0] == ("conflict", "1.1.1")
    assert seen[1][0] == "uid_new_collision"


def test_page_limit_larger_than_total_has_no_more():
    report, job_ids = _report_with_uid_diffs()
    page = consistency.page_differences(
        report, job_ids=job_ids, level="sop", limit=200, after=None
    )
    assert page["has_more"] is False
    assert page["next_cursor"] is None
    assert len(page["items"]) == 10


def test_page_empty_level():
    j1 = _make_job("j1")
    j2 = _make_job("j2")
    report = consistency.build_full_report([j1, j2])
    page = consistency.page_differences(
        report, job_ids=["j1", "j2"], level="series", limit=10, after=None
    )
    assert page == {"items": [], "next_cursor": None, "has_more": False}


def test_patient_page_order_conflict_before_coverage():
    j1 = _make_job("j1", patients={
        "id:P2": _patient("PID2", "ANON^PID2", 1),
        "id:P1": _patient("PID1", "ANON^PID1", 1),
    })
    j2 = _make_job("j2", patients={
        "id:P2": _patient("PIDX", "ANON^PIDX", 1),  # 冲突
        "id:P1": _patient("PID1", "ANON^PID1", 1),
    })
    report = consistency.build_full_report([j1, j2])
    page = consistency.page_differences(
        report, job_ids=["j1", "j2"], level="patient", limit=50, after=None
    )
    assert [d["difference_type"] for d in page["items"]] == ["conflict"]
    assert page["items"][0]["level"] == "patient"
    assert page["items"][0]["patient_key"] == "id:P2"


def test_paginated_response_keeps_full_counts_and_status():
    # 完整报告中计数不受分页影响
    report, job_ids = _report_with_uid_diffs()
    page = consistency.page_differences(
        report, job_ids=job_ids, level="sop", limit=2, after=None
    )
    out_dict = consistency.report_without_private(report)
    out_dict["items"] = page["items"]
    out_dict["next_cursor"] = page["next_cursor"]
    out_dict["has_more"] = page["has_more"]
    out = ConsistencyCheckOut.model_validate(out_dict)
    assert out.overall_status == "inconsistent"
    sop = next(l for l in out.uid_mappings if l.level == "sop")
    assert sop.conflict_count == 1
    assert sop.new_uid_collision_count == 2
    assert sop.only_in_one_job_count == 7
    assert len(out.items) == 2
    assert out.has_more is True
    assert out.next_cursor
    # 未请求分页时序列化结果不包含分页键（向后兼容）
    legacy = ConsistencyCheckOut.model_validate(
        consistency.report_without_private(report)
    ).model_dump()
    assert "items" not in legacy
    assert "next_cursor" not in legacy
    assert "has_more" not in legacy
