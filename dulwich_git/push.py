from .shared import *
from .lfs import *
from .stage import *
from .network import *
class Transport: # Git/LFS 共用标准库传输;默认校验证书;凭据按 origin 绑定
    def __init__(self,a,auths=None,require_tls=False,pack_cache=None):self.a=a;self.auths=auths or {};self.live=set();self.require_tls=require_tls;self.pack_cache=pack_cache
    def close(self):
        for r in list(self.live):r.close()
    def proxy(self,url):
        if self.a.no_proxy:return None
        p=urlsplit(url);raw=self.a.proxy
        if raw is None:
            if proxy_bypass(p.hostname):return None
            ps=getproxies();raw=ps.get(p.scheme) or ps.get("all")
        if not raw:return None
        raw=raw if "://" in raw else "http://"+raw;clean,user,password=split_credentials(raw);pp=urlsplit(clean)
        if pp.scheme!="http":raise StopPush("只支持 HTTP 代理及其 CONNECT;不支持 HTTPS/SOCKS 代理")
        return pp,basic(user,password)
    def socket(self,url):
        p=urlsplit(url);target_port=p.port or (443 if p.scheme=="https" else 8);proxy=self.proxy(url);host=proxy[0].hostname if proxy else p.hostname;port=(proxy[0].port or 80) if proxy else target_port;sock,deadline=resolve_connect(host,port,self.a)
        expired=threading.Event()
        def cancel():
            expired.set()
            with suppress(OSError):sock.shutdown(socket.SHUT_RDWR)
        guard=threading.Timer(max(.001,deadline-time.monotonic()),cancel);guard.daemon=True;guard.start()
        try:
            if proxy and p.scheme=="https":
                authority=(f"[{p.hostname}]" if ":" in p.hostname else p.hostname)+f":{target_port}";headers={"Host":authority}
                if proxy[1]:headers["Proxy-Authorization"]=proxy[1]
                trace(self.a,"=> CONNECT %s HTTP/1.1",authority)
                for k,v in headers.items():header_log(self.a,"=> Send",k,v)
                sock.sendall((f"CONNECT {authority} HTTP/1.1\r\n"+"".join(f"{k}: {v}\r\n" for k,v in headers.items())+"\r\n").encode("latin1"));pr=http.client.HTTPResponse(sock,method="CONNECT");pr.begin()
                for k,v in pr.getheaders():header_log(self.a,"<= Recv",k,v)
                status=pr.status;pr.close()
                if status!=200:raise HTTPFailure(status,url,"代理 CONNECT 失败")
            if p.scheme=="https":
                remaining=deadline-time.monotonic()
                if remaining<=0:raise NetworkFailure("TLS 握手前连接总超时")
                sock.settimeout(remaining);trace(self.a,"== TLS handshake: SNI=%s | 校验证书=开启",p.hostname);ctx=ssl.create_default_context(cafile=self.a.ca_file);sock=ctx.wrap_socket(sock,server_hostname=p.hostname);trace(self.a,"== TLS: %s | %s | 证书到期 %s",sock.version(),sock.cipher()[0],sock.getpeercert().get("notAfter","unknown"))
            if expired.is_set():raise NetworkFailure("TCP/CONNECT/TLS 总连接超时")
            sock.settimeout(self.a.io_timeout);return sock,proxy
        except (OSError,http.client.HTTPException) as exc:
            sock.close()
            if expired.is_set():raise NetworkFailure("TCP/CONNECT/TLS 总连接超时") from exc
            raise network_error(exc) from exc
        except BaseException:sock.close();raise
        finally:guard.cancel()
    def request(self,method,url,headers=None,data=None,label="HTTP",allow_error=False):
        original=url;extra={str(k):str(v) for k,v in (headers or {}).items()}
        use_cache=self.pack_cache is not None and method=="POST" and data is not None and urlsplit(url).path.endswith("git-receive-pack") and not isinstance(data,(bytes,bytearray,memoryview)) and not hasattr(data,"seek")
        with request_body(data,self.pack_cache if use_cache else None) as (stream,length,start):
            for redirect in range(6):
                p=urlsplit(url)
                if p.scheme not in ("http","https") or not p.hostname or p.username is not None:raise StopPush("HTTP 请求地址无效或包含未拆分凭据")
                if self.require_tls and p.scheme!="https":raise StopPush("拒绝将凭据/LFS 数据降级到 HTTP")
                if any(c in url for c in "\r\n\0"):raise StopPush("URL 包含控制字符")
                for k,v in parse_qsl(p.query):
                    if any(w in k.lower() for w in ("signature","credential","token","secret")):remember(v)
                sock,proxy=self.socket(url);connection=http.client.HTTPConnection(p.hostname,p.port or (443 if p.scheme=="https" else 80));connection.sock=sock;meter=TransferMonitor(sock,self.a,label);wrapper=None
                try:
                    target=(p.path or "/")+("?"+p.query if p.query else "");target=quote(target,safe="/%:@!$&'()*+,;=-._~?[]")
                    if proxy and p.scheme=="http":target=urlunsplit((p.scheme,p.netloc,target,"",""))
                    out={"user-agent":"purepush/2 dulwich/1.2.15","accept":"*/*","accept-encoding":"identity","pragma":"no-cache"}
                    for k,v in extra.items(): # dulwich 自带 Accept/Content-Type 等:同名头不重复发送,统一小写
                        lk=k.lower()
                        if lk in ("host","transfer-encoding","expect","proxy-authorization"):continue
                        out[lk]=v
                    out["connection"]="close"
                    if "authorization" not in out and origin(url) in self.auths:out["authorization"]=self.auths[origin(url)]
                    if p.scheme=="http" and "authorization" in out and p.hostname not in ("127.0.0.1","localhost","::1"):raise StopPush("不通过明文 HTTP 发送认证凭据")
                    if proxy and p.scheme=="http" and proxy[1]:out["proxy-authorization"]=proxy[1]
                    if stream is not None:out["content-length"]=str(length)
                    hostname=p.hostname.encode("idna").decode();out["host"]=(f"[{hostname}]" if ":" in hostname else hostname)+(f":{p.port}" if p.port else "")
                    trace(self.a,"=> %s %s HTTP/1.1",method,safe_url(url));connection.putrequest(method,target,skip_host=True,skip_accept_encoding=True)
                    for k,v in out.items():header_log(self.a,"=> Send",k,v);connection.putheader(k,v)
                    meter.switch("上传",length)
                    try:connection.endheaders()
                    except OSError as exc:raise network_error(exc,True) from exc
                    if stream is not None:
                        stream.seek(start);remaining=length;up=time.monotonic()
                        while remaining:
                            block=stream.read(min(CHUNK,remaining))
                            if not block:raise StopPush("本地请求体被截断,拒绝发送错误 Content-Length")
                            view=memoryview(block)
                            while view:
                                meter.check()
                                try:n=sock.send(view)
                                except OSError as exc:raise network_error(exc,True) from exc
                                if n<=0:raise NetworkFailure("socket 在发送请求体时关闭")
                                meter.add(n,True);remaining-=n;view=view[n:] # 只按 socket 实际接受量计速度
                        LOG.info("[%s] 请求体已交给 socket: %s | 平均 %s/s;等待服务端确认",label,human(length),human(length/max(time.monotonic()-up,.001)))
                    meter.switch("等待响应/接收")
                    try:raw=connection.getresponse()
                    except (OSError,http.client.HTTPException) as exc:raise network_error(exc,True) from exc
                    meter.check();trace(self.a,"<= HTTP/%s %d %s","1.1" if raw.version==11 else "1.0",raw.status,raw.reason)
                    for k,v in raw.getheaders():header_log(self.a,"<= Recv",k,v)
                    wrapper=Response(raw,connection,meter,self,url,original);self.live.add(wrapper)
                    if raw.status in (301,302,303,307,308):
                        location=raw.getheader("Location");wrapper.close()
                        if not location:raise StopPush("HTTP 重定向没有 Location")
                        new=urljoin(url,location)
                        if p.scheme=="https" and urlsplit(new).scheme!="https":raise StopPush("拒绝 HTTPS 降级重定向")
                        if method not in ("GET","HEAD") and raw.status not in (307,308):raise StopPush("拒绝可能改变上传方法的 301/302/303")
                        if origin(new)!=origin(url):extra={k:v for k,v in extra.items() if k.lower() in ("accept","content-type","git-protocol")};trace(self.a,"== 跨源重定向,移除认证及自定义头")
                        url=new;continue
                    if not allow_error and not 200<=raw.status<300:
                        body=wrapper.read(4096).decode("utf-8","replace");delay=retry_delay(raw.getheader("Retry-After"));wrapper.close();raise HTTPFailure(raw.status,url,body,delay)
                    if raw.getheader("Content-Encoding","identity").lower() not in ("identity",""):wrapper.close();raise StopPush("服务端返回了压缩内容,暂不支持")
                    return wrapper
                except BaseException:
                    if wrapper is not None:wrapper.close()
                    else:meter.close();connection.close()
                    raise
            raise StopPush("HTTP 重定向超过 5 次")
