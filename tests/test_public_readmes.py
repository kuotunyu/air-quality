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


def test_public_satellite_docs_record_measured_held_out_and_multiyear_value_without_overclaiming() -> (
    None
):
    zh, en = _readmes()
    plan = (REPO_ROOT / "PLAN.md").read_text(encoding="utf-8")
    sources = (REPO_ROOT / "docs" / "data-sources.md").read_text(encoding="utf-8")
    zh_prose = " ".join(zh.split())
    en_prose = " ".join(en.split())
    plan_prose = " ".join(plan.split())
    sources_prose = " ".join(sources.split())

    assert "S5P 與 MAIAC 來源取得 Stage A 已交付" in zh
    assert "S5P and MAIAC source-acquisition Stage A delivered" in en
    assert "2025 M8 關聯與 held-out predictive-value 診斷已交付" in zh
    assert "2025 M8 association and held-out predictive-value diagnostics delivered" in en
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
    for text in (zh, plan, sources):
        assert "851 筆共同完整站月、76 站、12 個月份" in text
        assert "3／4、9／10、37／40" in text
        assert "49／54" in text
        assert "44／54" in text
        assert "48／54" in text
        assert "25／54" in text
        assert "29／54" in text
        assert "−0.588" in text
        assert "+0.147" in text
        assert "不是 54 個獨立年份或測站" in text
        assert "不是未來年度 transfer" in text
    assert "851 common complete station-months, 76 stations, and 12 months" in en
    assert "3/4, 9/10, and 37/40" in en
    assert "49/54" in en
    assert "44/54" in en
    assert "48/54" in en
    assert "25/54" in en
    assert "29/54" in en
    assert "−0.588" in en
    assert "+0.147" in en
    assert "For the combined all-satellite feature set, the overall median ΔRMSE" in en_prose
    assert "For all satellite features, the overall median" not in en_prose
    assert "not 54 independent years or stations" in en
    assert "not future-year transfer" in en
    assert "MAIAC null 69 筆" in sources_prose
    assert "S5P NO₂ null 1 筆" in sources_prose
    assert "S5P SO₂ null 0 筆" in sources_prose
    assert "地面列缺席 2 筆" in sources_prose
    assert "地面 coverage 不足而 withheld 2 筆" in sources_prose
    assert "held-quarter 有 4 個 fold" in sources
    assert "held-station 有 10 個 fold" in sources
    assert "joint transfer 有 40 個 fold" in sources
    assert "2,553 個 test-row appearances" in sources
    assert "−0.317、−0.375、−0.600" in sources
    assert "+0.188、+0.050、+0.204" in sources
    assert "| AOD | 54 | 44／54 | 10／54 | −0.244 | −0.253 | +0.055 |" in sources
    assert "| NO₂ | 54 | 48／54 | 6／54 | −0.379 | −0.266 | +0.103 |" in sources
    assert "| SO₂ | 54 | 25／54 | 29／54 | +0.020 | +0.015 | −0.003 |" in sources
    assert "| all-satellite | 54 | 49／54 | 5／54 | −0.588 | −0.407 | +0.147 |" in sources
    assert "baseline 只含月份週期與測站經緯度" in sources
    assert "不支持因果、衛星 PM2.5 校正、融合場" in sources
    assert "或 M4 replacement" in sources
    assert "10 個 air-zone-aware held-station fold" in plan
    assert "10 個 air-zone-aware 測站" not in plan
    assert "combined all-satellite feature set 在多數 2025 held-out folds 顯示增量預測資訊" in plan
    assert "實際 batch 尚未送出" not in plan
    assert "S5P 查詢同樣尚未執行" not in sources
    for text in (zh, en, plan, sources):
        assert "848 / 851 common complete station-months" in text
        assert "2024_to_2025" in text
        assert "2025_to_2024" in text
        assert "all/AOD/NO₂/SO₂" in text
        assert "predictive robustness only" in text
        assert "not causal, calibration, fusion, satellite-estimated PM2.5" in text
        assert "not a spatial-resolution claim or an M4 replacement" in text
    assert "所有 baseline／candidate 比較都配對同一批 test rows" in zh_prose
    assert (
        "同年度 quarter replication 在 2024 同時改善 RMSE 與 R² 的 fold 數為 3/4, 4/4, 3/4, 3/4，2025 為 3/4, 3/4, 2/4, 1/4"
        in zh_prose
    )
    assert (
        "真正的 future-year `2024_to_2025` 對四組特徵是 forward: improve / improve / improve / improve，all-satellite 的配對結果為 −0.378 µg/m³ / +0.057 R²"
        in zh_prose
    )
    assert (
        "`2025_to_2024` 是反向複驗，不是預測過去；四組結果是 reverse: improve / improve / worsen / worsen，all-satellite 為 −0.179 µg/m³ / +0.024 R²"
        in zh_prose
    )
    assert (
        "同時留出測站與年份時，`2024_to_2025` 的改善 fold 數為 10/10, 10/10, 9/10, 6/10， `2025_to_2024` 為 10/10, 10/10, 9/10, 7/10"
        in zh_prose
    )
    assert "every baseline/candidate comparison paired identical test rows" in en_prose
    assert (
        "within-year quarter replication improved both RMSE and R² in 3/4, 4/4, 3/4, 3/4 folds in 2024 and 3/4, 3/4, 2/4, 1/4 in 2025"
        in en_prose
    )
    assert (
        "future-year `2024_to_2025` test was forward: improve / improve / improve / improve, with all-satellite at −0.378 µg/m³ / +0.057 R²"
        in en_prose
    )
    assert (
        "`2025_to_2024` is reverse-direction replication, not prediction of the past: reverse: improve / improve / worsen / worsen, with all-satellite at −0.179 µg/m³ / +0.024 R²"
        in en_prose
    )
    assert (
        "station and year were both held out, the forward direction improved in 10/10, 10/10, 9/10, 6/10 folds and the reverse in 10/10, 10/10, 9/10, 7/10"
        in en_prose
    )
    assert "baseline／candidate 均配對相同 test rows" in plan_prose
    assert "同年度 replication 改善數為 3/4, 4/4, 3/4, 3/4 與 3/4, 3/4, 2/4, 1/4" in plan_prose
    assert (
        "future-year `2024_to_2025` 是 forward: improve / improve / improve / improve（all-satellite −0.378 µg/m³ / +0.057 R²）"
        in plan_prose
    )
    assert (
        "`2025_to_2024` 是反向複驗，不是預測過去，為 reverse: improve / improve / worsen / worsen （all-satellite −0.179 µg/m³ / +0.024 R²）"
        in plan_prose
    )
    assert (
        "同時留出測站與年份的改善數分別為 10/10, 10/10, 9/10, 6/10 與 10/10, 10/10, 9/10, 7/10"
        in plan_prose
    )
    assert "每一個 candidate 都與 baseline 配對完全相同的 train/test rows" in sources_prose
    assert (
        "同年度 quarter replication 在 2024 同時改善 RMSE 與 R² 的 fold 數為 3/4, 4/4, 3/4, 3/4，2025 為 3/4, 3/4, 2/4, 1/4"
        in sources_prose
    )
    assert (
        "future-year `2024_to_2025` 為 forward: improve / improve / improve / improve； all-satellite 的配對 ΔRMSE／ΔR² 是 −0.378 µg/m³ / +0.057 R²"
        in sources_prose
    )
    assert (
        "`2025_to_2024` 是反向複驗，不是預測過去，結果為 reverse: improve / improve / worsen / worsen；all-satellite 為 −0.179 µg/m³ / +0.024 R²"
        in sources_prose
    )
    assert (
        "同時留出測站與年份的 10 個 air-zone-aware fold 中，`2024_to_2025` 四組特徵的改善數為 10/10, 10/10, 9/10, 6/10，`2025_to_2024` 為 10/10, 10/10, 9/10, 7/10"
        in sources_prose
    )
    assert "cross-year common-station cohort 的額外排除列與排除站均為 0" in sources_prose
    assert "76／73 筆 incomplete/null station-months 仍保留並計數在 source panels" in sources_prose


