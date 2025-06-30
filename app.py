import asyncio
import json
import logging
import hashlib
import os
import time
import weakref
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Dict, Optional, Set
from logging.handlers import RotatingFileHandler
import httpx
import aiosmtplib
from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pydantic import BaseModel, ValidationError
import uvicorn
from dotenv import load_dotenv

# ログ設定 - ローテーション付き
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# ファイルハンドラ (最大10MB、5つまでバックアップ)
file_handler = RotatingFileHandler(
    'watcher.log', 
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5,
    encoding='utf-8'
)
file_handler.setFormatter(log_formatter)

# コンソールハンドラ
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

# ルートロガー設定
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

# 環境変数読み込み
load_dotenv()

# レート制限設定
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Website Watcher", description="高信頼性サイト更新監視システム")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# セキュリティ設定
security = HTTPBasic()
ADMIN_PASSWORD = "1033"

# セッション管理（簡易版）
active_sessions = set()

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# グローバル変数
monitoring_task = None
sites_data = []
httpx_client = None
cache = {}
cache_ttl = {}
failed_sites: Set[str] = set()
metrics = {
    'total_checks': 0,
    'failed_checks': 0,
    'email_sent': 0,
    'email_failed': 0,
    'last_check_time': None,
    'uptime_start': datetime.now(),
    'circuit_breaker_active': 0
}

class Site(BaseModel):
    url: str
    email: str
    name: Optional[str] = ""

class CircuitBreaker:
    """サーキットブレーカーパターン実装"""
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 300):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    
    def call(self, func):
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.timeout:
                self.state = "HALF_OPEN"
                logger.info("サーキットブレーカー: HALF_OPEN状態")
            else:
                raise Exception("サーキットブレーカー: OPEN状態")
        
        try:
            result = func()
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                self.failure_count = 0
                logger.info("サーキットブレーカー: CLOSED状態に復旧")
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.failure_count >= self.failure_threshold:
                self.state = "OPEN"
                logger.warning(f"サーキットブレーカー: OPEN状態 ({self.failure_count}回失敗)")
                metrics['circuit_breaker_active'] += 1
            raise e

