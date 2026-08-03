# trun

HPC クラスタへのジョブ投入と、その実行記録の git への保存を1つの操作にまとめる CLI。

`trun submit` を打つと、投入と同時に jobid 入りのコミットが積まれる。
記録を残すかどうかを判断する余地がない。これがこのツールの存在理由。

```
$ git log --oneline
8f2a1c9 pull: Si-band
b39f7d2 run: Si-band/band band.sh jobid=8319143
9a12ff8 run: Si-band/unitcell relax.sh ENCUT収束 400-700eV jobid=8319101
```

## install

依存パッケージなし、Python 3.8+ の単一ファイル。

```bash
curl -Lo ~/.local/bin/trun https://raw.githubusercontent.com/ssk-yt/trun/main/trun.py
chmod +x ~/.local/bin/trun
trun --version
```

`~/.local/bin` が PATH にない場合は zsh に1行足す。

```zsh
# ~/.zshrc
export PATH="$HOME/.local/bin:$PATH"
```

更新は同じ `curl` をもう一度実行するだけ。

## セットアップ

プロジェクト（git リポジトリ配下の任意のディレクトリ）で1回だけ。

```
$ cd ~/research/Si-band
$ trun init
~/.ssh/config の候補: tsubame, github.com
ssh のホスト名 [tsubame]:
リモートのルート: /gs/bs/<group>/<user>
スケジューラを確認しています...
  qsub (Grid Engine) を検出

作成しました:
  .trun/config.toml
  .trunignore
```

`.trun/config.toml` は3行。

```toml
host = "tsubame"
remote_root = "/gs/bs/<group>/<user>"
scheduler = "uge"                 # "uge" | "pbs" | "slurm"
submit_opts = "-g <group>"      # 任意。qsub/sbatch にそのまま渡す
after_push = "uv sync"            # 任意。push のたびにリモートで実行
```

`submit_opts` の中身は解釈せずそのまま渡す。Grid Engine の `-g <group>`、
Slurm の `-A <account>`、PBS の `-P <project>` を同じキーに書ける。
TSUBAME はグループ未指定だと trial run 扱いで walltime 3 分に制限されるので、実質必須。

tsubame は Grid Engine (AGE) なので `uge`。`trun init` が自動判定する。

リモートのパスは、trun ルートからの相対パスをそのまま写す。

```
ローカル  ~/research/Si-band/band
リモート  /gs/bs/<group>/<user>/Si-band/band
```

### 転送しないものをリモートで再構成する

Python の仮想環境のようにビルド済みバイナリを含むものは、macOS から Linux へ
送っても動かない。`.trunignore` で転送を止め、`after_push` でリモート側に作らせる。

```
# .trunignore
.venv/
```

```toml
# .trun/config.toml
after_push = "$HOME/.local/bin/uv sync"
```

`uv.lock` は転送されるので、リモートでも同一バージョンの環境が再現される。
`after_push` の中身は解釈しないため、conda なら `conda env update -f environment.yml`、
make なら `make` を書けばよい。`submit` では `qsub` の前に走り、失敗したら投入しない。

`$SHELL -lc` は `.zshrc` を読まないので、`~/.local/bin` に入れたコマンドは
絶対パスで書くこと。

## 使い方

```bash
trun push                      # ローカル → クラスタ（ジョブは投げない）
trun submit band/band.sh       # 送信 → qsub → jobid 入りでコミット
trun stat                      # 走行中のジョブ一覧
trun pull                      # クラスタ → ローカル → コミット
```

ジョブスクリプトはユーザーが書く。`trun` は中身を知らない。

```bash
#!/bin/bash
#$ -cwd
#$ -l cpu_4=1
#$ -l h_rt=1:00:00
vise vs -t band
mpirun vasp_std
```

投入。引数は必須で、相対・絶対・`~` のいずれでもよい。

```
$ trun submit band/band.sh -m "ENCUT=520, 8x8x8"
送信 Si-band/ → tsubame:/gs/bs/<group>/<user>/Si-band/
投入 cd /gs/bs/<group>/<user>/Si-band/band && qsub -g <group> band.sh
  jobid 8319143
コミット b39f7d2 "run: Si-band/band band.sh ENCUT=520, 8x8x8 jobid=8319143"
```

すぐプロンプトが戻る。完了は待たない。

```
$ trun stat
job-ID     prior   name       user         state submit/start at     queue
------------------------------------------------------------------------------
   8319143 0.55354 band.sh    user01       r     08/02/2026 12:58:31 all.q@node01
```

`qstat`（Slurm なら `squeue`）の出力をそのまま表示する。整形しない。
どのジョブがどのディレクトリのものかは `git log --grep=<jobid>` で引ける。

回収。完了を待たないので、走行中でも途中経過（`OSZICAR` 等）を取れる。

```
$ trun pull
回収 tsubame:/gs/bs/<group>/<user>/Si-band/ → Si-band/
コミット 8f2a1c9 "pull: Si-band"
```

## 転送の除外

`.trunignore` に `.gitignore` と同じ書式で書く。push と pull の両方に効く。

```
WAVECAR
CHGCAR
```

以下は `.trunignore` からも設定からも解除できない。

| 対象 | 理由 |
|---|---|
| `POTCAR` | ライセンス上ローカルに置かない |
| `.git` | 転送すると履歴が壊れる |
| `.trun` | リモートに置く意味がない |

転送は常に `rsync -a -u`。`--delete` は使わないため、**どちらの側でもファイルが消えることはない**。
`-u` により、宛先の方が新しいファイルは送らない（走行中の出力を push で潰さない）。

## 設計

独自の索引・状態ファイル・スキーマを持たない。記録はすでに存在しているものに任せる。

- **git** — 入力の履歴
- **計算コードの出力**（`OUTCAR` 等）— 実行の事実
- **スケジューラのログ**（`*.o<jobid>`）— 異常終了の事実

`trun` が作るファイルは `.trun/config.toml` と `.trunignore` の2つだけ。
走行中のジョブは `qstat` に聞くので、ローカルに写しを持たない。
計算コードのファイル名も知らないため、VASP / QE / LAMMPS のいずれでも動く。

計算条件を変えるときはブランチではなくディレクトリを分ける。
クラスタ側にブランチの概念はなく、最後に push した内容が置かれているだけのため。

詳細と、意図的に作らないものの一覧は [SPEC.md](SPEC.md) を参照。

## 推奨設定（AI エージェントと併用する場合）

`trun` を経由せず `ssh <host> qsub ...` を直接叩けば記録は残らない。
これはツール側では防げないので、Claude Code の hook で禁止するとよい。

`.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "grep -qE 'ssh .*(qsub|sbatch)' <<< \"$CLAUDE_TOOL_INPUT\" && { echo 'ジョブ投入は trun submit を使ってください' >&2; exit 2; }; exit 0"
          }
        ]
      }
    ]
  }
}
```

あわせてプロジェクトの `CLAUDE.md` に書いておく。

```markdown
## 計算の実行
tsubame への投入は必ず `trun submit <script>` を使う。ssh や qsub を直接叩かない。
現況は `git log --oneline -20` で把握する。
```

## 開発

```bash
uv run --with pytest pytest -q
```

タスクは issue として管理する。issue 本文の1行目に完了条件を書く。

## ライセンス

MIT