def test_both_readmes_keep_hugging_face_to_l0_l1_and_owner_confirmed_publication() -> None:
    zh, en = _readmes()

    assert "L0／L1 Dataset bundle 可本機重建，遠端上架另行人工確認" in zh
    assert (
        "L0/L1 Dataset bundle is locally reproducible; remote publication needs owner confirmation"
        in en
    )
    assert "full HF Dataset publishes at closeout" not in en


def test_public_docs_distinguish_measured_micro_sensor_prediction_from_calibration_and_fusion() -> (
    None
):
    zh, en = _readmes()
    plan = (REPO_ROOT / "PLAN.md").read_text(encoding="utf-8")
    sources = (REPO_ROOT / "docs" / "data-sources.md").read_text(encoding="utf-8")
    methodology = (REPO_ROOT / "docs" / "methodology.md").read_text(encoding="utf-8")
    zh_prose = " ".join(zh.split())
    en_prose = " ".join(en.split())
    plan_prose = " ".join(plan.split())
    sources_prose = " ".join(sources.split())
    methodology_prose = " ".join(methodology.split())

    assert "微型感測器 2025-01 觀測、readiness 與 grouped predictive benchmark 已交付" in zh_prose
    assert (
        "January 2025 micro-sensor observations, readiness, and grouped predictive benchmark delivered"
        in en_prose
    )
    assert "validated calibration 與融合仍延後" in zh_prose
    assert "validated calibration and fusion remain deferred" in en_prose

    for text in (plan_prose, sources_prose):
        assert "10,999 筆站點清冊" in text
        assert "75／93 個預期日別變數檔案存在" in text
        assert "18 個缺席" in text
        assert "不能解讀為感測器回報完整率" in text

    for text in (plan_prose, sources_prose, methodology_prose):
        assert "282,581 筆 primary-radius device-hour" in text
        assert "271,138 筆" in text
        assert "470 個裝置" in text
        assert "60 個標準站" in text
        assert "25 個 held-date fold" in text
        assert "10 個 air-zone-aware held-station fold" in text
        assert "−0.618 µg/m³" in text
        assert "−0.649 µg/m³" in text
        assert "不是 validated calibration" in text
        assert "不是 sensor fusion" in text
        assert "不使用衛星特徵" in text

    assert "twair ingest micro-sensor-catalog --month 202501 --confirm-network" in sources
    assert "twair analyze micro-sensor-readiness" in sources
    assert "twair analyze micro-sensor-benchmark" in sources
    assert "c841ef16d7cc55920b6ab5b7b274c2f8b5e68e754d8cce4e1a5677f997e8e05b" in sources
    assert "1f76ea400995080027701f80c311438fab3e6d823f5665681b9ca79a4aad81fd" in sources
    assert "25cc89fdb57d1e64754edd5c3a7bbb140cad88e5e178137875dafae2103f0cc6" in sources
    assert "https://history.colife.org.tw/" in sources
    assert "https://ci.taiwan.gov.tw/dsp/Views/dataset/air.aspx" in sources
    assert "觀測 ZIP 尚未下載" not in " ".join(
        (zh_prose, en_prose, plan_prose, sources_prose, methodology_prose)
    )
