#!/usr/bin/env python3
"""Discovery providers for auto-tune.

Providers (any subset selectable via --providers):
  github   GitHub repository search API
  rss      Atom/RSS feeds listed in security/curators.txt
  grok     xAI Grok chat completions (skipped if XAI_API_KEY unset)
  web      Placeholder; actual WebSearch is orchestrated by SKILL.md.
           If --web-results <path> is supplied, ingest a JSON file the
           orchestrator wrote with rows {name, source_url, description}.

Every URL passes through security.py before fetch. Fetched bodies are
staged in security/quarantine/<sha>/ and scanned. Only candidates whose
content scan is `clean` are written to cache/candidates.json.

Usage:
  discover.py --role designer --providers github,rss [--dry-run]
              [--web-results /tmp/web.json] [--out cache/candidates.json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = SKILL_ROOT / "cache"
SECURITY_DIR = SKILL_ROOT / "security"
CURATORS_PATH = SECURITY_DIR / "curators.txt"
CURATORS_SYNCED_PATH = SECURITY_DIR / "curators_synced.txt"
TRUSTED_AUTHORS_PATH = SECURITY_DIR / "trusted_authors.txt"
TRUSTED_REPOS_PATH = SECURITY_DIR / "trusted_repos.txt"
WHITELISTED_AUTHORS_PATH = SECURITY_DIR / "whitelisted_authors.txt"

TRUSTED_RELEVANCE_BOOST = 0.4
TRUSTED_MIN_RELEVANCE = 0.1

# Spam-username heuristics: bot-pattern usernames flagged in sandbox testing
SPAM_USERNAME_PATTERNS = [
    re.compile(r"^[A-Za-z]+\d{3,}$"),                      # ostensiblemeeting210, Janianorthkorean166
    re.compile(r"^[A-Z][a-z]+[A-Z][a-z]+\d{2,}$"),         # Ridingbittknightsservice6966 style
    re.compile(r"^[A-Z][a-z]+[a-z]+\d{3,}$"),              # mixed-case + trailing digits
    re.compile(r"^[a-z]{18,}\d{2,}$"),                     # long lowercase + digit suffix
    # v5.3 additions: catch noun-noun mashups and timestamp-like digit suffixes
    re.compile(r"^[A-Z][a-z]{3,}[A-Z][a-z]{3,}\d{4,6}$"),  # CamelCaseTwoWordsLongerDigits
    re.compile(r"^[a-z]+[A-Z][a-z]+\d{3,}$"),              # mixedCase with no separator + digits
    re.compile(r"^[A-Za-z]{4,}-\d{4,}$"),                  # name-timestamp pattern
]
README_CLAUDE_MARKERS = (
    "skill.md", "claude code", "claude-code", "frontmatter:", "agent skill",
    "claude desktop", "anthropic", "sub-agent", "subagent",
)
# v5.3: README must contain at least one of these to count as "substantive."
# A repo whose README only mentions Claude in passing isn't a skill.
README_SUBSTANCE_MARKERS = (
    "## install",
    "## usage",
    "## getting started",
    "name:\n",  # frontmatter line
    "description:\n",
    "---\nname:",
    "---\ndescription:",
)
# v5.3: GitHub tool names that, when referenced in a README, indicate the repo
# is actually integrating with Claude Code's tool surface (not just topic-tagged).
CLAUDE_TOOL_NAMES = (
    "skill tool", "agent tool", "webfetch", "websearch", "tooluse",
    "mcp__", "claude_ai_",
)

ROLE_KEYWORDS = {
    "designer": {"design", "figma", "ui", "ux", "css", "tailwind", "accessibility", "motion", "spec", "component", "layout", "color", "typography", "prototype"},
    "pm": {"prd", "ticket", "jira", "linear", "requirement", "stakeholder", "roadmap", "epic", "story", "spec", "product"},
    "engineer": {"implement", "refactor", "test", "build", "ci", "deploy", "bug", "fix", "compile", "performance", "security", "lint", "format"},
}


def load_security():
    spec = importlib.util.spec_from_file_location("auto_tune_security", SKILL_ROOT / "scripts" / "security.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_community():
    spec = importlib.util.spec_from_file_location("auto_tune_community", SKILL_ROOT / "scripts" / "community.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def role_relevance(text: str, role: str) -> float:
    text = (text or "").lower()
    if not text:
        return 0.0
    kws = ROLE_KEYWORDS.get(role, set())
    if not kws:
        return 0.0
    hits = sum(1 for kw in kws if kw in text)
    return min(1.0, hits / 4.0)


def load_list(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.add(s.lower())
    return out


def author_of(url: str) -> str:
    m = re.search(r"github\.com/([^/]+)/", url or "")
    return m.group(1).lower() if m else ""


def repo_full_name(url: str) -> str:
    m = re.search(r"github\.com/([^/]+)/([^/]+)", url or "")
    if not m:
        return ""
    return f"{m.group(1).lower()}/{m.group(2).lower()}"


def is_spam_username(username: str, whitelisted: set[str]) -> bool:
    if not username:
        return False
    if username.lower() in whitelisted:
        return False
    return any(p.match(username) for p in SPAM_USERNAME_PATTERNS)


def readme_self_describes_claude(body: str) -> bool:
    if not body:
        return False
    low = body.lower()
    return any(marker in low for marker in README_CLAUDE_MARKERS)


def readme_has_substance(body: str) -> bool:
    """v5.3 hard filter: README must show structural depth, not just mention Claude.

    Returns True when the README contains either:
      - A YAML frontmatter block declaring name/description
      - An '## Install' or '## Usage' heading (or close variant)
      - A reference to an actual Claude Code tool name
    """
    if not body:
        return False
    low = body.lower()
    has_marker = any(marker in low for marker in README_SUBSTANCE_MARKERS)
    has_tool_ref = any(tn in low for tn in CLAUDE_TOOL_NAMES)
    return has_marker or has_tool_ref


def created_pushed_gap_suspicious(item: dict) -> bool:
    """v5.3 hard filter: repos created and pushed on the same day with <3 stars
    are almost always bot-farmed. Trusted-author candidates bypass this upstream.
    """
    created = item.get("created_at")
    pushed = item.get("pushed_at") or item.get("updated_at")
    stars = int(item.get("stargazers_count") or 0)
    if not created or not pushed or stars >= 3:
        return False
    try:
        c = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        p = dt.datetime.fromisoformat(pushed.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (p - c).total_seconds() < 86400  # <24h


def passes_file_count_floor(insp: dict | None) -> bool:
    """v5.3 hard filter: a real skill has at least a SKILL.md plus some scaffolding.

    Uses the deep-inspect result. If we don't have inspection data yet, returns
    True (we don't drop on missing data; only on confirmed absence).
    """
    if not insp:
        return True
    # If we have skill_md_path, that's substance enough.
    if insp.get("skill_md_path"):
        return True
    # Otherwise look for any of the structural dirs we recognize.
    if insp.get("examples_dir") or insp.get("tests_dir") or insp.get("scripts_dir"):
        return True
    if insp.get("has_releases"):
        return True
    return False


def has_skill_substance(insp: dict | None) -> bool:
    """v5.3 hard filter: candidate's repo must contain an actual SKILL.md or
    similar structural marker. README-only repos are not skills."""
    if not insp:
        return True  # Don't drop on missing data
    return bool(insp.get("skill_md_path"))


def fork_ratio_penalty(item: dict) -> float:
    """v5.3 soft signal: forks > stars indicates people copied without engaging.
    Returns 0.0 (no penalty) or negative number (penalty value)."""
    stars = int(item.get("stargazers_count") or 0)
    forks = int(item.get("forks_count") or 0)
    if stars <= 5 or forks == 0:
        return 0.0
    if forks / stars > 1.0:
        return -0.10
    return 0.0


def active_maintenance_bonus(item: dict) -> float:
    """v5.3 soft signal: +0.05 if pushed within last 90 days (active dev)."""
    pushed = item.get("pushed_at") or item.get("updated_at")
    if not pushed:
        return 0.0
    try:
        p = dt.datetime.fromisoformat(pushed.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0
    days = (dt.datetime.now(dt.timezone.utc) - p).total_seconds() / 86400.0
    return 0.05 if days <= 90 else 0.0


def issue_engagement_bonus(item: dict) -> float:
    """v5.3 soft signal: +0.05 if repo has any open issues (community uses it)."""
    open_issues = int(item.get("open_issues_count") or 0)
    return 0.05 if open_issues > 0 else 0.0


FEEDBACK_HISTORY_PATH = CACHE_DIR / "feedback_history.json"


def load_feedback_history() -> dict:
    """v5.3 feedback loop: load the user's install/reject history."""
    if not FEEDBACK_HISTORY_PATH.is_file():
        return {"installs": [], "rejections": []}
    try:
        return json.loads(FEEDBACK_HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"installs": [], "rejections": []}


def feedback_adjustment(candidate: dict, history: dict) -> float:
    """v5.3 feedback loop: shift quality_score based on past user decisions.

    +0.20 if author appears in installs (user already trusts them)
    +0.10 if candidate's facet matches a previously-installed facet
    -0.30 if this exact source_url is in rejections
    -0.15 if author appears in rejections (for a different candidate)
    """
    author = author_of(candidate.get("html_url") or candidate.get("source_url") or "")
    src = candidate.get("source_url") or candidate.get("html_url") or ""
    src_norm = src.rstrip("/").lower()

    installs = history.get("installs", [])
    rejections = history.get("rejections", [])

    install_authors = {(e.get("author") or "").lower() for e in installs if e.get("author")}
    install_facets = {e.get("facet") for e in installs if e.get("facet")}
    reject_urls = {(e.get("source_url") or "").rstrip("/").lower() for e in rejections}
    reject_authors = {(e.get("author") or "").lower() for e in rejections if e.get("author")}

    adj = 0.0
    if src_norm in reject_urls:
        adj -= 0.30  # User explicitly rejected this exact candidate
    if author and author in install_authors:
        adj += 0.20  # Author has a winning track record
    elif author and author in reject_authors:
        adj -= 0.15  # Mild author-level downweight
    cand_facet = candidate.get("facet")
    if cand_facet and cand_facet in install_facets:
        adj += 0.10  # User cares about this facet
    return adj


def popularity_factor(popularity: int, kind: str) -> float:
    """Map a raw popularity number to 0..1 with a log curve so a few stars/upvotes
    move the needle but viral hits don't dominate."""
    import math
    if not popularity or popularity <= 0:
        return 0.0
    saturation = {
        "stars": 500,
        "reddit_upvotes": 200,
        "hn_points": 150,
    }.get(kind, 200)
    return min(1.0, math.log1p(popularity) / math.log1p(saturation))


def recency_factor(iso_ts: str | None) -> float:
    """1.0 for activity within the last 14 days, decaying linearly to 0 over 365 days."""
    if not iso_ts:
        return 0.3
    try:
        ts = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.3
    now = dt.datetime.now(dt.timezone.utc)
    days = (now - ts).total_seconds() / 86400.0
    if days <= 14:
        return 1.0
    if days >= 365:
        return 0.0
    return max(0.0, 1.0 - (days - 14) / (365 - 14))


def quality_score(role_rel: float, popularity: int, popularity_kind: str,
                  last_activity_at: str | None, trusted_author: bool,
                  extra_signals: dict | None = None) -> dict:
    """Compute a 0..1 quality score for a candidate.

    Base formula (v3): 0.40*role_rel + 0.25*pop + 0.20*rec + 0.15*trust

    v5.3 extras (passed via `extra_signals`):
      cross_provider_count   number of providers that surfaced this candidate (>=1)
      fork_ratio_penalty     pre-computed via fork_ratio_penalty() (<=0)
      active_maintenance     pre-computed via active_maintenance_bonus() (>=0)
      issue_engagement       pre-computed via issue_engagement_bonus() (>=0)
      feedback_adjustment    pre-computed via feedback_adjustment() (any sign)
      structure_score        pre-computed via structure_score() (0..1)
    """
    rel = role_rel
    pop = popularity_factor(popularity, popularity_kind)
    rec = recency_factor(last_activity_at)
    trust = 1.0 if trusted_author else 0.0
    base = 0.40 * rel + 0.25 * pop + 0.20 * rec + 0.15 * trust

    extras = extra_signals or {}
    cross = extras.get("cross_provider_count", 1)
    cross_boost = 0.10 if cross == 2 else (0.15 if cross >= 3 else 0.0)
    fork_pen = float(extras.get("fork_ratio_penalty", 0.0))
    activity = float(extras.get("active_maintenance", 0.0))
    issues = float(extras.get("issue_engagement", 0.0))
    fb = float(extras.get("feedback_adjustment", 0.0))
    structure = float(extras.get("structure_score", 0.0))

    # Structure is additive, capped at 0.20 contribution.
    structure_contrib = min(0.20, structure * 0.20)

    weighted = base + cross_boost + fork_pen + activity + issues + fb + structure_contrib
    weighted = max(0.0, min(1.0, weighted))

    return {
        "score": round(weighted, 3),
        "components": {
            "role_relevance": round(rel, 3),
            "popularity": round(pop, 3),
            "recency": round(rec, 3),
            "trust": trust,
            "cross_provider_boost": round(cross_boost, 3),
            "fork_ratio_penalty": round(fork_pen, 3),
            "active_maintenance_bonus": round(activity, 3),
            "issue_engagement_bonus": round(issues, 3),
            "feedback_adjustment": round(fb, 3),
            "structure_contrib": round(structure_contrib, 3),
        },
    }


SKILL_DIR_NAMES = {"skills", "skill", "agents", "agent-skills"}
EXAMPLES_DIR_NAMES = {"examples", "example", "demos", "demo", "samples"}
TESTS_DIR_NAMES = {"tests", "test", "__tests__", "spec", "specs"}


def _gh_get_contents(owner: str, repo: str, path: str, security_mod) -> tuple[list | dict | None, str | None]:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}".rstrip("/")
    if not security_mod.check_url(url)["allowed"]:
        return None, "blocked"
    data, err = _github_request(url, use_token=True)
    if err and err.startswith("HTTP 401"):
        data, err = _github_request(url, use_token=False)
    return data, err


