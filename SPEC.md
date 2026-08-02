# trun 仕様書

v0.1 / 2026-08-02

HPC クラスタへのジョブ投入と、その実行記録の git への保存を1つの操作にまとめる CLI。

---

## 1. 目的と思想

### 目的

AI エージェント（Claude Code）が「投入 → 待機 → 解析」のループを回すとき、
**実行の記録が副作用として必ず残る**ようにする。

記録の欠落は規約では防げない。「実行したらログを残せ」は破られるが、
投入手段が `trun submit` しかなければ破れない。これがこのツールの存在理由である。

### 設計原則

**1. 真実を新造しない**

記録はすでに存在している。git が入力の履歴を、`OUTCAR` 等の出力が実行の事実を、
スケジューラのログ（`*.o<jobid>` / `*.e<jobid>`）が異常終了の事実を持つ。
trun はこれらを1つの操作に束ねるだけで、独自の索引・スキーマ・状態ファイルを持たない。

真実の写しを持てば、それは必ず腐る。

**2. 記録は規約ではなく副作用**

`trun submit` を打てば記録が残る。「記録しよう」と判断する余地を作らない。

**3. 揮発する状態と永続する記録を混ぜない**

走行中のジョブの一覧はスケジューラ（`qstat`）が持つ。trun は写しを持たない。
過去の記録は git が持つ。trun は写しを持たない。

**4. 成否を判定しない**

`qstat` から消えた = ジョブは終わった。成功したかは判定しない。
VASP は exit 0 でもイオン緩和が収束していないことが普通にある。
exit code は弱く、誤解を招く信号である。成否の解釈は、次に結果を読む人間または AI が行う。

**5. ドメイン知識を持たない**

VASP のファイル名も、vise の呼び方も、trun は知らない。
それらはユーザーが書くジョブスクリプトの中にある。
結果として VASP / QE / LAMMPS のいずれでも同じコードで動く。

**6. コマンドライン1行が実行内容の完全な記述である**

引数の自動選択・推測をしない。
Claude が実行しているシェルを人間が横から見て、何が起きるか分からない状態を作らない。

**7. ディレクトリ構成に干渉しない**

vise や他のツールが作った構造をそのまま使う。trun が要求する配置はない。

---

## 2. 用語

| 用語 | 意味 |
|---|---|
| trun ルート | `.trun/` ディレクトリを含むディレクトリ。プロジェクトの単位 |
| リモートルート | クラスタ側の対応するディレクトリ。`config.toml` の `remote_root` |
| 計算ディレクトリ | ジョブスクリプトと入出力が置かれるディレクトリ。trun ルートの任意の子孫 |

trun ルートは git リポジトリのルートと一致しなくてよい。
git は trun ルートから上向きに自動でリポジトリを探すため、trun は git ルートを知る必要がない。

---

## 3. ファイル構成

```
<trun ルート>/
  .trun/
    config.toml       # 3行
  .trunignore         # 転送除外。空でも可
  <計算ディレクトリ>/
    <script>.sh       # ユーザーが書く。vise 等の呼び出しはこの中
    INCAR POSCAR ...  # 入力
    OUTCAR OSZICAR    # 出力（pull で降りてくる）
    *.o<jobid>        # スケジューラの標準出力
    *.e<jobid>        # スケジューラの標準エラー
```

trun が作るファイルは `.trun/config.toml` と `.trunignore` の2つだけ。
状態ファイル・索引ファイル・生成スクリプトは一切作らない。

### `.trun/config.toml`

```toml
host = "tsubame"                              # ~/.ssh/config のエイリアス
remote_root = "/gs/bs/<group>/<user>"       # クラスタ側のルート
scheduler = "uge"                             # "uge" | "pbs" | "slurm"
submit_opts = "-g <group>"                  # 任意。qsub/sbatch にそのまま渡す
```

tsubame は **Grid Engine (AGE 2023.1.1)** であり PBS ではない。`uge` を使う。

必須は上 3 つ。`submit_opts` は省略可で、これ以外のキーは v0.1 では持たない。

`submit_opts` の中身を trun は解釈しない。サイト固有の知識をツールに持たせないためで、
Grid Engine の `-g <group>`、Slurm の `-A <account>`、PBS の `-P <project>` を
同じキーに書ける。TSUBAME ではグループ未指定だと trial run 扱いになり
walltime が 3 分に制限されるため、実質必須になる。

### `.trunignore`

`.gitignore` と同じ書式。trun ルートに1枚。rsync の `--exclude-from` にそのまま渡す。
空でも動作する。サブディレクトリごとの `.trunignore` は v0.1 では扱わない。

### 解除不可の除外（ハードコード）

