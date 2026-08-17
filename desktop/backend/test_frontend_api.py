import requests, json, sys
BASE = 'http://127.0.0.1:54114/api'

def ok(resp):
    return resp.status_code < 400

# login owner
creds = {'email': 'owner@robohub.local', 'password': 'Robotics2026!'}
r = requests.post(f'{BASE}/auth/login', json=creds)
print('login', r.status_code, r.text[:120])
if not ok(r):
    sys.exit(1)
token = r.json()['token']
headers = {'Authorization': f'Bearer {token}'}

# me
r = requests.get(f'{BASE}/auth/me', headers=headers)
print('me', r.status_code, r.json().get('role'))

# dashboard
r = requests.get(f'{BASE}/dashboard', headers=headers)
print('dashboard', r.status_code, r.json())

# channels
r = requests.get(f'{BASE}/channels', headers=headers)
print('channels', r.status_code, len(r.json()))
channels = r.json()
if not channels:
    sys.exit('no channels')
chan = channels[0]

# channel messages
r = requests.get(f'{BASE}/channels/{chan["id"]}/messages', headers=headers)
print('channel messages', r.status_code, len(r.json()))

# send message
r = requests.post(f'{BASE}/channels/{chan["id"]}/messages', headers=headers, json={'text': 'test message from sweep'})
print('send message', r.status_code, r.text[:120])

# create member
r = requests.post(f'{BASE}/users', headers=headers, json={'email': 'sweeptest@robohub.local', 'name': 'Sweep Test', 'password': 'SweepTest123!', 'role': 'member'})
print('create user', r.status_code, r.text[:120])
if ok(r):
    user = r.json()
    # edit permissions
    perms = {k: True for k in ['can_chat','can_upload_files','can_view_members_only','can_edit_calendar','can_manage_todos','can_delete_any_message','can_delete_any_file','can_manage_members']}
    r = requests.put(f'{BASE}/users/{user["id"]}/permissions', headers=headers, json={'permissions': perms})
    print('update permissions', r.status_code, r.text[:120])
    # fetch user permissions
    r = requests.get(f'{BASE}/users/{user["id"]}/permissions', headers=headers)
    print('fetch user perms', r.status_code, r.json().get('permissions'))

print('all API checks done')
