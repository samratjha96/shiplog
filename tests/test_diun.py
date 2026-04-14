"""Tests for shiplog.diun — diun env var parsing."""

import pytest

from shiplog.diun import DiunEvent, DiunParseError, parse_env


class TestParseEnv:
    def test_full_event(self):
        env = {
            "DIUN_ENTRY_STATUS": "new",
            "DIUN_ENTRY_IMAGE": "docker.io/crazymax/diun:v4.31.0",
            "DIUN_ENTRY_HUBLINK": "https://hub.docker.com/r/crazymax/diun",
            "DIUN_ENTRY_DIGEST": "sha256:216e3ae7de",
            "DIUN_ENTRY_CREATED": "2020-03-26 12:23:56 +0000 UTC",
            "DIUN_ENTRY_PLATFORM": "linux/amd64",
            "DIUN_ENTRY_PROVIDER": "docker",
        }
        event = parse_env(env)
        assert event.status == "new"
        assert event.image == "docker.io/crazymax/diun:v4.31.0"
        assert event.image_name == "docker.io/crazymax/diun"
        assert event.tag == "v4.31.0"
        assert event.digest == "sha256:216e3ae7de"
        assert event.platform == "linux/amd64"
        assert event.provider == "docker"

    def test_minimal_event(self):
        env = {
            "DIUN_ENTRY_STATUS": "update",
            "DIUN_ENTRY_IMAGE": "docker.io/library/nginx:latest",
        }
        event = parse_env(env)
        assert event.status == "update"
        assert event.image_name == "docker.io/library/nginx"
        assert event.tag == "latest"
        assert event.hub_link == ""
        assert event.digest == ""

    def test_missing_status_raises(self):
        env = {"DIUN_ENTRY_IMAGE": "docker.io/foo/bar:v1"}
        with pytest.raises(DiunParseError, match="DIUN_ENTRY_STATUS"):
            parse_env(env)

    def test_missing_image_raises(self):
        env = {"DIUN_ENTRY_STATUS": "new"}
        with pytest.raises(DiunParseError, match="DIUN_ENTRY_IMAGE"):
            parse_env(env)

    def test_empty_env_raises(self):
        with pytest.raises(DiunParseError):
            parse_env({})

    def test_image_without_tag(self):
        env = {
            "DIUN_ENTRY_STATUS": "new",
            "DIUN_ENTRY_IMAGE": "docker.io/library/nginx",
        }
        event = parse_env(env)
        assert event.image_name == "docker.io/library/nginx"
        assert event.tag == "latest"


class TestDiunEvent:
    def test_image_name_strips_tag(self):
        e = DiunEvent(
            status="new", image="ghcr.io/foo/bar:v2.0.0",
            hub_link="", digest="", created="", platform="", provider="",
        )
        assert e.image_name == "ghcr.io/foo/bar"
        assert e.tag == "v2.0.0"

    def test_image_with_port(self):
        e = DiunEvent(
            status="new", image="registry.local:5000/myapp:1.0",
            hub_link="", digest="", created="", platform="", provider="",
        )
        assert e.image_name == "registry.local:5000/myapp"
        assert e.tag == "1.0"
