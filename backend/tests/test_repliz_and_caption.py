"""
Tests for AI Caption & Hashtag generator and Repliz social publishing integration.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.v1.publish import router as publish_router
from backend.services.caption_generator import (
    CaptionGenerator,
    clean_product_links,
    clean_text,
    extract_hashtags_from_text,
)
from backend.services.repliz_publisher import (
    ReplizClient,
    ReplizConfig,
    ReplizError,
    load_config,
    save_config,
    verify_credentials,
)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(publish_router, prefix="/api/v1")
    return TestClient(app)


def test_clean_text_and_extract_hashtags():
    raw = "  <p>Trik <b>bangun pagi</b>   nyaman!  </p> "
    assert clean_text(raw) == "Trik bangun pagi nyaman!"

    text_with_tags = "Pagi yang produktif! #morning #fokus #selfimprovement #fokus"
    tags = extract_hashtags_from_text(text_with_tags)
    assert tags == ["#morning", "#fokus", "#selfimprovement"]

    text_with_link = "Beli di sini ya https://example.com/item link di bio ya guys!"
    cleaned = clean_product_links(text_with_link)
    assert "https://" not in cleaned
    assert "link di bio" not in cleaned.lower()


def test_caption_generator_heuristic():
    gen = CaptionGenerator()
    # Force heuristic fallback
    res = gen._heuristic_fallback(
        title="Rahasia Sukses Pagi",
        transcript="Bangun jam 6 pagi dan jangan langsung main gadget.",
        platform="threads",
    )
    assert "topic" in res
    assert len(res["variations"]) == 4
    for v in res["variations"]:
        assert "caption" in v
        assert "style" in v
        assert len(v["caption"]) > 10


def test_caption_generator_generate():
    gen = CaptionGenerator()
    res = gen.generate(
        title="Manajemen Waktu Mahasiswa",
        transcript="Bagi jadwal kuliah dan tugas dengan skala prioritas.",
        platform="threads",
    )
    assert res.topic
    assert len(res.variations) >= 1
    assert len(res.hashtags) >= 1
    assert res.best_caption


def test_repliz_config_masking():
    cfg = ReplizConfig(
        access_key="test_access_key_12345",
        secret_key="test_secret_key_67890",
        default_account_id="acc_1",
        configured=True,
    )
    assert "test_access_key_12345" not in cfg.masked_access_key()
    assert cfg.masked_access_key().startswith("test****")
    assert cfg.masked_secret_key().startswith("test****")


def test_repliz_client_rpc(monkeypatch):
    client = ReplizClient(access_key="dummy_acc", secret_key="dummy_sec")

    # Mock requests.post
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json", "Mcp-Session-Id": "sess_123"}
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"docs": [{"_id": "acc_1", "name": "Test Acc", "type": "threads"}]}),
                }
            ],
            "isError": False,
        },
        "id": 1,
    }

    with patch("requests.post", return_value=mock_resp):
        accounts = client.list_accounts()
        assert len(accounts) == 1
        assert accounts[0]["_id"] == "acc_1"
        assert accounts[0]["name"] == "Test Acc"


def test_api_generate_caption_endpoint(client: TestClient):
    sample_response = {
        "topic": "Mindfulness",
        "hashtags": ["#mindfulness", "#fokus", "#tenang", "#kesehatanmental"],
        "variations": [
            {
                "style": "viral",
                "style_label": "Viral & Catchy",
                "caption": "Cara cepat bikin pikiran tenang dalam 5 menit! ✨ #mindfulness #fokus",
            }
        ],
    }
    with patch.object(CaptionGenerator, "_call_gemini_direct", return_value=sample_response):
        resp = client.post(
            "/api/v1/publish/generate-caption",
            json={
                "title": "5 Menit Menenangkan Pikiran",
                "transcript": "Tarik napas dalam-dalam dan dengarkan suara sekitar.",
                "platform": "threads",
                "language": "id",
                "style": "viral",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["topic"] == "Mindfulness"
        assert "#mindfulness" in data["hashtags"]
        assert len(data["variations"]) == 1


def test_api_repliz_config_endpoint(client: TestClient):
    resp = client.get("/api/v1/publish/repliz/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "configured" in data
    assert "access_key_masked" in data


def test_api_repliz_accounts_endpoint(client: TestClient, monkeypatch):
    with patch.object(
        ReplizClient,
        "list_accounts",
        return_value=[{"_id": "acc_th", "name": "Gataloka", "type": "threads"}],
    ):
        resp = client.get("/api/v1/publish/repliz/accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert "accounts" in data
        assert len(data["accounts"]) == 1
        assert data["accounts"][0]["name"] == "Gataloka"
