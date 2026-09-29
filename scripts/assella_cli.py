#!/usr/bin/env python3
"""Headless CLI for ACCELA/ASSella workshop downloads.

Reimplements core/tasks/download_workshop_task.py's DownloadWorkshopTask.run()
logic without Qt, so it can run in a plain terminal instead of only through
the GUI's job queue.

Reads settings ACCELA itself already saved:
  - API key      : ~/.config/Tachibana Labs/ACCELA.conf  [morrenus_api_key]
  - manifests/keys: ~/.local/share/ACCELA/{manifests,workshop_keys.txt}
  - DepotDownloader.dll + DepotDownloader.deps.json/.runtimeconfig.json:
    extracted from the installed AppImage (cached under
    ~/.local/share/ACCELA/_cli_deps, refreshed if the AppImage changes)

Usage:
  python3 ASSella_cli.py 1906378036 2968144588
  python3 ASSella_cli.py --steam 1906378036          # write into real steamapps/workshop
  python3 ASSella_cli.py --no-steam 1906378036        # force loose 'mods/' output dir
"""
import argparse
import configparser
import os
import re
import subprocess
import sys
import time
import shutil

BASE_PATH = os.path.expanduser("~/.local/share/ACCELA")
CONF_PATH = os.path.expanduser("~/.config/Tachibana Labs/ACCELA.conf")
APPIMAGE_PATH = os.path.join(BASE_PATH, "ASSella.AppImage")
DEPS_CACHE = os.path.join(BASE_PATH, "_cli_deps")
API_BASE_URL = "https://hubcapmanifest.com/api/v1"


def load_conf():
    cfg = configparser.ConfigParser()
    cfg.read(CONF_PATH, encoding="utf-8")
    return cfg["General"] if "General" in cfg else {}


def ensure_deps():
    """Extract DepotDownloader.dll + friends from the AppImage into a stable
    cache dir, refreshing only if the AppImage was updated."""
    dll = os.path.join(DEPS_CACHE, "DepotDownloader.dll")
    marker = os.path.join(DEPS_CACHE, ".source_mtime")
    if not os.path.exists(APPIMAGE_PATH):
        sys.exit(f"AppImage not found: {APPIMAGE_PATH}")

    src_mtime = str(os.path.getmtime(APPIMAGE_PATH))
    cached_ok = (
        os.path.exists(dll)
        and os.path.exists(marker)
        and open(marker).read().strip() == src_mtime
    )
    if cached_ok:
        return dll

    import tempfile
    print("→ Extracting DepotDownloader runtime from AppImage (one-time)...")
    os.makedirs(DEPS_CACHE, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [APPIMAGE_PATH, "--appimage-extract", "bin/src/deps"],
            cwd=tmp, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        extracted = os.path.join(tmp, "squashfs-root", "bin", "src", "deps")
        if not os.path.isdir(extracted):
            sys.exit(f"Extraction failed: expected {extracted}")
        if os.path.exists(DEPS_CACHE):
            shutil.rmtree(DEPS_CACHE)
        shutil.copytree(extracted, DEPS_CACHE)
    with open(marker, "w") as f:
        f.write(src_mtime)
    return dll


def get_dotnet_path():
    return shutil.which("dotnet")


def parse_workshop_ids(text: str):
    tokens = re.split(r"[\s,]+", text)
    ids = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        m = re.search(r"[?&]id=(\d+)", t)
        if m:
            ids.append(m.group(1))
        elif t.isdigit():
            ids.append(t)
    return list(dict.fromkeys(ids))


def find_steam_install():
    home = os.path.expanduser("~")
    for c in (
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".steam", "root"),
        os.path.join(home, ".local", "share", "Steam"),
    ):
        if os.path.isdir(os.path.join(c, "steamapps")):
            return c
    return None