class StdlibGitClient(AbstractHttpGitClient): # Git 协商/pack/side-band 仍由 dulwich 实现,仅替换 HTTP 层
    def __init__(self,url,net):super().__init__(url,dumb=False);self.net=net
    def _http_request(self,url,headers=None,data=None,raise_for_status=True):
        r=self.net.request("GET" if data is None else "POST",url,headers,data,"Git HTTP body",allow_error=not raise_for_status)
        if raise_for_status and r.status!=200:
            code=r.status;r.close();raise HTTPFailure(code,url)
        return r,r.read
def ancestor(repo,old,new): # 判断 old 是否为 new 的祖先(本地遍历)
    if old==ZERO_SHA or old==new:return True
    todo=[new];seen=set()
    while todo:
        sha=todo.pop()
        if sha==old:return True
        if sha in seen:continue
        seen.add(sha)
        try:obj=repo.object_store[sha]
        except KeyError:raise StopPush("本地历史缺少对象,无法判断快进关系")
        if isinstance(obj,Commit):todo.extend(obj.parents)
    return False
def is_retryable(exc):
    if isinstance(exc,HTTPFailure):return exc.code in (408,425,429,500,502,503,504)
    if isinstance(exc,(NetworkFailure,HangupException)):return True
    if isinstance(exc,GitProtocolError):return any(t in str(exc).lower() for t in ("unexpected eof","unexpected end","unexpectedly closed","expected flush","connection reset","remote end hung up"))
    return False
def retry(a,label,operation): # 仅网络瞬断/超时/可恢复 HTTP 状态重试;认证/证书/程序错误立即传播
    for attempt in range(1,a.retry+1):
        if attempt>1:a.trace=True
        LOG.info("===== %s | 尝试 %d/%d | 间隔 %.1fs =====",label,attempt,a.retry,a.retry_wait)
        try:return operation()
        except Exception as exc:
            if not is_retryable(exc) or attempt==a.retry:raise
            delay=max(a.retry_wait,getattr(exc,"retry_after",0));LOG.warning("网络错误,%.1fs 后重试;下次重新探测引用与申请 LFS action: %s",delay,exc);time.sleep(delay)
def peel_commit_shas(store,shas): # 公告 tip 剥 annotated tag -> commit SHA 集合;本地不存在的 tip 保守忽略
    out=set()
    for sha in shas:
        if not sha or sha==ZERO_SHA or sha not in store:continue
        obj=store[sha]
        while isinstance(obj,Tag):obj=store[obj.object[1]]
        if isinstance(obj,Commit):out.add(obj.id)
    return out
def walk_tree_ids(store,tree_sha,out): # 自有递归保证闭包: 树/子树/Blob/符号链接; 显式跳过 gitlink(0o160000 会被 S_ISDIR 误判); 不使用 dulwich 有漏算的 get_tree_objects
    out.add(tree_sha)
    for _name,mode,sha in store[tree_sha].iteritems():
        if mode==0o160000:continue
        if stat.S_ISDIR(mode):walk_tree_ids(store,sha,out)
        else:out.add(sha)
def reachable_ids(store,commit_shas): # 这些提交可达的全部对象(提交+树+Blob)
    if not commit_shas:return set()
    result=set(store.get_reachability_provider().get_reachable_commits(commit_shas))
    for c in list(result):walk_tree_ids(store,store[c].tree,result)
    return result
def chunk_plan_make(repo,target,refs,threshold): # 按目标树中【真实引用】缺失普通 Blob 的原始大小贪心分组(压缩后只会更小);总量<=阈值返回 None。临时分支不参与分组: 保证重跑/重试时分组与检查点 SHA 稳定
    prefix=b"refs/heads/push-tmp-"+target[:8]+b"-"
    real={k:v for k,v in refs.items() if not k.startswith(prefix)}
    store=repo.object_store;remote=reachable_ids(store,peel_commit_shas(store,real.values()));items=[]
    for e in iter_tree_contents(store,repo[target].tree):
        if e.mode not in (0o100644,0o100755) or e.sha in remote:continue
        items.append((e.path,e.sha,e.mode,store[e.sha].raw_length()))
    items.sort(key=lambda x:x[0]);total=sum(x[3] for x in items)
    LOG.info("分块评估: 缺失普通 Blob %d 个 | 原始量 %s | 请求阈值 %s",len(items),human(total),human(threshold))
    if total<=threshold:return None
    groups=[];cur=[];size=0
    for item in items:
        if cur and size+item[3]>threshold:groups.append(cur);cur=[];size=0
        cur.append(item);size+=item[3]
    if cur:groups.append(cur)
    over=[sum(x[3] for x in g) for g in groups if sum(x[3] for x in g)>threshold]
    if over:LOG.warning("有 %d 个分块超过阈值: 单个普通 Blob 本身就过大,无法再切(该类文件应走 LFS)",len(over))
    return groups
def checkpoint_commit(repo,entries,parent,base): # 累积子集检查点提交: 含真实 Blob(同 SHA),元数据确定性派生自目标提交 -> 同目标 SHA 跨重跑稳定,可断点续传
    c=Commit();c.tree=commit_tree(repo.object_store,[(p,s,m) for p,s,m,_ in entries]);c.parents=[parent] if parent else []
    c.author=c.committer=base.author;c.author_time=c.commit_time=base.commit_time;c.author_timezone=c.commit_timezone=base.commit_timezone;c.message=b"purepush http chunk checkpoint\n"
    c.check();repo.object_store.add_object(c);return c.id
