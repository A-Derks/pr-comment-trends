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
import requests
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────────

WORKSPACE = "dkistdc"

# Your Bitbucket username (the slug shown in your profile URL, not display name)
AUTHOR_USERNAME = os.environ.get("BITBUCKET_AUTHOR_USERNAME", "")

# Accounts to exclude from "reviewer comment" counts
# (your own comments + AI reviewer)
EXCLUDE_FROM_REVIEWER_COUNT = {AUTHOR_USERNAME, "rovo-dev"}

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
    """Yield all items across paginated Bitbucket responses."""
    params = dict(params or {})
    while url:
        r = requests.get(url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        yield from data.get("values", [])
        url = data.get("next")
        params = {}  # 'next' URL already encodes params


def get_all_repos(workspace, headers):
    """Return list of repo slugs in the workspace."""
    url = f"{BASE_URL}/repositories/{workspace}"
    return [r["slug"] for r in paginate(url, headers, params={"pagelen": 100})]


def get_merged_prs_by_author(workspace, repo, author, headers):
    """Return merged PRs in this repo authored by `author` (matched by nickname)."""
    url = f"{BASE_URL}/repositories/{workspace}/{repo}/pullrequests"
    params = {
        "state": "MERGED",
        "pagelen": 50,
        "fields": (
            "values.id,values.title,values.created_on,"
            "values.author.nickname,next"
        ),
    }
    # author.nickname is not a filterable field in Bitbucket's query language,
    # so we fetch all merged PRs and filter client-side.
    all_prs = list(paginate(url, headers, params=params))
    return [p for p in all_prs if p.get("author", {}).get("nickname") == author]


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
    reviewer = sum(
        1 for c in active
        if c.get("author", {}).get("nickname", "").lower()
        not in {u.lower() for u in exclude_users if u}
    )
    return total, reviewer


# ── Main ─────────────────────────────────────────────────────────────────────

def collect_data(author_username):
    headers = get_headers()
    exclude = EXCLUDE_FROM_REVIEWER_COUNT | {author_username}

    print(f"Fetching repos in workspace '{WORKSPACE}'...")
    repos = get_all_repos(WORKSPACE, headers)
    print(f"Found {len(repos)} repos. Scanning for your merged PRs...\n")

    results = []
    for repo in sorted(repos):
        prs = get_merged_prs_by_author(WORKSPACE, repo, author_username, headers)
        if not prs:
            continue
        print(f"  {repo}: {len(prs)} PR(s)")
        for pr in prs:
            pr_id = pr["id"]
            total, reviewer = get_pr_comment_stats(
                WORKSPACE, repo, pr_id, headers, exclude
            )
            results.append({
                "repo": repo,
                "pr_id": pr_id,
                "title": pr["title"],
                "created": pr["created_on"][:10],
                "total_comments": total,
                "reviewer_comments": reviewer,
                "url": f"https://bitbucket.org/{WORKSPACE}/{repo}/pull-requests/{pr_id}",
            })
            print(f"    #{pr_id}: {reviewer} reviewer comments ({total} total) — {pr['title'][:45]}")

    results.sort(key=lambda x: x["created"])
    return results


# ── HTML report ───────────────────────────────────────────────────────────────

def make_html(results):
    if not results:
        return "<html><body><p>No merged PRs found.</p></body></html>"

    labels = [f"#{d['pr_id']} ({d['repo'].replace('dkist-processing-','').replace('dkist-','')})" for d in results]
    reviewer_counts = [d["reviewer_comments"] for d in results]
    total_counts = [d["total_comments"] for d in results]

    # Overall average
    avg = sum(reviewer_counts) / len(reviewer_counts) if reviewer_counts else 0
    overall_trend = [round(avg, 2)] * len(reviewer_counts)

    # Calendar-year average — steps up/down at year boundaries
    from collections import defaultdict
    year_buckets = defaultdict(list)
    for d, r in zip(results, reviewer_counts):
        year_buckets[d["created"][:4]].append(r)
    year_avgs = {y: sum(v) / len(v) for y, v in year_buckets.items()}
    yearly_trend = [round(year_avgs[d["created"][:4]], 2) for d in results]

    # Last-3-months average — only drawn over that window, null before it
    latest_dt = datetime.strptime(results[-1]["created"], "%Y-%m-%d")
    m, y = latest_dt.month - 3, latest_dt.year
    if m <= 0:
        m += 12
        y -= 1
    cutoff = latest_dt.replace(year=y, month=m)
    last3m = [r for d, r in zip(results, reviewer_counts)
              if datetime.strptime(d["created"], "%Y-%m-%d") >= cutoff]
    last3m_avg = round(sum(last3m) / len(last3m), 2) if last3m else None
    last3m_trend = [
        last3m_avg if datetime.strptime(d["created"], "%Y-%m-%d") >= cutoff else None
        for d in results
    ]

    total_prs = len(results)
    earliest = results[0]["created"]
    latest = results[-1]["created"]

    # Table rows
    table_rows = ""
    for d in reversed(results):
        repo_short = d["repo"].replace("dkist-processing-", "").replace("dkist-", "")
        table_rows += (
            f'<tr>'
            f'<td>{d["created"]}</td>'
            f'<td>{repo_short}</td>'
            f'<td><a href="{d["url"]}" target="_blank">#{d["pr_id"]}</a></td>'
            f'<td class="truncate" title="{d["title"]}">{d["title"][:55]}{"…" if len(d["title"])>55 else ""}</td>'
            f'<td class="num">{d["reviewer_comments"]}</td>'
            f'<td class="num dim">{d["total_comments"]}</td>'
            f'</tr>\n'
        )

    labels_json = json.dumps(labels)
    reviewer_json = json.dumps(reviewer_counts)
    total_json = json.dumps(total_counts)
    overall_trend_json = json.dumps(overall_trend)
    yearly_trend_json = json.dumps(yearly_trend)
    last3m_trend_json = json.dumps(last3m_trend)
    last3m_label = f"Last 3 months avg ({last3m_avg:.1f})" if last3m_avg is not None else "Last 3 months avg"

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
  .subtitle {{ color: #57606a; font-size: 0.85rem; margin-bottom: 28px; }}
  .stats {{ display: flex; gap: 20px; margin-bottom: 32px; flex-wrap: wrap; }}
  .stat {{ background: white; border: 1px solid #d0d7de; border-radius: 8px;
           padding: 16px 24px; min-width: 140px; }}
  .stat .value {{ font-size: 2rem; font-weight: 700; color: #0969da; }}
  .stat .label {{ font-size: 0.78rem; color: #57606a; margin-top: 2px; }}
  .chart-wrap {{ background: white; border: 1px solid #d0d7de; border-radius: 8px;
                 padding: 24px; margin-bottom: 28px; }}
  .chart-wrap h2 {{ font-size: 0.9rem; font-weight: 600; margin-bottom: 16px; color: #57606a; }}
  canvas {{ max-height: 320px; }}
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
<p class="subtitle">Workspace: {WORKSPACE} &nbsp;·&nbsp; {earliest} → {latest} &nbsp;·&nbsp; Generated {datetime.today().strftime('%Y-%m-%d')}</p>

<div class="stats">
  <div class="stat"><div class="value">{total_prs}</div><div class="label">merged PRs</div></div>
  <div class="stat"><div class="value">{avg:.1f}</div><div class="label">overall avg</div></div>
  <div class="stat"><div class="value">{year_avgs.get(datetime.today().strftime('%Y'), avg):.1f}</div><div class="label">{datetime.today().strftime('%Y')} avg</div></div>
  <div class="stat"><div class="value">{last3m_avg if last3m_avg is not None else "—"}</div><div class="label">last 3 months avg</div></div>
  <div class="stat"><div class="value">{min(reviewer_counts)}</div><div class="label">fewest comments</div></div>
  <div class="stat"><div class="value">{max(reviewer_counts)}</div><div class="label">most comments</div></div>
</div>

<div class="chart-wrap">
  <h2>Reviewer comments per merged PR (chronological)</h2>
  <canvas id="chart"></canvas>
</div>

<table>
  <thead>
    <tr>
      <th>Date</th><th>Repo</th><th>PR</th><th>Title</th>
      <th class="num">Reviewer comments</th><th class="num dim">Total (incl. yours)</th>
    </tr>
  </thead>
  <tbody>
{table_rows}  </tbody>
</table>

<script>
const ctx = document.getElementById('chart').getContext('2d');
new Chart(ctx, {{
  type: 'bar',
  data: {{
    labels: {labels_json},
    datasets: [
      {{
        label: 'Reviewer comments',
        data: {reviewer_json},
        backgroundColor: 'rgba(9, 105, 218, 0.18)',
        borderColor: 'rgba(9, 105, 218, 0.7)',
        borderWidth: 1.5,
        borderRadius: 3,
        order: 2,
      }},
      {{
        label: 'Overall avg ({avg:.1f})',
        data: {overall_trend_json},
        type: 'line',
        borderColor: '#cf222e',
        borderWidth: 2,
        borderDash: [6, 3],
        pointRadius: 0,
        fill: false,
        order: 1,
      }},
      {{
        label: 'Year avg',
        data: {yearly_trend_json},
        type: 'line',
        borderColor: '#8250df',
        borderWidth: 2,
        borderDash: [3, 3],
        pointRadius: 0,
        fill: false,
        order: 1,
      }},
      {{
        label: '{last3m_label}',
        data: {last3m_trend_json},
        type: 'line',
        borderColor: '#1a7f37',
        borderWidth: 2,
        pointRadius: 0,
        spanGaps: false,
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
          title: (items) => {{
            const i = items[0].dataIndex;
            return {json.dumps([d["title"][:60] for d in results])}[i];
          }}
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
</script>
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