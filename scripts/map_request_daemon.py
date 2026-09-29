#!/usr/bin/env python3
"""Local daemon that fulfills public "request a workshop map" jobs.

Polls a Cloudflare Worker for queued jobs (never accepts inbound connections),
and for each one: validates it's a Rocket League workshop item, downloads it
via ~/ASSella_cli.py's existing download logic, uploads the result to R2 via
the `r2:` rclone remote, appends an entry to maps.json, and pushes to GitHub.

See ~/.claude/plans/federated-hugging-puddle.md for the full design.

Standalone test mode (no Worker needed):
  python3 map_request_daemon.py --test-wid 2968144588 --branch test/map-request-daemon

Real polling loop (needs WORKER_BASE_URL + DAEMON_SECRET in
~/.config/hebnix-linux-dev/map-daemon.env):
  python3 map_request_daemon.py
"""
import argparse
import configparser
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPS_JSON = os.path.join(REPO_DIR, "maps.json")
NO_PREVIEW_FALLBACK = os.path.join(REPO_DIR, "no-preview.png")
ASSELLA_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assella_cli.py")
DAEMON_CONF_PATH = os.path.expanduser("~/.config/hebnix-linux-dev/map-daemon.env")
RL_APPID = "252950"
STEAM_PUBLISHED_FILE_DETAILS_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
)


