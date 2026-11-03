#!/usr/bin/env python3
"""Install and update the WoW Forever addons on macOS.

    python3 wow_addons.py             install or update, asks before changing anything
    python3 wow_addons.py --check     show what would change, change nothing
    python3 wow_addons.py --open-pages    also open the CurseForge pages for manual addons
    python3 wow_addons.py --help      every option

Automatic, from each project's official GitHub releases:
    Spoken and its English voice packs, Dialogue UI, Questie

Semi-automatic, because these publish no GitHub release for Forever and the CurseForge
API needs a reviewed developer key:
    Baganator, Syndicator (needed by Baganator), Auctionator, GatherLite
    Download the Forever zip from CurseForge in your browser and leave it in ~/Downloads.
    This script recognises it by its contents, checks it and installs it.

Safety rules:
    * Runs as your normal user, never as root, and uses no password or API key.
    * Never runs anything it downloads.
    * HTTPS only.  Automatic downloads must start at github.com.
    * Checks size and GitHub's SHA-256 digest before unpacking.
    * Rejects an archive with path traversal, links, app bundles, native binaries,
      folders outside the addon's expected names, or a folder with no .toc file.
    * Leaves stray scripts out of the install and clears every executable bit.
    * Touches only its own addon folders, never WTF or your saved settings.
    * Puts the old version back if a swap fails.

Needs only what macOS provides: python3 (Xcode Command Line Tools) and curl.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

GITHUB_API = "https://api.github.com"
DOWNLOAD_PREFIX = "https://github.com/"
CURL_PROTOCOL = "=https"

FLAVOR_FOLDER = "_forever_"
FOREVER_INTERFACE = "16001"
MANAGER_FOLDER = ".wow-addon-manager"

MAX_UNPACKED_BYTES = 12 * 1024 ** 3
MAX_IMPORT_ZIP_BYTES = 300 * 1024 ** 2
KEEP_BACKUP_BELOW_BYTES = 200 * 1024 ** 2
DOWNLOAD_ATTEMPTS = 30
RELEASE_PAGES = 6

CF_FORMS = "https://www.curseforge.com/wow/addons/{}/files/all?page=1&pageSize=20&gameVersionTypeId=88568"

BLOCKED_SUFFIXES = {
    ".app", ".bat", ".bundle", ".cmd", ".command", ".dll", ".dmg", ".dylib", ".exe",
    ".jar", ".kext", ".mpkg", ".msi", ".pkg", ".ps1", ".scpt", ".scptd", ".so",
    ".terminal", ".vbs", ".workflow",
}
LEFT_OUT_SUFFIXES = {
    ".bash", ".js", ".mjs", ".php", ".pl", ".py", ".rb", ".sh", ".ts", ".zsh",
}
BLOCKED_MAGIC = {
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\x7fELF", b"MZ\x90\x00",
}


@dataclass(frozen=True)
class GitHubAddon:
    name: str
    repo: str
    tag: str
    asset: str
    prefixes: Tuple[str, ...]


@dataclass(frozen=True)
class ManualAddon:
    name: str
    folder: str
    prefixes: Tuple[str, ...]
    page: str


SPOKEN_REPO = "rusty-key/spoken-wow"
GITHUB_ADDONS = (
    GitHubAddon("Spoken", SPOKEN_REPO, r"^spoken/v\d+\.\d+\.\d+$",
                r"^Spoken-\d[\w.]*\.zip$", ("Spoken",)),
    GitHubAddon("Dialogue UI", "Peterodox/YUI-Dialogue", r"^v\d",
                r"^DialogueUI-.*\.zip$", ("DialogueUI",)),
    GitHubAddon("Questie", "Questie/Questie", r"^bundle/v\d",
                r"^Questie.*\.zip$", ("Questie",)),
    GitHubAddon("Spoken voices: quests", SPOKEN_REPO, r"^quests-audio/v\d+\.\d+\.\d+$",
                r"^SpokenQuestsAudioAll-.*\.zip$", ("SpokenQuestsAudio",)),
    GitHubAddon("Spoken voices: places", SPOKEN_REPO, r"^places-audio/v\d+\.\d+\.\d+$",
                r"^SpokenPlacesAudio-.*\.zip$", ("SpokenPlacesAudio",)),
    GitHubAddon("Spoken voices: books", SPOKEN_REPO, r"^readables-audio/v\d+\.\d+\.\d+$",
                r"^SpokenReadablesAudio-.*\.zip$", ("SpokenReadablesAudio",)),
    GitHubAddon("Spoken voices: bosses", SPOKEN_REPO, r"^bosses-audio/v\d+\.\d+\.\d+$",
                r"^SpokenBossesAudio-.*\.zip$", ("SpokenBossesAudio",)),
)
MANUAL_ADDONS = (
    ManualAddon("Baganator", "Baganator", ("Baganator",), CF_FORMS.format("baganator")),
    ManualAddon("Syndicator", "Syndicator", ("Syndicator",), CF_FORMS.format("syndicator")),
    ManualAddon("Auctionator", "Auctionator", ("Auctionator",), CF_FORMS.format("auctionator")),
    ManualAddon("GatherLite", "GatherLite", ("GatherLite",), CF_FORMS.format("gatherlite")),
)


class Unsafe(Exception):
    """The archive failed a safety check, so nothing from it may be installed."""


class DownloadFailed(Exception):
    """The file could not be fetched or did not match what the release promised."""


@dataclass
class Job:
    name: str
    version: str
    size: int
    prefixes: Tuple[str, ...]
    identity: str
    url: Optional[str] = None
    sha256: Optional[str] = None
    part_name: Optional[str] = None
    local_zip: Optional[Path] = None
    source_mtime: float = 0.0


def say(message: str = "") -> None:
    print(message, flush=True)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def curl_base() -> List[str]:
    return ["curl", "--fail", "--location", "--proto", CURL_PROTOCOL,
            "--proto-redir", CURL_PROTOCOL, "--connect-timeout", "20"]


def fetch_json(url: str) -> object:
    command = curl_base() + [
        "--silent", "--show-error", "--max-time", "60", "--retry", "3", "--retry-delay", "3",
        "--header", "Accept: application/vnd.github+json",
        "--header", "X-GitHub-Api-Version: 2022-11-28", url,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip()
        if "403" in detail or "429" in detail:
            detail += "  GitHub limits anonymous requests per hour, so try again later."
        raise DownloadFailed(f"could not read {url}: {detail}")
    return json.loads(result.stdout)


def fetch_releases(wanted: List[GitHubAddon]) -> Dict[str, Tuple[dict, dict]]:
    """Find the newest stable release and asset for every wanted addon."""
    found: Dict[str, Tuple[dict, dict]] = {}
    for repo in sorted({addon.repo for addon in wanted}):
        pending = [addon for addon in wanted if addon.repo == repo]
        for page in range(1, RELEASE_PAGES + 1):
            releases = fetch_json(f"{GITHUB_API}/repos/{repo}/releases?per_page=100&page={page}")
            if not isinstance(releases, list):
                raise DownloadFailed(f"unexpected reply from GitHub for {repo}")
            for release in releases:
                if release.get("draft") or release.get("prerelease"):
                    continue
                for addon in list(pending):
                    if not re.search(addon.tag, release.get("tag_name", "")):
                        continue
                    asset = next((item for item in release.get("assets", [])
                                  if re.search(addon.asset, item.get("name", ""))), None)
                    if asset:
                        found[addon.name] = (release, asset)
                        pending.remove(addon)
            if not pending or len(releases) < 100:
                break
        for addon in pending:
            say(f"  ! no stable release found for {addon.name}")
    return found


def known_digest(asset: dict) -> Optional[str]:
    digest = asset.get("digest") or ""
    return digest[7:].lower() if digest.startswith("sha256:") else None


def job_from_release(addon: GitHubAddon, release: dict, asset: dict) -> Job:
    url = asset.get("browser_download_url", "")
    required = f"{DOWNLOAD_PREFIX}{addon.repo}/releases/download/"
    if not url.startswith(required) or re.search(r"[/\\]|\.\.", asset["name"]):
        raise DownloadFailed(f"{addon.name}: unexpected download address {url}")
    sha256 = known_digest(asset)
    return Job(name=addon.name, version=release["tag_name"], size=int(asset["size"]),
               prefixes=addon.prefixes, identity=sha256 or f"asset:{asset.get('id')}",
               url=url, sha256=sha256, part_name=f"{asset.get('id')}-{asset['name']}.part")


def is_current(job: Job, state: dict, addons_dir: Path) -> bool:
    entry = state.get(job.name)
    if not entry or entry.get("identity") != job.identity:
        return False
    return all((addons_dir / folder).is_dir() for folder in entry.get("folders", []))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(job: Job, cache: Path) -> Path:
    """Fetch a file, picking up where the last attempt stopped until it is complete."""
    if not job.url or not job.url.startswith(DOWNLOAD_PREFIX):
        raise DownloadFailed(f"{job.name}: refused download address {job.url}")
    part = cache / str(job.part_name)
    display = ["--progress-bar"] if sys.stdout.isatty() else ["--silent", "--show-error"]
    for verify_round in (1, 2):
        attempt = 0
        refusals = 0
        while not part.exists() or part.stat().st_size != job.size:
            have = part.stat().st_size if part.exists() else 0
            if have > job.size:
                part.unlink()
                have = 0
            attempt += 1
            if attempt > DOWNLOAD_ATTEMPTS:
                raise DownloadFailed(f"{job.name}: stopped after {DOWNLOAD_ATTEMPTS} attempts "
                                     f"at {human(have)}.  Run again to continue.")
            if attempt > 1:
                pause = min(5 * attempt, 30)
                say(f"  connection lost at {human(have)} of {human(job.size)}, "
                    f"resuming in {pause}s (attempt {attempt})")
                time.sleep(pause)
            result = subprocess.run(curl_base() + display + [
                "--continue-at", "-", "--speed-limit", "2048", "--speed-time", "45",
                "--output", str(part), job.url])
            if result.returncode == 33:
                part.unlink(missing_ok=True)
            refusals = refusals + 1 if result.returncode == 22 else 0
            if refusals >= 5:
                raise DownloadFailed(f"{job.name}: the server keeps refusing this download")
        if job.sha256 is None:
            say("  ! GitHub gave no digest for this file, so only its size was checked")
            return part
        if sha256_of(part) == job.sha256:
            say("  checksum OK")
            return part
        part.unlink()
        if verify_round == 1:
            say("  checksum did not match, downloading again from the start")
    raise DownloadFailed(f"{job.name}: checksum still wrong after a clean download")


def member_paths(archive: zipfile.ZipFile, prefixes: Tuple[str, ...]):
    """Check every entry and return (entries to install, entries left out)."""
    members = []
    left_out = []
    tops = set()
    with_toc = set()
    unpacked = 0
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if "\0" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise Unsafe(f"absolute path: {info.filename!r}")
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if not parts:
            continue
        if ".." in parts:
            raise Unsafe(f"path traversal: {info.filename!r}")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise Unsafe(f"link or special file: {info.filename!r}")
        if parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
            continue
        for part in parts:
            if Path(part).suffix.lower() in BLOCKED_SUFFIXES:
                raise Unsafe(f"blocked file type: {info.filename!r}")
        if info.is_dir():
            continue
        if len(parts) == 1:
            left_out.append(name)
            continue
        top = parts[0]
        if not top.startswith(prefixes):
            raise Unsafe(f"unexpected addon folder: {top!r}")
        if Path(parts[-1]).suffix.lower() in LEFT_OUT_SUFFIXES:
            left_out.append(name)
            continue
        tops.add(top)
        if len(parts) == 2 and parts[1].lower().endswith(".toc"):
            with_toc.add(top)
        unpacked += info.file_size
        members.append((info, parts))
    if not tops:
        raise Unsafe("no addon folders inside")
    if tops - with_toc:
        raise Unsafe(f"folder without a .toc file: {sorted(tops - with_toc)[0]!r}")
    if unpacked > MAX_UNPACKED_BYTES:
        raise Unsafe(f"unpacks to {human(unpacked)}, over the {human(MAX_UNPACKED_BYTES)} limit")
    return members, left_out, unpacked


def unpack(zip_path: Path, prefixes: Tuple[str, ...], stage: Path) -> int:
    """Validate the whole archive, then write plain, non-executable files to stage."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members, left_out, unpacked = member_paths(archive, prefixes)
            if shutil.disk_usage(stage.parent).free < unpacked + 500 * 1024 ** 2:
                raise DownloadFailed(f"not enough free disk space, needs {human(unpacked)}")
            for info, parts in members:
                target = stage.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    head = source.read(4)
                    if head in BLOCKED_MAGIC:
                        raise Unsafe(f"native program inside: {info.filename!r}")
                    output.write(head)
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod(0o644)
    except zipfile.BadZipFile as error:
        raise Unsafe(f"damaged archive: {error}")
    if left_out:
        say(f"  left out {len(left_out)} stray script or loose file(s)")
    return unpacked


