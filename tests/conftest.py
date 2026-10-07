"""测试公共夹具：独立临时数据库，避免污染本地 chan_trading.db。"""

import pytest

import app.config as config_module
from app.config import init_database
from app.marketdata.registry import reset_registry


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """每个测试一个全新的 SQLite 文件库。"""
    db_path = tmp_path / "test_marketdata.db"
    monkeypatch.setattr(
        config_module, "DATABASE_URL", f"sqlite:///{db_path}"
    )
    monkeypatch.setattr(config_module, "_engine", None)
    monkeypatch.setattr(config_module, "_SessionLocal", None)
    init_database()
    reset_registry()
    yield db_path
    reset_registry()
    monkeypatch.setattr(config_module, "_engine", None)
    monkeypatch.setattr(config_module, "_SessionLocal", None)
