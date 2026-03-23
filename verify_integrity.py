import hashlib, re

def check(fn):
    with open(fn, 'rb') as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    print(f"File: {fn} | SHA256: {digest[:16]}...{digest[-8:]}")
    
    # Check for interleaving (common patterns like "else:t.dataframe")
    text = data.decode('utf-8', errors='replace')
    bad = re.findall(r'else:[a-z]+', text)
    if bad:
        print(f"!!! POSSIBLE CORRUPTION DETECTED: {bad}")
    else:
        print("No obvious interleaving detected.")
    
    # Show last 5 lines exactly
    lines = [l for l in text.splitlines() if l.strip()]
    print("--- LAST 5 NON-EMPTY LINES ---")
    for l in lines[-5:]:
        print(f"| {l}")
    print("-" * 30)

check('app.py')
check('bot.py')
