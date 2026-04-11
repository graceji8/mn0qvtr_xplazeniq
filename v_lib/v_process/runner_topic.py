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
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# Add lib to path
script_dir = Path(__file__).parent.absolute()
v_lib_dir = script_dir.parent
if str(v_lib_dir) not in sys.path:
    sys.path.append(str(v_lib_dir))

from template_finder import ScreenTemplateFinder

# Global constants
AUTOMATION_STATE_FILE = script_dir / "automation_state.json"
TEMPLATES_DIR         = script_dir / "templates"
STATUS_FILE_NAME      = "processing_status.json"
STOP_SIGNAL_FILE      = "/tmp/stop_automation"

# ── App URL (Secretized) ──
APP_URL = os.environ.get("V_URL", "https://example.com/")

# ── Google Drive helpers ──
try:
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    import io
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive.metadata.readonly"]

# ─────────────────────────────────────────────────────────────────────────────

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass

root_dir    = script_dir.parent.parent
CONFIG_FILE = script_dir / "ui_config.json"
BROWSER_SESSION_DIR = root_dir / ".browser-session"

def ensure_chrome_running():
    """Ensure Chrome is running, clearing stale locks if necessary."""
    log("🔧 Checking if Chrome is running...")
    
    # 1. Clear stale locks
    lock_file = BROWSER_SESSION_DIR / "SingletonLock"
    if lock_file.exists():
        log(f"   🧹 Found stale lock at {lock_file}, removing...")
        try:
            lock_file.unlink(missing_ok=True)
            (BROWSER_SESSION_DIR / "SingletonCookie").unlink(missing_ok=True)
        except Exception as e:
            log(f"   ⚠️ Could not remove lock: {e}")

    # 2. Check if running (crude check via remote debugging port)
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    result = sock.connect_ex(('127.0.0.1', 9222))
    sock.close()
    
    if result == 0:
        log("   ✅ Chrome is already running (debug port 9222 active).")
        return True
        
    log("   🚀 Chrome not detected. Attempting to start...")
    try:
        chrome_cmd = [
            "google-chrome",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--remote-debugging-port=9222",
            "--disable-session-crashed-bubble",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--window-size=2000,1550",
            "--window-position=0,0",
            f"--user-data-dir={BROWSER_SESSION_DIR}",
            APP_URL
        ]
        # Run in background
        subprocess.Popen(chrome_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("   ⏳ Waiting for Chrome to initialize...")
        time.sleep(8)
        return True
    except Exception as e:
        log(f"   ❌ Failed to start Chrome: {e}")
        return False

def find_drive_file(filename):
    search_dirs = [script_dir, Path.cwd(), root_dir]
    for d in search_dirs:
        p = d / filename
        if p.exists(): return p
    return script_dir / filename 

CREDS_FILE = find_drive_file("credentials.json")
TOKEN_FILE = find_drive_file("token.pickle")
# ─────────────────────────────────────────────────────────────────────────────

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def update_automation_state(project_date_str):
    log(f"   📊 Skipping state update for {AUTOMATION_STATE_FILE.name}")
    pass
    # try:
    #     state = {"latest_mp4_date": project_date_str}
    #     with open(AUTOMATION_STATE_FILE, "w") as f:
    #         json.dump(state, f, indent=2)
    #     log(f"   📊 State persisted: {project_date_str}")
    # except Exception as e:
    #     log(f"   ⚠️ Could not update state: {e}")

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def save_debug_screenshot(name="debug"):
    try:
        import mss
        import time
        log_dir = root_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        filename_str = f"{name}_{int(time.time())}.png"
        filepath = log_dir / filename_str
        with mss.mss() as sct:
            sct.shot(mon=-1, output=str(filepath))
        log(f"📸 Saved debug screenshot locally to logs/{filename_str}")
        
        try:
            # For logs, we want them in a 'DebugLogs' folder inside the root 'news' folder
            url = upload_to_gdrive(
                local_path=str(filepath),
                folder_name="DebugLogs",
                parent_folder_name=None, # folder_name already starts with news if we want it there
                drive_filename=filename_str,
                make_public=True
            )
            
            file_id = ""
            if '/d/' in url: file_id = url.split('/d/')[1].split('/')[0]
            elif 'id=' in url: file_id = url.split('id=')[1].split('&')[0]
            
        except Exception as eu:
            log(f"   ⚠️ Could not upload screenshot to Drive: {eu}")
            
    except Exception as e:
        log(f"⚠️ Could not save screenshot: {e}")


def get_runner_identity():
    return {
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "repository": os.environ.get("GITHUB_REPOSITORY", "unknown-repo"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        "timestamp": datetime.now().isoformat()
    }


def get_sources_dir(project_dir: Path) -> Path:
    """Return the sources directory. Prefer 0.sources if it exists, otherwise use project root."""
    sources_dir = project_dir / "0.sources"
    if sources_dir.exists() and sources_dir.is_dir():
        return sources_dir
    # For news projects, files are directly in the project folder
    return project_dir

def mark_project_processing(project_dir: Path):
    sources_dir = get_sources_dir(project_dir)
    # Ensure directory exists (though it should for local projects)
    if not sources_dir.exists():
        sources_dir.mkdir(parents=True, exist_ok=True)
        
    status_file = sources_dir / STATUS_FILE_NAME
    identity = get_runner_identity()
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(identity, f, indent=2)
        log(f"🚩 Project marked as processing: {identity['run_id']}")
    except Exception as e:
        log(f"⚠️ Could not create status file: {e}")



def get_credentials():
    if not GDRIVE_AVAILABLE: raise RuntimeError("Google API client not installed.")
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as fh:
            try: creds = pickle.load(fh)
            except Exception: creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try: creds.refresh(Request())
            except Exception: creds = None
        if not creds:
            if not CREDS_FILE.exists(): raise FileNotFoundError(f"credentials.json not found.")
            flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GDRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as fh: pickle.dump(creds, fh)
    return creds


def get_drive_service():
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)


def get_drive_full_path(service, file_id: str) -> str:
    path = []
    curr = file_id
    try:
        while curr:
            meta = service.files().get(fileId=curr, fields="name, parents").execute()
            path.append(meta.get("name", ""))
            parents = meta.get("parents")
            curr = parents[0] if parents else None
    except Exception:
        pass
    return "/" + "/".join(reversed(path)) if path else ""

def get_or_create_folder(service, folder_name: str, parent_id: str = None, root_only: bool = False) -> str:
    safe_folder_name = folder_name.replace("'", "\\'")
    query = f"name = '{safe_folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id: 
        query += f" and '{parent_id}' in parents"
    elif root_only:
        query += " and 'root' in parents"
        
    results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    if files: return files[0]["id"]
    
    meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id: 
        meta["parents"] = [parent_id]
    elif root_only:
        meta["parents"] = ["root"]
        
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def upload_to_gdrive(local_path: str, folder_name: str  = None, parent_folder_name: str = None, drive_filename: str = None, make_public: bool = True) -> str:
    drive_filename = drive_filename or Path(local_path).name
    service = get_drive_service()
    parent_id = None
    if parent_folder_name: 
        parent_id = get_or_create_folder(service, parent_folder_name, root_only=True)
    if folder_name:
        parts = folder_name.replace("\\", "/").split("/")
        for i, part in enumerate(parts):
            if part: 
                # If first part is 'news', ensure it's at the root
                is_root_news = (i == 0 and part == "news")
                parent_id = get_or_create_folder(service, part, parent_id, root_only=is_root_news)

    safe_drive_filename = drive_filename.replace("'", "\\'")
    query = f"name = '{safe_drive_filename}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id, webViewLink)").execute()
    existing = results.get("files", [])
    if existing: return existing[0].get("webViewLink")

    if parent_id:
        log(f"☁️  Uploading '{drive_filename}' to {get_drive_full_path(service, parent_id)}...")
    else:
        log(f"☁️  Uploading '{drive_filename}'...")
    meta  = {"name": drive_filename}
    if parent_id: meta["parents"] = [parent_id]
    import mimetypes
    mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    req   = service.files().create(body=meta, media_body=media, fields="id,webViewLink")
    response = None
    while response is None:
        status, response = req.next_chunk()
    file_id  = response.get("id")
    view_url = response.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")
    if make_public:
        service.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()
    return view_url

