"""Deterministic regressions for the static documentation-site audit."""

from __future__ import annotations

from pathlib import Path

import pytest
from v026_site_static_acceptance import audit


def make_site(root: Path, *, english: str = "") -> Path:
    root.mkdir()
    (root / "zh-cn").mkdir()
    (root / "guide").mkdir()
    (root / "assets").mkdir()
    (root / "index.html").write_text(
        '<html lang="en"><a href="zh-cn/">中文</a>'
        '<a href="guide/#start">Guide</a>'
        '<link rel="stylesheet" href="/ServerFS_MCP/assets/site.css">'
        '<script src="assets/site.js"></script>' + english + "</html>",
        encoding="utf-8",
    )
    (root / "zh-cn/index.html").write_text(
        '<html lang="zh-CN"><a href="../">English</a></html>', encoding="utf-8"
    )
    (root / "guide/index.html").write_text(
        '<html lang="en"><h1 id="start">Guide</h1></html>', encoding="utf-8"
    )
    (root / "404.html").write_text('<html lang="en">Not found</html>', encoding="utf-8")
    (root / "assets/site.css").write_text("body{margin:0}", encoding="utf-8")
    (root / "assets/site.js").write_text("void 0", encoding="utf-8")
    return root


def test_static_audit_internal_references(tmp_path: Path) -> None:
    result = audit(
        make_site(tmp_path / "dist", english='<a href="https://example.org/">external</a>')
    )
    assert result["status"] == "PASS"
    assert result["page_count"] == 4
    assert result["counts"]["local_link_verified"] == 3
    assert result["counts"]["local_anchor_verified"] == 1
    assert result["counts"]["external_reference_unchecked"] == 1
    assert not result["browser_rendering_verified"]
    assert not result["external_links_verified"]


@pytest.mark.parametrize(
    ("markup", "category"),
    [
        ('<a href="missing/">broken</a>', "missing_link"),
        ('<a href="guide/#missing">broken</a>', "missing_anchor"),
        ('<img src="assets/missing.svg">', "missing_asset"),
        ('<a href="/outside/">outside</a>', "outside_project_base"),
        ('<a href="/ServerFS_MCP/%2e%2e/outside.html">outside</a>', "outside_build"),
        ('<script src="https://example.org/script.js"></script>', "external_script"),
        ('<script src="data:text/javascript,void%200"></script>', "non_http_script"),
        ('<a href="javascript:void(0)">unsafe</a>', "javascript_url"),
    ],
)
def test_static_audit_rejects_broken_or_unapproved_references(
    tmp_path: Path, markup: str, category: str
) -> None:
    result = audit(make_site(tmp_path / "dist", english=markup))
    assert result["status"] == "FAIL"
    assert result["failure_counts"] == {category: 1}
    assert result["failed_pages"] == ["index.html"]


def test_static_audit_requires_bilingual_routes(tmp_path: Path) -> None:
    root = make_site(tmp_path / "dist")
    (root / "zh-cn/index.html").unlink()
    (root / "404.html").unlink()
    result = audit(root)
    assert result["status"] == "FAIL"
    assert result["failure_counts"]["required_page_missing"] == 2


def test_static_audit_rejects_external_html_symlink(tmp_path: Path) -> None:
    root = make_site(tmp_path / "dist")
    external = tmp_path / "external.html"
    external.write_text("<html></html>", encoding="utf-8")
    try:
        (root / "outside.html").symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="outside build output"):
        audit(root)
