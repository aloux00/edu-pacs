# -*- coding: utf-8 -*-
"""EduPACS 备份/恢复命令行工具（网页管理界面之外的第二入口）

子命令：
  backup   创建加密备份       --dest local|offsite|<路径> [--note ...] [--keep N] [--hot]
                             [--password X | --password-file F | 环境变量 EDUPACS_BACKUP_PASS | 交互输入]
  list     列出目的地归档     --dest local|<路径>
  verify   完整校验归档       <归档文件> [--password ...]
  restore  恢复归档           <归档文件> [--apply] [--root 沙箱目录] [--password ...]
                             （默认干跑分析；--apply 才写入，要求服务已停止）
  prune    立即执行保留策略   --dest local|<路径> [--keep N]
  config   查看当前配置

示例：
  .venv\\Scripts\\python tools\\backup.py backup --dest local --password "MyStr0ngPass"
  .venv\\Scripts\\python tools\\backup.py list --dest local
  .venv\\Scripts\\python tools\\backup.py restore backups\\edupacs-backup-20261120-023000-SRV.edubak --password "MyStr0ngPass"
  .venv\\Scripts\\python tools\\backup.py restore backups\\xxx.edubak --password "..." --apply

定时备份（Windows 计划任务，密码放受保护文件避免命令行泄漏）：
  1. echo MyStr0ngPass > D:\\edu-pacs\\.secure\\bkpass.txt   （并用 NTFS ACL 限制仅管理员可读）
  2. schtasks /Create /TN EduPACS-Backup /SC DAILY /ST 02:30 /RU SYSTEM ^
       /TR "D:\\edu-pacs\\.venv\\Scripts\\python.exe D:\\edu-pacs\\tools\\backup.py backup --dest local --password-file D:\\edu-pacs\\.secure\\bkpass.txt"

Ubuntu（cron）：
  30 2 * * * cd /opt/edu-pacs && EDUPACS_BACKUP_PASS='...' .venv/bin/python tools/backup.py backup --dest local
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.backend import backup_core as bc  # noqa: E402


def _fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def _progress(stage, pct, msg):
    if pct is None:
        print(f"[{stage}] {msg}")
    else:
        print(f"[{stage} {pct:5.1f}%] {msg}")


def _get_password(args) -> str:
    pw = args.password
    if pw:
        return pw
    if args.password_file:
        p = Path(args.password_file)
        if not p.is_file():
            print(f"[错误] 密码文件不存在：{p}")
            sys.exit(1)
        return p.read_text(encoding="utf-8").strip()
    env = os.environ.get("EDUPACS_BACKUP_PASS", "")
    if env:
        return env
    pw = getpass.getpass("请输入备份密码（输入不回显）：")
    return pw


def _resolve_dest(args) -> dict:
    cfg = bc.load_config()
    if args.dest in (None, ""):
        args.dest = "local"
    d = bc.get_dest(cfg, args.dest)
    if d:
        return d
    # 不是目的地 id → 当作路径
    return {"id": "adhoc", "label": "命令行指定路径", "path": args.dest, "keep": 999, "enabled": True}


def cmd_backup(args):
    dest = _resolve_dest(args)
    password = _get_password(args)
    if not password:
        print("[错误] 未提供密码（--password / --password-file / 环境变量 EDUPACS_BACKUP_PASS）")
        sys.exit(1)
    try:
        r = bc.create_backup(dest, password, note=args.note or "",
                              mode="hot" if args.hot else "auto", progress=_progress)
    except KeyboardInterrupt:
        print("\n[中断] 已取消（若 Orthanc 被短暂停止会自动拉回）")
        sys.exit(130)
    except bc.BackupError as e:
        print(f"[失败] {e}")
        sys.exit(1)
    print()
    print("====== 备份完成 ======")
    print(f"  归档     ：{r['path']}")
    print(f"  大小     ：{_fmt_size(r['size'])}   耗时 {r['duration_s']}s   模式 {r['mode']}")
    print(f"  内容     ：{r['counts']['studies']} study / {r['counts']['instances']} 实例 / "
          f"{r['counts']['storage_files']} 存储文件")
    if r["pruned"]:
        print(f"  保留策略 ：已清理 {len(r['pruned'])} 个旧归档")
    print("  下一步   ：建议立刻执行 verify 确认可读性，并把归档异机保存一份")


def cmd_list(args):
    dest = _resolve_dest(args)
    path = bc.resolve_dest_path(dest)
    arcs = bc.list_archives(path)
    if not arcs:
        print(f"（{path} 下暂无归档）")
        return
    print(f"{'文件名':<62} {'大小':>10}  {'创建时间':<19}  备注")
    for a in arcs:
        note = (a.get("note") or "").strip()[:30]
        print(f"{a['name']:<62} {_fmt_size(a['size']):>10}  {a['mtime']:<19}  {note}")
    print(f"共 {len(arcs)} 个归档，占用 {_fmt_size(sum(a['size'] for a in arcs))}")


def cmd_verify(args):
    password = _get_password(args)
    r = bc.verify_archive(Path(args.archive), password, progress=_progress)
    if not r.get("ok"):
        print(f"[失败] {r.get('error')}")
        sys.exit(1)
    m = r.get("manifest", {})
    sec = m.get("sections", {})
    print("====== 校验通过 ======")
    print(f"  归档     ：{args.archive}")
    print(f"  创建     ：{m.get('created')}  主机 {m.get('host')}  模式 {m.get('mode')}")
    print(f"  备注     ：{m.get('note')}")
    idx = sec.get("orthanc_index", {})
    st = sec.get("storage", {})
    print(f"  内容     ：{idx.get('studies', '?')} study / {idx.get('instances', '?')} 实例 / "
          f"{st.get('files', '?')} 存储文件（{_fmt_size(st.get('bytes', 0))}）")
    print(f"  分区校验 ：{r.get('checks')}")
    print(f"  密码正确 ：是（AES-256-GCM tag 与分区哈希全部一致）")


def cmd_restore(args):
    password = _get_password(args)
    root = Path(args.root) if args.root else None
    r = bc.restore_archive(Path(args.archive), password, apply=args.apply, root=root,
                           progress=_progress)
    if not r.get("ok"):
        print(f"[失败] {r.get('error')}")
        if r.get("stage") == "precheck":
            print("提示：先停止服务（scripts\\stop_all.ps1 或 EduPACS启动器.exe --stop）再恢复")
        sys.exit(1)
    if not r.get("applied"):
        print("====== 干跑分析（未写入任何文件） ======")
        print(f"  归档     ：{args.archive}")
        print(f"  创建     ：{r['manifest'].get('created')}  主机 {r['manifest'].get('host')}")
        idx = r["manifest"]["sections"].get("orthanc_index", {})
        st = r["manifest"]["sections"].get("storage", {})
        print(f"  将恢复   ：app.db {r['planned'].get('app/data/app.db') and '√' or '×'}、"
              f"secret.key {r['planned'].get('app/data/secret.key') and '√' or '×'}、"
              f"orthanc.json {r['planned'].get('orthanc/orthanc.json') and '√' or '×'}、"
              f"index {r['planned'].get('orthanc/storage/index') and '√' or '×'}")
        print(f"  影像     ：{st.get('files')} 个存储文件 / {idx.get('instances')} 实例")
        print("  正式恢复命令（服务停止后执行）：")
        for k, cmd in r.get("commands", {}).items():
            print(f"    [{k}] {cmd}")
        print("  重新执行本命令加 --apply 即写入。")
        return
    print("====== 恢复完成 ======")
    print(f"  回滚保障 ：{r.get('safeguard')}（原数据，确认无误后可手动删除）")
    print(f"  自检     ：app.db 用户数 {r['postcheck']['users']}")
    print(f"  下一步   ：{r.get('next')}")


def cmd_prune(args):
    dest = _resolve_dest(args)
    keep = args.keep if args.keep is not None else int(dest.get("keep", 7))
    path = bc.resolve_dest_path(dest)
    removed = bc.prune_dest(path, keep)
    print(f"已清理 {len(removed)} 个归档（保留最近 {keep} 个）：")
    for n in removed:
        print(f"  - {n}")


def cmd_config(_args):
    cfg = bc.load_config()
    print(json_dumps(cfg))
    print(f"（配置文件：{bc.CONFIG_PATH}；网页端「备份恢复」页面可直接编辑目的地）")


def json_dumps(cfg):
    import json
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="EduPACS 备份/恢复工具（AES-256-GCM 加密）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="创建加密备份")
    b.add_argument("--dest", default="local", help="目的地 id（见 config/backup.json）或直接写路径")
    b.add_argument("--note", default="", help="备注（明文存于包头便于检索，勿写敏感信息）")
    b.add_argument("--keep", type=int, default=None, help="覆盖该目的地保留份数")
    b.add_argument("--hot", action="store_true", help="强制热备份（不停 Orthanc，带校验重试）")
    b.add_argument("--password", default=None)
    b.add_argument("--password-file", default=None)
    b.set_defaults(func=cmd_backup)

    l = sub.add_parser("list", help="列出归档")
    l.add_argument("--dest", default="local")
    l.set_defaults(func=cmd_list)

    v = sub.add_parser("verify", help="校验归档（密码 + GCM + 分区哈希）")
    v.add_argument("archive")
    v.add_argument("--password", default=None)
    v.add_argument("--password-file", default=None)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("restore", help="恢复归档（默认干跑；--apply 才写入）")
    r.add_argument("archive")
    r.add_argument("--apply", action="store_true", help="实际写入（要求服务已停止）")
    r.add_argument("--root", default=None, help="恢复到沙箱目录（测试用）")
    r.add_argument("--password", default=None)
    r.add_argument("--password-file", default=None)
    r.set_defaults(func=cmd_restore)

    p = sub.add_parser("prune", help="按保留策略清理旧归档")
    p.add_argument("--dest", default="local")
    p.add_argument("--keep", type=int, default=None)
    p.set_defaults(func=cmd_prune)

    c = sub.add_parser("config", help="查看配置")
    c.set_defaults(func=cmd_config)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
