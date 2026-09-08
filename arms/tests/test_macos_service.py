from types import SimpleNamespace
import plistlib
import sys

import pytest

from research_arms.macos_service import configuration, token_from_keychain


def test_plist_is_fixed_argv_without_secrets_or_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "SECRET_CANARY")
    args = SimpleNamespace(root=str(tmp_path), spaces_root=str(tmp_path), pi=sys.executable,
        fork_node=sys.executable, keychain_service="marginalia", keychain_account="owner")
    result = configuration(args)
    encoded = plistlib.dumps(result)
    assert plistlib.loads(encoded) == result
    assert b"SECRET_CANARY" not in encoded
    assert result["KeepAlive"] is False and result["Umask"] == 0o077
    assert "--sync-commands" not in result["ProgramArguments"]
    assert "--run-approved-absorption" not in result["ProgramArguments"]
    assert "EnvironmentVariables" not in result
    args.root = "relative"
    with pytest.raises(ValueError): configuration(args)


@pytest.mark.parametrize("code,data,ok", [(0, b"test-token\n", True), (1, b"SECRET", False),
    (0, b"", False), (0, b"a b", False), (0, b"x" * 8193, False)])
def test_keychain_capture_never_uses_shell_or_prints_secret(monkeypatch, capsys, code, data, ok):
    def run(argv, **kwargs):
        assert argv == ["/usr/bin/security", "find-generic-password", "-s", "service", "-a", "owner", "-w"]
        assert not kwargs.get("shell") and kwargs["timeout"] == 15
        return SimpleNamespace(returncode=code, stdout=data)
    monkeypatch.setattr("research_arms.macos_service.subprocess.run", run)
    if ok: assert token_from_keychain("service", "owner") == "test-token"
    else:
        with pytest.raises(ValueError): token_from_keychain("service", "owner")
    assert capsys.readouterr() == ("", "")