def push_target(repo,a,remote,ref,target,endpoint,auths,cache,done,lease=None): # 每次重试重读公告引用;响应丢失但引用已更新时按幂等成功处理
    holder={"advert":None,"pointers":None,"pack":None,"tmp":[]} # 跨重试复用:公告不变则 LFS 扫描与 PACK 字节都不变,不重算
    def drop_pack():
        pack=holder.pop("pack",None)
        if pack:
            with suppress(OSError):os.unlink(pack["path"])
    def attempt():
        net=Transport(a,auths,urlsplit(remote).scheme=="https",pack_cache=holder);client=StdlibGitClient(remote,net);rp=Progress("remote: ");pp=Progress("PACK: ");observed={};path=urlsplit(remote).path
        def repair_github_default(): # 检查点成了仓库默认分支时 GitHub 拒绝删除: PATCH 默认分支为真实引用,成功返回 True
            if not ref.startswith(b"refs/heads/"):return False
            api=github_api_repo(remote);token=auths.get(origin(remote))
            if not api or not token:return False
            short=text(ref)[len("refs/heads/"):]
            try:
                r=net.request("PATCH",api,{"accept":"application/vnd.github+json","content-type":"application/json","x-github-api-version":"2022-11-28"},json.dumps({"default_branch":short},separators=(",",":")).encode(),"GitHub API",allow_error=True)
                try:
                    body=read_limited(r);ok=200<=r.status<300
                    if ok:LOG.info("检查点此前被 GitHub 设为了仓库默认分支;已通过 API 将默认分支切回 %s",short)
                    else:LOG.warning("切换 GitHub 默认分支到 %s 被拒(HTTP %d): %s;请在仓库 Settings 手动改默认分支后重跑",short,r.status,body[:300].decode("utf-8","replace"))
                finally:r.close()
            except Exception as exc:LOG.warning("调用 GitHub API 切换默认分支失败(可在仓库 Settings 手动处理): %s",exc);return False
            return ok
        def delete_refs(names): # 只删引用不发对象;批量后对失败项逐个单发重试;返回最终仍失败的 {ref: 服务端原因}
            if not names:return {}
            def zgen(have,want,**kwargs):return pack_objects_to_data([],progress=pp)
            def why(m,k): # dulwich 1.2.15 的 ref_status 原因是 str,兼容 bytes
                v=(m or {}).get(k);return v if isinstance(v,str) else (v.decode("utf-8","replace") if isinstance(v,(bytes,bytearray)) else str(v))
            rr=client.send_pack(path,lambda advertised:{n:ZERO_SHA for n in names},zgen,progress=rp);bad={k:why(rr.ref_status,k) for k in names if (rr.ref_status or {}).get(k)}
            if bad and len(names)>1: # 批量中个别被拒时单发重试,排除批内引用互相牵连
                for k in list(bad):
                    one=client.send_pack(path,lambda advertised,kk=k:{kk:ZERO_SHA},zgen,progress=rp);reason=why(one.ref_status,k)
                    if reason:bad[k]=reason
                    else:del bad[k]
            stuck=[k for k,v in bad.items() if "current branch" in (v or "").lower()] # GitHub: 仓库默认分支禁止删除 -> 先改默认分支再删
            if stuck and repair_github_default():
                for k in stuck:
                    one=client.send_pack(path,lambda advertised,kk=k:{kk:ZERO_SHA},zgen,progress=rp);reason=why(one.ref_status,k)
                    if reason:bad[k]=reason
                    else:del bad[k]
            return bad
        def report_bad(title,bad):LOG.warning("%s: %s",title,["%s -> %s"%(text(k),v) for k,v in bad.items()])
        try:
            def update(refs):
                key=tuple(sorted((k,v) for k,v in refs.items() if not k.startswith(b"refs/heads/push-tmp-"))) # 排除本工具自己的检查点引用: 它们推进只代表分块进度,不得作废 LFS/PACK 缓存
                if holder["advert"]!=key:
                    holder["advert"]=key;holder["pointers"]=None;drop_pack()
                old=refs.get(ref) or ZERO_SHA;observed["old"]=old
                if old==target:LOG.info("远端已是目标提交,可能上次成功后响应丢失: %s",target.decode());return {}
                if lease is not None and old!=lease:raise StopPush("force-with-lease 条件不满足,远端已变化")
                if not a.force and lease is None and not ancestor(repo,old,target):raise StopPush("non-fast-forward:远端不是本地目标祖先;请先获取/合并远端历史,重试不能解决")
                if holder["pointers"] is None:holder["pointers"]=outgoing_lfs(repo,refs,target,a) # 53 秒级全历史扫描只做一次
                upload_lfs(net,endpoint,holder["pointers"],cache,ref,done);return {ref:target} # LFS 任一失败则 pack 不开始;done 集合跨重试去重
            def generate(have,want,**kwargs):
                kwargs["progress"]=pp
                return repo.generate_pack_data({s for s in have if s in repo.object_store},want,**kwargs)
            groups=None
            if a.http_chunk_size: # 先手工探测一次引用做校验/LFS/分块评估;send_pack 内部会再取一次(小 GET)
                advert=dict(client.get_refs(path).refs)
                if not update(advert):
                    stale_prefix=b"refs/heads/push-tmp-"+target[:8]+b"-"
                    leftovers=[k for k in advert if k.startswith(stale_prefix)] # 收尾已成功但响应丢失的场景: 目标引用已到位,仍要清掉本目标遗留检查点
                    if leftovers:
                        bad=delete_refs(leftovers)
                        if bad:report_bad("部分遗留临时分支被服务端拒绝删除(不影响推送结果;GitHub 常见原因: 分支保护/ruleset 或存在打开的 PR)",bad)
                        else:LOG.info("目标已在服务端,顺带清理 %d 个遗留临时分支",len(leftovers))
                    return # 远端已是目标(幂等成功)
                groups=chunk_plan_make(repo,target,advert,a.http_chunk_size)
            if groups:
                if a.atomic:raise StopPush("--atomic 与 --http-chunk-size 不兼容: 分块是多个独立 POST,无法整体原子提交")
                if a.push_option:raise StopPush("--push-option 与 --http-chunk-size 暂不支持同时使用(push option 会作用于检查点请求)")
                net.pack_cache=None;store=repo.object_store;base=repo[target]
                def manual_send(updates): # 显式对象集 PACK: 按公告引用可达对象做集合差,绕开 MissingObjectFinder 只认祖先边界的限制
                    def gen(have,want,**kwargs):
                        remote_ids=reachable_ids(store,peel_commit_shas(store,have));need=reachable_ids(store,peel_commit_shas(store,want));ids=sorted(need-remote_ids)
                        LOG.info("本请求 PACK 对象 %d 个 | 远端可达 %d | 目标可达 %d",len(ids),len(remote_ids),len(need))
                        return pack_objects_to_data([(store[i],None) for i in ids],ofs_delta=kwargs.get("ofs_delta",True),progress=pp)
                    def guard(advertised): # 收尾前确认目标引用没被别人改动;临时分支创建不做此检查
                        if ref in updates:
                            now=advertised.get(ref) or ZERO_SHA
                            if now not in (observed.get("old"),target):raise StopPush("推送期间目标引用被其他进程改变,已中止分块收尾")
                        return dict(updates)
                    return client.send_pack(path,guard,gen,progress=rp)
                tmp_prefix=b"refs/heads/push-tmp-"+target[:8]+b"-";present={v for k,v in advert.items() if k.startswith(tmp_prefix)} # 服务端已有的本目标检查点,按【内容 SHA】识别: 阈值/分组方案变化也不会撞序号名
                cumulative=[];parent=None;tmps=[]
                for n,group in enumerate(groups[:-1],1): # 最后一组随真实引用收尾
                    cumulative.extend(group);cp=checkpoint_commit(repo,cumulative,parent,base);parent=cp
                    tmpref=tmp_prefix+str(n).encode()+b"-"+cp[:8];tmps.append(tmpref) # 名字内嵌内容 SHA,天然唯一
                    if cp in present:LOG.info("检查点 %d/%d 内容已在服务端(断点续传),跳过上传",n,len(groups)-1);continue
                    LOG.info("===== 分块检查点 %d/%d | 本组原始量 %s =====",n,len(groups)-1,human(sum(x[3] for x in group)))
                    cr=manual_send({tmpref:cp});cs=cr.ref_status or {}
                    if any(cs.values()):raise StopPush("远端拒绝临时检查点引用: "+repr(cs))
                    present.add(cp);advert[tmpref]=cp
                holder["tmp"]=tmps
                LOG.info("===== 分块收尾: 更新真实引用 %s =====",text(ref))
                result=manual_send({ref:target})
                existing=[t for t in advert if t.startswith(tmp_prefix)] # 仅清理本目标的检查点(含旧命名),避免误删并发推送其他目标的临时分支;对象随最终历史保留
                if existing:
                    bad=delete_refs(existing)
                    if bad:report_bad("部分临时分支被服务端拒绝删除(不影响推送结果;GitHub 常见原因: 分支保护/ruleset 或存在打开的 PR)",bad)
                    else:LOG.info("已删除 %d 个临时分支",len(existing))
                holder["tmp"]=[]
            else:
                result=client.send_pack(path,update,generate,progress=rp,push_options=[v.encode() for v in a.push_option] or None,atomic=a.atomic)
            statuses=result.ref_status or {}
            if any(statuses.values()):raise StopPush("远端拒绝引用更新: "+repr(statuses))
            if observed.get("old")!=target and ref not in statuses:
                current=client.get_refs(path).refs.get(ref)
                if current!=target:raise StopPush("服务端未确认目标引用,不能报告成功")
            LOG.info("推送成功: %s -> %s",text(ref),target.decode())
        finally:rp.finish();pp.finish();net.close();client.close()
    success=False
    try:
        retry(a,f"推送 {safe_url(remote)} {text(ref)}",attempt);success=True
    finally:
        drop_pack()
        if not success and holder["tmp"]:LOG.warning("分块推送未完成;服务端临时分支已保留,重跑同一目标会自动续传: %s",[text(x) for x in holder["tmp"]])
def upload_lfs(net,endpoint,pointers,cache,ref,done): # batch 每组最多100;上传并 verify 成功才标记
    pending=[{"oid":oid,"size":size} for oid,size in sorted(pointers.items()) if (oid,size) not in done]
    for start in range(0,len(pending),100):
        group=pending[start:start+100];requested={i["oid"]:i["size"] for i in group};LOG.info("LFS batch: %d 个对象 -> %s",len(group),safe_url(endpoint))
        answer=json_request(net,endpoint,{"operation":"upload","transfers":["basic"],"hash_algo":"sha256","ref":{"name":text(ref)},"objects":group})
        if not isinstance(answer,dict) or answer.get("transfer","basic")!="basic" or answer.get("hash_algo","sha256")!="sha256":raise StopPush("服务端要求不支持的 LFS 传输/哈希")
        objects=answer.get("objects")
        if not isinstance(objects,list):raise StopPush("LFS batch 缺 objects 列表")
        seen=set()
        for item in objects:
            oid=item.get("oid");size=item.get("size")
            if oid not in requested or oid in seen or size!=requested[oid]:raise StopPush("LFS batch 返回重复/不匹配/未知对象")
            seen.add(oid)
            if item.get("error"):
                e=item["error"];raise HTTPFailure(e.get("code",422),endpoint,str(e.get("message","LFS 对象错误")))
            actions=item.get("actions") or {};up=actions.get("upload");verify=actions.get("verify")
            if up:
                p=cache.resolve(oid,size);LOG.info("LFS 上传对象: %s | %s | 源: %s",oid[:16],human(size),"缓存" if str(p.parent).startswith(str(cache.base)) else "工作区流式")
                with regular_reader(p) as (stream,st): # 读取全程变化即报错;零拷贝源直接来自工作区,无额外磁盘占用
                    r=net.request("PUT",up["href"],{"content-type":"application/octet-stream",**up.get("header",{})},stream,"LFS "+oid[:10])
                    try:read_limited(r)
                    finally:r.close()
                if verify:json_request(net,verify["href"],{"oid":oid,"size":size},verify.get("header",{}),"LFS verify")
            elif actions:raise StopPush("LFS batch 有 actions 但无 upload")
            else:LOG.info("LFS 远端已存在: %s",oid[:16])
            done.add((oid,size))
        if seen!=set(requested):raise StopPush("LFS batch 漏掉请求对象")
