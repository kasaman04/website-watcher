#!/usr/bin/env python3
import os
import sys

# 作業ディレクトリをアプリディレクトリに変更
app_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(app_dir)

# アプリを起動
sys.path.insert(0, app_dir)
from app import app
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")