def deep_inspect_github(html_url: str, security_mod) -> dict | None:
    """Fetch a tiny amount of repo structure (one or two API calls) to know
    whether the candidate is *actually* a skill, not just a tagged repo."""
    m = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+)", html_url or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)

    out = {
        "owner": owner,
        "repo": repo,
        "skill_md_path": None,
        "skill_md_frontmatter": None,
        "examples_dir": False,
        "tests_dir": False,
        "scripts_dir": False,
        "has_releases": False,
        "release_latest_tag": None,
    }

    contents, err = _gh_get_contents(owner, repo, "", security_mod)
    if err or not isinstance(contents, list):
        return out

    skills_dir_path: str | None = None
    for entry in contents:
        nm = (entry.get("name") or "").lower()
        et = entry.get("type")
        if nm == "skill.md" and et == "file":
            out["skill_md_path"] = entry.get("path")
        elif et == "dir":
            if nm in SKILL_DIR_NAMES:
                skills_dir_path = entry.get("path")
            elif nm in EXAMPLES_DIR_NAMES:
                out["examples_dir"] = True
            elif nm in TESTS_DIR_NAMES:
                out["tests_dir"] = True
            elif nm == "scripts":
                out["scripts_dir"] = True

    if not out["skill_md_path"] and skills_dir_path:
        sub, sub_err = _gh_get_contents(owner, repo, skills_dir_path, security_mod)
        if not sub_err and isinstance(sub, list):
            for entry in sub:
                if entry.get("type") == "dir":
                    sk_path = f"{entry.get('path')}/SKILL.md"
                    sk, sk_err = _gh_get_contents(owner, repo, sk_path, security_mod)
                    if not sk_err and isinstance(sk, dict) and sk.get("type") == "file":
                        out["skill_md_path"] = sk.get("path")
                        break
                elif entry.get("type") == "file" and (entry.get("name") or "").lower() == "skill.md":
                    out["skill_md_path"] = entry.get("path")
                    break

    releases_url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=1"
    if security_mod.check_url(releases_url)["allowed"]:
        rdata, rerr = _github_request(releases_url, use_token=True)
        if rerr and rerr.startswith("HTTP 401"):
            rdata, rerr = _github_request(releases_url, use_token=False)
        if not rerr and isinstance(rdata, list) and rdata:
            out["has_releases"] = True
            out["release_latest_tag"] = (rdata[0] or {}).get("tag_name")

    return out


