"""Start the Class Recorder website.  Hosting services set PORT automatically."""
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

for var in ("SITE_PASSWORD", "DEVICE_TOKEN"):
    if not os.environ.get(var):
        print(f"WARNING: {var} is not set. Set it in your hosting dashboard (Environment) or in .env")
if not os.environ.get("DEVICE_TOKEN"):
    os.environ["DEVICE_TOKEN"] = secrets.token_urlsafe(16)
    print("Temporary DEVICE_TOKEN for this run:", os.environ["DEVICE_TOKEN"])

import uvicorn  # noqa: E402
from webapp.app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Class Recorder running on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*", log_level="warning")
