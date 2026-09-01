"""启动入口；本机默认仅回环，容器可通过环境变量绑定。"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app", host=os.getenv("TEST_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("TEST_PORT", "8000")), reload=False,
    )
