from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import polars as pl
import pytest
from typer.testing import CliRunner

from twair import cli

ANNUAL_GENERATION = "c74ec40428a907e98821efbaf36c36386d2c1b99de69791b49f157eb7947e5bb"


def _module() -> Any:
    return importlib.import_module("twair.analysis.micro_sensor_annual_agreement")


def _published_result(tmp_path: Path) -> SimpleNamespace:
    directory = tmp_path / ("a" * 64)
    directory.mkdir(parents=True)
    names = (
        "calendar",
        "paired_days",
        "exclusions",
        "fold_membership",
        "folds",
        "predictions",
        "scores",
        "deltas",
    )
    written = {name: directory / f"{name}.parquet" for name in names}
    written.update(
        {
            "summary": directory / "summary.json",
            "manifest": directory / "manifest.json",
        }
    )
    for path in written.values():
        path.write_bytes(b"persisted")
    return SimpleNamespace(
        directory=directory,
        manifest={"complete": True, "generation_sha256": "a" * 64},
        summary={"output_rows": dict.fromkeys(names, 1)},
        written=written,
    )


def test_the_command_defaults_to_a_plan_without_starting_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = _module()
    monkeypatch.setattr(
        agreement,
        "run_and_write_annual_agreement",
        lambda: pytest.fail("plan-only mode started computation"),
    )

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-annual-agreement"])

    assert result.exit_code == 0
    assert "PLAN ONLY" in result.output
    assert ANNUAL_GENERATION in result.output
    assert "1 CPU thread" in result.output
    assert "6 GB" in result.output
    assert "--confirm-compute" in result.output
    assert "network, GEE, GPU: disabled" in result.output


def test_the_confirmed_command_uses_only_the_combined_lock_owning_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    events: list[str] = []
    published = _published_result(tmp_path)

    def combined() -> SimpleNamespace:
        events.append("combined-returned-persisted-result")
        return published

    monkeypatch.setattr(agreement, "run_and_write_annual_agreement", combined)
    monkeypatch.setattr(
        agreement,
        "prepare_annual_agreement_panel",
        lambda *_args, **_kwargs: pytest.fail("CLI called split preparation"),
    )
    monkeypatch.setattr(
        agreement,
        "write_annual_agreement_panel",
        lambda *_args, **_kwargs: pytest.fail("CLI called split publication"),
    )
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "micro-sensor-annual-agreement", "--confirm-compute"],
    )

    assert result.exit_code == 0
    assert events[0] == "combined-returned-persisted-result"
    assert set(events[1:]) == {"print"}


def test_a_failed_combined_run_prints_no_result_or_output_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = _module()
    printed: list[str] = []
    monkeypatch.setattr(
        agreement,
        "run_and_write_annual_agreement",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic publication failed")),
    )
    monkeypatch.setattr(
        cli.console, "print", lambda value, *_args, **_kwargs: printed.append(str(value))
    )

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "micro-sensor-annual-agreement", "--confirm-compute"],
    )

    assert result.exit_code != 0
    assert printed == []


def test_the_combined_entry_holds_one_run_lock_across_preparation_and_final_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    observed: list[str] = []

    def run_locked(*, plan: Any, config: Any) -> SimpleNamespace:
        del config
        with (
            pytest.raises(RuntimeError, match="another annual agreement run is active"),
            agreement._annual_agreement_run_lock(plan.lock_path),
        ):
            pass
        observed.extend(["inputs", "checkpoints", "panel", "model", "publication", "reload"])
        return SimpleNamespace(directory=plan.output_root, manifest={}, summary={}, written={})

    monkeypatch.setattr(agreement, "_run_annual_agreement_locked", run_locked)

    agreement.run_and_write_annual_agreement()

    assert observed == ["inputs", "checkpoints", "panel", "model", "publication", "reload"]
    with agreement._annual_agreement_run_lock(agreement.annual_agreement_run_plan().lock_path):
        pass