以下は `.trunignore` からも `config.toml` からも解除できない。

| 対象 | 理由 |
|---|---|
| `POTCAR` | ライセンス上ローカルに置かない。AI に読ませない |
| `.git` | 転送すると履歴が壊れる。pull 方向は致命的 |
| `.trun` | リモートに置く意味がない |

---

## 4. パスの解決

`trun` はどのディレクトリから呼ばれてもよい。上向きに `.trun/` を探索して trun ルートを決める。

探索の起点はコマンドによって異なる。

| コマンド | 起点 |
|---|---|
| `submit <script>` | `<script>` を絶対パスに解決した親ディレクトリ |
| `init` / `push` / `pull` / `stat` | cwd |

`submit` だけスクリプトを起点にするのは、別プロジェクトのディレクトリにいても
パスさえ渡せば投入できるようにするため。

```
起点 = ~/Personal/Research/Si-band/band
 └─ 上向き探索 → trun ルート = ~/Personal/Research/Si-band
                  プロジェクト名 = "Si-band"
```

リモートのパスは、trun ルートからの相対パスをそのまま写す。

```
ローカル  <trun ルート>/band
リモート  <remote_root>/<プロジェクト名>/band
        = /gs/bs/<group>/<user>/Si-band/band
```

対応表を持たない。ツリー構造そのものが対応である。

`.trun/` が見つからない場合はエラーとし、`trun init` を促す。

---

## 5. コマンド

### `trun init`

cwd に `.trun/config.toml` と空の `.trunignore` を作る。

1. cwd が git リポジトリ配下かを確認。そうでなければエラーとし `git init` を促す
2. 上向きに既存の `.trun/` を探索。見つかった場合は入れ子になる旨を警告し確認を取る
3. 対話的に `host` / `remote_root` を尋ねる。`~/.ssh/config` の Host 一覧を候補として提示する
4. `ssh <host> '$SHELL -lc "command -v qsub sbatch; echo SGE_ROOT=$SGE_ROOT"'` で判定。
   `sbatch` があれば `slurm`、`qsub` のみなら `SGE_ROOT` の有無で `uge` / `pbs` を分ける
5. `.trun/config.toml` と `.trunignore` を書き出す

**完了条件**: 3行の `config.toml` が生成され、`trun push` が動作する。

### `trun push`

trun ルート配下をクラスタへ送る。ジョブは投入しない。

```
rsync -a -u \
      --exclude-from=<trun ルート>/.trunignore \
      --exclude=POTCAR --exclude=.git --exclude=.trun \
      --rsync-path="mkdir -p <remote> && rsync" \
      <trun ルート>/  <host>:<remote>/
```

- `-u` により、宛先の方が新しいファイルは送らない（走行中の出力を潰さない）
- `--delete` は付けない。リモートのファイルは消えない
- 走行中のジョブがある場合は警告し確認を取る（`--force` で省略可）

**完了条件**: ローカルの変更がリモートに反映され、`.git` が転送対象に含まれない。

### `trun submit <script>`

引数は必須。省略・自動選択はしない。相対パス・絶対パス・`~` のいずれでもよい。

```
trun submit band.sh                                    # cwd からの相対
trun submit band/band.sh                               # 相対
trun submit ~/Personal/Research/Si-band/band/band.sh   # 絶対
```

**引数の解決**

1. `Path(arg).expanduser().resolve()` で絶対パスに解決する
2. ファイルが存在しなければエラー（`qsub` を叩く前に落とす）
3. 解決した親ディレクトリから上向きに `.trun/` を探索し、trun ルートを決める
4. 見つからなければエラー。見つかった場合、そこからの相対パスでリモートの `cd` 先を決める

```
引数        band/band.sh
解決        /Users/…/Research/Si-band/band/band.sh
trun ルート /Users/…/Research/Si-band
相対        band
cd 先       /gs/bs/<group>/<user>/Si-band/band
qsub 対象   band.sh
```

**実行手順**

1. `push` と同じ rsync を実行
2. `ssh <host> '$SHELL -lc "cd <cd 先> && qsub <submit_opts> <basename>"'`
   実際に叩くリモートコマンドをそのまま表示する（何が走ったかがログに残るように）
3. 標準出力から jobid を抽出（下表）
4. `git -C <trun ルート> add .`
5. `git -C <trun ルート> commit -m "run: <プロジェクト>/<dir> <script> jobid=<id>"`
6. jobid を表示

`-m "<説明>"` でコミットメッセージに説明を追加できる。

**jobid の抽出**

