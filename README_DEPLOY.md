# Website Watcher - Render.com デプロイガイド

## 🚀 デプロイ手順

### 1. GitHubリポジトリの作成

1. GitHubにログイン（https://github.com）
2. 新しいリポジトリを作成（例: `website-watcher`）
3. プライベートリポジトリとして作成

### 2. ファイルのアップロード

以下のファイルをGitHubリポジトリにアップロード：
- `app.py` - メインアプリケーション
- `requirements.txt` - Python依存関係
- `render.yaml` - Renderデプロイ設定
- `.gitignore` - Git無視ファイル
- `static/` フォルダ全体（index.html, monitor.html）

### 3. Render.comアカウント作成

1. https://render.com にアクセス
2. GitHubアカウントでサインアップ
3. GitHubリポジトリへのアクセスを許可

### 4. 新しいWebサービスの作成

1. Renderダッシュボードで「New +」→「Web Service」を選択
2. GitHubリポジトリを接続
3. `website-watcher`リポジトリを選択

### 5. 環境変数の設定

Renderダッシュボードで以下の環境変数を設定：

```
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=あなたのGmailアドレス
SMTP_PASSWORD=Gmailアプリパスワード
FROM_EMAIL=あなたのGmailアドレス
CHECK_INTERVAL=300
```

### 6. Gmailアプリパスワードの取得

1. Googleアカウントにログイン
2. https://myaccount.google.com/security にアクセス
3. 「2段階認証」を有効化
4. 「アプリパスワード」を生成
5. 生成されたパスワードを`SMTP_PASSWORD`に設定

### 7. デプロイ

1. 「Create Web Service」をクリック
2. デプロイが自動的に開始
3. 数分後、URLが発行される

## 📝 使用方法

1. 発行されたURL（例: https://website-watcher-xxxx.onrender.com）にアクセス
2. 監視したいサイトを登録
3. メール通知が自動的に送信される

## ⚠️ 注意事項

- 無料プランの場合、15分間アクセスがないとスリープ状態になります
- 最初のアクセス時は起動に30秒程度かかる場合があります
- 設定データは永続ディスクに保存されます

## 🔧 トラブルシューティング

### メールが送信されない場合
1. Gmailの「安全性の低いアプリのアクセス」を確認
2. アプリパスワードが正しく設定されているか確認
3. Renderのログでエラーを確認

### サイトにアクセスできない場合
1. デプロイが完了しているか確認
2. Renderダッシュボードでサービスの状態を確認
3. ログでエラーメッセージを確認