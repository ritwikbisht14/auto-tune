#!/usr/bin/env python3
"""Community-channel discovery providers for auto-tune.

Two sources, both zero-cost and no LLM in the loop:

  reddit   Reddit JSON API (r/ClaudeAI, r/LocalLLaMA, etc.) — extract every
           github.com / gist.github.com URL from post titles + selftext.
  awesome  Awesome-claude-code style aggregator lists — fetch raw markdown
           from each URL in security/aggregator_lists.txt and extract every
           github.com / gist.github.com link.

Output: a JSON list to stdout in the same uniform shape discover.py expects:
  {name, source_url, source_provider, description, fetched_at, raw_excerpt}

Discover.py merges this stream with its own GitHub + RSS + Grok results.

All URLs go through security.py before any fetch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SECURITY_DIR = SKILL_ROOT / "security"
AGGREGATOR_LISTS = SECURITY_DIR / "aggregator_lists.txt"

REDDIT_SUBS = ["ClaudeAI", "LocalLLaMA"]
REDDIT_QUERIES = {
    "designer": ["skill design", "skill ui", "agent designer"],
    "pm": ["skill product manager", "skill prd", "agent pm"],
    "engineer": ["skill code review", "skill testing", "agent dev"],
}

HN_QUERIES = {
    "designer": ["claude code skill design", "claude code ui", "claude agent figma"],
    "pm": ["claude code skill prd", "claude agent jira", "claude code product"],
    "engineer": ["claude code skill", "claude code subagent", "claude code review"],
}

GH_URL_RE = re.compile(r"https?://(?:gist\.github\.com|github\.com|raw\.githubusercontent\.com)/[A-Za-z0-9_.\-/?#=&]+")
TRAILING_PUNCT = re.compile(r"[)\].,;:!?'\"]+$")


def load_security():
    spec = importlib.util.spec_from_file_location("auto_tune_security", SKILL_ROOT / "scripts" / "security.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def extract_github_urls(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    for m in GH_URL_RE.finditer(text):
        url = TRAILING_PUNCT.sub("", m.group(0))
        if url.endswith(".git"):
            url = url[:-4]
        if url not in found:
            found.append(url)
    return found


def normalize_repo(url: str) -> tuple[str, str] | None:
    """Return (name, html_url) for a github.com repo URL. None if not a repo root."""
    m = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo in ("issues", "pulls", "blob", "tree", "wiki"):
        return None
    return (repo, f"https://github.com/{owner}/{repo}")


def reddit_search(role: str, security_mod, queries: list[str] | None = None) -> list[dict]:
    queries = queries or REDDIT_QUERIES.get(role, ["skill", "agent"])
    out: list[dict] = []
    headers = {"User-Agent": "auto-tune-community/0.3"}
    for sub in REDDIT_SUBS:
        for q in queries:
            qs = urllib.parse.quote(q)
            url = (
                f"https://www.reddit.com/r/{sub}/search.json?"
                f"q={qs}&restrict_sr=1&sort=new&t=month&limit=15"
            )
            gate = security_mod.check_url(url)
            if not gate["allowed"]:
                out.append({"_error": f"reddit:{sub}:{q}: blocked ({gate.get('reason')})"})
                continue
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
            except urllib.error.URLError as e:
                out.append({"_error": f"reddit:{sub}:{q}: {e}"})
                continue
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            for child in (data.get("data") or {}).get("children", []):
                d = (child.get("data") or {})
                title = (d.get("title") or "").strip()
                selftext = (d.get("selftext") or "")[:4000]
                permalink = "https://www.reddit.com" + (d.get("permalink") or "")
                urls = extract_github_urls(title + "\n" + selftext)
                for u in urls:
                    repo = normalize_repo(u) or (u.rsplit("/", 1)[-1] or "gist", u)
                    name = repo[0]
                    html = repo[1]
                    out.append({
                        "name": name[:60],
                        "source_url": html,
                        "html_url": html,
                        "source_provider": "reddit",
                        "description": (title[:220]),
                        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "raw_excerpt": f"sub=r/{sub} thread={permalink}",
                    })
    return out


def hn_search(role: str, security_mod, queries: list[str] | None = None) -> list[dict]:
    """Hacker News via Algolia public API. No key required."""
    queries = queries or HN_QUERIES.get(role, ["claude code skill"])
    out: list[dict] = []
    headers = {"User-Agent": "auto-tune-community/0.3"}
    seen: set[str] = set()
    for q in queries:
        qs = urllib.parse.quote(q)
        url = f"https://hn.algolia.com/api/v1/search?query={qs}&tags=story&hitsPerPage=20"
        gate = security_mod.check_url(url)
        if not gate["allowed"]:
            out.append({"_error": f"hn:{q}: blocked ({gate.get('reason')})"})
            continue
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            out.append({"_error": f"hn:{q}: {e}"})
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        for hit in (data.get("hits") or []):
            title = (hit.get("title") or "").strip()
            story_url = (hit.get("url") or "").strip()
            story_text = (hit.get("story_text") or "")[:4000]
            urls = extract_github_urls(title + " " + story_url + "\n" + story_text)
            for u in urls:
                if u in seen:
                    continue
                seen.add(u)
                repo = normalize_repo(u) or (u.rsplit("/", 1)[-1] or "gist", u)
                name, html = repo
                out.append({
                    "name": name[:60],
                    "source_url": html,
                    "html_url": html,
                    "source_provider": "hn",
                    "description": (title[:220]),
                    "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "raw_excerpt": f"hn_story=https://news.ycombinator.com/item?id={hit.get('objectID','')} points={hit.get('points','-')}",
                })
    return out


def awesome_lists(role: str, security_mod) -> list[dict]:
    if not AGGREGATOR_LISTS.is_file():
        return []
    feeds = [
        ln.strip()
        for ln in AGGREGATOR_LISTS.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    out: list[dict] = []
    seen: set[str] = set()
    headers = {"User-Agent": "auto-tune-community/0.3"}
    for url in feeds:
        gate = security_mod.check_url(url)
        if not gate["allowed"]:
            out.append({"_error": f"awesome:{url}: blocked ({gate.get('reason')})"})
            continue
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            out.append({"_error": f"awesome:{url}: {e}"})
            continue

        urls = extract_github_urls(body)
        # Pull preceding label text for each link so we can show a useful description
        for u in urls:
            repo = normalize_repo(u)
            if not repo:
                continue
            name, html = repo
            if html in seen:
                continue
            seen.add(html)
            label = ""
            m = re.search(rf"\[([^\]]+)\]\({re.escape(u)}", body)
            if m:
                label = m.group(1)[:200]
            out.append({
                "name": name[:60],
                "source_url": html,
                "html_url": html,
                "source_provider": "awesome",
                "description": label or f"Listed in {url}",
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "raw_excerpt": f"aggregator={url}",
            })
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=["reddit", "hn", "awesome", "all"], default="all")
    p.add_argument("--role", required=True)
    args = p.parse_args(argv)

    security = load_security()
    items: list[dict] = []
    if args.source in ("reddit", "all"):
        items += reddit_search(args.role, security)
    if args.source in ("hn", "all"):
        items += hn_search(args.role, security)
    if args.source in ("awesome", "all"):
        items += awesome_lists(args.role, security)

    json.dump({"items": items, "count": sum(1 for i in items if "_error" not in i)}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