| scheduler | 出力 | 抽出 |
|---|---|---|
| `uge` | `Your job 8319143 ("band.sh") has been submitted` | `[Yy]our job(-array)?\s+(\d+)` |
| `pbs` | `1234567.pbs-server` | 数字で始まる最初の行 |
| `slurm` | `Submitted batch job 1234567` | 最初の数字列 |

tsubame では TSUBAME グループ未指定時に trial run の警告バナーが出力に混ざるため、
行位置ではなく正規表現で探す。

**失敗時**:

- 引数のファイルが存在しない → エラー。`ssh` も `rsync` も実行しない
- 解決したパスが trun ルート配下にない → エラー（リモートに対応する場所がないため）
- 1 で失敗 → 中断。何も記録しない
- 2 で `qsub` が非ゼロ → コミットせず、`qsub` の stderr をそのまま出力。
  存在しないジョブの記録を残さない
- 4〜5 で失敗 → jobid を stderr に強調表示し、手動でのコミットを促す
  （ジョブは走っているため、記録の欠落を明示する）

**完了条件**: jobid が返り、その jobid を含むコミットが1つ積まれる。

### `trun stat`

スケジューラの一覧出力をそのまま表示する。解析しない。

```
$ trun stat
job-ID     prior   name       user         state submit/start at     queue
------------------------------------------------------------------------------
   8319143 0.55354 band.sh    user01       r     08/02/2026 12:58:31 all.q@node01
```

| scheduler | コマンド |
|---|---|
| `uge` / `pbs` | `qstat`（引数なしで自分のジョブだけが出る） |
| `slurm` | `squeue -u $(whoami)` |

- 列を切り出さない。完了判定に必要なのは jobid が一覧に居るか居ないかだけであり、
  整形する理由がない。環境ごとの素の表示をそのまま見せる方が壊れにくい
- 出力が空なら「走行中のジョブはありません」と出す
- どのジョブがどのディレクトリのものかは `git log --grep=<jobid>` で引ける

**ログインシェルが必要**: tsubame では非対話 ssh に `qstat` / `qsub` の PATH が通っていない。
スケジューラのコマンドは `$SHELL -lc "..."` で包む。`$SHELL` はリモート側で展開されるため、
相手が zsh でも bash でも効く。`rsync` は `/usr/bin/rsync` にあるため包む必要はない。

**完了条件**: 走行中のジョブが表示され、完了したジョブが消える。

### `trun pull`

trun ルート配下をクラスタから回収し、コミットする。引数はない（常に全体）。

```
rsync -a -u \
      --exclude-from=<trun ルート>/.trunignore \
      --exclude=POTCAR --exclude=.git --exclude=.trun \
      <host>:<remote>/  <trun ルート>/

git -C <trun ルート> add .
git -C <trun ルート> commit -m "pull: <プロジェクト>"
```

- 完了を待たない。走行中でも途中経過（`OSZICAR` 等）を取得できる
- `-u` によりローカルで編集したファイルは上書きされない
- 変更が無い場合はコミットを作らない

**完了条件**: リモートの出力がローカルに降り、コミットが1つ積まれる。
`.git` が上書きされない。

---

## 6. git との関係

- コミットはローカルのリポジトリにのみ積まれる。クラスタ側に git は置かない
- クラスタ側は単なるディレクトリである。同期は rsync のみが担う
- `git -C <trun ルート>` を使うため、trun がどこから呼ばれても動作は同じ

### なぜクラスタを git のリモートにしないか

`receive.denyCurrentBranch = updateInstead` を使えば push で作業ツリーを更新できるが、
ジョブスクリプト内の vise 等が `INCAR` を再生成するため、リモートの作業ツリーは常に dirty になる。
push は毎回拒否される。したがってこの方式は成立しない。

### ブランチ

クラスタ側にブランチの概念はない。最後に push した内容が置かれているだけである。

したがって **計算条件を変えるときはブランチではなくディレクトリを分ける**。
これは VASP の作法と一致し、リモートにも git にも同じ形で残る。

ブランチが有効なのは、クラスタに写さないもの（解析スクリプト、プロット、ドキュメント）に限る。

---

## 7. 記録される内容

```
$ git log --oneline
8f2a1c9 pull: Si-band
4a71e02 run: Si-band/dos dos.sh DOS NEDOS=3000 jobid=1234568
b39f7d2 run: Si-band/band band.sh jobid=1234567
e07c443 pull: Si-band
9a12ff8 run: Si-band/unitcell relax.sh ENCUT収束 400-700eV jobid=1234501
```

- `git show <sha>:<path>/INCAR` — 投入時点の入力
- `git log --grep=<jobid>` — 特定のジョブに関わる全コミット
- `git log -p <path>/<script>` — スクリプトの変更履歴

