#!/usr/bin/env python3
"""多站正文清洗 — HTML选择器 + Chrome兜底JS渲染 + PDF提取"""
import urllib.parse, io, subprocess, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup
import requests

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None

CHROME = "/tmp/chrome/chrome-linux64/chrome"
CHROME_FLAGS = ["--headless", "--no-sandbox", "--disable-gpu", "--dump-dom", "--timeout=20000"]

SELECTORS = [
    "div.v_news_content", "div.wp_articlecontent",
    "div.entry", "div.read", "div.article",
    "div.content", "div.con",
]

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        url = qs.get("url", [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if not url:
            self.wfile.write("missing url".encode())
            return
        try:
            text = self._fetch_and_extract(url)
            # 附上附件链接
            attachments = self._find_attachments(url)
            if attachments:
                text += "\n\n📎 附件:\n" + "\n".join(attachments)
            self.wfile.write(text.encode("utf-8"))
        except Exception as e:
            self.wfile.write(f"error: {e}".encode())

    def _fetch_and_extract(self, url):
        # 1. 普通请求
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, allow_redirects=True)
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        text = self._extract_html(soup)

        # 2. 太短 → Chrome 渲染
        if len(text) < 100:
            html = self._render_with_chrome(url)
            if html:
                soup2 = BeautifulSoup(html, "html.parser")
                text = self._extract_html(soup2)

        # 3. 仍然太短 → 找 PDF
        if len(text) < 100 and PdfReader:
            pdf_text = self._extract_pdfs(soup, url)
            if pdf_text:
                text = pdf_text

        return text if text else "(未找到正文)"

    def _extract_html(self, soup):
        for sel in SELECTORS:
            el = soup.select_one(sel)
            if el:
                txt = el.get_text(separator="\n", strip=True)
                if len(txt) > 50:
                    return txt
        # 备选：全页文本，跳过导航取正文
        full = soup.get_text()
        # 从第一个"发布日期"之后开始取
        for marker in ["发布日期：", "发布时间：", "发布日期", "发布时间"]:
            idx = full.find(marker)
            if idx > 0:
                rest = full[idx:].strip()
                lines = [l.strip() for l in rest.split("\n") if l.strip()]
                # 跳过 "发布日期：2026-xx-xx" 这类头部行
                body = []
                skip = 2  # 跳过发布日期行和浏览次数行
                for line in lines:
                    if skip > 0:
                        skip -= 1
                        continue
                    if len(line) < 3:
                        continue
                    body.append(line)
                text = "\n".join(body)
                if len(text) > 50:
                    return text[:5000]
        # 全部失败：返回全页文本（截断）
        lines = [l.strip() for l in full.split("\n") if len(l.strip()) > 3]
        return "\n".join(lines[:200])

    def _render_with_chrome(self, url):
        try:
            result = subprocess.run(
                [CHROME] + CHROME_FLAGS + [url],
                capture_output=True, timeout=25
            )
            if result.returncode == 0 or len(result.stdout) > 500:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
        return ""

    def _extract_pdfs(self, soup, page_url):
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.lower().endswith(".pdf"):
                continue
            pdf_url = urllib.parse.urljoin(page_url, href)
            try:
                resp = requests.get(pdf_url, timeout=15)
                if resp.status_code != 200 or len(resp.content) < 100:
                    continue
                reader = PdfReader(io.BytesIO(resp.content))
                pages = []
                for page in reader.pages:
                    txt = page.extract_text()
                    if txt:
                        pages.append(txt.strip())
                text = "\n".join(pages)
                if len(text) > 50:
                    return text
            except Exception:
                continue
        return ""

    def _find_attachments(self, url):
        """找页面中所有文档附件链接（PDF/Word/Excel等）"""
        exts = ('.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.zip','.rar')
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, allow_redirects=True)
            r.encoding = r.apparent_encoding
            soup = BeautifulSoup(r.text, "html.parser")
            found = []
            for a in soup.find_all("a", href=True):
                h = a["href"]
                if any(h.lower().endswith(ext) for ext in exts):
                    full = urllib.parse.urljoin(url, h)
                    name = a.get_text(strip=True) or h.split("/")[-1]
                    found.append(full)
            return list(dict.fromkeys(found))  # 去重保序
        except Exception:
            return []

    def log_message(self, *a):
        pass

HTTPServer(("", 8001), H).serve_forever()
