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
APP_URL = os.environ.get("V_URL", "https://realtime.pixverse.ai/generate/")

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
    return {
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "repository": os.environ.get("GITHUB_REPOSITORY", "unknown-repo"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        "timestamp": datetime.now().isoformat()
    }


def mark_project_processing(project_dir: Path):
    sources_dir = project_dir / "0.sources"
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


def get_or_create_folder(service, folder_name: str, parent_id: str = None) -> str:
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    if files: return files[0]["id"]
    meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id: meta["parents"] = [parent_id]
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def upload_to_gdrive(local_path: str, folder_name: str  = None, parent_folder_name: str = None, drive_filename: str = None, make_public: bool = True) -> str:
    drive_filename = drive_filename or Path(local_path).name
    service = get_drive_service()
    parent_id = None
    if parent_folder_name: parent_id = get_or_create_folder(service, parent_folder_name)
    if folder_name:
        for part in folder_name.replace("\\", "/").split("/"):
            if part: parent_id = get_or_create_folder(service, part, parent_id)

    query = f"name = '{drive_filename}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id, webViewLink)").execute()
    existing = results.get("files", [])
    if existing: return existing[0].get("webViewLink")

    log(f"☁️  Uploading '{drive_filename}'...")
    meta  = {"name": drive_filename}
    if parent_id: meta["parents"] = [parent_id]
    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True)
    req   = service.files().create(body=meta, media_body=media, fields="id,webViewLink")
    response = None
    while response is None:
        status, response = req.next_chunk()
    file_id  = response.get("id")
    view_url = response.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")
    if make_public:
        service.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()
    return view_url

def get_drive_folder_id(service, folder_name: str, parent_id: str = None) -> str:
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
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
    query = f"name = '{file_name}' and trashed = false"
    if parent_id: query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    return files[0]["id"] if files else None

def download_project_sources_from_drive(service, project_name: str, local_sources_dir: Path):
    log(f"🔍 Searching Drive for project: {project_name}")
    project_id = resolve_drive_project_id(service, project_name)
    if not project_id: raise FileNotFoundError(f"Project folder '{project_name}' not found on Drive.")
    sources_id = get_drive_folder_id(service, "0.sources", project_id)
    if not sources_id: raise FileNotFoundError(f"'0.sources' not found.")

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
    """Check if project on Drive lacks an .mp4 in 0.sources and is not already processing."""
    log(f"🔍 Checking Drive completion for: {project_name}")
    project_id = resolve_drive_project_id(service, project_name)
    if not project_id: return False
    sources_id = get_drive_folder_id(service, "0.sources", project_id)
    if not sources_id: return True
    if get_drive_file_id(service, STATUS_FILE_NAME, sources_id): return False
    results = service.files().list(q=f"'{sources_id}' in parents and name contains '.mp4' and trashed = false", fields="files(id, name)").execute()
    return not results.get('files', [])

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

def wait_for_visual_begin(monitor):
    log("🔍 Monitoring screen...")
    with mss.mss() as sct:
        time.sleep(1.0)
        base_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
        while True:
            time.sleep(1.0)
            curr_gray = cv2.cvtColor(np.array(sct.grab(monitor))[:, :, :3], cv2.COLOR_BGR2GRAY)
            if np.mean(cv2.absdiff(base_gray, curr_gray)) > 4.0:
                log("⏳ State change detected!")
                time.sleep(3.0)
                return True

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

