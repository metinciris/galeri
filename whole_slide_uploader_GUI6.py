#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Whole Slide GitHub Uploader - GUI + resumable single-file workflow.

Default:
    python whole_slide_uploader.py

The default mode opens a Tkinter desktop interface. The application scans the
"yuklenecek" directory, asks for title/description/thumbnail for every SVS,
then uploads all prepared slides. Progress is persisted in *.svs.upload.json so
an interrupted upload can continue after power/network failure.

The program keeps the original Turkish directory names used by the project:
    y\u00fcklenecek/
    y\u00fcklenen/
    repos/

.env example:
    GITHUB_USERNAME=metinciris
    GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx
    LOCAL_REPO_BASE=repos

Optional .env values:
    GALLERY_REPO_NAME=galeri
    REPO_PREFIX=gallery-
    REPO_DIGITS=3
    GITHUB_API_VERSION=2026-03-10
    THUMB_MAX_PX=1000
    THUMB_TARGET_KB=500
    PAGES_VERIFY_TIMEOUT=300
    PAGES_SAFE_LIMIT_MIB=950
"""

from __future__ import annotations

import argparse
import base64
import gc
import html
import json
import logging
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

APP_VERSION = "2026.08.19-GUI6"

try:
    import requests
except ImportError as exc:
    raise SystemExit("Eksik paket: requests. Kurulum: pip install requests") from exc


# -----------------------------------------------------------------------------
# Paths / configuration
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
INBOX_DIR = BASE_DIR / "y\u00fcklenecek"
DONE_DIR = BASE_DIR / "y\u00fcklenen"
LOG_PATH = BASE_DIR / "uploader.log"
UI_SETTINGS_PATH = BASE_DIR / ".uploader-ui.json"
MARKER_NAME = ".uploader-source.json"
META_SUFFIX = ".upload.json"

os.environ.setdefault("VIPS_WARNING", "0")


def load_simple_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_simple_env(ENV_PATH)

GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GALLERY_REPO_NAME = os.getenv("GALLERY_REPO_NAME", "galeri").strip() or "galeri"
REPO_PREFIX = os.getenv("REPO_PREFIX", "gallery-").strip() or "gallery-"
REPO_DIGITS = max(1, int(os.getenv("REPO_DIGITS", "3")))
GITHUB_API_VERSION = os.getenv("GITHUB_API_VERSION", "2026-03-10").strip() or "2026-03-10"
THUMB_MAX_PX = max(300, int(os.getenv("THUMB_MAX_PX", "1000")))
THUMB_TARGET_BYTES = max(100, int(os.getenv("THUMB_TARGET_KB", "500"))) * 1024
PAGES_VERIFY_TIMEOUT = max(60, int(os.getenv("PAGES_VERIFY_TIMEOUT", "300")))
PAGES_SAFE_LIMIT_BYTES = max(100, int(os.getenv("PAGES_SAFE_LIMIT_MIB", "950"))) * 1024 * 1024

_repo_base_raw = os.getenv("LOCAL_REPO_BASE", "repos").strip() or "repos"
LOCAL_REPO_BASE = Path(_repo_base_raw)
if not LOCAL_REPO_BASE.is_absolute():
    LOCAL_REPO_BASE = BASE_DIR / LOCAL_REPO_BASE
LOCAL_REPO_BASE = LOCAL_REPO_BASE.resolve()

for directory in (INBOX_DIR, DONE_DIR, LOCAL_REPO_BASE):
    directory.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)
LOGGER = logging.getLogger("whole-slide-uploader")

SESSION = requests.Session()
SESSION.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "whole-slide-uploader",
    }
)
if GITHUB_TOKEN:
    SESSION.headers.update({"Authorization": f"Bearer {GITHUB_TOKEN}"})
API_ROOT = "https://api.github.com"


# -----------------------------------------------------------------------------
# Events / errors / retries
# -----------------------------------------------------------------------------

class UploaderError(RuntimeError):
    pass


EVENT_SINK: Optional[Callable[[dict], None]] = None


def set_event_sink(callback: Optional[Callable[[dict], None]]) -> None:
    global EVENT_SINK
    EVENT_SINK = callback


def emit(
    kind: str,
    message: str,
    *,
    repo: Optional[str] = None,
    stage: Optional[str] = None,
    progress: Optional[int] = None,
    **extra: Any,
) -> None:
    LOGGER.info("%s%s", f"[{repo}] " if repo else "", message)
    if EVENT_SINK:
        payload = {
            "kind": kind,
            "message": message,
            "repo": repo,
            "stage": stage,
            "progress": progress,
        }
        payload.update(extra)
        try:
            EVENT_SINK(payload)
        except Exception:
            LOGGER.exception("Event sink failed")
    else:
        prefix = "UYARI: " if kind == "warning" else ""
        print(prefix + message, flush=True)


def say(message: str, *, repo: Optional[str] = None, stage: Optional[str] = None, progress: Optional[int] = None) -> None:
    emit("info", message, repo=repo, stage=stage, progress=progress)


def warn(message: str, *, repo: Optional[str] = None, stage: Optional[str] = None) -> None:
    emit("warning", message, repo=repo, stage=stage)


def fail(message: str) -> None:
    LOGGER.error(message)
    raise UploaderError(message)


def require_config() -> None:
    missing = []
    if not GITHUB_USERNAME:
        missing.append("GITHUB_USERNAME")
    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")
    if missing:
        fail(".env icinde eksik alan(lar): " + ", ".join(missing))


def _response_message(response: requests.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(data.get("message") or response.text[:500])
    except Exception:
        pass
    return response.text[:500]


def api_request(
    method: str,
    path: str,
    *,
    expected: Sequence[int] = (200,),
    timeout: int = 60,
    retries: int = 4,
    **kwargs: Any,
) -> requests.Response:
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = SESSION.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(12, 2 ** (attempt - 1)))
                continue
            raise UploaderError(f"GitHub API baglanti hatasi: {exc}") from exc

        if response.status_code in expected:
            return response
        if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else min(15, 2 ** (attempt - 1))
            except ValueError:
                delay = min(15, 2 ** (attempt - 1))
            time.sleep(delay)
            continue
        raise UploaderError(
            f"GitHub API hatasi {response.status_code} ({method} {path}): {_response_message(response)}"
        )
    raise UploaderError(f"GitHub API hatasi: {last_error or 'bilinmeyen hata'}")


def github_repo(repo_name: str) -> Optional[dict]:
    try:
        response = api_request(
            "GET",
            f"/repos/{GITHUB_USERNAME}/{repo_name}",
            expected=(200, 404),
        )
    except UploaderError:
        raise
    if response.status_code == 404:
        return None
    return response.json()


def list_owned_repos() -> List[dict]:
    repos: List[dict] = []
    page = 1
    while True:
        response = api_request(
            "GET",
            f"/user/repos?per_page=100&page={page}&affiliation=owner&sort=full_name",
        )
        batch = response.json()
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def gallery_repo_names(repo_objects: Optional[Iterable[dict]] = None) -> List[str]:
    if repo_objects is None:
        repo_objects = list_owned_repos()
    pattern = re.compile(rf"^{re.escape(REPO_PREFIX)}(\d+)$")
    names = [r["name"] for r in repo_objects if pattern.match(r.get("name", ""))]
    return sorted(names, key=lambda n: (int(pattern.match(n).group(1)), n))


def next_repo_name(reserved: Set[str]) -> str:
    pattern = re.compile(rf"^{re.escape(REPO_PREFIX)}(\d+)$")
    max_number = 0
    width = REPO_DIGITS
    for name in reserved:
        match = pattern.match(name)
        if match:
            digits = match.group(1)
            max_number = max(max_number, int(digits))
            width = max(width, len(digits))
    return f"{REPO_PREFIX}{max_number + 1:0{width}d}"


# -----------------------------------------------------------------------------
# Generic filesystem / git helpers
# -----------------------------------------------------------------------------

def run_command(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    capture: bool = True,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess:
    kwargs: Dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "env": env,
        "text": True,
        "check": False,
    }
    if capture:
        kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
    result = subprocess.run(list(args), **kwargs)
    if result.returncode != 0 and not allow_failure:
        stdout = (result.stdout or "").strip() if capture else ""
        stderr = (result.stderr or "").strip() if capture else ""
        detail = "\n".join(part for part in (stdout, stderr) if part)
        raise UploaderError(
            f"Komut basarisiz ({result.returncode}): {' '.join(args)}" + (f"\n{detail}" if detail else "")
        )
    return result


def git(args: Sequence[str], repo_path: Path, *, capture: bool = True, env: Optional[dict] = None, allow_failure: bool = False) -> subprocess.CompletedProcess:
    return run_command(["git", *args], cwd=repo_path, capture=capture, env=env, allow_failure=allow_failure)


def check_git() -> None:
    try:
        result = run_command(["git", "--version"])
    except FileNotFoundError as exc:
        raise UploaderError("Git bulunamadi. Git for Windows kurulu olmali.") from exc
    say((result.stdout or "git hazir").strip())


def safe_rmtree(path: Path) -> None:
    if not path.exists():
        return

    def onerror(func: Callable[..., Any], p: str, exc_info: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=onerror)


def folder_size(path: Path, *, exclude_git: bool = False) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path):
        if exclude_git and ".git" in dirs:
            dirs.remove(".git")
        for name in files:
            p = Path(root) / name
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{value} B"


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Optional[dict] = None) -> dict:
    if not path.exists():
        return dict(default or {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default or {})
    except Exception:
        LOGGER.exception("JSON okunamadi: %s", path)
        return dict(default or {})


def unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    stamp = time.strftime("%Y%m%d-%H%M%S")
    counter = 1
    while True:
        candidate = directory / f"{stem}_{stamp}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def open_local_path(path: Path) -> None:
    if not path.exists():
        raise UploaderError(f"Klasor bulunamadi: {path}")
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def make_askpass() -> Tuple[Path, dict]:
    temp_dir = Path(tempfile.mkdtemp(prefix="wsi_git_auth_"))
    env = os.environ.copy()
    env["GITHUB_USERNAME"] = GITHUB_USERNAME
    env["GITHUB_TOKEN"] = GITHUB_TOKEN
    env["GIT_TERMINAL_PROMPT"] = "0"
    if os.name == "nt":
        helper = temp_dir / "askpass.cmd"
        helper.write_text(
            "@echo off\n"
            "echo %~1 | findstr /I \"Username\" >nul\n"
            "if %errorlevel%==0 (\n"
            "  echo %GITHUB_USERNAME%\n"
            ") else (\n"
            "  echo %GITHUB_TOKEN%\n"
            ")\n",
            encoding="utf-8",
        )
    else:
        helper = temp_dir / "askpass.sh"
        helper.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*|*username*) printf '%s\\n' \"$GITHUB_USERNAME\" ;;\n"
            "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    env["GIT_ASKPASS"] = str(helper)
    return temp_dir, env


def write_git_exclude(repo_path: Path) -> None:
    info = repo_path / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    additions = [MARKER_NAME, ".deepzoom_tmp/", ".deepzoom_tmp.dzi", ".deepzoom_tmp_files/"]
    lines = set(current.splitlines())
    with exclude.open("a", encoding="utf-8") as handle:
        for item in additions:
            if item not in lines:
                if current and not current.endswith("\n"):
                    handle.write("\n")
                    current += "\n"
                handle.write(item + "\n")
                current += item + "\n"


def remote_branch_sha(repo_name: str, branch: str) -> Optional[str]:
    response = api_request(
        "GET",
        f"/repos/{GITHUB_USERNAME}/{repo_name}/commits/{branch}",
        expected=(200, 404, 409),
    )
    if response.status_code != 200:
        return None
    return response.json().get("sha")


def authenticated_git_push(repo_path: Path, branch: str, repo_name: str) -> None:
    auth_dir, env = make_askpass()
    try:
        local_sha = git(["rev-parse", "HEAD"], repo_path).stdout.strip()
        last_error = ""
        for attempt in range(1, 4):
            say(
                f"GitHub'a push ediliyor ({attempt}/3)",
                repo=repo_name,
                stage="push",
                progress=58,
            )
            result = git(
                ["push", "-u", "origin", branch],
                repo_path,
                capture=True,
                env=env,
                allow_failure=True,
            )
            if result.returncode == 0:
                return
            last_error = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
            try:
                if remote_branch_sha(repo_name, branch) == local_sha:
                    say("Push cevabi kesildi ama commit GitHub'da dogrulandi.", repo=repo_name)
                    return
            except Exception:
                pass
            if attempt < 3:
                time.sleep(3 * attempt)
        raise UploaderError(f"Git push basarisiz ({repo_name}): {last_error[-1200:]}")
    finally:
        shutil.rmtree(auth_dir, ignore_errors=True)


# -----------------------------------------------------------------------------
# Slide metadata / preparation
# -----------------------------------------------------------------------------

def metadata_path_for(svs_path: Path) -> Path:
    return Path(str(svs_path) + META_SUFFIX)


def sidecars_for(svs_path: Path) -> Tuple[Optional[Path], Optional[Path]]:
    txt = svs_path.with_suffix(".txt")
    description_path = txt if txt.exists() else None
    thumb = None
    for suffix in (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"):
        candidate = svs_path.with_suffix(suffix)
        if candidate.exists():
            thumb = candidate
            break
    return description_path, thumb


def read_text_flexible(path: Optional[Path]) -> str:
    if not path:
        return ""
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1254", errors="replace").strip()


def parse_explicit_repo(stem: str) -> Tuple[Optional[str], str]:
    match = re.match(rf"^({re.escape(REPO_PREFIX)}\d+)__(.+)$", stem, flags=re.IGNORECASE)
    if not match:
        return None, stem.replace("_", " ").strip()
    return match.group(1), match.group(2).replace("_", " ").strip()


@dataclass
class SlideJob:
    svs_path: Path
    repo_name: str
    slide_title: str
    description: str
    thumbnail_source: Optional[Path]
    description_path: Optional[Path] = None
    explicit_repo: bool = False
    branch: str = "main"
    prepared: bool = False
    state: Dict[str, Any] = field(default_factory=dict)

    @property
    def repo_path(self) -> Path:
        return LOCAL_REPO_BASE / self.repo_name

    @property
    def marker_path(self) -> Path:
        return self.repo_path / MARKER_NAME

    @property
    def meta_path(self) -> Path:
        return metadata_path_for(self.svs_path)

    @property
    def web_url(self) -> str:
        return f"https://{GITHUB_USERNAME}.github.io/{self.repo_name}/"

    def reload_state(self) -> None:
        self.state = load_json(self.meta_path, self.state)
        self.prepared = bool(self.state.get("prepared", self.prepared))

    def save_state(self, **updates: Any) -> None:
        self.state.update(updates)
        self.state.update(
            {
                "version": 2,
                "source_name": self.svs_path.name,
                "repo_name": self.repo_name,
                "title": self.slide_title,
                "description": self.description,
                "thumbnail_source": str(self.thumbnail_source) if self.thumbnail_source else "",
                "branch": self.branch,
                "explicit_repo": self.explicit_repo,
                "prepared": self.prepared,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        atomic_write_json(self.meta_path, self.state)


def find_pending_repo_for_source(source_name: str) -> Optional[dict]:
    for marker in LOCAL_REPO_BASE.glob(f"{REPO_PREFIX}*/{MARKER_NAME}"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("source_name") == source_name:
            data["repo_name"] = marker.parent.name
            return data
    return None


def write_marker(job: SlideJob) -> None:
    job.repo_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_name": job.svs_path.name,
        "repo_name": job.repo_name,
        "slide_title": job.slide_title,
        "description": job.description,
        "branch": job.branch,
        "created_at": job.state.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    job.marker_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_jobs(svs_files: List[Path], remote_names: Set[str]) -> List[SlideJob]:
    reserved = set(remote_names)
    for meta in INBOX_DIR.glob(f"*.svs{META_SUFFIX}"):
        data = load_json(meta)
        if data.get("repo_name"):
            reserved.add(str(data["repo_name"]))
    for marker in LOCAL_REPO_BASE.glob(f"{REPO_PREFIX}*/{MARKER_NAME}"):
        reserved.add(marker.parent.name)

    jobs: List[SlideJob] = []
    for svs_path in svs_files:
        desc_path, side_thumb = sidecars_for(svs_path)
        explicit_repo, default_title = parse_explicit_repo(svs_path.stem)
        meta = load_json(metadata_path_for(svs_path))
        pending = find_pending_repo_for_source(svs_path.name)

        repo_name = str(meta.get("repo_name") or "").strip()
        if not repo_name and pending:
            repo_name = str(pending.get("repo_name") or "")
        if not repo_name and explicit_repo:
            repo_name = explicit_repo
        if not repo_name:
            repo_name = next_repo_name(reserved)
        reserved.add(repo_name)

        title = str(meta.get("title") or meta.get("slide_title") or default_title or svs_path.stem).strip()
        description = str(meta.get("description") or "").strip()
        if not description:
            description = read_text_flexible(desc_path)
        thumb_raw = str(meta.get("thumbnail_source") or "").strip()
        thumb = Path(thumb_raw) if thumb_raw else side_thumb
        if thumb and not thumb.exists():
            thumb = side_thumb if side_thumb and side_thumb.exists() else None

        prepared = bool(meta.get("prepared", False))
        branch = str(meta.get("branch") or (pending or {}).get("branch") or "main")
        job = SlideJob(
            svs_path=svs_path,
            repo_name=repo_name,
            slide_title=title,
            description=description,
            thumbnail_source=thumb,
            description_path=desc_path,
            explicit_repo=bool(explicit_repo or meta.get("explicit_repo")),
            branch=branch,
            prepared=prepared,
            state=meta,
        )
        if "created_at" not in job.state:
            job.state["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        job.save_state(stage=job.state.get("stage", "preparation"), last_error=job.state.get("last_error", ""))
        jobs.append(job)
    return jobs


def save_job_preparation(job: SlideJob, title: str, description: str, thumbnail: Optional[Path]) -> None:
    title = title.strip()
    if not title:
        raise UploaderError("Baslik bos olamaz.")
    if thumbnail and not thumbnail.exists():
        raise UploaderError(f"Thumbnail dosyasi bulunamadi: {thumbnail}")
    job.slide_title = title
    job.description = description.strip()
    job.thumbnail_source = thumbnail
    job.prepared = True
    job.save_state(stage="prepared", last_error="")
    say(f"Hazirlik kaydedildi: {job.svs_path.name}", repo=job.repo_name, stage="prepared", progress=8)


# -----------------------------------------------------------------------------
# DeepZoom / thumbnail / slide repository
# -----------------------------------------------------------------------------

VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/openseadragon.min.js"></script>
    <style>
        html, body, #openseadragon {{ width: 100%; height: 100%; margin: 0; background: #111; }}
    </style>
</head>
<body>
    <div id="openseadragon"></div>
    <script>
        OpenSeadragon({{
            id: "openseadragon",
            prefixUrl: "https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/images/",
            tileSources: "slide.dzi",
            showNavigator: false,
            maxZoomPixelRatio: 2
        }});
    </script>
</body>
</html>
"""


