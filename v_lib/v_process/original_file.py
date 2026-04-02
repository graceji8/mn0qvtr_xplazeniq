import os
os.environ['DISPLAY'] = ':99'

import sys
import json
import time
import re
import threading
import pickle
import cv2
import mss
import numpy as np
import pyautogui
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# Add autogui to path
script_dir = Path(__file__).parent.absolute()
autogui_dir = script_dir.parent
if str(autogui_dir) not in sys.path:
    sys.path.append(str(autogui_dir))

from template_finder import ScreenTemplateFinder

# Global constants
AUTOMATION_STATE_FILE = script_dir / "automation_state.json"
TEMPLATES_DIR         = script_dir / "templates"
STATUS_FILE_NAME      = "processing_status.json"
STOP_SIGNAL_FILE      = "/tmp/stop_automation"


# ── Google Drive (OAuth — shared token.pickle with upload_folder_to_drive.py) ─
try:
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    import io
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

GDRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

# ─────────────────────────────────────────────────────────────────────────────

# Try to make the process DPI aware to fix multi-monitor/scaling issues
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

root_dir    = script_dir.parent.parent
CONFIG_FILE = script_dir / "ui_config.json"

# credentials.json + token.pickle search path (searches script folder, CWD, and project root)
def find_drive_file(filename):
    search_dirs = [script_dir, Path.cwd(), root_dir]
    for d in search_dirs:
        p = d / filename
        if p.exists():
            return p
    return script_dir / filename 

CREDS_FILE = find_drive_file("credentials.json")
TOKEN_FILE = find_drive_file("token.pickle")
# ─────────────────────────────────────────────────────────────────────────────