def get_project_dir(service=None):
    start_date_str = None
    if AUTOMATION_STATE_FILE.exists():
        try:
            with open(AUTOMATION_STATE_FILE, "r") as f: start_date_str = json.load(f).get("latest_mp4_date")
        except: pass
    
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d") if start_date_str else datetime.now() - timedelta(days=7)
    
    # ── 1. Check Drive first (if service is available) ──
    if service:
        log("🔍 Checking Google Drive for pending projects (priority)...")
        for i in range(0, 120):
            current_date = start_date + timedelta(days=i)
            date_str     = current_date.strftime("%Y-%m-%d")
            project_name = f"{date_str}-project"
            if check_drive_project_needs_video(service, project_name):
                log(f"✨ Found project needing video (on Drive): {date_str}")
                return root_dir / current_date.strftime("%Y") / current_date.strftime("%m") / project_name
    
    # ── 2. Fallback to Local history search ──
    log("🔍 Falling back to local history search...")
    for i in range(0, 120):
        current_date = start_date + timedelta(days=i)
        date_str     = current_date.strftime("%Y-%m-%d")
        project_name = f"{date_str}-project"
        test_dir     = root_dir / current_date.strftime("%Y") / current_date.strftime("%m") / project_name
        
        if (test_dir / "0.sources" / STATUS_FILE_NAME).exists():
            log(f"   ⏩ Skipping {date_str} (locally marked as processing).")
            continue
            
        if test_dir.exists() and not list((test_dir / "0.sources").glob("*.mp4")):
            log(f"✨ Found local project needing video: {date_str}")
            return test_dir
            
    # Fallback to current project
    now = datetime.now()
    today_str = (now + timedelta(days=1 if now.hour >= 20 else -1 if now.hour <= 6 else 0)).strftime("%Y-%m-%d")
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    return root_dir / today_dt.strftime("%Y") / today_dt.strftime("%m") / f"{today_str}-project"

