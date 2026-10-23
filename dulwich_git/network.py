from .shared import *
from .lfs import *
from .stage import *
def network_error(exc,established=False): # 区分本地配置错误(不重试)与可重试网络错误;established=True 表示 TLS 已握手成功、处于请求收发阶段
    if isinstance(exc,ssl.SSLCertVerificationError):return StopPush("TLS 证书校验失败;可用 --ca-file 指定,不会自动关闭校验")
    if isinstance(exc,ssl.SSLSyscallError):return NetworkFailure(f"SSL 底层连接中断({type(exc).__name__}): {exc}") # [SYS] SSL_ERROR_SYSCALL:底层 socket 读写系统调用失败(对端 RST/链路中断/超时),与证书/握手协议无关,必须重试
    if isinstance(exc,ssl.SSLError):
        if established:return NetworkFailure(f"已建立 TLS 连接传输中断({type(exc).__name__}): {exc}") # 握手成功后只可能在线路上出错(坏记录_MAC/注入等),按瞬断重试
        if not isinstance(exc,(ssl.SSLEOFError,ssl.SSLZeroReturnError)):return StopPush("TLS 握手/协议错误: "+str(exc))
    if isinstance(exc,OSError) and not isinstance(exc,(ConnectionError,TimeoutError,socket.gaierror,ssl.SSLError)):
        codes={errno.ETIMEDOUT,errno.ECONNRESET,errno.ECONNABORTED,errno.ECONNREFUSED,errno.EHOSTUNREACH,errno.ENETUNREACH,errno.ENETDOWN,errno.ENETRESET,errno.EPIPE,errno.EAGAIN,errno.ENOBUFS,10051,10054,10060,10061,10065}
        if exc.errno not in codes and getattr(exc,"winerror",None) not in codes:return StopPush(f"本地网络配置/资源错误(不重试): {exc}")
    if not isinstance(exc,(OSError,http.client.HTTPException)):return StopPush(f"非网络程序错误(不重试): {exc}")
    return NetworkFailure(f"{type(exc).__name__}: {exc}")
def resolve_connect(host,port,a): # DNS 在线程解析以免卡死;多地址按剩余预算逐个尝试
    started=time.monotonic();deadline=started+a.connect_timeout;answer=queue.Queue(maxsize=1)
    def resolve():
        try:answer.put((socket.getaddrinfo(host,port,0,socket.SOCK_STREAM),None))
        except Exception as exc:answer.put((None,exc))
    trace(a,"== DNS: %s:%s",host,port);threading.Thread(target=resolve,daemon=True).start()
    while True:
        remaining=deadline-time.monotonic()
        if remaining<=0:raise NetworkFailure("DNS/连接总超时")
        try:addresses,error=answer.get(timeout=min(1,remaining));break
        except queue.Empty:trace(a,"== DNS 等待: %s | %.1fs",host,time.monotonic()-started)
    if error:raise network_error(error)
    trace(a,"== DNS 完成: %s | %.3fs",list(dict.fromkeys(x[4][0] for x in addresses)),time.monotonic()-started);last=None
    for i,(family,kind,proto,_,address) in enumerate(addresses):
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        sock=socket.socket(family,kind,proto);sock.settimeout(min(remaining,max(1,remaining/(len(addresses)-i))));attempt=time.monotonic();trace(a,"== Trying %s:%s",address[0],address[1])
        try:
            sock.connect(address);trace(a,"== Connected %s -> %s | %.3fs",sock.getsockname(),sock.getpeername(),time.monotonic()-attempt);sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1);return sock,deadline
        except OSError as exc:
            last=exc;trace(a,"== TCP 失败: %s | %s",address,exc);sock.close();failure=network_error(exc)
            if isinstance(failure,StopPush):raise failure from exc
    raise NetworkFailure(f"TCP 连接失败或超时: {last or host}")
