#!/usr/bin/env python3
"""
PR Comment Trend Tracker — Bitbucket (dkistdc workspace)

Queries the Bitbucket REST API to count reviewer comments per merged PR
and outputs an HTML report showing your trend over time.

Setup:
  1. Create a Bitbucket API token (app passwords are removed as of July 28 2026):
       bitbucket.org → Your profile → Manage account → Access management → API tokens
       → Create token → scopes: Repositories (read) + Pull requests (read)

  2. Export environment variables:
       export BITBUCKET_API_TOKEN="your-api-token"
       export BITBUCKET_AUTHOR_USERNAME="your-username-slug"  # slug from your profile URL

Run:
  python3 pr_comment_trends.py

Output:
  pr_comment_trends.html  — open in your browser: open ~/repos/pr_comment_trends.html
"""

import os
import sys
import json
import time
import requests
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────────

WORKSPACE = "dkistdc"

# Your Bitbucket username (the slug shown in your profile URL, not display name)
AUTHOR_USERNAME = os.environ.get("BITBUCKET_AUTHOR_USERNAME", "")

# Accounts to exclude from "reviewer comment" counts
# (your own comments + AI reviewer)
# Matched against author.nickname (case-insensitive)
EXCLUDE_NICKNAMES = {AUTHOR_USERNAME, "rovo-dev", "aderks"}
# Matched against author.display_name (case-insensitive, exact)
EXCLUDE_DISPLAY_NAMES = {"alysa derks"}
# Excluded if author.nickname or author.display_name CONTAINS any of these substrings
EXCLUDE_NAME_SUBSTRINGS = {"rovo"}
# Keep legacy name for collect_data compatibility
EXCLUDE_FROM_REVIEWER_COUNT = EXCLUDE_NICKNAMES

# ── API helpers ───────────────────────────────────────────────────────────────

BASE_URL = "https://api.bitbucket.org/2.0"