def structure_score(insp: dict | None) -> float:
    if not insp:
        return 0.0
    s = 0.0
    if insp.get("skill_md_path"):
        s += 0.55
    if insp.get("examples_dir"):
        s += 0.12
    if insp.get("tests_dir"):
        s += 0.12
    if insp.get("scripts_dir"):
        s += 0.08
    if insp.get("has_releases"):
        s += 0.13
    return min(1.0, s)


def _github_request(url: str, use_token: bool) -> tuple[dict | None, str | None]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "auto-tune-discover/0.2"}
    if use_token:
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return None, str(e)


def _gh_item_to_candidate(item: dict, source_provider: str, extra_meta: dict | None = None) -> dict:
    name = item.get("name") or item.get("full_name", "")
    full_name = item.get("full_name", "")
    default_branch = item.get("default_branch") or "HEAD"
    readme_url = (
        f"https://raw.githubusercontent.com/{full_name}/{default_branch}/README.md"
        if full_name else item.get("html_url")
    )
    pushed_at = item.get("pushed_at") or item.get("updated_at")
    return {
        "name": name,
        "source_url": readme_url,
        "html_url": item.get("html_url"),
        "source_provider": source_provider,
        "description": (item.get("description") or "")[:280],
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "popularity": int(item.get("stargazers_count") or 0),
        "popularity_kind": "stars",
        "last_activity_at": pushed_at,
        "created_at": item.get("created_at"),  # v5.3: enables created-vs-pushed gap filter
        "stars_count": int(item.get("stargazers_count") or 0),  # v5.3: explicit field
        "forks_count": int(item.get("forks_count") or 0),  # v5.3: explicit field
        "open_issues_count": int(item.get("open_issues_count") or 0),  # v5.3
        "topics": item.get("topics", [])[:10],
        "forks": int(item.get("forks_count") or 0),
        "open_issues": int(item.get("open_issues_count") or 0),
        "has_license": bool(item.get("license")),
        "raw_excerpt": json.dumps({
            "stars": item.get("stargazers_count"),
            "topics": item.get("topics", []),
            **(extra_meta or {}),
        })[:200],
    }


