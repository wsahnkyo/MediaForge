import argparse
import getpass
import logging

import uvicorn
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from mediaforge.auth import USERNAME, hash_password
from mediaforge.config import Settings
from mediaforge.database import LoginSession, User, initialize_database, make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="MediaForge 本机影视管理")
    parser.add_argument("command", choices=["init-db", "serve", "create-admin", "reset-password"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    if args.command != "serve":
        engine = make_engine(settings.database_url.get_secret_value())
        try:
            initialize_database(engine)
            if args.command in {"create-admin", "reset-password"}:
                if not USERNAME.fullmatch(args.username):
                    parser.error("用户名需为 3–64 位英文字母、数字、下划线、点或短横线")
                password = getpass.getpass("请输入密码（12–128 位）: ")
                if password != getpass.getpass("再次输入密码: "):
                    parser.error("两次密码不一致")
                password_hash = hash_password(password)
                with sessionmaker(engine)() as session:
                    user = session.scalar(
                        select(User).where(User.username == args.username).with_for_update()
                    )
                    if args.command == "create-admin":
                        if user is not None:
                            parser.error("用户已存在；重置密码请用 reset-password")
                        session.add(
                            User(username=args.username, password_hash=password_hash, role="admin")
                        )
                    else:
                        if user is None:
                            parser.error("用户不存在")
                        user.password_hash = password_hash
                        session.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
                    session.commit()
                print("账号已保存")
            print("PostgreSQL mediaforge schema 已初始化")
        finally:
            engine.dispose()
    else:
        uvicorn.run("mediaforge.app:create_app", factory=True, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
