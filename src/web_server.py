import http.server
import socketserver
from pathlib import Path

PORT = 8081
DIRECTORY = Path(__file__).parent.parent / "web"

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIRECTORY), **kwargs)

print("\n🚀 Web 观察大厅服务已准备就绪")
print(f"👉 浏览器访问: http://localhost:{PORT}")
print("   (可用于潜水观察后台 AI 的实时聊天动态)\n")

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Web 服务已关闭")