# Force UTF-8 console output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def update_automation_state(project_date_str):
    try:
        state = {"latest_mp4_date": project_date_str}
        with open(AUTOMATION_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        log(f"   📊 State persisted: {project_date_str}")
    except Exception as e:
        log(f"   ⚠️ Could not update state: {e}")

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_runner_identity():
    """Return a dict with GitHub Action runner info or local defaults."""
    return {
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "repository": os.environ.get("GITHUB_REPOSITORY", "unknown-repo"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        "timestamp": datetime.now().isoformat()
    }


def mark_project_processing(project_dir: Path):
    """Create a status file to signal this project is being handled."""
    sources_dir = project_dir / "0.sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    status_file = sources_dir / STATUS_FILE_NAME
    
    identity = get_runner_identity()
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(identity, f, indent=2)
        log(f"🚩 Project marked as processing: {identity['run_id']} ({identity['repository']})")
    except Exception as e:
        log(f"⚠️ Could not create status file: {e}")


# ── Google Drive helpers (OAuth — same token.pickle as upload_folder_to_drive) ─

def get_credentials():
    if not GDRIVE_AVAILABLE:
        raise RuntimeError(
            "Google API client not installed.\n"
            "Run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as fh:
            try:
                creds = pickle.load(fh)
            except Exception:
                log("token.pickle unreadable — re-authenticating.")
                creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                log("Token refreshed silently.")
            except Exception as e:
                log(f"Token refresh failed ({e}) — re-authenticating.")
                creds = None

        if not creds:
            if not CREDS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at: {CREDS_FILE}\n"
                    "Download from Google Cloud Console:\n"
                    "  APIs & Services → Credentials → Create Credentials\n"
                    "  → OAuth client ID → Desktop app → Download JSON\n"
                    "Save as 'credentials.json' next to this script."
                )
            flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GDRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
            log("Google account authorised successfully.")

        with open(TOKEN_FILE, "wb") as fh:
            pickle.dump(creds, fh)

    return creds


def get_drive_service():
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)


def get_or_create_folder(service, folder_name: str, parent_id: str = None) -> str:
    """Return Drive folder ID, creating it if it doesn't exist."""
    query = (
        f"name = '{folder_name}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = service.files().create(body=meta, fields="id").execute()
    log(f"Created Drive folder: '{folder_name}'")
    return folder["id"]


def upload_to_gdrive(
    local_path: str,
    folder_name: str  = None,
    parent_folder_name: str = None,
    drive_filename: str = None,
    make_public: bool = True,
) -> str:
    """
    Upload a file to Google Drive using OAuth (token.pickle).

    folder_name         — destination folder on Drive (created if missing)
    parent_folder_name  — optional parent folder to put folder_name inside
    drive_filename      — filename on Drive (defaults to local filename)
    make_public         — share as anyone-with-link (default True)

    Returns the webViewLink URL.
    """
    drive_filename = drive_filename or Path(local_path).name
    log(f"☁️  Connecting to Google Drive...")
    service = get_drive_service()

    # Resolve folder chain: parent_folder_name / folder_name (splits A/B/C)
    parent_id = None
    if parent_folder_name:
        parent_id = get_or_create_folder(service, parent_folder_name)
    
    if folder_name:
        for part in folder_name.replace("\\", "/").split("/"):
            if part:
                parent_id = get_or_create_folder(service, part, parent_id)

    # Check for existing file to skip if found
    query = f"name = '{drive_filename}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    
    results = service.files().list(q=query, fields="files(id, webViewLink)").execute()
    existing = results.get("files", [])
    if existing:
        log(f"   ⏩ Skipping existing file: {drive_filename}")
        return existing[0].get("webViewLink")

    log(f"☁️  Uploading '{drive_filename}'...")
    meta  = {"name": drive_filename}
    if parent_id:
        meta["parents"] = [parent_id]

    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True)
    req   = service.files().create(body=meta, media_body=media, fields="id,webViewLink")

    response = None
    last_pct = -1
    while response is None:
        status, response = req.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct != last_pct:
                sys.stdout.write(f"\r   ☁️  Upload: {pct}%   ")
                sys.stdout.flush()
                last_pct = pct
    print("")

    file_id  = response.get("id")
    view_url = response.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")

    if make_public:
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
        log("File shared: anyone with the link can view.")

    log(f"✅ Upload complete → {view_url}")
    return view_url


def get_drive_folder_id(service, folder_name: str, parent_id: str = None) -> str:
    """Find a Drive folder by name."""
    query = (
        f"name = '{folder_name}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    return files[0]["id"] if files else None


def resolve_drive_project_id(service, project_name: str, parent_name: str = "2026-03") -> str:
    """Find a Drive project ID by traversing the PARENT/YEAR/MONTH structure or global search."""
    log(f"🔍 Resolving Drive ID for: {project_name} (parent: {parent_name})")
    
    # 1. Try global search first (fastest if unique)
    project_id = get_drive_folder_id(service, project_name)
    if project_id:
        # Verify it has a 0.sources folder to avoid false positives with same-name generic folders
        if get_drive_folder_id(service, "0.sources", project_id):
            return project_id
    
    # 2. Try structured path resolution if global search failed or was incomplete
    # project_name format: YYYY-MM-DD-project
    match = re.search(r"(\d{4})-(\d{2})-\d{2}-project", project_name)
    if match:
        year, month = match.group(1), match.group(2)
        log(f"   📂 Traversing path structure: {parent_name} -> {year} -> {month} -> {project_name}")
        
        parent_id = get_drive_folder_id(service, parent_name)
        if parent_id:
            year_id = get_drive_folder_id(service, year, parent_id)
            if year_id:
                month_id = get_drive_folder_id(service, month, year_id)
                if month_id:
                    project_id = get_drive_folder_id(service, project_name, month_id)
                    if project_id:
                        return project_id
    
    return None

def get_drive_file_id(service, file_name: str, parent_id: str = None) -> str:
    """Find a Drive file by name."""
    query = f"name = '{file_name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    return files[0]["id"] if files else None


def download_project_sources_from_drive(service, project_name: str, local_sources_dir: Path):
    """Download required sources from Drive project folder."""
    log(f"🔍 Searching Drive for project: {project_name}")
    project_id = resolve_drive_project_id(service, project_name)
    if not project_id:
        raise FileNotFoundError(f"Project folder '{project_name}' not found on Drive.")

    sources_id = get_drive_folder_id(service, "0.sources", project_id)
    if not sources_id:
        raise FileNotFoundError(f"'0.sources' folder not found in Drive project.")

    # 1. Download designated metadata files
    files_to_download = ["lyrics_with_prompts.md", "charactor.md", "cover.png"]
    for file_name in files_to_download:
        file_id = get_drive_file_id(service, file_name, sources_id)
        if not file_id:
            if file_name == "lyrics_with_prompts.md":
                raise FileNotFoundError(f"Required file '{file_name}' missing on Drive.")
            log(f"   ⏩ Skipping optional: {file_name}")
            continue

        local_path = local_sources_dir / file_name
        request = service.files().get_media(fileId=file_id)
        with io.FileIO(str(local_path), mode="wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    sys.stdout.write(f"\r   ☁️  Downloading {file_name}: {pct}%")
                    sys.stdout.flush()
            print(f"\n   ✅ {file_name} downloaded.")

    # 2. Automatically download any .mp3 files found in the folder
    query = f"'{sources_id}' in parents and name contains '.mp3' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    mp3_files = results.get('files', [])
    for mp3 in mp3_files:
        file_name = mp3['name']
        file_id   = mp3['id']
        local_path = local_sources_dir / file_name
        
        if local_path.exists():
            continue
            
        request = service.files().get_media(fileId=file_id)
        with io.FileIO(str(local_path), mode="wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    sys.stdout.write(f"\r   ☁️  Downloading {file_name}: {pct}%")
                    sys.stdout.flush()
            print(f"\n   ✅ {file_name} downloaded.")


def check_drive_project_needs_video(service, project_name: str) -> bool:
    """Check if project on Drive lacks an .mp4 in 0.sources and is not already processing."""
    log(f"🔍 Checking Drive completion for: {project_name}")
    project_id = resolve_drive_project_id(service, project_name)
    if not project_id:
        return False
    
    sources_id = get_drive_folder_id(service, "0.sources", project_id)
    if not sources_id:
        return True # Found project but no 0.sources -> needs setup/video
    
    # ── Check for processing status ──
    status_id = get_drive_file_id(service, STATUS_FILE_NAME, sources_id)
    if status_id:
        log(f"   ⏩ Project is already being processed (status file found).")
        return False

    # Check if any .mp4 exists in 0.sources
    query = f"'{sources_id}' in parents and name contains '.mp4' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])
    if not files:
        return True # 0.sources exists but no .mp4 -> needs video
    
    log(f"   ⏩ Project already completed on Drive: {files[0]['name']}")
    return False

# ─────────────────────────────────────────────────────────────────────────────


def save_config(config_data):
    try:
        # Convert pixels to percentages if they are integers > 1
        screen_w, screen_h = pyautogui.size()
        percent_config = {}
        for k, v in config_data.items():
            if k.endswith("_x") or k.endswith("x1") or k.endswith("x2"):
                percent_config[k] = v / screen_w if v > 1.0 else v
            elif k.endswith("_y") or k.endswith("y1") or k.endswith("y2"):
                percent_config[k] = v / screen_h if v > 1.0 else v
            else:
                percent_config[k] = v
        
        with open(CONFIG_FILE, "w") as f:
            json.dump(percent_config, f, indent=4)
        print(f"   ✅ UI Coordinates saved (as percentages) to {CONFIG_FILE.name}")
    except Exception as e:
        print(f"   ⚠️ Could not save config: {e}")

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def parse_veo_prompts(file_path):
    if not os.path.exists(file_path):
        log(f"Error: Prompt file not found: {file_path}")
        return []
    with open(file_path, "r", encoding="utf-8-sig") as f:
        content = f.read()
    prompts = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith(">"):
            match = re.search(r"^>\s*(\d{2}:\d{2}(?:\.\d{2})?)-(\d{2}:\d{2}(?:\.\d{2})?)\s*(.*)", line)
            if match:
                prompt_text = re.sub(r"^\[.*?\]\s*", "", match.group(3).strip()).strip()
                if prompt_text:
                    prompts.append({"start": match.group(1).strip(), "end": match.group(2).strip(), "text": prompt_text})
            else:
                match_single = re.search(r"^>\s*(\d{2}:\d{2}(?:\.\d{2})?)(?:-)?\s*(.*)", line)
                if match_single:
                    prompt_text = re.sub(r"^\[.*?\]\s*", "", match_single.group(2).strip()).strip()
                    if prompt_text:
                        prompts.append({"start": match_single.group(1).strip(), "end": None, "text": prompt_text})
                else:
                    prompt_text = re.sub(r"^>\s*", "", line).strip()
                    if prompt_text:
                        prompts.append({"start": "00:00.00", "end": None, "text": prompt_text})
    return prompts

def time_to_sec(t_str):
    if "." not in t_str:
        t_str += ".00"
    m, s   = t_str.split(":")
    sec, ms = s.split(".")
    return int(m) * 60 + int(sec) + int(ms) / 100.0

def wait_for_visual_begin(monitor):
    log("🔍 Monitoring screen for session initialization...")
    with mss.mss() as sct:
        time.sleep(1.0)
        base_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
        while True:
            time.sleep(1.0)
            curr_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
            if np.mean(cv2.absdiff(base_gray, curr_gray)) > 4.0:
                log("⏳ State change detected! Waiting for live state...")
                time.sleep(3.0)
                log("▶️ Live session active. Recording started.")
                return True

def wait_for_visual_end(monitor, max_total_wait=270, session_start_time=0):
    log("\n🏁 Monitoring for end state...")
    with mss.mss() as sct:
        static_start  = time.time()
        overall_start = time.time()
        top_h         = max(int(monitor["height"] * 0.15), 10)

        def get_top_gray():
            return cv2.cvtColor(np.array(sct.grab(monitor))[:top_h, :, :3], cv2.COLOR_BGR2GRAY)

        last_gray = get_top_gray()
        while True:
            time.sleep(1.0)
            curr_gray   = get_top_gray()
            mean_diff   = np.mean(cv2.absdiff(last_gray, curr_gray))
            last_gray   = curr_gray
            session_rem = max(0, 270 - (time.time() - session_start_time))
            sys.stdout.write(f"\r   ⏱️ Session left: {int(session_rem//60)}:{int(session_rem%60):02d} | Motion: {mean_diff:.2f}  ")
            sys.stdout.flush()
            if session_rem < 10.0:
                print(""); log("⏹️ Session < 10s — ending recording."); return True
            if mean_diff > 1.5:
                static_start = time.time()
            elif time.time() - static_start >= 5.0:
                print(""); log("⏹️ Static for 5s — session ended."); return True
            if time.time() - overall_start > max_total_wait:
                print(""); log("⏹️ Max wait reached."); return True

def record_screen(monitor, output_filename, fps, stop_event, is_recording, stats):
    log(f"🎬 Recorder ready: {monitor['width']}x{monitor['height']} @ {fps}fps")
    try:
        with mss.mss() as sct:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            out    = cv2.VideoWriter(output_filename, fourcc, fps, (monitor["width"], monitor["height"]))
            step   = 1.0 / fps
            while not stop_event.is_set():
                t0 = time.time()
                if is_recording.is_set():
                    out.write(np.array(sct.grab(monitor))[:, :, :3])
                    stats["total_frames"] += 1
                sleep_t = step - (time.time() - t0)
                if sleep_t > 0:
                    time.sleep(sleep_t)
            out.release()
            log("🛑 Recorder stopped.")
    except Exception as e:
        log(f"❌ Recorder error: {e}")

def get_project_dir(service=None):
    start_date_str = None
    if AUTOMATION_STATE_FILE.exists():
        try:
            with open(AUTOMATION_STATE_FILE, "r") as f:
                state = json.load(f)
                start_date_str = state.get("latest_mp4_date")
        except Exception:
            pass

    start_date = (datetime.strptime(start_date_str, "%Y-%m-%d")
                  if start_date_str else datetime.now() - timedelta(days=7))

    # Boundary for "today"
    now = datetime.now()
    if   now.hour >= 20: today_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    elif now.hour >   6: today_str = now.strftime("%Y-%m-%d")
    else:                today_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")

    log(f"📂 Searching for next project starting from: {start_date.strftime('%Y-%m-%d')}")
    
    # ── 1. Check Drive first (if service is available) ──
    if service:
        log("🔍 Checking Google Drive for pending projects (priority)...")
        for i in range(0, 120):
            current_date = start_date + timedelta(days=i)
            date_str     = current_date.strftime("%Y-%m-%d")
            project_name = f"{date_str}-project"
            if check_drive_project_needs_video(service, project_name):
                log(f"✨ Found project needing video (on Drive): {date_str}")
                # Return the expected local path (dated folder structure)
                return root_dir / current_date.strftime("%Y") / current_date.strftime("%m") / project_name
    
    # ── 2. Fallback to Local history search ──
    log("🔍 Falling back to local history search...")
    for i in range(0, 120):
        current_date = start_date + timedelta(days=i)
        date_str     = current_date.strftime("%Y-%m-%d")
        project_name = f"{date_str}-project"
        test_dir     = root_dir / current_date.strftime("%Y") / current_date.strftime("%m") / project_name
        
        # ── Check Local status ──
        local_done = False
        sources_dir = test_dir / "0.sources"
        if sources_dir.exists() and list(sources_dir.glob("*.mp4")):
            local_done = True
        
        if local_done:
            continue # Try next date

        if (sources_dir / STATUS_FILE_NAME).exists():
            log(f"   ⏩ Skipping {date_str} (locally marked as processing).")
            continue

        if test_dir.exists():
            log(f"✨ Found local project needing video: {date_str}")
            return test_dir

    # Fallback to current project if nothing found in history
    project_dir = root_dir / today_dt.strftime("%Y") / today_dt.strftime("%m") / f"{today_str}-project"
    return project_dir


def main():
    parser = argparse.ArgumentParser(description="Pixverse AI Automated Capture → Google Drive")
    parser.add_argument("--project",      "-p", type=str)
    parser.add_argument("--reset",              action="store_true")
    parser.add_argument("--wait",         "-w", type=float, default=None)
    parser.add_argument("--between",      "-b", type=float, default=3.0)
    parser.add_argument("--start-delay",        type=float, default=10.0)
    parser.add_argument("--cut-start",          type=float, default=6.0)
    parser.add_argument("--cut-end",            type=float, default=6.0)
    parser.add_argument("--yes",          "-y", action="store_true")
    # ── Action Modes ──────────────────────────────────────────────────────────
    parser.add_argument("--upload",       "-u", action="store_true",
                        help="Upload-only mode: skip capture and transcode, just upload latest final video.")
    parser.add_argument("--convert",      "-c", action="store_true",
                        help="Convert-only mode: skip capture, transcode existing raw video.")
    parser.add_argument("--input-raw",    "-i", type=str,
                        help="Specify raw video file to convert (default: latest in 7.downloads).")
    # ── Google Drive ──────────────────────────────────────────────────────────
    parser.add_argument("--no-gdrive",          action="store_true",
                        help="Disable Google Drive upload after transcoding.")
    parser.add_argument("--gdrive-folder",      type=str, default=None,
                        help="Drive folder name to upload into (created if missing).")
    parser.add_argument("--gdrive-parent",      type=str, default=None,
                        help="Parent folder name on Drive (optional, e.g. '2026-03').")
    parser.add_argument("--gdrive-name",        type=str, default=None,
                        help="Custom filename on Drive.")
    parser.add_argument("--public",             action="store_true", default=True,
                        help="Share uploaded file publicly (default: on).")
    parser.add_argument("--no-local",           action="store_true",
                        help="Delete local file after successful Drive upload.")
    # ─────────────────────────────────────────────────────────────────────────
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print(" 🚀 Pixverse AI Automated Capture → Google Drive")
    print("=" * 60 + "\n")

    # ── 1. UI geometry ───────────────────────────────────────────────────────
    config = load_config()
    screen_w, screen_h = pyautogui.size()
    
    if args.reset or not config:
        log("🎬 Resetting UI configuration. Follow the instructions:")
        try:
            # Give the user a moment
            time.sleep(1)
            
            input("🎯 1. Move mouse to TOP-LEFT of video area and press ENTER...")
            x_abs1, y_abs1 = pyautogui.position()
            log(f"   ✅ Recorded Top-Left: ({x_abs1}, {y_abs1})\n")

            input("🎯 2. Move mouse to BOTTOM-RIGHT of video area and press ENTER...")
            x_abs2, y_abs2 = pyautogui.position()
            log(f"   ✅ Recorded Bottom-Right: ({x_abs2}, {y_abs2})\n")

            input("🎯 3. Move mouse to PROMPT TEXT INPUT area and press ENTER...")
            text_x_abs, text_y_abs = pyautogui.position()
            log(f"   ✅ Recorded Text Input: ({text_x_abs}, {text_y_abs})\n")

            config = {
                "vid_x1": x_abs1 / screen_w, "vid_y1": y_abs1 / screen_h,
                "vid_x2": x_abs2 / screen_w, "vid_y2": y_abs2 / screen_h,
                "text_x": text_x_abs / screen_w, "text_y": text_y_abs / screen_h
            }
            save_config(config)
            log("✅ UI Coordinates saved (as percentages).")
        except KeyboardInterrupt:
            log("\n❌ Reset cancelled."); return
    
    # Always convert percentages to current screen pixels
    x1 = int(config["vid_x1"] * screen_w) if config["vid_x1"] <= 1.0 else int(config["vid_x1"])
    y1 = int(config["vid_y1"] * screen_h) if config["vid_y1"] <= 1.0 else int(config["vid_y1"])
    x2 = int(config["vid_x2"] * screen_w) if config["vid_x2"] <= 1.0 else int(config["vid_x2"])
    y2 = int(config["vid_y2"] * screen_h) if config["vid_y2"] <= 1.0 else int(config["vid_y2"])
    text_x = int(config["text_x"] * screen_w) if config["text_x"] <= 1.0 else int(config["text_x"])
    text_y = int(config["text_y"] * screen_h) if config["text_y"] <= 1.0 else int(config["text_y"])

    w, h = abs(x2 - x1), abs(y2 - y1)
    if w % 2: w -= 1
    if h % 2: h -= 1
    monitor = {"top": min(y1, y2), "left": min(x1, x2), "width": w, "height": h}
    log(f"🎯 Capture region: {w}x{h} at ({monitor['left']}, {monitor['top']})")

    # ── 2. Locate project & prompts ──────────────────────────────────────────
    service = None
    if not args.project:
        # Auto-infer project date if a raw video path is provided
        if args.input_raw:
            match = re.search(r"(\d{4}-\d{2}-\d{2}-project)", str(args.input_raw))
            if match:
                args.project = match.group(1)
                log(f"📋 Inferred project from input-raw: {args.project}")

    if not args.project:
        try:
            service = get_drive_service()
        except Exception as e:
            log(f"⚠️ Could not init Drive service for search: {e}")
        project_dir = get_project_dir(service)
    else:
        project_dir = Path(args.project)
        # Attempt to resolve YYYY-MM-DD-project to full path if not absolute
        if not project_dir.is_absolute():
            # Priority 1: Check projects/ subfolder
            test_dir = root_dir / "projects" / args.project
            if test_dir.exists():
                project_dir = test_dir
            else:
                # Priority 2: Check dated structure
                parts = str(args.project).split("-")
                if len(parts) >= 2 and parts[0].isdigit():
                    # root_dir / YEAR / MONTH / project
                    test_dir = root_dir / parts[0] / parts[1] / args.project
                    if test_dir.exists():
                        project_dir = test_dir
                
                # Priority 3: Fallback to CWD
                if not project_dir.exists():
                    project_dir = Path(os.getcwd()) / args.project

    if project_dir:
        sources_dir = project_dir / "0.sources"
        prompts_file = sources_dir / "lyrics_with_prompts.md"
        
        if not prompts_file.exists():
            log(f"📂 Prompts file missing for '{project_dir.name}'. Fetching sources from Drive...")
            try:
                if not service:
                    service = get_drive_service()
                sources_dir.mkdir(parents=True, exist_ok=True)
                download_project_sources_from_drive(service, project_dir.name, sources_dir)
            except Exception as e:
                log(f"⚠️ Failed to fetch sources from Drive for {project_dir.name}: {e}")
                # Don't return, let it try to parse prompts later and fail naturally if still missing
        else:
            log(f"✅ Found local prompts for {project_dir.name}")

    log(f"📂 Using Project: {project_dir.name}")
    
    # ── 2.5. Mark as processing to prevent other runners from starting ──
    mark_project_processing(project_dir)

    prompts = parse_veo_prompts(project_dir / "0.sources" / "lyrics_with_prompts.md")
    if not prompts:
        log("❌ No prompts found."); return
    log(f"✨ Loaded {len(prompts)} prompts.\n")

    # ── 3. File paths ────────────────────────────────────────────────────────
    downloads_dir = project_dir / "0.sources" / "7.downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_video = str(downloads_dir / f"realtime_pixverse_raw_{ts}.mp4")
    out_video = str(project_dir / "0.sources" / f"pixverse_{ts}.mp4")

    # ── 3.5. Determine Action ────────────────────────────────────────────────
    do_capture   = not args.upload and not args.convert
    do_transcode = not args.upload
    do_cleanup   = not args.upload and not args.convert
    total_frames = 0

    if do_capture:
        char_file = project_dir / "0.sources" / "charactor.md"
        cover_img = project_dir / "0.sources" / "cover.png"
        
        # Use semi-tolerant threshold and multi-scale for Linux compatibility
        # On Linux, Windows-captured templates often need ~0.7 scale
        finder = ScreenTemplateFinder(confidence_threshold=0.6)
        search_scales = [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]

        pyautogui.click(x1, y1); time.sleep(3)
        # pyautogui.scroll(-5000); time.sleep(1)

        pyautogui.hotkey('ctrl', 'l')
        time.sleep(1)   
        pyautogui.write("https://realtime.pixverse.ai/generate/")
        pyautogui.press('enter')
        time.sleep(3)

        # for tpl in ["create_world.png", "scrolling.png"]:
        #     t = str(autogui_dir / "pixverse_ai" / "realtime" / tpl)
        #     if not finder.wait_and_click_template(t, timeout=10, times=3, scales=search_scales):
        #         log(f"⚠️ Could not find {tpl}. Continuing...")
        #     else:
        #         time.sleep(2)

        log("Typing character text...")
        text = char_file.read_text(encoding="utf-8")[:300] if char_file.exists() else "Character reference"
        t = str(autogui_dir / "pixverse_ai" / "realtime" / "prompt_input.png")
        finder.wait_and_click_template(t, timeout=5, scales=search_scales)
        pyautogui.write(text, interval=0.01)
        time.sleep(1)

        if cover_img.exists():
            log(f"Uploading cover image: {cover_img.name}")
            t = str(autogui_dir / "pixverse_ai" / "realtime" / "image_reference.png")
            if finder.wait_and_click_template(t, timeout=10, scales=search_scales):
                time.sleep(1.5)
                pyautogui.write(os.path.abspath(cover_img))
                time.sleep(1.5)
                pyautogui.press("enter")
                time.sleep(1.5)
                t = str(autogui_dir / "pixverse_ai" / "realtime" / "open_file.png")
                if finder.wait_and_click_template(t, timeout=10, scales=search_scales):
                    time.sleep(1.5) 
                else:
                    log("⚠️ Could not find open_file.png. Continuing...")

                log("Cover image submitted. Waiting 5s...")
                time.sleep(8)

        for tpl in ["submit.png"]:
            t = str(autogui_dir / "pixverse_ai" / "realtime" / tpl)
            if not finder.wait_and_click_template(t, timeout=10, times=3, scales=search_scales):
                log(f"⚠️ Could not find {tpl}. Continuing...")
            else:
                time.sleep(2)

        time.sleep(6)

        # t = str(autogui_dir / "pixverse_ai" / "realtime" / "no_avatar.png")
        # if not finder.wait_and_click_template(t, timeout=10, times=3, scales=search_scales):
        #     log("⚠️ Could not find no_avatar.png. Continuing...")
        # else:
        #     time.sleep(2)

        # t = str(autogui_dir / "pixverse_ai" / "realtime" / "no_avatar_confirm.png")
        # if not finder.wait_and_click_template(t, timeout=10, times=3, scales=search_scales):
        #     log("⚠️ Could not find no_avatar_confirm.png. Continuing...")
        # else:
        #     time.sleep(2)

        # ── 4. Recording thread ───────────────────────────────────────────────
        stop_event   = threading.Event()
        is_recording = threading.Event()
        stats        = {"total_frames": 0}
        recorder_thread = threading.Thread(
            target=record_screen,
            args=(monitor, raw_video, 30, stop_event, is_recording, stats)
        )
        recorder_thread.start()
        time.sleep(1.0)

        # ── 5. Automation loop ────────────────────────────────────────────────
        try:
            wait_for_visual_begin(monitor)
            is_recording.set()

            log(f"📋 Session active. Waiting {args.start_delay}s before first prompt...")
            t0 = time.time()
            while time.time() - t0 < args.start_delay:
                sys.stdout.write(f"\r   ⏱️ Starting in: {max(0, args.start_delay-(time.time()-t0)):.1f}s... ")
                sys.stdout.flush(); time.sleep(0.1)
            print("")

            session_start_time = time.time()

            for i, p in enumerate(prompts):
                log(f"\n=> Prompt [{i+1}/{len(prompts)}]: {p['text'][:60]}...")
                pyautogui.click(text_x, text_y); time.sleep(0.2)
                pyautogui.hotkey("ctrl", "a"); pyautogui.press("delete")
                pyautogui.write(p["text"], interval=0.01); pyautogui.press("enter")

                duration = 3.0
                if args.wait is not None:
                    duration = args.wait
                elif p["end"]:
                    duration = time_to_sec(p["end"]) - time_to_sec(p["start"])
                elif i < len(prompts) - 1:
                    duration = time_to_sec(prompts[i+1]["start"]) - time_to_sec(p["start"])
                duration = max(duration, 3.0)

                t0 = time.time()
                while time.time() - t0 < duration:
                    rem         = max(0, duration - (time.time() - t0))
                    session_rem = max(0, 270 - (time.time() - session_start_time))
                    sys.stdout.write(f"\r   ⏱️ {rem:.1f}s | Session: {int(session_rem//60)}:{int(session_rem%60):02d}   ")
                    sys.stdout.flush()
                    if session_rem < 10.0: break
                    if os.path.exists(STOP_SIGNAL_FILE):
                        log("🛑 Stop signal detected in wait loop.")
                        break
                    time.sleep(0.2)

                print("")

                if i < len(prompts) - 1:
                    time.sleep(args.between)
                if max(0, 270 - (time.time() - session_start_time)) < 10.0:
                    log("⏹️ Session < 10s. Stopping."); break
                if os.path.exists(STOP_SIGNAL_FILE):
                    log("🛑 Stop signal detected. Exiting prompt loop.")
                    try: os.remove(STOP_SIGNAL_FILE)
                    except: pass
                    break


            wait_for_visual_end(monitor, session_start_time=session_start_time)
            is_recording.clear()

        except KeyboardInterrupt:
            log("\n⚠️ Interrupted.")
        finally:
            log("\n🔌 Stopping recorder...")
            stop_event.set()
            recorder_thread.join()
            total_frames = stats.get("total_frames", 0)

    if do_transcode:
        import subprocess
        gdrive_url = None

        if args.convert:
            if args.input_raw:
                raw_video = args.input_raw
            else:
                raw_videos = sorted(list(downloads_dir.glob("realtime_pixverse_raw_*.mp4")), key=os.path.getmtime, reverse=True)
                if not raw_videos:
                    log("❌ No raw videos found in 7.downloads to convert."); return
                raw_video = str(raw_videos[0])
                log(f"📋 Convert-only: Using latest raw video: {Path(raw_video).name}")
            
            # Get frame count from file for progress bar
            try:
                cap = cv2.VideoCapture(raw_video)
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                cap.release()
                if total_frames <= 0:
                    log(f"⚠️ Could not read frame count from {raw_video}. Progress bar may be inaccurate.")
                    total_frames = 100
            except Exception as e:
                log(f"⚠️ Error reading video info: {e}")
                total_frames = 100

        try:
            if total_frames > 0 and os.path.exists(raw_video):
                    target_duration = max(1.0, total_frames / 30.0 - args.cut_start - args.cut_end)

                    # ── Audio ────────────────────────────────────────────────
                    log("🎵 Preparing audio...")
                    sources_dir  = project_dir / "0.sources"

                    # 🔊 Ensure audio files exist locally before transcoding
                    if not list(sources_dir.glob("*.mp3")):
                        log("   ⚠️ Audio files (.mp3) missing locally. Attempting to fetch from Drive...")
                        try:
                            if not service: service = get_drive_service()
                            download_project_sources_from_drive(service, project_dir.name, sources_dir)
                        except Exception as e:
                            log(f"   ⚠️ Could not download audio from Drive: {e}")

                    mp3_0        = sources_dir / "part_000.mp3"
                    mp3_9        = sources_dir / "part_000 (9).mp3"
                    combined_mp3 = sources_dir / f"{project_dir.name}_combined.mp3"

                    if combined_mp3.exists():
                        log(f"   🔹 Using existing combined audio: {combined_mp3.name}")
                    elif mp3_0.exists() and mp3_9.exists():
                        log("   ➕ Combining part_000.mp3 + part_000 (9).mp3...")
                        subprocess.run([
                            "ffmpeg", "-y", "-i", str(mp3_0), "-i", str(mp3_9),
                            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                            "-map", "[a]", str(combined_mp3)
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        # Fallback: look for ANY mp3 if the standard naming fails
                        audio_files  = [f for f in sources_dir.glob("*.mp3") if not f.name.endswith("_combined.mp3")]
                        combined_mp3 = audio_files[0] if audio_files else None
                        if combined_mp3:
                            log(f"   🔹 Using source audio: {combined_mp3.name}")
                        else:
                            log("   ⚠️ No audio files found. Video will be mute.")

                    # ── Transcode ────────────────────────────────────────────
                    log(f"⏳ Transcoding {total_frames} frames → {target_duration:.1f}s...")
                    
                    # Pixverse watermark removal: Top-Right and Bottom-Right
                    w_wm, h_wm = 200, 70
                    margin = 10
                    
                    # 1. Top-Right
                    tr_x = monitor["width"] - w_wm - margin
                    tr_y = margin
                    
                    # 2. Bottom-Right
                    br_x = monitor["width"] - w_wm - margin
                    br_y = monitor["height"] - h_wm - margin
                    
                    delogo_parts = []
                    if tr_x >= 0 and tr_y >= 0:
                        delogo_parts.append(f"delogo=x={tr_x}:y={tr_y}:w={w_wm}:h={h_wm}")
                    if br_x >= 0 and br_y >= 0:
                        delogo_parts.append(f"delogo=x={br_x}:y={br_y}:w={w_wm}:h={h_wm}")
                        
                    if delogo_parts:
                        delogo_filter = ",".join(delogo_parts)
                        tr_info = f"TR: {tr_x},{tr_y}" if tr_x >= 0 else ""
                        br_info = f"BR: {br_x},{br_y}" if br_x >= 0 else ""
                        log(f"   🛡️ Applying watermark removal ({tr_info} | {br_info})")
                    else:
                        delogo_filter = "null" # no-op filter
                        log("   🛡️ Skipping watermark removal (capture frame too small)")

                    if combined_mp3:
                        cmd = [
                            "ffmpeg", "-y", "-ss", str(args.cut_start), "-i", raw_video,
                            "-i", str(combined_mp3),
                            "-filter_complex",
                            f"[0:v]{delogo_filter}[v];"
                            f"[1:a]afade=t=out:st={round(target_duration-5, 3)}:d=5[a]",
                            "-map", "[v]", "-map", "[a]",
                            "-t", str(round(target_duration, 3)),
                            "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-crf", "18", "-preset", "fast",
                            "-c:a", "aac", "-b:a", "192k",
                            out_video,
                        ]
                    else:
                        cmd = [
                            "ffmpeg", "-y", "-ss", str(args.cut_start), "-i", raw_video,
                            "-vf", delogo_filter,
                            "-t", str(round(target_duration, 3)),
                            "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-crf", "18", "-preset", "fast",
                            out_video,
                        ]

                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    for line in proc.stdout:
                        m = re.search(r"frame=\s*(\d+)", line)
                        if m:
                            pct = min(100, int(int(m.group(1)) / total_frames * 100))
                            sys.stdout.write(f"\r   🔄 Transcoding {pct}%   "); sys.stdout.flush()
                    proc.wait(); print("")

                    if proc.returncode == 0:
                        log("✅ Transcoding complete.")
                        try:
                            update_automation_state(project_dir.name.split("-project")[0])
                        except Exception: pass
                    else:
                        log(f"⚠️ Transcode failed (code {proc.returncode})")
            else:
                log(f"❌ Input raw video not found or invalid: {raw_video}")
        except Exception as e:
            log(f"⚠️ Post-processing error: {e}")

        print("\n" + "=" * 60)
        if os.path.exists(out_video):
            print(f" 📁 Local : {out_video}")
        print("=" * 60 + "\n")

    else:
        # Upload-only mode: find the latest existing video if none recently made
        if not os.path.exists(out_video):
            videos = sorted(list(project_dir.glob("0.sources/pixverse_*.mp4")), key=os.path.getmtime, reverse=True)
            if videos:
                out_video = str(videos[0])
                log(f"📋 Upload-only: Found latest video: {Path(out_video).name}")

    # ── 6. Final Upload Logic (Runs in both modes) ───────────────────────────
    if (os.path.exists(out_video) or downloads_dir.exists()) and not getattr(args, "no_gdrive", False):
        log("☁️  Starting Google Drive upload sequence...")
        
        # Auto-derive folder/parent if not specified
        parent = args.gdrive_parent or "2026-03"
        
        # Subfolder structure: year / month / project
        try:
            year       = project_dir.parent.parent.name
            month      = project_dir.parent.name
            project    = project_dir.name
            folder_path = f"{year}/{month}/{project}/0.sources"
        except Exception:
            folder_path = f"{project_dir.name}/0.sources"

        gdrive_url = None
        try:
            # ── 6a. Upload Final Video ──
            if os.path.exists(out_video):
                drive_name = args.gdrive_name or Path(out_video).name
                gdrive_url = upload_to_gdrive(
                    local_path         = out_video,
                    folder_name        = folder_path,
                    parent_folder_name = parent,
                    drive_filename     = drive_name,
                    make_public        = args.public,
                )
            
            # ── 6b. Upload 7.downloads folder contents ──
            if downloads_dir.exists():
                log(f"📂 Syncing contents of {downloads_dir.name} to Drive...")
                service = get_drive_service()
                
                # Resolve the project folder ID by following the chain
                curr_pid = get_or_create_folder(service, parent) # 2026-03
                for part in folder_path.split("/"):
                    if part:
                        curr_pid = get_or_create_folder(service, part, curr_pid)
                
                # Get or create '7.downloads' inside that project folder
                downloads_pid = get_or_create_folder(service, "7.downloads", curr_pid)
                
                # List existing files in Drive downloads folder to avoid re-uploading
                query = f"'{downloads_pid}' in parents and trashed = false"
                results = service.files().list(q=query, fields="files(name)").execute()
                drive_files = {f['name'] for f in results.get('files', [])}

                for f in sorted(downloads_dir.glob("*.mp4")):
                    if f.name in drive_files:
                        sys.stdout.write(f"\r   ⏩ Skipping existing raw: {f.name}   "); sys.stdout.flush()
                        continue
                    
                    sys.stdout.write(f"\r   ☁️  Uploading raw: {f.name}...           "); sys.stdout.flush()
                    upload_to_gdrive(
                        local_path         = str(f),
                        folder_name        = f"{folder_path}/7.downloads",
                        parent_folder_name = parent,
                        drive_filename     = f.name,
                        make_public        = args.public
                    )
                print("")

            if args.no_local and gdrive_url:
                os.remove(out_video)
                log("🗑️  Local copy deleted (--no-local).")
        except Exception as e:
            log(f"⚠️  Drive upload failed: {e}")
    elif not getattr(args, "no_gdrive", False):
        log("ℹ️  No files found to upload.")
    else:
        log("ℹ️  Google Drive upload skipped (--no-gdrive).")

    # ── 7. Cleanup & Browser Reset (Only if we did a capture) ────────────────
    if do_cleanup:
        try:
            # Re-define scales for cleanup if necessary
            cleanup_scales = [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
            finder = ScreenTemplateFinder(confidence_threshold=0.6)
            for tpl in ["no_publish.png", "no_publish.png"]:
                t = str(autogui_dir / "pixverse_ai" / "realtime" / tpl)
                if finder.wait_and_click_template(t, timeout=3, scales=cleanup_scales):
                    time.sleep(2)

            pyautogui.click(x1, y1); time.sleep(3)
            # pyautogui.scroll(-5000); time.sleep(1)

            t = str(autogui_dir / "pixverse_ai" / "realtime" / "back_to_button.png")
            if finder.wait_and_click_template(t, timeout=10, scales=cleanup_scales):
                time.sleep(2)
            pyautogui.scroll(-5000); time.sleep(1)

            t = str(autogui_dir / "pixverse_ai" / "realtime" / "back_to_button.png")
            if finder.wait_and_click_template(t, timeout=10, scales=search_scales):
                time.sleep(2)
        except Exception:
            pass

    print("\n" + "=" * 60)
    print(" ✅ Operation Complete.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log(f"❌ Runtime failure: {e}")
        traceback.print_exc()