def get_project_drive_path(project_dir: Path):
    try:
        is_news = "news" in project_dir.parts
        if is_news:
            parts = project_dir.parts
            try:
                news_idx = parts.index("news")
                relevant_parts = parts[news_idx:]
                # Guard: if path ends at a bare date (YYYY-MM-DD), don't use it as-is
                last = relevant_parts[-1]
                if re.match(r'^\d{4}-\d{2}-\d{2}$', last):
                    # Caller passed a date folder, not a News_XX project folder.
                    # Return None or raise so the caller knows to pick a subfolder.
                    raise ValueError(f"project_dir ends at date folder, not a News_XX subfolder: {project_dir}")
                return "/".join(relevant_parts)
            except ValueError:
                raise
        year    = project_dir.parent.parent.name
        month   = project_dir.parent.name
        project = project_dir.name
        if not (year.isdigit() and month.isdigit()):
            return project
        return f"{year}/{month}/{project}"
    except Exception:
        return project_dir.name


def get_drive_folder_id(service, folder_name: str, parent_id: str = None, root_only: bool = False) -> str:
    safe_folder_name = folder_name.replace("'", "\\'")
    query = f"name = '{safe_folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id: 
        query += f" and '{parent_id}' in parents"
    elif root_only:
        query += " and 'root' in parents"
    results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    return files[0]["id"] if files else None

def resolve_drive_project_id(service, project_name: str, parent_name: str = None) -> str:
    if project_name.startswith("news/"):
        log(f"   📂 Traversing News path (root-enforced): {project_name}")
        curr_id = None
        parts = project_name.split("/")
        for i, part in enumerate(parts):
            is_root_part = (i == 0 and part == "news")
            curr_id = get_drive_folder_id(service, part, curr_id, root_only=is_root_part)
            if not curr_id:
                break
        if curr_id:
            return curr_id

    # Standard YYYY-MM-DD-project path
    match = re.search(r"(\d{4})-(\d{2})-\d{2}-project", project_name)
    if match:
        year, month = match.group(1), match.group(2)
        if not parent_name:
            parent_name = f"{year}-{month}"
        log(f"🔍 Resolving Drive ID for: {project_name} (parent: {parent_name})")
        parent_id = get_drive_folder_id(service, parent_name, root_only=True)
        if parent_id:
            year_id = get_drive_folder_id(service, year, parent_id)
            if year_id:
                month_id = get_drive_folder_id(service, month, year_id)
                if month_id:
                    project_id = get_drive_folder_id(service, project_name, month_id)
                    if project_id:
                        return project_id

    log(f"   ⚠️ Falling back to global search for {project_name}...")
    return get_drive_folder_id(service, project_name)


def get_drive_file_id(service, file_name: str, parent_id: str = None) -> str:
    safe_file_name = file_name.replace("'", "\\'")
    query = f"name = '{safe_file_name}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    return files[0]["id"] if files else None

