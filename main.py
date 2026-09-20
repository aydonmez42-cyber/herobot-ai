import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('PORT', '8080'))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("TEST32 PAPER TRADING is online.\n"
                "REAL BINANCE MARKET DATA / NO REAL ORDERS\n"
                "Run: python paper_trading.py\n").encode()
        self.send_response(200)
        self.send_header('Content-Type','text/plain; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): return

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()
