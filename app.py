import asyncio
import json
import logging
import smtplib
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
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
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

# CORS設定（スマホアクセス対応）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本番環境では具体的なドメインを指定
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# グローバル変数とヘルスチェック
monitoring_task = None
sites_data = []
last_health_check = datetime.now()
failed_sites: Set[str] = set()  # サーキットブレーカー用
site_failure_count: Dict[str, int] = {}  # 失敗回数追跡
site_last_failure: Dict[str, datetime] = {}  # 最後の失敗時刻
http_client: Optional[httpx.AsyncClient] = None  # HTTPクライアント
api_cache: Dict[str, tuple] = {}  # APIキャッシュ (key: (data, timestamp))

class Site(BaseModel):
    url: str
    email: str
    name: Optional[str] = ""
    
    def __init__(self, **data):
        # URL validation
        if 'url' in data and not data['url'].startswith(('http://', 'https://')):
            data['url'] = 'https://' + data['url']
        super().__init__(**data)

class HealthStatus(BaseModel):
    status: str
    timestamp: datetime
    monitoring_active: bool
    total_sites: int
    failed_sites: int
    last_check: Optional[datetime] = None
    uptime: str

class EmailService:
    """高信頼性メール送信サービス - サーキットブレーカー付き"""
    
    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.username = os.getenv("SMTP_USERNAME", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.from_email = os.getenv("FROM_EMAIL", "")
        self.failure_count = 0
        self.last_failure_time = None
        self.circuit_breaker_threshold = 5  # 5回連続失敗でサーキットオープン
        self.circuit_breaker_timeout = 300  # 5分でリセット
        
        # 設定験証
        self._validate_config()
    
    def _validate_config(self):
        """メール設定の验証"""
        missing = []
        if not self.username: missing.append("SMTP_USERNAME")
        if not self.password: missing.append("SMTP_PASSWORD")
        if not self.from_email: missing.append("FROM_EMAIL")
        
        if missing:
            logger.error(f"必要な環境変数が未設定: {', '.join(missing)}")
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    
    def _is_circuit_open(self) -> bool:
        """サーキットブレーカー状態チェック"""
        if self.failure_count < self.circuit_breaker_threshold:
            return False
            
        if self.last_failure_time is None:
            return False
            
        # タイムアウト後はリセット
        if time.time() - self.last_failure_time > self.circuit_breaker_timeout:
            self.failure_count = 0
            self.last_failure_time = None
            logger.info("メールサーキットブレーカーをリセット")
            return False
            
        return True
    
    def _exponential_backoff(self, attempt: int) -> float:
        """指数バックオフ計算"""
        return min(2 ** attempt, 60)  # 最大60秒
    
    async def send_email(self, to_email: str, subject: str, body: str) -> bool:
        """非同期メール送信（サーキットブレーカー付き）"""
        if self._is_circuit_open():
            logger.warning(f"メールサーキットブレーカーオープン中 - 送信スキップ: {to_email}")
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
                
                # 非同期SMTP送信
                def _send_sync():
                    with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30) as server:
                        server.starttls()
                        server.login(self.username, self.password)
                        server.send_message(msg)
                
                # ブロッキング処理を非同期実行
                await asyncio.to_thread(_send_sync)
                
                logger.info(f"✅ メール送信成功: {to_email}")
                self.failure_count = 0  # 成功時はリセット
                return True
                
            except Exception as e:
                logger.error(f"❌ メール送信失敗 (試行{attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    backoff_time = self._exponential_backoff(attempt)
                    logger.info(f"⚙️ {backoff_time}秒待機後にリトライ")
                    await asyncio.sleep(backoff_time)
        
        # 全ての試行が失敗
        self.failure_count += 1
        self.last_failure_time = time.time()
        logger.error(f"❌ メール送信完全失敗: {to_email} (失敗回数: {self.failure_count})")
        return False
    
    async def test_connection(self) -> bool:
        """非同期SMTP接続テスト"""
        try:
            logger.info("SMTP接続テスト開始...")
            
            def _test_sync():
                with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(self.username, self.password)
            
            await asyncio.to_thread(_test_sync)
            logger.info("✅ SMTP接続成功")
            return True
        except Exception as e:
            logger.error(f"❌ SMTP接続失敗: {e}")
            return False

class SiteChecker:
    """非同期サイトチェッククラス - サーキットブレーカー付き"""
    
    def __init__(self, http_client: httpx.AsyncClient):
        self.http_client = http_client
        self.site_timeouts = {}  # サイト別タイムアウト設定
    
    def _get_timeout_for_site(self, url: str) -> int:
        """サイト別の動的タイムアウト取得"""
        base_timeout = int(os.getenv("SITE_TIMEOUT", "15"))
        return self.site_timeouts.get(url, base_timeout)
    
    def _update_timeout_for_site(self, url: str, response_time: float):
        """レスポンス時間に基づいてタイムアウトを調整"""
        # レスポンス時間の1.5倍をタイムアウトとして設定（最大60秒）
        new_timeout = min(max(int(response_time * 1.5), 10), 60)
        self.site_timeouts[url] = new_timeout
    
    def _is_site_in_circuit_breaker(self, url: str) -> bool:
        """サイトがサーキットブレーカー状態かチェック"""
        if url not in site_failure_count:
            return False
            
        failure_count = site_failure_count[url]
        if failure_count < 3:  # 3回連続失敗でサーキットオープン
            return False
            
        last_failure = site_last_failure.get(url)
        if last_failure is None:
            return False
            
        # 10分経過でリセット
        if datetime.now() - last_failure > timedelta(minutes=10):
            site_failure_count[url] = 0
            if url in site_last_failure:
                del site_last_failure[url]
            if url in failed_sites:
                failed_sites.remove(url)
            logger.info(f"サイトサーキットブレーカーリセット: {url}")
            return False
            
        return True
    
    async def get_site_hash(self, url: str) -> Optional[str]:
        """非同期サイトハッシュ取得"""
        if self._is_site_in_circuit_breaker(url):
            logger.warning(f"サイトサーキットブレーカーオープン中 - スキップ: {url}")
            return None
        
        try:
            logger.info(f"サイトチェック開始: {url}")
            start_time = time.time()
            
            timeout_val = self._get_timeout_for_site(url)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ja,en-US;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
            
            response = await self.http_client.get(
                url, 
                timeout=timeout_val, 
                headers=headers,
                follow_redirects=True
            )
            response.raise_for_status()
            
            response_time = time.time() - start_time
            self._update_timeout_for_site(url, response_time)
            
            # ハッシュ計算
            content_hash = hashlib.md5(response.text.encode('utf-8')).hexdigest()
            
            # 成功時は失敗カウンターをリセット
            if url in site_failure_count:
                site_failure_count[url] = 0
            if url in failed_sites:
                failed_sites.remove(url)
            
            logger.info(f"✅ サイトチェック成功: {url} (hash: {content_hash[:8]}..., {response_time:.2f}s)")
            return content_hash
            
        except Exception as e:
            # 失敗情報を記録
            site_failure_count[url] = site_failure_count.get(url, 0) + 1
            site_last_failure[url] = datetime.now()
            failed_sites.add(url)
            
            logger.error(f"❌ サイトチェック失敗: {url} - {e} (失敗回数: {site_failure_count[url]})")
            return None

# サービスインスタンス (遅延初期化)
email_service = None
site_checker = None

# アプリケーション起動時刻
app_start_time = datetime.now()

def load_sites() -> List[Dict]:
    """堅牢なサイトデータ読み込み（バックアップ対応）"""
    config_files = ['config.json', 'config.json.backup']
    
    for config_file in config_files:
        try:
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    sites = data.get('sites', [])
                    logger.info(f"設定ファイル読み込み成功: {config_file} ({len(sites)}サイト)")
                    return sites
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"設定ファイル読み込み失敗: {config_file} - {e}")
            continue
    
    logger.warning("すべての設定ファイルの読み込みに失敗")
    return []

