from __future__ import annotations

from nymeria.triggers.cli.rendering.tool_rows import (
    format_artifact_line,
    workspace_download_url,
)
from nymeria.triggers.cli.state import WorkspaceArtifact


def test_workspace_download_url_quotes_path():
    url = workspace_download_url("http://host:8000/", "/work space/c.png")
    assert url == "http://host:8000/workspace/download?path=%2Fwork%20space%2Fc.png"


def test_image_artifact_line_uses_download_url_with_base_url():
    art = WorkspaceArtifact(
        path="/workspace/u/img.png", name="img.png", mime_type="image/png", size_bytes=1234
    )
    line = format_artifact_line(art, width=300, base_url="http://host:8000")

    assert "http://host:8000/workspace/download?path=" in line
    assert "image/png" in line  # detail suffix preserved
    # Raw (unquoted) path is not shown as the label; it appears url-encoded.
    assert "/workspace/u/img.png" not in line


def test_image_artifact_line_falls_back_to_path_without_base_url():
    art = WorkspaceArtifact(
        path="/workspace/u/img.png", name="img.png", mime_type="image/png", size_bytes=1234
    )
    line = format_artifact_line(art, width=300)

    assert "/workspace/u/img.png" in line
    assert "download?path=" not in line


def test_non_image_artifact_line_keeps_path_even_with_base_url():
    art = WorkspaceArtifact(
        path="/workspace/u/data.csv", name="data.csv", mime_type="text/csv", size_bytes=10
    )
    line = format_artifact_line(art, width=300, base_url="http://host:8000")

    assert "/workspace/u/data.csv" in line
    assert "download?path=" not in line