CURATED_SEEDS_PATH = SECURITY_DIR / "curated_seeds.json"


def curated_seeds_provider(role: str, security_mod) -> list[dict]:
    """v5.3 provider: emit editorial picks from security/curated_seeds.json.

    These bypass quality scoring (score=1.0) so they always appear in the
    proposal's facet groupings as alternatives the user can consider. The
    `why` field becomes their rationale.
    """
    if not CURATED_SEEDS_PATH.is_file():
        return []
    try:
        data = json.loads(CURATED_SEEDS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    role_seeds = data.get(role) or {}
    if not isinstance(role_seeds, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for facet_name, entries in role_seeds.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            repo = (entry.get("repo") or "").strip()
            if not repo or "/" not in repo or repo in seen:
                continue
            seen.add(repo)
            owner, name = repo.split("/", 1)
            html_url = f"https://github.com/{owner}/{name}"
            # We don't fetch here — propose.py / compose.py can deep-inspect
            # later if needed. We just declare the candidate exists.
            out.append({
                "name": name,
                "source_url": f"https://raw.githubusercontent.com/{repo}/HEAD/README.md",
                "html_url": html_url,
                "source_provider": "curated",
                "description": entry.get("why") or f"Curated pick for {role}/{facet_name}",
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "popularity": 0,
                "popularity_kind": "stars",
                "last_activity_at": None,
                "topics": [],
                "facet_hint": facet_name,
                "added_by_user": bool(entry.get("added_by_user")),
                "quality_score": 1.0,
                "_curated_rationale": entry.get("why", ""),
            })
    return out


def github_trusted_author_search(role: str, security_mod, trusted_authors: set[str], limit: int = 6) -> list[dict]:
    """Find more skills from authors the user has already vouched for.

    Strategy: query `user:<author>+claude` to find their other repos. This
    sidesteps the topic-tag spam problem entirely — we ask GitHub for what
    a known-good account has pushed.
    """
    results: list[dict] = []
    if not trusted_authors:
        return results
    for author in sorted(trusted_authors):
        q = f"user:{author}+claude"
        url = f"https://api.github.com/search/repositories?q={q}&sort=updated&per_page={limit}"
        gate = security_mod.check_url(url)
        if not gate["allowed"]:
            continue
        data, err = _github_request(url, use_token=True)
        if err and err.startswith("HTTP 401"):
            data, err = _github_request(url, use_token=False)
        if err:
            results.append({"_error": f"github-trusted:{author}:{err}"})
            continue
        for item in (data.get("items") or [])[:limit]:
            results.append(_gh_item_to_candidate(item, "github-trusted", {"trusted_author": author}))
    return results


def github_search(role: str, security_mod, limit: int = 8) -> list[dict]:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180)).strftime("%Y-%m-%d")
    topics = ["claude-skill", "claude-code", "claude-agent", "agent-skill"]
    results: list[dict] = []

    role_term = {"designer": "design+OR+ui", "pm": "product+OR+pm", "engineer": "engineering+OR+code"}.get(role, role)
    for topic in topics:
        q = f"topic:{topic}+{role_term}+pushed:>{cutoff}"
        url = f"https://api.github.com/search/repositories?q={q}&sort=updated&per_page=5"
        gate = security_mod.check_url(url)
        if not gate["allowed"]:
            continue

        data, err = _github_request(url, use_token=True)
        if err and err.startswith("HTTP 401"):
            results.append({"_warning": f"github:{topic}: token invalid, retrying anonymously"})
            data, err = _github_request(url, use_token=False)
        if err:
            results.append({"_error": f"github:{topic}:{err}"})
            continue
        for item in (data.get("items") or [])[:limit]:
            results.append(_gh_item_to_candidate(item, "github"))
    return results


