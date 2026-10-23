from .shared import *
from .lfs import *
def tree_map(repo,head):
    if not head:return {}
    return {e.path:(e.sha,e.mode) for e in iter_tree_contents(repo.object_store,repo[head].tree)}
def check_repo_state(repo,config): # merge/rebase/浅克隆/部分克隆/替换历史必须先由用户处理
    if repo.bare:raise StopPush("自动暂存 push 需要非 bare 工作区")
    if cfg(config,b"extensions",b"objectformat",b"sha1")!=b"sha1":raise StopPush("本版本只处理 SHA-1 仓库;LFS 内容仍使用 SHA-256")
    if repo.get_shallow():raise StopPush("浅仓库尚未支持,请先补全历史")
    if cfg(config,b"extensions",b"partialclone"):raise StopPush("部分克隆尚未支持,请先补全对象")
    for section in config.sections():
        if section[:1]==(b"remote",) and cfg(config,section,b"promisor"):raise StopPush("promisor 部分克隆尚未支持")
    if config.get_boolean((b"core",),b"sparsecheckout",False) or cfg(config,b"core",b"splitindex"):raise StopPush("不自动修改稀疏检出或分裂索引仓库")
    for name in ("MERGE_HEAD","CHERRY_PICK_HEAD","REVERT_HEAD","rebase-merge","rebase-apply","sequencer"):
        if (Path(repo.controldir())/name).exists():raise StopPush(f"仓库操作尚未结束: {name},请先处理")
    if config.get_boolean((b"commit",),b"gpgsign",False):raise StopPush("配置要求签名提交,本程序不调用签名程序")
    if (Path(repo.commondir())/"info"/"grafts").exists() or any(r.startswith(b"refs/replace/") for r in repo.refs.keys()):raise StopPush("存在 grafts/replace refs")
def commit_staged(repo,index,a,identity,expected): # 按最终 Blob 大小分批;LFS 只计指针大小;最后原子更新 HEAD
    chain,head=expected
    if repo.refs.follow(b"HEAD")!=expected:raise StopPush("暂存期间本地 HEAD 已变化")
    flat=tree_map(repo,head);target={p:(e.sha,e.mode) for p,e in index.items()}
    changed=sorted(set(flat)|set(target),key=lambda p:(p in target,p));changed=[p for p in changed if flat.get(p)!=target.get(p)]
    if not changed:LOG.info("暂存区为空,不创建空提交");return [],None
    LOG.info("变更文件: %d 个 | 前 10 项: %s",len(changed),[os.fsdecode(p) for p in changed[:10]]);root=Path(repo.path);largest=None;largest_size=-1;empty=None
    for p in changed:
        fp=root/os.fsdecode(p)
        if fp.is_file() and not fp.is_symlink():
            size=fp.stat().st_size
            if size>largest_size:largest,largest_size=os.fsdecode(p),size
            if p==b"ReadMe.md" and size<1024*1024:
                data=read_regular(fp)
                if b"#EmptyAfterPush" in data:empty=hashlib.sha256(data).hexdigest()
    message=a.message or ((f"[{largest} {largest_size}B] " if largest else "")+f"{stamp()} {Path(__file__).name[-20:]} auto")
    batches=[];current=[];total=0
    for p in changed:
        size=repo.object_store[target[p][0]].raw_length() if p in target and target[p][1]!=0o160000 else 0
        if current and total+size>a.max_commit_size:batches.append(current);current=[];total=0
        current.append(p);total+=size
    if current:batches.append(current)
    now=int(time.time());tz=int(datetime.now().astimezone().utcoffset().total_seconds());ids=[];parent=head
    for number,paths in enumerate(batches,1):
        for p in paths:
            if p in target:flat[p]=target[p]
            else:flat.pop(p,None) # 删除先于新增分组,避免文件/目录互换时构造冲突中间树
        c=Commit();c.tree=commit_tree(repo.object_store,[(p,s,m) for p,(s,m) in flat.items()]);c.parents=[parent] if parent else [];c.author=c.committer=identity;c.author_time=c.commit_time=now;c.author_timezone=c.commit_timezone=tz;c.encoding=b"UTF-8";c.message=((f"[{number}/{len(batches)}] 文件数:{len(paths)} " if len(batches)>1 else "")+message).encode("utf-8")
        c.check();repo.object_store.add_object(c);ids.append(c.id);parent=c.id;LOG.info("准备提交 %d/%d: %s",number,len(batches),c.id.decode())
    if repo.refs.follow(b"HEAD")[0]!=chain:raise StopPush("本地 HEAD 分支在暂存期间改变")
    options={"committer":identity,"timestamp":now,"timezone":tz,"message":b"commit: "+message.encode("utf-8").splitlines()[0]}
    updated=repo.refs.add_if_new(b"HEAD",parent,**options) if head is None else repo.refs.set_if_equals(b"HEAD",head,parent,**options)
    if not updated:raise StopPush("本地 HEAD 被其他进程更新,未覆盖")
    return ids,empty
def prepare(repo,a,identity,cache): # 索引锁覆盖读取/扫描/写入/提交全程;失败只撤销本进程锁,对象保留
    config=repo.get_config_stack();check_repo_state(repo,config);du=shutil.disk_usage(Path(repo.path));LOG.info("磁盘可用空间: %s",human(du.free))
    if du.free<1024*1024:raise StopPush("磁盘剩余空间不足 1MiB;LFS 已走零拷贝流式,请先清理其它文件")
    lock=GitFile(repo.index_path(),"wb") # 尊重已有 index.lock,不猜测过期不删别人的锁
    try:
        expected=repo.refs.follow(b"HEAD");index=repo.open_index();stage_all(repo,index,a,config,cache);writer=SHA1Writer(lock);write_index_dict(writer,dict(index.items()),version=3);ids,empty=commit_staged(repo,index,a,identity,expected);writer.close();return ids,empty
    except BaseException:
        lock.abort();raise
