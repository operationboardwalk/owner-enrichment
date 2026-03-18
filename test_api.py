"""Quick RocketReach API test — run on the server to diagnose enrichment issues."""
import sys
import requests

API_KEY = sys.argv[1] if len(sys.argv) > 1 else ""
if not API_KEY:
    print("Usage: python3 test_api.py <your_api_key>")
    sys.exit(1)

BASE = "https://api.rocketreach.co/api/v2"
HDRS = {"Api-Key": API_KEY, "Content-Type": "application/json"}

def test(label, url, params):
    print(f"\n--- {label} ---")
    print(f"GET {url} params={params}")
    try:
        r = requests.get(url, params=params, headers=HDRS, timeout=20)
        print(f"HTTP {r.status_code}")
        print(r.text[:500])
    except Exception as e:
        print(f"ERROR: {e}")

# Test account/validity
test("Account check", f"{BASE}/account", {})

# Test person lookup by name+location
test("Person lookup: Christina Ahumada", f"{BASE}/person/lookup",
     {"name": "Christina Ahumada", "location_city": "Long Beach", "location_state": "CA"})

# Test person search (POST)
print(f"\n--- Person search POST: Jim Louis ---")
body = {"query": {"name": ["Jim Louis"], "current_employer": ["Long Beach Rescue Mission"]}, "start": 1, "page_size": 3}
try:
    r = requests.post(f"{BASE}/person/search", json=body, headers=HDRS, timeout=20)
    print(f"HTTP {r.status_code}")
    print(r.text[:500])
except Exception as e:
    print(f"ERROR: {e}")

# Test name-only search
print(f"\n--- Person search POST: Christina Ahumada (name+location) ---")
body2 = {"query": {"name": ["Christina Ahumada"], "location_city": ["Long Beach"], "location_region": ["CA"]}, "start": 1, "page_size": 3}
try:
    r2 = requests.post(f"{BASE}/person/search", json=body2, headers=HDRS, timeout=20)
    print(f"HTTP {r2.status_code}")
    print(r2.text[:500])
except Exception as e:
    print(f"ERROR: {e}")