def get_headers():
    """Return Authorization headers for the Bitbucket API.

    Uses an Atlassian API token (id.atlassian.com) with Basic auth:
      username = your Atlassian account email
      password = the API token
    Create a token at: https://id.atlassian.com/manage-profile/security/api-tokens
    """
    import base64
    token = os.environ.get("BITBUCKET_API_TOKEN", "").strip()
    email = os.environ.get("BITBUCKET_EMAIL", "").strip()
    if not token or not email:
        print(
            "ERROR: Set both environment variables:\n"
            "  export BITBUCKET_EMAIL='your-atlassian-email@example.com'\n"
            "  export BITBUCKET_API_TOKEN='your-atlassian-api-token'\n"
            "\n"
            "Create a token at: https://id.atlassian.com/manage-profile/security/api-tokens"
        )
        sys.exit(1)
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def paginate(url, headers, params=None):
    """Yield all items across paginated Bitbucket responses.

    Retries on 429 (rate limit) and on transient network errors
    (connection reset, timeout, etc.) with exponential backoff.
    """
    params = dict(params or {})
    while url:
        r = None
        last_exc = None
        for attempt in range(6):
            try:
                r = requests.get(url, headers=headers, params=params, timeout=30)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                wait = min(60, 2 ** attempt) + attempt * 2
                print(f"    Network error ({exc.__class__.__name__}) — retrying in {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60)) + attempt * 10
                print(f"    Rate limited — waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                wait = min(60, 2 ** attempt) + attempt * 2
                print(f"    Server error {r.status_code} — retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        else:
            # Exhausted all retries
            if r is not None:
                r.raise_for_status()
            raise last_exc
        data = r.json()
        yield from data.get("values", [])
        url = data.get("next")
        params = {}  # 'next' URL already encodes params
        time.sleep(0.3)  # small delay between pages to stay under rate limit


def get_all_repos(workspace, headers):
    """Return list of repo slugs in the workspace."""
    url = f"{BASE_URL}/repositories/{workspace}"
    return [r["slug"] for r in paginate(url, headers, params={"pagelen": 100})]


def get_prs_by_author(workspace, repo, author, headers, states=("MERGED", "OPEN")):
    """Return PRs in this repo authored by `author` (matched by nickname)."""
    url = f"{BASE_URL}/repositories/{workspace}/{repo}/pullrequests"
    all_prs = []
    for state in states:
        params = {
            "state": state,
            "pagelen": 50,
            "fields": (
                "values.id,values.title,values.created_on,"
                "values.author.nickname,values.state,next"
            ),
        }
        # author.nickname is not a filterable field in Bitbucket's query language,
        # so we fetch all PRs and filter client-side.
        prs = list(paginate(url, headers, params=params))
        all_prs.extend([p for p in prs if p.get("author", {}).get("nickname") == author])
    return all_prs


def get_pr_comment_stats(workspace, repo, pr_id, headers, exclude_users):
    """
    Return (total, reviewer) comment counts for one PR.
    - total: all non-deleted comments
    - reviewer: comments whose author is not in exclude_users
    """
    url = f"{BASE_URL}/repositories/{workspace}/{repo}/pullrequests/{pr_id}/comments"
    comments = list(paginate(url, headers, params={"pagelen": 100}))
    active = [c for c in comments if not c.get("deleted", False)]
    total = len(active)

    exclude_nicks = {u.lower() for u in exclude_users if u}

    def _is_excluded(comment):
        # Bitbucket comment objects put the author under "user", not "author"
        author = comment.get("user") or comment.get("author") or {}
        nick = (author.get("nickname") or "").lower()
        display = (author.get("display_name") or "").lower()
        if nick in exclude_nicks:
            return True
        if display in EXCLUDE_DISPLAY_NAMES:
            return True
        for sub in EXCLUDE_NAME_SUBSTRINGS:
            if sub in nick or sub in display:
                return True
        return False

    reviewer = sum(1 for c in active if not _is_excluded(c))
    return total, reviewer


# ── Main ─────────────────────────────────────────────────────────────────────

CACHE_FILE = "pr_comment_trends.json"

# Bump this any time the comment-counting/exclusion logic changes.
# Cached entries written under an older version are ignored and recomputed,
# so a logic fix doesn't get silently masked by stale cached values.
CACHE_LOGIC_VERSION = 2


def load_cache():
    """Load previously fetched PR data. Returns dict keyed by (repo, pr_id).

    Entries missing a matching _cache_version (or written by older script
    versions) are dropped so they get recomputed with current logic.
    """
    if not os.path.exists(CACHE_FILE):
        return {}
    with open(CACHE_FILE) as f:
        data = json.load(f)
    return {
        (d["repo"], d["pr_id"]): d
        for d in data
        if d.get("_cache_version") == CACHE_LOGIC_VERSION
    }


def collect_data(author_username):
    headers = get_headers()
    exclude = EXCLUDE_FROM_REVIEWER_COUNT | {author_username, author_username.lower()}

    # Load cached results — MERGED PRs are immutable so we skip re-fetching them
    cache = load_cache()
    cached_merged = {k: v for k, v in cache.items() if v.get("state", "MERGED") == "MERGED"}
    print(f"Loaded {len(cached_merged)} cached MERGED PRs (cache logic v{CACHE_LOGIC_VERSION}).")

    print(f"Fetching repos in workspace '{WORKSPACE}'...")
    repos = get_all_repos(WORKSPACE, headers)
    print(f"Found {len(repos)} repos. Scanning for your merged PRs...\n")

    results = []
    for repo in sorted(repos):
        prs = get_prs_by_author(WORKSPACE, repo, author_username, headers)
        if not prs:
            continue
        print(f"  {repo}: {len(prs)} PR(s)")
        for pr in prs:
            pr_id = pr["id"]
            state = pr.get("state", "MERGED")
            cache_key = (repo, pr_id)

            # Re-use cached data for MERGED PRs to avoid redundant API calls
            if state == "MERGED" and cache_key in cached_merged:
                results.append(cached_merged[cache_key])
                print(f"    #{pr_id}: (cached) — {pr['title'][:45]}")
                continue

            total, reviewer = get_pr_comment_stats(
                WORKSPACE, repo, pr_id, headers, exclude
            )
            results.append({
                "repo": repo,
                "pr_id": pr_id,
                "title": pr["title"],
                "created": pr["created_on"][:10],
                "state": state,
                "total_comments": total,
                "reviewer_comments": reviewer,
                "url": f"https://bitbucket.org/{WORKSPACE}/{repo}/pull-requests/{pr_id}",
                "_cache_version": CACHE_LOGIC_VERSION,
            })
            print(f"    #{pr_id}: {reviewer} reviewer comments ({total} total) — {pr['title'][:45]}")

    results.sort(key=lambda x: x["created"])
    return results


# ── HTML report ───────────────────────────────────────────────────────────────

MEETING_CUTOFF = "2026-06-12"  # "before" = on or before; "after" = after


def _section_html(chunk, label, date_range, chart_id):
    """Return the HTML string for one time-period section (metrics + chart)."""
    if not chunk:
        return f'<p style="color:#57606a;margin-bottom:28px;">No merged PRs in the <strong>{label}</strong> period.</p>'

    counts = [d["reviewer_comments"] for d in chunk]
    avg = sum(counts) / len(counts)
    avg_trend = [round(avg, 2)] * len(counts)
    titles_json = json.dumps([d["title"][:60] for d in chunk])
    labels_json = json.dumps([
        f"#{d['pr_id']} ({d['repo'].replace('dkist-processing-','').replace('dkist-','')})"
        for d in chunk
    ])
    counts_json = json.dumps(counts)
    avg_trend_json = json.dumps(avg_trend)

    # Section header color: green for "after", blue-gray for "before"
    hdr_color = "#1a7f37" if "After" in label else "#0969da"
    badge_bg  = "#dafbe1" if "After" in label else "#dbeafe"
    badge_txt = "#1a7f37" if "After" in label else "#1e40af"

    return f"""
<div class="section">
  <div class="section-header">
    <span class="section-badge" style="background:{badge_bg};color:{badge_txt};">{label}</span>
    <span class="section-range">{date_range}</span>
  </div>
  <div class="stats">
    <div class="stat"><div class="value" style="color:{hdr_color};">{len(chunk)}</div><div class="label">merged PRs</div></div>
    <div class="stat"><div class="value" style="color:{hdr_color};">{avg:.1f}</div><div class="label">overall avg reviewer comments</div></div>
    <div class="stat"><div class="value" style="color:{hdr_color};">{max(counts)}</div><div class="label">most comments</div></div>
  </div>
  <div class="chart-wrap">
    <h2>Reviewer comments per merged PR — {label}</h2>
    <canvas id="{chart_id}"></canvas>
  </div>
</div>
<script>
(function() {{
  const ctx = document.getElementById('{chart_id}').getContext('2d');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {labels_json},
      datasets: [
        {{
          label: 'Reviewer comments',
          data: {counts_json},
          backgroundColor: '{"rgba(26,127,55,0.18)" if "After" in label else "rgba(9,105,218,0.18)"}',
          borderColor: '{"rgba(26,127,55,0.7)" if "After" in label else "rgba(9,105,218,0.7)"}',
          borderWidth: 1.5,
          borderRadius: 3,
          order: 2,
        }},
        {{
          label: 'Overall avg ({avg:.1f})',
          data: {avg_trend_json},
          type: 'line',
          borderColor: '#cf222e',
          borderWidth: 2,
          borderDash: [6, 3],
          pointRadius: 0,
          fill: false,
          order: 1,
        }},
      ]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ position: 'top', labels: {{ font: {{ size: 12 }} }} }},
        tooltip: {{
          callbacks: {{
            title: (items) => {titles_json}[items[0].dataIndex]
          }}
        }}
      }},
      scales: {{
        y: {{
          beginAtZero: true,
          ticks: {{ stepSize: 1 }},
          title: {{ display: true, text: 'Comment count' }}
        }},
        x: {{
          ticks: {{ font: {{ size: 10 }}, maxRotation: 45 }}
        }}
      }}
    }}
  }});
}})();
</script>"""


def make_html(results):
    if not results:
        return "<html><body><p>No merged PRs found.</p></body></html>"

    cutoff_dt = datetime.strptime(MEETING_CUTOFF, "%Y-%m-%d")
    before = [d for d in results if datetime.strptime(d["created"], "%Y-%m-%d") <= cutoff_dt]
    after  = [d for d in results if datetime.strptime(d["created"], "%Y-%m-%d") >  cutoff_dt]

    earliest = results[0]["created"]
    latest   = results[-1]["created"]
    today    = datetime.today().strftime("%Y-%m-%d")

    before_range = f"{earliest} → {MEETING_CUTOFF}"
    after_range  = f"2026-06-13 → {today}"

    after_html  = _section_html(after,  "After Meeting",  after_range,  "chart-after")
    before_html = _section_html(before, "Before Meeting", before_range, "chart-before")

    # Table rows — PRs on or before cutoff get red text
    table_rows = ""
    for d in reversed(results):
        repo_short = d["repo"].replace("dkist-processing-", "").replace("dkist-", "")
        is_before  = datetime.strptime(d["created"], "%Y-%m-%d") <= cutoff_dt
        row_style  = ' style="color:#cf222e;"' if is_before else ""
        link_style = ' style="color:#cf222e;"' if is_before else ""
        state = d.get("state", "MERGED")
        state_badge = (
            '<span style="background:#dafbe1;color:#1a7f37;font-size:0.75rem;'
            'padding:2px 8px;border-radius:10px;font-weight:600;">OPEN</span>'
            if state == "OPEN" else
            '<span style="background:#f0f0f0;color:#57606a;font-size:0.75rem;'
            'padding:2px 8px;border-radius:10px;">MERGED</span>'
        )
        table_rows += (
            f'<tr{row_style}>'
            f'<td>{d["created"]}</td>'
            f'<td>{repo_short}</td>'
            f'<td><a href="{d["url"]}" target="_blank"{link_style}>#{d["pr_id"]}</a></td>'
            f'<td class="truncate" title="{d["title"]}">{d["title"][:55]}{"…" if len(d["title"])>55 else ""}</td>'
            f'<td>{state_badge}</td>'
            f'<td class="num">{d["reviewer_comments"]}</td>'
            f'<td class="num dim">{d["total_comments"]}</td>'
            f'</tr>\n'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>PR Comment Trends — {WORKSPACE}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f6f8fa; color: #24292f; padding: 32px; }}
  h1 {{ font-size: 1.4rem; font-weight: 600; margin-bottom: 4px; }}
  .page-subtitle {{ color: #57606a; font-size: 0.85rem; margin-bottom: 36px; }}
  .section {{ margin-bottom: 48px; }}
  .section-header {{ display: flex; align-items: center; gap: 14px; margin-bottom: 20px; }}
  .section-badge {{ font-size: 0.9rem; font-weight: 700; padding: 4px 14px;
                    border-radius: 20px; letter-spacing: 0.02em; }}
  .section-range {{ color: #57606a; font-size: 0.82rem; }}
  .stats {{ display: flex; gap: 20px; margin-bottom: 24px; flex-wrap: wrap; }}
  .stat {{ background: white; border: 1px solid #d0d7de; border-radius: 8px;
           padding: 16px 24px; min-width: 160px; }}
  .stat .value {{ font-size: 2rem; font-weight: 700; }}
  .stat .label {{ font-size: 0.78rem; color: #57606a; margin-top: 2px; }}
  .chart-wrap {{ background: white; border: 1px solid #d0d7de; border-radius: 8px;
                 padding: 24px; margin-bottom: 12px; }}
  .chart-wrap h2 {{ font-size: 0.9rem; font-weight: 600; margin-bottom: 16px; color: #57606a; }}
  canvas {{ max-height: 300px; }}
  .section-divider {{ border: none; border-top: 2px solid #d0d7de; margin: 16px 0 44px; }}
  .table-heading {{ font-size: 1rem; font-weight: 600; margin-bottom: 14px; color: #24292f; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border: 1px solid #d0d7de; border-radius: 8px; overflow: hidden; font-size: 0.83rem; }}
  th {{ background: #f6f8fa; padding: 10px 12px; text-align: left;
        font-weight: 600; border-bottom: 1px solid #d0d7de; white-space: nowrap; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #eaecef; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f6f8fa; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .dim {{ color: #57606a; }}
  a {{ color: #0969da; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .truncate {{ max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
</style>
</head>
<body>
<h1>PR Comment Trends</h1>
<p class="page-subtitle">Workspace: {WORKSPACE} &nbsp;·&nbsp; Generated {today}</p>

{after_html}

<hr class="section-divider">

{before_html}

<hr class="section-divider">

<p class="table-heading">All Merged PRs</p>
<table>
  <thead>
    <tr>
      <th>Date</th><th>Repo</th><th>PR</th><th>Title</th><th>State</th>
      <th class="num">Reviewer comments</th><th class="num dim">Total (incl. yours)</th>
    </tr>
  </thead>
  <tbody>
{table_rows}  </tbody>
</table>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    author = AUTHOR_USERNAME
    if not author:
        author = input("Your Bitbucket username (slug from your profile URL): ").strip()
        if not author:
            print("ERROR: Bitbucket username required.")
            sys.exit(1)

    results = collect_data(author)

    if not results:
        print("\nNo merged PRs found. Check that your username is correct.")
        sys.exit(0)

    # Save JSON
    with open("pr_comment_trends.json", "w") as f:
        json.dump(results, f, indent=2)

    # Save HTML
    html = make_html(results)
    out_path = "pr_comment_trends.html"
    with open(out_path, "w") as f:
        f.write(html)

    print(f"\n✓ {len(results)} PRs processed")
    print(f"  HTML report: {out_path}")
    print(f"  JSON data:   pr_comment_trends.json")

    avg = sum(d["reviewer_comments"] for d in results) / len(results)
    print(f"\n  Average reviewer comments per PR: {avg:.1f}")
    print(f"  Trend (last 3 PRs avg): "
          f"{sum(d['reviewer_comments'] for d in results[-3:]) / min(3, len(results)):.1f}")