# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

オタクDJ大会の公開 Google Drive フォルダに散らばったセットリストから、プレイログ DB と閲覧 GUI を作るリポジトリ。詳しい背景・出典側の既知の問題は [README.md](README.md) にある。

## コマンド

```bash
# Drive のフォルダ構成を走査 → data/manifest.json（新しい開催回が増えたときだけ）
uv run python -m odj.crawl

# セトリを読んで出力一式を組み立てる
uv run python -m odj.build
uv run python -m odj.build --no-infer-from-filenames   # ファイル名からの推定を切る

# xlsx リーダの回帰テスト
python3 -m unittest discover -s tests
python3 -m unittest tests.test_xlsx.XlsxReaderTest.test_boolean_keeps_its_word  # 単体

# 閲覧 GUI（web/）
cd web && npm install
npm run dev      # 5173 固定。終了は q + Enter（Ctrl+C が届かないことがある）
npm run stop     # 取り残した dev サーバー（5173 と 5174）を落とす
npm run lint     # oxlint
npm run verify   # 名寄せ・集計ロジックを実データに当てる
npm run build    # tsc -b && vite build

# 表記ゆれの名寄せ（data/aliases/）
PYTHONPATH=src python3 -m odj.aliases fetch --field work   # 外部APIで裏取り。要ネットワーク
PYTHONPATH=src python3 -m odj.aliases block --field work   # 候補クラスタ。fetch の後に回す
PYTHONPATH=src python3 -m odj.aliases ask --field work      # LLM に提案させる。要 GROQ_API_KEY
PYTHONPATH=src python3 -m odj.aliases ask --field work --dry-run   # 投げる文字列とリクエスト数だけ見る。キー不要
PYTHONPATH=src python3 -m odj.aliases auto --field work --dry-run  # 規則に合う提案の件数と、人間に残る理由の内訳
PYTHONPATH=src python3 -m odj.aliases auto --field work            # 自動承認 → works.auto.toml（--undo で全部取り消し）
cd web && npm run review   # 5174。1件ずつ承認する GUI。dev 専用でビルドには入らない
PYTHONPATH=src python3 -m odj.aliases export               # 承認済み → aliases.json
```

`npm run review` は `out/aliases/clusters.<field>.json` を読むが、`out/` は gitignore
なので clone しただけでは無い。**先に `fetch` → `block` の順で回す**（`block` が外部 API の
リダイレクトを辺として使うため。「ナナシス」と「Tokyo 7th シスターズ」は文字列類似では
繋がらない）。候補の生成そのものは GitHub Actions の `aliases.yml` が回して PR で運んでくる。

`auto` は LLM の提案のうち**規則で安全と言い切れるものだけ**を人手を通さず承認し、
`data/aliases/works.auto.toml` / `artists.auto.toml`（人が育てる works.toml とは別の、
機械が丸ごと書き直すファイル）に書く。規則と、なぜその線引きなのか（`artist-as-work`
のヒントが付いたクラスタには合同名義の分解が混ざっていた）は
[src/odj/aliases/auto.py](src/odj/aliases/auto.py) の冒頭にある。work では 150 クラスタ中
71 個が対象。取り消しは `--undo` で、消した値は再び候補に戻る。

`ask` だけ外部 API（Groq）を叩く。GitHub Models → OpenAI 直叩き → Gemini と渡り歩いた末、
Gemini は無料枠が1日20リクエストしかなく完走できないため Groq に落ち着いた（経緯の詳細は
git 履歴）。OpenAI 互換エンドポイント（`https://api.groq.com/openai/v1/chat/completions`）
を叩いており、既定モデルが `openai/gpt-oss-120b` なのは、Groq でスキーマ強制（strict な
json_schema）が効くのが gpt-oss 系（120b/20b）だけだから。`--model` で他のモデルに
差し替えるとスキーマ強制が外れ、提案が静かに質を落とす。無料枠は RPD 1,000 / TPM 8,000 /
TPD 200,000（ドキュメント記載時点、openai/gpt-oss-120b）で、縛りになるのは TPM。バッチの
刻みを広げすぎると1リクエストで分あたり上限を超える。`GROQ_API_KEY`（APIキーは Groq
Console で発行）が要り、Actions で回すには**リポジトリの Settings > Secrets に手で
登録しておく**必要がある（`secrets.GITHUB_TOKEN` と違い自動では用意されない）。金を
使わずに確認したいときは `--dry-run`。

`uv` を使わない場合は `PYTHONPATH=src python3 -m odj.build`。Python 側は標準ライブラリのみで書いてあり（`pyproject.toml` の dependencies は空）、追加インストールは不要。lint / 型チェックの設定は置いていないので、Python 側の自動チェックは unittest だけ。

`odj.build` は毎回 Drive から既存マスターDBを取りに行くため**ネットワークが要る**（`data/raw/` にキャッシュされ、以降は再取得しない。このディレクトリは gitignore）。

