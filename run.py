"""Convenience launcher for local development:  python run.py

In production, run uvicorn directly (see deploy/hyperfixed-web.service) with a
single worker.

--proxy-headers is OFF by default and only turned on by passing it here. It
makes uvicorn rewrite the client address from X-Forwarded-For, trusting
whatever the peer sends; in dev there is no proxy, so a browser could spoof its
IP and dodge per-IP limits. The app does its own trusted-proxy handling
(TRUSTED_PROXY in .env), so production does not use the flag either.
"""

import argparse

import uvicorn

from app import config

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the dev server.")
    ap.add_argument(
        "--proxy-headers",
        action="store_true",
        help="trust X-Forwarded-* from the peer (only behind a proxy you control)",
    )
    args = ap.parse_args()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        root_path=config.ROOT_PATH,
        reload=True,
        reload_dirs=["app"],  # don't reload when the SQLite db in data/ is written
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips="127.0.0.1",
    )