class AsyncEmailService:
    """非同期メール送信サービス（サーキットブレーカー付き）"""
    
    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.username = os.getenv("SMTP_USERNAME", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.from_email = os.getenv("FROM_EMAIL", "")
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, timeout=300)
    
    async def send_email(self, to_email: str, subject: str, body: str) -> bool:
        """非同期メール送信"""
        if not all([self.username, self.password, self.from_email]):
            logger.error("Gmail設定が不完全です")
            return False
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self._send_with_circuit_breaker(to_email, subject, body)
                logger.info(f"✅ メール送信成功: {to_email}")
                metrics['email_sent'] += 1
                return True
                
            except Exception as e:
                logger.error(f"❌ メール送信失敗 (試行{attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # 指数バックオフ
        
        metrics['email_failed'] += 1
        return False
    
    async def _send_with_circuit_breaker(self, to_email: str, subject: str, body: str):
        """サーキットブレーカー付きメール送信"""
        def send_func():
            return asyncio.create_task(self._send_email_core(to_email, subject, body))
        
        task = self.circuit_breaker.call(send_func)
        await task
    
    async def _send_email_core(self, to_email: str, subject: str, body: str):
        """コアメール送信機能"""
        msg = MIMEMultipart()
        msg['From'] = self.from_email
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        await aiosmtplib.send(
            msg,
            hostname=self.smtp_server,
            port=self.smtp_port,
            start_tls=True,
            username=self.username,
            password=self.password,
            timeout=30
        )
    
    async def test_connection(self) -> bool:
        """SMTP接続テスト"""
        try:
            logger.info("Gmail SMTP接続テスト開始...")
            async with aiosmtplib.SMTP(hostname=self.smtp_server, port=self.smtp_port) as server:
                await server.starttls()
                await server.login(self.username, self.password)
            logger.info("✅ Gmail SMTP接続成功")
            return True
        except Exception as e:
            logger.error(f"❌ Gmail SMTP接続失敗: {e}")
            return False

class AsyncSiteChecker:
    """非同期サイトチェッククラス"""
    
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.site_circuits = {}  # サイト別サーキットブレーカー
    
    async def get_site_hash(self, url: str, timeout: int = 10) -> Optional[str]:
        """非同期サイトハッシュ取得"""
        if url not in self.site_circuits:
            self.site_circuits[url] = CircuitBreaker(failure_threshold=3, timeout=180)
        
        try:
            def check_func():
                return self._check_site_core(url, timeout)
            
            content_hash = await self.site_circuits[url].call(check_func)
            logger.info(f"✅ サイトチェック成功: {url} (hash: {content_hash[:8]}...)")
            return content_hash
            
        except Exception as e:
            logger.error(f"❌ サイトチェック失敗: {url} - {e}")
            metrics['failed_checks'] += 1
            return None
    
    async def _check_site_core(self, url: str, timeout: int) -> str:
        """コアサイトチェック機能"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = await self.client.get(url, timeout=timeout, headers=headers)
        response.raise_for_status()
        
        content_hash = hashlib.md5(response.text.encode('utf-8')).hexdigest()
        metrics['total_checks'] += 1
        return content_hash

# サービスインスタンス
email_service = AsyncEmailService()
site_checker = None  # 後で初期化

# 認証関数
def verify_password(credentials: HTTPBasicCredentials = Depends(security)):
    """パスワード認証"""
    if credentials.password != ADMIN_PASSWORD:
        raise HTTPException(
            status_code=401,
            detail="パスワードが間違っています",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def create_session_token():
    """セッショントークン生成"""
    import secrets
    token = secrets.token_urlsafe(32)
    active_sessions.add(token)
    return token

def verify_session(request: Request):
    """セッション確認"""
    session_token = request.cookies.get("session_token")
    if not session_token or session_token not in active_sessions:
        raise HTTPException(status_code=401, detail="認証が必要です")

def get_cached_data(key: str, ttl_seconds: int = 30):
    """キャッシュデータ取得"""
    if key in cache and key in cache_ttl:
        if time.time() - cache_ttl[key] < ttl_seconds:
            return cache[key]
    return None

def set_cached_data(key: str, data):
    """キャッシュデータ設定"""
    cache[key] = data
    cache_ttl[key] = time.time()

def load_sites() -> List[Dict]:
    """サイトデータ読み込み（原子性保証）"""
    try:
        # キャッシュチェック
        cached = get_cached_data('sites_config')
        if cached:
            return cached
        
        # Render.comのディスクパスを優先
        config_path = '/opt/render/project/data/config.json'
        if not os.path.exists(config_path):
            config_path = 'config.json'
        
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                sites = data.get('sites', [])
                set_cached_data('sites_config', sites)
                return sites
    except Exception as e:
        logger.error(f"設定ファイル読み込みエラー: {e}")
    return []

def save_sites(sites: List[Dict]):
    """サイトデータ保存（原子性保証）"""
    try:
        config = {
            'sites': sites, 
            'settings': {
                'check_interval': int(os.getenv('CHECK_INTERVAL', '300')), 
                'timeout': 10, 
                'max_retries': 3
            }
        }
        
        # Render.comのディスクパスを優先
        config_path = '/opt/render/project/data/config.json'
        config_dir = os.path.dirname(config_path)
        
        # ディレクトリが存在しない場合は作成
        if not os.path.exists(config_dir):
            try:
                os.makedirs(config_dir, exist_ok=True)
            except:
                config_path = 'config.json'
        
        # 一時ファイルに書き込み → 原子的リネーム
        temp_file = config_path + '.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        os.replace(temp_file, config_path)
        
        # キャッシュ更新
        set_cached_data('sites_config', sites)
        logger.info("設定ファイル保存完了")
        
    except Exception as e:
        logger.error(f"設定ファイル保存エラー: {e}")

async def check_all_sites():
    """全サイトチェック（並列処理）"""
    global sites_data
    
    sites_data = load_sites()
    if not sites_data:
        logger.info("監視対象サイトがありません")
        return
    
    logger.info(f"🔍 {len(sites_data)}サイトの監視を開始")
    metrics['last_check_time'] = datetime.now()
    
    # 最大5サイト並列処理
    semaphore = asyncio.Semaphore(5)
    tasks = []
    
    for site in sites_data:
        task = asyncio.create_task(check_single_site(site, semaphore))
        tasks.append(task)
    
    # 全サイトチェック完了を待機
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # 設定保存
    save_sites(sites_data)

async def check_single_site(site: Dict, semaphore: asyncio.Semaphore):
    """単一サイトチェック"""
    async with semaphore:
        try:
            url = site['url']
            email = site['email']
            name = site.get('name', url)
            last_hash = site.get('hash', '')
            
            # サイトチェック
            current_hash = await site_checker.get_site_hash(url)
            if not current_hash:
                return
            
            # 初回チェック
            if not last_hash:
                site['hash'] = current_hash
                site['last_check'] = datetime.now().isoformat()
                logger.info(f"📝 初回ハッシュ設定: {name}")
                return
            
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
                
                success = await email_service.send_email(email, subject, body)
                if success:
                    site['hash'] = current_hash
                    site['last_check'] = datetime.now().isoformat()
                    site['last_notified'] = datetime.now().isoformat()
                    logger.info(f"✅ 通知完了: {name} → {email}")
                else:
                    logger.error(f"❌ 通知失敗: {name} → {email}")
            else:
                site['last_check'] = datetime.now().isoformat()
                logger.info(f"📍 変更なし: {name}")
            
            # 負荷軽減用待機
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"サイトチェックエラー: {e}")

async def monitoring_loop():
    """監視ループ（指数バックオフ付き）"""
    logger.info("🔄 監視ループ開始")
    consecutive_failures = 0
    
    while True:
        try:
            await check_all_sites()
            consecutive_failures = 0  # 成功時リセット
            
            check_interval = int(os.getenv("CHECK_INTERVAL", "300"))
            logger.info(f"⏰ {check_interval}秒後に再チェック")
            await asyncio.sleep(check_interval)
            
        except Exception as e:
            consecutive_failures += 1
            wait_time = min(60 * (2 ** consecutive_failures), 3600)  # 最大1時間
            logger.error(f"監視ループエラー (連続{consecutive_failures}回): {e}")
            logger.info(f"⏰ {wait_time}秒後にリトライ")
            await asyncio.sleep(wait_time)

async def task_monitor():
    """タスク監視（自動復旧）"""
    global monitoring_task
    
    while True:
        try:
            await asyncio.sleep(60)  # 1分ごとチェック
            
            if monitoring_task and monitoring_task.done():
                logger.warning("⚠️ 監視タスクが停止。再起動します")
                monitoring_task = asyncio.create_task(monitoring_loop())
                
        except Exception as e:
            logger.error(f"タスク監視エラー: {e}")

# 静的ファイル配信
app_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(app_dir, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# FastAPI エンドポイント
@app.on_event("startup")
async def startup_event():
    """アプリ起動時の処理"""
    global monitoring_task, httpx_client, site_checker
    
    logger.info("🚀 Website Watcher 起動")
    
    # HTTPクライアント初期化
    httpx_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
    )
    site_checker = AsyncSiteChecker(httpx_client)
    
    # 設定検証
    required_env = ['SMTP_USERNAME', 'SMTP_PASSWORD', 'FROM_EMAIL']
    missing_env = [env for env in required_env if not os.getenv(env)]
    if missing_env:
        logger.warning(f"⚠️ 未設定の環境変数: {missing_env}")
    
    # SMTP接続テスト
    if await email_service.test_connection():
        logger.info("✅ メール設定確認完了")
    else:
        logger.warning("⚠️ メール設定に問題があります")
    
    # 監視タスク開始
    monitoring_task = asyncio.create_task(monitoring_loop())
    asyncio.create_task(task_monitor())

@app.on_event("shutdown")
async def shutdown_event():
    """アプリ終了時の処理"""
    global monitoring_task, httpx_client
    
    if monitoring_task:
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
    
    if httpx_client:
        await httpx_client.aclose()
    
    logger.info("👋 Website Watcher 終了")

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """ログインページ"""
    return FileResponse(os.path.join(static_dir, "login.html"))

@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    """ログイン処理"""
    if password != ADMIN_PASSWORD:
        return RedirectResponse(url="/login?error=1", status_code=303)
    
    # セッショントークン生成
    session_token = create_session_token()
    
    # メインページにリダイレクト
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="session_token", 
        value=session_token, 
        httponly=True, 
        secure=False,  # 開発用はFalse
        samesite="lax"
    )
    return response

@app.get("/logout")
async def logout(request: Request):
    """ログアウト"""
    session_token = request.cookies.get("session_token")
    if session_token and session_token in active_sessions:
        active_sessions.remove(session_token)
    
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    return response

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """メインページ（認証必須）"""
    try:
        verify_session(request)
        return FileResponse(os.path.join(static_dir, "index.html"))
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)

@app.get("/api/health")
@limiter.limit("60/minute")
async def health_check(request: Request):
    """ヘルスチェック"""
    uptime = datetime.now() - metrics['uptime_start']
    
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "uptime_seconds": int(uptime.total_seconds()),
        "monitoring_active": monitoring_task and not monitoring_task.done(),
        "sites_count": len(load_sites()),
        "circuit_breakers_active": metrics['circuit_breaker_active']
    }

@app.get("/api/metrics")
@limiter.limit("30/minute")
async def get_metrics(request: Request):
    """メトリクス取得"""
    return {
        "metrics": metrics,
        "uptime": str(datetime.now() - metrics['uptime_start']),
        "cache_size": len(cache)
    }

@app.get("/api/sites")
@limiter.limit("120/minute")
async def get_sites(request: Request):
    """サイト一覧取得（キャッシュ付き）"""
    verify_session(request)
    sites = load_sites()
    return {"sites": sites}

@app.post("/api/sites")
@limiter.limit("10/minute")
async def add_site(site: Site, request: Request):
    """サイト追加"""
    verify_session(request)
    sites = load_sites()
    
    # 重複チェック
    for existing_site in sites:
        if existing_site['url'] == site.url:
            raise HTTPException(status_code=400, detail="このURLは既に登録されています")
    
    # URL検証
    if not site.url.startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="有効なURLを入力してください")
    
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
@limiter.limit("10/minute")
async def delete_site(site_index: int, request: Request):
    """サイト削除"""
    verify_session(request)
    sites = load_sites()
    
    if 0 <= site_index < len(sites):
        deleted_site = sites.pop(site_index)
        save_sites(sites)
        logger.info(f"🗑️ サイト削除: {deleted_site.get('name', deleted_site['url'])}")
        return {"message": "サイトを削除しました"}
    else:
        raise HTTPException(status_code=404, detail="サイトが見つかりません")

@app.post("/api/test-email")
@limiter.limit("5/minute")
async def test_email(email_data: dict, request: Request):
    """テストメール送信"""
    verify_session(request)
    to_email = email_data.get("email")
    if not to_email:
        raise HTTPException(status_code=400, detail="メールアドレスが必要です")
    
    subject = "📧 Website Watcher テストメール"
    body = f"""
このメールは Website Watcher のテスト送信です。

送信時刻: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}

このメールが届いていれば、メール設定は正常です。
"""
    
    success = await email_service.send_email(to_email, subject, body)
    if success:
        return {"message": "テストメールを送信しました"}
    else:
        raise HTTPException(status_code=500, detail="メール送信に失敗しました")

@app.post("/api/check-now")
@limiter.limit("3/minute")
async def check_now(request: Request):
    """手動チェック実行"""
    verify_session(request)
    logger.info("🔍 手動チェック実行")
    await check_all_sites()
    return {"message": "チェックを実行しました"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8888"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")