def save_sites(sites: List[Dict]):
    """アトミックなサイトデータ保存（バックアップ付き）"""
    try:
        # 現在の設定をバックアップ
        if os.path.exists('config.json'):
            import shutil
            shutil.copy2('config.json', 'config.json.backup')
        
        # 新しい設定を一時ファイルに書き込み
        config = {
            'sites': sites, 
            'settings': {
                'check_interval': int(os.getenv('CHECK_INTERVAL', '300')),
                'timeout': int(os.getenv('SITE_TIMEOUT', '15')),
                'max_retries': 3
            },
            'last_updated': datetime.now().isoformat()
        }
        
        temp_file = 'config.json.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        # アトミックに置き換え
        import os
        if os.name == 'nt':  # Windows
            if os.path.exists('config.json'):
                os.remove('config.json')
        os.rename(temp_file, 'config.json')
        
        logger.info(f"設定ファイル保存完了 ({len(sites)}サイト)")
        
    except Exception as e:
        logger.error(f"設定ファイル保存エラー: {e}")
        # 一時ファイルが残っていたら削除
        if os.path.exists('config.json.tmp'):
            try:
                os.remove('config.json.tmp')
            except:
                pass

async def check_all_sites():
    """全サイトチェック（並列処理・エラー処理強化版）"""
    global sites_data, last_health_check
    
    try:
        sites_data = load_sites()
        if not sites_data:
            logger.info("監視対象サイトがありません")
            return
        
        logger.info(f"🔍 {len(sites_data)}サイトの並列監視を開始")
        last_health_check = datetime.now()
        
        # 並列処理でサイトをチェック（最大5並列）
        semaphore = asyncio.Semaphore(5)
        tasks = []
        
        for site in sites_data:
            task = asyncio.create_task(_check_single_site(site, semaphore))
            tasks.append(task)
        
        # 全てのタスクが完了するまで待機
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 結果を処理
        success_count = sum(1 for r in results if r is True)
        error_count = sum(1 for r in results if isinstance(r, Exception))
        skip_count = len(results) - success_count - error_count
        
        logger.info(f"📊 チェック完了: 成功={success_count}, エラー={error_count}, スキップ={skip_count}")
        
        # 設定保存（エラーが発生してもデータは保存）
        save_sites(sites_data)
        
    except Exception as e:
        logger.error(f"監視処理で予期しないエラー: {e}")

