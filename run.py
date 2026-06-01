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
    )
