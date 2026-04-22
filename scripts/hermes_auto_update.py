#!/opt/data/venv/bin/python3
"""
🐝 Hermes Auto-Update Script
Checks for new upstream releases, detects conflicts, backs up to Google Drive,
syncs the fork, and verifies the update.

Designed to be called by the Hermes Auto-Update Monitor cron job.
Outputs JSON results for the cron agent to post to Discord.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── Config ──────────────────────────────────────────────────────────────
UPSTREAM_REPO = "NousResearch/hermes-agent"
FORK_REPO = "Buzz-Hive-Life/buzz-hermes-agent"
FORK_DEFAULT_BRANCH = "main"
DATA_DIR = "/opt/data"
CONFIG_PATH = f"{DATA_DIR}/config.yaml"
BACKUP_SCRIPT = f"{DATA_DIR}/scripts/backup_to_gdrive.py"
RAILWAY_WAIT_MINUTES = 5
RAILWAY_TIMEOUT_MINUTES = 10
CUSTOM_PATCHED_FILES = ["gateway/run.py", "Dockerfile", "pyproject.toml"]

# Midnight Atlantic (Moncton, NB) = 03:00 UTC (ADT, summer) or 04:00 UTC (AST, winter)
MIDNIGHT_ATLANTIC_UTC_HOURS = {3, 4}

# ── GitHub Helpers ──────────────────────────────────────────────────────

def get_github_token():
    """Read GITHUB_TOKEN from environment or .env file."""
    token = os.environ.get("GITHUB_TOKEN")
    if token and token != "***":
        return token

    env_path = f"{DATA_DIR}/.env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN=") and not line.startswith("#"):
                    token = line.split("=", 1)[1].strip()
                    if token and token != "***":
                        return token
    return None


def github_api(endpoint, method="GET", data=None, token=None):
    """Make a GitHub API call."""
    if token is None:
        token = get_github_token()

    url = f"https://api.github.com{endpoint}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Buzz-Hive-Life/1.0",
    }
    if token:
        headers["Authorization"] = f"token {token}"

    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return {"error": True, "status": e.code, "message": error_body}


# ── Version Check ───────────────────────────────────────────────────────

def get_current_version():
    """Get the currently running Hermes version.
    Returns the full version string and the date-based tag for comparison.
    e.g., 'Hermes Agent v0.10.0 (2026.4.16)' -> {'full': '...', 'tag': 'v2026.4.16'}
    """
    try:
        result = subprocess.run(
            ["/opt/hermes/.venv/bin/hermes", "--version"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()
        # Parse date-based tag from parentheses: "Hermes Agent v0.10.0 (2026.4.16)"
        import re
        match = re.search(r'\((\d{4}\.\d+\.\d+)\)', output)
        if match:
            return {"full": output, "tag": f"v{match.group(1)}"}
        # Fallback: find first v-prefixed part
        for part in output.split():
            if part.startswith("v") and "." in part:
                return {"full": output, "tag": part}
        return {"full": output, "tag": output}
    except Exception as e:
        return {"full": f"unknown ({e})", "tag": "unknown"}


def get_latest_release():
    """Get the latest upstream release info."""
    data = github_api(f"/repos/{UPSTREAM_REPO}/releases/latest")
    if "error" in data:
        return None, f"GitHub API error: {data.get('status')} - {data.get('message', '')[:200]}"

    return {
        "tag": data["tag_name"],
        "name": data["name"],
        "published_at": data["published_at"],
        "url": data["html_url"],
        "body": data.get("body", "")[:500],
    }, None


def versions_differ(current_tag, latest_tag):
    """Check if version tags differ."""
    c = current_tag.lstrip("v").strip() if current_tag else ""
    l = latest_tag.lstrip("v").strip() if latest_tag else ""
    return c != l


# ── Conflict Detection ─────────────────────────────────────────────────

def check_conflicts():
    """Compare upstream changes against our custom patches."""
    # Get upstream main HEAD SHA
    upstream_data = github_api(f"/repos/{UPSTREAM_REPO}/branches/{FORK_DEFAULT_BRANCH}")
    if "error" in upstream_data:
        return {"risk": "UNKNOWN", "error": f"Cannot fetch upstream branch: {upstream_data.get('message', '')[:200]}"}

    upstream_sha = upstream_data.get("commit", {}).get("sha", "")
    if not upstream_sha:
        return {"risk": "UNKNOWN", "error": "Cannot get upstream HEAD SHA"}

    # Get fork main HEAD SHA
    fork_data = github_api(f"/repos/{FORK_REPO}/branches/{FORK_DEFAULT_BRANCH}")
    if "error" in fork_data:
        return {"risk": "UNKNOWN", "error": f"Cannot fetch fork branch: {fork_data.get('message', '')[:200]}"}

    fork_sha = fork_data.get("commit", {}).get("sha", "")
    if not fork_sha:
        return {"risk": "UNKNOWN", "error": "Cannot get fork HEAD SHA"}

    # Compare using SHAs (works for cross-repo)
    data = github_api(f"/repos/{FORK_REPO}/compare/{upstream_sha}...{fork_sha}")
    if "error" in data:
        return {"risk": "UNKNOWN", "error": f"Compare failed: {data.get('message', '')[:200]}"}

    behind = data.get("behind_by", 0)
    ahead = data.get("ahead_by", 0)
    changed_files = [f["filename"] for f in data.get("files", [])]

    # Check if any of our patched files are modified upstream
    # (behind_by > 0 means upstream has commits we don't)
    conflict_files = [f for f in CUSTOM_PATCHED_FILES if f in changed_files and behind > 0]

    if not conflict_files:
        risk = "LOW"
    elif len(conflict_files) == 1:
        risk = "MEDIUM"
    else:
        risk = "HIGH"

    return {
        "risk": risk,
        "behind_by": behind,
        "ahead_by": ahead,
        "conflict_files": conflict_files,
        "changed_files": changed_files[:10],  # First 10 for brevity
    }


# ── Backup ──────────────────────────────────────────────────────────────

def run_backup():
    """Run the Google Drive backup script."""
    if not os.path.exists(BACKUP_SCRIPT):
        return {"success": False, "error": f"Backup script not found: {BACKUP_SCRIPT}"}

    try:
        result = subprocess.run(
            ["/opt/data/venv/bin/python3", BACKUP_SCRIPT],
            capture_output=True, text=True, timeout=120
        )

        # Parse JSON result from script output (after "--- RESULT ---" marker)
        output = result.stdout
        marker = "--- RESULT ---"
        if marker in output:
            json_text = output.split(marker, 1)[1].strip()
            try:
                return {"success": True, "details": json.loads(json_text)}
            except json.JSONDecodeError:
                pass

        # Fallback: try to find a JSON object in the output
        if result.returncode == 0:
            return {"success": True, "output": result.stdout[-500:]}
        else:
            return {"success": False, "error": result.stderr[-500:]}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Backup timed out after 120 seconds"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Fork Sync ───────────────────────────────────────────────────────────

def get_current_commit_sha():
    """Get the current HEAD commit SHA of the fork's main branch."""
    data = github_api(f"/repos/{FORK_REPO}/branches/{FORK_DEFAULT_BRANCH}")
    if "error" in data:
        return None
    return data.get("commit", {}).get("sha")