def lists_forever(stage: Path) -> bool:
    for toc in stage.glob("*/*.toc"):
        with toc.open(errors="replace") as handle:
            for line in handle:
                if line.lower().startswith("## interface:") and FOREVER_INTERFACE in line:
                    return True
    return False


def swap_in(job: Job, stage: Path, addons_dir: Path, manager: Path, old: List[str]) -> List[str]:
    """Replace the addon's folders, restoring the previous ones if anything fails."""
    new = sorted(entry.name for entry in stage.iterdir())
    backup = manager / "backups" / re.sub(r"[^\w.-]+", "_", job.name)
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True)
    moved: List[str] = []
    placed: List[str] = []
    try:
        for folder in sorted(set(new) | set(old)):
            if os.path.lexists(addons_dir / folder):
                os.rename(addons_dir / folder, backup / folder)
                moved.append(folder)
        for folder in new:
            os.rename(stage / folder, addons_dir / folder)
            placed.append(folder)
    except BaseException:
        for folder in placed:
            shutil.rmtree(addons_dir / folder, ignore_errors=True)
        for folder in moved:
            os.rename(backup / folder, addons_dir / folder)
        raise
    kept = sum(item.stat().st_size for item in backup.rglob("*") if item.is_file())
    if kept > KEEP_BACKUP_BELOW_BYTES:
        shutil.rmtree(backup, ignore_errors=True)
    return new


