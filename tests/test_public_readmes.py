"""The two public READMEs must describe the same measured release boundary."""

from __future__ import annotations

from pathlib import Path

from twair.paths import REPO_ROOT


def _readmes() -> tuple[str, str]:
    return (
        (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        (REPO_ROOT / "README.en.md").read_text(encoding="utf-8"),
    )


def test_public_contract_tests_do_not_require_deleted_internal_docs() -> None:
    deleted_name = "PLAN" + ".md"

    assert deleted_name not in Path(__file__).read_text(encoding="utf-8")


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


def test_both_readmes_keep_population_weighted_exposure_outside_the_release_boundary() -> None:
    zh, en = _readmes()

    assert "HYSPLIT／1 km 場／人口加權暴露延後（repo 無人口網格）" in zh
    assert (
        "HYSPLIT, a 1 km field, and population-weighted exposure deferred "
        "(no population grid in repo)"
    ) in en


def test_public_docs_record_measured_era5_value_without_reframing_the_published_m4() -> None:
    zh, en = _readmes()
    sources = (REPO_ROOT / "docs" / "data-sources.md").read_text(encoding="utf-8")

    for text in (zh, sources):
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
    sources = (REPO_ROOT / "docs" / "data-sources.md").read_text(encoding="utf-8")
    zh_prose = " ".join(zh.split())
    en_prose = " ".join(en.split())
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
    assert "77 站 immutable generation 已完成" in sources
    assert "851（92.1%）" in sources
    assert "919（99.5%）" in sources
    assert "920（99.6%）" in sources
    for text in (zh, sources):
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
    assert "S5P 查詢同樣尚未執行" not in sources
    for text in (zh, en, sources):
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


def test_public_docs_distinguish_measured_micro_sensor_prediction_and_annual_readiness_from_calibration_and_fusion() -> (
    None
):
    zh, en = _readmes()
    sources = (REPO_ROOT / "docs" / "data-sources.md").read_text(encoding="utf-8")
    methodology = (REPO_ROOT / "docs" / "methodology.md").read_text(encoding="utf-8")
    zh_prose = " ".join(zh.split())
    en_prose = " ".join(en.split())
    sources_prose = " ".join(sources.split())
    methodology_prose = " ".join(methodology.split())

    assert "微型感測器 2025-01 觀測、readiness 與 grouped predictive benchmark 已交付" in zh_prose
    assert (
        "January 2025 micro-sensor observations, readiness, and grouped predictive benchmark delivered"
        in en_prose
    )
    assert "validated calibration 與融合仍延後" in zh_prose
    assert "validated calibration and fusion remain deferred" in en_prose

    assert "10,999 筆站點清冊" in sources_prose
    assert "75／93 個預期日別變數檔案存在" in sources_prose
    assert "18 個缺席" in sources_prose
    assert "不能解讀為感測器回報完整率" in sources_prose

    for text in (sources_prose, methodology_prose):
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
        (zh_prose, en_prose, sources_prose, methodology_prose)
    )

    assert "微型感測器 2025 全年 readiness audit 已交付" in zh_prose
    assert "2025 annual micro-sensor readiness audit delivered" in en_prose

    # The annual agreement ran on 2026-08-17 and only 5 of its 29 folds are
    # scorable. A public claim of "delivered" that omits that denominator would
    # be the exact overstatement this test exists to prevent, so both READMEs
    # have to carry the fold accounting beside the delivery.
    assert "Q4-supported cross-station agreement" in zh_prose
    assert "29 個 fold 中只有 5 個可評分" in zh_prose
    assert "held-quarter 與 joint station-quarter 不可估計" in zh_prose
    assert "5 of 29 folds scorable" in en_prose
    assert "held-quarter and joint station-quarter are not estimable" in en_prose

    annual_details = (sources_prose, methodology_prose)
    for text in annual_details:
        assert "c74ec40428a907e98821efbaf36c36386d2c1b99de69791b49f157eb7947e5bb" in text
        assert "365 日日曆" in text
        assert "322 個已解析日期" in text
        assert "43 個來源目錄缺席日期" in text
        assert "2,775,609 筆 device-day" in text
        assert "11,556 個裝置" in text
        assert "1,708 個裝置通過空間篩選" in text
        assert "1,343 個裝置符合寬鬆 eligibility 門檻" in text
        assert "3 個 active months" in text
        assert "30 個 trio dates" in text
        assert "360 個 trio-observed hours" in text
        assert "距最近標準站不超過 10 km" in text
        assert "不是 calibration、不是 bias estimation、不是 sensor fusion" in text
        assert "沒有取得衛星資料，也沒有補值" in text
        assert "最近標準站不是微型感測器位置的 colocated ground truth" in text
        assert "沒有建立高解析度 PM2.5 場" in text

    for text in (sources, methodology):
        assert "| invalid or null coordinate | 6,049 |" in text
        assert "| moving coordinate | 3,794 |" in text
        assert "| outside Taiwan | 4 |" in text
        assert "| missing PM2.5 coordinate | 1 |" in text

    assert "四種去向的裝置數都保留在 exclusion ledger，沒有靜默移除" in sources_prose
    assert "座標沒有被平均、修復或移到最近標準站" in methodology_prose
    assert "flag 不是 valid 或 PM2.5 為 null" in methodology_prose


def test_public_docs_publish_the_verified_satellite_context_limit_without_calling_it_fusion() -> (
    None
):
    zh, en = _readmes()
    sources = (REPO_ROOT / "docs" / "data-sources.md").read_text(encoding="utf-8")
    methodology = (REPO_ROOT / "docs" / "methodology.md").read_text(encoding="utf-8")
    zh_prose = " ".join(zh.split())
    en_prose = " ".join(en.split())
    detailed = tuple(" ".join(text.split()) for text in (sources, methodology))

    assert "一月 reference-station satellite-context predictive-value limit 已交付" in zh_prose
    assert (
        "January 2025 reference-station satellite-context predictive-value limit delivered"
        in en_prose
    )
    assert "validated calibration 與融合仍延後" in zh_prose
    assert "validated calibration and fusion remain deferred" in en_prose

    for text in detailed:
        assert "twair analyze micro-sensor-satellite-value" in text
        assert "a308372bbbb02ea49362b732579649d498c98831f3ec9a4f7cc07bba1f8ff974" in text
        assert "269,952 筆共同 cohort device-hour" in text
        assert "1,186 筆排除列" in text
        assert "468 個裝置" in text
        assert "58 個標準站" in text
        assert "25 個 held-date fold" in text
        assert "10 個 air-zone-aware held-station fold" in text
        assert "140 次 fit" in text
        assert "539,904 筆 prediction" in text
        assert "+0.320 µg/m³（3／10 fold 改善）" in text
        assert "+0.127 µg/m³（3／10 fold 改善）" in text
        assert "每月標準站 satellite context" in text
        assert "不是微型感測器位置的衛星觀測值" in text
        assert "不是 sensor fusion" in text
        assert "held-station 是主要證據" in text