def load_assella_cli():
    spec = importlib.util.spec_from_file_location("assella_cli", ASSELLA_CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DAEMON_CONF_KEYS = (
    "DAEMON_SECRET", "WORKER_BASE_URL", "MORRENUS_API_KEY",
    "STEAM_API_KEY", "DDM_DLL_PATH",
)


def load_daemon_conf():
    """Simple `KEY=value` env-style file, same shape as r2-credentials.env
    but without `export`/shell semantics since we just need key/value pairs.
    Real environment variables (as set by GitHub Actions secrets in CI)
    take priority over the file, so the same code works unmodified locally
    (file-based) and in CI (env-var-based, no file present)."""
    conf = {}
    if os.path.exists(DAEMON_CONF_PATH):
        with open(DAEMON_CONF_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line = line.removeprefix("export ").strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip().strip('"').strip("'")
    for key in DAEMON_CONF_KEYS:
        if os.environ.get(key):
            conf[key] = os.environ[key]
    return conf


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sanitize_title(title: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
    return s or "Untitled_Map"


def strip_bbcode(text: str) -> str:
    """Steam Workshop descriptions use BBCode ([h1], [b], [url], etc.) which
    would otherwise show up as literal tag text on the site."""
    text = re.sub(r"\[/?[a-zA-Z0-9=\"'.:/_ -]+\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def resolve_persona_name(steamid64: str, daemon_conf: dict):
    """Steam's GetPublishedFileDetails only returns a raw SteamID64 for the
    creator; resolving it to a display name needs the separate
    ISteamUser/GetPlayerSummaries endpoint and its own Web API key."""
    if not steamid64:
        return None
    api_key = daemon_conf.get("STEAM_API_KEY", "").strip()
    if not api_key:
        return None
    import requests
    try:
        r = requests.get(
            "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
            params={"key": api_key, "steamids": steamid64}, timeout=10,
        )
        r.raise_for_status()
        players = r.json().get("response", {}).get("players", [])
        if players:
            return players[0].get("personaname")
    except Exception:
        pass
    return None


def get_published_file_details(wid: str):
    import requests
    r = requests.post(
        STEAM_PUBLISHED_FILE_DETAILS_URL,
        data={"itemcount": 1, "publishedfileids[0]": wid},
        timeout=15,
    )
    r.raise_for_status()
    details = r.json().get("response", {}).get("publishedfiledetails", [{}])[0]
    return details


def load_maps_json():
    with open(MAPS_JSON, encoding="utf-8") as f:
        return json.load(f)


def save_maps_json(maps):
    with open(MAPS_JSON, "w", encoding="utf-8") as f:
        json.dump(maps, f, indent=2)
        f.write("\n")


def find_existing_entry(maps, wid: str):
    for entry in maps:
        steam_url = entry.get("steamUrl", "")
        if f"id={wid}" in steam_url:
            return entry
    return None


def git(*args, cwd=REPO_DIR, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                           capture_output=True, text=True)


IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".png"}


# Boilerplate files bundled with every UDK-based Rocket League workshop map
# by the SDK/editor itself — not the actual map, regardless of size.
BOILERPLATE_FILENAMES = {"maptemplates.upk", "editorlandscaperesources.upk",
                          "workshopiteminfo.json"}


def largest_file(root_dir: str, exclude_exts=frozenset()):
    best_path, best_size = None, -1
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in exclude_exts:
                continue
            if fn.lower() in BOILERPLATE_FILENAMES:
                continue
            p = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size > best_size:
                best_path, best_size = p, size
    return best_path


def find_bundled_preview(root_dir: str):
    """Depot downloads often already include a preview image (e.g.
    <MapName>Preview.jpg) alongside the map file itself — prefer that over
    hitting Steam's API-reported preview_url or falling back to a placeholder."""
    candidates = []
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in IMAGE_EXTS:
                p = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size > 1024:  # skip tiny icons/thumbnails
                    candidates.append((p, size, "preview" in fn.lower()))
    if not candidates:
        return None
    # Prefer filenames containing "preview", then largest.
    candidates.sort(key=lambda c: (c[2], c[1]), reverse=True)
    return candidates[0][0]


def download_preview(url: str, dest_path: str) -> bool:
    import requests
    try:
        r = requests.get(url, timeout=20, allow_redirects=True)
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            return False
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception:
        return False


def rclone_upload(local_path: str, remote_folder: str) -> bool:
    result = subprocess.run(
        ["rclone", "copy", local_path, f"r2:files/{remote_folder}/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"  rclone failed (exit {result.returncode}): {result.stderr.strip()[:500]}")
    return result.returncode == 0


def process_job(wid: str, git_branch: str = "main", report=None) -> dict:
    """Runs one full job: validate -> dedupe -> download -> upload -> commit+push.
    Returns {status, message?, entry?} matching the Worker's /job-result shape.
    status is one of: done, failed, duplicate, not_rl, not_found

    `report(status, **fields)`, if given, is called for mid-flight progress
    updates (e.g. title becoming known, status moving to uploading) so a
    persistent site-wide activity list can show more than just "queued"."""
    if report is None:
        report = lambda *a, **k: None

    assella = load_assella_cli()
    daemon_conf = load_daemon_conf()

    # 1. Validate + enrich via Steam (before spending any paid API credit)
    log(f"[{wid}] Looking up published file details...")
    try:
        details = get_published_file_details(wid)
    except Exception as e:
        return {"status": "failed", "message": f"Steam lookup failed: {e}"}

    if details.get("result") != 1:
        return {"status": "not_found"}

    consumer_appid = str(details.get("consumer_app_id", ""))
    if consumer_appid != RL_APPID:
        return {"status": "not_rl", "message": f"consumer_app_id={consumer_appid}"}

    title = details.get("title") or f"Workshop Item {wid}"
    creator_steamid = details.get("creator", "")
    description = strip_bbcode(details.get("description", "") or "")
    preview_url = details.get("preview_url", "")
    author = resolve_persona_name(creator_steamid, daemon_conf) or creator_steamid or "Unknown"
    log(f"[{wid}] Title: {title!r}, appid={consumer_appid}, author={author!r}")
    report("downloading", title=title)

    # 2. De-dupe against maps.json (belt-and-braces beyond Worker KV)
    git("fetch", "origin")
    git("checkout", git_branch)
    git("pull", "--rebase", "origin", git_branch, check=False)
    maps = load_maps_json()
    existing = find_existing_entry(maps, wid)
    if existing:
        log(f"[{wid}] Already present in maps.json, skipping.")
        return {"status": "duplicate", "entry": existing}

    # 3. Download
    conf = assella.load_conf()
    api_key = daemon_conf.get("MORRENUS_API_KEY") or conf.get("morrenus_api_key", "").strip()
    if not api_key:
        return {"status": "failed", "message": "No morrenus API key configured."}

    max_downloads = int(conf.get("workshop_max_downloads", 4) or 4)
    # In CI (GitHub Actions), DepotDownloaderMod is built fresh from its own
    # public source (SteamAutoCracks/DepotDownloaderMod) and the path is
    # passed in via env var — there's no local ASSella AppImage to extract
    # from there. Locally, fall back to the usual AppImage-extraction cache.
    ddm_dll = daemon_conf.get("DDM_DLL_PATH") or assella.ensure_deps()

    with tempfile.TemporaryDirectory(prefix="map_request_") as scratch:
        log(f"[{wid}] Downloading...")
        results = assella.download_items(
            [wid], api_key, max_downloads, cellid=None,
            steam_integration=False, dest_path=scratch, ddm_dll=ddm_dll, log=log,
        )
        result = results[0] if results else {"success": False, "error": "no_result"}
        if not result.get("success"):
            return {"status": "failed",
                     "message": f"Download failed: {result.get('error')}"}

        out_dir = result["out_dir"]
        map_file = largest_file(out_dir, exclude_exts=IMAGE_EXTS)
        if not map_file:
            return {"status": "failed", "message": "No output file found after download."}
        map_ext = os.path.splitext(map_file)[1].lstrip(".").lower() or "upk"

        # 5. Preview image: prefer one already bundled in the depot download,
        # then Steam's API-reported preview_url, then a generic placeholder.
        preview_local = find_bundled_preview(out_dir)
        if preview_local:
            log(f"[{wid}] Using bundled preview image: {os.path.basename(preview_local)}")
        else:
            fetched_path = os.path.join(scratch, "preview.jpg")
            if preview_url and download_preview(preview_url, fetched_path):
                preview_local = fetched_path
                log(f"[{wid}] Using Steam API preview_url.")
            elif os.path.exists(NO_PREVIEW_FALLBACK):
                preview_local = NO_PREVIEW_FALLBACK
                log(f"[{wid}] No preview found, using fallback placeholder.")
            else:
                preview_local = None
                log(f"[{wid}] No preview and no fallback placeholder present.")

        # 6. Upload to R2
        folder = sanitize_title(title)
        report("uploading", title=title)
        log(f"[{wid}] Uploading to R2 under files/{folder}/ ...")
        if not rclone_upload(map_file, folder):
            return {"status": "failed", "message": "R2 upload of map file failed."}
        preview_filename = None
        if preview_local:
            preview_filename = os.path.basename(preview_local)
            if not rclone_upload(preview_local, folder):
                log(f"[{wid}] Preview upload failed, continuing without preview URL.")
                preview_filename = None

    # 7. Update maps.json
    git("fetch", "origin")
    git("checkout", git_branch)
    git("pull", "--rebase", "origin", git_branch, check=False)
    maps = load_maps_json()
    if find_existing_entry(maps, wid):
        log(f"[{wid}] Became duplicate during processing, skipping maps.json edit.")
        return {"status": "duplicate"}

    base_url = f"https://files.xplodingeggo.space/{folder}"
    entry = {
        "Title": title,
        "Author": author,
        "Description": description[:280],
        "category": ["other"],
        "PreviewUrl": f"{base_url}/{preview_filename}" if preview_filename else "",
        "downloadUrl": f"{base_url}/{os.path.basename(map_file)}",
        "steamUrl": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
        "format": map_ext,
    }
    maps.append(entry)
    save_maps_json(maps)

    git("add", "maps.json")
    git("commit", "-m", f"Add {title} via automated workshop request (wid {wid})")
    push = git("push", "origin", git_branch, check=False)
    if push.returncode != 0:
        log(f"[{wid}] Push failed, retrying after pull --rebase...")
        git("pull", "--rebase", "origin", git_branch, check=False)
        push = git("push", "origin", git_branch, check=False)
        if push.returncode != 0:
            return {"status": "failed",
                     "message": "git push conflict, needs manual resolution "
                                "(commit kept locally)."}

    log(f"[{wid}] Done: {title}")
    return {"status": "done", "entry": entry}


POLL_INTERVAL_SECONDS = 20


def _worker_auth():
    conf = load_daemon_conf()
    base_url = conf.get("WORKER_BASE_URL", "").rstrip("/")
    secret = conf.get("DAEMON_SECRET", "")
    if not base_url or not secret:
        sys.exit(f"WORKER_BASE_URL and DAEMON_SECRET must be set "
                  f"(in {DAEMON_CONF_PATH} or as env vars).")
    return base_url, {"Authorization": f"Bearer {secret}"}


def try_one_job(base_url: str, headers: dict, git_branch: str) -> bool:
    """Claims and processes at most one queued job. Returns True if a job
    was found (processed or not), False if the queue was empty."""
    import requests

    try:
        r = requests.get(f"{base_url}/next-job", headers=headers, timeout=15)
    except Exception as e:
        log(f"Poll failed: {e}")
        return False

    if r.status_code == 204:
        return False
    if r.status_code != 200:
        log(f"Unexpected /next-job response: {r.status_code} {r.text[:200]}")
        return False

    job = r.json()
    job_id, wid = job["jobId"], job["wid"]
    log(f"Picked up job {job_id} (wid={wid})")

    def report(status, **fields):
        try:
            requests.post(f"{base_url}/job-result", headers=headers, timeout=15,
                          json={"jobId": job_id, "status": status, **fields})
        except Exception as e:
            log(f"Progress report failed (non-fatal): {e}")

    try:
        result = process_job(wid, git_branch=git_branch, report=report)
    except Exception as e:
        log(f"process_job crashed: {e}")
        result = {"status": "failed", "message": f"daemon exception: {e}"}

    try:
        requests.post(f"{base_url}/job-result", headers=headers, timeout=15,
                      json={"jobId": job_id, **result})
    except Exception as e:
        log(f"Failed to report job result: {e}")

    return True


def run_poll_loop(git_branch: str):
    base_url, headers = _worker_auth()
    log(f"Polling {base_url} every {POLL_INTERVAL_SECONDS}s...")
    while True:
        found = try_one_job(base_url, headers, git_branch)
        if not found:
            time.sleep(POLL_INTERVAL_SECONDS)
        # If a job was found, loop immediately to check for another.


def run_once(git_branch: str):
    """Drains the current queue (one job at a time) then exits — the mode
    used by the GitHub Actions workflow, which runs on a schedule instead
    of staying resident."""
    base_url, headers = _worker_auth()
    log(f"Draining queue at {base_url} (single CI run)...")
    processed = 0
    while try_one_job(base_url, headers, git_branch):
        processed += 1
    log(f"Done. Processed {processed} job(s).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-wid", help="Run one job standalone, no Worker involved.")
    ap.add_argument("--branch", default="main",
                     help="Git branch to commit/push to (use a test branch for --test-wid).")
    ap.add_argument("--once", action="store_true",
                     help="Drain the current queue once and exit (for CI), "
                          "instead of polling forever.")
    args = ap.parse_args()

    if args.test_wid:
        result = process_job(args.test_wid, git_branch=args.branch)
        print(json.dumps(result, indent=2))
        return

    if args.once:
        run_once(git_branch=args.branch)
        return

    run_poll_loop(git_branch=args.branch)


if __name__ == "__main__":
    main()
