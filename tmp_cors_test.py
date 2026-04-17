import requests

r = requests.get('http://127.0.0.1:8000/ping', timeout=10)
print('ping', r.status_code, r.headers.get('access-control-allow-origin'), r.text)

try:
    r2 = requests.get('http://127.0.0.1:8000/search-memory?query=java', timeout=30)
    print('search', r2.status_code, r2.headers.get('access-control-allow-origin'), r2.text[:400])
except Exception as e:
    print('search error', repr(e))