def fetch_manifest(wid, api_key, manifests_dir, log):
    import requests
    try:
        r = requests.get(
            f"{API_BASE_URL}/generate/workshopmanifest/{wid}",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        log(f"  ✗ Manifest request failed: {e}")
        return None
    appid = r.headers.get("X-App-Id")
    manifest_id = r.headers.get("X-Manifest-Id")
    depot_key = r.headers.get("X-Depot-Key")
    if not appid or not manifest_id:
        log("  ✗ Missing required headers in manifest response.")
        return None
    manifest_path = os.path.join(manifests_dir, f"{appid}_{manifest_id}.manifest")
    with open(manifest_path, "wb") as f:
        f.write(r.content)
    return {"appid": appid, "manifest_id": manifest_id, "depot_key": depot_key,
            "manifest_path": manifest_path}


def key_exists(keys_file, appid):
    if not os.path.exists(keys_file):
        return False
    with open(keys_file, encoding="utf-8") as f:
        return any(line.strip().startswith(f"{appid};") for line in f)


def save_key(keys_file, appid, key):
    with open(keys_file, "a", encoding="utf-8") as f:
        f.write(f"{appid};{key}\n")


def _get_dir_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


def _parse_acf_block(text):
    result, stack, current_key = {}, [{}], None
    stack[0] = result
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        for tok in re.findall(r'"[^"]*"|\{|\}', s):
            if tok == "{":
                new = {}
                if current_key is not None:
                    stack[-1][current_key] = new
                    stack.append(new)
                    current_key = None
            elif tok == "}":
                if len(stack) > 1:
                    stack.pop()
            else:
                val = tok.strip('"')
                if current_key is None:
                    current_key = val
                else:
                    stack[-1][current_key] = val
                    current_key = None
    return result


def _write_acf(path, appid, items):
    existing, root_meta = {}, {}
    if os.path.exists(path):
        parsed = _parse_acf_block(open(path, encoding="utf-8").read())
        aw = parsed.get("AppWorkshop", {})
        root_meta = {k: v for k, v in aw.items()
                     if k not in ("WorkshopItemsInstalled", "WorkshopItemDetails",
                                  "NeedsUpdate", "NeedsDownload") and isinstance(v, str)}
        installed = aw.get("WorkshopItemsInstalled", {})
        details = aw.get("WorkshopItemDetails", {})
        for wid, data in installed.items():
            existing[wid] = {
                "size": data.get("size", "0"),
                "timeupdated": data.get("timeupdated", "0"),
                "manifest": data.get("manifest", ""),
                "timetouched": details.get(wid, {}).get("timetouched", "0"),
                "subscribedby": details.get(wid, {}).get("subscribedby", "0"),
            }
    for wid, info in items.items():
        existing[wid] = {
            "size": str(info["size"]), "timeupdated": str(info["timeupdated"]),
            "manifest": str(info["manifest"]),
            "timetouched": existing.get(wid, {}).get("timetouched", "0"),
            "subscribedby": existing.get(wid, {}).get("subscribedby", "0"),
        }

    def q(v):
        return f'"{v}"'

    lines = ['"AppWorkshop"', "{", f'\t"appid"\t\t{q(appid)}']
    for k, v in root_meta.items():
        if k != "appid":
            lines.append(f'\t{q(k)}\t\t{q(v)}')
    lines += ['\t"NeedsUpdate"\t\t"0"', '\t"NeedsDownload"\t\t"0"',
              '\t"WorkshopItemsInstalled"', "\t{"]
    for wid, d in existing.items():
        lines += [f'\t\t{q(wid)}', "\t\t{",
                  f'\t\t\t"size"\t\t{q(d["size"])}',
                  f'\t\t\t"timeupdated"\t\t{q(d["timeupdated"])}',
                  f'\t\t\t"manifest"\t\t{q(d["manifest"])}', "\t\t}"]
    lines += ["\t}", '\t"WorkshopItemDetails"', "\t{"]
    for wid, d in existing.items():
        lines += [f'\t\t{q(wid)}', "\t\t{",
                  f'\t\t\t"manifest"\t\t{q(d["manifest"])}',
                  f'\t\t\t"timeupdated"\t\t{q(d["timeupdated"])}',
                  f'\t\t\t"timetouched"\t\t{q(d["timetouched"])}',
                  f'\t\t\t"subscribedby"\t\t{q(d["subscribedby"])}',
                  f'\t\t\t"latest_timeupdated"\t\t{q(d["timeupdated"])}',
                  f'\t\t\t"latest_manifest"\t\t{q(d["manifest"])}', "\t\t}"]
    lines += ["\t}", "}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def apply_steam_integration(appid, wid, manifest_id, mod_dir, dest_path, log):
    acf_path = os.path.join(dest_path, "steamapps", "workshop", f"appworkshop_{appid}.acf")
    size, now = _get_dir_size(mod_dir), int(time.time())
    try:
        _write_acf(acf_path, appid, {wid: {"size": size, "timeupdated": now,
                                            "manifest": manifest_id}})
        log(f"  ✓ ACF updated → {acf_path}")
    except Exception as e:
        log(f"  ✗ Failed to update ACF: {e}")


def download_items(wids, api_key, max_downloads, cellid, steam_integration,
                    dest_path, ddm_dll, log):
    """Returns a list of per-wid result dicts:
    {wid, success, appid, manifest_id, out_dir, error}
    (error is a short reason string when success is False)."""
    manifests_dir = os.path.join(BASE_PATH, "manifests")
    keys_file = os.path.join(BASE_PATH, "workshop_keys.txt")
    os.makedirs(manifests_dir, exist_ok=True)
    results = []

    dotnet_exe = get_dotnet_path()
    if not dotnet_exe:
        sys.exit("dotnet runtime not found. Install .NET 9 runtime.")

    for wid in wids:
        log(f"\n{'─' * 52}\n  Workshop ID : {wid}\n  → Fetching manifest...")
        info = fetch_manifest(wid, api_key, manifests_dir, log)
        if not info:
            log("  ✗ Could not fetch manifest. Skipping.")
            results.append({"wid": wid, "success": False, "error": "manifest_fetch_failed"})
            continue

        appid, manifest_id, depot_key, manifest_path = (
            info["appid"], info["manifest_id"], info["depot_key"], info["manifest_path"])
        log(f"  ✓ App ID     : {appid}\n  ✓ Manifest ID: {manifest_id}\n"
            f"  ✓ Manifest   : {manifest_path}")

        if not depot_key:
            log("  ✗ No depot key in response. Skipping.")
            results.append({"wid": wid, "success": False, "error": "no_depot_key"})
            continue

        if not key_exists(keys_file, appid):
            save_key(keys_file, appid, depot_key)
            log(f"  ✓ Key saved  : {appid};{depot_key[:10]}…")
        else:
            log(f"  ✓ Depot key for App ID {appid} already cached.")

        if steam_integration and dest_path:
            out_dir = os.path.join(dest_path, "steamapps", "workshop", "content", appid, wid)
        else:
            out_dir = os.path.join(dest_path or BASE_PATH, "mods", appid, wid)

        cmd = [dotnet_exe, ddm_dll, "-app", appid, "-ugc", wid,
               "-manifestfile", manifest_path, "-depotkeys", keys_file,
               "-dir", out_dir, "-max-downloads", str(max_downloads)]
        if cellid:
            cmd += ["-cellid", str(cellid)]

        log(f"  → Running    : {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")
            for line in proc.stdout:
                log(f"    {line.rstrip()}")
            proc.wait()
            if proc.returncode != 0:
                log(f"  ✗ DepotDownloader exited with code {proc.returncode}.")
                results.append({"wid": wid, "success": False,
                                 "error": f"depotdownloader_exit_{proc.returncode}"})
                continue
            log(f"  ✓ Download complete → {out_dir}")
        except Exception as e:
            log(f"  ✗ Failed to launch DepotDownloader: {e}")
            results.append({"wid": wid, "success": False, "error": f"launch_failed: {e}"})
            continue

        if steam_integration and dest_path:
            apply_steam_integration(appid, wid, manifest_id, out_dir, dest_path, log)

        results.append({"wid": wid, "success": True, "appid": appid,
                         "manifest_id": manifest_id, "out_dir": out_dir})

    log(f"\n{'─' * 52}\n  All tasks finished.")
    return results


def main():
    conf = load_conf()

    ap = argparse.ArgumentParser(description="ACCELA/ASSella workshop downloader (CLI)")
    ap.add_argument("ids", nargs="+", help="Workshop IDs or Steam workshop URLs")
    ap.add_argument("--api-key", default=None,
                     help="Overrides morrenus_api_key from ACCELA.conf")
    ap.add_argument("--max-downloads", type=int,
                     default=int(conf.get("workshop_max_downloads", 4) or 4))
    ap.add_argument("--cellid", default=conf.get("workshop_cell_id", "") or None)
    steam_default = str(conf.get("workshop_steam_enabled", "true")).lower() == "true"
    ap.add_argument("--steam", dest="steam", action="store_true", default=steam_default,
                     help="Write into real steamapps/workshop (default: matches ACCELA setting)")
    ap.add_argument("--no-steam", dest="steam", action="store_false",
                     help="Force loose output under mods/<appid>/<wid>/")
    ap.add_argument("--dest", default=None,
                     help="Steam library root (default: auto-detected install)")
    args = ap.parse_args()

    api_key = args.api_key or conf.get("morrenus_api_key", "").strip()
    if not api_key:
        sys.exit("No API key found (morrenus_api_key missing from ACCELA.conf; "
                  "set it via the app once, or pass --api-key).")

    wids = parse_workshop_ids(" ".join(args.ids))
    if not wids:
        sys.exit("No valid workshop IDs parsed from input.")

    dest_path = args.dest or find_steam_install()
    if args.steam and not dest_path:
        sys.exit("Could not auto-detect Steam install; pass --dest /path/to/steam/root")

    ddm_dll = ensure_deps()

    download_items(wids, api_key, args.max_downloads, args.cellid,
                    args.steam, dest_path, ddm_dll, log=print)


if __name__ == "__main__":
    main()