async def _check_single_site(site: Dict, semaphore: asyncio.Semaphore) -> bool:
    """単一サイトの非同期チェック"""
    async with semaphore:
        try:
            url = site['url']
            email = site['email']
            name = site.get('name', url)
            last_hash = site.get('hash', '')
            
            # サイトチェック
            current_hash = await site_checker.get_site_hash(url)
            if not current_hash:
                return False  # サーキットブレーカーまたはエラー
            
            # 初回チェック
            if not last_hash:
                site['hash'] = current_hash
                site['last_check'] = datetime.now().isoformat()
                logger.info(f"📝 初回ハッシュ設定: {name}")
                return True
            
            # 変更検知
            if current_hash != last_hash:
                logger.info(f"🚨 変更検知: {name}")
                
                # メール送信
                subject = f"🔔 サイト更新通知: {name}"
                body = f"""サイトが更新されました！

サイト名: {name}
URL: {url}
更新検知時刻: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}

このメールは Website Watcher により自動送信されました。
運用状況: https://your-domain:8888/api/health
"""
                
                success = await email_service.send_email(email, subject, body)
                if success:
                    # ハッシュ更新
                    site['hash'] = current_hash
                    site['last_check'] = datetime.now().isoformat()
                    site['last_notified'] = datetime.now().isoformat()
                    logger.info(f"✅ 通知完了: {name} → {email}")
                else:
                    logger.error(f"❌ 通知失敗: {name} → {email}")
                    # 通知失敗でもlast_checkは更新（無限リトライを防ぐ）
                    site['last_check'] = datetime.now().isoformat()
            else:
                site['last_check'] = datetime.now().isoformat()
                logger.info(f"📍 変更なし: {name}")
            
            # サイト間のレート制限
            await asyncio.sleep(1)
            return True
            
        except Exception as e:
            logger.error(f"サイト個別チェックエラー ({site.get('name', site.get('url', 'unknown'))}): {e}")
            return False