別セッションの AI エージェントは `git log --oneline -20` を読むだけで経緯を把握できる。
そのための専用ファイル（STATE.md 等）は作らない。生成物は腐るため。

---

## 8. 非目標

以下は意図的に作らない。

| 項目 | 理由 |
|---|---|
| 完了待ち（`twait`） | エージェントは数時間ブロックできない。`stat` を見て `pull` すればよい |
| 実行記録の索引（JSONL 等） | git が持つ情報の写しであり、腐る |
| 現況ファイル（STATE.md） | 同上。`git log` で足りる |
| 走行中ジョブの状態ファイル | `qstat` が持つ情報の写し |
| 成否の判定 | exit code は弱く誤解を招く。解釈は読む側が行う |
| ラッパスクリプトの生成 | 生成物を増やさない。ユーザーのスクリプトをそのまま `qsub` する |
| ファイルサイズによる転送制御 | 名前でもサイズでも、除外は `.trunignore` に一本化する |
| 重いファイルの遅延取得 | 同上 |
| クラスタ側の git 運用 | 成立しない（§6） |

---

## 9. 実装方針

- **Python 3.8+、単一ファイル `trun.py`、標準ライブラリのみ**
  （`subprocess` / `argparse` / `pathlib` / `shlex` / `json`）
- サードパーティ依存がないため、パッケージングを行わない
- 想定行数 300〜400 行

**`tomllib` を使わない理由**: `tomllib` は Python 3.11 以降にしかない。
macOS の `/usr/bin/python3` は 3.9 系であり、`#!/usr/bin/env python3` で配ると動かない。
config は 3 個のフラットな文字列キーだけなので、`key = "value"` と `#` コメントだけを解釈する
10 行程度の自前パーサで足りる。ファイルは valid TOML のままであり、
将来 3.11+ を前提にできるようになれば `tomllib` に差し替えられる。

同じ理由で、3.10 以降の構文（`X | Y` 形式の型注釈など）は使わない。

### install

```bash
curl -Lo ~/.local/bin/trun https://raw.githubusercontent.com/<user>/trun/main/trun.py
chmod +x ~/.local/bin/trun
```

`#!/usr/bin/env python3` を先頭に置く。`~/.local/bin` が PATH にあることを README に記載する。

将来サードパーティ依存が必要になった場合は PEP 723 のインラインメタデータを使い、
単一ファイル配布を維持する。

### 開発

- GitHub の独立リポジトリ。研究データのリポジトリ（`~/Personal/Research`）とは分離する
- タスクは issue として立てる。issue 本文に「これが通れば完了」を1行書く
- テストは pytest。issue の完了条件はテストで機械判定できる形にする

---

## 10. 既知の課題

| # | 内容 |
|---|---|
| 1 | PBS / Slurm のバックエンドは実装したが未検証。実機で検証済みなのは `uge` (tsubame) のみ |
| 2 | `submit_opts` は config を見ないと何が渡るか分からない。投入時にリモートコマンドを全文表示することで緩和している |
| 3 | `trun submit` を使わず `ssh <host> qsub` を直接叩けば記録が残らない。ツールでは防げない。Claude Code の hook による禁止を README で推奨設定として案内する |
| 4 | zsh 補完は未対応 |
| 5 | サブディレクトリごとの `.trunignore` は未対応 |
| 6 | `submit` の引数解決に `Path.resolve()` を使うため、シンボリックリンクは辿られる。trun ルート外を指すリンクを渡すとエラーになる。実害が出たら `..` の正規化のみ行う方式に変える |

---

## 11. issue 一覧（v0.1）

| # | 内容 | 完了条件 |
|---|---|---|
| 1 | config 読み込みと trun ルート探索、除外規則の基盤 | 3行の TOML を読み、任意の cwd からルートを解決し、`.git` が除外対象に入る |
| 2 | `trun init` | 対話で 3 行の config が生成され、既存 `.trun/` の入れ子を警告する |
| 3 | `trun push` | ローカルの変更がリモートに反映され、`.git` が転送されない |
| 4 | `trun submit <script>` | 相対・絶対・`~` のいずれのパスでも同じ `cd` 先が導かれる。存在しないパスは `ssh` 前に落ちる。jobid が返り、jobid を含むコミットが1つ積まれる。`qsub` 失敗時にコミットしない |
| 5 | `trun pull` | リモートの出力が降り、コミットが1つ積まれる。ローカルの編集が上書きされない |
| 6 | `trun stat` | 走行中ジョブと作業ディレクトリが対応付けて表示される |
| 7 | README と install 手順 | 記載通りの手順で別マシンに install でき、`trun --version` が動く |

3〜5 が通ればループが回る。6〜7 は後続でよい。
