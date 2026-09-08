from __future__ import annotations

from proximic_ring.diagnostic_log import RotatingDiagnosticLog


def test_diagnostic_log_rotates_to_a_fixed_number_of_backups(tmp_path) -> None:
    path = tmp_path / "logs" / "diagnostic.log"
    log = RotatingDiagnosticLog(path, max_bytes=80, backup_count=2)

    assert log.append("a" * 50)
    assert log.append("b" * 40)
    assert log.append("c" * 40)
    assert log.append("d" * 40)

    assert path.read_text(encoding="utf-8") == "d" * 40 + "\n"
    assert path.with_name("diagnostic.log.1").read_text(encoding="utf-8") == (
        "c" * 40 + "\n"
    )
    assert path.with_name("diagnostic.log.2").read_text(encoding="utf-8") == (
        "b" * 40 + "\n"
    )
    assert not path.with_name("diagnostic.log.3").exists()


def test_diagnostic_log_io_failure_never_escapes(tmp_path) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("occupied", encoding="utf-8")
    log = RotatingDiagnosticLog(blocked_parent / "diagnostic.log")

    assert log.append("must not affect voice input") is False