def test_the_locked_body_runs_the_reviewed_boundaries_in_exact_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    plan = agreement.annual_agreement_run_plan()
    config = agreement.load_annual_agreement_config()
    annual = SimpleNamespace(generation_dir=plan.annual_generation_dir)
    checkpoints = (SimpleNamespace(directory=plan.checkpoint_root / "2025-01-01"),)
    prepared_panel = SimpleNamespace(manifest={"analysis": "panel"})
    panel = SimpleNamespace(manifest={"generation_sha256": "b" * 64})
    evaluation = SimpleNamespace(manifest={"generation_sha256": "c" * 64})
    final = SimpleNamespace(directory=plan.output_root / ("d" * 64))
    events: list[str] = []

    def load_input(path: Path) -> Any:
        events.append(f"load-input:{path}")
        return annual

    def prepare_checkpoints(loaded: Any, *, plan: Any, config: Any) -> Any:
        del loaded, plan, config
        events.append("checkpoints")
        return checkpoints

    def prepare_panel(loaded: Any, observed: Any, selected: Any) -> Any:
        del loaded, observed, selected
        events.append("panel")
        return prepared_panel

    def materialize(prepared: Any, *, destination: Path) -> Any:
        assert prepared is prepared_panel
        assert destination == plan.panel_destination
        events.append("panel-stage-reload")
        return panel

    def evaluate(prepared: Any) -> Any:
        del prepared
        events.append("evaluation")
        return evaluation

    def publish(prepared: Any, measured: Any, *, output_root: Path) -> Any:
        del prepared, measured, output_root
        events.append("publish-reload")
        return final

    monkeypatch.setattr(agreement, "load_annual_readiness_input", load_input)
    monkeypatch.setattr(agreement, "_prepare_annual_agreement_checkpoints", prepare_checkpoints)
    monkeypatch.setattr(agreement, "_prepare_annual_agreement_panel", prepare_panel)
    monkeypatch.setattr(agreement, "_load_or_write_annual_agreement_panel", materialize)
    monkeypatch.setattr(agreement, "evaluate_annual_agreement", evaluate)
    monkeypatch.setattr(agreement, "_publish_annual_agreement_result", publish)

    returned = agreement._run_annual_agreement_locked(plan=plan, config=config)

    assert returned is final
    assert events == [
        f"load-input:{plan.annual_generation_dir}",
        "checkpoints",
        "panel",
        "panel-stage-reload",
        "evaluation",
        "publish-reload",
    ]


@dataclass(frozen=True)
class _FrameBundle:
    calendar: pl.DataFrame
    paired_days: pl.DataFrame
    exclusions: pl.DataFrame
    memberships: pl.DataFrame
    folds: pl.DataFrame
    predictions: pl.DataFrame
    scores: pl.DataFrame
    deltas: pl.DataFrame
    summary: dict[str, object]
    manifest: dict[str, object]


def _final_bundle() -> tuple[_FrameBundle, SimpleNamespace]:
    agreement = _module()
    config = agreement.load_annual_agreement_config()
    config_payload = json.loads(json.dumps(asdict(config), allow_nan=False))
    claim_boundary = dict(config.claim_boundary)
    checkpoint_inventory = [{"date": "2025-01-01"}]
    panel_summary: dict[str, object] = {"panel": "synthetic"}
    panel_identity = {
        "schema_version": 1,
        "analysis": "annual_reference_station_agreement_panel",
        "inputs": {"annual_generation_sha256": ANNUAL_GENERATION},
        "checkpoint_inventory": checkpoint_inventory,
        "config": config_payload,
        "claim_boundary": claim_boundary,
        "output_rows": {},
        "members": {},
        "summary_file": {},
        "summary_sha256": agreement._canonical_hash(panel_summary),
    }
    panel_manifest = {
        **panel_identity,
        "complete": True,
        "generation_sha256": agreement._canonical_hash(panel_identity),
    }
    panel = _FrameBundle(
        calendar=pl.DataFrame({"date": [date(2025, 1, 1)]}),
        paired_days=pl.DataFrame(
            {
                "radius_km": [0.5],
                "date": [date(2025, 1, 1)],
                "device_id": ["d"],
                "station_name": ["s"],
                "quarter": [1],
                "reason": ["catalogue_absent"],
            }
        ),
        exclusions=pl.DataFrame({"reason": ["catalogue_absent"]}),
        memberships=pl.DataFrame(),
        folds=pl.DataFrame(),
        predictions=pl.DataFrame(),
        scores=pl.DataFrame(),
        deltas=pl.DataFrame(),
        summary=panel_summary,
        manifest=panel_manifest,
    )
    evaluation = SimpleNamespace(
        memberships=pl.DataFrame({"fold": ["held"]}),
        folds=pl.DataFrame({"fold": ["held"]}),
        predictions=pl.DataFrame({"model": ["raw_micro"]}),
        scores=pl.DataFrame({"scope": ["overall"]}),
        deltas=pl.DataFrame({"model": ["pooled_micro_ridge"]}),
        manifest={
            "generation_sha256": "c" * 64,
            "panel_generation_sha256": panel_manifest["generation_sha256"],
            "config": config_payload,
            "claim_boundary": claim_boundary,
        },
    )
    return panel, evaluation


def test_final_publication_writes_all_reviewed_members_and_reloads_the_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)

    result = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=tmp_path / "generations",
    )

    assert result.manifest["complete"] is True
    assert result.directory.name == result.manifest["generation_sha256"]
    assert set(result.written) == {
        "calendar",
        "paired_days",
        "exclusions",
        "fold_membership",
        "folds",
        "predictions",
        "scores",
        "deltas",
        "summary",
        "manifest",
    }
    assert all(path.is_file() for path in result.written.values())
    manifest = json.loads(result.written["manifest"].read_text(encoding="utf-8"))
    assert manifest == result.manifest
    assert manifest["panel_generation_sha256"] == panel.manifest["generation_sha256"]
    assert manifest["evaluation_generation_sha256"] == "c" * 64
    assert manifest["checkpoint_inventory"] == [{"date": "2025-01-01"}]
    assert set(manifest["members"]) == set(result.written) - {"summary", "manifest"}


