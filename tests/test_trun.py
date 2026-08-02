import json
from pathlib import Path

import pytest

import trun


CONFIG = 'host = "tsubame"\nremote_root = "/scratch/user"\nscheduler = "uge"\n'


def make_project(tmp_path, name="Si-band", config=CONFIG, ignore=None):
    root = tmp_path / name
    (root / trun.CONFIG_DIR).mkdir(parents=True)
    (root / trun.CONFIG_DIR / trun.CONFIG_NAME).write_text(config)
    if ignore is not None:
        (root / trun.IGNORE_NAME).write_text(ignore)
    return root.resolve()


# ---------------------------------------------------------------- ルート探索

def test_find_root_from_nested_dir(tmp_path):
    root = make_project(tmp_path)
    deep = root / "band" / "sub"
    deep.mkdir(parents=True)
    assert trun.find_root(deep) == root


def test_find_root_from_file(tmp_path):
    root = make_project(tmp_path)
    f = root / "band" / "band.sh"
    f.parent.mkdir()
    f.write_text("#!/bin/bash\n")
    assert trun.find_root(f) == root


def test_find_root_returns_none_outside(tmp_path):
    outside = tmp_path / "nowhere"
    outside.mkdir()
    assert trun.find_root(outside) is None


def test_find_root_picks_nearest(tmp_path):
    outer = make_project(tmp_path, "outer")
    inner = outer / "inner"
    (inner / trun.CONFIG_DIR).mkdir(parents=True)
    (inner / trun.CONFIG_DIR / trun.CONFIG_NAME).write_text(CONFIG)
    assert trun.find_root(inner / "band") == inner.resolve()


# -------------------------------------------------------------------- config

def test_parse_config_handles_comments_and_quotes():
    cfg = trun.parse_config(
        '# comment\nhost = "tsubame"  # 末尾コメント\n\nscheduler = pbs\n')
    assert cfg["host"] == "tsubame"
    assert cfg["scheduler"] == "pbs"


def test_load_config_ok(tmp_path):
    root = make_project(tmp_path)
    cfg = trun.load_config(root)
    assert cfg["host"] == "tsubame"
    assert cfg["remote_root"] == "/scratch/user"
    assert cfg["scheduler"] == "uge"


def test_load_config_missing_key(tmp_path):
    root = make_project(tmp_path, config='host = "tsubame"\n')
    with pytest.raises(SystemExit):
        trun.load_config(root)


def test_load_config_bad_scheduler(tmp_path):
    root = make_project(
        tmp_path,
        config='host = "a"\nremote_root = "/b"\nscheduler = "torque"\n')
    with pytest.raises(SystemExit):
        trun.load_config(root)


# ------------------------------------------------------------------ 除外規則

def test_hard_excludes_always_present(tmp_path):
    root = make_project(tmp_path)
    opts = trun.build_excludes(root)
    for pat in ("POTCAR", ".git", ".trun"):
        assert pat in opts


def test_trunignore_used_when_present(tmp_path):
    root = make_project(tmp_path, ignore="WAVECAR\n")
    opts = trun.build_excludes(root)
    assert "--exclude-from" in opts
    assert str(root / trun.IGNORE_NAME) in opts


def test_trunignore_optional(tmp_path):
    root = make_project(tmp_path)
    assert "--exclude-from" not in trun.build_excludes(root)


def test_trunignore_cannot_disable_hard_excludes(tmp_path):
    # .trunignore に何を書いてもハードコードの除外は残る
    root = make_project(tmp_path, ignore="!POTCAR\n!.git\n")
    opts = trun.build_excludes(root)
    assert opts.count("--exclude") == len(trun.HARD_EXCLUDES)
    assert ".git" in opts


@pytest.mark.parametrize("builder", [trun.rsync_push_cmd, trun.rsync_pull_cmd])
def test_rsync_never_transfers_git(tmp_path, builder):
    root = make_project(tmp_path)
    cmd = builder(root, trun.load_config(root))
    assert cmd[0] == "rsync"
    assert "-u" in cmd
    assert "--delete" not in cmd
    for pat in (".git", ".trun", "POTCAR"):
        assert cmd[cmd.index(pat) - 1] == "--exclude"