def sync_fork():
    """Sync the fork with upstream via GitHub API."""
    data = github_api(
        f"/repos/{FORK_REPO}/merge-upstream",
        method="POST",
        data={"branch": FORK_DEFAULT_BRANCH}
    )
    if "error" in data:
        return False, f"Sync failed: HTTP {data.get('status')} - {data.get('message', '')[:300]}"

    return True, f"Synced: {data.get('message', 'OK')}"


def rollback_fork(sha):
    """Rollback fork to a previous commit (not easily done via API).
    Returns instructions for manual rollback."""
    return {
        "manual": True,
        "instructions": (
            f"To rollback, run:\n"
            f"  git clone https://github.com/{FORK_REPO}.git\n"
            f"  cd buzz-hermes-agent\n"
            f"  git reset --hard {sha}\n"
            f"  git push --force origin {FORK_DEFAULT_BRANCH}"
        )
    }


# ── Railway Verification ───────────────────────────────────────────────

def wait_for_railway_deploy():
    """Wait for Railway to rebuild and deploy, then verify."""
    print(f"  ⏳ Waiting {RAILWAY_WAIT_MINUTES} min for Railway to rebuild...")
    time.sleep(RAILWAY_WAIT_MINUTES * 60)

    # Check version
    new_version = get_current_version()

    # Check if gateway is responsive (simple version check)
    try:
        result = subprocess.run(
            ["/opt/hermes/.venv/bin/hermes", "--version"],
            capture_output=True, text=True, timeout=15
        )
        gateway_ok = result.returncode == 0
    except Exception:
        gateway_ok = False

    return {
        "version": new_version,
        "gateway_responsive": gateway_ok,
    }


# ── Main Pipeline ───────────────────────────────────────────────────────