def import_pyvips():
    try:
        import pyvips  # type: ignore
    except Exception as exc:
        raise UploaderError(
            "pyvips yuklenemedi. Python paketi ve libvips kurulu olmali. " + str(exc)
        ) from exc
    return pyvips


def prepare_local_repo(job: SlideJob, remote_info: Optional[dict]) -> str:
    repo_path = job.repo_path
    pending_marker = job.marker_path.exists()
    remote_has_commit = False
    if remote_info:
        job.branch = remote_info.get("default_branch") or job.branch or "main"
        try:
            remote_has_commit = remote_branch_sha(job.repo_name, job.branch) is not None
        except Exception:
            remote_has_commit = bool(remote_info.get("size"))

    if (repo_path / ".git").exists():
        if remote_info and remote_has_commit and not pending_marker and job.explicit_repo:
            git(["fetch", "origin", job.branch], repo_path)
            git(["checkout", "-B", job.branch, f"origin/{job.branch}"], repo_path)
    else:
        if repo_path.exists() and any(repo_path.iterdir()) and not pending_marker:
            safe_rmtree(repo_path)
        repo_path.mkdir(parents=True, exist_ok=True)
        if remote_info and remote_has_commit and job.explicit_repo and not pending_marker:
            safe_rmtree(repo_path)
            run_command(
                [
                    "git", "clone", "--branch", job.branch, "--single-branch",
                    f"https://github.com/{GITHUB_USERNAME}/{job.repo_name}.git", str(repo_path),
                ],
                cwd=LOCAL_REPO_BASE,
            )
        else:
            run_command(["git", "init"], cwd=repo_path)
            git(["branch", "-M", job.branch], repo_path)
            git(["remote", "add", "origin", f"https://github.com/{GITHUB_USERNAME}/{job.repo_name}.git"], repo_path)

    remotes = git(["remote"], repo_path).stdout.split()
    if "origin" not in remotes:
        git(["remote", "add", "origin", f"https://github.com/{GITHUB_USERNAME}/{job.repo_name}.git"], repo_path)
    write_git_exclude(repo_path)
    write_marker(job)
    git(["config", "user.name", GITHUB_USERNAME], repo_path)
    git(["config", "user.email", f"{GITHUB_USERNAME}@users.noreply.github.com"], repo_path)
    job.save_state(branch=job.branch, stage="local_repo")
    return job.branch


def clear_slide_payload(repo_path: Path) -> None:
    for name in ("slide.dzi", "slide_files", "thumbnail.jpg", "thumbnail.jpeg", "thumbnail.png"):
        path = repo_path / name
        if path.is_dir():
            safe_rmtree(path)
        elif path.exists():
            path.unlink()


def deepzoom_complete(repo_path: Path) -> bool:
    dzi = repo_path / "slide.dzi"
    tiles = repo_path / "slide_files"
    if not dzi.exists() or not tiles.is_dir():
        return False
    try:
        return any(tiles.rglob("*.jpeg")) or any(tiles.rglob("*.jpg")) or any(tiles.rglob("*.png"))
    except OSError:
        return False