def test_push_creates_remote_dir(tmp_path):
    root = make_project(tmp_path)
    cmd = trun.rsync_push_cmd(root, trun.load_config(root))
    i = cmd.index("--rsync-path")
    assert cmd[i + 1].startswith("mkdir -p ")
    assert cmd[-1] == "tsubame:/scratch/user/Si-band/"


def test_dry_run_is_verbose(tmp_path):
    """-v がないと --dry-run は何も表示しない"""
    root = make_project(tmp_path)
    cfg = trun.load_config(root)
    for builder in (trun.rsync_push_cmd, trun.rsync_pull_cmd):
        cmd = builder(root, cfg, dry_run=True)
        assert "--dry-run" in cmd and "-v" in cmd


def test_pull_direction(tmp_path):
    root = make_project(tmp_path)
    cmd = trun.rsync_pull_cmd(root, trun.load_config(root))
    assert cmd[-2] == "tsubame:/scratch/user/Si-band/"
    assert cmd[-1] == "{}/".format(root)


# ------------------------------------------------------------ パスの対応付け

def test_remote_base(tmp_path):
    root = make_project(tmp_path)
    assert trun.remote_base(root, trun.load_config(root)) == \
        "/scratch/user/Si-band"


def test_remote_for(tmp_path):
    root = make_project(tmp_path)
    cfg = trun.load_config(root)
    assert trun.remote_for(root, cfg, Path("band")) == \
        "/scratch/user/Si-band/band"
    assert trun.remote_for(root, cfg, Path(".")) == "/scratch/user/Si-band"


def test_label_for(tmp_path):
    root = make_project(tmp_path)
    assert trun.label_for(root, Path("band")) == "Si-band/band"
    assert trun.label_for(root, Path(".")) == "Si-band"


# ------------------------------------------------------- submit の引数解決

def _script(tmp_path):
    root = make_project(tmp_path)
    d = root / "band"
    d.mkdir()
    f = d / "band.sh"
    f.write_text("#!/bin/bash\nmpirun vasp_std\n")
    return root, f


def test_resolve_script_absolute(tmp_path):
    root, f = _script(tmp_path)
    assert trun.resolve_script(str(f)) == (root, f.resolve(), Path("band"))


def test_resolve_script_relative(tmp_path, monkeypatch):
    root, f = _script(tmp_path)
    monkeypatch.chdir(root)
    assert trun.resolve_script("band/band.sh") == (root, f.resolve(), Path("band"))
    monkeypatch.chdir(root / "band")
    assert trun.resolve_script("band.sh") == (root, f.resolve(), Path("band"))


