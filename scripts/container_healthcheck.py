import base64
import os
import urllib.request


request = urllib.request.Request("http://127.0.0.1:8090/api/health")
credentials = f"{os.environ['PLATFORM_USERNAME']}:{os.environ['PLATFORM_PASSWORD']}"
token = base64.b64encode(credentials.encode()).decode()
request.add_header("Authorization", f"Basic {token}")
urllib.request.urlopen(request, timeout=5).read()