def main():
    parser = argparse.ArgumentParser(description="V-Process Automated Capture")
    parser.add_argument("--project", "-p", type=str)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--wait", "-w", type=float)
    parser.add_argument("--between", "-b", type=float, default=3.0)
    parser.add_argument("--start-delay", type=float, default=10.0)
    parser.add_argument("--cut-start", type=float, default=6.0)
    parser.add_argument("--cut-end", type=float, default=6.0)
    parser.add_argument("--upload", "-u", action="store_true")
    parser.add_argument("--convert", "-c", action="store_true")
    parser.add_argument("--input-raw", "-i", type=str)
    parser.add_argument("--no-gdrive", action="store_true")
    args = parser.parse_args()

    print("\n🚀 V-Process Runner Starting...")
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

    service = None
    if not args.project:
        try: service = get_drive_service()
        except: pass
        project_dir = get_project_dir(service)
    else:
        project_dir = Path(args.project)
        # Attempt to resolve project path if not absolute
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
        else:
            log(f"✅ Found local prompts for {project_dir.name}")

    log(f"📂 Using Project: {project_dir.name}")
    mark_project_processing(project_dir)
    prompts = parse_veo_prompts(project_dir / "0.sources" / "lyrics_with_prompts.md")
    if not prompts: return

    downloads_dir = project_dir / "0.sources" / "7.downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_video, out_video = str(downloads_dir / f"v_lib_raw_{ts}.mp4"), str(project_dir / "0.sources" / f"auto_{ts}.mp4")

    if not args.upload and not args.convert:
        finder = ScreenTemplateFinder(confidence_threshold=0.6)
        search_scales = [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
        pyautogui.click(x1, y1); time.sleep(3)
        pyautogui.hotkey('ctrl', 'l'); time.sleep(1); pyautogui.write(APP_URL); pyautogui.press('enter'); time.sleep(3)

        char_file, cover_img = project_dir / "0.sources" / "charactor.md", project_dir / "0.sources" / "cover.png"
        text = char_file.read_text(encoding="utf-8")[:300] if char_file.exists() else "Reference"
        finder.wait_and_click_template(str(v_lib_dir / "v_process" / "realtime" / "prompt_input.png"), timeout=5, scales=search_scales)
        pyautogui.write(text, interval=0.01); time.sleep(1)

        if cover_img.exists():
            if finder.wait_and_click_template(str(v_lib_dir / "v_process" / "realtime" / "image_reference.png"), timeout=10, scales=search_scales):
                time.sleep(1.5); pyautogui.write(os.path.abspath(cover_img)); time.sleep(1.5); pyautogui.press("enter"); time.sleep(1.5)
                finder.wait_and_click_template(str(v_lib_dir / "v_process" / "realtime" / "open_file.png"), timeout=10, scales=search_scales)
                time.sleep(8)

        finder.wait_and_click_template(str(v_lib_dir / "v_process" / "realtime" / "submit.png"), timeout=10, times=3, scales=search_scales)
        time.sleep(6)

        stop_event, is_recording, stats = threading.Event(), threading.Event(), {"total_frames": 0}
        threading.Thread(target=record_screen, args=(monitor, raw_video, 30, stop_event, is_recording, stats)).start()
        
        try:
            wait_for_visual_begin(monitor); is_recording.set()
            time.sleep(args.start_delay); session_start_time = time.time()
            for i, p in enumerate(prompts):
                pyautogui.click(text_x, text_y); time.sleep(0.2); pyautogui.hotkey("ctrl", "a"); pyautogui.press("delete"); pyautogui.write(p["text"], interval=0.01); pyautogui.press("enter")
                duration = max(3.0, args.wait or (time_to_sec(p["end"]) - time_to_sec(p["start"]) if p["end"] else (time_to_sec(prompts[i+1]["start"]) - time_to_sec(p["start"]) if i < len(prompts)-1 else 3.0)))
                t0 = time.time()
                while time.time() - t0 < duration:
                    if os.path.exists(STOP_SIGNAL_FILE): break
                    time.sleep(0.2)
                if i < len(prompts) - 1: time.sleep(args.between)
                if max(0, 270 - (time.time() - session_start_time)) < 10.0 or os.path.exists(STOP_SIGNAL_FILE): break
            wait_for_visual_end(monitor, session_start_time=session_start_time)
        finally: stop_event.set()

    if not args.upload:
        if args.convert: raw_video = args.input_raw or str(sorted(list(downloads_dir.glob("v_lib_raw_*.mp4")), key=os.path.getmtime, reverse=True)[0])
        cap = cv2.VideoCapture(raw_video); total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)); cap.release()
        target_duration = max(1.0, total_frames / 30.0 - args.cut_start - args.cut_end)
        sources_dir = project_dir / "0.sources"
        m0, m9, comb = sources_dir / "part_000.mp3", sources_dir / "part_000 (9).mp3", sources_dir / f"{project_dir.name}_combined.mp3"
        if not comb.exists() and m0.exists() and m9.exists():
            os.system(f'ffmpeg -y -i "{m0}" -i "{m9}" -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1[a]" -map "[a]" "{comb}"')
        audio = comb if comb.exists() else (list(sources_dir.glob("*.mp3"))[0] if list(sources_dir.glob("*.mp3")) else None)
        
        w_wm, h_wm, margin = 200, 70, 10
        tr, br = (monitor["width"]-w_wm-margin, margin), (monitor["width"]-w_wm-margin, monitor["height"]-h_wm-margin)
        delogo = ",".join([f"delogo=x={x}:y={y}:w={w_wm}:h={h_wm}" for x,y in [tr, br] if x>=0 and y>=0]) or "null"
        
        cmd = f'ffmpeg -y -ss {args.cut_start} -i "{raw_video}" ' + (f'-i "{audio}" -filter_complex "[0:v]{delogo}[v];[1:a]afade=t=out:st={round(target_duration-5, 3)}:d=5[a]" -map "[v]" -map "[a]" ' if audio else f'-vf "{delogo}" ') + f'-t {round(target_duration, 3)} -c:v libx264 -pix_fmt yuv420p -crf 18 -preset fast -c:a aac -b:a 192k "{out_video}"'
        os.system(cmd)
        if os.path.exists(out_video): update_automation_state(project_dir.name.split("-project")[0])

    if not args.no_gdrive:
        parent = os.environ.get("GDRIVE_PARENT", "2026-03")
        upload_to_gdrive(out_video, project_dir.name+"/0.sources", parent)
    print("✅ Done.")

if __name__ == "__main__": main()
