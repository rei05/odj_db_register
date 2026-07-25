# odj_db_register

オタクDJ大会の Google Drive フォルダに散らばったセットリストファイルから、
これまでにプレイされた曲のデータベースを作る。

- 対象: [オタクDJ大会フォルダ](https://drive.google.com/drive/folders/1Ti5vLERqTNbK1WMLuTh1Xk_Y6o9ZL_Ud)（第1〜15回）
- 形式: Google スプレッドシート / xlsx / PDF / .numbers / テキスト / Google ドキュメント
- 出力: `out/plays.csv`、`out/odj.sqlite`、`out/paste.tsv`、`out/report.md`、`web/public/data/plays.json`

フォルダは「リンクを知っている全員」で共有されているため、**Drive の認証情報は不要**。

## 使い方

```bash
# 1. フォルダ構成を走査（新しい開催回が増えたときだけ実行すればよい）
uv run python -m odj.crawl

# 2. セトリを読んで DB を組み立てる
uv run python -m odj.build

# 3. 閲覧 GUI
cd web && npm install && npm run dev
```

GUI は `web/public/data/plays.json` だけを読む静的サイトなので、
`npm run build` した `web/dist` をそのまま配れる。`main` への push で
GitHub Pages に上がる。

`uv` を使わない場合は `PYTHONPATH=src python3 -m odj.build` でも動く。
標準ライブラリのみで書いてあるので追加インストールは不要。

## 作り

```
src/odj/
  drive.py      公開 Drive フォルダの一覧取得とダウンロード
  crawl.py      開催回 / DJ / セトリファイルの一覧を data/manifest.json へ
  xlsx.py       標準ライブラリだけの xlsx リーダ
  normalize.py  バラバラな列名を正準スキーマへ寄せる
  readers.py    ファイル1つ -> レコード列
  build.py      既存マスターDBと突き合わせて出力
```

### 既存マスターDBとの関係

Drive のルートには手作業で育ててきた
[オタクDJ大会DB](https://docs.google.com/spreadsheets/d/1MEDuHQixRB9_2Kf3YLfK2QHmsT-PV1pTUnSGSgWkISU)
がある。これを捨てずに、**セトリファイルを正・マスターDBを穴埋め**として
union マージする。

- ファイルがある DJ … ファイルの内容を採用し、空欄だけマスターDBで補う
  （手入力されたアーティスト・URL・play順を落とさないため）
- ファイルが無い DJ … マスターDBの行をそのまま採用（`source_kind=master-db`）
- どちらも無い … `out/report.md` に未登録として列挙

各行には `source_file_id` / `source_kind` / `confidence` が付くので、
どの行がどこから来たのかは後から追える。

### data/overrides.toml

自動判定では正しく扱えないファイルを個別に指定する。理由も併記してあるので、
挙動を変えたいときはまずここを読むとよい。`skip`（ファイル単位）、
`drop_row`（行単位）、`fix`（マスターDB側の誤りの補正）の3種類。

### data/manual/

PDF は ToUnicode マップを持たずテキスト抽出ができないため、
`sips` で画像化して人間が読み取った結果を `data/manual/<Driveファイル ID>.csv`
に置く。同じ ID のファイルを読むときは、そちらが自動的に優先される。

現状 2 件（第2回 おかりん、第15回 おかりん）。他の PDF はスプレッドシート版が
同じフォルダにあるか、マスターDBに同内容が入っているため不要。

## 閲覧 GUI（web/）

React + Vite + TypeScript。タブは4つ。

- **検索** … 曲名 / アーティスト / アニメ・元ネタ の横断検索と、開催回・DJ・
  REMIX 有無での絞り込み
- **既出判定** … 曲名を1行ずつ貼ると過去のプレイ履歴が出る。
  「完全一致」と「原曲一致（別リミックス）」を分けて表示するので、
  セトリを組むときのネタ被り確認に使える
- **集計** … よくかかる曲・作品・アーティストのランキング、DJ 別の曲数と
  REMIX 率、開催回ごとの曲数（その回の初出曲と既出曲の積み上げ）
- **セトリ** … 開催回を選ぶと play順 に DJ が並び、各セトリを通読できる

DB は名寄せせずプレイログのまま持っているので、表記ゆれの吸収は
`web/src/lib/normalize.ts` がクエリ時に行う。`normKey()` が全角半角・記号・
空白を均し、`baseKey()` がさらにリミックス表記を落として原曲名に寄せる。

`npm run verify` で、この名寄せと集計を実データに当てた確認が走る。

## 判っている出典側の問題

`out/report.md` に毎回出力されるが、特筆すべきものを挙げる。

- **第14回 マスオ が回=15として入力されていた** … マスターDBの既知の誤り。
  `overrides.toml` の `fix` で取り込み時に補正している。
- **第10回 tri が71曲になっていた** … マスターDBがセトリの後ろに続く候補曲
  リスト52曲ぶんを一緒に取り込んでいた。実際に流したのは18曲。
- **セトリ未登録が5件** … 第14回 あちょ／ふっちー、第15回 マスオ／tri／あちょ。
  音源しか残っていない。
- **第1回 tri はファイル名からの推定** … `神社の人` フォルダの mp3 が
  `0-マーシャル・マキシマイザー_8bit_.mp3` のように曲順付きの曲名になっているため、
  そこから10曲を起こしている（`source_kind=filename` / `confidence=low`）。
  `ReボカロMIX` のようなマッシュアップの区間名やローマ字表記が混ざるので、
  他のセトリより精度は落ちる。外すなら `--no-infer-from-filenames`。

## DJ 名の表記ゆれ

`⛩` / `⛩️` / `神社の人` はいずれも **tri** で、`crawl.py` の `DJ_ALIASES` で
統合している。フォルダ名は `dj_folder` 列に元のまま残してあるので、
どのフォルダ由来かは追える。DJ フォルダの `01_` `xx_` といった接頭辞も
ここで落としている（接頭辞の数字は play順 として拾う）。