def outgoing_lfs(repo,remote_refs,target,a): # 检查真正待发送历史,不只当前工作区
    known={v for v in remote_refs.values() if v and v!=ZERO_SHA and v in repo.object_store};pointers={};checked=0;last=time.monotonic();progress=Progress("PACK 规划: ")
    try:
        for oid,_ in MissingObjectFinder(repo.object_store,known,{target},progress=progress):
            obj=repo.object_store[oid];checked+=1
            if isinstance(obj,Blob):
                length=obj.raw_length()
                if length>=a.max_blob_size:raise StopPush(f"未推送历史含普通大 Blob: {oid.decode()} ({human(length)});新增 .gitattributes 不会迁移旧提交")
                if length<=1024:
                    p=pointer_info(obj.data)
                    if p:
                        k,n=p
                        if k in pointers and pointers[k]!=n:raise StopPush("同一 LFS OID 出现不同 size")
                        pointers[k]=n
            if time.monotonic()-last>=1:LOG.info("检查待推送历史对象: %d | LFS: %d",checked,len(pointers));last=time.monotonic()
    finally:progress.finish()
    return pointers
def parse_identity(value,dn,de):
    value=value.replace("，",",").replace("、",",").strip()
    if not value:return dn,de
    if "," in value:
        name,email=(x.strip() for x in value.split(",",1));return name,email
    parts=value.rsplit(None,1)
    if len(parts)==2:return parts[0],parts[1]
    return parts[0],de
def identity_for(repo,a,user,remote): # -u 强制应用 URL 推导身份,只写仓库配置;否则保留已有/环境身份
    config=repo.get_config_stack();name=text(cfg(config,b"user",b"name"));email=text(cfg(config,b"user",b"email"));parts=urlsplit(remote).path.strip("/").split("/");dn=user if user and user!="x-access-token" else (parts[0] if parts else "");de=f"{dn}@users.noreply.github.com" if urlsplit(remote).hostname in ("github.com","www.github.com") else ""
    if a.user is not None:
        name,email=(dn,de) if a.user=="AUTO" else parse_identity(a.user,dn,de)
    else:
        name=os.environ.get("GIT_AUTHOR_NAME",name);email=os.environ.get("GIT_AUTHOR_EMAIL",email)
    name=a.name or name;email=a.email or email
    if not name or not email:raise StopPush("缺少提交身份;GitHub 可用 -u,其他服务器用 --name/--email")
    if any(c in name+email for c in "\n\r\0<>") or "@" not in email:raise StopPush("提交姓名/邮箱格式无效")
    if a.user is not None or a.name or a.email:
        local=repo.get_config();local.set((b"user",),b"name",name.encode());local.set((b"user",),b"email",email.encode());local.write_to_path()
    LOG.info("提交身份: %s <%s>",name,email);return f"{name} <{email}>".encode()
def lfs_settings(repo,config,remote,remote_name,a,auths): # 支持独立 LFS endpoint;不同主机不继承 Git token
    section=(b"remote",remote_name.encode());endpoint=a.lfs_url or text(cfg(config,b"lfs",b"pushurl") or cfg(config,section,b"lfspushurl") or cfg(config,b"lfs",b"url") or cfg(config,section,b"lfsurl"))
    if not endpoint:
        p=Path(repo.path)/".lfsconfig"
        if p.exists() and not p.is_symlink():
            local=ConfigFile.from_file(io.BytesIO(read_regular(p,1024*1024)));endpoint=text(cfg(local,b"lfs",b"pushurl") or cfg(local,b"lfs",b"url") or cfg(local,section,b"lfsurl"))
    endpoint=endpoint or remote.removesuffix(".git")+".git/info/lfs";clean,user,password=split_credentials(endpoint)
    if urlsplit(clean).query:raise StopPush("LFS endpoint 不能含查询串;action 签名地址除外")
    token=basic(user,password)
    if token:auths[origin(clean)]=token
    return clean.rstrip("/")+"/objects/batch"
def preprocess(argv): # 与参考脚本一致:-m 后全部构成消息;-u 不吞 push/URL
    out=[];i=0;value_flags={"--repo","--repo-path","--path","-path","-p","--branch","-b","--size","-s","--threshold","--retry","-retry","-r","--retry-wait","--retry-seconds","--verbose","-v","--connect-timeout","--io-timeout","--low-speed-limit","--low-speed-time","--progress-interval","--max-commit-size","--max-pack-size","--max-blob-size","--http-chunk-size","--chunk-size","--name","--email","--proxy","--ca-file","--lfs-url","--push-option"}
    while i<len(argv):
        arg=argv[i]
        if arg in ("-m","--message","--commit-msg","--commit_msg"):
            if i+1>=len(argv):raise StopPush("-m 后缺少提交消息")
            out.extend(["--message"," ".join(argv[i+1:])]);break
        if arg in ("-u","--user","--auto-user"):
            nxt=argv[i+1] if i+1<len(argv) else ""
            if nxt and nxt!="push" and not nxt.startswith(("-","git@")) and "://" not in nxt:out.append("--user="+nxt);i+=2
            else:out.append("--user=AUTO");i+=1
            continue
        if arg=="--force-with-lease":
            nxt=argv[i+1] if i+1<len(argv) else ""
            if re.fullmatch("[0-9a-fA-F]{40}",nxt):out.append(arg+"="+nxt.lower());i+=2
            else:out.append(arg+"=auto");i+=1
            continue
        if arg=="--remote" or arg in value_flags:
            if i+1>=len(argv):raise StopPush(f"{arg} 缺少参数")
            out.extend(argv[i:i+2]);i+=2;continue
        if arg.startswith("-"):out.append(arg)
        elif "://" in arg or arg.startswith("git@"):out.extend(["--remote",arg])
        else:out.append(arg)
        i+=1
    return out
def make_parser():
    p=argparse.ArgumentParser(description="dulwich 1.2.15 + 标准库 HTTP(S) 自动提交 push;不运行外部过滤器")
    p.add_argument("mode",nargs="?",choices=["push"],default="push");p.add_argument("--remote",default="");p.add_argument("--repo","--repo-path","--path","-path","-p",default=".");p.add_argument("--branch","-b",default=os.environ.get("BRANCH"));p.add_argument("--user","-u","--auto-user",nargs="?",const="AUTO");p.add_argument("--name");p.add_argument("--email");p.add_argument("--message","-m","--commit-msg","--commit_msg",default="")
    p.add_argument("--no-ask","--noask","-noask","-y","-yes",action="store_true");p.add_argument("--size","-s",type=parse_size,default=100*1024**2);p.add_argument("--threshold",type=int,default=0);p.add_argument("--max-blob-size",type=parse_size,default=100*1024**2);p.add_argument("--max-commit-size",type=parse_size,default=1900*1024**2);p.add_argument("--max-pack-size",type=parse_size,default=1900*1024**2)
    p.add_argument("--retry","-retry","-r",type=int,default=10);p.add_argument("--retry-wait","--retry-seconds",type=float,default=5);p.add_argument("--verbose","-v","-verbosity",type=int,default=2);p.add_argument("--connect-timeout",type=float,default=45);p.add_argument("--io-timeout",type=float,default=300);p.add_argument("--low-speed-limit",type=int,default=10);p.add_argument("--low-speed-time",type=float,default=60);p.add_argument("--progress-interval",type=float,default=.5)
    p.add_argument("--http-chunk-size","--chunk-size",type=parse_size,default=100*1024**2,help="单个 git-receive-pack 请求体阈值(按缺失普通Blob原始量计),超出则用临时分支检查点切成多个请求;0=关闭切分;LFS 不受影响")
    p.add_argument("--proxy");p.add_argument("--no-proxy",action="store_true");p.add_argument("--ca-file");p.add_argument("--lfs-url");p.add_argument("--no-auto-lfs",action="store_true");p.add_argument("--renormalize",action="store_true");p.add_argument("--force",action="store_true");p.add_argument("--force-with-lease",nargs="?",const="auto");p.add_argument("--set-upstream",action="store_true");p.add_argument("--push-option",action="append",default=[]);p.add_argument("--atomic",action="store_true");p.add_argument("--self-test",action="store_true")
    return p