def header_log(a,direction,name,value): # 请求/响应头日志,敏感头打码并记入密钥表
    lower=name.lower();shown="***" if any(w in lower for w in ("authorization","cookie","token","signature","secret")) else str(value)
    if lower=="location" and "://" in shown:shown=safe_url(shown)
    if shown=="***":remember(value)
    trace(a,"%s header: %s: %s",direction,name,shown)
class TransferMonitor:
    def __init__(self,sock,a,label):
        self.sock=sock;self.a=a;self.label=label;self.lock=threading.Lock();self.done=threading.Event();self.failure=None;self.sent=0;self.received=0;self.phase="等待";self.total=0;self.started=time.monotonic();self.phase_start=self.started;self.phase_count=0;self.last=(self.started,0);self.history=deque([(self.started,0)]);self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def switch(self,phase,total=0):
        with self.lock:self.phase=phase;self.total=total;self.phase_start=time.monotonic();self.phase_count=0;self.last=(self.phase_start,0);self.history=deque([self.last])
    def add(self,count,upload=False):
        with self.lock:
            self.phase_count+=count
            if upload:self.sent+=count
            else:self.received+=count
    def run(self): # 独立线程:send/read 阻塞时也持续输出实时速度;滑动窗持续低速则中断真实 socket
        while not self.done.wait(self.a.progress_interval):
            with self.lock:
                now=time.monotonic();n=self.phase_count;phase=self.phase;elapsed=now-self.phase_start;instant=(n-self.last[1])/max(now-self.last[0],.001);self.last=(now,n);self.history.append((now,n));window=self.a.low_speed_time
                while len(self.history)>1 and self.history[1][0]<=now-window:self.history.popleft()
                slow=window>0 and self.a.low_speed_limit>0 and elapsed>=window and (n-self.history[0][1])/max(now-self.history[0][0],.001)<self.a.low_speed_limit;total=self.total
            LOG.info("[%s] %s %s%s | 当前 %s/s | 平均 %s/s | %.1fs",self.label,phase,human(n),("/"+human(total)) if total else "",human(instant),human(n/max(elapsed,.001)),elapsed)
            if slow:
                self.failure=f"{phase}持续 {window}s 低于 {self.a.low_speed_limit}B/s";LOG.warning("[%s] %s",self.label,self.failure)
                with suppress(OSError):self.sock.shutdown(socket.SHUT_RDWR)
                return
    def check(self):
        if self.failure:raise NetworkFailure(self.failure)
    def close(self):
        self.done.set();self.thread.join(timeout=max(1,self.a.progress_interval+1))
class Response: # dulwich 需要的响应接口,读取与连接释放纳入监测
    def __init__(self,raw,connection,meter,net,url,original):
        self.raw=raw;self.connection=connection;self.meter=meter;self.net=net;self.url=url;self.status=raw.status;self.headers=raw.headers;self.content_type=raw.getheader("Content-Type");self.redirect_location=url if url!=original else "";self.expected=raw.length;self.received=0;self.closed=False
    def geturl(self):return self.url
    def read(self,amt=None):
        if amt is None or amt<0:
            parts=[]
            while True:
                block=self.read(CHUNK)
                if not block:break
                parts.append(block)
            return b"".join(parts)
        if amt==0:return b""
        self.meter.check()
        try:data=self.raw.read(amt)
        except (OSError,http.client.HTTPException) as exc:self.meter.check();raise network_error(exc,True) from exc
        self.meter.check();self.received+=len(data);self.meter.add(len(data))
        if not data and self.expected is not None and self.received<self.expected:raise NetworkFailure("HTTP 响应未达到声明的 Content-Length")
        return data
    def close(self):
        if self.closed:return
        self.closed=True;self.meter.close();self.raw.close();self.connection.close();self.net.live.discard(self);trace(self.net.a,"== Connection closed: %s | 上传 %s | 接收 %s",safe_url(self.url),human(self.meter.sent),human(self.meter.received))
