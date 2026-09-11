from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def test_bundled_proximity_model_versions_are_complete_and_hashed():
    assets = Path(__file__).parents[1] / "src" / "proximic_ring" / "assets"
    registry = json.loads(
        (assets / "proximity_model_versions.json").read_text(encoding="utf-8")
    )

    assert registry["active_version"] == "v2"
    assert registry["active_model"] == "ringo-near-v2.model"
    assert set(registry["versions"]) == {"v1", "v2"}
    for version, entry in registry["versions"].items():
        model_path = assets / entry["model"]
        sidecar_path = model_path.with_name(model_path.name + ".json")
        assert model_path.is_file()
        assert sidecar_path.is_file()
        digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
        assert digest == entry["sha256"]
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["model_version"] == version
        assert sidecar["model_sha256"] == digest
        assert (
            sidecar["recommended_stage2_threshold"]
            == entry["recommended_stage2_threshold"]
        )


def test_bundled_model_migration_updates_only_the_old_default(tmp_path):
    pytest.importorskip("PySide6")
    from proximic_ring.ui.controller import _migrated_bundled_near_model_path

    default_model = tmp_path / "ringo-near-v2.model"
    default_model.write_bytes(b"model")
    old_default = tmp_path / "old-install" / "ringo-near-v1.model"
    custom_model = tmp_path / "my-custom.model"

    assert _migrated_bundled_near_model_path("", 0, default_model) == str(
        default_model
    )
    assert _migrated_bundled_near_model_path(
        str(old_default), 0, default_model
    ) == str(default_model)
    assert _migrated_bundled_near_model_path(
        str(custom_model), 0, default_model
    ) == str(custom_model)
    assert _migrated_bundled_near_model_path(
        str(old_default), 2, default_model
    ) == str(old_default)