def test_final_reload_failure_rolls_back_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    output_root = tmp_path / "generations"
    real_load = agreement._load_annual_agreement_result_unlocked
    failed = False

    def fail_once(directory: Path, *, trusted_panel: Any) -> Any:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic final reload failure")
        return real_load(directory, trusted_panel=trusted_panel)

    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    monkeypatch.setattr(agreement, "_load_annual_agreement_result_unlocked", fail_once)

    with pytest.raises(RuntimeError, match="final reload failure"):
        agreement._publish_annual_agreement_result(panel, evaluation, output_root=output_root)
    assert not list(output_root.glob("[0-9a-f]*"))
    assert not list(output_root.glob(".*.staging-*"))

    retried = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=output_root,
    )
    assert retried.directory.is_dir()


def test_a_complete_final_generation_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    output_root = tmp_path / "generations"
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    first = agreement._publish_annual_agreement_result(panel, evaluation, output_root=output_root)
    manifest_bytes = first.written["manifest"].read_bytes()

    second = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=output_root,
    )

    assert second.directory == first.directory
    assert first.written["manifest"].read_bytes() == manifest_bytes


@pytest.mark.parametrize("residue_kind", ["staging", "backup"])
def test_a_unique_contained_process_death_residue_is_removed_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    residue_kind: str,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    output_root = tmp_path / "generations"
    output_root.mkdir()
    residue = output_root / f".annual-agreement.{residue_kind}-{'d' * 32}"
    if residue_kind == "staging":
        script = (
            "import os\n"
            "from pathlib import Path\n"
            f"residue = Path({str(residue)!r})\n"
            "residue.mkdir()\n"
            "(residue / 'summary.json').write_text('{}\\n', encoding='utf-8')\n"
            "os._exit(0)\n"
        )
        subprocess.run([sys.executable, "-c", script], check=True)
    else:
        residue.mkdir()
        (residue / "summary.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)

    published = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=output_root,
    )

    assert published.directory.is_dir()
    assert not residue.exists()
    assert not tuple(output_root.glob(".annual-agreement.*-*"))


def test_ambiguous_final_process_death_residue_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    output_root = tmp_path / "generations"
    output_root.mkdir()
    for suffix in ("1" * 32, "2" * 32):
        (output_root / f".annual-agreement.staging-{suffix}").mkdir()
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)

    with pytest.raises(RuntimeError, match="ambiguous final publication residue"):
        agreement._publish_annual_agreement_result(
            panel,
            evaluation,
            output_root=output_root,
        )


def test_a_linked_final_process_death_residue_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    output_root = tmp_path / "generations"
    output_root.mkdir()
    residue = output_root / f".annual-agreement.staging-{'3' * 32}"
    residue.mkdir()
    real_is_link_like = agreement._is_link_like
    monkeypatch.setattr(
        agreement,
        "_is_link_like",
        lambda path: path == residue or real_is_link_like(path),
    )
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)

    with pytest.raises(RuntimeError, match="final publication residue is linked or outside"):
        agreement._publish_annual_agreement_result(
            panel,
            evaluation,
            output_root=output_root,
        )


def test_a_retry_after_process_death_reuses_the_exact_renamed_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    output_root = tmp_path / "generations"
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    first = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=output_root,
    )
    before = {name: path.read_bytes() for name, path in first.written.items()}

    script = (
        "import importlib, runpy\n"
        "from pathlib import Path\n"
        "scope = runpy.run_path('tests/test_micro_sensor_annual_agreement_cli.py')\n"
        "panel, evaluation = scope['_final_bundle']()\n"
        "agreement = importlib.import_module('twair.analysis.micro_sensor_annual_agreement')\n"
        "agreement._validate_loaded_annual_agreement_result = lambda *_: None\n"
        f"result = agreement._publish_annual_agreement_result(panel, evaluation, output_root=Path({str(output_root)!r}))\n"
        "print(result.directory.name)\n"
    )
    retried = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert retried.stdout.strip() == first.directory.name
    assert {name: path.read_bytes() for name, path in first.written.items()} == before
    assert len(tuple(output_root.glob("[0-9a-f]*"))) == 1
    assert not tuple(output_root.glob(".annual-agreement.*-*"))


