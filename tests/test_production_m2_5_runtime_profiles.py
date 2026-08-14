import json

import pytest
from click.testing import CliRunner

from manju.cli import _production_service, cli
from manju.production.models import ProductionError, ReasonCode
from manju.production.runtime_profiles import PROFILE_CONFIG_ENV, resolve_visual_provider_registry


def _async_profiles():
    return json.dumps({
        "async-image": {
            "mode": "async_http",
            "base_url": "https://provider.example.test",
            "api_key_env": "TEST_ASYNC_IMAGE_KEY",
            "allowed_artifact_origins": ["https://artifacts.example.test"],
        },
    })


def test_async_profile_registry_uses_process_config_and_keeps_credentials_out_of_project():
    registry = resolve_visual_provider_registry(required_profile="async-image", environ={
        PROFILE_CONFIG_ENV: _async_profiles(), "TEST_ASYNC_IMAGE_KEY": "offline-key",
    })
    provider = registry.resolve("async-image")
    assert provider.base_url == "https://provider.example.test"
    assert provider.capabilities.automatic_recovery_safe and provider.capabilities.verified_cost
    with pytest.raises(ProductionError) as caught:
        registry.resolve("other-profile")
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


def test_manual_sync_profile_is_refused_before_any_provider_dispatch():
    profiles = json.dumps({"sync-image": {"mode": "manual_sync"}})
    with pytest.raises(ProductionError) as caught:
        resolve_visual_provider_registry(required_profile="sync-image", environ={PROFILE_CONFIG_ENV: profiles})
    assert caught.value.code == ReasonCode.OPERATION_OUTCOME_UNKNOWN.value


def test_missing_credential_or_unapproved_config_is_rejected_without_network():
    with pytest.raises(ProductionError) as caught:
        resolve_visual_provider_registry(required_profile="async-image", environ={PROFILE_CONFIG_ENV: _async_profiles()})
    assert caught.value.code == ReasonCode.HMAC_KEY_UNAVAILABLE.value
    with pytest.raises(ProductionError) as caught:
        resolve_visual_provider_registry(required_profile="async-image", environ={
            PROFILE_CONFIG_ENV: json.dumps({"async-image": {"mode": "async_http", "base_url": "https://provider.example.test", "api_key_env": "K", "allowed_artifact_origins": ["https://artifacts.example.test"], "extra": "no"}}),
            "K": "offline-key",
        })
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 61])
def test_async_profile_rejects_nonfinite_or_out_of_range_timeouts(timeout):
    config = {"async-image": {
        "mode": "async_http", "base_url": "https://provider.example.test", "api_key_env": "K",
        "allowed_artifact_origins": ["https://artifacts.example.test"], "timeout_seconds": timeout,
    }}
    with pytest.raises(ProductionError) as caught:
        resolve_visual_provider_registry(required_profile="async-image", environ={PROFILE_CONFIG_ENV: json.dumps(config), "K": "offline-key"})
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


def test_cli_init_freezes_only_profile_and_public_request_then_runtime_assembles_registry(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    request = tmp_path / "request.json"
    project = tmp_path / "project"
    source.write_text("offline source", encoding="utf-8")
    request.write_text(json.dumps({"prompt": "public", "model": "image", "size": "1024x1024"}), encoding="utf-8")
    result = CliRunner().invoke(cli, [
        "project", "init", "--source", str(source), "--source-type", "script", "--output-dir", str(project),
        "--visual-provider-profile", "async-image", "--visual-request-file", str(request), "--visual-max-amount", "1", "--json",
    ])
    assert result.exit_code == 0, result.output
    value = json.loads((project / "project.json").read_text(encoding="utf-8"))
    visual = value["production"]["visual"]
    assert visual["provider_profile"] == "async-image"
    assert visual["provider_request"] == {"prompt": "public", "model": "image", "size": "1024x1024"}
    serialized = json.dumps(value, ensure_ascii=False)
    assert "provider.example.test" not in serialized and "offline-key" not in serialized
    no_credential_status = CliRunner().invoke(cli, ["status", str(project / "project.json"), "--json"])
    assert no_credential_status.exit_code == 0, no_credential_status.output
    no_credential_doctor = CliRunner().invoke(cli, ["doctor", str(project / "project.json"), "--json"])
    assert no_credential_doctor.exit_code == 0, no_credential_doctor.output
    monkeypatch.setenv(PROFILE_CONFIG_ENV, _async_profiles())
    monkeypatch.setenv("TEST_ASYNC_IMAGE_KEY", "offline-key")
    service = _production_service(str(project / "project.json"))
    assert service.visual_adapter._provider_registry.resolve("async-image").base_url == "https://provider.example.test"
    assert service.get_status().status == "ready"