def test_resolve_script_tilde(tmp_path, monkeypatch):
    root, f = _script(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    got = trun.resolve_script("~/Si-band/band/band.sh")
    assert got == (root, f.resolve(), Path("band"))


def test_resolve_script_all_forms_agree(tmp_path, monkeypatch):
    """相対・絶対・~ のいずれでも同じ cd 先が導かれること（issue #4 の完了条件）"""
    root, f = _script(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(root / "band")
    cfg = trun.load_config(root)
    remotes = set()
    for arg in ["band.sh", str(f), "~/Si-band/band/band.sh", "../band/band.sh"]:
        r, _, rel = trun.resolve_script(arg)
        remotes.add(trun.remote_for(r, cfg, rel))
    assert remotes == {"/scratch/user/Si-band/band"}


def test_resolve_script_missing_file(tmp_path, monkeypatch):
    root, _ = _script(tmp_path)
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit):
        trun.resolve_script("band/nope.sh")


def test_resolve_script_outside_root(tmp_path):
    make_project(tmp_path)
    stray = tmp_path / "stray.sh"
    stray.write_text("#!/bin/bash\n")
    with pytest.raises(SystemExit):
        trun.resolve_script(str(stray))


# --------------------------------------------------------------- jobid 解析

def test_parse_jobid_uge():
    # trial run のバナーが混ざっても jobid を取り出せること
    out = ("You are using the trial run feature as you did not specify a "
           "TSUBAME group.\n"
           'Your job 8319143 ("band.sh") has been submitted\n')
    assert trun.parse_jobid(out, "uge") == "8319143"


def test_parse_jobid_uge_array():
    out = 'Your job-array 8319144.1-4:1 ("band.sh") has been submitted\n'
    assert trun.parse_jobid(out, "uge") == "8319144"


def test_parse_jobid_pbs():
    assert trun.parse_jobid("1234567.pbs\n", "pbs") == "1234567.pbs"


def test_parse_jobid_slurm():
    assert trun.parse_jobid("Submitted batch job 1234567\n", "slurm") == "1234567"


def test_parse_jobid_empty():
    assert trun.parse_jobid("", "uge") is None


# ---------------------------------------------------------------- 投入コマンド

def test_submit_cmd_without_opts(tmp_path):
    root = make_project(tmp_path)
    cmd = trun.submit_cmd(trun.load_config(root), "/gs/x/band", "band.sh")
    assert cmd == "cd /gs/x/band && qsub band.sh"


def test_submit_cmd_with_opts(tmp_path):
    root = make_project(
        tmp_path, "P",
        config=('host = "h"\nremote_root = "/r"\nscheduler = "uge"\n'
                'submit_opts = "-g mygroup"\n'))
    cmd = trun.submit_cmd(trun.load_config(root), "/gs/x/band", "band.sh")
    assert cmd == "cd /gs/x/band && qsub -g mygroup band.sh"


def test_submit_cmd_slurm(tmp_path):
    root = make_project(
        tmp_path, "S",
        config=('host = "h"\nremote_root = "/r"\nscheduler = "slurm"\n'
                'submit_opts = "-A proj"\n'))
    cmd = trun.submit_cmd(trun.load_config(root), "/r/x", "j.sh")
    assert cmd == "cd /r/x && sbatch -A proj j.sh"


def test_submit_opts_is_optional(tmp_path):
    """submit_opts が無い config でも動くこと（必須キーではない）"""
    root = make_project(tmp_path)
    cfg = trun.load_config(root)
    assert "submit_opts" not in trun.REQUIRED_KEYS
    assert trun.submit_cmd(cfg, "/a", "b.sh").endswith("qsub b.sh")


# ------------------------------------------------------------- キュー照会

def test_queue_cmd(tmp_path):
    root = make_project(tmp_path)
    assert trun.queue_cmd(trun.load_config(root)) == "qstat"
    root2 = make_project(
        tmp_path, "S",
        config='host = "h"\nremote_root = "/r"\nscheduler = "slurm"\n')
    assert trun.queue_cmd(trun.load_config(root2)).startswith("squeue")


def test_probe_cmd_defers_sge_root_expansion():
    """$SGE_ROOT はログインシェル側で展開されること。

    quote が外れると非対話シェルが先に展開して空になり、
    Grid Engine を PBS と誤判定する（実機で踏んだ）。
    """
    cmd = trun.probe_cmd()
    assert cmd.startswith("$SHELL -lc ")
    assert "'command -v qsub sbatch; echo SGE_ROOT=$SGE_ROOT'" in cmd


def test_login_shell_wrapping(tmp_path, monkeypatch):
    """スケジューラの呼び出しはログインシェルで包まれること（tsubame では PATH が無い）"""
    seen = {}

    def fake_run(cmd, capture=False, check=True):
        seen["cmd"] = cmd

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(trun, "run", fake_run)
    root = make_project(tmp_path)
    trun.fetch_queue(trun.load_config(root))
    assert seen["cmd"][:2] == ["ssh", "tsubame"]
    assert seen["cmd"][2].startswith("$SHELL -lc ")
    assert "qstat" in seen["cmd"][2]


# ------------------------------------------------------------------- 表示

def test_scheduler_submit_bin():
    assert trun.SUBMIT_BIN["uge"] == "qsub"
    assert trun.SUBMIT_BIN["slurm"] == "sbatch"


# --------------------------------------------------------------------- CLI

def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        trun.main(["--version"])
    assert e.value.code == 0
    assert trun.VERSION in capsys.readouterr().out


def test_submit_requires_script():
    with pytest.raises(SystemExit):
        trun.main(["submit"])


def test_no_args_prints_help(capsys):
    assert trun.main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()
