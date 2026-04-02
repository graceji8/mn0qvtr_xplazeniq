import os
import sys
import argparse
import pickle
import io
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive.readonly', 'https://www.googleapis.com/auth/drive.metadata.readonly']

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
            creds.refresh(Request())
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

def get_drive_file_id(service, file_name, parent_id=None):
    query = f"name = '{file_name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project_name', type=str, help='Project folder name to search for')
    parser.add_argument('--out', type=str, default='.', help='Local directory to save the file')
    args = parser.parse_args()

    creds = get_credentials()
    if not creds:
        sys.exit(1)
        
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)

    project_folder_id = get_drive_folder_id(service, args.project_name)
    if not project_folder_id:
        sys.exit(1)

    sources_folder_id = get_drive_folder_id(service, "0.sources", project_folder_id)
    if not sources_folder_id:
        sys.exit(1)
        
    files_to_download = ["lyrics_with_prompts.md", "charactor.md", "cover.png"]
    os.makedirs(args.out, exist_ok=True)

    for file_name in files_to_download:
        file_id = get_drive_file_id(service, file_name, sources_folder_id)
        if not file_id:
            if file_name == "lyrics_with_prompts.md":
                sys.exit(1)
            continue

        out_path = os.path.join(args.out, file_name)
        request = service.files().get_media(fileId=file_id)
        with open(out_path, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
    print("Download complete.")

if __name__ == '__main__':
    main()
