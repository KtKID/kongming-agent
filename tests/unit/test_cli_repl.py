"""unit：cli.repl.confirm_yn 安全失败承诺。

B8 / CR 报告 cr-report-20260424-202744.md。
``confirm_yn`` 文档明确承诺：EOF / stdin closed / KeyboardInterrupt → False。
这是审批类安全语义（"无法确认" ≡ "不批准"），必须守住。
"""

from __future__ import annotations

import io

from cli.repl import confirm_yn


def test_confirm_yn_yes(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert confirm_yn("proceed?") is True


def test_confirm_yn_no(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    assert confirm_yn("proceed?") is False


def test_confirm_yn_uppercase_yes(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("YES\n"))
    assert confirm_yn("proceed?") is True


def test_confirm_yn_mixed_case_y(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("  Y  \n"))
    assert confirm_yn("proceed?") is True


def test_confirm_yn_empty_line_returns_default_false(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert confirm_yn("proceed?") is False


def test_confirm_yn_empty_line_returns_default_true(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert confirm_yn("proceed?", default=True) is True


def test_confirm_yn_stdin_closed_returns_false(monkeypatch):
    """readline 返回空串（""）代表 stdin 已关闭 → False。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert confirm_yn("proceed?", default=True) is False


def test_confirm_yn_eof_returns_false(monkeypatch):
    """EOFError 路径等同拒绝。"""

    class _EOFStdin:
        def readline(self):
            raise EOFError

    monkeypatch.setattr("sys.stdin", _EOFStdin())
    assert confirm_yn("proceed?", default=True) is False


def test_confirm_yn_keyboard_interrupt_returns_false(monkeypatch):
    """KeyboardInterrupt 路径等同拒绝。"""

    class _KIStdin:
        def readline(self):
            raise KeyboardInterrupt

    monkeypatch.setattr("sys.stdin", _KIStdin())
    assert confirm_yn("proceed?", default=True) is False


def test_confirm_yn_unknown_answer_treated_as_no(monkeypatch):
    """既不是 y/yes 也不是空串 → False（保守拒绝）。"""
    monkeypatch.setattr("sys.stdin", io.StringIO("maybe\n"))
    assert confirm_yn("proceed?") is False


def test_confirm_yn_prints_prompt(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    confirm_yn("Approve?", default=False)
    out = capsys.readouterr().out
    assert "Approve?" in out
    assert "[y/N]" in out  # default=False 的 hint


def test_confirm_yn_hint_flips_with_default(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    confirm_yn("Approve?", default=True)
    out = capsys.readouterr().out
    assert "[Y/n]" in out  # default=True 的 hint