def install(job: Job, cache: Path, addons_dir: Path, manager: Path, state: dict,
            state_path: Path) -> None:
    say(f"\n{job.name} {job.version}")
    zip_path = job.local_zip or download(job, cache)
    stage = manager / "staging" / "current"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        unpack(zip_path, job.prefixes, stage)
        if not lists_forever(stage):
            say(f"  ! {job.name} does not list Forever ({FOREVER_INTERFACE}) in its .toc, so it may "
                "show as out of date in game")
        owners = {folder: name for name, entry in state.items() if name != job.name
                  for folder in entry.get("folders", [])}
        clash = [entry.name for entry in stage.iterdir() if entry.name in owners]
        if clash:
            raise Unsafe(f"{clash[0]} already belongs to {owners[clash[0]]}")
        previous = state.get(job.name, {}).get("folders", [])
        folders = swap_in(job, stage, addons_dir, manager, previous)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    state[job.name] = {
        "version": job.version, "identity": job.identity, "folders": folders,
        "source_mtime": job.source_mtime,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_state(state_path, state)
    if not job.local_zip:
        zip_path.unlink(missing_ok=True)
    say(f"  installed: {', '.join(folders)}")


def identify_zip(path: Path) -> Optional[ManualAddon]:
    try:
        with zipfile.ZipFile(path) as archive:
            tops = {name.replace("\\", "/").split("/")[0] for name in archive.namelist()}
    except (zipfile.BadZipFile, OSError):
        return None
    tops -= {"__MACOSX", ".DS_Store"}
    for addon in MANUAL_ADDONS:
        if addon.folder in tops:
            return addon
    return None


def manual_jobs(downloads: Path, state: dict, addons_dir: Path) -> Tuple[List[Job], List[ManualAddon]]:
    """Match zips in the Downloads folder to the manual addons."""
    newest: Dict[str, Path] = {}
    zips = sorted(downloads.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in zips:
        if path.stat().st_size > MAX_IMPORT_ZIP_BYTES:
            continue
        addon = identify_zip(path)
        if addon and addon.name not in newest:
            newest[addon.name] = path
    jobs: List[Job] = []
    waiting: List[ManualAddon] = []
    for addon in MANUAL_ADDONS:
        path = newest.get(addon.name)
        entry = state.get(addon.name, {})
        if path is None:
            if not (addons_dir / addon.folder).is_dir():
                waiting.append(addon)
            continue
        if path.stat().st_mtime < entry.get("source_mtime", 0):
            continue
        digest = sha256_of(path)
        job = Job(name=addon.name, version=path.name, size=path.stat().st_size,
                  prefixes=addon.prefixes, identity=digest, local_zip=path,
                  source_mtime=path.stat().st_mtime)
        if not is_current(job, state, addons_dir):
            jobs.append(job)
    return jobs, waiting


def candidate_game_folders() -> List[Path]:
    home = Path.home()
    roots = [Path("/Applications/World of Warcraft"), home / "Applications/World of Warcraft"]
    roots += list(Path("/Volumes").glob("*/Applications/World of Warcraft"))
    roots += list(Path("/Volumes").glob("*/World of Warcraft"))
    return roots


def spotlight_flavor_folders() -> List[Path]:
    if not shutil.which("mdfind"):
        return []
    try:
        output = subprocess.run(["mdfind", f"kMDItemFSName == '{FLAVOR_FOLDER}'"],
                                capture_output=True, text=True, timeout=15).stdout
    except subprocess.TimeoutExpired:
        return []
    return [Path(line) for line in output.splitlines() if line.strip()]


def from_user_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.name == "AddOns":
        path = path.parent.parent
    elif (path / FLAVOR_FOLDER).is_dir():
        path = path / FLAVOR_FOLDER
    if not path.is_dir():
        sys.exit(f"Error: {path} does not exist.")
    if not re.fullmatch(r"_\w+_", path.name):
        sys.exit(f"Error: no {FLAVOR_FOLDER} folder found in {path}.")
    return path


def locate_game(raw: Optional[str]) -> Path:
    if raw:
        return from_user_path(raw)
    found = [root / FLAVOR_FOLDER for root in candidate_game_folders()
             if (root / FLAVOR_FOLDER).is_dir()]
    found += [path for path in spotlight_flavor_folders() if path.is_dir()]
    unique = sorted({path.resolve() for path in found})
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        listing = "\n  ".join(str(path) for path in unique)
        sys.exit(f"Found several WoW Forever folders:\n  {listing}\nChoose one with --wow-path.")
    sys.exit(f"Could not find a {FLAVOR_FOLDER} folder.  Install Forever through Battle.net and "
             "start it once, or pass --wow-path \"/path/to/World of Warcraft\".")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as handle:
        return json.load(handle)


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(temporary, path)


def game_is_running() -> bool:
    if not shutil.which("pgrep"):
        return False
    pattern = r"World of Warcraft.*\.app/Contents/MacOS"
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def cache_folder() -> Path:
    base = Path.home() / "Library/Caches" if sys.platform == "darwin" else Path.home() / ".cache"
    folder = base / "wow-addon-manager"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def keep_mac_awake() -> None:
    if shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install and update WoW Forever addons on macOS.")
    parser.add_argument("--check", action="store_true", help="show what would change and stop")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--wow-path", help="game folder, _forever_ folder or its AddOns folder")
    parser.add_argument("--downloads", default="~/Downloads",
                        help="where to look for CurseForge zips (default: ~/Downloads)")
    parser.add_argument("--skip", action="append", default=[], metavar="NAME",
                        help='leave an addon out, for example --skip "Spoken voices: bosses"')
    parser.add_argument("--open-pages", action="store_true",
                        help="open the CurseForge Forever file pages for the manual addons")
    parser.add_argument("--force", action="store_true", help="continue even if WoW seems to be running")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.exit("Do not run this with sudo.  It only needs your normal user.")
    if not shutil.which("curl"):
        sys.exit("curl was not found.")

    flavor = locate_game(args.wow_path)
    addons_dir = flavor / "Interface" / "AddOns"
    addons_dir.mkdir(parents=True, exist_ok=True)
    manager = flavor / MANAGER_FOLDER
    manager.mkdir(exist_ok=True)
    lock = (manager / "lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another copy of this script is already running.")
    if game_is_running() and not (args.force or args.check):
        sys.exit("WoW is running.  Quit it first, then run this again.")

    state_path = manager / "state.json"
    state = load_state(state_path)
    say(f"WoW Forever: {flavor}\n")

    wanted = [addon for addon in GITHUB_ADDONS if addon.name not in args.skip]
    say("Checking GitHub for releases...")
    try:
        releases = fetch_releases(wanted)
        github_jobs = [job_from_release(addon, *releases[addon.name])
                       for addon in wanted if addon.name in releases]
    except DownloadFailed as error:
        sys.exit(f"Error: {error}")

    downloads = Path(args.downloads).expanduser()
    imports, waiting = manual_jobs(downloads, state, addons_dir)
    jobs = imports + [job for job in github_jobs if not is_current(job, state, addons_dir)]
    jobs.sort(key=lambda job: job.size)

    current = [job.name for job in github_jobs if is_current(job, state, addons_dir)]
    if current:
        say(f"Up to date: {', '.join(current)}")
    if jobs:
        say("\nTo install or update:")
        for job in jobs:
            origin = "from Downloads" if job.local_zip else "from GitHub"
            say(f"  {job.name:<26} {human(job.size):>9}   {job.version}  ({origin})")
        total = sum(job.size for job in jobs if not job.local_zip)
        say(f"\nTo download: {human(total)}")
    else:
        say("\nNothing to install or update.")

    exit_code = 0
    if jobs and not args.check:
        needed = sum(job.size * 2 for job in jobs if not job.local_zip)
        if shutil.disk_usage(flavor).free < needed:
            sys.exit(f"Not enough free disk space.  This needs about {human(needed)} while it runs.")
        if not args.yes:
            if not sys.stdin.isatty():
                sys.exit("Add --yes to run without a confirmation prompt.")
            if input("\nContinue? [y/N] ").strip().lower() not in ("y", "yes"):
                return 0
        keep_mac_awake()
        cache = cache_folder()
        for job in jobs:
            try:
                install(job, cache, addons_dir, manager, state, state_path)
            except Unsafe as error:
                say(f"  BLOCKED, nothing installed for {job.name}: {error}")
                exit_code = 1
            except (DownloadFailed, OSError) as error:
                say(f"  FAILED: {error}")
                exit_code = 1

    say("\nManual addons, from CurseForge:")
    for addon in MANUAL_ADDONS:
        entry = state.get(addon.name)
        missing = addon in waiting
        status = "not installed yet" if missing else (
            f"installed {entry['installed_at'][:10]}" if entry else "installed outside this script")
        say(f"  {addon.name:<12} {status}")
    if waiting:
        say("\nDownload the Forever file for each of these, then run this script again:")
        for addon in waiting:
            say(f"  {addon.name}: {addon.page}")
    say("\nThese can't be checked for updates here.  To update one, download its newest Forever "
        "zip and run this again.")
    if args.open_pages and shutil.which("open"):
        for addon in MANUAL_ADDONS:
            subprocess.run(["open", addon.page])
    if exit_code:
        say("\nSome items failed.  Run this again to continue: finished items are kept and "
            "partial downloads resume.")
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nStopped.  Run this again to continue: partial downloads resume.")
