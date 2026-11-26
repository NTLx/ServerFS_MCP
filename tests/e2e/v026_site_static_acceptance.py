"""Offline static audit of the built ServerFS site; no network or browser claims."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

BASE = "/ServerFS_MCP/"
ORIGIN = "https://ntlx.github.io"


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.references: list[tuple[str, str]] = []
        self.lang = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "html":
            self.lang = values.get("lang", "")
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "a" and values.get("name"):
            self.ids.add(values["name"])
        if tag == "a" and values.get("href"):
            self.references.append(("link", values["href"]))
        if tag == "link" and values.get("href"):
            rel = set(values.get("rel", "").split())
            if rel & {"stylesheet", "icon", "preload", "modulepreload"}:
                self.references.append(("asset", values["href"]))
        if tag in {"img", "script", "source", "video", "audio"} and values.get("src"):
            self.references.append(("script" if tag == "script" else "asset", values["src"]))


def audit(dist: Path) -> dict:
    dist = dist.resolve(strict=True)
    pages = {}
    for path in sorted(dist.rglob("*.html")):
        if path.is_symlink() or not path.resolve().is_relative_to(dist):
            raise ValueError("unexpected HTML file outside build output")
        parser = Page()
        parser.feed(path.read_text(encoding="utf-8"))
        pages[path.relative_to(dist).as_posix()] = parser
    counts: Counter[str] = Counter()
    failures = []
    for name, page in pages.items():
        route = name.removesuffix("index.html") if name.endswith("index.html") else name
        for kind, value in page.references:
            url = urlsplit(urljoin(ORIGIN + BASE + route, value))
            if url.scheme not in {"http", "https"}:
                counts["non_http_reference"] += 1
                if url.scheme == "javascript":
                    failures.append({"page": name, "class": "javascript_url"})
                elif kind == "script":
                    failures.append({"page": name, "class": "non_http_script"})
                continue
            if url.netloc != "ntlx.github.io":
                counts["external_reference_unchecked"] += 1
                if kind == "script":
                    failures.append({"page": name, "class": "external_script"})
                continue
            decoded = unquote(url.path)
            if not decoded.startswith(BASE):
                failures.append({"page": name, "class": "outside_project_base"})
                continue
            target = (dist / decoded.removeprefix(BASE)).resolve()
            if not target.is_relative_to(dist):
                failures.append({"page": name, "class": "outside_build"})
                continue
            if target.is_dir():
                target = (target / "index.html").resolve()
            if not target.is_relative_to(dist):
                failures.append({"page": name, "class": "outside_build"})
                continue
            if not target.is_file():
                failures.append({"page": name, "class": "missing_" + kind})
                continue
            counts["local_" + kind + "_verified"] += 1
            if url.fragment and target.suffix == ".html":
                key = target.relative_to(dist).as_posix()
                fragment = unquote(url.fragment)
                if key not in pages or fragment not in pages[key].ids:
                    failures.append({"page": name, "class": "missing_anchor"})
                else:
                    counts["local_anchor_verified"] += 1
    required = {"index.html", "zh-cn/index.html", "404.html"}
    for name in sorted(required - pages.keys()):
        failures.append({"page": name, "class": "required_page_missing"})
    if "index.html" in pages and not pages["index.html"].lang.lower().startswith("en"):
        failures.append({"page": "index.html", "class": "locale_lang_mismatch"})
    if "zh-cn/index.html" in pages and not pages["zh-cn/index.html"].lang.lower().startswith("zh"):
        failures.append({"page": "zh-cn/index.html", "class": "locale_lang_mismatch"})
    return {
        "scope": "static_html_references",
        "page_count": len(pages),
        "english_pages": sum(not x.startswith("zh-cn/") for x in pages),
        "chinese_pages": sum(x.startswith("zh-cn/") for x in pages),
        "counts": dict(counts),
        "failure_counts": dict(Counter(row["class"] for row in failures)),
        "failed_pages": sorted({row["page"] for row in failures}),
        "status": "PASS" if pages and not failures else "FAIL",
        "browser_rendering_verified": False,
        "external_links_verified": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()
    result = audit(args.dist)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(int(result["status"] != "PASS"))
