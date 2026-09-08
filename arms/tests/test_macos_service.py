from types import SimpleNamespace
import plistlib
import sys

import pytest

from research_arms.macos_service import configuration, token_from_keychain


def test_entrypoint_generation_never_reads_keychain(tmp_path, monkeypatch, capsysbinary):
    from research_arms.macos_service import main
    monkeypatch.setattr(sys, "argv", ["macos_service", "--plist", "--root", str(tmp_path),
        "--spaces-root", str(tmp_path), "--pi", sys.executable, "--fork-node", sys.executable,
        "--keychain-service", "service", "--keychain-account", "owner"])
    def forbidden(*args): raise AssertionError("Generation must not read secrets")
    monkeypatch.setattr("research_arms.macos_service.token_from_keychain", forbidden)
    main()
    result = plistlib.loads(capsysbinary.readouterr().out)
    assert result["Label"] == "local.marginalia.arms"


def test_entrypoint_exec_passes_token_only_in_environment(tmp_path, monkeypatch, capsys):
    from research_arms.macos_service import main
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", ["macos_service", "--run", "--root", str(tmp_path),
        "--spaces-root", str(tmp_path), "--pi", sys.executable, "--fork-node", sys.executable,
        "--keychain-service", "service", "--keychain-account", "owner"])
    monkeypatch.setattr("research_arms.macos_service.token_from_keychain", lambda *args: "SECRET_CANARY")
    calls = []
    def execute(path, argv, environment):
        calls.append(argv)
        assert environment["DISCORD_BOT_TOKEN"] == "SECRET_CANARY"
        assert "SECRET_CANARY" not in repr(argv)
        assert argv[:3] == [sys.executable, "-m", "research_arms.gateway"]
        assert argv[-1] == "--connect"
        assert "--run-approved-absorption" not in argv and "--sync-commands" not in argv
        raise OSError("SECRET_CANARY")  # Even OS failure details must not leak.
    monkeypatch.setattr("research_arms.macos_service.os.execve", execute)
    with pytest.raises(SystemExit) as error: main()
    assert error.value.code == 1 and len(calls) == 1
    output = capsys.readouterr()
    assert "SECRET_CANARY" not in output.out + output.err


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