@contextmanager
def request_body(data,cache=None): # 请求体落临时文件:避免整包常驻内存,并让 307/308 可安全重放;cache 非空时 PACK 只生成一次并跨网络重试复用
    if data is None:yield None,0,0;return
    pack=cache.get("pack") if cache is not None else None
    if pack is not None: # 同一远端公告下重试:直接重放已固化 PACK,不重新扫描对象/压缩
        reused=open(pack["path"],"rb")
        try:
            LOG.info("复用已缓存 PACK(跳过对象计算与压缩): %s",human(pack["length"]));yield reused,pack["length"],0
        finally:
            reused.close()
        return
    owned=None;persist=cache is not None
    try:
        if isinstance(data,(bytes,bytearray,memoryview)):owned=io.BytesIO(bytes(data));stream=owned;start=0
        elif hasattr(data,"read") and hasattr(data,"seek"):stream=data;start=stream.tell() # 尊重调用方当前位置,不强行回到 0
        else:
            if persist:
                fd,name=tempfile.mkstemp(prefix=".purepush-pack-",suffix=".tmp");owned=os.fdopen(fd,"w+b")
            else:
                name=None;owned=tempfile.TemporaryFile()
            stream=owned;start=0;size=0;last=time.monotonic();LOG.info("准备 Git 请求体到临时文件,尚未开始网络上传")
            for block in data:
                stream.write(block);size+=len(block)
                if time.monotonic()-last>=1:LOG.info("PACK 本地准备: %s",human(size));last=time.monotonic()
        stream.seek(0,2);length=stream.tell()-start
        if persist: # 固化为命名文件供后续重试原样重放;BytesIO/外部流则复制一份
            stream.flush();name=getattr(stream,"name",None)
            if not isinstance(name,str):
                fd,name=tempfile.mkstemp(prefix=".purepush-pack-",suffix=".tmp");persisted=os.fdopen(fd,"w+b");stream.seek(start);shutil.copyfileobj(stream,persisted,1024*1024);persisted.flush();persisted.close()
                if owned is not None:owned.close();owned=None
                stream=open(name,"rb");start=0;stream.seek(0,2);length=stream.tell()
            cache["pack"]={"path":name,"length":length};LOG.info("PACK 已固化缓存: %s | 网络重试将直接复用,不重算对象",human(length))
        stream.seek(start);yield stream,length,start
    finally:
        if owned is not None:owned.close()
def retry_delay(value):
    if not value:return 0
    try:return min(300,max(0,float(value)))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            return min(300,max(0,parsedate_to_datetime(value).timestamp()-time.time()))
        except (ValueError,TypeError,OverflowError):return 0
def read_limited(response,limit=8*1024*1024):
    blocks=[];size=0
    while True:
        block=response.read(min(CHUNK,limit+1-size));size+=len(block)
        if size>limit:raise StopPush("HTTP JSON 响应超过安全大小限制")
        if not block:return b"".join(blocks)
        blocks.append(block)
def json_request(net,url,payload,headers=None,label="LFS batch"):
    r=net.request("POST",url,{"accept":MEDIA,"content-type":MEDIA,**(headers or {})},json.dumps(payload,separators=(",",":")).encode(),label)
    try:
        body=read_limited(r)
        if not body:return {}
        try:return json.loads(body)
        except ValueError as exc:raise StopPush("LFS 服务端未返回有效 JSON") from exc
    finally:r.close()
def github_api_repo(remote): # github.com/GHE 仓库 API 根;非 GitHub 形态返回 None
    p=urlsplit(remote)
    if p.scheme!="https" or not p.hostname:return None
    parts=[x for x in p.path.strip("/").split("/") if x]
    if len(parts)<2:return None
    slug="/".join(parts[:2]).removesuffix(".git")
    if p.hostname in ("github.com","www.github.com"):return "https://api.github.com/repos/"+slug
    return "https://%s/api/v3/repos/%s"%(p.hostname,slug) # GitHub Enterprise 约定前缀
