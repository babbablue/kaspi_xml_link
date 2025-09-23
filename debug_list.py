import os
import json
import base64
from google.oauth2 import service_account
from googleapiclient.discovery import build

folder_id = os.getenv('GDRIVE_FOLDER_ID')
if not folder_id:
    raise SystemExit('GDRIVE_FOLDER_ID not set')

# choose credentials
creds_file = None
creds_env = os.getenv('GDRIVE_CREDENTIALS')
if creds_env:
    if os.path.exists(creds_env):
        creds_file = creds_env
    else:
        try:
            parsed = json.loads(creds_env)
            creds_file = 'credentials_from_env.json'
            with open(creds_file, 'w', encoding='utf-8') as f:
                json.dump(parsed, f, ensure_ascii=False)
        except Exception:
            try:
                decoded = base64.b64decode(creds_env).decode('utf-8')
                parsed = json.loads(decoded)
                creds_file = 'credentials_from_env.json'
                with open(creds_file, 'w', encoding='utf-8') as f:
                    json.dump(parsed, f, ensure_ascii=False)
            except Exception as e:
                raise SystemExit('GDRIVE_CREDENTIALS set but not valid JSON/path/base64: %s' % e)
else:
    if os.path.exists('credentials.json'):
        creds_file = 'credentials.json'

if not creds_file or not os.path.exists(creds_file):
    raise SystemExit('No credentials file found (GDRIVE_CREDENTIALS or credentials.json)')

print('Using credentials file:', creds_file)
with open(creds_file, 'r', encoding='utf-8') as f:
    jd = json.load(f)
    print('client_email:', jd.get('client_email'))

creds = service_account.Credentials.from_service_account_file(creds_file, scopes=['https://www.googleapis.com/auth/drive'])
service = build('drive', 'v3', credentials=creds)

# get folder meta
try:
    meta = service.files().get(fileId=folder_id, fields='id,name,mimeType,owners', supportsAllDrives=True).execute()
    print('\nFolder metadata:')
    print('id:', meta.get('id'))
    print('name:', meta.get('name'))
    print('mimeType:', meta.get('mimeType'))
    print('owners:', meta.get('owners'))

    perms = service.permissions().list(fileId=folder_id, supportsAllDrives=True, fields='permissions(id,type,role,emailAddress)').execute()
    print('\nPermissions:')
    for p in perms.get('permissions', []):
        print(p)

    # list files in folder
    print('\nFiles in folder:')
    res = service.files().list(q=f"'{folder_id}' in parents",
                               spaces='drive',
                               fields='files(id,name,mimeType)',
                               supportsAllDrives=True,
                               pageSize=100).execute()
    files = res.get('files', [])
    if not files:
        print('No files found (folder may be empty)')
    else:
        for f in files:
            print(f)

except Exception as e:
    print('Error when fetching folder info or listing files:', e)
    raise
