#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, json, base64, hashlib,requests,sys
requests.packages.urllib3.disable_warnings()

_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "!config.json")
if not os.path.isfile(_cfg_path):
    raise SystemExit(f"[FATAL] 找不到配置文件: {_cfg_path}")
with open(_cfg_path, "r", encoding="utf-8") as _f:
    _cfg = json.load(_f)
DEFAULT_TOKEN = _cfg.get("DEFAULT_TOKEN")
DEFAULT_DOMAIN = _cfg.get("DEFAULT_DOMAIN")
if not DEFAULT_TOKEN or not DEFAULT_DOMAIN:
    raise SystemExit("[FATAL] !config.json 必须包含非空的 DEFAULT_TOKEN 和 DEFAULT_DOMAIN")
    
# DEFAULT_TOKEN = "pt-#32_#8-#4-#4-#4-#16"
# DEFAULT_DOMAIN = "###-cn-hangzhou.devops.aliyuncs.com"
DEFAULT_ORG_ID = DEFAULT_DOMAIN.split('-')[0]# None
DEFAULT_REPO = "qpsu-repo"
DEFAULT_BRANCH = "master"
DEFAULT_VISIBILITY = "private"
DEFAULT_TIMEOUT = 600
DEFAULT_COMMIT_MSG = ""
MAX_OPENAPI_SIZE = 1024*1024* 50  # MB 硬上限  50.03MB msg=2026-09-22 02:27:56.164=52459227B=都不行
'''
CodeupError: 上传失败 [413]: {"code":413,"errorCode":"SYSTEM_ILLEGAL_ARGUMENT_ERROR","errorDescription":"Create file content too large","errorMessage":"413 PAYLOAD_TOO_LARGE - Create file content too large","pushRuleExist":false,"status":false,"traceId":"792b865417900152783993151e105e"}
'''

class CodeupError(Exception): pass
_repo_cache = {}

class _ProgressStream:
    def __init__(self, payload_bytes, prefix="↑[上行]"):
        self.payload = payload_bytes; self.length = len(payload_bytes); self.offset = 0
        self.start_time = time.time(); self.finish_time = self.start_time; self.prefix = prefix
    def read(self, size=-1):
        if self.offset >= self.length: return b""
        if size < 0: size = self.length - self.offset
        chunk = self.payload[self.offset:self.offset+size]; self.offset += len(chunk)
        elapsed = time.time() - self.start_time
        speed = (self.offset/1024/1024)/elapsed if elapsed > 0 else 0
        print(f"\r{self.prefix}:{self.offset/1024/1024:.2f}/{self.length/1024/1024:.2f}MB {speed:.2f}MB/s", end="", flush=True)
        if self.offset >= self.length: self.finish_time = time.time()
        return chunk
    def __len__(self): return self.length

def _auto_msg_from_bytes(payload):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + f".{int(time.time()*1000)%1000:03d}"
    h = hashlib.sha256(payload).hexdigest()[:16]
    return f"{ts}={len(payload)}B={h}"

def _base(token=None, domain=None, org_id=None):
    token = token or DEFAULT_TOKEN; domain = domain or DEFAULT_DOMAIN; org_id = org_id or DEFAULT_ORG_ID
    if domain == "openapi-rdc.aliyuncs.com":
        if not org_id: raise CodeupError("中心版需要 organization_id")
        base = f"https://{domain}/oapi/v1/codeup/organizations/{org_id}"
    else:
        base = f"https://{domain}/oapi/v1/codeup"
    headers = {"Content-Type": "application/json", "x-yunxiao-token": token}
    return base, headers, domain

def _ensure_repo(repo_name, token=None, domain=None, org_id=None, visibility=DEFAULT_VISIBILITY, timeout=DEFAULT_TIMEOUT):
    key = (repo_name, domain or DEFAULT_DOMAIN)
    if key in _repo_cache: return _repo_cache[key]
    base, headers, _ = _base(token, domain, org_id)
    body = {"name": repo_name, "path": repo_name, "visibility": visibility, "readMeType": "EMPTY"}
    r = requests.post(f"{base}/repositories?createParentPath=true", headers=headers, json=body, verify=False, timeout=timeout)
    if r.status_code in (200, 201):
        rid = r.json().get("id")
        if rid: _repo_cache[key] = rid; return rid
    if r.status_code == 409:
        r2 = requests.get(f"{base}/repositories", headers=headers, params={"search": repo_name, "page": 1, "perPage": 10}, verify=False, timeout=timeout)
        if r2.status_code == 200:
            data = r2.json(); repos = data if isinstance(data, list) else data.get("result", [])
            for repo in repos:
                if repo.get("name") == repo_name or repo.get("path") == repo_name:
                    rid = repo.get("id"); _repo_cache[key] = rid; return rid
        raise CodeupError(f"仓库 '{repo_name}' 已存在但无法找到")
    raise CodeupError(f"创建仓库失败 [{r.status_code}]: {r.text[:300]}")

def _make_url(domain, rid, file_path, branch):
    return f"https://{domain}/oapi/v1/codeup/repositories/{rid}/files/{requests.utils.quote(file_path, safe='')}?ref={branch}"

