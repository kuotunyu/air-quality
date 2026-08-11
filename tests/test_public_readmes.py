"""The two public READMEs must describe the same measured release boundary."""

from __future__ import annotations

from twair.paths import REPO_ROOT


def _readmes() -> tuple[str, str]:
    return (
        (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        (REPO_ROOT / "README.en.md").read_text(encoding="utf-8"),
    )


def test_both_readmes_distinguish_delivered_era5_robustness_from_deferred_calibration() -> None:
    zh, en = _readmes()

    assert "ERA5 2024–2025 來源取得與多年度／留出測站 robustness 已交付；CWA 延後" in zh
    assert (
        "ERA5 2024–2025 acquisition and multi-year/held-out-station robustness delivered; "
        "CWA deferred"
    ) in en
    assert "multi-year and held-out-station robustness complete; calibration not delivered" in en
    assert "ERA5 analysis and calibration remain deferred" not in en
    assert "value-add analyses deferred" not in en
    assert "CWA／ERA5 延後" not in zh
    assert "CWA/ERA5 deferred" not in en
    assert "Deferred; absent from current results" not in en


def test_public_docs_record_measured_era5_value_without_reframing_the_published_m4() -> None:
    zh, en = _readmes()
    plan = (REPO_ROOT / "PLAN.md").read_text(encoding="utf-8")
    sources = (REPO_ROOT / "docs" / "data-sources.md").read_text(encoding="utf-8")

    for text in (zh, plan, sources):
        assert "674,520 筆 station-hour" in text
        assert "六個來源變數皆為 0 個 null" in text
        assert "632,760 筆" in text
        assert "205／222" in text
        assert "2024 年 636,244 筆、2025 年 632,760 筆" in text
        assert "63／74、66／74、70／74" in text
        assert "177／222、205／222" in text
        assert "尚未納入已發布的 M4" in text
    assert "674,520 station-hour rows" in en
    assert "zero source nulls across all six variables" in en
    assert "632,760" in en
    assert "205 of 222" in en
    assert "636,244 paired rows in 2024 and 632,760 in 2025" in en
    assert "63/74, 66/74, and 70/74" in en
    assert "177/222 and 205/222" in en
    assert "has not been added to the published M4 model" in en
    assert "not causal attribution, calibration, or fusion" in en
    assert "one-year held-out predictive value" not in en
    assert "676,368 筆" in sources
    assert "57,288 筆" in sources
    assert "1 個未分類 air-zone stratum" in sources


def test_public_satellite_docs_keep_m8_provisional_and_record_the_completed_generation() -> None:
    zh, en = _readmes()
    plan = (REPO_ROOT / "PLAN.md").read_text(encoding="utf-8")
    sources = (REPO_ROOT / "docs" / "data-sources.md").read_text(encoding="utf-8")

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
    assert "77 站 S5P／MAIAC generation 已完成" in plan
    assert "MAIAC 851／924、S5P NO₂ 919／924、S5P SO₂ 920／924" in plan
    assert "77 站 immutable generation 已完成" in sources
    assert "851（92.1%）" in sources
    assert "919（99.5%）" in sources
    assert "920（99.6%）" in sources
    assert "實際 batch 尚未送出" not in plan
    assert "S5P 查詢同樣尚未執行" not in sources


def test_both_readmes_keep_hugging_face_to_l0_l1_and_owner_confirmed_publication() -> None:
    zh, en = _readmes()

    assert "L0／L1 Dataset bundle 可本機重建，遠端上架另行人工確認" in zh
    assert (
        "L0/L1 Dataset bundle is locally reproducible; remote publication needs owner confirmation"
        in en
    )
    assert "full HF Dataset publishes at closeout" not in en