async def monitoring_loop():
    """堅牢な監視ループ（自動復旧・指数バックオフ付き）"""
    logger.info("🔄 監視ループ開始")
    consecutive_errors = 0
    max_errors = 10
    
    while True:
        try:
            await check_all_sites()
            consecutive_errors = 0  # 成功時はエラーカウンターリセット
            
            check_interval = max(int(os.getenv("CHECK_INTERVAL", "300")), 60)  # 最小60秒
            logger.info(f"⏰ {check_interval}秒後に再チェック")
            await asyncio.sleep(check_interval)
            
        except asyncio.CancelledError:
            logger.info("監視ループがキャンセルされました")
            break
            
        except Exception as e:
            consecutive_errors += 1
            backoff_time = min(2 ** consecutive_errors, 300)  # 最大5分
            
            logger.error(f"監視ループエラー ({consecutive_errors}/{max_errors}): {e}")
            logger.info(f"⚙️ {backoff_time}秒待機後にリトライ")
            
            # 連続エラーが多すぎる場合は長時間待機
            if consecutive_errors >= max_errors:
                logger.critical(f"連続エラー上限到達 - 10分間待機")
                consecutive_errors = 0  # リセット
                await asyncio.sleep(600)  # 10分
            else:
                await asyncio.sleep(backoff_time)

async def task_monitor():
    """監視タスクの健全性をチェックし、必要に応じて再起動"""
    global monitoring_task
    
    while True:
        try:
            await asyncio.sleep(300)  # 5分ごとにチェック
            
            if monitoring_task is None or monitoring_task.done():
                if monitoring_task and monitoring_task.done():
                    exception = monitoring_task.exception()
                    if exception:
                        logger.error(f"監視タスクが異常終了: {exception}")
                    else:
                        logger.warning("監視タスクが正常終了（予期しない）")
                
                logger.info("🔄 監視タスクを再起動")
                monitoring_task = asyncio.create_task(monitoring_loop())
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"タスクモニターエラー: {e}")
            await asyncio.sleep(60)

