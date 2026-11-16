import json
import re

import pytest

from config import DEFAULT_CONFIG, load_config, stack_name
from deploy import aws_environment


def test_region_account_and_names_are_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("HPM_ARTIFACT_DIR", raising=False)
    cfg = json.loads(DEFAULT_CONFIG.read_text())
    cfg.update(name="test", account="123456789012", region="eu-west-1", prefix="hpm-test",
               mediaBucket="123456789012-hpm-test-media")
    cfg.pop("stackPrefix")
    path = tmp_path / "test.json"
    path.write_text(json.dumps(cfg))
    loaded = load_config(path)
    assert stack_name(loaded, "Application") == "HpmTestApplication"
    assert loaded["runtimePrefix"] == "hpm_test"
    assert loaded["artifactDir"].endswith("/infra/.artifacts/test")
    assert loaded["frontendDir"].endswith("/frontend/dist-deploy/test")
    env = aws_environment(loaded["region"])
    assert env["AWS_REGION"] == env["AWS_DEFAULT_REGION"] == "eu-west-1"


def test_credentials_follow_the_standard_chain_unless_strict(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "my-sso-profile")
    monkeypatch.delenv("HPM_INSTANCE_PROFILE_ONLY", raising=False)
    env = aws_environment("us-west-2")
    assert env["AWS_PROFILE"] == "my-sso-profile" and "AWS_SHARED_CREDENTIALS_FILE" not in env
    monkeypatch.setenv("HPM_INSTANCE_PROFILE_ONLY", "1")
    env = aws_environment("us-west-2")
    assert "AWS_PROFILE" not in env and env["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"


class _Session:
    def __init__(self, method, account, role_arn=None):
        self.method, self.account, self.role = method, account, role_arn

    def get_credentials(self):
        return type("C", (), {"method": self.method})() if self.method else None

    def client(self, name):
        session = self

        class Client:
            def get_caller_identity(self):
                return {"Account": session.account, "Arn": f"arn:aws:sts::{session.account}:assumed-role/R/s"}

            def get_role(self, RoleName):
                if not session.role:
                    raise PermissionError("no iam:GetRole")
                return {"Role": {"Arn": session.role}}
        return Client()


def test_check_identity(monkeypatch):
    from deploy import check_identity
    cfg = {"account": "123456789012"}
    monkeypatch.delenv("HPM_INSTANCE_PROFILE_ONLY", raising=False)
    assert check_identity(_Session("sso", "123456789012"), cfg)["Account"] == "123456789012"
    with pytest.raises(SystemExit, match="account 210987654321"):
        check_identity(_Session("sso", "210987654321"), cfg)
    with pytest.raises(SystemExit, match="No AWS credentials"):
        check_identity(_Session(None, "123456789012"), cfg)
    monkeypatch.setenv("HPM_INSTANCE_PROFILE_ONLY", "1")
    with pytest.raises(SystemExit, match="instance-profile"):
        check_identity(_Session("sso", "123456789012"), cfg)
    assert check_identity(_Session("iam-role", "123456789012"), cfg)


def test_principal_arn_keeps_the_sso_role_path():
    from deploy import principal_arn
    caller = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Admin_abc/alice"
    full = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_abc"
    assert principal_arn(_Session("sso", "123456789012", full), caller) == full
    assert principal_arn(_Session("sso", "123456789012"), caller) == \
        "arn:aws:iam::123456789012:role/AWSReservedSSO_Admin_abc"
    assert principal_arn(_Session("env", "123456789012"), "arn:aws:iam::123456789012:user/alice") == \
        "arn:aws:iam::123456789012:user/alice"


def test_fixed_session_id_is_optional_and_validated(tmp_path):
    assert load_config(valid(tmp_path))["fixedSessionId"] == ""
    good = "event-2026-11-" + "a" * 24
    assert load_config(valid(tmp_path, fixedSessionId=good))["fixedSessionId"] == good
    with pytest.raises(ValueError, match="fixedSessionId"):
        load_config(valid(tmp_path, fixedSessionId="too-short"))


def test_rejects_artifact_path_escape(tmp_path):
    cfg = json.loads(DEFAULT_CONFIG.read_text())
    cfg.update(account="123456789012", mediaBucket="123456789012-hpm-test-media")
    cfg["name"] = "../other"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="name must"):
        load_config(path)


def test_example_config_has_only_placeholders_for_the_account():
    text = DEFAULT_CONFIG.read_text()
    assert DEFAULT_CONFIG.name == "example.json"
    assert "<your-account-id>" in text
    assert not re.search(r"\b\d{12}\b", text)
    with pytest.raises(ValueError, match="placeholders"):
        load_config(DEFAULT_CONFIG)


def valid(tmp_path, **changes):
    cfg = json.loads(DEFAULT_CONFIG.read_text())
    cfg.update({"account": "123456789012", "mediaBucket": "123456789012-hpm-demo-media", **changes})
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cfg))
    return path


def test_optional_buckets_and_defaults(tmp_path):
    loaded = load_config(valid(tmp_path))
    assert loaded["sceneBucket"] == loaded["scriptsBucket"] == ""
    assert loaded["soccerUrl"] == "/media/soccer/horizontal/football.mp4"
    assert loaded["soccerVerticalUrl"] == "/media/soccer/vertical/football.mp4"
    assert "f1MobileUrl" not in loaded and "sourceMediaRegion" not in loaded
    with pytest.raises(ValueError, match="dataLoaderPrincipals"):
        load_config(valid(tmp_path, dataLoaderPrincipals=["someone"]))
    with pytest.raises(ValueError, match="mediaBucket"):
        load_config(valid(tmp_path, mediaBucket="Bad_Bucket"))


def test_deployer_role_arn():
    from deploy import role_arn
    assert role_arn("arn:aws:sts::123456789012:assumed-role/Deployer/i-0abc") == "arn:aws:iam::123456789012:role/Deployer"
    assert role_arn("arn:aws:iam::123456789012:user/alice") == "arn:aws:iam::123456789012:user/alice"
