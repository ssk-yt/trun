#!/usr/bin/env python3
"""trun -- HPC クラスタへのジョブ投入と、その記録の git 保存を1つの操作にまとめる CLI。

設計方針は SPEC.md を参照。標準ライブラリのみ、Python 3.8+。
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

VERSION = "0.1.0"

CONFIG_DIR = ".trun"
CONFIG_NAME = "config.toml"
IGNORE_NAME = ".trunignore"

# .trunignore からも config からも解除できない除外。
# POTCAR: ライセンス上ローカルに置かない
# .git  : 転送すると履歴が壊れる（pull 方向は致命的）
# .trun : リモートに置く意味がない
HARD_EXCLUDES = ["POTCAR", ".git", ".trun"]

REQUIRED_KEYS = ["host", "remote_root", "scheduler"]
SCHEDULERS = ["uge", "pbs", "slurm"]

SUBMIT_BIN = {"uge": "qsub", "pbs": "qsub", "slurm": "sbatch"}


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------

def die(msg, *rest):
    print("エラー: " + msg, file=sys.stderr)
    for line in rest:
        print("  " + line, file=sys.stderr)
    sys.exit(1)


def info(msg):
    # 子プロセス (rsync 等) の出力と順序が入れ替わらないよう毎回 flush する
    print(msg, flush=True)


# --------------------------------------------------------------------------
# ルート探索と設定
# --------------------------------------------------------------------------

def find_root(start):
    """start から上向きに .trun/ を探す。見つからなければ None。"""
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    while True:
        if (p / CONFIG_DIR).is_dir():
            return p
        if p == p.parent:
            return None
        p = p.parent


def require_root(start):
    root = find_root(start)
    if root is None:
        die("`.trun/` が見つかりません",
            "`trun init` でプロジェクトを初期化してください")
    return root


def parse_config(text):
    """`key = "value"` と `#` コメントだけを解釈する TOML のサブセット。

    tomllib は Python 3.11+ にしかなく、macOS の /usr/bin/python3 は 3.9 系のため
    使わない。config は 3 個のフラットな文字列キーだけなのでこれで足りる。
    """
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def load_config(root):
    path = Path(root) / CONFIG_DIR / CONFIG_NAME
    if not path.is_file():
        die("設定が見つかりません: {}".format(path),
            "`trun init` を実行してください")
    cfg = parse_config(path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        die("設定に必須キーがありません: {}".format(", ".join(missing)),
            "対象: {}".format(path))
    if cfg["scheduler"] not in SCHEDULERS:
        die("scheduler は {} のいずれかにしてください: {}".format(
            " / ".join(SCHEDULERS), cfg["scheduler"]))
    return cfg


# --------------------------------------------------------------------------
# パスの対応
# --------------------------------------------------------------------------

def remote_base(root, cfg):
    """<remote_root>/<プロジェクト名>"""
    return "{}/{}".format(cfg["remote_root"].rstrip("/"), Path(root).name)


def remote_for(root, cfg, rel):
    """trun ルートからの相対パス rel に対応するリモートのパス。"""
    base = remote_base(root, cfg)
    rel = Path(rel)
    if str(rel) in (".", ""):
        return base
    return "{}/{}".format(base, rel.as_posix())


def resolve_script(arg):
    """submit の引数を解決して (root, script, rel_dir) を返す。

    相対・絶対・~ のいずれでもよい。存在しなければここで落とす
    （ssh も rsync も叩く前に落とすため）。
    """
    script = Path(arg).expanduser()
    try:
        script = script.resolve()
    except OSError as e:
        die("パスを解決できません: {} ({})".format(arg, e))
    if not script.is_file():
        die("スクリプトが見つかりません: {}".format(script))
    root = find_root(script.parent)
    if root is None:
        die("スクリプトの位置から `.trun/` が見つかりません: {}".format(script),
            "trun ルート配下のスクリプトを指定してください")
    try:
        rel_dir = script.parent.relative_to(root)
    except ValueError:
        die("スクリプトが trun ルート配下にありません",
            "スクリプト: {}".format(script),
            "trun ルート: {}".format(root))
    return root, script, rel_dir


def label_for(root, rel_dir):
    """コミットメッセージ用の表示名。"""
    name = Path(root).name
    if str(rel_dir) in (".", ""):
        return name
    return "{}/{}".format(name, Path(rel_dir).as_posix())


# --------------------------------------------------------------------------
# rsync
# --------------------------------------------------------------------------

def build_excludes(root):
    """rsync に渡す除外オプション。HARD_EXCLUDES は常に含まれる。"""
    opts = []
    ignore = Path(root) / IGNORE_NAME
    if ignore.is_file():
        opts += ["--exclude-from", str(ignore)]
    for pat in HARD_EXCLUDES:
        opts += ["--exclude", pat]
    return opts


def rsync_push_cmd(root, cfg, dry_run=False):
    remote = remote_base(root, cfg)
    cmd = ["rsync", "-a", "-u"]
    if dry_run:
        # -v がないと --dry-run は何も表示しない
        cmd += ["--dry-run", "-v"]
    cmd += build_excludes(root)
    cmd += ["--rsync-path", "mkdir -p {} && rsync".format(shlex.quote(remote))]
    cmd += ["{}/".format(root), "{}:{}/".format(cfg["host"], remote)]
    return cmd


def rsync_pull_cmd(root, cfg, dry_run=False):
    remote = remote_base(root, cfg)
    cmd = ["rsync", "-a", "-u"]
    if dry_run:
        # -v がないと --dry-run は何も表示しない
        cmd += ["--dry-run", "-v"]
    cmd += build_excludes(root)
    cmd += ["{}:{}/".format(cfg["host"], remote), "{}/".format(root)]
    return cmd


# --------------------------------------------------------------------------
# 外部コマンド
# --------------------------------------------------------------------------

def run(cmd, capture=False, check=True):
    try:
        if capture:
            p = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
        else:
            p = subprocess.run(cmd)
    except FileNotFoundError:
        die("コマンドが見つかりません: {}".format(cmd[0]))
    if check and p.returncode != 0:
        if capture and p.stderr:
            sys.stderr.write(p.stderr)
        die("失敗しました (exit {}): {}".format(p.returncode, " ".join(cmd[:3]) + " ..."))
    return p


def ssh(cfg, remote_cmd, capture=True, check=True, login=False):
    """リモートでコマンドを実行する。

    login=True のときはログインシェルで包む。tsubame では非対話 ssh に
    スケジューラ (qsub/qstat) の PATH が通っていないため、これが必要になる。
    `$SHELL` はリモート側で展開されるので、相手が zsh でも bash でも効く。
    rsync は /usr/bin にあるため包む必要はない。
    """
    if login:
        remote_cmd = "$SHELL -lc {}".format(shlex.quote(remote_cmd))
    return run(["ssh", cfg["host"], remote_cmd], capture=capture, check=check)


def git(root, args, capture=False, check=True):
    return run(["git", "-C", str(root)] + args, capture=capture, check=check)


# --------------------------------------------------------------------------
# スケジューラ
# --------------------------------------------------------------------------

def parse_jobid(out, scheduler):
    """qsub / sbatch の出力から jobid を取り出す。

    tsubame では TSUBAME グループ未指定時に trial run の警告バナーが混ざるため、
    行位置ではなく正規表現で探す。
    """
    text = (out or "").strip()
    if not text:
        return None
    if scheduler == "uge":
        # 'Your job 8319143 ("band.sh") has been submitted'
        m = re.search(r"[Yy]our job(?:-array)?\s+(\d+)", text)
        return m.group(1) if m else None
    if scheduler == "slurm":
        m = re.search(r"(\d+)", text)
        return m.group(1) if m else None
    # PBS: "1234567.pbs-server" のように 1 行で返る
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"^\d+", line):
            return line
    return None


def probe_cmd():
    """スケジューラ判定用のリモートコマンド。

    `$SGE_ROOT` はログインシェルの中で展開されなければならない。
    quote せずに渡すと ssh が起動する非対話シェルが先に展開してしまい、
    値が空になって Grid Engine を PBS と誤判定する。
    """
    return "$SHELL -lc {}".format(
        shlex.quote("command -v qsub sbatch; echo SGE_ROOT=$SGE_ROOT"))


def submit_cmd(cfg, remote_dir, script_name):
    """リモートで実行する投入コマンドを組み立てる。

    submit_opts は qsub / sbatch にそのまま渡す（例: "-g <group>"）。
    trun はその中身を解釈しない。サイト固有の知識をツールに持たせないため。
    """
    parts = ["cd", shlex.quote(remote_dir), "&&", SUBMIT_BIN[cfg["scheduler"]]]
    opts = (cfg.get("submit_opts") or "").strip()
    if opts:
        parts.append(opts)
    parts.append(shlex.quote(script_name))
    return " ".join(parts)


def queue_cmd(cfg):
    """自分の走行中ジョブを一覧するコマンド。"""
    return "squeue -u $(whoami)" if cfg["scheduler"] == "slurm" else "qstat"


def fetch_queue(cfg):
    """スケジューラの一覧出力をそのまま返す。解析はしない。

    完了判定に必要なのは jobid が一覧に居るか居ないかだけであり、
    列を切り出す理由がない。各スケジューラの素の表示をそのまま見せる。
    スケジューラのコマンドは非対話 ssh に PATH が通っていないため
    ログインシェルで包む。
    """
    p = ssh(cfg, queue_cmd(cfg), check=False, login=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr or "")
        die("{} の実行に失敗しました".format(queue_cmd(cfg).split()[0]))
    return (p.stdout or "").rstrip("\n")


def warn_if_running(root, cfg, force, action):
    """走行中のジョブがあるときに確認を取る。push で計算中のファイルを潰さないため。"""
    if force:
        return
    try:
        out = fetch_queue(cfg)
    except SystemExit:
        raise
    except Exception:
        return
    if not out.strip():
        return
    print("警告: 走行中のジョブがあります", file=sys.stderr)
    for line in out.splitlines():
        print("  " + line, file=sys.stderr)
    print("{}すると計算中のファイルが上書きされる可能性があります。".format(action),
          file=sys.stderr)
    if not sys.stdin.isatty():
        die("走行中のジョブがあります", "--force を付けると無視して続行します")
    ans = input("続けますか? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        die("中断しました")


# --------------------------------------------------------------------------
# コマンド
# --------------------------------------------------------------------------

def ssh_hosts():
    path = Path.home() / ".ssh" / "config"
    if not path.is_file():
        return []
    hosts = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.lower().startswith("host "):
            for h in line.split()[1:]:
                if "*" not in h and "?" not in h:
                    hosts.append(h)
    return hosts


def ask(prompt, default=None):
    suffix = " [{}]".format(default) if default else ""
    ans = input("{}{}: ".format(prompt, suffix)).strip()
    return ans or (default or "")


def cmd_init(args):
    cwd = Path.cwd()

    p = run(["git", "rev-parse", "--is-inside-work-tree"], capture=True, check=False)
    if p.returncode != 0:
        die("git リポジトリの中ではありません",
            "trun は実行記録を git に残すため git を必要とします",
            "`git init` を実行してから再度お試しください")

    if (cwd / CONFIG_DIR).is_dir():
        die("すでに初期化されています: {}".format(cwd / CONFIG_DIR))

    outer = find_root(cwd)
    if outer is not None:
        print("上位に .trun/ があります: {}".format(outer), file=sys.stderr)
        print("入れ子にすると、このディレクトリ以下は新しい設定が使われます。", file=sys.stderr)
        if input("続けますか? [y/N] ").strip().lower() not in ("y", "yes"):
            die("中断しました")

    hosts = ssh_hosts()
    if hosts:
        info("~/.ssh/config の候補: {}".format(", ".join(hosts[:10])))
    host = ask("ssh のホスト名", hosts[0] if hosts else None)
    if not host:
        die("ホスト名は必須です")
    remote_root = ask("リモートのルート")
    if not remote_root:
        die("リモートのルートは必須です")

    info("スケジューラを確認しています...")
    scheduler = ""
    # ログインシェル経由。非対話 ssh では PATH が通っていないことがある。
    # 内側を quote しないと $SGE_ROOT を非対話シェルが先に展開して空になり、
    # Grid Engine を PBS と誤判定する
    p = run(["ssh", host, probe_cmd()], capture=True, check=False)
    out = p.stdout or ""
    if "sbatch" in out:
        scheduler = "slurm"
    elif "qsub" in out:
        # Grid Engine (UGE/AGE) と PBS はどちらも qsub。SGE_ROOT の有無で分ける
        scheduler = "uge" if re.search(r"SGE_ROOT=\S", out) else "pbs"
    label = {"uge": "qsub (Grid Engine)", "pbs": "qsub (PBS)", "slurm": "sbatch (Slurm)"}
    if scheduler:
        info("  {} を検出".format(label[scheduler]))
    else:
        info("  自動判定できませんでした")
        scheduler = ask("スケジューラ ({})".format(" / ".join(SCHEDULERS)), "uge")

    info("投入時の追加オプション（qsub/sbatch にそのまま渡す。例: -g <group>）")
    submit_opts = ask("  submit_opts", "")

    (cwd / CONFIG_DIR).mkdir()
    (cwd / CONFIG_DIR / CONFIG_NAME).write_text(
        'host = "{}"\nremote_root = "{}"\nscheduler = "{}"\nsubmit_opts = "{}"\n'
        .format(host, remote_root.rstrip("/"), scheduler, submit_opts),
        encoding="utf-8")
    ignore = cwd / IGNORE_NAME
    if not ignore.exists():
        ignore.write_text("", encoding="utf-8")

    info("")
    info("作成しました:")
    info("  {}/{}".format(CONFIG_DIR, CONFIG_NAME))
    info("  {}".format(IGNORE_NAME))
    info("")
    info("リモート: {}".format(remote_base(cwd, {"remote_root": remote_root})))


def do_push(root, cfg, dry_run=False):
    cmd = rsync_push_cmd(root, cfg, dry_run=dry_run)
    info("送信 {}/ → {}:{}/".format(Path(root).name, cfg["host"], remote_base(root, cfg)))
    run(cmd)


def cmd_push(args):
    root = require_root(Path.cwd())
    cfg = load_config(root)
    warn_if_running(root, cfg, args.force, "push")
    do_push(root, cfg, dry_run=args.dry_run)


def cmd_submit(args):
    root, script, rel_dir = resolve_script(args.script)
    cfg = load_config(root)
    warn_if_running(root, cfg, args.force, "投入")

    do_push(root, cfg)

    remote_dir = remote_for(root, cfg, rel_dir)
    remote_cmd = submit_cmd(cfg, remote_dir, script.name)
    # 何を投げたかがそのまま見えるよう、実際のリモートコマンドを表示する
    info("投入 {}".format(remote_cmd))
    p = ssh(cfg, remote_cmd, check=False, login=True)
    if p.returncode != 0:
        sys.stderr.write(p.stdout or "")
        sys.stderr.write(p.stderr or "")
        die("{} が失敗しました。記録は残していません".format(
            SUBMIT_BIN[cfg["scheduler"]]))

    jobid = parse_jobid(p.stdout, cfg["scheduler"])
    if not jobid:
        sys.stderr.write(p.stdout or "")
        die("jobid を取り出せませんでした。記録は残していません")
    info("  jobid {}".format(jobid))

    label = label_for(root, rel_dir)
    parts = ["run:", label, script.name]
    if args.message:
        parts.append(args.message)
    parts.append("jobid={}".format(jobid))
    msg = " ".join(parts)

    git(root, ["add", "."], capture=True, check=False)
    # --allow-empty: 前回と入力が同じでも投入の記録は必ず残す
    c = git(root, ["commit", "--allow-empty", "-m", msg], capture=True, check=False)
    if c.returncode != 0:
        sys.stderr.write(c.stdout or "")
        sys.stderr.write(c.stderr or "")
        print("", file=sys.stderr)
        print("!! ジョブは投入されましたが、記録のコミットに失敗しました", file=sys.stderr)
        print("!! jobid = {}".format(jobid), file=sys.stderr)
        print("!! 手動でコミットしてください: {}".format(msg), file=sys.stderr)
        sys.exit(1)
    sha = git(root, ["rev-parse", "--short", "HEAD"], capture=True).stdout.strip()
    info('コミット {} "{}"'.format(sha, msg))


def cmd_pull(args):
    root = require_root(Path.cwd())
    cfg = load_config(root)
    cmd = rsync_pull_cmd(root, cfg, dry_run=args.dry_run)
    info("回収 {}:{}/ → {}/".format(cfg["host"], remote_base(root, cfg), Path(root).name))
    run(cmd)
    if args.dry_run:
        return

    git(root, ["add", "."], capture=True, check=False)
    status = git(root, ["status", "--porcelain"], capture=True).stdout.strip()
    if not status:
        info("変更はありません")
        return
    msg = "pull: {}".format(Path(root).name)
    c = git(root, ["commit", "-m", msg], capture=True, check=False)
    if c.returncode != 0:
        sys.stderr.write(c.stdout or "")
        die("コミットに失敗しました")
    sha = git(root, ["rev-parse", "--short", "HEAD"], capture=True).stdout.strip()
    info('コミット {} "{}"'.format(sha, msg))


def cmd_stat(args):
    root = require_root(Path.cwd())
    cfg = load_config(root)
    out = fetch_queue(cfg)
    if not out.strip():
        info("走行中のジョブはありません")
        return
    print(out)


# --------------------------------------------------------------------------
# エントリポイント
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="trun",
        description="HPC クラスタへのジョブ投入と、その記録の git 保存を1つの操作にまとめる")
    p.add_argument("--version", action="version", version="trun {}".format(VERSION))
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="プロジェクトを初期化する")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("push", help="ローカル → クラスタ")
    s.add_argument("-f", "--force", action="store_true", help="走行中ジョブの確認を省略する")
    s.add_argument("-n", "--dry-run", action="store_true", help="転送せずに一覧だけ出す")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("submit", help="送信 → 投入 → コミット")
    s.add_argument("script", help="ジョブスクリプト（相対・絶対・~ のいずれでも可）")
    s.add_argument("-m", "--message", default="", help="コミットメッセージに足す説明")
    s.add_argument("-f", "--force", action="store_true", help="走行中ジョブの確認を省略する")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("stat", help="走行中のジョブ一覧")
    s.set_defaults(func=cmd_stat)

    s = sub.add_parser("pull", help="クラスタ → ローカル → コミット")
    s.add_argument("-n", "--dry-run", action="store_true", help="転送せずに一覧だけ出す")
    s.set_defaults(func=cmd_pull)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        args.func(args)
    except KeyboardInterrupt:
        die("中断しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
