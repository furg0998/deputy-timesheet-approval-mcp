import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mangum import Mangum

from mcp_server import build_app

app = build_app()
handler = Mangum(app, lifespan="auto")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
