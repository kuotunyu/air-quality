"""The two public READMEs must describe the same measured release boundary."""

from __future__ import annotations

from twair.paths import REPO_ROOT


def _readmes() -> tuple[str, str]:
    return (
        (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        (REPO_ROOT / "README.en.md").read_text(encoding="utf-8"),
    )


def test_both_readmes_distinguish_the_delivered_gee_stage_from_deferred_weather_sources() -> None:
    zh, en = _readmes()

    assert "GEE 衛星 Stage A 已交付；CWA／ERA5 延後" in zh
    assert "GEE satellite Stage A delivered; CWA/ERA5 deferred" in en
    assert "CWA/ERA5 deferred; satellite bounded Stage B diagnostic" in en
    assert "Deferred; absent from current results" not in en


def test_both_readmes_keep_m8_provisional_and_defer_calibration_and_fusion() -> None:
    zh, en = _readmes()

    assert "S5P 與 MAIAC 來源取得 Stage A 已交付" in zh
    assert "S5P and MAIAC source-acquisition Stage A delivered" in en
    assert "2025 provisional M8 關聯診斷已交付" in zh
    assert "2025 provisional M8 association diagnostic delivered" in en
    assert "校正與融合仍延後" in zh
    assert "calibration and fusion remain deferred" in en
    assert "不是因果、不是校正，也不是衛星推估 PM2.5" in zh
    assert "not causal, not calibration, and not satellite-estimated PM2.5" in en
    assert "分析與融合仍延後" not in zh
    assert "analysis and fusion remain deferred" not in en
    assert "batch acquisition and calibration remain" not in en
    assert "analysis, MAIAC, and fusion remain deferred" not in en


def test_both_readmes_keep_hugging_face_to_l0_l1_and_owner_confirmed_publication() -> None:
    zh, en = _readmes()

    assert "L0／L1 Dataset bundle 可本機重建，遠端上架另行人工確認" in zh
    assert (
        "L0/L1 Dataset bundle is locally reproducible; remote publication needs owner confirmation"
        in en
    )
    assert "full HF Dataset publishes at closeout" not in en