# 静的ファイル配信
import os
app_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(app_dir, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# FastAPI エンドポイント
@app.on_event("startup")
async def startup_event():
    """アプリ起動時の処理（サービス初期化・ヘルスチェック付き）"""
    global monitoring_task, email_service, site_checker, http_client
    
    logger.info("🚀 Website Watcher 起動")
    
    try:
        # HTTPクライアント初期化
        http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            verify=True,
            follow_redirects=True
        )
        logger.info("✅ HTTPクライアント初期化完了")
        
        # サービス初期化
        try:
            email_service = EmailService()
            logger.info("✅ メールサービス初期化完了")
        except ValueError as e:
            logger.error(f"❌ メールサービス初期化失敗: {e}")
            email_service = None  # メール機能無効化
        
        site_checker = SiteChecker(http_client)
        logger.info("✅ サイトチェッカー初期化完了")
        
        # SMTP接続テスト（メールサービスが有効な場合のみ）
        if email_service:
            if await email_service.test_connection():
                logger.info("✅ メール設定確認完了")
            else:
                logger.warning("⚠️ メール設定に問題があります - 通知は無効化されます")
        
        # 監視タスク開始
        monitoring_task = asyncio.create_task(monitoring_loop())
        
        # タスクモニター開始（監視タスクの死活監視）
        task_monitor_task = asyncio.create_task(task_monitor())
        
        logger.info("🎯 全サービス起動完了")
        
    except Exception as e:
        logger.critical(f"❌ 起動時エラー: {e}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """アプリ終了時の処理（リソース解放）"""
    global monitoring_task, http_client
    
    logger.info("🛑 Website Watcher 終了処理開始")
    
    try:
        # 監視タスク終了
        if monitoring_task and not monitoring_task.done():
            monitoring_task.cancel()
            try:
                await asyncio.wait_for(monitoring_task, timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        
        # HTTPクライアント終了
        if http_client:
            await http_client.aclose()
            logger.info("✅ HTTPクライアント終了")
        
        logger.info("👋 Website Watcher 終了完了")
        
    except Exception as e:
        logger.error(f"終了時エラー: {e}")

@app.get("/", response_class=HTMLResponse)
async def root():
    """メインページ"""
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.get("/api/sites")
@limiter.limit("10/minute")
async def get_sites(request: Request):
    """サイト一覧取得（キャッシュ付き）"""
    cache_key = "sites_list"
    now = time.time()
    
    # キャッシュチェック（30秒有効）
    if cache_key in api_cache:
        data, timestamp = api_cache[cache_key]
        if now - timestamp < 30:
            return data
    
    try:
        sites = load_sites()
        result = {"sites": sites, "cached": False, "timestamp": datetime.now().isoformat()}
        
        # キャッシュ更新
        api_cache[cache_key] = (result, now)
        
        return result
    except Exception as e:
        logger.error(f"サイト一覧取得エラー: {e}")
        raise HTTPException(status_code=500, detail="サイト一覧の取得に失敗しました")

@app.post("/api/sites")
@limiter.limit("5/minute")
async def add_site(request: Request, site: Site):
    """サイト追加（入力検証強化）"""
    try:
        sites = load_sites()
        
        # URLの正規化とバリデーション
        normalized_url = site.url.strip()
        if not normalized_url.startswith(('http://', 'https://')):
            normalized_url = 'https://' + normalized_url
        
        # 重複チェック
        for existing_site in sites:
            if existing_site['url'] == normalized_url:
                raise HTTPException(status_code=400, detail="このURLは既に登録されています")
        
        # サイト数制限
        max_sites = int(os.getenv('MAX_SITES_PER_INSTANCE', '50'))
        if len(sites) >= max_sites:
            raise HTTPException(status_code=400, detail=f"サイト登録数上限({max_sites})に達しました")
        
        # 新サイト追加
        new_site = {
            "url": normalized_url,
            "email": site.email.strip().lower(),
            "name": site.name.strip() if site.name else normalized_url,
            "hash": "",
            "created_at": datetime.now().isoformat()
        }
        
        sites.append(new_site)
        save_sites(sites)
        
        # キャッシュクリア
        if "sites_list" in api_cache:
            del api_cache["sites_list"]
        
        logger.info(f"📝 新サイト登録: {new_site['name']} ({new_site['url']})")
        return {"message": "サイトを登録しました", "site": new_site}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"サイト追加エラー: {e}")
        raise HTTPException(status_code=500, detail="サイトの追加に失敗しました")

@app.delete("/api/sites/{site_index}")
@limiter.limit("5/minute")
async def delete_site(request: Request, site_index: int):
    """サイト削除（安全性強化）"""
    try:
        sites = load_sites()
        
        if 0 <= site_index < len(sites):
            deleted_site = sites.pop(site_index)
            save_sites(sites)
            
            # キャッシュクリア
            if "sites_list" in api_cache:
                del api_cache["sites_list"]
            
            logger.info(f"🗑️ サイト削除: {deleted_site.get('name', deleted_site['url'])}")
            return {"message": "サイトを削除しました", "deleted_site": deleted_site}
        else:
            raise HTTPException(status_code=404, detail="サイトが見つかりません")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"サイト削除エラー: {e}")
        raise HTTPException(status_code=500, detail="サイトの削除に失敗しました")

@app.post("/api/test-email")
@limiter.limit("3/minute")
async def test_email(request: Request, email_data: dict):
    """テストメール送信（レート制限付き）"""
    if not email_service:
        raise HTTPException(status_code=503, detail="メールサービスが無効です")
    
    to_email = email_data.get("email")
    if not to_email:
        raise HTTPException(status_code=400, detail="メールアドレスが必要です")
    
    try:
        subject = "📧 Website Watcher テストメール"
        body = f"""このメールは Website Watcher のテスト送信です。

送信時刻: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}
サーバー状態: 正常動作中

