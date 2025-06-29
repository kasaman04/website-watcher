import asyncio
import json
import logging
import smtplib
import hashlib
import os
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Dict, Optional
import requests
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import uvicorn
from dotenv import load_dotenv

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('watcher.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 環境変数読み込み
load_dotenv()

app = FastAPI(title="Website Watcher", description="シンプルなサイト更新監視システム")

# グローバル変数
monitoring_task = None
sites_data = []

class Site(BaseModel):
    url: str
    email: str
    name: Optional[str] = ""

class EmailService:
    """確実なメール送信サービス"""
    
    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.username = os.getenv("SMTP_USERNAME", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.from_email = os.getenv("FROM_EMAIL", "")
    
    def send_email(self, to_email: str, subject: str, body: str) -> bool:
        """メール送信（確実性重視）"""
        if not all([self.username, self.password, self.from_email]):
            logger.error("Gmail設定が不完全です。.envファイルを確認してください。")
            return False
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"メール送信試行 {attempt + 1}/{max_retries}: {to_email}")
                
                # メール作成
                msg = MIMEMultipart()
                msg['From'] = self.from_email
                msg['To'] = to_email
                msg['Subject'] = subject
                msg.attach(MIMEText(body, 'plain', 'utf-8'))
                
                # SMTP接続・送信
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    server.starttls()
                    server.login(self.username, self.password)
                    server.send_message(msg)
                
                logger.info(f"✅ メール送信成功: {to_email}")
                return True
                
            except Exception as e:
                logger.error(f"❌ メール送信失敗 (試行{attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(2)  # リトライ前に待機
        
        logger.error(f"❌ メール送信完全失敗: {to_email}")
        return False
    
    def test_connection(self) -> bool:
        """SMTP接続テスト"""
        try:
            logger.info("Gmail SMTP接続テスト開始...")
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
            logger.info("✅ Gmail SMTP接続成功")
            return True
        except Exception as e:
            logger.error(f"❌ Gmail SMTP接続失敗: {e}")
            return False

class SiteChecker:
    """サイトチェッククラス"""
    
    @staticmethod
    def get_site_hash(url: str, timeout: int = 10) -> Optional[str]:
        """サイトのハッシュ値を取得"""
        try:
            logger.info(f"サイトチェック開始: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, timeout=timeout, headers=headers)
            response.raise_for_status()
            
            # ハッシュ計算
            content_hash = hashlib.md5(response.text.encode('utf-8')).hexdigest()
            logger.info(f"✅ サイトチェック成功: {url} (hash: {content_hash[:8]}...)")
            return content_hash
            
        except Exception as e:
            logger.error(f"❌ サイトチェック失敗: {url} - {e}")
            return None

# サービスインスタンス
email_service = EmailService()
site_checker = SiteChecker()

def load_sites() -> List[Dict]:
    """サイトデータ読み込み"""
    try:
        if os.path.exists('config.json'):
            with open('config.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('sites', [])
    except Exception as e:
        logger.error(f"設定ファイル読み込みエラー: {e}")
    return []

def save_sites(sites: List[Dict]):
    """サイトデータ保存"""
    try:
        config = {'sites': sites, 'settings': {'check_interval': 300, 'timeout': 10, 'max_retries': 3}}
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info("設定ファイル保存完了")
    except Exception as e:
        logger.error(f"設定ファイル保存エラー: {e}")

async def check_all_sites():
    """全サイトチェック（メイン監視ループ）"""
    global sites_data
    
    sites_data = load_sites()
    if not sites_data:
        logger.info("監視対象サイトがありません")
        return
    
    logger.info(f"🔍 {len(sites_data)}サイトの監視を開始")
    
    for site in sites_data:
        try:
            url = site['url']
            email = site['email']
            name = site.get('name', url)
            last_hash = site.get('hash', '')
            
            # サイトチェック
            current_hash = site_checker.get_site_hash(url)
            if not current_hash:
                continue
            
            # 初回チェック
            if not last_hash:
                site['hash'] = current_hash
                site['last_check'] = datetime.now().isoformat()
                logger.info(f"📝 初回ハッシュ設定: {name}")
                continue
            
            # 変更検知
            if current_hash != last_hash:
                logger.info(f"🚨 変更検知: {name}")
                
                # メール送信
                subject = f"🔔 サイト更新通知: {name}"
                body = f"""
サイトが更新されました！

サイト名: {name}
URL: {url}
更新検知時刻: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}

このメールは Website Watcher により自動送信されました。
"""
                
                success = email_service.send_email(email, subject, body)
                if success:
                    # ハッシュ更新
                    site['hash'] = current_hash
                    site['last_check'] = datetime.now().isoformat()
                    site['last_notified'] = datetime.now().isoformat()
                    logger.info(f"✅ 通知完了: {name} → {email}")
                else:
                    logger.error(f"❌ 通知失敗: {name} → {email}")
            else:
                site['last_check'] = datetime.now().isoformat()
                logger.info(f"📍 変更なし: {name}")
            
            # 少し待機（サーバー負荷軽減）
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"サイトチェックエラー: {e}")
    
    # 設定保存
    save_sites(sites_data)

async def monitoring_loop():
    """監視ループ"""
    logger.info("🔄 監視ループ開始")
    
    while True:
        try:
            await check_all_sites()
            check_interval = int(os.getenv("CHECK_INTERVAL", "300"))
            logger.info(f"⏰ {check_interval}秒後に再チェック")
            await asyncio.sleep(check_interval)
        except Exception as e:
            logger.error(f"監視ループエラー: {e}")
            await asyncio.sleep(60)  # エラー時は1分待機

# 静的ファイル配信
import os
app_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(app_dir, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# FastAPI エンドポイント
@app.on_event("startup")
async def startup_event():
    """アプリ起動時の処理"""
    global monitoring_task
    
    logger.info("🚀 Website Watcher 起動")
    
    # SMTP接続テスト
    if email_service.test_connection():
        logger.info("✅ メール設定確認完了")
    else:
        logger.warning("⚠️ メール設定に問題があります")
    
    # 監視タスク開始
    monitoring_task = asyncio.create_task(monitoring_loop())

@app.on_event("shutdown")
async def shutdown_event():
    """アプリ終了時の処理"""
    global monitoring_task
    if monitoring_task:
        monitoring_task.cancel()
    logger.info("👋 Website Watcher 終了")

@app.get("/", response_class=HTMLResponse)
async def root():
    """メインページ"""
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.get("/api/sites")
async def get_sites():
    """サイト一覧取得"""
    return {"sites": load_sites()}

@app.post("/api/sites")
async def add_site(site: Site):
    """サイト追加"""
    sites = load_sites()
    
    # 重複チェック
    for existing_site in sites:
        if existing_site['url'] == site.url:
            raise HTTPException(status_code=400, detail="このURLは既に登録されています")
    
    # 新サイト追加
    new_site = {
        "url": site.url,
        "email": site.email,
        "name": site.name or site.url,
        "hash": "",
        "created_at": datetime.now().isoformat()
    }
    
    sites.append(new_site)
    save_sites(sites)
    
    logger.info(f"📝 新サイト登録: {site.name or site.url}")
    return {"message": "サイトを登録しました", "site": new_site}

@app.delete("/api/sites/{site_index}")
async def delete_site(site_index: int):
    """サイト削除"""
    sites = load_sites()
    
    if 0 <= site_index < len(sites):
        deleted_site = sites.pop(site_index)
        save_sites(sites)
        logger.info(f"🗑️ サイト削除: {deleted_site.get('name', deleted_site['url'])}")
        return {"message": "サイトを削除しました"}
    else:
        raise HTTPException(status_code=404, detail="サイトが見つかりません")

@app.post("/api/test-email")
async def test_email(email_data: dict):
    """テストメール送信"""
    to_email = email_data.get("email")
    if not to_email:
        raise HTTPException(status_code=400, detail="メールアドレスが必要です")
    
    subject = "📧 Website Watcher テストメール"
    body = f"""
このメールは Website Watcher のテスト送信です。

送信時刻: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}

このメールが届いていれば、メール設定は正常です。
"""
    
    success = email_service.send_email(to_email, subject, body)
    if success:
        return {"message": "テストメールを送信しました"}
    else:
        raise HTTPException(status_code=500, detail="メール送信に失敗しました")

@app.post("/api/check-now")
async def check_now():
    """手動チェック実行"""
    logger.info("🔍 手動チェック実行")
    await check_all_sites()
    return {"message": "チェックを実行しました"}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8888, log_level="info")