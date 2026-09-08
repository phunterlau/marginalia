"""Opt-in launchd configuration and Keychain-backed foreground launcher.

Generating a plist never reads credentials, installs a service or starts work.
"""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys


def absolute(value, *, executable=False):
    path = Path(value)
    if not path.is_absolute() or not path.exists():
        raise ValueError("An existing absolute backend path is required")
    if executable and (not path.is_file() or not os.access(path, os.X_OK)):
        raise ValueError("An executable backend file is required")
    return str(path)


def credential_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid Keychain item name")
    return value


def arguments(args):
    return ["--root", absolute(args.root), "--spaces-root", absolute(args.spaces_root),
        "--pi", absolute(args.pi, executable=True), "--fork-node", absolute(args.fork_node, executable=True)]


def configuration(args):
    """No token, shell command, paid-worker flag, or automatic restart."""
    argv = [absolute(sys.executable, executable=True), "-m", "research_arms.macos_service", "--run",
        "--keychain-service", credential_name(args.keychain_service),
        "--keychain-account", credential_name(args.keychain_account), *arguments(args)]
    return {"Label": "local.marginalia.arms", "ProgramArguments": argv,
        "RunAtLoad": True, "KeepAlive": False, "ProcessType": "Background",
        "WorkingDirectory": absolute(args.root), "Umask": 0o077}


def token_from_keychain(service, account):
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
        credential_name(service), "-a", credential_name(account), "-w"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=15, check=False)
    if result.returncode != 0 or len(result.stdout) > 8192:
        raise ValueError("Keychain token unavailable; unlock or configure the item locally")
    token = result.stdout.decode("utf-8").strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("Invalid Keychain token")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plist", action="store_true", help="Print configuration only; never install or connect")
    mode.add_argument("--run", action="store_true", help="Read Keychain and start the foreground Gateway")
    for name in ("root", "spaces-root", "pi", "fork-node", "keychain-service", "keychain-account"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        config = configuration(args)
        if args.plist:
            sys.stdout.buffer.write(plistlib.dumps(config))
            return
        if sys.platform != "darwin": raise ValueError("Keychain launch requires macOS")
        token = token_from_keychain(args.keychain_service, args.keychain_account)
        environment = dict(os.environ, DISCORD_BOT_TOKEN=token)
        # exec preserves process ownership and Ctrl-C/launchd signal semantics.
        os.execve(sys.executable, [sys.executable, "-m", "research_arms.gateway",
            *arguments(args), "--connect"], environment)
    except (ValueError, OSError, subprocess.SubprocessError):
        parser.exit(1, "Arms launch failed; check local paths and Keychain access. No credentials are logged.\n")


if __name__ == "__main__":
    main()
