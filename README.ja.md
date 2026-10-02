<div align="center">

<img src="docs/media/brand-wide.png" alt="TokenLens" width="720">

# TokenLens

**すべての AI 呼び出しのトークンとコストを、ローカルの台帳に記録する。**

ゼロ侵入プロキシ · データは手元から出ない · 37+ 上流 · 5 分で導入

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-137%20passing-brightgreen)](scripts/smoke_test.py)
[![Version](https://img.shields.io/badge/version-1.2.1-orange)](CHANGELOG.md)

[Website](https://x33834.github.io/tokenlens/) ｜ [English](README.md) ｜ [中文](README.zh-CN.md) ｜ [Changelog](CHANGELOG.md)

</div>

---

<table>
<tr><td width="50%">

**こんな悩みはありませんか**

- 月末の請求を見て、いくら使ったか初めて知る
- 複数のプロジェクト / アプリが同じキーを共有し、どこにお金が消えたか分からない
- 使用量を抑えたいのに、ダッシュボードを監視し続けるしかない
- クラウド監視サービスはプロンプトとキーを預けさせる

</td><td width="50%">

**TokenLens の答え**

- すべての呼び出しをリアルタイムに記録：トークン数・コスト・レイテンシ・成否
- プロジェクト / アプリ / モデル / プロバイダ別に分解し、「誰がお金を使っているか」まで見える
- 予算しきい値でアラート、超過したリクエストは上流へ転送せず 402 でブロック
- 完全ローカル：記録するのはメタデータのみ。プロンプト / レスポンスは保存せず、キーはフィンガープリントだけ

</td></tr>
</table>

## 60 秒で導入

```bash
# 1. インストールして起動（デフォルトポート 8787）
pip install . && python -m tokenlens start

# 2. クライアントの base_url を向ける（OpenAI 互換アプリなら何でも）
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1

# 3. 台帳ダッシュボードを開く
http://127.0.0.1:8787
```

以上です。業務コードの変更も、サービスへの登録も不要。実際の API キーがなくても体験できます：`python -m tokenlens seed-demo` でデモデータを投入、`python examples/mock_upstream.py` でモック上流を起動。

### その他の導入方法

| 方法 | 向いている場面 | コマンド |
|---|---|---|
| pip / ソース | 日常の開発 | 上の 3 行 |
| Docker | サーバー / NAS で常駐 | `docker compose up -d` |
| Windows インストール不要 | ダブルクリックで使いたい人 | 単一ファイル `dist/TokenLens.exe` をダブルクリック（プロキシ + ダッシュボード :8788） |
| 複数マシン統合 | 複数台で分散運用 | 各マシンで CSV エクスポート → `tokenlens import-csv` で冪等マージ |

## スクリーンショット

<video src="docs/media/tokenlens-promo.mp4" controls width="720" poster="docs/media/desktop.png"></video>

*22 秒の実操作録画：スケルトン読み込み → KPI カウントアニメーション → グラフの切り替え → 行の展開 → 設定ドロワー → アラートタイムライン。*

| デスクトップ | モバイル |
|---|---|
| <img src="docs/media/desktop.png" alt="デスクトップ版" width="100%"> | <img src="docs/media/mobile.png" alt="モバイル版" width="72%"> |

## アーキテクチャ

<img src="docs/media/architecture.svg" alt="アーキテクチャ：アプリ → TokenLens（プロキシ/計測/台帳/ダッシュボード）→ 上流 LLM" width="100%">

- **計測の三段フォールバック**：まず上流が返す usage（最も正確）→ tiktoken → ヒューリスティック。オフラインや未知のモデルでも台帳は欠かさない
- **ストリーミングも完全計測**：SSE をチャンク単位で解析し、`include_usage` を自動注入して実際の使用量を取得
- **予算ブロックは転送前**：超過リクエストは 402 で拒否し、上流のクォータを消費しない
- **キーはフィンガープリントのみ保存**：SHA-256 の先頭 12 文字。アプリ名にマッピング可能

## 既知の限界（正直に）

- 予算ブロックは極端な同時実行ではベストエフォート（チェック後記録の固有レース）
- 価格テーブルはスナップショット（CI で毎月 LiteLLM から同期）、価格改定に遅れが出る可能性あり。設定で手動上書き可
- Postgres バックエンドはロードマップ上。現状は SQLite のみ

## ドキュメント

[更新履歴](CHANGELOG.md) · [アーキテクチャとセキュリティ分析](docs/ANALYSIS.md) · [貢献ガイド](CONTRIBUTING.md) · [セキュリティポリシー](SECURITY.md)

## ライセンス

[MIT](LICENSE) — ご自由に。何に使ったか issue で教えてもらえると嬉しいです。
