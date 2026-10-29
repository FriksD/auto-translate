import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.app:app",
        host=os.environ.get("ATN_HOST", "127.0.0.1"),
        port=int(os.environ.get("ATN_PORT", "8000")),
    )
