import http.server
import socketserver
import threading
import webbrowser
from abc import ABC, abstractmethod


class CharterLister(ABC):
    @abstractmethod
    def list_charters(self) -> str:
        """Return an HTML string listing the charters."""
        pass

def open_html_once(html: str):
    class OneShotHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # Send the HTML
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

            # Shut down the server right after this response
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format, *args):
            # Disable logging to stderr
            pass

    # Bind to localhost on any free port
    with socketserver.TCPServer(("127.0.0.1", 0), OneShotHandler) as httpd:
        port = httpd.server_address[1]

        # Open the page in the default browser
        webbrowser.open(f"http://127.0.0.1:{port}/")

        # Block here until the first request is served and shutdown() is called
        httpd.serve_forever()


# Example usage:
if __name__ == "__main__":
    big_html = """
    <!doctype html>
    <html>
      <head><meta charset="utf-8"><title>Big Page</title></head>
      <p>""" + "Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n" * 100000 + """</p>
      <body><h1>Huge HTML string</h1><p>This came from memory only.</p></body>
    </html>
    """
    open_html_once(big_html)
