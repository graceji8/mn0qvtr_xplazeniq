import os
import sys
import pickle
import argparse
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/drive.metadata.readonly']

def get_credentials():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            try:
                creds = pickle.load(token)
            except Exception:
                creds = None
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        else:
            if not os.path.exists('credentials.json'):
                return None
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return creds

def get_drive_folder_id(service, folder_name, parent_id=None):
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def check_file_exists(service, file_name, parent_id):
    query = f"name = '{file_name}' and '{parent_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    return len(results.get('files', [])) > 0

def check_any_video_exists(service, parent_id):
    query = f"'{parent_id}' in parents and mimeType contains 'video/' and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    return len(results.get('files', [])) > 0

def main():
    parser = argparse.ArgumentParser(description="Find the first project missing a result in Google Drive.")
    parser.add_argument("--month", type=str, help="Limit search to a specific month (e.g. '09')")
    args = parser.parse_args()

    creds = get_credentials()
    if not creds:
        sys.exit(1)
        
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    
    query = "name contains '-project' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)", pageSize=1000).execute()
    folder_list = results.get('files', [])
    
    if args.month:
        folder_list = [f for f in folder_list if f['name'].split('-')[1] == args.month]
    
    folder_list.sort(key=lambda x: x['name'])
    
    for folder in folder_list:
        project_id = folder['id']
        project_name = folder['name']
        sources_id = get_drive_folder_id(service, "0.sources", project_id)
        if not sources_id: continue
        has_prompts = check_file_exists(service, "lyrics_with_prompts.md", sources_id)
        if not has_prompts: continue
        has_video = check_any_video_exists(service, sources_id)
        if has_video: continue
        downloads_id = get_drive_folder_id(service, "7.downloads", sources_id)
        if downloads_id:
            has_video = check_any_video_exists(service, downloads_id)
            if has_video: continue
        print(project_name)
        return

if __name__ == '__main__':
    main()
