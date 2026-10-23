#!/usr/bin/env python3
# pure_push.py: dulwich 1.2.15 + Python 标准库实现的全功能 git push(正确处理 gitignore/LFS/实时连接详情与速度,零外部进程)
import argparse,base64,codecs,errno,hashlib,http.client,io,json,logging,math,os,queue,re,shutil,socket,ssl,stat,sys,tempfile,threading,time
from collections import deque
from contextlib import contextmanager,suppress
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from urllib.parse import parse_qsl,quote,unquote,urljoin,urlsplit,urlunsplit
from urllib.request import getproxies,proxy_bypass
from dulwich.attrs import Pattern as AttrPattern
from dulwich.client import AbstractHttpGitClient
from dulwich.config import ConfigFile,apply_instead_of
from dulwich.errors import GitProtocolError,HangupException,NotGitRepository
from dulwich.file import GitFile
from dulwich.ignore import IgnoreFilter,IgnoreFilterManager,default_user_ignore_filter_path
from dulwich.index import IndexEntry,commit_tree,index_entry_from_stat,write_index_dict,validate_path,get_path_element_validator
from dulwich.object_store import MissingObjectFinder,iter_tree_contents
from dulwich.objects import Blob,Commit,Tag
from dulwich.pack import SHA1Writer,pack_objects_to_data
from dulwich.protocol import ZERO_SHA
from dulwich.refs import check_ref_format
from dulwich.repo import Repo
LOG=logging.getLogger("PurePush");SECRETS=set();CHUNK=64*1024;POINTER_PREFIX=b"version https://git-lfs.github.com/spec/v1\n";MEDIA="application/vnd.git-lfs+json"
class StopPush(RuntimeError):pass # 致命错误:立即停止,不重试
class NetworkFailure(RuntimeError):pass # 可重试的网络错误
class HTTPFailure(StopPush):
    def __init__(self,code,url,detail="",retry_after=0):
        super().__init__(f"HTTP {code} {safe_url(url)} {detail[:400]}");self.code=int(code);self.retry_after=retry_after
def remember(secret):
    if secret and len(str(secret))>3:SECRETS.add(str(secret))
def safe_url(value): # 日志 URL 只保留 scheme/host/port/path,去掉用户信息与查询串
    try:
        p=urlsplit(str(value));host=p.hostname or "";host=f"[{host}]" if ":" in host else host
        return urlunsplit((p.scheme,host+(f":{p.port}" if p.port else ""),p.path,"",""))
    except ValueError:return "[URL 已隐藏]"
def redact(value): # 全文脱敏:先替换已知密钥,再把任意内嵌 URL 变成安全形式
    text=str(value)
    for secret in sorted(SECRETS,key=len,reverse=True):text=text.replace(secret,"***")
    return re.sub(r"https?://[^\s\"'<>]+",lambda m:safe_url(m.group()),text).replace("\x1b","\\x1b")
class SafeFormatter(logging.Formatter):
    def format(self,record):return redact(super().format(record)) # 连异常堆栈统一脱敏
def setup_logging(v):
    h=logging.StreamHandler(sys.stdout);h.setFormatter(SafeFormatter("%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s","%Y-%m-%d %H:%M:%S"))
    LOG.handlers[:]=[h];LOG.propagate=False;LOG.setLevel({0:logging.ERROR,1:logging.WARNING,2:logging.INFO}.get(v,logging.DEBUG))
def trace(a,message,*values):
    if a.trace:LOG.debug(message,*values)
def cfg(config,section,key,default=b""):
    section=(section,) if isinstance(section,bytes) else section
    try:return config.get(section,key)
    except KeyError:return default
def text(v):return v.decode("utf-8","surrogateescape") if isinstance(v,bytes) else str(v)
def human(n):
    units=("B","KiB","MiB","GiB","TiB");i=0;n=float(n)
    while n>=1024 and i<len(units)-1:n/=1024;i+=1
    return f"{n:.2f} {units[i]}"
def parse_size(value):
    m=re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*",str(value),re.I)
    if not m:raise argparse.ArgumentTypeError("大小应为字节数或 100MB、1.5GiB 等格式")
    return int(float(m[1])*1024**("kmgt".find(m[2].lower())+1 if m[2] else 0))
def stamp():return datetime.now().strftime("%Y-%m-%d__%H.%M.%S__.%f")[:-3]
class Progress: # dulwich 进度回调:按换行/回车切分,避免把 \r 进度和正文混在一行
    def __init__(self,prefix=""):self.prefix=prefix;self.decoder=codecs.getincrementaldecoder("utf-8")("replace");self.pending=""
    def __call__(self,data):
        chunk=self.decoder.decode(data) if isinstance(data,bytes) else str(data);self.pending+=chunk
        parts=re.split(r"[\r\n]",self.pending);self.pending=parts.pop()
        for line in parts:
            if line:LOG.info("%s%s",self.prefix,line)
        if len(self.pending)>4096:LOG.info("%s%s",self.prefix,self.pending);self.pending=""
    def finish(self):
        if self.pending.strip():LOG.info("%s%s",self.prefix,self.pending);self.pending=""
def split_credentials(raw): # username/password 分别 URL 解码;主机只从 hostname/port 读取
    p=urlsplit(raw);user=unquote(p.username) if p.username is not None else None;password=unquote(p.password) if p.password is not None else None
    remember(password);remember(p.password)
    if p.scheme not in ("http","https") or not p.hostname:raise StopPush("本程序仅支持 HTTP(S);SSH 需要额外实现,不会偷偷启动 ssh")
    host=p.hostname;host=f"[{host}]" if ":" in host else host;port=p.port
    return urlunsplit((p.scheme,host+(f":{port}" if port else ""),p.path,p.query,"")),user,password
def origin(url):
    p=urlsplit(url);return p.scheme,p.hostname,p.port or (443 if p.scheme=="https" else 80)
def basic(user,password):
    if password is None:return None
    if ":" in (user or ""):raise StopPush("Basic 认证用户名不能含冒号")
    value="Basic "+base64.b64encode(f"{user or 'x-access-token'}:{password}".encode()).decode();remember(value);remember(value[6:]);return value
def normalize_remote(raw): # 规整 GitHub 网页/仓库地址为 canonical smart-http URL,网页路径仅用于取分支
    clean,user,password=split_credentials(raw);p=urlsplit(clean);branch=None;parts=p.path.strip("/").split("/")
    if p.query:raise StopPush("仓库 URL 不应包含查询串;请使用仓库首页地址")
    if p.hostname in ("github.com","www.github.com"):
        if len(parts)<2 or not all(parts[:2]):raise StopPush("GitHub URL 必须包含 owner/repository")
        if len(parts)>2:
            branch=unquote(parts[3] if parts[2] in ("tree","blob") and len(parts)>3 else parts[2]);LOG.warning("网页路径只用于定位仓库,推送范围仍是整个本地仓库;含斜杠的分支请显式 --branch")
        clean=urlunsplit((p.scheme,p.netloc,"/"+"/".join(parts[:2]).removesuffix(".git")+".git","",""))
    return clean.rstrip("/"),user,password,branch
def file_id(st):return stat.S_IFMT(st.st_mode),st.st_size,st.st_mtime_ns # 与 git 自身一致:只用类型/大小/mtime,不含 inode/dev/ctime