def upload(data, repo_name=DEFAULT_REPO, file_path=None, token=None, domain=None, org_id=DEFAULT_ORG_ID, branch=None, visibility=None, commit_message=None, overwrite=True, timeout=None):
    repo_name = repo_name or DEFAULT_REPO; branch = branch or DEFAULT_BRANCH
    visibility = visibility or DEFAULT_VISIBILITY; timeout = timeout or DEFAULT_TIMEOUT
    if isinstance(data, str):
        if not os.path.isfile(data): raise CodeupError(f"文件不存在: {data}")
        file_path = file_path or os.path.basename(data)
        size = os.path.getsize(data)
        if size > MAX_OPENAPI_SIZE: raise CodeupError(f"文件 {size/1024/1024:.1f}MB 超过 OpenAPI MB 上限，请使用 Git LFS")
        with open(data, "rb") as f: payload = f.read()
    elif isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
        if len(payload) > MAX_OPENAPI_SIZE: raise CodeupError(f"bytes {len(payload)/1024/1024:.1f}MB 超过 200MB 上限")
        if not file_path: raise CodeupError("bytes 输入必须提供 file_path")
    else:
        raise CodeupError("data 必须是 bytes 或文件路径字符串")
    if not commit_message: commit_message = _auto_msg_from_bytes(payload)
    base, headers, dom = _base(token, domain, org_id)
    # rid = _ensure_repo(repo_name, token, domain, org_id, visibility, timeout)
    rid=f'{org_id}%2F{repo_name}' # 减少不必要的请求
    print(f"[*] OpenAPI 上传 [{file_path}] {len(payload)/1024/1024:.2f}MB msg={commit_message}")
    content_b64 = base64.b64encode(payload).decode()
    body = {"branch": branch, "commitMessage": commit_message, "content": content_b64, "encoding": "base64", "filePath": file_path}
    payload_json = json.dumps(body).encode()
    stream = _ProgressStream(payload_json, prefix="↑[上行]")
    h = headers.copy(); h["Content-Length"] = str(len(payload_json))
    try:
        r = requests.request("POST", f"{base}/repositories/{rid}/files", data=stream, headers=h, verify=False, timeout=timeout)
    except requests.RequestException as e:
        print(); raise CodeupError(f"网络上传失败: {e}")
    delay = (time.time() - stream.finish_time) * 1000
    print(f"\n[-] 服务端处理延时: {delay:.2f}ms")
    if r.status_code in (200, 201): return _make_url(dom, rid, file_path, branch)
    if r.status_code in (400, 409) and overwrite:
        print("[*] 已存在，覆盖上传...")
        url_put = f"{base}/repositories/{rid}/files/{requests.utils.quote(file_path, safe='')}"
        stream2 = _ProgressStream(payload_json, prefix="↑[上行]")
        try:
            r2 = requests.request("PUT", url_put, data=stream2, headers=h, verify=False, timeout=timeout)
        except requests.RequestException as e:
            print(); raise CodeupError(f"覆盖网络失败: {e}")
        print(f"\n[-] 覆盖服务端延时: {(time.time()-stream2.finish_time)*1000:.2f}ms")
        if r2.status_code in (200, 201): return _make_url(dom, rid, file_path, branch)
        raise CodeupError(f"覆盖失败 [{r2.status_code}]: {r2.text[:300]}")
    raise CodeupError(f"上传失败 [{r.status_code}]: {r.text[:300]}")

def download(file_path=None, repo_name=None, token=None, domain=None, org_id=None, branch=None, timeout=None, save_to=None):
    if not file_path: raise CodeupError("必须提供 file_path")
    repo_name = repo_name or DEFAULT_REPO; branch = branch or DEFAULT_BRANCH; timeout = timeout or DEFAULT_TIMEOUT
    base, headers, dom = _base(token, domain, org_id)
    rid = _ensure_repo(repo_name, token, domain, org_id, timeout=timeout)
    url = f"{base}/repositories/{rid}/files/{requests.utils.quote(file_path, safe='')}?ref={branch}"
    print(f"[*] OpenAPI 下载 [{file_path}] ...")
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, verify=False, stream=True, timeout=timeout)
    except requests.RequestException as e:
        raise CodeupError(f"请求失败: {e}")
    ttfb = (time.time() - t0) * 1000
    print(f"[-] TTFB: {ttfb:.2f}ms")
    if r.status_code != 200: raise CodeupError(f"下载失败 [{r.status_code}]: {r.text[:300]}")
    total = int(r.headers.get("Content-Length", 0))
    chunks = []; downloaded = 0; t0 = time.time()
    for chunk in r.iter_content(131072):
        if chunk:
            chunks.append(chunk); downloaded += len(chunk)
            el = time.time() - t0; sp = (downloaded/1024/1024)/el if el > 0 else 0
            if total > 0: print(f"\r↓[下行]:{downloaded/1024/1024:.2f}/{total/1024/1024:.2f}MB {sp:.2f}MB/s", end="", flush=True)
            else: print(f"\r↓[下行]:{downloaded/1024/1024:.2f}MB {sp:.2f}MB/s", end="", flush=True)
    print()
    raw = b"".join(chunks)
    obj = json.loads(raw.decode())
    content = obj.get("content", ""); enc = obj.get("encoding", "base64")
    result = base64.b64decode(content) if enc == "base64" else content.encode("utf-8")
    if save_to:
        os.makedirs(os.path.dirname(os.path.abspath(save_to)), exist_ok=True)
        with open(save_to, "wb") as f: f.write(result)
        print(f"[+] 已保存到 {save_to} ({len(result)} bytes)")
        return save_to
    return result

if __name__ == "__main__":
    url = upload(r"D:\test\qpsu.zip")
    print(f"[+] 链接: {url}")
    data = download("qpsu.zip")
    print(f"[+] {len(data)}B magic={data[:4]}")