def main():
    print("🐝 Hermes Auto-Update Pipeline — Starting...")
    print(f"   Timestamp: {datetime.now(timezone.utc).isoformat()}")

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "none",
        "current_version": None,
        "latest_version": None,
        "update_available": False,
        "conflict_risk": None,
        "backup": None,
        "sync": None,
        "verification": None,
        "rollback_sha": None,
        "success": False,
        "error": None,
    }

    # 1. Check current version
    print("\n🔍 Step 1: Checking current version...")
    current = get_current_version()
    current_tag = current["tag"]
    result["current_version"] = current["full"]
    print(f"   Current: {current['full']}")

    # 2. Check latest release
    print("\n🔍 Step 2: Checking latest upstream release...")
    release, err = get_latest_release()
    if err:
        result["error"] = f"Failed to fetch release: {err}"
        print(f"   ❌ {result['error']}")
        return result

    latest = release["tag"]
    result["latest_version"] = latest
    result["release_url"] = release["url"]
    print(f"   Latest: {latest} ({release['name']})")

    # 3. Compare versions
    if not versions_differ(current_tag, latest):
        result["action"] = "skipped"
        result["success"] = True
        print(f"   ✅ Already up to date!")
        return result

    result["update_available"] = True
    result["action"] = "update"
    print(f"   🆕 Update available: {current_tag} → {latest}")

    # 3b. Check if it's midnight Atlantic time (update window)
    utc_hour = datetime.now(timezone.utc).hour
    is_midnight_atlantic = utc_hour in MIDNIGHT_ATLANTIC_UTC_HOURS
    result["utc_hour"] = utc_hour
    result["is_midnight_atlantic"] = is_midnight_atlantic
    print(f"   🕐 Current UTC hour: {utc_hour} (midnight Atlantic = {MIDNIGHT_ATLANTIC_UTC_HOURS})")

    if not is_midnight_atlantic:
        result["action"] = "pending"
        result["success"] = True
        print(f"   ⏰ Not update window yet — will update at midnight Atlantic (03:00 or 04:00 UTC)")
        return result

    print(f"   🌙 Midnight Atlantic — proceeding with update!")

    # 4. Check conflicts
    print("\n🔍 Step 3: Checking for conflicts...")
    conflicts = check_conflicts()
    result["conflict_risk"] = conflicts.get("risk")
    print(f"   Risk: {conflicts.get('risk')}")
    print(f"   Behind by: {conflicts.get('behind_by', '?')} commits")
    print(f"   Ahead by: {conflicts.get('ahead_by', '?')} commits")

    if conflicts.get("conflict_files"):
        print(f"   ⚠️  Conflict files: {', '.join(conflicts['conflict_files'])}")

    if conflicts.get("risk") == "HIGH":
        result["error"] = (
            f"High conflict risk! Upstream modified our patched files: "
            f"{', '.join(conflicts.get('conflict_files', []))}. "
            f"Manual review required."
        )
        print(f"   🚨 {result['error']}")
        return result

    # 5. Save rollback SHA
    print("\n💾 Step 4: Saving rollback point...")
    rollback_sha = get_current_commit_sha()
    result["rollback_sha"] = rollback_sha
    print(f"   Rollback SHA: {rollback_sha}")

    # 6. Backup
    print("\n📦 Step 5: Backing up /opt/data to Google Drive...")
    backup = run_backup()
    result["backup"] = backup
    if backup.get("success"):
        print(f"   ✅ Backup complete!")
    else:
        print(f"   ⚠️  Backup issue: {backup.get('error', 'unknown')}")
        # Don't abort — backup failure isn't critical for update

    # 7. Sync fork
    print("\n📋 Step 6: Syncing fork with upstream...")
    sync_ok, sync_msg = sync_fork()
    result["sync"] = {"success": sync_ok, "message": sync_msg}
    if sync_ok:
        print(f"   ✅ {sync_msg}")
    else:
        result["error"] = sync_msg
        print(f"   ❌ {sync_msg}")
        return result

    # 7b. Re-pin discord.py (protect against upstream overwrites)
    print("   🔒 Re-pinning discord.py to ==2.7.1...")
    try:
        # Get current pyproject.toml content
        pt_data = github_api(f"/repos/{FORK_REPO}/contents/pyproject.toml")
        if "content" in pt_data:
            import base64
            current = base64.b64decode(pt_data["content"]).decode("utf-8")
            # Check if pin needs re-applying
            if 'discord.py[voice]>=2.7.1,<3' in current or 'discord.py[voice]>=2.7.1' in current:
                fixed = current.replace(
                    '"discord.py[voice]>=2.7.1,<3"',
                    '"discord.py[voice]==2.7.1"'
                ).replace(
                    '"discord.py[voice]>=2.7.1"',
                    '"discord.py[voice]==2.7.1"'
                )
                github_api(
                    f"/repos/{FORK_REPO}/contents/pyproject.toml",
                    method="PUT",
                    data={
                        "message": "fix: re-pin discord.py to ==2.7.1 (post-sync protection)",
                        "content": base64.b64encode(fixed.encode()).decode(),
                        "sha": pt_data["sha"]
                    }
                )
                print("   ✅ discord.py re-pinned after sync")
            else:
                print("   ✅ discord.py already pinned")
    except Exception as e:
        print(f"   ⚠️  Re-pin failed (non-critical): {e}")

    # 8. Wait for Railway
    print(f"\n🚂 Step 7: Waiting for Railway redeploy...")
    verification = wait_for_railway_deploy()
    result["verification"] = verification
    print(f"   Version after deploy: {verification.get('version', 'unknown')}")
    print(f"   Gateway responsive: {verification.get('gateway_responsive', False)}")

    if not verification.get("gateway_responsive"):
        result["error"] = "Gateway not responsive after deploy!"
        print(f"   🚨 {result['error']}")
        return result

    # 9. Success!
    result["success"] = True
    result["action"] = "updated"
    print(f"\n✅ Update complete! {current_tag} → {latest}")
    return result


if __name__ == "__main__":
    result = main()
    print("\n--- RESULT ---")
    print(json.dumps(result, indent=2))
