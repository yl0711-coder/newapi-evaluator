import argparse
import os

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="启动模型测试工作台或单独的测试工具")
    parser.add_argument("--app", choices=["all", "channels", "admission", "stability", "reasoning"], default="all")
    parser.add_argument("--host", default=os.getenv("PLATFORM_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--reload", action="store_true", help="代码变更时自动重启（仅开发环境）")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        if not os.getenv("PLATFORM_USERNAME") or len(os.getenv("PLATFORM_PASSWORD", "")) < 12:
            parser.error("非本机监听必须设置 PLATFORM_USERNAME 和至少 12 位 PLATFORM_PASSWORD")
    ports = {"all": 8090, "channels": 8094, "admission": 8091, "stability": 8092, "reasoning": 8093}
    os.environ["PLATFORM_APP"] = args.app
    # A single worker owns the scheduler; run either all or stability for a given data directory.
    uvicorn.run("workbench:create_app", factory=True, host=args.host,
                port=args.port or ports[args.app], access_log=False, log_level="warning",
                reload=args.reload, reload_dirs=[os.getcwd()] if args.reload else None)


if __name__ == "__main__":
    main()