def generate_deepzoom_atomic(job: SlideJob) -> None:
    if deepzoom_complete(job.repo_path):
        say("DeepZoom zaten hazir; yeniden uretilmiyor.", repo=job.repo_name, stage="deepzoom", progress=30)
        return
    pyvips = import_pyvips()
    temp_root = LOCAL_REPO_BASE / f".{job.repo_name}.deepzoom_tmp"
    safe_rmtree(temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    say("DeepZoom uretiliyor...", repo=job.repo_name, stage="deepzoom", progress=20)
    try:
        image = pyvips.Image.new_from_file(str(job.svs_path), access="sequential")
        image.dzsave(str(temp_root / "slide"))
        del image
        temp_dzi = temp_root / "slide.dzi"
        temp_tiles = temp_root / "slide_files"
        if not temp_dzi.exists() or not temp_tiles.exists() or not any(temp_tiles.iterdir()):
            raise UploaderError("DeepZoom ciktilari eksik olustu.")
        clear_slide_payload(job.repo_path)
        shutil.move(str(temp_dzi), str(job.repo_path / "slide.dzi"))
        shutil.move(str(temp_tiles), str(job.repo_path / "slide_files"))
        job.save_state(stage="deepzoom_ready", last_error="")
        say("DeepZoom tamamlandi.", repo=job.repo_name, stage="deepzoom", progress=34)
    except Exception as exc:
        job.save_state(stage="deepzoom_error", last_error=str(exc))
        raise UploaderError(f"SVS -> DeepZoom donusumu basarisiz: {exc}") from exc
    finally:
        safe_rmtree(temp_root)


def _save_small_jpeg(image: Any, destination: Path) -> None:
    if getattr(image, "hasalpha", lambda: False)():
        image = image.flatten(background=[255, 255, 255])
    max_dim = max(int(image.width), int(image.height))
    if max_dim > THUMB_MAX_PX:
        image = image.resize(THUMB_MAX_PX / float(max_dim))

    qualities = (84, 76, 68, 60)
    for quality in qualities:
        image.jpegsave(str(destination), Q=quality, strip=True, optimize_coding=True)
        if destination.stat().st_size <= THUMB_TARGET_BYTES:
            return
    current = image
    while destination.stat().st_size > THUMB_TARGET_BYTES and max(current.width, current.height) > 420:
        current = current.resize(0.82)
        current.jpegsave(str(destination), Q=68, strip=True, optimize_coding=True)


def prepare_thumbnail(job: SlideJob) -> None:
    destination = job.repo_path / "thumbnail.jpg"
    pyvips = import_pyvips()
    source = job.thumbnail_source
    image = None
    try:
        if source:
            say("Secilen thumbnail kucultuluyor...", repo=job.repo_name, stage="thumbnail", progress=38)
            image = pyvips.Image.thumbnail(str(source), THUMB_MAX_PX)
        else:
            say("Thumbnail SVS'den otomatik uretiliyor...", repo=job.repo_name, stage="thumbnail", progress=38)
            image = pyvips.Image.thumbnail(str(job.svs_path), THUMB_MAX_PX)
        _save_small_jpeg(image, destination)
        say(
            f"Thumbnail hazir: {human_bytes(destination.stat().st_size)}",
            repo=job.repo_name,
            stage="thumbnail",
            progress=41,
        )
    except Exception as exc:
        if destination.exists():
            try:
                destination.unlink()
            except OSError:
                pass
        warn(f"Thumbnail uretilemedi; slayt yuklemesi devam edecek: {exc}", repo=job.repo_name)
    finally:
        image = None
        release_vips_file_handles()


def write_slide_files(job: SlideJob) -> None:
    (job.repo_path / "index.html").write_text(
        VIEWER_HTML.format(title=html.escape(job.slide_title)), encoding="utf-8"
    )
    readme = f"# {job.slide_title}\n\n"
    if job.description:
        readme += job.description.strip() + "\n\n"
    if (job.repo_path / "thumbnail.jpg").exists():
        readme += "![Thumbnail](thumbnail.jpg)\n\n"
    readme += f"View the slide at [{job.web_url}]({job.web_url})\n"
    (job.repo_path / "README.md").write_text(readme, encoding="utf-8")


def ensure_repo_size_safe(job: SlideJob) -> int:
    size = folder_size(job.repo_path, exclude_git=True)
    job.save_state(site_bytes=size)
    if size > PAGES_SAFE_LIMIT_BYTES:
        raise UploaderError(
            f"Yayim dosyalari {human_bytes(size)}. Guvenli Pages siniri {human_bytes(PAGES_SAFE_LIMIT_BYTES)} olarak ayarli; yukleme durduruldu."
        )
    return size


def commit_if_needed(job: SlideJob) -> bool:
    git(["add", "-A"], job.repo_path)
    status = git(["status", "--porcelain"], job.repo_path).stdout.strip()
    if not status:
        return False
    git(["commit", "-m", f"Slide added/updated: {job.slide_title}"], job.repo_path)
    return True


# -----------------------------------------------------------------------------
# GitHub repo / Pages / live verification
# -----------------------------------------------------------------------------

def create_remote_repo(job: SlideJob) -> dict:
    say("GitHub reposu olusturuluyor...", repo=job.repo_name, stage="repo", progress=48)
    try:
        response = api_request(
            "POST",
            "/user/repos",
            expected=(201,),
            json={
                "name": job.repo_name,
                "description": f"Virtual microscopy for {job.slide_title}",
                "private": False,
                "has_issues": False,
                "has_projects": False,
                "has_wiki": False,
                "auto_init": False,
            },
        )
        return response.json()
    except UploaderError:
        existing = github_repo(job.repo_name)
        if existing:
            say("Repo olusturma cevabi belirsizdi; repo GitHub'da bulundu.", repo=job.repo_name)
            return existing
        raise


def ensure_pages(repo_name: str, branch: str) -> None:
    path = f"/repos/{GITHUB_USERNAME}/{repo_name}/pages"
    current = api_request("GET", path, expected=(200, 404))
    payload = {"source": {"branch": branch, "path": "/"}}
    if current.status_code == 200:
        api_request("PUT", path, expected=(200, 204), json=payload)
        return
    response = api_request("POST", path, expected=(201, 409, 422), json=payload)
    if response.status_code == 422:
        # Often means Pages is already being configured. A later verification decides success.
        warn(f"Pages ayari 422 dondurdu; canli sayfa kontrolu ile devam edilecek: {_response_message(response)}", repo=repo_name)


def latest_pages_build(repo_name: str) -> Tuple[Optional[str], str]:
    response = api_request(
        "GET",
        f"/repos/{GITHUB_USERNAME}/{repo_name}/pages/builds/latest",
        expected=(200, 404),
        retries=2,
    )
    if response.status_code != 200:
        return None, ""
    data = response.json()
    error = data.get("error") or {}
    return data.get("status"), str(error.get("message") or "")


def public_get(url: str, *, timeout: int = 20) -> requests.Response:
    headers = {"Cache-Control": "no-cache", "User-Agent": "whole-slide-uploader-live-check"}
    return requests.get(url, headers=headers, timeout=timeout)


def wait_for_pages_live(job: SlideJob, timeout: Optional[int] = None) -> None:
    timeout = timeout or PAGES_VERIFY_TIMEOUT
    deadline = time.monotonic() + timeout
    last = ""
    say("GitHub Pages canli yayin bekleniyor...", repo=job.repo_name, stage="pages", progress=76)
    while time.monotonic() < deadline:
        try:
            status, build_error = latest_pages_build(job.repo_name)
            if status in {"errored", "error"}:
                raise UploaderError(f"GitHub Pages build hatasi: {build_error or status}")
            stamp = int(time.time())
            page = public_get(job.web_url + f"?v={stamp}", timeout=15)
            dzi = public_get(job.web_url + f"slide.dzi?v={stamp}", timeout=15)
            if page.status_code == 200 and dzi.status_code == 200 and "<Image" in dzi.text[:1200]:
                job.save_state(stage="pages_live", pages_verified=True, last_error="")
                say("Web sayfasi ve slide.dzi canli olarak dogrulandi.", repo=job.repo_name, stage="pages", progress=86)
                return
            last = f"page={page.status_code}, dzi={dzi.status_code}, build={status or 'unknown'}"
        except UploaderError:
            raise
        except Exception as exc:
            last = str(exc)
        time.sleep(7)
    job.save_state(stage="pages_wait", pages_verified=False, last_error=f"Canli yayin henuz dogrulanamadi: {last}")
    raise UploaderError(f"Pages {timeout} saniye icinde dogrulanamadi ({last}). Sonraki calistirmada buradan devam eder.")


def process_slide_upload(job: SlideJob) -> SlideJob:
    job.reload_state()
    if not job.prepared:
        raise UploaderError("Slayt hazirligi tamamlanmamis.")

    say(f"Isleniyor: {job.svs_path.name}", repo=job.repo_name, stage="start", progress=10)
    remote_info = github_repo(job.repo_name)

    if job.state.get("pages_verified") and remote_info:
        say("Slayt daha once web'de dogrulanmis; tekrar yuklenmiyor.", repo=job.repo_name, stage="pages", progress=86)
        return job

    if job.state.get("pushed") and remote_info:
        ensure_pages(job.repo_name, job.branch)
        wait_for_pages_live(job)
        return job

    branch = prepare_local_repo(job, remote_info)
    generate_deepzoom_atomic(job)
    prepare_thumbnail(job)
    write_slide_files(job)
    size = ensure_repo_size_safe(job)
    say(f"Repo yayin icerigi: {human_bytes(size)}", repo=job.repo_name, stage="repo", progress=44)
    commit_if_needed(job)

    if remote_info is None:
        remote_info = create_remote_repo(job)
    authenticated_git_push(job.repo_path, branch, job.repo_name)
    job.save_state(stage="pushed", pushed=True, last_error="")
    say("GitHub push tamamlandi.", repo=job.repo_name, stage="push", progress=66)

    ensure_pages(job.repo_name, branch)
    job.save_state(stage="pages_configured")
    wait_for_pages_live(job)
    return job

# -----------------------------------------------------------------------------
# Gallery synchronization
# -----------------------------------------------------------------------------

def get_repo_text_file(repo_name: str, file_path: str) -> Tuple[Optional[str], Optional[str]]:
    response = api_request(
        "GET",
        f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{file_path}",
        expected=(200, 404),
    )
    if response.status_code == 404:
        return None, None
    data = response.json()
    content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
    return content, data.get("sha")


def put_repo_text_file(repo_name: str, file_path: str, content: str, message: str) -> None:
    old_content, sha = get_repo_text_file(repo_name, file_path)
    if old_content == content:
        return
    payload: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    try:
        api_request(
            "PUT",
            f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{file_path}",
            expected=(200, 201),
            json=payload,
        )
    except UploaderError:
        # If the response was lost, verify whether the desired bytes are already there.
        current, _ = get_repo_text_file(repo_name, file_path)
        if current == content:
            return
        raise


def read_slide_metadata(repo_name: str) -> Tuple[str, str, bool]:
    title = repo_name
    description = "Whole slide image"
    has_thumbnail = False
    readme, _ = get_repo_text_file(repo_name, "README.md")
    if readme:
        lines = [line.strip() for line in readme.splitlines()]
        for line in lines:
            if line.startswith("# "):
                title = line[2:].strip() or repo_name
                break
        paragraphs: List[str] = []
        current: List[str] = []
        for line in lines:
            if not line:
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
                continue
            if line.startswith("#") or line.startswith("![") or line.lower().startswith("view the slide"):
                continue
            current.append(line)
        if current:
            paragraphs.append(" ".join(current))
        if paragraphs:
            description = paragraphs[0]
        if "thumbnail.jpg" in readme:
            has_thumbnail = True
    if not has_thumbnail:
        response = api_request(
            "GET",
            f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/thumbnail.jpg",
            expected=(200, 404),
            retries=2,
        )
        has_thumbnail = response.status_code == 200
    return title, description, has_thumbnail


def make_gallery_entry(repo_name: str) -> str:
    title, description, has_thumbnail = read_slide_metadata(repo_name)
    pages_link = f"https://{GITHUB_USERNAME}.github.io/{repo_name}/"
    thumbnail = ""
    if has_thumbnail:
        thumbnail = (
            f'<img src="{pages_link}thumbnail.jpg" alt="Thumbnail" '
            'class="w-full h-64 object-contain rounded-lg mb-4">'
        )
    return (
        '<li class="gallery-item bg-white p-6 rounded-xl shadow-lg">'
        f'<a href="{html.escape(pages_link, quote=True)}" class="block"><div>'
        f'{thumbnail}<h2 class="text-xl font-semibold text-blue-600 hover:underline">'
        f'{html.escape(title)}</h2>'
        f'<p class="text-gray-600 mt-2 text-sm">{html.escape(description)}</p>'
        '</div></a></li>'
    )


def repo_name_from_entry(entry: str) -> Optional[str]:
    match = re.search(
        rf"https://{re.escape(GITHUB_USERNAME)}\.github\.io/({re.escape(REPO_PREFIX)}\d+)/",
        entry,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def entry_to_markdown(entry: str) -> str:
    repo = repo_name_from_entry(entry)
    if not repo:
        return ""
    link = f"https://{GITHUB_USERNAME}.github.io/{repo}/"
    title_match = re.search(r"<h2\b[^>]*>(.*?)</h2>", entry, flags=re.DOTALL | re.IGNORECASE)
    desc_match = re.search(r"<p\b[^>]*>(.*?)</p>", entry, flags=re.DOTALL | re.IGNORECASE)
    title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else repo
    desc = re.sub(r"<[^>]+>", "", desc_match.group(1)).strip() if desc_match else "Whole slide image"
    return f"- [**{html.unescape(title)}**]({link}) - {html.unescape(desc)}"


def gallery_shell(entries: List[str], gallery_title: str, gallery_description: str) -> str:
    joined = "\n        ".join(entries)
    title_escaped = html.escape(gallery_title)
    desc_escaped = html.escape(gallery_description)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="{html.escape(gallery_description, quote=True)}">
    <title>{title_escaped}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .gallery-item {{ transition: transform 0.3s ease, box-shadow 0.3s ease; }}
        .gallery-item:hover {{ transform: scale(1.03); box-shadow: 0 12px 20px -4px rgba(0, 0, 0, 0.15); }}
        .gallery-item img {{ transition: transform 0.3s ease; }}
        .gallery-item:hover img {{ transform: scale(1.08); }}
        body {{ background: linear-gradient(to bottom, #f3f4f6, #e5e7eb); }}
    </style>
</head>
<body>
    <div class="container mx-auto px-4 sm:px-6 lg:px-8 py-12">
        <h1 class="text-4xl sm:text-5xl font-extrabold text-center text-gray-900 mb-4">{title_escaped}</h1>
        <p id="gallery-description" class="text-center text-gray-600 max-w-3xl mx-auto mb-12">{desc_escaped}</p>
        <ul id="sortable" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-10">
        {joined}
        </ul>
        <footer class="mt-12 text-center text-gray-500 text-sm">
            Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}
        </footer>
    </div>
</body>
</html>
"""


def replace_gallery_list(existing_html: str, entries: List[str]) -> str:
    if not existing_html:
        return ""
    pattern = re.compile(
        r'(<ul\b[^>]*id=["\']sortable["\'][^>]*>)(.*?)(</ul>)',
        flags=re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(existing_html)
    if not match:
        return ""
    joined = "\n        " + "\n        ".join(entries) + "\n        "
    updated = existing_html[: match.start(2)] + joined + existing_html[match.end(2) :]
    updated = re.sub(
        r"Last updated:\s*[^<\r\n]+",
        f"Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        updated,
        count=1,
        flags=re.IGNORECASE,
    )
    return updated


def apply_gallery_header(existing_html: str, title: str, description: str) -> str:
    title_e = html.escape(title)
    desc_e = html.escape(description)
    desc_attr = html.escape(description, quote=True)
    result = existing_html
    if re.search(r"<title>.*?</title>", result, flags=re.DOTALL | re.IGNORECASE):
        result = re.sub(r"<title>.*?</title>", f"<title>{title_e}</title>", result, count=1, flags=re.DOTALL | re.IGNORECASE)
    else:
        result = result.replace("</head>", f"    <title>{title_e}</title>\n</head>", 1)

    meta_pattern = r'<meta\s+name=["\']description["\'][^>]*>'
    meta_tag = f'<meta name="description" content="{desc_attr}">'
    if re.search(meta_pattern, result, flags=re.IGNORECASE):
        result = re.sub(meta_pattern, meta_tag, result, count=1, flags=re.IGNORECASE)
    else:
        result = result.replace("</head>", f"    {meta_tag}\n</head>", 1)

    h1_pattern = r"(<h1\b[^>]*>)(.*?)(</h1>)"
    if re.search(h1_pattern, result, flags=re.DOTALL | re.IGNORECASE):
        result = re.sub(
            h1_pattern,
            lambda m: m.group(1) + title_e + m.group(3),
            result,
            count=1,
            flags=re.DOTALL | re.IGNORECASE,
        )

    desc_pattern = r'(<p\b[^>]*id=["\']gallery-description["\'][^>]*>)(.*?)(</p>)'
    if re.search(desc_pattern, result, flags=re.DOTALL | re.IGNORECASE):
        result = re.sub(
            desc_pattern,
            lambda m: m.group(1) + desc_e + m.group(3),
            result,
            count=1,
            flags=re.DOTALL | re.IGNORECASE,
        )
    else:
        h1_match = re.search(r"</h1>", result, flags=re.IGNORECASE)
        if h1_match:
            insertion = f'\n        <p id="gallery-description" class="text-center text-gray-600 max-w-3xl mx-auto mb-12">{desc_e}</p>'
            result = result[: h1_match.end()] + insertion + result[h1_match.end() :]
    return result


def parse_gallery_settings(index_html: str, readme: str) -> Tuple[str, str]:
    title = "Slide Gallery"
    description = "Interactive whole-slide microscopy gallery."
    if index_html:
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", index_html, flags=re.DOTALL | re.IGNORECASE)
        if h1:
            title = html.unescape(re.sub(r"<[^>]+>", "", h1.group(1))).strip() or title
        meta = re.search(
            r'<meta\s+name=["\']description["\'][^>]*content=["\'](.*?)["\'][^>]*>',
            index_html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        pdesc = re.search(
            r'<p\b[^>]*id=["\']gallery-description["\'][^>]*>(.*?)</p>',
            index_html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if pdesc:
            description = html.unescape(re.sub(r"<[^>]+>", "", pdesc.group(1))).strip() or description
        elif meta:
            description = html.unescape(meta.group(1)).strip() or description
    if readme:
        heading = re.search(r"^#\s+(.+)$", readme, flags=re.MULTILINE)
        if heading and title == "Slide Gallery":
            title = heading.group(1).strip()
        paras = [p.strip() for p in re.split(r"\n\s*\n", readme) if p.strip()]
        for p in paras:
            if p.startswith("#") or p.lower().startswith("live gallery:") or p.startswith("##"):
                continue
            if "github.io" in p and len(p.split()) < 12:
                continue
            if description == "Interactive whole-slide microscopy gallery.":
                description = p
                break
    return title, description


def load_remote_gallery_settings() -> Tuple[str, str]:
    index_html, _ = get_repo_text_file(GALLERY_REPO_NAME, "index.html")
    readme, _ = get_repo_text_file(GALLERY_REPO_NAME, "README.md")
    return parse_gallery_settings(index_html or "", readme or "")


def ensure_gallery_repo_exists() -> dict:
    info = github_repo(GALLERY_REPO_NAME)
    if info is None:
        raise UploaderError(f"Ana galeri reposu bulunamadi: {GITHUB_USERNAME}/{GALLERY_REPO_NAME}")
    return info


def sync_gallery(
    refresh_repos: Optional[Set[str]] = None,
    *,
    gallery_title: Optional[str] = None,
    gallery_description: Optional[str] = None,
) -> None:
    discover_missing = refresh_repos is None
    refresh_repos = refresh_repos or set()
    info = ensure_gallery_repo_exists()
    say("Ana galeri senkronize ediliyor...", stage="gallery", progress=90)
    remote_names = gallery_repo_names()
    existing_html, _ = get_repo_text_file(GALLERY_REPO_NAME, "index.html")
    existing_html = existing_html or ""
    existing_readme, _ = get_repo_text_file(GALLERY_REPO_NAME, "README.md")
    existing_readme = existing_readme or ""
    current_title, current_desc = parse_gallery_settings(existing_html, existing_readme)
    title = (gallery_title or current_title).strip() or "Slide Gallery"
    desc = (gallery_description if gallery_description is not None else current_desc).strip()

    existing_entries = re.findall(r"<li\b[^>]*>.*?</li>", existing_html, flags=re.DOTALL | re.IGNORECASE)
    entry_map: Dict[str, str] = {}
    order: List[str] = []
    for entry in existing_entries:
        repo = repo_name_from_entry(entry)
        if repo and repo in remote_names and repo not in entry_map:
            entry_map[repo] = entry.strip()
            order.append(repo)

    for repo in remote_names:
        if repo not in entry_map:
            if discover_missing or repo in refresh_repos:
                entry_map[repo] = make_gallery_entry(repo)
                order.append(repo)
        elif repo in refresh_repos:
            entry_map[repo] = make_gallery_entry(repo)

    order = [repo for repo in order if repo in remote_names]
    entries = [entry_map[repo] for repo in order]
    updated_html = replace_gallery_list(existing_html, entries)
    if not updated_html:
        updated_html = gallery_shell(entries, title, desc)
    else:
        updated_html = apply_gallery_header(updated_html, title, desc)

    put_repo_text_file(GALLERY_REPO_NAME, "index.html", updated_html, "Update slide gallery")

    md_lines = [line for line in (entry_to_markdown(entry) for entry in entries) if line]
    readme = (
        f"# {title}\n\n"
        + (desc + "\n\n" if desc else "")
        + f"Live gallery: https://{GITHUB_USERNAME}.github.io/{GALLERY_REPO_NAME}/\n\n"
        + "## Slides Overview\n\n"
        + ("\n\n".join(md_lines) if md_lines else "No slides found.")
        + "\n\n---\n"
        + f"Updated automatically on {time.strftime('%Y-%m-%d %H:%M:%S')}.\n"
    )
    put_repo_text_file(GALLERY_REPO_NAME, "README.md", readme, "Update gallery README")
    branch = info.get("default_branch") or "main"
    ensure_pages(GALLERY_REPO_NAME, branch)
    say(f"Galeri GitHub'a yazildi ({len(entries)} slayt).", stage="gallery", progress=94)


def wait_for_gallery_live(
    repo_names: Set[str],
    *,
    expected_title: Optional[str] = None,
    timeout: Optional[int] = None,
) -> None:
    timeout = timeout or PAGES_VERIFY_TIMEOUT
    url = f"https://{GITHUB_USERNAME}.github.io/{GALLERY_REPO_NAME}/"
    deadline = time.monotonic() + timeout
    missing: Set[str] = set(repo_names)
    last = ""
    while time.monotonic() < deadline:
        try:
            response = public_get(url + f"?v={int(time.time())}", timeout=15)
            if response.status_code == 200:
                body = response.text
                missing = {
                    repo for repo in repo_names
                    if f"https://{GITHUB_USERNAME}.github.io/{repo}/" not in body
                }
                title_ok = not expected_title or html.escape(expected_title) in body or expected_title in body
                if not missing and title_ok:
                    say("Ana galeri canli olarak dogrulandi.", stage="gallery", progress=98)
                    return
                last = f"HTTP 200; eksik={','.join(sorted(missing)) or '-'}"
            else:
                last = f"HTTP {response.status_code}"
        except Exception as exc:
            last = str(exc)
        time.sleep(7)
    raise UploaderError(f"Ana galeri {timeout} saniye icinde dogrulanamadi ({last}). Sonraki calistirmada tekrar denenir.")


# -----------------------------------------------------------------------------
# Archive / local cleanup / recovery safety
# -----------------------------------------------------------------------------

def release_vips_file_handles() -> None:
    """Best-effort release of libvips cached file handles before moving SVS files on Windows."""
    try:
        pyvips = import_pyvips()
        for name, value in (("cache_set_max", 0), ("cache_set_max_mem", 0), ("cache_set_max_files", 0)):
            func = getattr(pyvips, name, None)
            if callable(func):
                try:
                    func(value)
                except Exception:
                    pass
    except Exception:
        pass
    gc.collect()


def _move_if_in_inbox(path: Optional[Path]) -> Optional[Path]:
    if not path or not path.exists():
        return None
    try:
        path.resolve().relative_to(INBOX_DIR.resolve())
    except ValueError:
        return None
    destination = unique_destination(DONE_DIR, path.name)

    # On Windows libvips/antivirus can keep an SVS handle open briefly after
    # DeepZoom/thumbnail generation. Never classify that as an upload failure
    # immediately: release caches and retry the rename first.
    last_error: Optional[BaseException] = None
    for attempt in range(1, 9):
        try:
            release_vips_file_handles()
            shutil.move(str(path), str(destination))
            return destination
        except (PermissionError, OSError) as exc:
            last_error = exc
            if attempt < 8:
                time.sleep(min(0.35 * attempt, 2.0))
                continue
            break
    raise UploaderError(f"Dosya arsive tasinamadi: {path.name} ({last_error})")


def archive_completed_job(job: SlideJob) -> None:
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    job.save_state(stage="archiving", archived=False)
    meta_path = job.meta_path
    # Move source/sidecars first and metadata last. If power fails, metadata remains
    # available to recover the operation until the critical source move is done.
    _move_if_in_inbox(job.description_path)
    if job.thumbnail_source:
        _move_if_in_inbox(job.thumbnail_source)
    moved_svs = _move_if_in_inbox(job.svs_path)
    if moved_svs is None and job.svs_path.exists():
        raise UploaderError("SVS arsiv klasorune tasinamadi.")
    if meta_path.exists():
        state = load_json(meta_path, job.state)
        state["archived"] = True
        state["stage"] = "complete"
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_json(meta_path, state)
        _move_if_in_inbox(meta_path)
    say("SVS yuklenen klasorune tasindi.", repo=job.repo_name, stage="archive", progress=100)


def cleanup_job_local_repo(job: SlideJob, *, automatic: bool = False) -> bool:
    if not job.repo_path.exists():
        return True
    if not (job.state.get("pages_verified") and job.state.get("gallery_verified")):
        if automatic:
            return False
        raise UploaderError("Web ve ana galeri dogrulanmadan yerel repo silinemez.")
    size = folder_size(job.repo_path)
    safe_rmtree(job.repo_path)
    if job.repo_path.exists():
        raise UploaderError(f"Yerel repo silinemedi: {job.repo_path}")
    job.state["local_repo_deleted"] = True
    say(f"Yerel repo temizlendi; {human_bytes(size)} alan bosaldi.", repo=job.repo_name, stage="cleanup", progress=100)
    return True


def verify_existing_local_repo_safe(repo_path: Path) -> Tuple[bool, str, int]:
    repo_name = repo_path.name
    if not re.match(rf"^{re.escape(REPO_PREFIX)}\d+$", repo_name):
        return False, "slide repo degil", 0
    if not (repo_path / ".git").exists():
        return False, "git reposu degil", folder_size(repo_path)
    status = git(["status", "--porcelain"], repo_path, allow_failure=True)
    if status.returncode != 0:
        return False, "git durumu okunamadi", folder_size(repo_path)
    if (status.stdout or "").strip():
        return False, "yerel degisiklik var", folder_size(repo_path)
    info = github_repo(repo_name)
    if not info:
        return False, "GitHub reposu yok", folder_size(repo_path)
    branch = info.get("default_branch") or "main"
    local_sha_res = git(["rev-parse", "HEAD"], repo_path, allow_failure=True)
    if local_sha_res.returncode != 0:
        return False, "yerel commit yok", folder_size(repo_path)
    local_sha = (local_sha_res.stdout or "").strip()
    remote_sha = remote_branch_sha(repo_name, branch)
    if not remote_sha or local_sha != remote_sha:
        return False, "yerel ve GitHub commit farkli", folder_size(repo_path)
    url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}/"
    try:
        page = public_get(url + f"?v={int(time.time())}", timeout=10)
        dzi = public_get(url + f"slide.dzi?v={int(time.time())}", timeout=10)
        if page.status_code != 200 or dzi.status_code != 200 or "<Image" not in dzi.text[:1200]:
            return False, f"web dogrulanmadi ({page.status_code}/{dzi.status_code})", folder_size(repo_path)
    except Exception as exc:
        return False, f"web kontrol hatasi: {exc}", folder_size(repo_path)
    return True, "GitHub commit ve web dogrulandi", folder_size(repo_path)


def scan_cleanup_candidates(skip_repos: Optional[Set[str]] = None) -> List[Tuple[str, Path, int]]:
    skip_repos = skip_repos or set()
    candidates: List[Tuple[str, Path, int]] = []
    for path in sorted(LOCAL_REPO_BASE.glob(f"{REPO_PREFIX}*")):
        if not path.is_dir() or path.name in skip_repos:
            continue
        try:
            safe, _, size = verify_existing_local_repo_safe(path)
            if safe:
                candidates.append((path.name, path, size))
        except Exception:
            LOGGER.exception("Eski repo temizleme kontrolu basarisiz: %s", path)
    return candidates


# -----------------------------------------------------------------------------
# Batch workflow
# -----------------------------------------------------------------------------

def process_batch(
    jobs: List[SlideJob],
    *,
    gallery_title: str,
    gallery_description: str,
    auto_cleanup: bool,
) -> Tuple[List[SlideJob], List[Tuple[SlideJob, str]]]:
    successful_uploads: List[SlideJob] = []
    failed: List[Tuple[SlideJob, str]] = []

    for index, job in enumerate(jobs, start=1):
        emit("batch", f"{index}/{len(jobs)}: {job.slide_title}", repo=job.repo_name, batch_index=index, batch_total=len(jobs))
        try:
            successful_uploads.append(process_slide_upload(job))
        except Exception as exc:
            job.save_state(last_error=str(exc), stage="error")
            LOGGER.exception("Slide upload failed: %s", job.svs_path.name)
            emit("error", str(exc), repo=job.repo_name, stage="error")
            failed.append((job, str(exc)))

    if successful_uploads:
        try:
            refresh = {job.repo_name for job in successful_uploads}
            sync_gallery(
                refresh,
                gallery_title=gallery_title,
                gallery_description=gallery_description,
            )
            wait_for_gallery_live(refresh, expected_title=gallery_title)
            for job in successful_uploads:
                job.save_state(gallery_verified=True, stage="gallery_live", last_error="")
                emit("info", "Ana galeride gorunuyor.", repo=job.repo_name, stage="gallery", progress=98)
        except Exception as exc:
            LOGGER.exception("Gallery synchronization failed")
            for job in successful_uploads:
                job.save_state(gallery_verified=False, last_error=f"Galeri: {exc}", stage="gallery_error")
                failed.append((job, f"Galeri guncellenemedi: {exc}"))
            successful_uploads = []

    completed: List[SlideJob] = []
    for job in successful_uploads:
        try:
            archive_completed_job(job)
            # Keep in-memory state usable after metadata moved away.
            job.state["archived"] = True
            completed.append(job)
        except Exception as exc:
            LOGGER.exception("Post-upload completion failed: %s", job.repo_name)
            # The slide is already on GitHub and verified in the main gallery.
            # Keep it as a post-publication archive problem instead of presenting
            # the upload itself as failed. The next run will retry this step.
            job.save_state(stage="archive_pending", last_error=f"Arsivleme: {exc}", gallery_verified=True)
            failed.append((job, f"Yayinlandi; yerel arsivleme bekliyor: {exc}"))
            emit(
                "warning",
                f"Web'de yayinlandi; SVS arsive tasinamadi. Sonraki calistirmada tekrar denenecek: {exc}",
                repo=job.repo_name,
                stage="archive_pending",
                progress=99,
            )
            continue

        if auto_cleanup:
            try:
                cleanup_job_local_repo(job, automatic=True)
            except Exception as exc:
                LOGGER.exception("Local cleanup failed: %s", job.repo_name)
                emit(
                    "cleanup_available",
                    f"Yukleme tamamlandi; yerel kopya otomatik silinemedi ({exc}). Arayuzden tekrar deneyebilirsiniz.",
                    repo=job.repo_name,
                    stage="cleanup",
                    progress=100,
                )
        elif job.repo_path.exists():
            emit(
                "cleanup_available",
                f"Yerel kopya guvenle silinebilir ({human_bytes(folder_size(job.repo_path))}).",
                repo=job.repo_name,
                stage="cleanup",
                progress=100,
            )

    emit(
        "batch_done",
        f"Tamamlandi: {len(completed)} tam, {len(failed)} yeniden deneme bekliyor",
        completed=len(completed),
        failed=len(failed),
    )
    return completed, failed


# -----------------------------------------------------------------------------
# CLI checks
# -----------------------------------------------------------------------------

def run_check() -> None:
    require_config()
    check_git()
    response = api_request("GET", "/user")
    login = response.json().get("login")
    if login and login.lower() != GITHUB_USERNAME.lower():
        warn(f"Token sahibi '{login}', .env GITHUB_USERNAME ise '{GITHUB_USERNAME}'.")
    pyvips = import_pyvips()
    test = pyvips.Image.black(16, 16)
    _ = test.width
    say(f"GitHub API hazir: {login or GITHUB_USERNAME}")
    say("pyvips/libvips hazir")
    say(f"Yuklenecek: {INBOX_DIR}")
    say(f"Yuklenen: {DONE_DIR}")
    say(f"Local repos: {LOCAL_REPO_BASE}")
    say("KONTROL BASARILI")


def cli_upload() -> int:
    require_config()
    check_git()
    svs_files = sorted(
        [p for p in INBOX_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".svs"],
        key=lambda p: p.name.lower(),
    )
    if not svs_files:
        title, desc = load_remote_gallery_settings()
        sync_gallery(gallery_title=title, gallery_description=desc)
        return 0
    jobs = build_jobs(svs_files, set(gallery_repo_names()))
    for job in jobs:
        if not job.prepared:
            save_job_preparation(job, job.slide_title, job.description, job.thumbnail_source)
    title, desc = load_remote_gallery_settings()
    _, failed = process_batch(jobs, gallery_title=title, gallery_description=desc, auto_cleanup=False)
    return 1 if failed else 0

# -----------------------------------------------------------------------------
# Tkinter GUI
# -----------------------------------------------------------------------------

def launch_gui() -> int:
    LOGGER.info("GUI baslatiliyor: %s | dosya=%s", APP_VERSION, Path(__file__).resolve())
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:
        raise UploaderError(f"Tkinter acilamadi: {exc}. Komut satiri icin --cli kullanabilirsiniz.") from exc

    STAGE_LABELS = {
        "preparation": "Bilgi bekliyor",
        "prepared": "Hazir",
        "start": "Basliyor",
        "local_repo": "Yerel repo",
        "deepzoom": "DeepZoom",
        "deepzoom_ready": "DeepZoom hazir",
        "thumbnail": "Thumbnail",
        "repo": "GitHub repo",
        "push": "GitHub'a yukleniyor",
        "pushed": "Push tamamlandi",
        "pages": "Web sayfasi",
        "pages_configured": "Pages ayarlandi",
        "pages_live": "Web dogrulandi",
        "gallery": "Ana galeri",
        "gallery_live": "Galeride gorunuyor",
        "archive": "Arsivleniyor",
        "archive_pending": "Yayinlandi - arsiv bekliyor",
        "cleanup": "Yerel kopya temizligi",
        "complete": "Tamamlandi",
        "error": "Hata",
        "gallery_error": "Galeri hatasi",
        "deepzoom_error": "DeepZoom hatasi",
        "complete_error": "Tamamlama hatasi",
    }

    def stage_text(stage: str, prepared: bool = False) -> str:
        if not stage:
            return "Hazir" if prepared else "Bilgi bekliyor"
        return STAGE_LABELS.get(stage, stage)

    def job_milestones(job: SlideJob) -> Tuple[str, str, str, str, str]:
        state = job.state
        github = "Yuklendi" if state.get("pushed") else "Bekliyor"
        page = "Acildi" if state.get("pages_verified") else "Bekliyor"
        gallery = "Eklendi" if state.get("gallery_verified") else "Bekliyor"
        archive = "Arsivlendi" if state.get("archived") else "Bekliyor"
        if state.get("pages_verified") and state.get("gallery_verified") and state.get("archived"):
            if not job.repo_path.exists():
                hdd = "Silindi"
            else:
                hdd = "Silinebilir"
        elif not job.repo_path.exists() and state.get("pushed"):
            hdd = "Silindi"
        else:
            hdd = "Bekliyor"
        return github, page, gallery, archive, hdd

    class JobAccordion(ttk.Frame):
        def __init__(self, parent: Any, job: SlideJob, app: Any):
            super().__init__(parent, padding=(0, 2))
            self.job = job
            self.app = app
            self.expanded = False
            self.last_message = ""
            self.last_error = str(job.state.get("last_error") or "")
            self.progress_value = 8 if job.prepared else 2

            self.header = ttk.Frame(self)
            self.header.pack(fill="x")
            self.toggle_btn = ttk.Button(self.header, text=">", width=3, command=self.toggle)
            self.toggle_btn.pack(side="left", padx=(0, 6))
            self.title_label = ttk.Label(self.header, text="", font=("Segoe UI", 10, "bold"))
            self.title_label.pack(side="left", fill="x", expand=True)
            self.status_label = ttk.Label(self.header, text="")
            self.status_label.pack(side="right", padx=(8, 2))

            self.milestone_var = tk.StringVar(value="")
            self.milestone_label = ttk.Label(self, textvariable=self.milestone_var)
            self.milestone_label.pack(fill="x", padx=(38, 4), pady=(1, 1))

            self.progress = ttk.Progressbar(self, maximum=100, mode="determinate")
            self.progress.pack(fill="x", padx=(38, 4), pady=(2, 3))

            self.details = ttk.Frame(self, padding=(38, 4, 6, 8))
            self.info_var = tk.StringVar(value="")
            self.info = ttk.Label(self.details, textvariable=self.info_var, justify="left", wraplength=950)
            self.info.pack(anchor="w", fill="x")
            self.error_var = tk.StringVar(value="")
            self.error_label = ttk.Label(self.details, textvariable=self.error_var, justify="left", wraplength=950)
            self.error_label.pack(anchor="w", fill="x", pady=(4, 4))
            buttons = ttk.Frame(self.details)
            buttons.pack(fill="x", pady=(3, 0))
            self.web_btn = ttk.Button(buttons, text="Web'i Ac", command=self.open_web)
            self.web_btn.pack(side="left", padx=(0, 6))
            self.local_btn = ttk.Button(buttons, text="Yerel Klasoru Ac", command=self.open_local)
            self.local_btn.pack(side="left", padx=(0, 6))
            self.delete_btn = ttk.Button(buttons, text="Yerel Kopyayi Sil", command=self.delete_local)
            self.delete_btn.pack(side="left")
            self.refresh_from_job()

        def toggle(self) -> None:
            self.expanded = not self.expanded
            self.toggle_btn.configure(text="v" if self.expanded else ">")
            if self.expanded:
                self.details.pack(fill="x")
            else:
                self.details.pack_forget()

        def open_web(self) -> None:
            webbrowser.open(self.job.web_url)

        def open_local(self) -> None:
            try:
                open_local_path(self.job.repo_path)
            except Exception as exc:
                messagebox.showerror("Yerel repo", str(exc))

        def delete_local(self) -> None:
            self.app.delete_current_job_repo(self.job)

        def update_event(self, stage: Optional[str], progress: Optional[int], message: str, error: bool = False) -> None:
            if stage:
                self.job.state["stage"] = stage
            if progress is not None:
                self.progress_value = max(0, min(100, int(progress)))
            self.last_message = message
            if error:
                self.last_error = message
            self.refresh_from_job()

        def refresh_from_job(self) -> None:
            stage = str(self.job.state.get("stage") or ("prepared" if self.job.prepared else "preparation"))
            if self.job.state.get("gallery_verified") and self.job.state.get("archived"):
                stage = "complete"
            self.title_label.configure(text=f"{self.job.repo_name}  |  {self.job.slide_title}")
            self.status_label.configure(text=stage_text(stage, self.job.prepared))
            github, page, gallery, archive, hdd = job_milestones(self.job)
            self.milestone_var.set(
                f"GitHub: {github}   |   Page: {page}   |   Galeri: {gallery}   |   SVS: {archive}   |   HDD repo: {hdd}"
            )
            self.progress["value"] = self.progress_value
            local_size = folder_size(self.job.repo_path) if self.job.repo_path.exists() else 0
            lines = [
                f"Dosya: {self.job.svs_path.name}",
                f"Repo: {self.job.repo_name}",
                f"Web: {self.job.web_url}",
                f"Yerel: {self.job.repo_path}",
                f"Yerel boyut: {human_bytes(local_size) if local_size else '-'}",
            ]
            if self.last_message:
                lines.append(f"Son islem: {self.last_message}")
            self.info_var.set("\n".join(lines))
            current_error = self.last_error or str(self.job.state.get("last_error") or "")
            self.error_var.set(f"Hata: {current_error}" if current_error else "")
            self.web_btn.configure(state="normal" if self.job.state.get("pages_verified") else "disabled")
            self.local_btn.configure(state="normal" if self.job.repo_path.exists() else "disabled")
            safe_delete = bool(
                self.job.repo_path.exists()
                and self.job.state.get("pages_verified")
                and self.job.state.get("gallery_verified")
            )
            self.delete_btn.configure(state="normal" if safe_delete else "disabled")

    class UploaderApp(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title(f"Whole Slide GitHub Uploader - {APP_VERSION}")
            self.geometry("1450x860")
            self.minsize(1150, 700)
            self.protocol("WM_DELETE_WINDOW", self.on_close)

            self.events: "queue.Queue[dict]" = queue.Queue()
            set_event_sink(self.events.put)
            self.jobs: List[SlideJob] = []
            self.job_by_repo: Dict[str, SlideJob] = {}
            self.cards: Dict[str, JobAccordion] = {}
            self.current_job: Optional[SlideJob] = None
            self.busy = False
            self.cleanup_candidates: List[Tuple[str, Path, int]] = []
            self.ui_settings = load_json(UI_SETTINGS_PATH, {"auto_cleanup": True})
            self.gallery_title = "Slide Gallery"
            self.gallery_description = "Interactive whole-slide microscopy gallery."

            style = ttk.Style(self)
            try:
                style.theme_use("vista" if "vista" in style.theme_names() else "clam")
            except Exception:
                pass
            style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
            style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))

            self._build_ui()
            self.after(100, self.process_events)
            if os.getenv("WSI_GUI_SMOKE_TEST") == "1":
                self.after(350, self.destroy)
            else:
                self.after(200, self.startup_scan)

        def _build_ui(self) -> None:
            outer = ttk.Frame(self, padding=12)
            outer.pack(fill="both", expand=True)

            top = ttk.Frame(outer)
            top.pack(fill="x", pady=(0, 8))
            ttk.Label(top, text="Whole Slide Uploader", style="Title.TLabel").pack(side="left")
            self.connection_var = tk.StringVar(value="Hazirlaniyor...")
            ttk.Label(top, textvariable=self.connection_var).pack(side="right")

            tracking_box = ttk.LabelFrame(outer, text="Yukleme Takibi", padding=10)
            tracking_box.pack(fill="x", pady=(0, 8))
            self.tracking_var = tk.StringVar(
                value="GitHub: 0/0 yuklendi   |   Page: 0/0 acildi   |   Galeri: 0/0 eklendi   |   SVS: 0/0 arsivlendi   |   HDD repo: 0/0 silindi"
            )
            ttk.Label(tracking_box, textvariable=self.tracking_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")
            ttk.Label(
                tracking_box,
                text="Her repo icin asagidaki satirdan ayrintiyi acabilirsiniz. HDD repo ancak web ve galeri dogrulandiktan sonra silinir.",
            ).pack(anchor="w", pady=(4, 0))

            middle = ttk.Panedwindow(outer, orient="horizontal")
            middle.pack(fill="both", expand=True, pady=(0, 8))

            left = ttk.LabelFrame(middle, text="Yuklenecek Slaytlar", padding=8)
            right = ttk.LabelFrame(middle, text="Secili Slayt Hazirligi", padding=10)
            middle.add(left, weight=3)
            middle.add(right, weight=2)

            self.tree = ttk.Treeview(
                left,
                columns=("repo", "github", "page", "gallery", "archive", "hdd"),
                show="tree headings",
                height=10,
            )
            self.tree.heading("#0", text="SVS")
            self.tree.heading("repo", text="Repo")
            self.tree.heading("github", text="GitHub")
            self.tree.heading("page", text="Page")
            self.tree.heading("gallery", text="Galeri")
            self.tree.heading("archive", text="SVS")
            self.tree.heading("hdd", text="HDD repo")
            self.tree.column("#0", width=230, minwidth=160)
            self.tree.column("repo", width=88, anchor="center")
            self.tree.column("github", width=82, anchor="center")
            self.tree.column("page", width=75, anchor="center")
            self.tree.column("gallery", width=75, anchor="center")
            self.tree.column("archive", width=88, anchor="center")
            self.tree.column("hdd", width=90, anchor="center")
            self.tree.pack(fill="both", expand=True)
            self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
            left_buttons = ttk.Frame(left)
            left_buttons.pack(fill="x", pady=(6, 0))
            self.rescan_btn = ttk.Button(left_buttons, text="Klasoru Yeniden Tara", command=self.rescan)
            self.rescan_btn.pack(side="left")
            ttk.Button(left_buttons, text="Yuklenen Klasorunu Ac", command=lambda: self.safe_open(DONE_DIR)).pack(side="right")

            ttk.Label(right, text="Baslik:").grid(row=0, column=0, sticky="w")
            self.slide_title_var = tk.StringVar()
            self.slide_title_entry = ttk.Entry(right, textvariable=self.slide_title_var)
            self.slide_title_entry.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 8))
            ttk.Label(right, text="Aciklama:").grid(row=2, column=0, sticky="w")
            self.slide_desc = tk.Text(right, height=6, wrap="word")
            self.slide_desc.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(2, 8))
            ttk.Label(right, text="Thumbnail (bos = SVS'den otomatik):").grid(row=4, column=0, columnspan=3, sticky="w")
            self.thumb_var = tk.StringVar()
            self.thumb_entry = ttk.Entry(right, textvariable=self.thumb_var)
            self.thumb_entry.grid(row=5, column=0, sticky="ew", pady=(2, 4))
            ttk.Button(right, text="Sec...", command=self.choose_thumbnail).grid(row=5, column=1, padx=(6, 4))
            ttk.Button(right, text="Otomatik", command=lambda: self.thumb_var.set("")).grid(row=5, column=2)
            self.thumb_help_var = tk.StringVar(value=f"Otomatik thumbnail: en fazla {THUMB_MAX_PX}px, hedef {THUMB_TARGET_BYTES // 1024} KB JPEG.")
            ttk.Label(right, textvariable=self.thumb_help_var).grid(row=6, column=0, columnspan=3, sticky="w")
            self.save_prep_btn = ttk.Button(right, text="Hazirligi Kaydet", command=self.save_current_preparation)
            self.save_prep_btn.grid(row=7, column=0, sticky="w", pady=(10, 0))
            self.prep_status_var = tk.StringVar(value="")
            ttk.Label(right, textvariable=self.prep_status_var).grid(row=7, column=1, columnspan=2, sticky="e", pady=(10, 0))
            right.columnconfigure(0, weight=1)
            right.rowconfigure(3, weight=1)

            control = ttk.LabelFrame(outer, text="Yukleme", padding=10)
            control.pack(fill="x", pady=(0, 8))
            row = ttk.Frame(control)
            row.pack(fill="x")
            self.auto_cleanup_var = tk.BooleanVar(value=bool(self.ui_settings.get("auto_cleanup", True)))
            ttk.Checkbutton(
                row,
                text="Web + ana galeri dogrulaninca yeni gallery-* yerel reposunu otomatik sil",
                variable=self.auto_cleanup_var,
            ).pack(side="left")
            self.start_btn = ttk.Button(row, text="Tumunu Yukle", command=self.start_upload, state="disabled")
            self.start_btn.pack(side="right")
            self.overall_var = tk.StringVar(value="Klasor taraniyor...")
            ttk.Label(control, textvariable=self.overall_var).pack(anchor="w", pady=(6, 2))
            self.overall_progress = ttk.Progressbar(control, maximum=100, mode="determinate")
            self.overall_progress.pack(fill="x")

            progress_box = ttk.LabelFrame(outer, text="Repo Ilerlemesi - satira tiklayarak ayrintiyi ac/kapat", padding=6)
            progress_box.pack(fill="both", expand=True, pady=(0, 8))
            self.progress_canvas = tk.Canvas(progress_box, height=190, highlightthickness=0)
            self.progress_scroll = ttk.Scrollbar(progress_box, orient="vertical", command=self.progress_canvas.yview)
            self.progress_inner = ttk.Frame(self.progress_canvas)
            self.progress_window = self.progress_canvas.create_window((0, 0), window=self.progress_inner, anchor="nw")
            self.progress_canvas.configure(yscrollcommand=self.progress_scroll.set)
            self.progress_canvas.pack(side="left", fill="both", expand=True)
            self.progress_scroll.pack(side="right", fill="y")
            self.progress_inner.bind("<Configure>", lambda e: self.progress_canvas.configure(scrollregion=self.progress_canvas.bbox("all")))
            self.progress_canvas.bind("<Configure>", lambda e: self.progress_canvas.itemconfigure(self.progress_window, width=e.width))

            disk_box = ttk.LabelFrame(outer, text="Disk Alani", padding=8)
            disk_box.pack(fill="x")
            self.cleanup_var = tk.StringVar(value="Eski yerel gallery-* kopyalari guvenlik acisindan kontrol edilecek.")
            ttk.Label(disk_box, textvariable=self.cleanup_var).pack(side="left", fill="x", expand=True)
            self.cleanup_list_btn = ttk.Button(disk_box, text="Liste", command=self.show_cleanup_list, state="disabled")
            self.cleanup_list_btn.pack(side="right", padx=(6, 0))
            self.cleanup_btn = ttk.Button(disk_box, text="Dogrulanmis Eski Kopyalari Sil", command=self.delete_cleanup_candidates, state="disabled")
            self.cleanup_btn.pack(side="right")

        def safe_open(self, path: Path) -> None:
            try:
                open_local_path(path)
            except Exception as exc:
                messagebox.showerror("Klasor", str(exc))

        def set_busy(self, busy: bool) -> None:
            self.busy = busy
            normal = "disabled" if busy else "normal"
            self.rescan_btn.configure(state=normal)
            self.save_prep_btn.configure(state=normal)
            self.start_btn.configure(state="disabled" if busy or not self.jobs else "normal")

        def startup_scan(self) -> None:
            if self.busy:
                return
            self.set_busy(True)
            self.connection_var.set("GitHub'a baglaniliyor...")
            threading.Thread(target=self._startup_worker, daemon=True).start()

        def _startup_worker(self) -> None:
            try:
                require_config()
                check_git()
                remote_names = set(gallery_repo_names())
                svs_files = sorted(
                    [p for p in INBOX_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".svs"],
                    key=lambda p: p.name.lower(),
                )
                jobs = build_jobs(svs_files, remote_names)
                try:
                    title, desc = load_remote_gallery_settings()
                except Exception as exc:
                    LOGGER.exception("Gallery settings load failed")
                    title, desc = "Slide Gallery", "Interactive whole-slide microscopy gallery."
                    self.events.put({"kind": "warning", "message": f"Galeri bilgileri okunamadi: {exc}"})
                self.events.put({"kind": "startup_loaded", "jobs": jobs, "gallery_title": title, "gallery_description": desc})
            except Exception as exc:
                LOGGER.exception("Startup scan failed")
                self.events.put({"kind": "startup_error", "message": str(exc)})

        def populate_jobs(self, jobs: List[SlideJob]) -> None:
            self.jobs = jobs
            self.job_by_repo = {job.repo_name: job for job in jobs}
            self.tree.delete(*self.tree.get_children())
            for child in list(self.progress_inner.winfo_children()):
                child.destroy()
            self.cards.clear()
            for job in jobs:
                stage = str(job.state.get("stage") or ("prepared" if job.prepared else "preparation"))
                github, page, gallery, archive, hdd = job_milestones(job)
                self.tree.insert(
                    "", "end", iid=job.repo_name, text=job.svs_path.name,
                    values=(job.repo_name, github, page, gallery, archive, hdd),
                )
                card = JobAccordion(self.progress_inner, job, self)
                card.pack(fill="x", pady=(0, 3))
                self.cards[job.repo_name] = card
            if jobs:
                self.tree.selection_set(jobs[0].repo_name)
                self.tree.focus(jobs[0].repo_name)
                self.load_job_editor(jobs[0])
                prepared_count = sum(1 for job in jobs if job.prepared)
                self.overall_var.set(f"{len(jobs)} SVS bulundu. {prepared_count}/{len(jobs)} hazirlandi.")
                self.start_btn.configure(state="normal" if not self.busy else "disabled")
            else:
                self.current_job = None
                self.clear_editor()
                self.overall_var.set("Yuklenecek klasorunde SVS yok.")
                self.start_btn.configure(state="disabled")
            self.refresh_tracking_summary()
            threading.Thread(target=self._cleanup_scan_worker, daemon=True).start()

        def refresh_tracking_summary(self) -> None:
            total = len(self.jobs)
            if total == 0:
                self.tracking_var.set(
                    "GitHub: 0/0 yuklendi   |   Page: 0/0 acildi   |   Galeri: 0/0 eklendi   |   SVS: 0/0 arsivlendi   |   HDD repo: 0/0 silindi"
                )
                return
            pushed = sum(1 for job in self.jobs if job.state.get("pushed"))
            pages = sum(1 for job in self.jobs if job.state.get("pages_verified"))
            gallery = sum(1 for job in self.jobs if job.state.get("gallery_verified"))
            archived = sum(1 for job in self.jobs if job.state.get("archived"))
            deleted = sum(
                1 for job in self.jobs
                if job.state.get("archived") and job.state.get("gallery_verified") and not job.repo_path.exists()
            )
            self.tracking_var.set(
                f"GitHub: {pushed}/{total} yuklendi   |   Page: {pages}/{total} acildi   |   "
                f"Galeri: {gallery}/{total} eklendi   |   SVS: {archived}/{total} arsivlendi   |   "
                f"HDD repo: {deleted}/{total} silindi"
            )

        def clear_editor(self) -> None:
            self.slide_title_var.set("")
            self.slide_desc.delete("1.0", "end")
            self.thumb_var.set("")
            self.prep_status_var.set("")

        def on_tree_select(self, event: Any = None) -> None:
            selection = self.tree.selection()
            if not selection:
                return
            job = self.job_by_repo.get(selection[0])
            if job:
                self.load_job_editor(job)

        def load_job_editor(self, job: SlideJob) -> None:
            self.current_job = job
            self.slide_title_var.set(job.slide_title)
            self.slide_desc.delete("1.0", "end")
            self.slide_desc.insert("1.0", job.description)
            self.thumb_var.set(str(job.thumbnail_source) if job.thumbnail_source else "")
            if job.state.get("gallery_verified") and job.state.get("archived"):
                self.prep_status_var.set("Tamamlandi")
            else:
                self.prep_status_var.set("Hazir" if job.prepared else "Bilgileri duzenleyip kaydedin")

        def choose_thumbnail(self) -> None:
            filename = filedialog.askopenfilename(
                title="Thumbnail sec",
                filetypes=[("Gorseller", "*.jpg *.jpeg *.png *.webp *.tif *.tiff"), ("Tum dosyalar", "*.*")],
            )
            if filename:
                self.thumb_var.set(filename)

        def save_current_preparation(self, silent: bool = False) -> bool:
            job = self.current_job
            if not job:
                return False
            try:
                title = self.slide_title_var.get().strip()
                description = self.slide_desc.get("1.0", "end").strip()
                thumb_raw = self.thumb_var.get().strip()
                thumb = Path(thumb_raw) if thumb_raw else None
                save_job_preparation(job, title, description, thumb)
                self.prep_status_var.set("Hazirlik kaydedildi")
                if self.tree.exists(job.repo_name):
                    self.tree.set(job.repo_name, "status", "Hazir")
                card = self.cards.get(job.repo_name)
                if card:
                    card.refresh_from_job()
                prepared_count = sum(1 for item in self.jobs if item.prepared)
                self.overall_var.set(f"{len(self.jobs)} SVS bulundu. {prepared_count}/{len(self.jobs)} hazirlandi.")
                if not silent:
                    messagebox.showinfo("Hazirlik", "Baslik, aciklama ve thumbnail tercihi kaydedildi.")
                return True
            except Exception as exc:
                if not silent:
                    messagebox.showerror("Hazirlik", str(exc))
                return False

        def start_upload(self) -> None:
            if self.busy or not self.jobs:
                return
            if self.current_job and not self.save_current_preparation(silent=True):
                messagebox.showerror("Hazirlik", "Secili slaytin bilgileri kaydedilemedi.")
                return
            unprepared = [job for job in self.jobs if not job.prepared]
            if unprepared:
                first = unprepared[0]
                self.tree.selection_set(first.repo_name)
                self.tree.focus(first.repo_name)
                self.tree.see(first.repo_name)
                self.load_job_editor(first)
                messagebox.showwarning(
                    "Hazirlik eksik",
                    f"Once tum SVS'lerin hazirligini kaydedin. Eksik: {len(unprepared)}",
                )
                return
            title = self.gallery_title or "Slide Gallery"
            desc = self.gallery_description or "Interactive whole-slide microscopy gallery."
            if not messagebox.askyesno(
                "Yuklemeyi baslat",
                f"{len(self.jobs)} slayt yuklenecek. Tum hazirliklar tamam. Baslatilsin mi?",
            ):
                return
            self.set_busy(True)
            self.overall_progress["value"] = 2
            self.overall_var.set("Yukleme basladi...")
            jobs_snapshot = list(self.jobs)
            auto_cleanup = bool(self.auto_cleanup_var.get())
            threading.Thread(
                target=self._upload_worker,
                args=(jobs_snapshot, title, desc, auto_cleanup),
                daemon=True,
            ).start()

        def _upload_worker(self, jobs: List[SlideJob], title: str, desc: str, auto_cleanup: bool) -> None:
            try:
                process_batch(jobs, gallery_title=title, gallery_description=desc, auto_cleanup=auto_cleanup)
            except Exception as exc:
                LOGGER.exception("Batch worker failed")
                self.events.put({"kind": "batch_crash", "message": str(exc)})

        def rescan(self) -> None:
            if self.busy:
                return
            if self.current_job:
                self.save_current_preparation(silent=True)
            self.startup_scan()

        def _cleanup_scan_worker(self) -> None:
            try:
                skip = {job.repo_name for job in self.jobs}
                candidates = scan_cleanup_candidates(skip)
                self.events.put({"kind": "cleanup_scan_done", "candidates": candidates})
            except Exception as exc:
                self.events.put({"kind": "cleanup_scan_error", "message": str(exc)})

        def show_cleanup_list(self) -> None:
            if not self.cleanup_candidates:
                return
            lines = [f"{name}  -  {human_bytes(size)}" for name, _, size in self.cleanup_candidates]
            messagebox.showinfo("Guvenle silinebilir yerel repolar", "\n".join(lines))

        def delete_cleanup_candidates(self) -> None:
            if self.busy or not self.cleanup_candidates:
                return
            total = sum(size for _, _, size in self.cleanup_candidates)
            if not messagebox.askyesno(
                "Yerel kopyalari sil",
                f"{len(self.cleanup_candidates)} eski repo tekrar dogrulanip silinecek. Yaklasik {human_bytes(total)} alan bosalacak. Devam?",
            ):
                return
            self.set_busy(True)
            threading.Thread(target=self._delete_cleanup_worker, daemon=True).start()

        def _delete_cleanup_worker(self) -> None:
            deleted: List[str] = []
            freed = 0
            skipped: List[str] = []
            for name, path, _ in list(self.cleanup_candidates):
                try:
                    safe, reason, size = verify_existing_local_repo_safe(path)
                    if safe:
                        safe_rmtree(path)
                        if not path.exists():
                            deleted.append(name)
                            freed += size
                        else:
                            skipped.append(f"{name}: silinemedi")
                    else:
                        skipped.append(f"{name}: {reason}")
                except Exception as exc:
                    skipped.append(f"{name}: {exc}")
            self.events.put({"kind": "cleanup_delete_done", "deleted": deleted, "freed": freed, "skipped": skipped})

        def delete_current_job_repo(self, job: SlideJob) -> None:
            if self.busy:
                return
            if not (job.state.get("pages_verified") and job.state.get("gallery_verified")):
                messagebox.showwarning("Yerel repo", "Web ve ana galeri dogrulanmadan silme yapilmaz.")
                return
            size = folder_size(job.repo_path)
            if not messagebox.askyesno(
                "Yerel repoyu sil",
                f"{job.repo_name} web'de dogrulandi. Yerel kopya {human_bytes(size)}. Silinsin mi?",
            ):
                return
            try:
                cleanup_job_local_repo(job)
                self.update_tree_job(job)
                card = self.cards.get(job.repo_name)
                if card:
                    card.refresh_from_job()
            except Exception as exc:
                messagebox.showerror("Yerel repo", str(exc))

        def update_tree_job(self, job: SlideJob) -> None:
            if not self.tree.exists(job.repo_name):
                return
            github, page, gallery, archive, hdd = job_milestones(job)
            self.tree.item(
                job.repo_name,
                text=job.svs_path.name,
                values=(job.repo_name, github, page, gallery, archive, hdd),
            )
            self.refresh_tracking_summary()

        def process_events(self) -> None:
            try:
                while True:
                    event = self.events.get_nowait()
                    kind = event.get("kind", "")
                    repo = event.get("repo")
                    message = str(event.get("message") or "")

                    if kind == "startup_loaded":
                        self.gallery_title = event.get("gallery_title") or "Slide Gallery"
                        self.gallery_description = event.get("gallery_description") or "Interactive whole-slide microscopy gallery."
                        self.connection_var.set(f"GitHub: {GITHUB_USERNAME}")
                        self.set_busy(False)
                        self.populate_jobs(event.get("jobs") or [])
                        continue
                    if kind == "startup_error":
                        self.set_busy(False)
                        self.connection_var.set("Baglanti / ayar hatasi")
                        self.overall_var.set(message)
                        messagebox.showerror("Baslatma hatasi", message)
                        continue
                    if kind == "cleanup_scan_done":
                        self.cleanup_candidates = event.get("candidates") or []
                        total = sum(item[2] for item in self.cleanup_candidates)
                        if self.cleanup_candidates:
                            self.cleanup_var.set(
                                f"{len(self.cleanup_candidates)} eski yerel repo GitHub commit + web ile dogrulandi; {human_bytes(total)} guvenle silinebilir."
                            )
                            self.cleanup_btn.configure(state="normal" if not self.busy else "disabled")
                            self.cleanup_list_btn.configure(state="normal")
                        else:
                            self.cleanup_var.set("Silinmesi onerilen dogrulanmis eski yerel repo yok.")
                            self.cleanup_btn.configure(state="disabled")
                            self.cleanup_list_btn.configure(state="disabled")
                        continue
                    if kind == "cleanup_scan_error":
                        self.cleanup_var.set("Eski repo kontrolu tamamlanamadi: " + message)
                        continue
                    if kind == "cleanup_delete_done":
                        self.set_busy(False)
                        deleted = event.get("deleted") or []
                        skipped = event.get("skipped") or []
                        freed = int(event.get("freed") or 0)
                        self.cleanup_candidates = []
                        self.cleanup_var.set(f"{len(deleted)} repo silindi; {human_bytes(freed)} alan bosaldi.")
                        self.cleanup_btn.configure(state="disabled")
                        self.cleanup_list_btn.configure(state="disabled")
                        if skipped:
                            messagebox.showwarning("Disk temizligi", "Bazi repolar silinmedi:\n" + "\n".join(skipped[:15]))
                        else:
                            messagebox.showinfo("Disk temizligi", f"{len(deleted)} repo silindi. {human_bytes(freed)} alan bosaldi.")
                        threading.Thread(target=self._cleanup_scan_worker, daemon=True).start()
                        continue
                    if kind == "batch_crash":
                        self.set_busy(False)
                        self.overall_var.set("Yukleme durdu: " + message)
                        messagebox.showerror("Yukleme", message)
                        continue

                    if repo and repo in self.job_by_repo:
                        job = self.job_by_repo[repo]
                        stage = event.get("stage")
                        if stage:
                            job.state["stage"] = stage
                        if kind == "error":
                            job.state["last_error"] = message
                        card = self.cards.get(repo)
                        if card:
                            card.update_event(stage, event.get("progress"), message, error=(kind == "error"))
                        self.update_tree_job(job)

                    if kind == "batch":
                        idx = int(event.get("batch_index") or 0)
                        total = max(1, int(event.get("batch_total") or len(self.jobs) or 1))
                        self.overall_var.set(message)
                        self.overall_progress["value"] = max(2, min(90, int((idx - 1) / total * 90) + 5))
                    elif kind == "batch_done":
                        self.set_busy(False)
                        completed = int(event.get("completed") or 0)
                        failed = int(event.get("failed") or 0)
                        self.overall_progress["value"] = 100 if failed == 0 else 95
                        self.overall_var.set(message)
                        for job in self.jobs:
                            # Do not mark a job archived just because its web page
                            # and main-gallery entry are verified. Archiving is a
                            # separate local filesystem step and may fail on Windows.
                            self.update_tree_job(job)
                            card = self.cards.get(job.repo_name)
                            if card:
                                if job.state.get("gallery_verified") and job.state.get("archived"):
                                    card.progress_value = 100
                                elif job.state.get("gallery_verified"):
                                    card.progress_value = max(card.progress_value, 99)
                                card.refresh_from_job()
                        if failed:
                            published = sum(1 for job in self.jobs if job.state.get("gallery_verified"))
                            archive_pending = sum(
                                1 for job in self.jobs
                                if job.state.get("gallery_verified") and not job.state.get("archived")
                            )
                            messagebox.showwarning(
                                "Islem tamamlandi",
                                f"Web'de yayinlanan: {published}\n"
                                f"Tam arsivlenen: {completed}\n"
                                f"Arsiv/yeniden deneme bekleyen: {archive_pending}\n\n"
                                "Yayinlanmis bir slayt arsive tasinamadiysa tekrar yuklenmez; sonraki acilista yalnizca eksik son adim tamamlanir.",
                            )
                        else:
                            messagebox.showinfo("Yukleme tamamlandi", f"{completed} slayt basariyla yayinlandi ve galeride dogrulandi.")
                        threading.Thread(target=self._cleanup_scan_worker, daemon=True).start()
                    elif kind in {"info", "warning", "error", "cleanup_available"} and message:
                        if not repo:
                            self.overall_var.set(message)
            except queue.Empty:
                pass
            finally:
                self.after(100, self.process_events)

        def on_close(self) -> None:
            if self.busy:
                if not messagebox.askyesno(
                    "Cikis",
                    "Bir islem suruyor. Pencereyi kapatmak islemi kesebilir; durum dosyalari sonraki acilista devam etmek icin saklanir. Yine de cikilsin mi?",
                ):
                    return
            try:
                atomic_write_json(UI_SETTINGS_PATH, {"auto_cleanup": bool(self.auto_cleanup_var.get())})
            except Exception:
                pass
            set_event_sink(None)
            self.destroy()

    app = UploaderApp()
    app.mainloop()
    return 0


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SVS dosyalarini DeepZoom'a cevirir, GitHub Pages'e yukler ve galeri reposunu gunceller."
    )
    parser.add_argument("--gui", action="store_true", help="Gorsel arayuzu zorla ac")
    parser.add_argument("--version", action="store_true", help="Program surumunu yazdir ve cik")
    parser.add_argument("--check", action="store_true", help="Bagimlilik ve GitHub API kontrolu")
    parser.add_argument("--gallery-only", action="store_true", help="Sadece ana galeriyi senkronize et")
    parser.add_argument("--cli", action="store_true", help="Gorsel arayuz yerine komut satiri akisini calistir")
    args = parser.parse_args()

    try:
        LOGGER.info("Program basladi: %s | dosya=%s | argv=%s", APP_VERSION, Path(__file__).resolve(), sys.argv[1:])
        if args.version:
            print(f"{APP_VERSION} | {Path(__file__).resolve()}")
            return 0
        if args.check:
            run_check()
            return 0
        if args.gallery_only:
            require_config()
            check_git()
            title, desc = load_remote_gallery_settings()
            sync_gallery(gallery_title=title, gallery_description=desc)
            wait_for_gallery_live(set(), expected_title=title)
            return 0
        if args.cli and args.gui:
            raise UploaderError("--cli ve --gui ayni anda kullanilamaz.")
        if args.cli:
            return cli_upload()

        # GUI is the default. --gui exists so a launcher can make the intent
        # explicit and avoid accidentally running an older CLI copy.
        return launch_gui()
    except KeyboardInterrupt:
        print("Kullanici tarafindan durduruldu.", flush=True)
        return 130
    except Exception as exc:
        LOGGER.exception("Fatal error")
        print(f"HATA: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
