import requests
import base64
import json
import time

api_key = "13295095ZVfnoYFeegwJhdWsZoiUayaUWYCYy"
api_secret = "OogIuvZnqds73Vx-INBtImwJ2dNl_wetFtKJTHIkp-g"

auth_string = f"{api_key}:{api_secret}"
encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')

headers = {
    "Authorization": f"Basic {encoded_auth}",
    "Accept": "application/json"
}

# Check specific tickers via metadata endpoint (if supported) or just search in full list again with delay
url = "https://demo.trading212.com/api/v0/equity/metadata/instruments"

print("Waiting 30s for rate limit...")
time.sleep(30)

try:
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    instruments = resp.json()
    
    check_list = ["NVDA_US_EQ", "AAPL_US_EQ", "RGTI_US_EQ", "RGTI_US", "RGTI"]
    found = [i for i in instruments if i['ticker'] in check_list]
    print(f"Results: {json.dumps(found, indent=2)}")
    
except Exception as e:
    print(f"Error: {e}")