def arguments(argv=None):
    p=make_parser();a=p.parse_args(preprocess(list(sys.argv[1:] if argv is None else argv)));a.trace=a.verbose>=3
    if a.threshold>0:a.size=a.threshold
    vals=(a.connect_timeout,a.io_timeout,a.progress_interval,a.retry_wait,a.low_speed_time)
    if not all(math.isfinite(x) for x in vals):p.error("时间参数不能是 NaN 或无穷大")
    if min(a.size,a.max_blob_size,a.max_commit_size,a.max_pack_size,a.connect_timeout,a.io_timeout,a.progress_interval)<=0 or a.retry<1 or min(a.low_speed_limit,a.low_speed_time,a.retry_wait)<0:p.error("大小/超时/间隔必须为正;retry>=1;低速参数与重试间隔不得为负")
    if a.http_chunk_size<0:p.error("--http-chunk-size 不能为负(0 表示关闭切分)")
    if a.force and a.force_with_lease:p.error("--force 与 --force-with-lease 不能同时使用")
    if a.force_with_lease and a.force_with_lease!="auto" and not re.fullmatch("[0-9a-f]{40}",a.force_with_lease):p.error("--force-with-lease 需要 auto 或完整 40 位预期 ID")
    return a
def main(a):
    installed=version("dulwich");LOG.info("Dulwich 版本: %s | 网络: Python 标准库 http.client/socket/ssl",installed)
    if installed!="1.2.15":LOG.warning("本源码按 1.2.15 接口编写,其他版本未验证")
    root=Path(a.repo).expanduser().resolve()
    if not root.is_dir() and a.repo==".":root.mkdir(parents=True,exist_ok=True)
    if not root.is_dir():raise StopPush("指定仓库目录不存在")
    try:repo=Repo.discover(str(root))
    except NotGitRepository:
        if not a.no_ask and (not sys.stdin.isatty() or input("当前目录不是仓库,是否初始化?[Y/n]: ").strip().lower() not in ("","y","yes")):raise StopPush("已取消初始化;无人值守请用 --no-ask")
        root.mkdir(parents=True,exist_ok=True) # 关键:只预建父目录,dulwich 的 Repo.init 会自己创建 .git,绝不能预建 .git
        repo=Repo.init(str(root))
        if a.branch:repo.refs.set_symbolic_ref(b"HEAD",b"refs/heads/"+a.branch.encode())
    with repo:
        root=Path(repo.path);config=repo.get_config_stack();check_repo_state(repo,config);chain,head=repo.refs.follow(b"HEAD")
        local_branch=text(chain[-1][11:]) if chain and chain[-1].startswith(b"refs/heads/") else ""
        tracking=text(cfg(config,(b"branch",local_branch.encode()),b"remote"));remote_name="origin";section=(b"remote",b"origin");raw=a.remote or text(cfg(config,section,b"pushurl") or cfg(config,section,b"url"))
        if not raw and tracking:remote_name=tracking;section=(b"remote",tracking.encode());raw=text(cfg(config,section,b"pushurl") or cfg(config,section,b"url"))
        if not raw:raise StopPush("没有远程 URL,也未找到 origin/tracking 配置")
        raw=apply_instead_of(config,raw,push=True);remote,user,password,url_branch=normalize_remote(raw);branch=a.branch or url_branch or local_branch
        if not branch:raise StopPush("detached HEAD 必须显式指定 --branch")
        ref=b"refs/heads/"+branch.encode()
        if not check_ref_format(ref):raise StopPush("目标分支名不合法")
        host=urlsplit(remote).hostname
        if host in ("github.com","www.github.com"):
            a.max_blob_size=min(a.max_blob_size,100*1024**2)
            if password is None:password=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN");remember(password)
            user=user or urlsplit(remote).path.strip("/").split("/")[0]
        token=basic(user,password);auths={origin(remote):token} if token else {};endpoint=lfs_settings(repo,config,remote,remote_name,a,auths)
        a.proxy=a.proxy or text(cfg(config,(b"remote",remote_name.encode()),b"proxy") or cfg(config,(b"http",),b"proxy")) or None
        a.ca_file=a.ca_file or text(cfg(config,(b"http",),b"sslcainfo")) or None
        if a.ca_file:
            a.ca_file=str(Path(a.ca_file).expanduser())
            if not Path(a.ca_file).is_file():raise StopPush("--ca-file/http.sslCAInfo 指定的 CA 文件不存在")
        if a.remote:
            local=repo.get_config();local.set((b"remote",remote_name.encode()),b"url",remote.encode());local.set((b"remote",remote_name.encode()),b"pushurl",remote.encode());local.set((b"remote",remote_name.encode()),b"fetch",f"+refs/heads/*:refs/remotes/{remote_name}/*".encode());local.write_to_path()
        LOG.info("仓库路径: %s",root);LOG.info("远程地址: %s | 分支: %s",safe_url(remote),branch);LOG.info("LFS 阈值: %s | 最大普通 Blob: %s",human(a.size),human(a.max_blob_size));LOG.info("连接超时: %ss | 低速: %dB/s 持续 %ss | 每 %.2fs 输出",a.connect_timeout,a.low_speed_limit,a.low_speed_time,a.progress_interval)
        identity=identity_for(repo,a,user,remote);cache=LFSCache(repo,config)
        ids,empty=prepare(repo,a,identity,cache)
        try:target=repo.head()
        except KeyError:raise StopPush("工作区没有可跟踪文件,仓库仍无提交,无法 push")
        ids=ids or [target];lease=None
        if a.force_with_lease:
            expected_ref=b"refs/remotes/"+remote_name.encode()+b"/"+branch.encode()
            lease=repo.refs[expected_ref] if a.force_with_lease=="auto" and expected_ref in repo.refs else (ZERO_SHA if a.force_with_lease=="auto" else a.force_with_lease.encode())
        done=set()
        for position,commit in enumerate(ids,1):
            LOG.info("推送分段 %d/%d",position,len(ids));push_target(repo,a,remote,ref,commit,endpoint,auths,cache,done,lease)
            repo.refs[b"refs/remotes/"+remote_name.encode()+b"/"+branch.encode()]=commit
            if lease is not None:lease=commit
        repo.refs[b"refs/remotes/"+remote_name.encode()+b"/"+branch.encode()]=target
        if a.set_upstream and local_branch:
            local=repo.get_config();local.set((b"branch",local_branch.encode()),b"remote",remote_name.encode());local.set((b"branch",local_branch.encode()),b"merge",ref);local.write_to_path()
        if empty:
            readme=root/"ReadMe.md"
            if readme.is_file() and not readme.is_symlink() and hashlib.sha256(read_regular(readme)).hexdigest()==empty:atomic_write(readme,b"");LOG.info("EmptyAfterPush: ReadMe.md 已按标记清空,保留为工作区修改")
            else:LOG.warning("ReadMe.md 推送后已变化或不存在,不执行清空")
        LOG.info("推送成功 %s;操作结束",stamp())
