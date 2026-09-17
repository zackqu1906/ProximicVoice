#!/usr/bin/env python3
"""Local-only preview server with byte ranges for reliable WAV seeking."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import argparse, json, re, threading, webbrowser

ROOT=Path(__file__).resolve().parent
class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        self.byte_range=None
        super().__init__(*args,directory=str(ROOT),**kwargs)
    def send_head(self):
        if self.path=='/__ring_asr_comparison__':
            data=json.dumps({'root':str(ROOT)}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)));self.end_headers()
            if self.command!='HEAD':self.wfile.write(data)
            return None
        path=Path(self.translate_path(self.path))
        # Keep the preview scoped to its own artifact directory.
        if not path.resolve().is_relative_to(ROOT):
            self.send_error(403);return None
        header=self.headers.get('Range')
        if not header or not path.is_file():return super().send_head()
        total=path.stat().st_size
        m=re.fullmatch(r'bytes=(\d*)-(\d*)',header.strip())
        if not m or not total:
            self.send_error(416);return None
        if m[1]:start=int(m[1]);end=int(m[2]) if m[2] else total-1
        else:start=max(0,total-int(m[2] or 0));end=total-1
        end=min(end,total-1)
        if start>end or start>=total:
            self.send_response(416);self.send_header('Content-Range',f'bytes */{total}');self.end_headers();return None
        f=path.open('rb');f.seek(start);self.byte_range=(start,end)
        self.send_response(206);self.send_header('Content-Type',self.guess_type(str(path)))
        self.send_header('Content-Range',f'bytes {start}-{end}/{total}')
        self.send_header('Content-Length',str(end-start+1));self.end_headers()
        return f
    def end_headers(self):
        self.send_header('Accept-Ranges','bytes')
        super().end_headers()
    def copyfile(self,source,outputfile):
        if self.byte_range is None:return super().copyfile(source,outputfile)
        remaining=self.byte_range[1]-self.byte_range[0]+1
        while remaining>0:
            data=source.read(min(65536,remaining))
            if not data:break
            try:outputfile.write(data)
            except (BrokenPipeError,ConnectionResetError):break
            remaining-=len(data)
    def log_message(self,format,*args):pass

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8878);p.add_argument('--open',action='store_true');args=p.parse_args()
    # Reuse this exact artifact server if the command is opened again.
    if args.open:
        import urllib.request
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/__ring_asr_comparison__',timeout=1) as response:
                same=json.load(response).get('root')==str(ROOT)
            if same:
                webbrowser.open(f'http://127.0.0.1:{args.port}/');raise SystemExit(0)
        except (OSError,ValueError):pass
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    url=f'http://127.0.0.1:{server.server_port}/'
    print(f'语音识别对比：{url}\n保持此窗口打开；按 Control-C 停止。',flush=True)
    if args.open:threading.Timer(.2,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
