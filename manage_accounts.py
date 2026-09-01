"""管理平台账号。密码默认通过无回显终端输入，不进入命令历史。"""
import argparse
import getpass
import sqlite3
import sys

from app import auth, store


def main() -> int:
    parser = argparse.ArgumentParser(description="API 中转站测试平台账号管理")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add = subparsers.add_parser("add", help="创建独立账号")
    add.add_argument("username")
    add.add_argument("--display-name", default="")
    subparsers.add_parser("list", help="列出账号及启停状态")
    disable = subparsers.add_parser("disable", help="停用账号并立即撤销全部会话")
    disable.add_argument("username")
    enable = subparsers.add_parser("enable", help="重新启用账号")
    enable.add_argument("username")
    args = parser.parse_args()

    store.init()
    if args.command == "list":
        rows = store.query(
            "SELECT id,username,display_name,active,updated_at "
            "FROM users ORDER BY username"
        )
        for row in rows:
            print(f"{row['id']}\t{row['username']}\t{row['display_name']}\t"
                  f"{'启用' if row['active'] else '停用'}")
        return 0
    if args.command in {"disable", "enable"}:
        active = args.command == "enable"
        try:
            user = auth.set_user_active(args.username, active)
        except ValueError as exc:
            print(f"操作失败：{exc}", file=sys.stderr)
            return 1
        auth.audit(None, "server-cli", f"user.{args.command}", "user",
                   str(user["id"]), "success",
                   detail={"username": user["username"], "active": active})
        print(f"账号已{'启用' if active else '停用'}：{user['username']}")
        return 0
    if args.command == "add":
        password = getpass.getpass("新密码（至少 12 位）：")
        confirm = getpass.getpass("再次输入新密码：")
        if password != confirm:
            print("两次输入的密码不一致", file=sys.stderr)
            return 2
        try:
            user_id = auth.create_user(args.username, password, args.display_name)
        except (ValueError, sqlite3.IntegrityError) as exc:
            print(f"创建失败：{exc}", file=sys.stderr)
            return 1
        auth.audit(user_id, args.username, "user.create", "user", str(user_id), "success")
        print(f"账号已创建：{args.username}（ID {user_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
