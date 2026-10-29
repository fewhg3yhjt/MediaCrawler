# -*- coding: utf-8 -*-
"""微博数据导出/导入合并 CLI。

用法:
  python -m weibo_transfer export [--users ...] [--collections ...] [--out DIR]
  python -m weibo_transfer import FILE [--policy integrity|freshness|source_priority|keep_both] [--dry-run]
"""

import sys
import io

# Force UTF-8 output (Windows GBK console 中文不乱码)
if sys.stdout and hasattr(sys.stdout, "buffer"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import typer

from store.weibo import transfer

app = typer.Typer(help="微博数据导出/导入合并（多机协作）", add_completion=False)


def _split_csv(value: str):
    return [x.strip() for x in value.split(",") if x.strip()] or None


@app.command("export")
def export_cmd(
    users: str = typer.Option("", help="按用户过滤（user_id，逗号分隔；留空=全部）"),
    collections: str = typer.Option("", help="按集合过滤（creator/favorite/liked/user_xxx，逗号分隔；留空=全部）"),
    out: str = typer.Option("", help="导出目录（默认 data/weibo/exports）"),
):
    """导出本地微博数据为 zip 包。"""
    path = transfer.export(
        users=_split_csv(users),
        collections=_split_csv(collections),
        dest=out or None,
    )
    typer.secho(f"导出完成: {path}", fg=typer.colors.GREEN)


@app.command("import")
def import_cmd(
    file: str = typer.Argument(..., help="weibo 导出包 zip 路径"),
    policy: str = typer.Option("integrity", help="重复保留策略: integrity|freshness|source_priority|keep_both"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只出统计报告，不落盘"),
):
    """导入导出包并合并去重。"""
    report = transfer.import_(file, policy=policy, dry_run=dry_run)
    print("\n=== 导入报告 ===")
    print(f"  来源机器:   {report.get('source_host')}   格式版本: {report.get('format_version')}")
    print(f"  策略:       {report['policy']}   dry-run: {report.get('dry_run')}")
    print(f"  记录总数:   {report['total']}")
    print(f"  新增:       {report['new']}")
    print(f"  重复:       {report['duplicate']}")
    print(f"    取导入版: {report['kept_incoming']}    保留本地: {report['kept_local']}    两版都留: {report['kept_both']}")
    print(f"  跳过(无id): {report['skipped_no_id']}")
    print(f"  媒体:       导入 {report['media_imported']}   跳过(已存在) {report['media_skipped']}")
    print(f"  新增用户:   {report['creators_imported']}")
    typer.secho("\n完成（dry-run，未写盘）" if report.get("dry_run") else "\n完成，已合并写入", fg=typer.colors.GREEN)


@app.command("flag")
def flag_cmd(
    note_ids: str = typer.Argument(..., help="微博 note_id 列表（逗号分隔），也可用 --file 从文件按行读"),
    status: str = typer.Option("deleted_on_source", help="标记状态: deleted_on_source | ok"),
    file: str = typer.Option("", help="从文件读 note_id（每行一个），与位置参数二选一"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计不落盘"),
):
    """手动标记 availability（半自动源端已删管理）。

    用途：源端已删/被封导致爬取不到，但本地已有存档的微博，标记后可单独筛选。
    """
    from store.weibo import jsonl_ops
    if file:
        with open(file, encoding="utf-8") as f:
            ids = [l.strip() for l in f if l.strip()]
    else:
        ids = [x.strip() for x in note_ids.split(",") if x.strip()]
    if status not in ("ok", "deleted_on_source"):
        raise typer.BadParameter("status 只能是 ok 或 deleted_on_source")
    if dry_run:
        records_by_id, _ = jsonl_ops.load_all_content_records()
        hit = [i for i in ids if i in records_by_id]
        print(f"（dry-run）待标记 {len(ids)} 条，命中本地存档 {len(hit)} 条")
        return
    from store import weibo
    marked = weibo.mark_availability(ids, status)
    typer.secho(f"已标记 {marked} 条为 {status}（未命中 {len(ids) - marked} 条，本地无此记录）", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
