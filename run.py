"""Convenience launcher for local development:  python run.py

In production, run uvicorn directly (see deploy/hyperfixed-web.service).
"""
import uvicorn
from app import config

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        root_path=config.ROOT_PATH,
        reload=True,
        reload_dirs=["app"],  # don't reload when the SQLite db in data/ is written
    )
