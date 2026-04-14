"""Tests for shiplog.diun — diun env var parsing."""

import pytest

from shiplog.diun import DiunEvent, DiunParseError, parse_env, split_image_ref


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


class TestSplitImageRef:
    def test_standard_image_with_tag(self):
        assert split_image_ref("docker.io/foo/bar:v1") == ("docker.io/foo/bar", "v1")

    def test_no_tag_defaults_to_latest(self):
        assert split_image_ref("docker.io/foo/bar") == ("docker.io/foo/bar", "latest")

    def test_port_with_tag(self):
        assert split_image_ref("registry.local:5000/app:v2") == ("registry.local:5000/app", "v2")

    def test_port_without_tag(self):
        assert split_image_ref("registry.local:5000/app") == ("registry.local:5000/app", "latest")

    def test_port_deep_path_with_tag(self):
        assert split_image_ref("registry.local:5000/org/app:v3") == ("registry.local:5000/org/app", "v3")

    def test_port_deep_path_without_tag(self):
        assert split_image_ref("registry.local:5000/org/app") == ("registry.local:5000/org/app", "latest")

    def test_simple_name_with_tag(self):
        assert split_image_ref("nginx:alpine") == ("nginx", "alpine")

    def test_simple_name_without_tag(self):
        assert split_image_ref("nginx") == ("nginx", "latest")

    def test_ghcr(self):
        assert split_image_ref("ghcr.io/owner/repo:sha-abc") == ("ghcr.io/owner/repo", "sha-abc")


class TestDiunEvent:
    def test_image_name_strips_tag(self):
        e = DiunEvent(
            status="new", image="ghcr.io/foo/bar:v2.0.0",
            hub_link="", digest="", created="", platform="", provider="",
        )
        assert e.image_name == "ghcr.io/foo/bar"
        assert e.tag == "v2.0.0"

    def test_image_with_port_and_tag(self):
        e = DiunEvent(
            status="new", image="registry.local:5000/myapp:1.0",
            hub_link="", digest="", created="", platform="", provider="",
        )
        assert e.image_name == "registry.local:5000/myapp"
        assert e.tag == "1.0"

    def test_image_with_port_no_tag(self):
        e = DiunEvent(
            status="new", image="registry.local:5000/myapp",
            hub_link="", digest="", created="", platform="", provider="",
        )
        assert e.image_name == "registry.local:5000/myapp"
        assert e.tag == "latest"

    def test_image_with_port_deep_path_and_tag(self):
        e = DiunEvent(
            status="new", image="registry.local:5000/org/app:v2",
            hub_link="", digest="", created="", platform="", provider="",
        )
        assert e.image_name == "registry.local:5000/org/app"
        assert e.tag == "v2"

    def test_image_with_port_deep_path_no_tag(self):
        e = DiunEvent(
            status="new", image="registry.local:5000/org/app",
            hub_link="", digest="", created="", platform="", provider="",
        )
        assert e.image_name == "registry.local:5000/org/app"
        assert e.tag == "latest"