CI（`.github/workflows/deploy.yml`）は `web/` に対して lint → verify → build を走らせ、`main` への push で GitHub Pages に配る。Python 側は CI で動かない。

## 構造

**2層構成で、接点は `web/public/data/plays.json` 1ファイルだけ。**

```
Drive（公開フォルダ）
  → drive.py    認証なしの薄いクライアント（フォルダHTMLの _DRIVE_ivd を読む）
  → crawl.py    開催回 / DJ / セトリ候補ファイルの一覧を data/manifest.json へ
  → readers.py  ファイル1つ → レコード列（xlsx / txt / 手読みCSV）
     normalize.py  バラバラな列名・値を正準フィールドへ寄せる
     xlsx.py       標準ライブラリだけの xlsx リーダ
  → build.py    既存マスターDBと union マージして out/*, web/public/data/plays.json
  → web/        plays.json だけを読む静的サイト（React + Vite）
```

`out/` は gitignore だが **`web/public/data/plays.json` は git 管理下**。パイプラインの出力が変わる変更をしたら、このファイルを再生成してコミットしないと GUI と CI に反映されない（`data/manifest.json`、`data/overrides.toml`、`data/manual/*.csv` も同様に管理下）。

### 押さえておくべき設計上の判断

- **開催回・日付・DJ・play順はフォルダ名から決める。** `EVENT_FOLDER_RE`（`第N回_YYYYMMDD`）と `DJ_PREFIX_RE`（`01_`・`xx_` 接頭辞）が出典。ファイル内に「回」「DJ」列があっても [readers.py:54-55](src/odj/readers.py#L54-L55) で捨てている。DJ 名の表記ゆれは `crawl.py` の `DJ_ALIASES` で統合し、元のフォルダ名は `dj_folder` 列に残す。

- **「ファイルを正・マスターDBを穴埋め」の union マージ。** 手作業で育ててきた既存スプレッドシート（オタクDJ大会DB）を捨てないための方針。ファイルがある DJ はファイルを採用し空欄だけ `ENRICHABLE`（`source_work` / `artist` / `url` / `is_remix` / `play_order`）で補い、ファイルが無い DJ はマスターDB の行をそのまま採る。どの行がどこから来たかは `source_file_id` / `source_kind` / `confidence` で追える。

- **候補ファイルとタブの選択は行数ではなく [readers.py の `_score()`](src/odj/readers.py#L81) のタプル比較。** 同じフォルダに候補曲リストや下書きが同居しているため、行数比べだとそちらが勝つ。(列の種類数, 曲順の素直さ, 埋まり具合, 行数) の順で見る。`read_dj_files` と `read_xlsx` が同じ関数を使っている。

- **実データの例外は3か所に外出ししてある。** ①[data/overrides.toml](data/overrides.toml) … `skip`（ファイル単位）/ `drop_row`（行単位）/ `fix`（マスターDB側の誤りの補正）。理由コメント付きなので挙動を変える前にまず読む。②`data/manual/<Driveファイル ID>.csv` … PDF は ToUnicode を持たずテキスト抽出できないので人間が読み取った結果を置く。`readers.read()` が常に最優先する。③`crawl.py` の定数（`NOT_SETLIST_NAMES` / `NON_DJ_FOLDERS` / `SKIP_SUFFIXES`）。コードに if を足す前に、この3つで済まないか検討する。

- **名寄せは Python 側でやらない。** DB はプレイログのまま持ち、表記ゆれの吸収は [web/src/lib/normalize.ts](web/src/lib/normalize.ts) がクエリ時に行う。`normKey()` が全角半角・記号・空白を均し、`baseKey()` がさらにリミックス表記を落として原曲名に寄せる。既出判定（完全一致 / 原曲一致）と集計のランキングはすべてこの2つのキーの上に載っているので、挙動を変えたいときは TS 側を触る。`npm run verify`（[web/scripts/verify.ts](web/scripts/verify.ts)）が実データに当てて確認する。

- **`xlsx.py` を触ったら必ず `tests/test_xlsx.py` を走らせる。** ふりがな（`rPh`）の混入、真偽値セル（アーティスト名 "TRUE" が 1 になる）、`14.0` 形式の整数など、いずれも「読めない」ではなく「静かに値が化ける」種類の不具合の回帰テスト。

## 書き方の慣習

- コメント・docstring・出力メッセージはすべて日本語。「なぜそうしたか」を実データの具体例（どのファイルのどの行で踏んだか）と一緒に書いてあるので、同じ調子で足す。
- Python は標準ライブラリのみ。依存を増やす前に代替を検討する。
- 変更後の確認は `.claude/agents/` のサブエージェントに任せられる（`odj-pipeline-checker` = Python 側、`web-checker` = web/ 側）。