def download_project_sources_from_drive(service, project_name: str, local_sources_dir: Path):
    log(f"🔍 Searching Drive for project: {project_name}")
    project_id = resolve_drive_project_id(service, project_name)
    if not project_id: raise FileNotFoundError(f"Project folder '{project_name}' not found on Drive.")
    # Use 0.sources if it exists, otherwise use project root
    sources_id = get_drive_folder_id(service, "0.sources", project_id) or project_id

    
    full_path = get_drive_full_path(service, sources_id)
    results = service.files().list(q=f"'{sources_id}' in parents and trashed = false", fields="files(name)").execute()
    files = [f['name'] for f in results.get('files', [])]
    log(f"   📂 Downloading from: {full_path}")
    log(f"   📄 Files available: {', '.join(files) if files else 'None'}")

    files_to_download = ["lyrics_with_prompts.md", "charactor.md", "cover.png"]
    for file_name in files_to_download:
        file_id = get_drive_file_id(service, file_name, sources_id)
        if not file_id: continue
        local_path = local_sources_dir / file_name
        request = service.files().get_media(fileId=file_id)
        with io.FileIO(str(local_path), mode="wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: status, done = downloader.next_chunk()

    results = service.files().list(q=f"'{sources_id}' in parents and name contains '.mp3' and trashed = false", fields="files(id, name)").execute()
    for mp3 in results.get('files', []):
        file_name, file_id = mp3['name'], mp3['id']
        local_path = local_sources_dir / file_name
        if local_path.exists(): continue
        request = service.files().get_media(fileId=file_id)
        with io.FileIO(str(local_path), mode="wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: status, done = downloader.next_chunk()

def check_drive_project_needs_video(service, project_name: str) -> bool:
    """Check if project on Drive lacks an .mp4, has required files, and is not already processing."""
    log(f"🔍 Checking Drive completion for: {project_name}")
    project_id = resolve_drive_project_id(service, project_name)
    if not project_id: return False
    
    # Use 0.sources if it exists, otherwise use project root
    sources_id = get_drive_folder_id(service, "0.sources", project_id) or project_id


    full_path = get_drive_full_path(service, sources_id)
    results = service.files().list(q=f"'{sources_id}' in parents and trashed = false", fields="files(id, name, modifiedTime, size)").execute()
    all_files = results.get('files', [])
    file_names = [f['name'] for f in all_files]
    
    log(f"   📁 Drive Path: {full_path}")
    log(f"   📄 Files: {', '.join(file_names) if file_names else 'None'}")

    if not all_files: return False

    status_file = next((f for f in all_files if f['name'] == STATUS_FILE_NAME), None)
    if status_file:
        try:
            from datetime import timezone
            # modifiedTime examples: '2026-04-06T10:26:24.123Z'
            modified_time = datetime.fromisoformat(status_file['modifiedTime'].replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - modified_time).total_seconds() > 30 * 60:
                log(f"   🧹 Found stale Drive status file (>{30} min), removing: {STATUS_FILE_NAME}")
                service.files().delete(fileId=status_file['id']).execute()
            else:
                return False
        except Exception as e:
            log(f"   ⚠️ Could not check/remove stale Drive status file: {e}")
            return False

    has_mp4 = False
    has_source = False
    for f in all_files:
        name = f['name']
        size = int(f.get('size', 0))
        
        if name.endswith('.mp4'):
            if size < 50 * 1024 * 1024:
                log(f"   🧹 Found small MP4 (<50MB) on Drive, removing: {name}")
                try:
                    service.files().delete(fileId=f['id']).execute()
                except Exception as e:
                    log(f"   ⚠️ Could not delete small MP4 on Drive: {e}")
            else:
                has_mp4 = True
        elif name in ['charactor.md', 'lyrics_with_prompts.md'] and size < 5:
            log(f"   ⏭️ Skipping project {project_name} on Drive because {name} is empty.")
            return False
            
        if name.endswith('.md') or name.endswith('.mp3'):
            has_source = True
            
    if has_mp4:
        log(f"   ⏭️ Skipping project {project_name}: Finished MP4 already exists.")
        return False
    return True

def sync_project_to_drive(project_dir: Path, args, out_video: str = None):
    if getattr(args, "no_gdrive", False):
        return

    # Ensure we're not trying to sync a bare date folder
    if re.match(r'^\d{4}-\d{2}-\d{2}$', project_dir.name) and "news" in project_dir.parts:
        log(f"⚠️  sync_project_to_drive: project_dir appears to be a date folder, not a News_XX project: {project_dir}")
        log(f"   Please pass the News_XX subfolder (e.g. news/2026/04/2026-04-09/News_01)")
        return

    log(f"☁️  Starting Google Drive sync for: {project_dir.name}...")
    
    # Subfolder structure: news/YYYY/MM/YYYY-MM-DD/News_0N or YYYY/MM/Project
    folder_path = get_project_drive_path(project_dir)
    is_news = "news" in project_dir.parts
    downloads_dir = get_sources_dir(project_dir) / "7.downloads"

    # Auto-derive folder/parent if not specified
    if is_news:
        parent = args.gdrive_parent or None
    else:
        parent = args.gdrive_parent or datetime.now().strftime("%Y-%m")

    gdrive_url = None
    try:
        service = get_drive_service()
        
        # ── 0. Resolve the project folder ID once (root-enforced) ──
        curr_pid = None
        if parent:
            curr_pid = get_or_create_folder(service, parent, root_only=True)
        
        parts = folder_path.split("/")
        for i, part in enumerate(parts):
            if part:
                # Enforce root-only for the 'news' top-level folder if it's starting at root
                is_root_news = (curr_pid is None and i == 0 and part == "news")
                curr_pid = get_or_create_folder(service, part, curr_pid, root_only=is_root_news)
        
        project_folder_id = curr_pid

        # ── 1. Upload Final Video ──
        if out_video and os.path.exists(out_video):
            drive_name = args.gdrive_name or Path(out_video).name
            gdrive_url = upload_to_gdrive(
                local_path         = out_video,
                folder_name        = folder_path,
                parent_folder_name = parent,
                drive_filename     = drive_name,
                make_public        = args.public,
            )
            log(f"🎬 FINAL VIDEO URL: {gdrive_url}")
            
            summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_file and os.path.exists(summary_file):
                with open(summary_file, "a", encoding="utf-8") as sf:
                    sf.write("## 🎬 Final Processed Video\n")
                    sf.write(f"**[Click here to view the final generated video on Google Drive]({gdrive_url})**\n\n")

        # ── 2. Sync 7.downloads ──
        if downloads_dir.exists():
            log(f"📂 Syncing {downloads_dir.name} contents...")
            downloads_pid = get_or_create_folder(service, "7.downloads", project_folder_id)
            query = f"'{downloads_pid}' in parents and trashed = false"
            results = service.files().list(q=query, fields="files(name)").execute()
            drive_files = {f['name'] for f in results.get('files', [])}

            for f in sorted(downloads_dir.glob("*.mp4")):
                if f.name in drive_files:
                    continue
                upload_to_gdrive(
                    local_path         = str(f),
                    folder_name        = f"{folder_path}/7.downloads",
                    parent_folder_name = parent,
                    drive_filename     = f.name,
                    make_public        = args.public
                )

        # ── 3. Sync Source files (md, mp3, png) ──
        log(f"📂 Syncing source files (.md, .mp3, .png)...")
        # Get existing files in project root to avoid dupes
        query = f"'{project_folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, fields="files(name)").execute()
        drive_files = {f['name'] for f in results.get('files', [])}
        
        # User requested syncing specific extensions during bulk sync
        for ext in ["*.md", "*.mp3", "*.png", "*.mp4"]:
            for f in project_dir.glob(ext):
                # Don't re-upload the final video if we already did it in step 1
                if out_video and f.resolve() == Path(out_video).resolve():
                    continue
                if f.name in drive_files:
                    continue
                sys.stdout.write(f"\r   ☁️  Uploading source: {f.name}...           "); sys.stdout.flush()
                upload_to_gdrive(
                    local_path         = str(f),
                    folder_name        = folder_path,
                    parent_folder_name = parent,
                    drive_filename     = f.name,
                    make_public        = args.public
                )
        print("")

        # ── 4. Upload Status File ──
        status_path = get_sources_dir(project_dir) / STATUS_FILE_NAME
        if status_path.exists():
            upload_to_gdrive(
                local_path         = str(status_path),
                folder_name        = folder_path,
                parent_folder_name = parent,
                drive_filename     = STATUS_FILE_NAME,
                make_public        = args.public
            )
            
        if args.no_local and out_video and os.path.exists(out_video):
            os.remove(out_video)
            log("🗑️  Local copy deleted (--no-local).")
            
    except Exception as e:
        log(f"⚠️  Sync failed for {project_dir.name}: {e}")

def sync_all_news_local(args):
    """Recursively find and sync all local news projects to Google Drive."""
    log("🚀 Starting bulk news sync to Google Drive...")
    news_root = root_dir / "news"
    if not news_root.exists() and os.path.exists("/My Drive/news"):
        news_root = Path("/My Drive/news")
    
    if not news_root.exists():
        log("❌ News root not found. Nothing to sync.")
        return

    # Walk: news/YYYY/MM/YYYY-MM-DD/News_XX
    count = 0
    for year_dir in sorted(news_root.glob("202*")):
        for month_dir in sorted(year_dir.glob("[01][0-9]")):
            for date_dir in sorted(month_dir.glob("202*")):
                for project_dir in sorted(date_dir.glob("[nN]ews_*")):
                    if project_dir.is_dir():
                        videos = sorted(list(project_dir.glob("veo_*.mp4")), key=os.path.getmtime, reverse=True)
                        if videos:
                            log(f"🔎 Found processed project: {project_dir.name}")
                            sync_project_to_drive(project_dir, args, out_video=str(videos[0]))
                            count += 1
                        else:
                            log(f"   (Skipping {project_dir.name}: no video)")
    log(f"✅ Bulk news sync finished. Projects processed: {count}")

# ─────────────────────────────────────────────────────────────────────────────

def save_config(config_data):
    try:
        screen_w, screen_h = pyautogui.size()
        percent_config = {}
        for k, v in config_data.items():
            if k.endswith("_x") or k.endswith("x1") or k.endswith("x2"): percent_config[k] = v / screen_w if v > 1.0 else v
            elif k.endswith("_y") or k.endswith("y1") or k.endswith("y2"): percent_config[k] = v / screen_h if v > 1.0 else v
            else: percent_config[k] = v
        with open(CONFIG_FILE, "w") as f: json.dump(percent_config, f, indent=4)
        print(f"   ✅ UI Coordinates saved to {CONFIG_FILE.name}")
    except Exception as e: print(f"   ⚠️ Could not save config: {e}")

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f: return json.load(f)
        except Exception: pass
    return None

def parse_veo_prompts(file_path):
    if not os.path.exists(file_path): return []
    with open(file_path, "r", encoding="utf-8-sig") as f: content = f.read()
    prompts = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith(">"):
            match = re.search(r"^>\s*(\d{2}:\d{2}(?:\.\d{2})?)-(\d{2}:\d{2}(?:\.\d{2})?)\s*(.*)", line)
            if match:
                prompt_text = re.sub(r"^\[.*?\]\s*", "", match.group(3).strip()).strip()
                if prompt_text: prompts.append({"start": match.group(1).strip(), "end": match.group(2).strip(), "text": prompt_text})
            else:
                match_single = re.search(r"^>\s*(\d{2}:\d{2}(?:\.\d{2})?)(?:-)?\s*(.*)", line)
                if match_single:
                    prompt_text = re.sub(r"^\[.*?\]\s*", "", match_single.group(2).strip()).strip()
                    if prompt_text: prompts.append({"start": match_single.group(1).strip(), "end": None, "text": prompt_text})
                else:
                    prompt_text = re.sub(r"^>\s*", "", line).strip()
                    if prompt_text: prompts.append({"start": "00:00.00", "end": None, "text": prompt_text})
    return prompts

def time_to_sec(t_str):
    if "." not in t_str: t_str += ".00"
    m, s   = t_str.split(":")
    sec, ms = s.split(".")
    return int(m) * 60 + int(sec) + int(ms) / 100.0

def wait_for_visual_begin(monitor, timeout=180, finder=None, retry_tpl=None, scales=None):
    log("🔍 Monitoring screen...")
    with mss.mss() as sct:
        time.sleep(1.0)
        base_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
        last_save = time.time()
        start_t = time.time()
        last_retry = time.time()
        while True:
            time.sleep(1.0)
            curr_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
            if np.mean(cv2.absdiff(base_gray, curr_gray)) > 4.0:
                log("⏳ State change detected!")
                time.sleep(3.0)
                return True
                
            if finder and retry_tpl and (time.time() - last_retry > 15):
                if finder.wait_and_click_template(retry_tpl, timeout=1, times=1, scales=scales):
                    log("🔄 Re-clicked submit button during monitor wait!")
                    pyautogui.moveTo(10, 10)
                    time.sleep(1.0)
                    base_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
                last_retry = time.time()
                
            if time.time() - start_t > timeout:
                log(f"⚠️ Timeout ({timeout}s) waiting for state change.")
                return False
            if time.time() - last_save > 60:
                save_debug_screenshot("waiting_visual_begin")
                last_save = time.time()

def wait_for_visual_end(monitor, max_total_wait=270, session_start_time=0):
    log("\n🏁 Monitoring end state...")
    with mss.mss() as sct:
        static_start, overall_start = time.time(), time.time()
        top_h = max(int(monitor["height"] * 0.15), 10)
        def get_top_gray(): return cv2.cvtColor(np.array(sct.grab(monitor))[:top_h, :, :3], cv2.COLOR_BGR2GRAY)
        last_gray = get_top_gray()
        while True:
            time.sleep(1.0)
            curr_gray, mean_diff = get_top_gray(), np.mean(cv2.absdiff(last_gray, get_top_gray()))
            last_gray, session_rem = curr_gray, max(0, 270 - (time.time() - session_start_time))
            if session_rem < 10.0 or (mean_diff < 1.5 and time.time() - static_start >= 5.0) or (time.time() - overall_start > max_total_wait):
                return True
            if mean_diff > 1.5: static_start = time.time()

def record_screen(monitor, output_filename, fps, stop_event, is_recording, stats):
    try:
        with mss.mss() as sct:
            out = cv2.VideoWriter(output_filename, cv2.VideoWriter_fourcc(*"mp4v"), fps, (monitor["width"], monitor["height"]))
            step = 1.0 / fps
            while not stop_event.is_set():
                t0 = time.time()
                if is_recording.is_set():
                    out.write(np.array(sct.grab(monitor))[:, :, :3])
                    stats["total_frames"] += 1
                sleep_t = step - (time.time() - t0)
                if sleep_t > 0: time.sleep(sleep_t)
            out.release()
    except Exception as e: log(f"❌ Recorder error: {e}")

_LAST_FOUND_DATE_STR = None

def get_project_dir(service=None, fallback=True):
    global _LAST_FOUND_DATE_STR
    start_date_str = None
    
    if _LAST_FOUND_DATE_STR:
        start_date = datetime.strptime(_LAST_FOUND_DATE_STR, "%Y-%m-%d") + timedelta(days=1)
    else:
        if AUTOMATION_STATE_FILE.exists():
            try:
                with open(AUTOMATION_STATE_FILE, "r") as f: start_date_str = json.load(f).get("latest_mp4_date")
            except: pass
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d") if start_date_str else datetime.now() - timedelta(days=7)

    # ── 0. Check for News projects first (local) ──────────────────────────────
    log("🔍 Scanning for local news projects...")
    news_root = root_dir / "news"
    if not news_root.exists() and os.path.exists("/My Drive/news"):
        news_root = Path("/My Drive/news")
    elif not news_root.exists() and os.path.exists("/home/runner/work/ai_server/ai_server/news"):
        news_root = Path("/home/runner/work/ai_server/ai_server/news")

    news_mode = news_root.exists()  # <-- track whether we're in news mode

    if news_mode:
        log(f"   📂 News root: {news_root}")
        target_date = datetime.now(ZoneInfo('America/Los_Angeles'))
        date_dir = news_root / target_date.strftime("%Y") / target_date.strftime("%m") / target_date.strftime("%Y-%m-%d")
        if date_dir.exists():
            potential_folders = list(date_dir.glob("News_*")) + list(date_dir.glob("news_*"))
            project_dirs = sorted(list(set(potential_folders)))
            for project_dir in project_dirs:
                if project_dir.is_dir():
                    status_file = project_dir / STATUS_FILE_NAME
                    if status_file.exists():
                        if time.time() - status_file.stat().st_mtime > 30 * 60:
                            log(f"   🧹 Found stale status file at {project_dir.name}, removing...")
                            try: status_file.unlink()
                            except: pass
                        else:
                            continue
                    if not list(project_dir.glob("veo_*.mp4")):
                        log(f"✨ Found pending news project: {project_dir.name} (in {target_date.strftime('%Y-%m-%d')})")
                        return project_dir
    else:
        log("   ⚠️ News root folder not found.")

    # ── 1. Check Drive for news projects ─────────────────────────────────────
    if service:
        log("🔍 Checking Google Drive for pending projects (priority)...")
        log("   📂 Scanning Drive for news projects...")
        news_root_id = get_drive_folder_id(service, "news", root_only=True)

        if news_root_id:
            news_mode = True  # Drive has a news folder — we're in news mode
            current_date = datetime.now(ZoneInfo('America/Los_Angeles'))
            year_str  = current_date.strftime("%Y")
            month_str = current_date.strftime("%m")
            date_str  = current_date.strftime("%Y-%m-%d")
            year_id = get_drive_folder_id(service, year_str, news_root_id)
            if year_id:
                month_id = get_drive_folder_id(service, month_str, year_id)
                if month_id:
                    date_id = get_drive_folder_id(service, date_str, month_id)
                    if date_id:
                        results = service.files().list(
                            q=f"'{date_id}' in parents and (name contains 'News_' or name contains 'news_') and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                            fields="files(name)"
                        ).execute()
                        news_folders = results.get('files', [])
                        if news_folders:
                            log(f"   🔎 Found {len(news_folders)} news folders in {date_str}: {[f['name'] for f in news_folders]}")
                            for f in sorted(news_folders, key=lambda x: x['name']):
                                project_name = f"news/{year_str}/{month_str}/{date_str}/{f['name']}"
                                if check_drive_project_needs_video(service, project_name):
                                    log(f"✨ Found news project needing video (on Drive): {f['name']} ({date_str})")
                                    return root_dir / "news" / year_str / month_str / date_str / f['name']

        # ── If we found a news root (local or Drive), do NOT fall through to standard projects ──
        if news_mode:
            log("✅ News mode active — no pending news projects found. Exiting project search.")
            return None

        # ── 1b. Only reach here if no news infrastructure exists at all ──────
        log("   📂 No news folder found on Drive. Scanning standard projects...")
        for i in range(0, 120):
            current_date = start_date + timedelta(days=i)
            date_str     = current_date.strftime("%Y-%m-%d")
            project_name = f"{date_str}-project"
            if check_drive_project_needs_video(service, project_name):
                log(f"✨ Found standard project needing video (on Drive): {date_str}")
                _LAST_FOUND_DATE_STR = date_str
                return root_dir / current_date.strftime("%Y") / current_date.strftime("%m") / project_name

    # ── If news mode was active (local or Drive), stop here ──────────────────
    if news_mode:
        log("✅ News mode active — no pending news projects found. Exiting project search.")
        return None

    # ── 2. Fallback: local standard projects (only if no news infrastructure) ─
    log("🔍 Checking local history for pending standard projects...")
    for i in range(0, 60):
        current_date = start_date + timedelta(days=i)
        date_str     = current_date.strftime("%Y-%m-%d")
        project_name = f"{date_str}-project"
        test_dir     = root_dir / current_date.strftime("%Y") / current_date.strftime("%m") / project_name
        status_file_path = get_sources_dir(test_dir) / STATUS_FILE_NAME
        if status_file_path.exists():
            if time.time() - status_file_path.stat().st_mtime > 30 * 60:
                try: status_file_path.unlink()
                except: pass
            else:
                continue

        if test_dir.exists():
            sources_dir = get_sources_dir(test_dir)
            skip_due_to_empty = False
            for md_file in ["charactor.md", "lyrics_with_prompts.md"]:
                md_path = sources_dir / md_file
                if md_path.exists() and md_path.stat().st_size < 5:
                    log(f"   Skip project: {md_file} is empty: {date_str}")
                    skip_due_to_empty = True
                    break
            if skip_due_to_empty:
                continue

            for mp4_file in get_sources_dir(test_dir).rglob("*.mp4"):
                try:
                    if mp4_file.is_file() and mp4_file.stat().st_size / (1024 * 1024) < 50:
                        log(f"   🧹 Deleting small MP4 (<50MB) before check: {mp4_file.name}")
                        mp4_file.unlink()
                except Exception:
                    pass

            if not list(get_sources_dir(test_dir).glob("*.mp4")):
                log(f"✨ Found local project needing video: {date_str}")
                _LAST_FOUND_DATE_STR = date_str
                return test_dir

    if not fallback:
        return None

    # Last resort fallback to today's date
    now = datetime.now()
    today_str = (now + timedelta(days=1 if now.hour >= 20 else -1 if now.hour <= 6 else 0)).strftime("%Y-%m-%d")
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    _LAST_FOUND_DATE_STR = today_str
    return root_dir / today_dt.strftime("%Y") / today_dt.strftime("%m") / f"{today_str}-project"

def main():
    parser = argparse.ArgumentParser(description="V-Process Automated Capture")
    parser.add_argument("--project", "-p", type=str)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--wait", "-w", type=float)
    parser.add_argument("--between", "-b", type=float, default=6.0)
    parser.add_argument("--start-delay", type=float, default=10.0)
    parser.add_argument("--cut-start", type=float, default=6.0)
    parser.add_argument("--cut-end", type=float, default=6.0)
    parser.add_argument("--upload-only", "-u", action="store_true", help="Skip capture and transcode, just upload the latest final video.")
    parser.add_argument("--convert", "-c", action="store_true")
    parser.add_argument("--loop", "-l", action="store_true", help="Keep running and wait for projects")
    parser.add_argument("--input-raw", "-i", type=str)
    parser.add_argument("--gdrive-folder", type=str, default=None)
    parser.add_argument("--gdrive-parent", type=str, default=None)
    parser.add_argument("--gdrive-name",   type=str, default=None)
    parser.add_argument("--public",        action="store_true", default=True)
    parser.add_argument("--no-local",      action="store_true")
    parser.add_argument("--no-gdrive",     action="store_true")
    parser.add_argument("--sync-news",     action="store_true", help="Bulk upload all local news projects to Drive.")
    args = parser.parse_args()

    print("\n🚀 V-Process Runner Starting...")
    
    # Ensure Chrome is ready
    if not args.upload_only and not args.convert and not args.sync_news:
        ensure_chrome_running()

    if args.sync_news:
        sync_all_news_local(args)
        return

    pyautogui.click(1968, 104); time.sleep(3)

    pyautogui.click(1970, 107); time.sleep(3)

    # print(f"\nPress ENTER to continue with  second account's project posting...")
    # input()
        
    config = load_config()
    screen_w, screen_h = pyautogui.size()
    if args.reset or not config:
        print("🎬 Configuration needed...")
        return # Interactive config skipped in headless

    x1, y1 = int(config["vid_x1"] * screen_w if config["vid_x1"] <= 1.0 else config["vid_x1"]), int(config["vid_y1"] * screen_h if config["vid_y1"] <= 1.0 else config["vid_y1"])
    x2, y2 = int(config["vid_x2"] * screen_w if config["vid_x2"] <= 1.0 else config["vid_x2"]), int(config["vid_y2"] * screen_h if config["vid_y2"] <= 1.0 else config["vid_y2"])
    text_x, text_y = int(config["text_x"] * screen_w if config["text_x"] <= 1.0 else config["text_x"]), int(config["text_y"] * screen_h if config["text_y"] <= 1.0 else config["text_y"])
    w, h = (abs(x2 - x1) // 2 * 2), (abs(y2 - y1) // 2 * 2)
    monitor = {"top": min(y1, y2), "left": min(x1, x2), "width": w, "height": h}

    while True:
        if os.path.exists("/tmp/save"):
            log("🛑 Exit signal detected. Quitting runner.")
            break

        service = None
        if not args.project:
            try: service = get_drive_service()
            except: pass
            project_dir = get_project_dir(service, fallback=not args.loop)
        else:
            project_dir = Path(args.project)
            if not project_dir.is_absolute():
                test_dir = root_dir / "projects" / args.project
                if test_dir.exists(): project_dir = test_dir
                else:
                    parts = str(args.project).split("-")
                    if len(parts) >= 2 and parts[0].isdigit():
                        test_dir = root_dir / parts[0] / parts[1] / args.project
                        if test_dir.exists(): project_dir = test_dir
                    if not project_dir.exists(): project_dir = Path(os.getcwd()) / args.project

        # ── Expand bare date folders to their News_XX subfolders ──────────────────
        if (project_dir and project_dir.is_dir()
                and re.match(r'^\d{4}-\d{2}-\d{2}$', project_dir.name)
                and "news" in project_dir.parts):
            subfolders = sorted([d for d in project_dir.iterdir()
                                 if d.is_dir() and re.match(r'(?i)^news_\d+$', d.name)])
            if subfolders:
                log(f"📂 Expanding date folder to first pending News_XX subfolder...")
                project_dir = subfolders[0]
                log(f"   → {project_dir}")
            else:
                log(f"❌ Date folder {project_dir} has no News_XX subfolders. Skipping.")
                args.project = None
                continue

        if not project_dir:
            try:
                with open('/tmp/v_current_project', 'w') as f: f.write('Idle')
            except: pass
            if args.loop:
                log("😴 No projects found. Waiting 60s...")
                # time.sleep(60)
                return
            else:
                log("❌ No project specified or found.")
                return

        try:
            with open('/tmp/v_current_project', 'w') as f: f.write(project_dir.name)
        except: pass

        # ── Cleanup invalid small MP4 files project-wide (if skipped by auto-detection) ──
        for mp4_file in get_sources_dir(project_dir).rglob("*.mp4"):
            try:
                if mp4_file.is_file() and mp4_file.stat().st_size / (1024 * 1024) < 50:
                    log(f"   🧹 Deleting small MP4 (<50MB): {mp4_file.name}")
                    mp4_file.unlink()
            except Exception as e:
                log(f"⚠️ Could not check/delete {mp4_file.name}: {e}")

        # ── 2. Mark as processing locally & on Drive ──
        mark_project_processing(project_dir)
        if not getattr(args, "no_gdrive", False):
            try:
                status_path = get_sources_dir(project_dir) / STATUS_FILE_NAME
                folder_path = get_project_drive_path(project_dir)
                is_news = "news" in project_dir.parts                 
                # Derive parent dynamically: news projects have no parent, 
                # standard projects use YEAR-MONTH (e.g. 2026-04)
                if is_news:
                    parent = args.gdrive_parent
                else:
                    parent = args.gdrive_parent or datetime.now().strftime("%Y-%m")
                
                log(f"☁️  Uploading {STATUS_FILE_NAME} as global lock...")
                upload_to_gdrive(
                    local_path         = str(status_path),
                    folder_name        = folder_path,
                    parent_folder_name = parent,
                    drive_filename     = STATUS_FILE_NAME,
                    make_public        = args.public
                )
            except Exception as e:
                log(f"⚠️  Could not upload status file for locking: {e}")

        sources_dir = get_sources_dir(project_dir)

        prompts_file = sources_dir / "lyrics_with_prompts.md"
        
        # ── 2.5. Sync sources from Drive (only if capturing) ──
        if not args.upload_only and not args.convert:
            log(f"📂 Syncing sources for '{project_dir.name}' from Drive...")
            try:
                if not service: service = get_drive_service()
                sources_dir.mkdir(parents=True, exist_ok=True)
                # Use full path for news projects to ensure correct resolution
                drive_project_path = get_project_drive_path(project_dir)
                download_project_sources_from_drive(service, drive_project_path, sources_dir)
            except Exception as e:
                log(f"⚠️ Failed to fetch sources from Drive for {project_dir.name}: {e}")

            if not prompts_file.exists():
                log(f"🛑 Missing required sources! Skipping project {project_dir.name}...")
                log(f"👉 Expected to find: {prompts_file}")
                args.project = None
                continue

        skip_due_to_empty = False
        for md_file in ["charactor.md", "lyrics_with_prompts.md"]:
            md_path = get_sources_dir(project_dir) / md_file
            if md_path.exists() and md_path.stat().st_size < 5:
                log(f"📝 {md_file} is empty. Skipping project {project_dir.name}.")
                skip_due_to_empty = True
                break
        
        if skip_due_to_empty:
            args.project = None
            continue

        prompts = parse_veo_prompts(get_sources_dir(project_dir) / "lyrics_with_prompts.md")
        if not prompts:
            log("❌ No prompts found.")
            args.project = None
            continue
        log(f"✨ Loaded {len(prompts)} prompts.\n")

        # ── 3. File paths ────────────────────────────────────────────────────────
        downloads_dir = get_sources_dir(project_dir) / "7.downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_video = str(downloads_dir / f"realtime_veo_raw_{ts}.mp4")
        out_video = str(get_sources_dir(project_dir) / f"veo_{ts}.mp4")


        # ── 3.5. Determine Action ────────────────────────────────────────────────
        do_capture   = not args.upload_only and not args.convert
        do_transcode = not args.upload_only
        do_cleanup   = not args.upload_only and not args.convert
        total_frames = 0
        char_text_ok = True  # stays True when capture is skipped (upload-only / convert)

        if do_capture:
            char_file = get_sources_dir(project_dir) / "charactor.md"
            cover_img = get_sources_dir(project_dir) / "cover.png"

            
            if cover_img.exists():
                try:
                    target_size = 5 * 1024 * 1024
                    while os.path.getsize(cover_img) > target_size:
                        log(f"Cover image > 5MB ({os.path.getsize(cover_img)} bytes). Resizing...")
                        img = cv2.imread(str(cover_img))
                        if img is not None:
                            h, w = img.shape[:2]
                            img = cv2.resize(img, (int(w * 0.9), int(h * 0.9)), interpolation=cv2.INTER_AREA)
                            cv2.imwrite(str(cover_img), img)
                        else:
                            break
                except Exception as e:
                    log(f"⚠️ Error checking/resizing cover image: {e}")

            # Use semi-tolerant threshold and multi-scale for Linux compatibility
            # On Linux, Windows-captured templates often need ~0.7 scale
            finder = ScreenTemplateFinder(confidence_threshold=0.6)
            search_scales = [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]

            pyautogui.click(x1, y1); time.sleep(3)
            # pyautogui.scroll(-5000); time.sleep(1)

            pyautogui.hotkey('ctrl', 'l')
            time.sleep(1)   
            pyautogui.write(APP_URL)
            pyautogui.press('enter')
            time.sleep(3)

            log("Waiting for page load (prompt input field)...")
            t_input = str(script_dir / "realtime" / "prompt_input.png")
            last_screenshot = time.time()
            while not finder.wait_for_template(t_input, timeout=10, scales=search_scales):
                log("   ⏳ Still waiting for page load...")
                if time.time() - last_screenshot > 30:
                    save_debug_screenshot("page_load_stuck")
                    last_screenshot = time.time()
            log("✅ Page loaded.")

            # for tpl in ["create_world.png", "scrolling.png"]:
            #     t = str(script_dir / "realtime" / tpl)
            #     if not finder.wait_and_click_template(t, timeout=10, times=3, scales=search_scales):
            #         log(f"⚠️ Could not find {tpl}. Continuing...")
            #     else:
            #         time.sleep(2)

            log("Typing character text...")
            text = "Character reference"
            if char_file.exists():
                raw_text = char_file.read_text(encoding="utf-8")
                text = re.sub(r'```json.*?```', '', raw_text, flags=re.DOTALL).strip()
                text = re.sub(r'\s+', ' ', text).strip()
                if not text: text = "Character reference"
            t = str(script_dir / "realtime" / "prompt_input.png")
            finder.wait_and_click_template(t, timeout=5, scales=search_scales)
            pyautogui.write(text, interval=0.01)
            # (Character verification moved to occur 30s after recording starts)
            char_text_ok = True

            if cover_img.exists():
                for attempt in range(3):
                    log(f"Uploading cover image: {cover_img.name} (Attempt {attempt+1}/3)")
                    t_img_ref = str(script_dir / "realtime" / "image_reference.png")
                    if finder.wait_and_click_template(t_img_ref, timeout=10, scales=search_scales):
                        time.sleep(1.5)
                        pyautogui.write(os.path.abspath(cover_img))
                        time.sleep(1.5)
                        pyautogui.press("enter")
                        time.sleep(1.5)
                        t_open = str(script_dir / "realtime" / "open_file.png")
                        if finder.wait_and_click_template(t_open, timeout=10, scales=search_scales):
                            time.sleep(1.5) 
                        else:
                            log("⚠️ Could not find open_file.png. Continuing...")
                            save_debug_screenshot("missing_open_file")

                        log("Cover image submitted. Waiting 18s for upload...")
                        time.sleep(18)

                        if finder.wait_for_template(t_img_ref, timeout=5, scales=search_scales):
                            log("⚠️ Cover image upload failed or image_reference still visible. Retrying...")
                            continue
                        else:
                            log("✅ Cover image upload successful.")
                            break
                    else:
                        log("⚠️ Could not find image_reference.png to trigger upload. Possibly already uploaded.")
                        break
                   

            for tpl in ["submit.png"]:
                t = str(script_dir / "realtime" / tpl)
                if not finder.wait_and_click_template(t, timeout=10, times=3, scales=search_scales):
                    log(f"⚠️ Could not find {tpl}. Continuing...")
                    save_debug_screenshot(f"missing_{tpl.replace('.png','')}")
                else:
                    time.sleep(2)

            time.sleep(6)

            # t = str(script_dir / "realtime" / "no_avatar.png")
            # if not finder.wait_and_click_template(t, timeout=10, times=3, scales=search_scales):
            #     log("⚠️ Could not find no_avatar.png. Continuing...")
            # else:
            #     time.sleep(2)

            # t = str(script_dir / "realtime" / "no_avatar_confirm.png")
            # if not finder.wait_and_click_template(t, timeout=10, times=3, scales=search_scales):
            #     log("⚠️ Could not find no_avatar_confirm.png. Continuing...")
            # else:
            #     time.sleep(2)

            # Start recording loop
            capture_done = False
            skip_current_project = False
            while not capture_done:
                ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
                raw_video = str(downloads_dir / f"realtime_veo_raw_{ts}.mp4")

                stop_event   = threading.Event()
                is_recording = threading.Event()
                stats        = {"total_frames": 0}
                recorder_thread = threading.Thread(
                    target=record_screen,
                    args=(monitor, raw_video, 30, stop_event, is_recording, stats)
                )
                save_debug_screenshot(f"before_recording_{ts}")
                recorder_thread.start()
                time.sleep(1.0)

                redo_capture = False

                # ── 5. Automation loop ────────────────────────────────────────────────
                try:
                    submit_tpl = str(script_dir / "realtime" / "submit.png")
                    if not wait_for_visual_begin(monitor, timeout=180, finder=finder, retry_tpl=submit_tpl, scales=search_scales):
                        raise ValueError("visual_timeout")
                        
                    is_recording.set()
                    session_start_time = time.time()
                    save_debug_screenshot(f"during_recording_{ts}")

                    log("🔍 Waiting for actual video picture (not black) before sending first prompt...")
                    wait_pic_start = time.time()
                    with mss.mss() as sct:
                        base_p_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
                        while True:
                            curr_p_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
                            diff = np.mean(cv2.absdiff(base_p_gray, curr_p_gray))
                            if diff > 12.0 or np.mean(curr_p_gray) > 15.0:
                                log(f"✅ Video picture detected! (diff={diff:.1f}, mean={np.mean(curr_p_gray):.1f})")
                                break
                            if time.time() - wait_pic_start > 120:
                                log("⚠️ Timeout waiting for video picture. Proceeding anyway.")
                                break
                            if os.path.exists(STOP_SIGNAL_FILE):
                                break
                            time.sleep(0.2)

                    log("📋 Session active. Waiting 2.0s before first prompt...")
                    time.sleep(2.0)

                    if not char_text_ok:
                        capture_done = True
                    else:
                        for i, p in enumerate(prompts):
                            log(f"\n=> Prompt [{i+1}/{len(prompts)}]: {p['text'][:60]}...")
                            pyautogui.click(text_x, text_y); time.sleep(0.2)
                            pyautogui.hotkey("ctrl", "a"); pyautogui.press("delete")
                            pyautogui.write(p["text"], interval=0.01)
                            time.sleep(0.3)

                            # ── Verify prompt was typed correctly (skipping first 3) ──────────────
                            if i < 3:
                                log(f"   ⏩ Prompt [{i+1}] verification skipped (initial session phase).")
                            else:
                                try:
                                    pyautogui.hotkey("ctrl", "a"); time.sleep(0.15)
                                    pyautogui.hotkey("ctrl", "c"); time.sleep(0.25)
                                    clipboard_text = ""
                                    try:
                                        clipboard_text = subprocess.run(
                                            ["xclip", "-o", "-selection", "clipboard"],
                                            capture_output=True, text=True, timeout=3
                                        ).stdout.strip()
                                    except Exception:
                                        try:
                                            clipboard_text = subprocess.run(
                                                ["xsel", "--clipboard", "--output"],
                                                capture_output=True, text=True, timeout=3
                                            ).stdout.strip()
                                        except Exception:
                                            clipboard_text = p["text"]  # unreadable – assume OK

                                    expected = p["text"].strip()
                                    log(f"   🐛 DEBUG [Prompt verification]:\n      Expected: {repr(expected)}\n      Clipboard: {repr(clipboard_text)}")
                                    if clipboard_text != expected:
                                        elapsed = time.time() - session_start_time
                                        log(f"⚠️ Prompt mismatch! Expected: '{expected[:50]}' | Got: '{clipboard_text[:50]}'")
                                        save_debug_screenshot("prompt_mismatch")
                                        if elapsed > 90:
                                            log(f"✅ {elapsed:.0f}s recorded (> 1.5 min). Proceeding with current footage.")
                                        else:
                                            log(f"🔄 {elapsed:.0f}s recorded (< 1.5 min). Skipping project.")
                                            skip_current_project = True
                                        break  # exit prompt loop in either mismatch case
                                    else:
                                        log(f"   ✅ Prompt [{i+1}] verified OK.")
                                except Exception as verify_err:
                                    log(f"⚠️ Prompt verification skipped: {verify_err}")

                            pyautogui.press("enter")

                            duration = 6.0
                            if args.wait is not None:
                                duration = args.wait
                            elif p["end"]:
                                duration = time_to_sec(p["end"]) - time_to_sec(p["start"])
                            elif i < len(prompts) - 1:
                                duration = time_to_sec(prompts[i+1]["start"]) - time_to_sec(p["start"])
                            duration = max(duration, 10.0)

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
                    save_debug_screenshot(f"end_recording_{ts}")

                except KeyboardInterrupt:
                    log("\n⚠️ Interrupted.")
                except ValueError as e:
                    if str(e) == "visual_timeout":
                        log("⚠️ Visual timeout. Forcing redo_capture.")
                        redo_capture = True
                    else:
                        raise e
                finally:
                    log("\n🔌 Stopping recorder...")
                    stop_event.set()
                    recorder_thread.join()
                    total_frames = stats.get("total_frames", 0)

                if skip_current_project:
                    capture_done = True
                    try:
                        if os.path.exists(raw_video):
                            os.remove(raw_video)
                            log(f"   🗑️ Removed short/empty recording: {Path(raw_video).name}")
                    except Exception: pass
                    break

                if not redo_capture and os.path.exists(raw_video):
                    file_size_mb = os.path.getsize(raw_video) / (1024 * 1024)
                    if file_size_mb < 50:
                        log(f"⚠️ Raw video size ({file_size_mb:.1f}MB) is less than 50MB. Skipping project...")
                        skip_current_project = True
                        capture_done = True
                        try:
                            if os.path.exists(raw_video):
                                os.remove(raw_video)
                                log(f"   🗑️ Removed short/empty recording: {Path(raw_video).name}")
                        except Exception: pass
                        break

                if redo_capture:
                    log("🔄 Discarding short recording and restarting capture...")
                    try:
                        if os.path.exists(raw_video):
                            os.remove(raw_video)
                            log(f"   🗑️ Removed: {Path(raw_video).name}")
                    except Exception as e:
                        log(f"   ⚠️ Could not remove raw video: {e}")
                    time.sleep(3)
                    # Reload page before retrying
                    pyautogui.click(x1, y1); time.sleep(1)
                    pyautogui.hotkey('ctrl', 'l'); time.sleep(1)
                    pyautogui.write(APP_URL)
                    pyautogui.press('enter')
                    time.sleep(6)
                else:
                    capture_done = True

        if do_capture and skip_current_project:
            args.project = None
            continue

        # ── Character-mismatch length gate ────────────────────────────────────
        if not char_text_ok:
            MIN_FRAMES = int(1.3 * 60 * 30)  # 1.3 min × 60 s × 30 fps = 2340 frames
            if total_frames < MIN_FRAMES:
                recorded_sec = total_frames / 30.0
                log(f"🛑 Character mismatch AND only {recorded_sec:.1f}s recorded "
                    f"(< 1.3 min). Skipping this project.")
                args.project = None
                continue
            else:
                recorded_sec = total_frames / 30.0
                log(f"⚠️ Character mismatch but {recorded_sec:.1f}s recorded "
                    f"(>= 1.3 min). Continuing to process existing footage.")

        if do_transcode:
            import subprocess
            gdrive_url = None

            if args.convert:
                if args.input_raw:
                    raw_video = args.input_raw
                else:
                    raw_videos = sorted(list(downloads_dir.glob("realtime_veo_raw_*.mp4")), key=os.path.getmtime, reverse=True)
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
                        # ── Auto-detect actual start to bypass 'Preparing your world' ──
                        log("🔍 Auto-detecting video start (bypassing 'Preparing your world')...")
                        actual_cut_start = args.cut_start
                        try:
                            cap = cv2.VideoCapture(raw_video)
                            v_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                            success, base_frame = cap.read()
                            if success:
                                base_gray = cv2.cvtColor(base_frame, cv2.COLOR_BGR2GRAY)
                                for i in range(1, int(45 * v_fps)):
                                    success, curr_frame = cap.read()
                                    if not success: break
                                    if i % 3 == 0:
                                        curr_gray = cv2.cvtColor(curr_frame, cv2.COLOR_BGR2GRAY)
                                        diff = np.mean(cv2.absdiff(base_gray, curr_gray))
                                        if diff > 12.0:
                                            detected_sec = i / v_fps
                                            log(f"   🎥 Transition off black screen detected at {detected_sec:.2f}s (diff={diff:.1f})")
                                            actual_cut_start = max(args.cut_start, detected_sec + 0.5)
                                            break
                            cap.release()
                        except Exception as e:
                            log(f"   ⚠️ Start detection failed: {e}")

                        target_duration = max(1.0, total_frames / 30.0 - actual_cut_start - args.cut_end)

                        # ── Audio ────────────────────────────────────────────────
                        log("🎵 Preparing audio...")
                        sources_dir  = get_sources_dir(project_dir)


                        # # 🔊 Ensure audio files exist locally before transcoding
                        # if not list(sources_dir.glob("*.mp3")):
                        #     log("   ⚠️ Audio files (.mp3) missing locally. Attempting to fetch from Drive...")
                        #     try:
                        #         if not service: service = get_drive_service()
                        #         download_project_sources_from_drive(service, project_dir.name, sources_dir)
                        #     except Exception as e:
                        #         log(f"   ⚠️ Could not download audio from Drive: {e}")

                        # mp3_0        = sources_dir / "part_000.mp3"
                        # mp3_9        = sources_dir / "part_000 (9).mp3"
                        combined_mp3 = sources_dir / f"combined_audio.mp3"

                        if combined_mp3.exists():
                            log(f"   🔹 Using existing combined audio: {combined_mp3.name}")
                        # elif mp3_0.exists() and mp3_9.exists():
                        #     log("   ➕ Combining part_000.mp3 + part_000 (9).mp3...")
                        #     subprocess.run([
                        #         "ffmpeg", "-y", "-i", str(mp3_0), "-i", str(mp3_9),
                        #         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                        #         "-map", "[a]", str(combined_mp3)
                        #     ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                        
                        # Veo watermark removal: Top-Right and Bottom-Right
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
                                "ffmpeg", "-y", "-ss", str(actual_cut_start), "-i", raw_video,
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
                                "ffmpeg", "-y", "-ss", str(actual_cut_start), "-i", raw_video,
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
                            if os.path.exists(out_video):
                                final_size_mb = os.path.getsize(out_video) / (1024 * 1024)
                                log(f"✅ Transcoding complete. Final video size: {final_size_mb:.1f}MB")
                            else:
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
                file_size_mb = os.path.getsize(out_video) / (1024 * 1024)
                print(f" 📁 Local : {out_video} ({file_size_mb:.1f}MB)")
            print("=" * 60 + "\n")

            # Upload-only mode: find the latest existing video if none recently made
            if not os.path.exists(out_video):
                videos = sorted(list(get_sources_dir(project_dir).glob("veo_*.mp4")), key=os.path.getmtime, reverse=True)

                if videos:
                    out_video = str(videos[0])
                    log(f"📋 Upload-only: Found latest video: {Path(out_video).name}")

        # ── 6. Final Upload Logic (Runs in both modes) ───────────────────────────
        if (os.path.exists(out_video) or downloads_dir.exists()) and not getattr(args, "no_gdrive", False):
            sync_project_to_drive(project_dir, args, out_video=out_video if os.path.exists(out_video) else None)
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
                    t = str(script_dir / "realtime" / tpl)
                    if finder.wait_and_click_template(t, timeout=3, scales=cleanup_scales):
                        time.sleep(2)

                # pyautogui.click(x1, y1); time.sleep(3)
                # # pyautogui.scroll(-5000); time.sleep(1)

                # t = str(script_dir / "realtime" / "back_to_button.png")
                # if finder.wait_and_click_template(t, timeout=10, scales=cleanup_scales):
                #     time.sleep(2)
                # pyautogui.scroll(-5000); time.sleep(1)

                # t = str(script_dir / "realtime" / "back_to_button.png")
                # if finder.wait_and_click_template(t, timeout=10, scales=search_scales):
                #     time.sleep(2)
            except Exception:
                pass

        print("\n" + "=" * 60)
        print(" ✅ Operation Complete.")
        print("=" * 60 + "\n")

        # ── After finishing a project, clear the pinned --project arg so the
        # next iteration calls get_project_dir() and auto-finds the next one.
        args.project = None

        if not args.loop:
            # Even without --loop, try to pick up the next pending project.
            # We only stop if get_project_dir() returns nothing.
            pass  # fall through to the top of the while-loop


if __name__ == "__main__": main()