def self_test():
    import unittest
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from dulwich.client import LocalGitClient
    class T(unittest.TestCase):
        def setUp(self):
            self.temp=tempfile.TemporaryDirectory();td=Path(self.temp.name);self.root=td/"repo";self.root.mkdir(parents=True);self.repo=Repo.init(str(self.root))
            self.a=arguments(["push","--no-ask","--no-proxy","--size","256B","--retry","2","--retry-wait","0","--low-speed-time","999"]);self.a.trace=False
            cf=self.repo.get_config();cf.set((b"user",),b"name",b"test");cf.set((b"user",),b"email",b"test@example.com");cf.set((b"core",),b"autocrlf",b"false");cf.set((b"core",),b"safecrlf",b"false");cf.set((b"core",),b"attributesfile",os.fsencode(td/"none-attrs"));cf.set((b"core",),b"excludesfile",os.fsencode(td/"global-ignore"));cf.set((b"commit",),b"gpgsign",b"false");cf.write_to_path()
            self.identity=b"test <test@example.com>";self.cache=LFSCache(self.repo,self.repo.get_config())
        def tearDown(self):
            self.repo.close();self.temp.cleanup()
        def write(self,name,data):
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p
        def stage(self):return prepare(self.repo,self.a,self.identity,self.cache)
        def test_01_init_layout(self):
            self.assertTrue((self.root/".git").is_dir());head=self.repo.refs.read_ref(b"HEAD");self.assertIn(b"ref: refs/heads/",head)
        def test_02_basic_commit(self):
            self.write("a.txt",b"hello");ids,_=self.stage();self.assertEqual(len(ids),1);self.assertEqual(self.repo.head(),ids[0]);self.assertEqual(self.stage()[0],[])
        def test_03_ignore_basic(self):
            self.write(".gitignore",b"*.tmp\n!keep.tmp\n");self.write("d.tmp",b"x");self.write("keep.tmp",b"x");self.stage();idx=self.repo.open_index();self.assertIn(b"keep.tmp",idx);self.assertNotIn(b"d.tmp",idx)
        def test_04_negate_parent_dir(self):
            self.write(".gitignore",b"blocked/\n!blocked/no.txt\nselect/*\n!select/keep.txt\n");self.write("blocked/no.txt",b"x");self.write("select/keep.txt",b"y");self.write("select/no.txt",b"z");self.stage();idx=self.repo.open_index();self.assertIn(b"select/keep.txt",idx);self.assertNotIn(b"select/no.txt",idx);self.assertNotIn(b"blocked/no.txt",idx)
        def test_05_tracked_bypasses_ignore(self):
            self.write("t.tmp",b"old");self.stage();self.write(".gitignore",b"*.tmp\n");self.write("t.tmp",b"new");self.stage();self.assertIn(b"t.tmp",self.repo.open_index())
        def test_06_deletion(self):
            p=self.write("gone.txt",b"x");self.stage();p.unlink();self.stage();self.assertNotIn(b"gone.txt",self.repo.open_index())
        def test_07_global_info_precedence(self):
            td=Path(self.temp.name);(td/"global-ignore").write_bytes(b"*.bak\n!special.dat\n");info=self.root/".git"/"info";info.mkdir(parents=True,exist_ok=True);(info/"exclude").write_bytes(b"!keep.bak\nspecial.dat\n")
            self.write("keep.bak",b"1");self.write("drop.bak",b"2");self.write("special.dat",b"3");self.stage();idx=self.repo.open_index();self.assertIn(b"keep.bak",idx);self.assertNotIn(b"drop.bak",idx);self.assertNotIn(b"special.dat",idx)
        def test_08_auto_lfs_exact_rules(self):
            self.write("big.bin",b"x"*600);self.write("has space.bin",b"y"*600);self.write("化学[上册].pdf",b"z"*600);self.stage();idx=self.repo.open_index()
            for n in (b"big.bin",b"has space.bin","化学[上册].pdf".encode()):
                oid,size=pointer_info(self.repo.object_store[idx[n].sha].data);sp,fid=self.cache.sources[(oid,size)];self.assertEqual(sp.stat().st_size,size) # 零拷贝:工作区源而非缓存副本
            self.assertFalse((self.cache.base/"objects").exists()) # 对象目录根本不创建,不占额外磁盘
            attr=(self.root/".gitattributes").read_bytes();self.assertIn(b"big.bin",attr)
        def test_09_manual_pointer_needs_cache(self): # 小文件不再因通配规则进 LFS;手工指针必须有缓存对象背书
            self.write(".gitattributes",b"*.lfs filter=lfs diff=lfs merge=lfs -text\n");src=self.write("cache-src.bin",b"w"*5);oid,size=self.cache.import_object(src)
            self.write("s.lfs",b"q");self.write("already.bin",pointer_bytes(oid,size));self.stage();idx=self.repo.open_index()
            self.assertEqual(self.repo.object_store[idx[b"s.lfs"].sha].data,b"q") # *.lfs 通配行已按阈值清除
            self.assertEqual(self.repo.object_store[idx[b"already.bin"].sha].data,pointer_bytes(oid,size));self.assertEqual((self.root/".gitattributes").read_bytes().find(b"*.lfs"),-1)
        def test_10_oversize_reject(self):
            self.a.max_blob_size=400;self.a.no_auto_lfs=True;self.write("big",b"v"*512)
            with self.assertRaises(StopPush):self.stage()
        def test_11_attr_macro_cquote(self): # 宏展开 + 根锚定 C 引号路径匹配由 Attributes 验证;最终是否 LFS 仍由阈值裁决
            self.write(".gitattributes",b"[attr]large filter=lfs -text\n\"/has space.txt\" large\n");self.write("has space.txt",b"a"*300)
            attrs=Attributes(self.repo,self.repo.get_config_stack(),self.repo.open_index());self.assertEqual(attrs.get(b"has space.txt")[b"filter"],b"lfs") # [attr]large 宏已展开并匹配根锚定引号路径
            self.stage();self.assertIsNotNone(pointer_info(self.repo.object_store[self.repo.open_index()[b"has space.txt"].sha].data))
        def test_12_crlf_normalize(self):
            self.write(".gitattributes",b"*.txt text eol=lf\n");self.write("c.txt",b"a\r\nb\r\n");self.stage();self.assertEqual(self.repo.object_store[self.repo.open_index()[b"c.txt"].sha].data,b"a\nb\n")
        def test_13_split_commits(self):
            self.a.max_commit_size=5
            for i in range(3):self.write(f"f{i}",b"abcd")
            ids,_=self.stage();self.assertEqual(len(ids),3);self.assertEqual(self.repo.head(),ids[-1])
        def test_14_local_push(self):
            self.write("f",b"data");self.stage();bare=Repo.init_bare(str(Path(self.temp.name)/"bare.git"),mkdir=True)
            try:
                c=LocalGitClient();r=c.send_pack(bare.path,lambda refs:{b"refs/heads/main":self.repo.head()},self.repo.generate_pack_data);self.assertFalse(any((r.ref_status or {}).values()));self.assertEqual(bare.refs[b"refs/heads/main"],self.repo.head())
                r=c.send_pack(bare.path,lambda refs:{b"refs/heads/main":self.repo.head()},self.repo.generate_pack_data);self.assertFalse(any((r.ref_status or {}).values()))
            finally:bare.close()
        def smart_server(self):
            from dulwich.server import DictBackend
            from dulwich.web import make_wsgi_chain
            from wsgiref.simple_server import make_server,WSGIRequestHandler
            class Quiet(WSGIRequestHandler):
                def log_message(self,*a):pass
            bare=Repo.init_bare(str(Path(self.temp.name)/"remote.git"),mkdir=True);app=make_wsgi_chain(DictBackend({"/test.git":bare}));server=make_server("127.0.0.1",0,app,handler_class=Quiet);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url=f"http://127.0.0.1:{server.server_port}/test.git"
            return bare,server,thread,url
        def test_15_smart_http_push(self):
            self.write("http-file",b"smart");self.stage();bare,server,thread,url=self.smart_server()
            try:
                for _ in range(2):push_target(self.repo,self.a,url,b"refs/heads/main",self.repo.head(),url+"/info/lfs/objects/batch",{},self.cache,set())
                self.assertEqual(bare.refs[b"refs/heads/main"],self.repo.head())
            finally:server.shutdown();server.server_close();thread.join(timeout=2);bare.close()
        def test_16_empty_after_push(self): # 端到端:子进程跑真实 CLI,推送成功后才清空 ReadMe.md
            self.write("ReadMe.md",b"#EmptyAfterPush\nnote");bare,server,thread,url=self.smart_server()
            import subprocess
            try:
                args=[sys.executable,os.path.abspath(__file__),"--no-ask","--no-proxy","--remote",url,"--size","256B","--retry","2","--retry-wait","0","--low-speed-time","999","push"]
                p=subprocess.run(args,cwd=str(self.root),stdout=subprocess.PIPE,stderr=subprocess.STDOUT);out=p.stdout.decode("utf-8","replace")
                self.assertEqual(p.returncode,0,out);self.assertEqual((self.root/"ReadMe.md").read_bytes(),b"");self.assertEqual(bare.refs[b"refs/heads/master"]!=ZERO_SHA,True)
            finally:server.shutdown();server.server_close();thread.join(timeout=2);bare.close()
        def test_17_http_retry_and_auth(self):
            counters={}
            class H(BaseHTTPRequestHandler):
                protocol_version="HTTP/1.1"
                def log_message(self,*a):pass
                def reply(self,code,body=b"ok"):
                    self.send_response(code);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
                def do_GET(self):
                    counters[self.path]=counters.get(self.path,0)+1
                    if self.path=="/flaky" and counters[self.path]==1:self.reply(503);return
                    if self.path=="/deny":self.reply(401,b"no");return
                    self.reply(200)
            server=ThreadingHTTPServer(("127.0.0.1",0),H);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();net=Transport(self.a);base=f"http://127.0.0.1:{server.server_port}"
            try:
                def get(p):
                    def op():
                        r=net.request("GET",base+p)
                        try:self.assertEqual(r.read(),b"ok")
                        finally:r.close()
                    return retry(self.a,"测试重试",op)
                get("/flaky");self.assertEqual(counters["/flaky"],2)
                with self.assertRaises(HTTPFailure):get("/deny")
                self.assertEqual(counters["/deny"],1)
            finally:net.close();server.shutdown();server.server_close();thread.join(timeout=2)
        def test_18_redirect_strips_auth(self): # 跨源 302:认证头不得带到新 origin
            def make_handler(target=None):
                class H(BaseHTTPRequestHandler):
                    protocol_version="HTTP/1.1"
                    def log_message(self,*a):pass
                    def do_GET(self):
                        if self.path=="/start":self.send_response(302);self.send_header("Location",f"http://127.0.0.1:{target}/check");self.end_headers()
                        else:
                            body=self.headers.get("Authorization","<none>").encode();self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
                return H
            sa=ThreadingHTTPServer(("127.0.0.1",0),make_handler());sb=ThreadingHTTPServer(("127.0.0.1",0),make_handler(sa.server_port));ta=threading.Thread(target=sa.serve_forever,daemon=True);tb=threading.Thread(target=sb.serve_forever,daemon=True);ta.start();tb.start()
            net=Transport(self.a,{origin(f"http://127.0.0.1:{sb.server_port}"):"Basic secret"})
            try:
                r=net.request("GET",f"http://127.0.0.1:{sb.server_port}/start")
                try:self.assertEqual(r.read(),b"<none>")
                finally:r.close()
            finally:net.close();sa.shutdown();sb.shutdown();sa.server_close();sb.server_close();ta.join(timeout=2);tb.join(timeout=2)
        def test_19_low_speed_watchdog(self):
            class Dummy:
                closed=False
                def shutdown(self,h):self.closed=True
            sock=Dummy();self.a.progress_interval=.01;self.a.low_speed_limit=1;self.a.low_speed_time=.04;m=TransferMonitor(sock,self.a,"t");deadline=time.monotonic()+2
            while not sock.closed and time.monotonic()<deadline:time.sleep(.01)
            try:self.assertTrue(sock.closed);self.assertIsNotNone(m.failure)
            finally:m.close()
        def test_20_rapid_recreate_read(self):
            p=self.root/"rapid.bin"
            for _ in range(5):
                p.write_bytes(b"a");p.unlink();p.write_bytes(b"b"*33);self.assertEqual(read_regular(p),b"b"*33);p.unlink()
        def test_21_cli_parse(self):
            a=arguments(["-v","3","-u","push","https://u:ghp_x@github.com/u/r","--retry","4","-m","hello","world"]);self.assertEqual(a.user,"AUTO");self.assertEqual(a.message,"hello world");self.assertEqual(a.retry,4)
            a=arguments(["--remote=https://github.com/u/r","--proxy=http://127.0.0.1:8080"]);self.assertEqual(a.proxy,"http://127.0.0.1:8080")
        def lfs_server(self): # 独立 LFS 服务(batch/PUT/verify),返回 server/thread/endpoint/state
            state={"puts":{},"verify":0,"batch":0}
            class LH(BaseHTTPRequestHandler):
                PORT=0;protocol_version="HTTP/1.1"
                def log_message(self,*a):pass
                def read_body(self):
                    n=int(self.headers.get("Content-Length","0"));return self.rfile.read(n) if n else b""
                def send_json(self,code,obj):
                    body=json.dumps(obj,separators=(",",":")).encode();self.send_response(code);self.send_header("Content-Type",MEDIA);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
                def do_POST(self):
                    body=self.read_body()
                    if self.path.endswith("/objects/batch"):
                        state["batch"]+=1;req=json.loads(body);objs=[]
                        for o in req["objects"]:
                            oid=o["oid"];objs.append({"oid":oid,"size":o["size"],"actions":{"upload":{"href":f"http://127.0.0.1:{self.PORT}/put/{oid}","header":{"X-Test":"yes"}},"verify":{"href":f"http://127.0.0.1:{self.PORT}/verify/{oid}"}}})
                        self.send_json(200,{"transfer":"basic","objects":objs})
                    elif self.path.startswith("/verify/"):
                        state["verify"]+=1;json.loads(body);self.send_json(200,{})
                    else:self.send_json(404,{"message":"no"})
                def do_PUT(self):
                    state["puts"][self.path.rsplit("/",1)[-1]]=self.read_body();self.send_response(200);self.send_header("Content-Length","0");self.end_headers()
            server=ThreadingHTTPServer(("127.0.0.1",0),LH);LH.PORT=server.server_port;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            return server,thread,f"http://127.0.0.1:{server.server_port}/objects/batch",state
        def test_22_lfs_end_to_end(self): # 全链路零拷贝流式:smart + LFS(batch/PUT/verify);二次 push 幂等不重复 batch
            self.write("big.dat",b"Z"*300);self.stage();bare,server,thread,url=self.smart_server();lfs,tl,endpoint,state=self.lfs_server()
            try:
                for _ in range(2):push_target(self.repo,self.a,url,b"refs/heads/main",self.repo.head(),endpoint,{},self.cache,set())
                self.assertEqual(state["batch"],1);self.assertEqual(state["verify"],1);self.assertEqual(len(state["puts"]),1);oid,blob=next(iter(state["puts"].items()));self.assertEqual(len(blob),300);self.assertEqual(hashlib.sha256(blob).hexdigest(),oid)
                self.assertFalse((self.cache.base/"objects").exists()) # 全程零拷贝,缓存对象目录不存在
                self.assertEqual(bare.refs[b"refs/heads/main"],self.repo.head());tree=bare[bare[bare.refs[b"refs/heads/main"]].tree];self.assertIsNotNone(pointer_info(bare.object_store[tree[b"big.dat"][1]].data))
            finally:server.shutdown();server.server_close();thread.join(timeout=2);lfs.shutdown();lfs.server_close();tl.join(timeout=2);bare.close()
        def test_23_threshold_authoritative(self): # 复现 energetic 事故:*.pdf 通配规则 + 过期精确规则必须被阈值重建清除
            self.write(".gitattributes",b"*.pdf filter=lfs diff=lfs merge=lfs -text\n*.epub filter=lfs diff=lfs merge=lfs -text\n\"old.pdf\" filter=lfs diff=lfs merge=lfs -text\n# \xe4\xbf\x9d\xe7\x95\x99\xe6\xb3\xa8\xe9\x87\x8a\nother.txt binary\n")
            self.write("small.pdf",b"s"*200);self.write("old.pdf",b"o"*100);self.write("big.pdf",b"b"*300);self.stage();idx=self.repo.open_index();attr=(self.root/".gitattributes").read_bytes()
            self.assertEqual(attr.find(b"*.pdf"),-1);self.assertEqual(attr.find(b"*.epub"),-1);self.assertEqual(attr.find(b'"old.pdf"'),-1) # 通配/过期行全清除
            self.assertIn(b"# \xe4\xbf\x9d\xe7\x95\x99\xe6\xb3\xa8\xe9\x87\x8a",attr);self.assertIn(b"other.txt binary",attr) # 非 LFS 行保留
            self.assertEqual(self.repo.object_store[idx[b"small.pdf"].sha].data,b"s"*200) # 49MB 类小文件:普通 Blob 不是 LFS
            self.assertEqual(self.repo.object_store[idx[b"old.pdf"].sha].data,b"o"*100)
            self.assertIsNotNone(pointer_info(self.repo.object_store[idx[b"big.pdf"].sha].data)) # ≥阈值才是指针
        def test_24_source_changed_before_upload(self): # 暂存后、上传前工作区源被改动:零拷贝校验拒绝,不重试不发错误内容
            self.write("big.dat",b"Z"*300);self.stage();(self.root/"big.dat").write_bytes(b"Z"*300+b"X") # 模拟上传前文件变化
            bare,server,thread,url=self.smart_server();lfs,tl,endpoint,state=self.lfs_server()
            try:
                with self.assertRaises(StopPush):push_target(self.repo,self.a,url,b"refs/heads/main",self.repo.head(),endpoint,{},self.cache,set())
                self.assertEqual(len(state["puts"]),0) # 一个字节都不允许上传
            finally:server.shutdown();server.server_close();thread.join(timeout=2);lfs.shutdown();lfs.server_close();tl.join(timeout=2);bare.close()
        def test_25_disk_space_preflight(self): # 磁盘不足 1MiB:在拿索引锁之前直接拒绝
            real=shutil.disk_usage(self.root);tiny=type(real)(real.total,real.total-1,1);old=shutil.disk_usage;shutil.disk_usage=lambda p:tiny
            try:
                with self.assertRaises(StopPush):self.stage()
                self.assertFalse((self.root/".git"/"index.lock").exists()) # 预检失败不得留锁
            finally:shutil.disk_usage=old
        def test_26_request_body_generator(self): # generator 经临时文件 spool:length 不得为 0,且 307 重放内容完整
            with request_body(x for x in [b"abc",b"def",b"g"]) as (stream,length,start):
                self.assertEqual(length,7);self.assertEqual(stream.read(),b"abcdefg") # 消费后 start=0,不是末尾
            bio=io.BytesIO(b"zzzpayload");bio.read(2) # 调用方预置位置必须尊重
            with request_body(bio) as (stream,length,start):self.assertEqual(start,2);self.assertEqual(length,8);self.assertEqual(stream.read(),b"zpayload")
        def test_27_ssl_error_classification(self): # [SYS] SSL_ERROR_SYSCALL 是底层连接中断,必须可重试;握手期的通用协议错误才致命
            syscall=ssl.SSLSyscallError(0,"[SYS] unknown error (_ssl.c:2417)")
            self.assertIsInstance(network_error(syscall),NetworkFailure)
            self.assertIsInstance(network_error(syscall,True),NetworkFailure)
            self.assertTrue(is_retryable(network_error(syscall)))
            self.assertIsInstance(network_error(ssl.SSLEOFError("unexpected eof")),NetworkFailure)
            self.assertIsInstance(network_error(ssl.SSLZeroReturnError("closed")),NetworkFailure)
            self.assertIsInstance(network_error(ssl.SSLError(1,"[SSL: WRONG_VERSION_NUMBER]")),StopPush) # 握手期协议/中间人问题不盲目重试
            self.assertIsInstance(network_error(ssl.SSLError(1,"[SSL: BAD_DECRYPT]"),True),NetworkFailure) # 同一错误发生在收发阶段则按线路瞬断重试
            self.assertIsInstance(network_error(ssl.SSLCertVerificationError("cert verify failed")),StopPush)
        def test_28_http_chunk_split(self): # 网络分块: 每 POST 仅一个阈值内的 PACK;真实提交/分支只出现一次;临时分支清理干净;内容逐字节一致;检查点确定性可续传;二次推送幂等
            self.a.size=self.a.max_blob_size=64*1024**2;N=26;UNIT=256*1024;THRESH=2*1024**2;payloads={}
            for i in range(N):
                d=os.urandom(UNIT);payloads[f"f{i:02d}.bin"]=d;self.write(f"f{i:02d}.bin",d)
            self.stage();target=self.repo.head();bare,server,thread,url=self.smart_server();sent=[]
            class Measured(Transport):
                def request(self,method,u,headers=None,data=None,label="HTTP",allow_error=False):
                    r=super().request(method,u,headers,data,label,allow_error)
                    if method=="POST" and urlsplit(u).path.endswith("git-receive-pack"):sent.append(r.meter.sent)
                    return r
            old=globals()["Transport"];globals()["Transport"]=Measured;self.a.http_chunk_size=THRESH
            try:
                push_target(self.repo,self.a,url,b"refs/heads/main",target,url+"/info/lfs/objects/batch",{},self.cache,set())
                self.assertEqual(len(sent),5,sent) # 3 检查点 + 1 收尾 + 1 批量删临时分支
                for n in sent:self.assertLessEqual(n,THRESH+256*1024,(n,sent))
                self.assertEqual(bare.refs[b"refs/heads/main"],target)
                left=[r for r in list(bare.refs) if r.startswith(b"refs/heads/push-tmp-")];self.assertEqual(left,[])
                remote=Repo(str(bare.path)) # Windows 下重开句柄才能看到新 pack
                try:
                    rc=remote[remote.refs[b"refs/heads/main"]]
                    for e in iter_tree_contents(remote.object_store,rc.tree):
                        if e.mode in (0o100644,0o100755):self.assertEqual(remote.object_store[e.sha].data,self.repo.object_store[e.sha].data)
                finally:remote.close()
                push_target(self.repo,self.a,url,b"refs/heads/main",target,url+"/info/lfs/objects/batch",{},self.cache,set()) # 幂等:不再发 POST
                self.assertEqual(len(sent),5)
                # 复现真实事故: 旧方案留下同序号但 SHA 不同的检查点 push-tmp-<目标8位>-1;先把真实引用退回一个空提交(强制)
                from dulwich.objects import Tree
                et=Tree();self.repo.object_store.add_object(et);ec=Commit();ec.tree=et.id;ec.author=ec.committer=b"t <t@t>";ec.author_time=ec.commit_time=0;ec.author_timezone=ec.commit_timezone=0;ec.message=b"x";ec.check();self.repo.object_store.add_object(ec)
                stale=Measured(self.a);cl=StdlibGitClient(url,stale)
                try:
                    cl.send_pack("/test.git",lambda refs:{b"refs/heads/main":ec.id},lambda have,want,**k:pack_objects_to_data([(et,None),(ec,None)])) # 强制退回: 目标对象仍由旧临时引用保活
                    oldname=b"refs/heads/push-tmp-"+target[:8]+b"-1"
                    cl.send_pack("/test.git",lambda refs:{oldname:target},lambda have,want,**k:pack_objects_to_data([]))
                finally:stale.close()
                self.assertIn(oldname,{k:bare.refs[k] for k in bare.refs if k.startswith(b"refs/")})
                self.a.force=True
                try:
                    push_target(self.repo,self.a,url,b"refs/heads/main",target,url+"/info/lfs/objects/batch",{},self.cache,set()) # 不得再因同名检查点报错;旧引用在收尾时被清掉
                finally:self.a.force=False
                self.assertEqual(bare.refs[b"refs/heads/main"],target)
                self.assertFalse([r for r in list(bare.refs) if r.startswith(b"refs/heads/push-tmp-"+target[:8])])
                remote=Repo(str(bare.path)) # 重开句柄;旧引用保活的全部 Blob 必须仍逐字节一致
                try:
                    rc=remote[remote.refs[b"refs/heads/main"]]
                    for e in iter_tree_contents(remote.object_store,rc.tree):
                        if e.mode in (0o100644,0o100755):self.assertEqual(remote.object_store[e.sha].data,self.repo.object_store[e.sha].data)
                finally:remote.close()
                stale=Measured(self.a);cl=StdlibGitClient(url,stale) # 幂等路径(目标已到位)也必须清理本目标旧命名遗留,且不动别的目标的前缀
                try:
                    cl.send_pack("/test.git",lambda refs:{b"refs/heads/push-tmp-"+target[:8]+b"-7":target,b"refs/heads/push-tmp-other00-1":target},lambda have,want,**k:pack_objects_to_data([]))
                finally:stale.close()
                push_target(self.repo,self.a,url,b"refs/heads/main",target,url+"/info/lfs/objects/batch",{},self.cache,set())
                sr={k:bare.refs[k] for k in bare.refs if k.startswith(b"refs/")}
                self.assertNotIn(b"refs/heads/push-tmp-"+target[:8]+b"-7",sr)
                self.assertIn(b"refs/heads/push-tmp-other00-1",sr) # 只清本目标,不误伤并发推送
                server_refs={k:bare.refs[k] for k in bare.refs if k.startswith(b"refs/")}
                groups=chunk_plan_make(self.repo,target,server_refs,THRESH) # 全量已在服务端 -> 无需分块
                self.assertIsNone(groups)
                groups=chunk_plan_make(self.repo,target,{b"refs/heads/main":ZERO_SHA},THRESH)
                self.assertEqual(len(groups),4) # 8+8+8+2
                def rebuild():
                    cum=[];parent=None;ids=[]
                    for g in groups:
                        cum=cum+g;parent=checkpoint_commit(self.repo,cum,parent,self.repo[target]);ids.append(parent)
                    return ids
                self.assertEqual(rebuild(),rebuild()) # 检查点 SHA 确定性: 断点续传靠它
            finally:
                globals()["Transport"]=old;server.shutdown();server.server_close();thread.join(timeout=2);bare.close()
        def test_29_github_api_repo_url(self):
            self.assertEqual(github_api_repo("https://github.com/eightobox/eightobox.git"),"https://api.github.com/repos/eightobox/eightobox")
            self.assertEqual(github_api_repo("https://github.com/o/r/"),"https://api.github.com/repos/o/r")
            self.assertEqual(github_api_repo("https://ghe.corp/team/r.git"),"https://ghe.corp/api/v3/repos/team/r")
            self.assertIsNone(github_api_repo("http://127.0.0.1:8080/test.git")) # 本地 HTTP 测试服务不走 GitHub API
            self.assertIsNone(github_api_repo("https://github.com/"))
    r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(T));return 0 if r.wasSuccessful() else 1
if __name__=="__main__":
    try:
        a=arguments();setup_logging(a.verbose)
        if a.self_test:sys.exit(self_test())
        main(a)
    except KeyboardInterrupt:LOG.warning("用户中断;已创建提交与 LFS 快照保留,下次可继续");sys.exit(130)
    except Exception as exc:
        if not LOG.handlers:setup_logging(2)
        LOG.error("失败: %s",exc,exc_info=LOG.isEnabledFor(logging.DEBUG));sys.exit(1)