def _reviewed_source_fixture(
    tmp_path: Path,
    *,
    parsed_generation: str = "a" * 64,
    raw_generation: str = "b" * 64,
) -> tuple[Any, Any, str, str, date, Path]:
    agreement = _module()
    day = date(2025, 1, 2)
    parsed_root = tmp_path / "interim" / "micro_sensors" / "observations" / "generations"
    parsed_directory = parsed_root / parsed_generation
    parsed_directory.mkdir(parents=True)
    members: dict[str, dict[str, object]] = {}
    input_files: list[dict[str, object]] = []
    for index, variable in enumerate(("pm25", "humidity", "temperature"), start=1):
        path = parsed_directory / f"{variable}.parquet"
        path.write_bytes(f"member-{index}".encode())
        identity = {
            "path": path.relative_to(tmp_path).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": agreement.sha256_file(path),
        }
        input_files.append(identity)
        members[path.name] = {key: identity[key] for key in ("bytes", "sha256")}
    ground_path = (
        tmp_path / "processed" / "observations" / "year=2025" / "month=01" / "part-0.parquet"
    )
    ground_path.parent.mkdir(parents=True)
    ground_path.write_bytes(b"ground")
    ground_identity = {
        "path": ground_path.relative_to(tmp_path).as_posix(),
        "bytes": ground_path.stat().st_size,
        "sha256": agreement.sha256_file(ground_path),
    }
    annual_input = SimpleNamespace(
        manifest={
            "inputs": {
                "parsed_generations": [
                    {
                        "date": day.isoformat(),
                        "generation_sha256": parsed_generation,
                        "input_files": input_files,
                    }
                ],
                "ground_files": [ground_identity],
            }
        }
    )
    loaded = SimpleNamespace(
        generation_sha256=parsed_generation,
        directory=parsed_directory,
        manifest={
            "date": day.isoformat(),
            "raw_observation_generation_sha256": raw_generation,
            "members": members,
        },
    )
    (parsed_directory / "manifest.json").write_text(
        json.dumps(loaded.manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return annual_input, loaded, parsed_generation, raw_generation, day, ground_path


def test_checkpoint_preparation_keeps_catalogue_and_manifest_raw_generations_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    catalogue_generation = "c841ef16d7cc55920b6ab5b7b274c2f8b5e68e754d8cce4e1a5677f997e8e05b"
    raw_observation_generation = "38d3634c8628c9be301b2a28ad63ae320cfa1b30c8870b844c6ead98eaf26dfb"
    parsed_generation = "bc9ee6c7f7e47770649091afa6580a8bf281499c24114a42f4de5ab9161343ae"
    annual, loaded, _, _, day, ground_path = _reviewed_source_fixture(
        tmp_path,
        parsed_generation=parsed_generation,
        raw_generation=raw_observation_generation,
    )
    config = agreement.load_annual_agreement_config()
    candidates = pl.DataFrame(
        {"device_id": ["device-1"], "station_name": ["station-1"], "distance_km": [0.1]}
    )
    annual_path = tmp_path / "annual-device-days.parquet"
    pl.DataFrame(schema=dict(agreement.ANNUAL_DEVICE_DAY_SCHEMA)).write_parquet(annual_path)
    annual.device_days = agreement.PinnedAnnualMember(
        path=annual_path,
        generation_dir=annual_path.parent,
        bytes=annual_path.stat().st_size,
        sha256=agreement.sha256_file(annual_path),
    )
    annual.candidate_cohorts = (
        SimpleNamespace(radius_km=config.primary_distance_km, candidates=candidates),
    )
    annual.manifest["inputs"]["ground_files"] = [
        annual.manifest["inputs"]["ground_files"][0],
        *(
            {"path": (f"processed/observations/year=2025/month={month:02d}/part-0.parquet")}
            for month in range(2, 13)
        ),
    ]
    reviewed = SimpleNamespace(
        year=2025,
        parsed_generations=(SimpleNamespace(date=day, generation_sha256=parsed_generation),),
        catalog_generations=(("202501", catalogue_generation),),
    )
    captured: list[dict[str, object]] = []
    checkpoint = SimpleNamespace(directory=tmp_path / "checkpoint")

    def load_checkpoint(**kwargs: object) -> Any:
        captured.append(cast(dict[str, object], kwargs["input_identities"]))
        return checkpoint

    monkeypatch.setattr(agreement, "configured_data_root", lambda: tmp_path)
    monkeypatch.setattr(agreement, "load_annual_micro_sensor_panel_config", lambda: reviewed)
    monkeypatch.setattr(
        agreement,
        "load_micro_sensor_observation_generation",
        lambda *_args, **_kwargs: loaded,
    )
    monkeypatch.setattr(agreement, "_load_agreement_day_checkpoint", load_checkpoint)
    monkeypatch.setattr(
        agreement,
        "aggregate_agreement_day",
        lambda **_kwargs: pytest.fail("distinct source identities reached aggregation"),
    )
    monkeypatch.setattr(
        agreement,
        "write_agreement_day_checkpoint",
        lambda *_args, **_kwargs: pytest.fail("distinct source identities reached publication"),
    )

    prepared = agreement._prepare_annual_agreement_checkpoints(
        annual,
        plan=SimpleNamespace(checkpoint_root=tmp_path / "checkpoints"),
        config=config,
    )

    assert prepared == (checkpoint,)
    assert len(captured) == 1
    assert captured[0]["catalog_generation_sha256"] == catalogue_generation
    assert captured[0]["raw_observation_generation_sha256"] == raw_observation_generation
    assert captured[0]["parsed_generation_sha256"] == parsed_generation
    assert ground_path.is_file()


def test_combined_sources_use_the_reviewed_parsed_loader_and_remain_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    annual, loaded, parsed_generation, raw_generation, day, ground_path = _reviewed_source_fixture(
        tmp_path
    )
    calls: list[tuple[str, Path]] = []

    def load_parsed(generation: str, *, interim_observation_root: Path) -> Any:
        calls.append((generation, interim_observation_root))
        return loaded

    monkeypatch.setattr(agreement, "load_micro_sensor_observation_generation", load_parsed)

    sources = agreement._load_reviewed_agreement_day_sources(
        annual,
        day=day,
        parsed_generation_sha256=parsed_generation,
        data_root=tmp_path,
        reviewed_year=2025,
    )
    sources.assert_unchanged()

    assert calls == [
        (
            parsed_generation,
            tmp_path / "interim" / "micro_sensors" / "observations" / "generations",
        )
    ]
    assert dict(sources.micro_paths) == {
        variable: loaded.directory / f"{variable}.parquet"
        for variable in ("pm25", "humidity", "temperature")
    }
    assert sources.ground_path == ground_path
    assert sources.raw_observation_generation_sha256 == raw_generation


@pytest.mark.parametrize(
    "raw_observation_generation",
    [None, "not-a-sha256", True],
    ids=["missing", "malformed", "boolean"],
)
def test_combined_sources_reject_an_invalid_manifest_raw_observation_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_observation_generation: object,
) -> None:
    agreement = _module()
    annual, loaded, parsed_generation, _, day, _ = _reviewed_source_fixture(tmp_path)
    if raw_observation_generation is None:
        del loaded.manifest["raw_observation_generation_sha256"]
    else:
        loaded.manifest["raw_observation_generation_sha256"] = raw_observation_generation
    (loaded.directory / "manifest.json").write_text(
        json.dumps(loaded.manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agreement,
        "load_micro_sensor_observation_generation",
        lambda *_args, **_kwargs: loaded,
    )

    with pytest.raises(RuntimeError, match="raw-observation generation changed"):
        agreement._load_reviewed_agreement_day_sources(
            annual,
            day=day,
            parsed_generation_sha256=parsed_generation,
            data_root=tmp_path,
            reviewed_year=2025,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "misplaced",
        "parsed_directory_link",
        "parsed_member_link",
        "ground_directory_link",
        "ground_member_link",
    ],
)
def test_combined_sources_reject_misplacement_and_linked_directories_or_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    agreement = _module()
    annual, loaded, parsed_generation, _, day, ground_path = _reviewed_source_fixture(tmp_path)
    if mutation == "misplaced":
        outside = tmp_path / "outside" / parsed_generation
        outside.mkdir(parents=True)
        loaded.directory = outside
    linked = {
        "parsed_directory_link": loaded.directory,
        "parsed_member_link": loaded.directory / "pm25.parquet",
        "ground_directory_link": ground_path.parent,
        "ground_member_link": ground_path,
    }.get(mutation)
    real_is_link_like = agreement._is_link_like
    monkeypatch.setattr(
        agreement,
        "_is_link_like",
        lambda path: (linked is not None and path == linked) or real_is_link_like(path),
    )
    monkeypatch.setattr(
        agreement,
        "load_micro_sensor_observation_generation",
        lambda *_args, **_kwargs: loaded,
    )

    with pytest.raises(RuntimeError, match="reviewed source is linked or outside"):
        agreement._load_reviewed_agreement_day_sources(
            annual,
            day=day,
            parsed_generation_sha256=parsed_generation,
            data_root=tmp_path,
            reviewed_year=2025,
        )


def test_combined_sources_reject_a_member_change_after_duckdb_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    annual, loaded, parsed_generation, _, day, _ = _reviewed_source_fixture(tmp_path)
    monkeypatch.setattr(
        agreement,
        "load_micro_sensor_observation_generation",
        lambda *_args, **_kwargs: loaded,
    )
    sources = agreement._load_reviewed_agreement_day_sources(
        annual,
        day=day,
        parsed_generation_sha256=parsed_generation,
        data_root=tmp_path,
        reviewed_year=2025,
    )
    dict(sources.micro_paths)["pm25"].write_bytes(b"changed-after-duckdb")

    with pytest.raises(RuntimeError, match="reviewed source changed during use"):
        sources.assert_unchanged()


@pytest.mark.parametrize(
    "mutation",
    ["extra_member", "replaced_manifest", "replaced_raw_observation_generation"],
)
def test_combined_sources_reject_mutation_after_the_public_parsed_loader_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    agreement = _module()
    annual, loaded, parsed_generation, _, day, _ = _reviewed_source_fixture(tmp_path)

    def load_then_mutate(*_args: object, **_kwargs: object) -> Any:
        if mutation == "extra_member":
            (loaded.directory / "unexpected.bin").write_bytes(b"unexpected")
        else:
            changed = (
                {**loaded.manifest, "date": "2025-01-03"}
                if mutation == "replaced_manifest"
                else {**loaded.manifest, "raw_observation_generation_sha256": "c" * 64}
            )
            (loaded.directory / "manifest.json").write_text(
                json.dumps(changed) + "\n",
                encoding="utf-8",
            )
        return loaded

    monkeypatch.setattr(agreement, "load_micro_sensor_observation_generation", load_then_mutate)

    with pytest.raises(RuntimeError, match="reviewed parsed generation changed"):
        agreement._load_reviewed_agreement_day_sources(
            annual,
            day=day,
            parsed_generation_sha256=parsed_generation,
            data_root=tmp_path,
            reviewed_year=2025,
        )


@pytest.mark.parametrize("member", ["parsed", "ground"])
def test_combined_sources_never_self_baseline_a_change_after_evidence_comparison(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    member: str,
) -> None:
    agreement = _module()
    annual, loaded, parsed_generation, _, day, ground_path = _reviewed_source_fixture(tmp_path)
    target = loaded.directory / "pm25.parquet" if member == "parsed" else ground_path

    def identify_then_mutate(path: Path, *, data_root: Path) -> dict[str, object]:
        identity = {
            "path": path.relative_to(data_root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": agreement.sha256_file(path),
        }
        if path == target:
            path.write_bytes(b"changed-after-trusted-evidence")
        return identity

    monkeypatch.setattr(
        agreement,
        "load_micro_sensor_observation_generation",
        lambda *_args, **_kwargs: loaded,
    )
    monkeypatch.setattr(agreement, "_portable_reviewed_identity", identify_then_mutate)

    sources = agreement._load_reviewed_agreement_day_sources(
        annual,
        day=day,
        parsed_generation_sha256=parsed_generation,
        data_root=tmp_path,
        reviewed_year=2025,
    )
    with pytest.raises(RuntimeError, match="reviewed source changed during use"):
        sources.assert_unchanged()


def test_combined_checkpoint_preparation_rechecks_sources_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    config = agreement.load_annual_agreement_config()
    day = date(2025, 1, 2)
    parsed_generation = "a" * 64
    raw_generation = "b" * 64
    record = SimpleNamespace(date=day, generation_sha256=parsed_generation)
    reviewed = SimpleNamespace(
        year=2025,
        parsed_generations=(record,),
        catalog_generations=(("202501", raw_generation),),
    )
    candidates = pl.DataFrame(
        {
            "device_id": [f"d{index:03d}" for index in range(124)],
            "station_name": [f"s{index % 13:02d}" for index in range(124)],
            "distance_km": [0.1] * 124,
        }
    )
    annual = SimpleNamespace(
        manifest={
            "inputs": {
                "parsed_generations": [
                    {
                        "date": day.isoformat(),
                        "generation_sha256": parsed_generation,
                        "input_files": [],
                    }
                ],
                "ground_files": [
                    {"path": f"processed/observations/year=2025/month={month:02d}/part-0.parquet"}
                    for month in range(1, 13)
                ],
            }
        },
        candidate_cohorts=(
            SimpleNamespace(radius_km=config.primary_distance_km, candidates=candidates),
        ),
        device_days=SimpleNamespace(),
    )
    events: list[str] = []
    guard_calls = 0

    def assert_unchanged() -> None:
        nonlocal guard_calls
        guard_calls += 1
        events.append("guard")
        if guard_calls == 2:
            raise RuntimeError("annual agreement reviewed source changed during use")

    sources = SimpleNamespace(
        micro_paths=tuple(
            (variable, tmp_path / f"{variable}.parquet")
            for variable in ("pm25", "humidity", "temperature")
        ),
        ground_path=tmp_path / "part-0.parquet",
        assert_unchanged=assert_unchanged,
    )

    def load_sources(*_args: object, **_kwargs: object) -> Any:
        events.append("sources")
        return sources

    def input_identities(**_kwargs: object) -> dict[str, bool]:
        events.append("identities")
        return {"bound": True}

    def load_checkpoint(**_kwargs: object) -> Any:
        events.append("load")
        raise FileNotFoundError

    def aggregate(**_kwargs: object) -> Any:
        events.append("aggregate")
        return SimpleNamespace()

    monkeypatch.setattr(agreement, "load_annual_micro_sensor_panel_config", lambda: reviewed)
    monkeypatch.setattr(agreement, "_load_reviewed_agreement_day_sources", load_sources)
    monkeypatch.setattr(agreement, "_annual_agreement_input_identities", input_identities)
    monkeypatch.setattr(agreement, "_load_agreement_day_checkpoint", load_checkpoint)
    monkeypatch.setattr(agreement, "aggregate_agreement_day", aggregate)
    monkeypatch.setattr(
        agreement,
        "write_agreement_day_checkpoint",
        lambda *_args, **_kwargs: pytest.fail("changed sources reached checkpoint publication"),
    )

    with pytest.raises(RuntimeError, match="reviewed source changed during use"):
        agreement._prepare_annual_agreement_checkpoints(
            annual,
            plan=SimpleNamespace(checkpoint_root=tmp_path / "checkpoints"),
            config=config,
        )

    assert events == ["sources", "identities", "guard", "load", "aggregate", "guard"]


def test_the_final_loader_rejects_a_semantically_rebound_prediction_member(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    result = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=tmp_path / "generations",
    )
    prediction_path = result.written["predictions"]
    pl.read_parquet(prediction_path).with_columns(pl.lit("changed").alias("model")).write_parquet(
        prediction_path
    )
    manifest = json.loads(result.written["manifest"].read_text(encoding="utf-8"))
    manifest["members"]["predictions"] = {
        "path": "predictions.parquet",
        "bytes": prediction_path.stat().st_size,
        "sha256": agreement.sha256_file(prediction_path),
    }
    identity = {field: manifest[field] for field in agreement._FINAL_IDENTITY_FIELDS}
    new_generation = agreement._canonical_hash(identity)
    manifest["generation_sha256"] = new_generation
    result.directory.rename(result.directory.parent / new_generation)
    changed = result.directory.parent / new_generation
    (changed / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.undo()
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda *_: None)
    monkeypatch.setattr(
        agreement,
        "evaluate_annual_agreement",
        lambda _panel: evaluation,
    )

    with pytest.raises(RuntimeError, match="persisted model evidence changed"):
        agreement._load_annual_agreement_result_unlocked(changed, trusted_panel=panel)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("annual_generation_sha256", "e" * 64),
        ("panel_generation_sha256", "e" * 64),
        ("evaluation_generation_sha256", "e" * 64),
        ("checkpoint_inventory", [{"date": "rebound"}]),
        ("claim_boundary", {"reference_station_agreement_only": False}),
    ],
)
def test_coherently_rebound_top_level_evidence_must_match_its_trusted_embedded_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    result = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=tmp_path / "generations",
    )
    manifest = json.loads(result.written["manifest"].read_text(encoding="utf-8"))
    manifest[field] = value
    identity = {name: manifest[name] for name in agreement._FINAL_IDENTITY_FIELDS}
    new_generation = agreement._canonical_hash(identity)
    manifest["generation_sha256"] = new_generation
    result.directory.rename(result.directory.parent / new_generation)
    changed = result.directory.parent / new_generation
    (changed / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="final evidence relationships changed"):
        agreement._load_annual_agreement_result_unlocked(changed, trusted_panel=panel)