def rss_search(role: str, security_mod, limit_per_feed: int = 5) -> list[dict]:
    feeds: list[str] = []
    for path in (CURATORS_PATH, CURATORS_SYNCED_PATH):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                feeds.append(line)
    # Dedupe while preserving order (static seeds first, user-synced second).
    seen: set[str] = set()
    feeds = [f for f in feeds if not (f in seen or seen.add(f))]
    if not feeds:
        return []
    results: list[dict] = []
    for url in feeds:
        gate = security_mod.check_url(url)
        if not gate["allowed"]:
            results.append({"_error": f"rss:blocked:{url}:{gate.get('reason')}"})
            continue
        req = urllib.request.Request(url, headers={"User-Agent": "auto-tune-discover/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            results.append({"_error": f"rss:{url}:{e}"})
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns)[:limit_per_feed]:
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            summary_el = entry.find("atom:summary", ns) or entry.find("atom:content", ns)
            title = (title_el.text or "").strip() if title_el is not None else ""
            link = link_el.get("href") if link_el is not None else ""
            summary = (summary_el.text or "").strip()[:280] if summary_el is not None else ""
            results.append({
                "name": title,
                "source_url": link,
                "source_provider": "rss",
                "description": summary,
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "raw_excerpt": f"feed={url}",
            })
    return results


GROK_PROMPT_PATH = SKILL_ROOT / "prompts" / "grok_xsearch.md"
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = os.environ.get("XAI_MODEL", "grok-4-latest")
GH_URL_RE = re.compile(r"https?://(?:gist\.github\.com|github\.com|raw\.githubusercontent\.com)/[A-Za-z0-9_.\-/?#=&]+")
TRAILING_PUNCT_RE = re.compile(r"[)\].,;:!?'\"]+$")


def load_grok_prompt(role: str) -> str:
    if not GROK_PROMPT_PATH.is_file():
        return (
            f"Search X for posts in the last 30 days mentioning Claude Code skills "
            f"useful for a {role}. Return a JSON array of objects with keys "
            f"name, description, author_handle, x_post_url, posted_date, urls. "
            f"Return [] if uncertain."
        )
    text = GROK_PROMPT_PATH.read_text(encoding="utf-8")
    # Strip the doc preamble (everything up to the first ---)
    body = text.split("---", 1)[1] if "---" in text else text
    return body.replace("{{ROLE}}", role).strip()


def _extract_github_urls(text: str) -> list[str]:
    found: list[str] = []
    for m in GH_URL_RE.finditer(text or ""):
        u = TRAILING_PUNCT_RE.sub("", m.group(0))
        if u.endswith(".git"):
            u = u[:-4]
        if u not in found:
            found.append(u)
    return found


def grok_search(role: str, security_mod) -> list[dict]:
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return [{"_warning": "grok: XAI_API_KEY not set; skipping X-search provider"}]
    gate = security_mod.check_url(GROK_API_URL)
    if not gate["allowed"]:
        return [{"_error": f"grok:blocked:{gate.get('reason')}"}]

    prompt = load_grok_prompt(role)
    payload = {
        "model": GROK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        GROK_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "auto-tune-discover/0.3",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as e:
        return [{"_error": f"grok:{e}"}]
    except json.JSONDecodeError:
        return [{"_error": "grok: non-JSON response"}]

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return [{"_error": "grok: malformed response"}]

    m = re.search(r"\[.*\]", content, re.DOTALL)
    if not m:
        return [{"_warning": "grok: no JSON array in response"}]
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [{"_error": "grok: array parse failed"}]

    out: list[dict] = []
    seen_urls: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        urls = it.get("urls") or []
        if isinstance(urls, str):
            urls = _extract_github_urls(urls)
        elif isinstance(urls, list):
            flat: list[str] = []
            for u in urls:
                if isinstance(u, str):
                    flat += _extract_github_urls(u)
            urls = flat
        else:
            urls = []
        # Also harvest URLs from description in case Grok inlined them
        urls += _extract_github_urls(it.get("description") or "")
        if not urls:
            continue
        post_url = it.get("x_post_url") or it.get("post_url") or ""
        for u in urls:
            if u in seen_urls:
                continue
            seen_urls.add(u)
            # Get a sensible name from the URL
            slug = u.rstrip("/").rsplit("/", 1)[-1] or it.get("name", "")
            out.append({
                "name": (it.get("name") or slug)[:60],
                "source_url": u,
                "html_url": u,
                "source_provider": "grok",
                "description": (it.get("description") or "")[:280],
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "raw_excerpt": f"x_author={it.get('author_handle','')} x_post={post_url} posted={it.get('posted_date','')}",
                "_first_seen_via_x": True,
            })
    return out


def ingest_web_results(path: str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return [{"_error": f"web: file not found: {path}"}]
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [{"_error": f"web: bad json: {e}"}]
    out: list[dict] = []
    for r in rows if isinstance(rows, list) else []:
        out.append({
            "name": r.get("name", ""),
            "source_url": r.get("source_url", ""),
            "source_provider": "websearch",
            "description": (r.get("description") or "")[:280],
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "raw_excerpt": r.get("excerpt", "")[:200],
        })
    return out


def dedupe(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for it in items:
        if "_error" in it or "_warning" in it:
            out.append(it)
            continue
        key = (it.get("name", "").strip().lower(), (it.get("source_url") or "").strip().lower())
        if key in seen or not key[0]:
            continue
        seen.add(key)
        out.append(it)
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", required=True)
    p.add_argument("--providers", default="github,rss",
                   help="comma-separated subset of: github, rss, grok, web")
    p.add_argument("--web-results", default=None)
    p.add_argument("--out", default=str(CACHE_DIR / "candidates.json"))
    p.add_argument("--dry-run", action="store_true",
                   help="run providers but skip body-fetch/quarantine step")
    args = p.parse_args(argv)

    security = load_security()
    providers = {pr.strip() for pr in args.providers.split(",") if pr.strip()}
    trusted_authors = load_list(TRUSTED_AUTHORS_PATH)
    trusted_repos = load_list(TRUSTED_REPOS_PATH)
    whitelisted = load_list(WHITELISTED_AUTHORS_PATH)

    raw: list[dict] = []
    community_mod = None
    if any(p in providers for p in ("reddit", "hn", "awesome", "community")):
        community_mod = load_community()
    if "github" in providers:
        raw += github_trusted_author_search(args.role, security, trusted_authors)
        raw += github_search(args.role, security)
    if "rss" in providers:
        raw += rss_search(args.role, security)
    if "grok" in providers:
        raw += grok_search(args.role, security)
    if community_mod and ("reddit" in providers or "community" in providers):
        raw += community_mod.reddit_search(args.role, security)
    if community_mod and ("hn" in providers or "community" in providers):
        raw += community_mod.hn_search(args.role, security)
    if community_mod and ("awesome" in providers or "community" in providers):
        raw += community_mod.awesome_lists(args.role, security)
    if "web" in providers and args.web_results:
        raw += ingest_web_results(args.web_results)

    # v5.3: include curated seeds as a provider (always-on, no fetch needed)
    raw += curated_seeds_provider(args.role, security)

    merged = dedupe(raw)
    candidates: list[dict] = []
    diagnostics: list[dict] = [it for it in merged if "_error" in it or "_warning" in it]
    cleaned = [it for it in merged if "_error" not in it and "_warning" not in it]

    # v5.3: cross-provider corroboration — count distinct providers per dedup'd repo
    provider_counts: dict[str, set[str]] = {}
    for it in cleaned:
        repo = repo_full_name(it.get("html_url") or it.get("source_url") or "")
        if repo:
            provider_counts.setdefault(repo, set()).add(it.get("source_provider", "unknown"))

    # v5.3: load feedback history once for the scoring loop
    feedback_history = load_feedback_history()

    for it in cleaned:
        text_for_score = f"{it.get('name','')} {it.get('description','')}"
        rel = role_relevance(text_for_score, args.role)

        candidate_url = it.get("html_url") or it.get("source_url") or ""
        candidate_author = author_of(candidate_url)
        candidate_repo = repo_full_name(candidate_url)
        is_trusted_author = bool(candidate_author and candidate_author in trusted_authors)
        is_trusted_repo = bool(candidate_repo and candidate_repo in trusted_repos)
        if is_trusted_repo:
            continue
        if is_trusted_author:
            rel = min(1.0, rel + TRUSTED_RELEVANCE_BOOST)
            it["trusted_author_boost"] = True

        if not is_trusted_author and is_spam_username(candidate_author, whitelisted):
            diagnostics.append({"_spam_username": candidate_url, "author": candidate_author})
            continue

        # v5.3 hard filter: drop bot-farmed repos (created+pushed same day, <3 stars)
        if not is_trusted_author and provider_name != "curated" and created_pushed_gap_suspicious(it):
            diagnostics.append({"_created_pushed_gap": candidate_url, "author": candidate_author})
            continue

        provider_name = it.get("source_provider", "")
        is_curated_source = provider_name in ("reddit", "hn", "awesome", "rss")
        if is_trusted_author:
            threshold = TRUSTED_MIN_RELEVANCE
        elif is_curated_source:
            threshold = 0.15
        else:
            threshold = 0.3
        if rel < threshold:
            diagnostics.append({"_below_threshold": candidate_url, "rel": round(rel, 3), "threshold": threshold, "provider": provider_name})
            continue
        url = it.get("source_url") or ""
        if not url:
            continue

        quarantine_status = "skipped(dry-run)"
        sha256 = None
        findings: list[dict] = []
        if not args.dry_run:
            gate = security.check_url(url)
            if not gate["allowed"]:
                diagnostics.append({"_blocked": url, "reason": gate.get("reason")})
                continue
            try:
                qf = security.quarantine_fetch(url)
            except Exception as e:  # noqa: BLE001
                diagnostics.append({"_fetch_error": url, "reason": str(e)})
                continue
            quarantine_status = qf.get("status", "error")
            sha256 = qf.get("sha256")
            findings = qf.get("findings", [])
            body_bytes = qf.get("bytes", 0)
            if quarantine_status != "clean":
                diagnostics.append({"_flagged": url, "findings": findings, "sha256": sha256})
                continue
            if body_bytes < 200:
                diagnostics.append({"_too_thin": url, "bytes": body_bytes, "sha256": sha256})
                continue
            if not is_trusted_author:
                quarantine_path = Path(qf.get("quarantine_path", ""))
                body_text = ""
                if quarantine_path.is_file():
                    try:
                        body_text = quarantine_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        body_text = ""
                if not readme_self_describes_claude(body_text):
                    diagnostics.append({"_readme_no_claude": url, "sha256": sha256})
                    continue
                # v5.3 hard filter: README must show structural depth, not just mention Claude
                if not is_trusted_author and not readme_has_substance(body_text):
                    diagnostics.append({"_readme_too_thin": url, "sha256": sha256})
                    continue

        # v5.3: gather soft signals for the candidate
        candidate_repo_key = repo_full_name(candidate_url)
        cross_provider_count = len(provider_counts.get(candidate_repo_key, set())) if candidate_repo_key else 1
        candidate_with_facet = dict(it)
        # Facet inference: use the curated seeds facet_hint if present; otherwise leave None.
        # propose.py will do role+facet matching when grouping.
        if "facet" not in candidate_with_facet and "facet_hint" in candidate_with_facet:
            candidate_with_facet["facet"] = candidate_with_facet["facet_hint"]
        fb_adj = feedback_adjustment(candidate_with_facet, feedback_history)
        extra_signals = {
            "cross_provider_count": cross_provider_count,
            "fork_ratio_penalty": fork_ratio_penalty(it),
            "active_maintenance": active_maintenance_bonus(it),
            "issue_engagement": issue_engagement_bonus(it),
            "feedback_adjustment": fb_adj,
        }

        # Curated seeds carry their own quality_score=1.0; preserve it.
        if provider_name == "curated":
            qs = {
                "score": 1.0,
                "components": {
                    "curated": 1.0,
                    "feedback_adjustment": round(fb_adj, 3),
                    "_note": "editorial pick (curated_seeds.json) — bypasses scoring",
                },
            }
        else:
            qs = quality_score(
                role_rel=rel,
                popularity=int(it.get("popularity") or 0),
                popularity_kind=it.get("popularity_kind") or "stars",
                last_activity_at=it.get("last_activity_at"),
                trusted_author=is_trusted_author,
                extra_signals=extra_signals,
            )
        candidates.append({
            **it,
            "role_relevance": round(rel, 3),
            "quality_score": qs["score"],
            "quality_components": qs["components"],
            "security_status": quarantine_status,
            "quarantine_sha256": sha256,
            "findings": findings,
            "cross_provider_count": cross_provider_count,
        })

    candidates.sort(key=lambda c: -c.get("quality_score", 0.0))

    DEEP_INSPECT_TOP_N = 12
    skill_substance_drops: list[dict] = []
    for c in candidates[:DEEP_INSPECT_TOP_N]:
        html = c.get("html_url") or ""
        if "github.com" not in html:
            continue
        # Curated seeds skip inspection (their score is already 1.0).
        if c.get("source_provider") == "curated":
            continue
        insp = deep_inspect_github(html, security)
        c["inspection"] = insp
        s_score = structure_score(insp)
        c["structure_score"] = round(s_score, 3)
        c["quality_score_v1"] = c.get("quality_score", 0.0)

        # v5.3 hard filter: of the top deeply-inspected candidates, drop those
        # that lack a SKILL.md path AND fail the file-count floor.
        if not has_skill_substance(insp) and not passes_file_count_floor(insp):
            skill_substance_drops.append({
                "_no_skill_substance": html,
                "had_inspection": True,
                "_note": "no SKILL.md found in top-level or skills/ subdir",
            })
            c["_drop_for_no_substance"] = True
            continue

        c["quality_score"] = round(min(1.0, c["quality_score_v1"] + s_score * 0.20), 3)
        c["quality_components"] = {
            **(c.get("quality_components") or {}),
            "structure": round(s_score, 3),
        }

    # Apply the deep-inspect drops.
    if skill_substance_drops:
        diagnostics.extend(skill_substance_drops)
        candidates = [c for c in candidates if not c.get("_drop_for_no_substance")]

    candidates.sort(key=lambda c: -c.get("quality_score", 0.0))

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "role": args.role,
        "providers": sorted(providers),
        "trusted_authors_count": len(trusted_authors),
        "trusted_repos_count": len(trusted_repos),
        "dry_run": args.dry_run,
        "candidates": candidates,
        "diagnostics": diagnostics,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote": args.out,
        "candidates": len(candidates),
        "diagnostics": len(diagnostics),
        "providers": sorted(providers),
        "dry_run": args.dry_run,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