このメールが届いていれば、メール設定は正常です。
運用状況: http://localhost:8888/api/health
"""
        
        success = await email_service.send_email(to_email, subject, body)
        if success:
            return {"message": "テストメールを送信しました"}
        else:
            raise HTTPException(status_code=500, detail="メール送信に失敗しました")
    except Exception as e:
        logger.error(f"テストメールエラー: {e}")
        raise HTTPException(status_code=500, detail="メール送信に失敗しました")

@app.post("/api/check-now")
@limiter.limit("2/minute")
async def check_now(request: Request):
    """手動チェック実行（適度な制限付き）"""
    try:
        logger.info("🔍 手動チェック実行")
        start_time = time.time()
        
        await check_all_sites()
        
        end_time = time.time()
        duration = end_time - start_time
        
        # キャッシュクリア
        if "sites_list" in api_cache:
            del api_cache["sites_list"]
        
        return {
            "message": "チェックを実行しました",
            "duration": f"{duration:.2f}秒",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"手動チェックエラー: {e}")
        raise HTTPException(status_code=500, detail="チェックの実行に失敗しました")

# ヘルスチェックエンドポイント
@app.get("/api/health")
async def health_check():
    """アプリケーションヘルスチェック"""
    try:
        current_time = datetime.now()
        uptime = current_time - app_start_time
        
        # サービス状態チェック
        monitoring_active = monitoring_task is not None and not monitoring_task.done()
        email_available = email_service is not None
        sites = load_sites()
        
        health_data = {
            "status": "healthy" if monitoring_active else "degraded",
            "timestamp": current_time.isoformat(),
            "uptime": str(uptime),
            "uptime_seconds": uptime.total_seconds(),
            "monitoring_active": monitoring_active,
            "email_service_available": email_available,
            "total_sites": len(sites),
            "failed_sites": len(failed_sites),
            "last_health_check": last_health_check.isoformat() if last_health_check else None,
            "circuit_breakers": {
                "email_failures": email_service.failure_count if email_service else 0,
                "site_failures": dict(site_failure_count)
            },
            "memory_info": {
                "cache_entries": len(api_cache)
            }
        }
        
        return health_data
        
    except Exception as e:
        logger.error(f"ヘルスチェックエラー: {e}")
        return {
            "status": "error",
            "timestamp": datetime.now().isoformat(),
            "error": str(e)
        }

@app.get("/api/metrics")
async def metrics():
    """メトリクス情報（モニタリング用）"""
    try:
        sites = load_sites()
        current_time = datetime.now()
        
        # 最近のチェック時刻を計算
        last_checks = []
        for site in sites:
            if 'last_check' in site and site['last_check']:
                try:
                    last_check = datetime.fromisoformat(site['last_check'])
                    last_checks.append(last_check)
                except:
                    pass
        
        metrics_data = {
            "total_sites": len(sites),
            "active_sites": len([s for s in sites if 'hash' in s and s['hash']]),
            "failed_sites_count": len(failed_sites),
            "email_circuit_breaker_open": email_service._is_circuit_open() if email_service else False,
            "site_timeouts": dict(site_checker.site_timeouts) if site_checker else {},
            "last_successful_check": max(last_checks).isoformat() if last_checks else None,
            "monitoring_uptime_seconds": (current_time - app_start_time).total_seconds(),
            "cache_hit_ratio": len(api_cache) / max(len(sites), 1)
        }
        
        return metrics_data
        
    except Exception as e:
        logger.error(f"メトリクス取得エラー: {e}")
        raise HTTPException(status_code=500, detail="メトリクスの取得に失敗しました")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")