"""Tests for the ``ownpanel`` CLI entry points (cli.py).

test_bootstrap.py covers the service-level token logic; here we pin the
CLI output format itself, which is where a leaked token would surface.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from ownpanel import cli


def _write_dev_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 25443\n"
        "dev_http = true\n"
        "\n"
        "[paths]\n"
        f"data_dir = {json.dumps(str(tmp_path / 'data'))}\n"
        f"helper_socket = {json.dumps(str(tmp_path / 'helper.sock'))}\n"
        f"terminal_socket = {json.dumps(str(tmp_path / 'terminal.sock'))}\n",
        encoding="utf-8",
    )
    return config_path


def test_bootstrap_token_is_not_embedded_in_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: the setup token is printed in the clear exactly once and never
    placed in a URL query, so it cannot reach uvicorn access logs or browser history."""
    config_path = _write_dev_config(tmp_path)

    rc = cli._cmd_bootstrap_token(argparse.Namespace(config=str(config_path)))

    assert rc == 0
    out = capsys.readouterr().out
    match = re.search(r"^Bootstrap token: (\S+)$", out, re.MULTILINE)
    assert match, f"no bootstrap token line in output:\n{out}"
    token = match.group(1)

    assert "?token=" not in out
    # The token string appears exactly once: the clear-text line, not the setup URL.
    assert out.count(token) == 1
    assert "/setup" in out


def test_bootstrap_token_masks_wildcard_listen_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 0.0.0.0 / :: listen host is not a reachable address; the printed setup URL
    uses a placeholder rather than an unusable literal host."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[server]\n"
        'host = "0.0.0.0"\n'
        "port = 25443\n"
        "dev_http = false\n"
        "\n"
        "[paths]\n"
        f"data_dir = {json.dumps(str(tmp_path / 'data'))}\n",
        encoding="utf-8",
    )

    rc = cli._cmd_bootstrap_token(argparse.Namespace(config=str(config_path)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "0.0.0.0" not in out
    assert "<panel-address>" in out
    assert "https://<panel-address>:25443/setup" in out