def test_a_coherently_rehashed_final_summary_must_equal_the_exact_persisted_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    result = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=tmp_path / "generations",
    )
    summary = json.loads(result.written["summary"].read_text(encoding="utf-8"))
    summary["unknown"] = "rebound"
    summary["output_rows"]["calendar"] = 999999
    result.written["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = json.loads(result.written["manifest"].read_text(encoding="utf-8"))
    manifest["summary_file"] = {
        "path": "summary.json",
        "bytes": result.written["summary"].stat().st_size,
        "sha256": agreement.sha256_file(result.written["summary"]),
    }
    manifest["summary_sha256"] = agreement._canonical_hash(summary)
    identity = {name: manifest[name] for name in agreement._FINAL_IDENTITY_FIELDS}
    new_generation = agreement._canonical_hash(identity)
    manifest["generation_sha256"] = new_generation
    result.directory.rename(result.directory.parent / new_generation)
    changed = result.directory.parent / new_generation
    (changed / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="final summary changed"):
        agreement._load_annual_agreement_result_unlocked(changed, trusted_panel=panel)


def test_a_coherently_rebound_embedded_task_3_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    panel_identity = {
        "schema_version": 1,
        "analysis": "annual_reference_station_agreement_panel",
        "inputs": panel.manifest["inputs"],
        "checkpoint_inventory": panel.manifest["checkpoint_inventory"],
        "config": panel.manifest["config"],
        "claim_boundary": panel.manifest["claim_boundary"],
        "output_rows": {},
        "members": {},
        "summary_file": {},
        "summary_sha256": "a" * 64,
    }
    panel.manifest.clear()
    panel.manifest.update(
        {
            **panel_identity,
            "complete": True,
            "generation_sha256": agreement._canonical_hash(panel_identity),
        }
    )
    evaluation.manifest["panel_generation_sha256"] = panel.manifest["generation_sha256"]
    evaluation.manifest["generation_sha256"] = "c" * 64
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    result = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=tmp_path / "generations",
    )
    manifest = json.loads(result.written["manifest"].read_text(encoding="utf-8"))
    embedded = manifest["panel_manifest"]
    embedded["checkpoint_inventory"] = [{"manifest_sha256": "e" * 64}]
    rebound_panel_identity = {
        name: embedded[name] for name in agreement._AGREEMENT_PANEL_IDENTITY_FIELDS
    }
    embedded["generation_sha256"] = agreement._canonical_hash(rebound_panel_identity)
    manifest["panel_generation_sha256"] = embedded["generation_sha256"]
    manifest["checkpoint_inventory"] = embedded["checkpoint_inventory"]
    evaluation_manifest = manifest["evaluation_manifest"]
    evaluation_manifest["panel_generation_sha256"] = embedded["generation_sha256"]
    evaluation_identity = {
        name: value for name, value in evaluation_manifest.items() if name != "generation_sha256"
    }
    evaluation_manifest["generation_sha256"] = agreement._canonical_hash(evaluation_identity)
    manifest["evaluation_generation_sha256"] = evaluation_manifest["generation_sha256"]
    final_identity = {name: manifest[name] for name in agreement._FINAL_IDENTITY_FIELDS}
    new_generation = agreement._canonical_hash(final_identity)
    manifest["generation_sha256"] = new_generation
    result.directory.rename(result.directory.parent / new_generation)
    changed = result.directory.parent / new_generation
    (changed / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    plan = SimpleNamespace(
        output_root=changed.parent,
        panel_destination=tmp_path / "trusted-panel",
        lock_path=tmp_path / ".run.lock",
    )
    loaded_panel_paths: list[Path] = []

    def load_trusted_panel(path: Path) -> _FrameBundle:
        loaded_panel_paths.append(path)
        return panel

    monkeypatch.setattr(agreement, "annual_agreement_run_plan", lambda: plan)
    monkeypatch.setattr(agreement, "load_annual_agreement_panel", load_trusted_panel)

    with pytest.raises(RuntimeError, match="embedded panel manifest changed"):
        agreement.load_annual_agreement_result(changed)
    assert loaded_panel_paths == [plan.panel_destination]


@pytest.mark.parametrize("linked_name", ["manifest.json", "predictions.parquet"])
def test_the_final_loader_rejects_a_direct_linked_member(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    linked_name: str,
) -> None:
    agreement = _module()
    panel, evaluation = _final_bundle()
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    result = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=tmp_path / "generations",
    )
    linked = result.directory / linked_name
    real_is_link_like = agreement._is_link_like
    monkeypatch.setattr(
        agreement,
        "_is_link_like",
        lambda path: path == linked or real_is_link_like(path),
    )

    with pytest.raises(RuntimeError, match="linked or outside generation"):
        agreement._load_annual_agreement_result_unlocked(
            result.directory,
            trusted_panel=panel,
        )


