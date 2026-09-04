"""Builder main.py pure helpers: config normalization purity + error translation."""

import httpx

from app._helpers import _normalize_config, _turncall_http_error


class TestNormalizeConfig:
    def test_does_not_mutate_input(self):
        cfg = {"name": "a", "custom_tools": [{"name": "book"}]}
        _normalize_config(cfg, port=9001)
        # input untouched — no server_url added, tool unchanged
        assert "server_url" not in cfg
        assert cfg["custom_tools"][0] == {"name": "book"}

    def test_fills_server_url_and_tool_urls(self):
        out = _normalize_config({"name": "a", "custom_tools": [{"name": "book"}]}, port=9001)
        assert out["server_url"] == "http://host.docker.internal:9001"
        assert out["custom_tools"][0]["server_url"] == (
            "http://host.docker.internal:9001/tools/book"
        )

    def test_keeps_explicit_tool_server_url(self):
        out = _normalize_config(
            {"name": "a", "custom_tools": [{"name": "book", "server_url": "https://x/h"}]},
            port=9001,
        )
        assert out["custom_tools"][0]["server_url"] == "https://x/h"

    def test_no_custom_tools_key_stays_absent(self):
        out = _normalize_config({"name": "a"}, port=9001)
        assert "custom_tools" not in out

    def test_no_port_leaves_urls_none(self):
        out = _normalize_config({"name": "a", "custom_tools": [{"name": "book"}]})
        assert out["server_url"] is None
        assert out["custom_tools"][0]["server_url"] is None

    def test_s2s_mode_strips_cascade_blocks(self):
        out = _normalize_config(
            {"name": "a", "pipeline_mode": "s2s", "s2s": {"model": "m"},
             "stt": {"provider": "deepgram"}, "llm": {"provider": "openai"}, "tts": {"provider": "deepgram"}}
        )
        assert "s2s" in out
        assert "llm" not in out and "tts" not in out and "stt" not in out

    def test_cascade_mode_strips_s2s_block(self):
        out = _normalize_config({"name": "a", "llm": {"provider": "openai"}, "s2s": {"model": "m"}})
        assert "llm" in out and "s2s" not in out


class TestTurncallHttpError:
    def _exc(self, status, *, json_body=None, text=""):
        resp = httpx.Response(
            status,
            json=json_body if json_body is not None else None,
            content=None if json_body is not None else text.encode(),
            request=httpx.Request("GET", "http://tc/x"),
        )
        return httpx.HTTPStatusError("e", request=resp.request, response=resp)

    def test_extracts_error_field(self):
        exc = self._exc(409, json_body={"error": "already exists"})
        http_exc = _turncall_http_error(exc)
        assert http_exc.status_code == 409
        assert http_exc.detail == "already exists"

    def test_falls_back_to_text_when_not_json(self):
        exc = self._exc(500, text="upstream boom")
        http_exc = _turncall_http_error(exc)
        assert http_exc.status_code == 500
        assert http_exc.detail == "upstream boom"
