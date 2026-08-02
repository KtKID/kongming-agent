"""config_store.py 单测：per-cwd JSON 读写 + 原子写 + 并发安全。"""

from __future__ import annotations

import concurrent.futures
import json
import threading
from pathlib import Path

import pytest

from hosts.web.approvals.auto.config_store import ConfigStore, ProjectConfig, cwd_hash
from safety.auto_approval.disposition import ApprovalDispositionMode

# ---------- cwd_hash ----------


class TestCwdHash:
    def test_deterministic(self) -> None:
        assert cwd_hash("/proj/x") == cwd_hash("/proj/x")

    def test_different(self) -> None:
        assert cwd_hash("/proj/x") != cwd_hash("/proj/y")

    def test_empty_returns_sentinel(self) -> None:
        assert cwd_hash("") == "_empty"

    def test_length(self) -> None:
        assert len(cwd_hash("/proj/x")) == 12

    def test_normalize_trailing_slash(self) -> None:
        # /proj/x 和 /proj/x/ 应同 hash（normpath）
        assert cwd_hash("/proj/x") == cwd_hash("/proj/x/")


# ---------- ProjectConfig ----------


class TestProjectConfig:
    def test_defaults(self) -> None:
        cfg = ProjectConfig(cwd="/tmp")
        assert cfg.mode is ApprovalDispositionMode.USER
        assert cfg.rule_overrides == {}
        assert cfg.timeout_ms == 0

    def test_to_from_json_roundtrip(self) -> None:
        cfg = ProjectConfig(
            cwd="/tmp",
            mode=ApprovalDispositionMode.LLM,
            rule_overrides={"bash_sudo": False},
            timeout_ms=5000,
        )
        data = cfg.to_json()
        cfg2 = ProjectConfig.from_json(data)
        assert cfg2 == cfg

    def test_from_json_coerces_types(self) -> None:
        cfg = ProjectConfig.from_json(
            {
                "cwd": "/x",
                "mode": "full_trust",
                "rule_overrides": {"a": 0, "b": 1},
                "timeout_ms": "3000",
            },
        )
        assert cfg.mode is ApprovalDispositionMode.FULL_TRUST
        assert cfg.rule_overrides == {"a": False, "b": True}
        assert cfg.timeout_ms == 3000


# ---------- ConfigStore CRUD ----------


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "auto_approval")


class TestConfigStoreCrud:
    def test_get_missing_returns_none(self, store: ConfigStore) -> None:
        assert store.get("/no/such/cwd") is None

    def test_get_or_default_returns_default(self, store: ConfigStore) -> None:
        cfg = store.get_or_default("/new/cwd")
        assert cfg.cwd == "/new/cwd"
        assert cfg.mode is ApprovalDispositionMode.USER
        assert cfg.rule_overrides == {}

    def test_get_or_default_does_not_persist(self, store: ConfigStore) -> None:
        store.get_or_default("/new/cwd")
        assert store.get("/new/cwd") is None

    def test_set_persists(self, store: ConfigStore) -> None:
        cfg = ProjectConfig(cwd="/x", mode=ApprovalDispositionMode.LLM)
        store.set(cfg)
        loaded = store.get("/x")
        assert loaded is not None
        assert loaded.mode is ApprovalDispositionMode.LLM

    def test_set_empty_cwd_raises(self, store: ConfigStore) -> None:
        with pytest.raises(ValueError, match="cwd must be non-empty"):
            store.set(ProjectConfig(cwd=""))

    def test_set_overwrites(self, store: ConfigStore) -> None:
        store.set(ProjectConfig(cwd="/x", mode=ApprovalDispositionMode.LLM))
        store.set(ProjectConfig(cwd="/x", mode=ApprovalDispositionMode.USER))
        loaded = store.get("/x")
        assert loaded is not None
        assert loaded.mode is ApprovalDispositionMode.USER

    def test_delete_existing(self, store: ConfigStore) -> None:
        store.set(ProjectConfig(cwd="/x", mode=ApprovalDispositionMode.LLM))
        assert store.delete("/x") is True
        assert store.get("/x") is None

    def test_delete_missing(self, store: ConfigStore) -> None:
        assert store.delete("/no") is False

    def test_list_cwds(self, store: ConfigStore) -> None:
        store.set(ProjectConfig(cwd="/a"))
        store.set(ProjectConfig(cwd="/b"))
        store.set(ProjectConfig(cwd="/c"))
        cwds = store.list_cwds()
        assert sorted(cwds) == ["/a", "/b", "/c"]

    def test_rule_overrides_persist(self, store: ConfigStore) -> None:
        store.set(
            ProjectConfig(
                cwd="/x",
                mode=ApprovalDispositionMode.LLM,
                rule_overrides={"bash_sudo": False, "bash_dd": False},
            ),
        )
        loaded = store.get("/x")
        assert loaded is not None
        assert loaded.rule_overrides == {"bash_sudo": False, "bash_dd": False}


# ---------- 原子写 / 并发安全 ----------


class TestAtomicWrite:
    def test_no_tmp_files_left_after_success(self, store: ConfigStore) -> None:
        store.set(ProjectConfig(cwd="/x", mode=ApprovalDispositionMode.LLM))
        # 不应留下 .tmp 文件
        tmps = list(Path(store._root).glob("*.tmp"))
        assert tmps == []

    def test_corrupted_file_raises_on_get(self, store: ConfigStore, tmp_path: Path) -> None:
        # 手动写入损坏内容
        path = store._path_for("/x")
        path.write_text("not json{{{", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            store.get("/x")

    def test_concurrent_set_no_corruption(self, store: ConfigStore) -> None:
        """50 个线程并发写同一 cwd，最终读出来必须是 valid json + 内容是其中一次写入的。"""
        cwd = "/proj/concurrent"
        # 准备 50 个不同的 timeout_ms 值
        values = list(range(1000, 6001, 100))
        assert len(values) == 51

        def writer(v: int) -> None:
            store.set(ProjectConfig(cwd=cwd, mode=ApprovalDispositionMode.LLM, timeout_ms=v))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(writer, values))

        loaded = store.get(cwd)
        assert loaded is not None
        # 内容必须是 valid 且 timeout 是其中一个写入值（不是垃圾）
        assert loaded.timeout_ms in values
        assert loaded.cwd == cwd

    def test_concurrent_read_during_write(self, store: ConfigStore) -> None:
        """边写边读：读到的必须是 valid JSON（旧版或新版完整内容，不会是 half-written）。"""
        cwd = "/proj/rw"
        store.set(ProjectConfig(cwd=cwd, mode=ApprovalDispositionMode.LLM, timeout_ms=1000))

        stop = threading.Event()
        errors: list[Exception] = []

        def reader() -> None:
            while not stop.is_set():
                try:
                    cfg = store.get(cwd)
                    if cfg is not None:
                        assert cfg.cwd == cwd
                except Exception as e:
                    errors.append(e)

        def writer() -> None:
            for i in range(50):
                store.set(
                    ProjectConfig(
                        cwd=cwd,
                        mode=(
                            ApprovalDispositionMode.LLM if i % 2 else ApprovalDispositionMode.USER
                        ),
                        timeout_ms=1000 + i,
                    )
                )

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        writer_t = threading.Thread(target=writer)
        writer_t.start()
        writer_t.join()
        stop.set()
        for t in readers:
            t.join()

        assert errors == [], f"读取到损坏数据: {errors[:3]}"
