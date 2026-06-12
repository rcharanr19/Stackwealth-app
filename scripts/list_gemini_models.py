import os
import sys

try:
    from google import genai
except Exception as e:
    print("Missing dependency 'google-genai'. Install with: pip install google-genai")
    sys.exit(2)

# Try env var first
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    # Try reading Streamlit secrets file
    secrets_path = os.path.join('.streamlit', 'secrets.toml')
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if 'GEMINI_API_KEY' in line:
                        # naive parse of TOML line like: GEMINI_API_KEY = "value"
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            val = parts[1].strip().strip('"').strip("'")
                            if val:
                                api_key = val
                                break
        except Exception:
            pass

if not api_key:
    print('No GEMINI_API_KEY found in environment or .streamlit/secrets.toml')
    sys.exit(3)

client = genai.Client(api_key=api_key)

try:
    models = client.models.list()
except Exception as e:
    print('Failed to list models:', e)
    sys.exit(4)

for m in models:
    try:
        name = getattr(m, 'name', repr(m))
        supported = getattr(m, 'supportedMethods', None)
        print(name, 'supportedMethods=', supported)
    except Exception:
        print(repr(m))

print('\nDone.')