def test_the_public_final_loader_rejects_a_generation_junction_outside_the_reviewed_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if sys.platform != "win32":
        pytest.skip("junction mutation is Windows-specific")
    agreement = _module()
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    panel, evaluation = _final_bundle()
    monkeypatch.setattr(agreement, "_validate_loaded_annual_agreement_result", lambda *_: None)
    result = agreement._publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=agreement.annual_agreement_run_plan().output_root,
    )
    outside = tmp_path / "outside-generation"
    result.directory.replace(outside)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(result.directory), str(outside)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(RuntimeError, match="linked or outside generation"):
        agreement.load_annual_agreement_result(result.directory)


def test_a_protected_exception_releases_the_combined_run_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        agreement,
        "_run_annual_agreement_locked",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        agreement.run_and_write_annual_agreement()

    with agreement._annual_agreement_run_lock(agreement.annual_agreement_run_plan().lock_path):
        pass


def test_a_second_process_cannot_enter_the_combined_run_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agreement = _module()
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    plan = agreement.annual_agreement_run_plan()
    ready = tmp_path / "child-ready"
    release = tmp_path / "child-release"
    script = (
        "from pathlib import Path\n"
        "import time\n"
        "from twair.analysis.micro_sensor_annual_agreement import _annual_agreement_run_lock\n"
        f"lock=Path({str(plan.lock_path)!r})\n"
        f"ready=Path({str(ready)!r})\n"
        f"release=Path({str(release)!r})\n"
        "with _annual_agreement_run_lock(lock):\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    while not release.exists(): time.sleep(0.01)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child did not acquire the annual agreement run lock"
        with pytest.raises(RuntimeError, match="another annual agreement run is active"):
            agreement.run_and_write_annual_agreement()
    finally:
        release.write_text("release", encoding="utf-8")
        child.wait(timeout=10)


def test_the_cli_rejects_caller_controlled_identity_and_destination_flags() -> None:
    runner = CliRunner()
    for flag, value in (
        ("--generation", "a" * 64),
        ("--destination", "elsewhere"),
        ("--config", "alternate.yaml"),
    ):
        result = runner.invoke(
            cli.app,
            ["analyze", "micro-sensor-annual-agreement", flag, value],
        )

        assert result.exit_code != 0
        assert "No such option" in result.output
