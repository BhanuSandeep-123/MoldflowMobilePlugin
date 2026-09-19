import http.server
import socketserver
import json
import os
from pathlib import Path

PORT = 8000
HERE = Path(__file__).resolve().parent

class MyHandler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/api/respond":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            # Read the CURRENT session's ui_state.json. The state file moved
            # into a per-Synergy-window directory (see session_context.py), so
            # this dev harness has to resolve it the same way the plugin does
            # rather than assume the old fixed path.
            import sys
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            import session_context
            state_path = session_context.session_path("ui_state.json")
            if state_path.exists():
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        state = json.load(f)
                except Exception:
                    state = {}
            else:
                state = {}
                
            # Update pending_prompt.answer
            if "pending_prompt" in state and state["pending_prompt"]:
                state["pending_prompt"]["answer"] = data.get("answer")
                try:
                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2)
                except Exception:
                    pass
                    
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        
        super().do_POST()

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

# Run server
os.chdir(HERE)
# Allow port reuse
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("", PORT), MyHandler) as httpd:
    print(f"Serving Moldflow Automation Center at http://localhost:{PORT}")
    httpd.serve_forever()
