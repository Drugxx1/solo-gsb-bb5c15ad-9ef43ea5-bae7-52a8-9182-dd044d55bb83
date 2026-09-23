"""加密派生的单元测试。"""
import pytest

from app import crypto


def test_pseudonym_is_stable_and_16_chars():
    a = crypto.patient_pseudonyms("secret-12345678", "id:P-001")
    b = crypto.patient_pseudonyms("secret-12345678", "id:P-001")
    assert a == b
    pid, pname = a
    assert len(pid) == 16 and pid.isalnum()
    assert pname.startswith("ANON^")


def test_different_patients_differ():
    a = crypto.patient_pseudonyms("secret-12345678", "id:P-001")
    b = crypto.patient_pseudonyms("secret-12345678", "id:P-002")
    assert a != b


def test_different_secrets_differ():
    a = crypto.patient_pseudonyms("secret-12345678", "id:P-001")
    b = crypto.patient_pseudonyms("secret-99999999", "id:P-001")
    assert a[0] != b[0]


def test_derive_uid_deterministic_and_valid_format():
    original = "1.2.840.113619.2.55.3.604688119.1"
    u1 = crypto.derive_uid("secret-12345678", original)
    u2 = crypto.derive_uid("secret-12345678", original)
    assert u1 == u2
    assert u1.startswith("2.25.")
    assert all(part.isdigit() for part in u1.split("."))
    # DICOM UI 规则：总长 <=64，每个组件 <=64 位
    assert len(u1) <= 64
    assert all(len(part) <= 64 for part in u1.split("."))
    assert u1 != crypto.derive_uid("other-secret-9999", original)


def test_date_shift_per_patient_stable_in_range():
    d1 = crypto.date_shift_days("secret-12345678", "id:P-001")
    d2 = crypto.date_shift_days("secret-12345678", "id:P-001")
    assert d1 == d2
    assert -365 <= d1 <= 365


def test_date_shift_default_range_matches_v1():
    # 默认 -365..365 必须与升级前的固定派生词一致（旧批次可继续核对）
    assert crypto.date_shift_days("secret-12345678", "id:P-001") == 342
    assert crypto.date_shift_days(
        "secret-12345678", "id:P-001", -365, 365
    ) == crypto.date_shift_days("secret-12345678", "id:P-001")


@pytest.mark.parametrize("shift_min,shift_max", [
    (-365, 365), (-30, -30), (0, 0), (-10, 10), (-7, 7), (1, 365), (-365, -1),
])
def test_date_shift_custom_range_stable_and_bounded(shift_min, shift_max):
    seen = {
        crypto.date_shift_days("secret-12345678", f"id:P-{i}", shift_min, shift_max)
        for i in range(300)
    }
    assert all(shift_min <= value <= shift_max for value in seen)
    # 同一患者在“跨批次”（重复调用）且范围相同时结果一致
    for i in range(20):
        assert (
            crypto.date_shift_days("secret-12345678", f"id:P-{i}", shift_min, shift_max)
            == crypto.date_shift_days("secret-12345678", f"id:P-{i}", shift_min, shift_max)
        )


def test_date_shift_range_is_part_of_derivation():
    # 范围不同 -> 派生结果可以不同（区间参与确定性）；范围相同 -> 必相同
    identity = "id:P-001"
    narrow = crypto.date_shift_days("secret-12345678", identity, 0, 10)
    assert 0 <= narrow <= 10
    assert narrow == crypto.date_shift_days("secret-12345678", identity, 0, 10)
    assert crypto.date_shift_days("secret-12345678", identity, -10, 0) in range(-10, 1)


def test_date_shift_zero_width_range():
    assert crypto.date_shift_days("secret-12345678", "id:P-001", -5, -5) == -5
    assert crypto.date_shift_days("secret-12345678", "id:P-001", 0, 0) == 0
