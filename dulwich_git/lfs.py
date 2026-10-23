from .shared import *
@contextmanager
def regular_reader(path): # 安全读取普通文件,正确处理 Windows NTFS 时间隧道:绝不跨 lstat/fstat 比较 inode/dev/ctime
    p=Path(path)
    try:before=p.lstat()
    except FileNotFoundError:raise
    if not stat.S_ISREG(before.st_mode):raise StopPush(f"不是普通文件,拒绝跟随链接: {p}")
    fd=os.open(p,os.O_RDONLY|getattr(os,"O_BINARY",0));f=os.fdopen(fd,"rb")
    try:
        if file_id(os.fstat(f.fileno()))!=file_id(before):raise StopPush(f"打开瞬间文件已变化: {p}")
        yield f,before
        if file_id(os.fstat(f.fileno()))!=file_id(before):raise StopPush(f"读取期间文件被修改: {p}")
        if file_id(p.lstat())!=file_id(before):raise StopPush(f"读取期间文件被替换: {p}")
    finally:f.close()
def read_regular(path,limit=None):
    with regular_reader(path) as (f,st):
        if limit is not None and st.st_size>limit:raise StopPush(f"文件过大: {path}")
        return f.read()
def config_bytes(path): # 配置文件读取:符号链接一律拒绝;缺失返回空
    p=Path(path)
    if p.is_symlink():return b""
    try:return read_regular(p,8*1024*1024)
    except (FileNotFoundError,NotADirectoryError):return b""
class SafeIgnore(IgnoreFilterManager): # dulwich 1.2.15 语义:True=忽略 False=!重新包含 None=无匹配;_decide 已实现父目录忽略不可再包含
    def _load_path(self,path):
        if (Path(self._top_path)/path/".gitignore").is_symlink():return None # 符号链接 .gitignore 不加载
        return super()._load_path(path)
    def _is_dir(self,path):
        p=Path(self._top_path)/path
        return path.endswith("/") or (p.is_dir() and not p.is_symlink())
def ignore_manager(repo,config): # 优先级低到高:core.excludesfile < info/exclude < 各级 .gitignore(深目录覆盖浅目录)
    ignorecase=config.get_boolean((b"core",),b"ignorecase",False);filters=[]
    user=Path(default_user_ignore_filter_path(config)).expanduser()
    try:filters.append(IgnoreFilter.from_path(user,ignorecase))
    except (FileNotFoundError,NotADirectoryError):pass
    info=Path(repo.controldir())/"info"/"exclude"
    try:filters.append(IgnoreFilter.from_path(info,ignorecase))
    except (FileNotFoundError,NotADirectoryError):pass
    return SafeIgnore(str(repo.path),filters,ignorecase)
def attr_words(line): # 解析 .gitattributes 一行:支持 C 引号路径(dulwich 自带 split 不识别空格引号)
    line=line.strip()
    if not line or line.startswith(b"#"):return None,[]
    if not line.startswith(b'"'):
        words=line.split();return words[0],words[1:]
    out=bytearray();i=1;esc={ord("a"):7,ord("b"):8,ord("t"):9,ord("n"):10,ord("v"):11,ord("f"):12,ord("r"):13,34:34,92:92}
    while i<len(line):
        c=line[i];i+=1
        if c==34:
            if i<len(line) and line[i] not in b" \t":raise StopPush(".gitattributes 引号后缺少空白")
            return bytes(out),line[i:].split()
        if c==92:
            if i>=len(line):break
            c=line[i];i+=1
            if 48<=c<=55:
                number=bytes([c])
                for _ in range(2):
                    if i<len(line) and 48<=line[i]<=55:number+=line[i:i+1];i+=1
                    else:break
                c=int(number,8)
                if c>255:raise StopPush("属性路径八进制转义超出字节范围")
            elif c in esc:c=esc[c]
            else:raise StopPush(".gitattributes 存在不支持的 C 转义")
        out.append(c)
    raise StopPush(".gitattributes 路径引号未闭合")
def parse_attribute(token):
    if token[:1] in (b"-",b"!"):return token[1:],False if token[:1]==b"-" else None
    if b"=" in token:return tuple(token.split(b"=",1))
    return token,True
class Attributes: # 按 全局/根目录/嵌套目录/info 优先级合并,支持取消属性、[attr] 宏与内建 binary 宏
    def __init__(self,repo,config,index):
        self.repo=repo;self.root=Path(repo.path);self.index=index;self.cache={}
        xdg=os.environ.get("XDG_CONFIG_HOME",str(Path.home()/".config"));self.global_path=Path(text(cfg(config,b"core",b"attributesfile",os.fsencode(Path(xdg)/"git"/"attributes")))).expanduser();self.info=Path(repo.controldir())/"info"/"attributes"
    def load(self,path,relative=None,macro_allowed=True):
        key=str(path)
        if key in self.cache:return self.cache[key]
        data=config_bytes(path)
        if not path.exists() and relative is not None and relative in self.index: # 文件已删但已暂存:从索引 Blob 取属性
            entry=self.index[relative]
            if entry.mode in (0o100644,0o100755):data=self.repo.object_store[entry.sha].data
        rules=[];macros={}
        for line in data.splitlines():
            pattern,tokens=attr_words(line)
            if pattern is None:continue
            values=[parse_attribute(t) for t in tokens]
            if pattern.startswith(b"[attr]"):
                if macro_allowed:macros[pattern[6:]]=values
                else:LOG.warning("忽略子目录中不允许定义的属性宏: %s",path)
            elif pattern.startswith(b"!"):raise StopPush(f".gitattributes 不允许负模式: {path}")
            else:
                try:rules.append((AttrPattern(pattern),values))
                except ValueError as exc:raise StopPush(f"属性模式无效: {path}: {exc}") from exc
        self.cache[key]=(rules,macros);return rules,macros
    def get(self,rel):
        parts=rel.split(b"/");levels=[(self.global_path,rel,None,True)] # 实测 git 优先级(低到高):全局 attributesFile < 根 < 更深目录 < info/attributes
        for i in range(len(parts)):
            name=b"/".join(parts[:i]+[b".gitattributes"]);levels.append((self.root/os.fsdecode(name),b"/".join(parts[i:]),name,i==0))
        levels.append((self.info,rel,None,True));loaded=[(self.load(p,key,macros),local) for p,local,key,macros in levels]
        definitions={b"binary":[(b"diff",False),(b"merge",False),(b"text",False)]};result={}
        for (_,macros),_ in loaded:definitions.update(macros)
        def apply(values,seen=frozenset()):
            for name,value in values:
                result[name]=value
                if value is True and name in definitions:
                    if name in seen:raise StopPush("属性宏循环引用: "+text(name))
                    apply(definitions[name],seen|{name})
        for (rules,_),local in loaded:
            for pattern,values in rules:
                if pattern.match(local):apply(values)
        return {k:v for k,v in result.items() if v is not None}
    def invalidate(self):self.cache.clear()
def c_quote(rel): # 把已转义的精确路径做 C 引号包装:可打印 ASCII/UTF-8 原样,引号反斜杠转义,控制字符八进制
    out=bytearray()
    for c in rel:
        if c==34:out+=b'\\"'
        elif c==92:out+=b'\\\\'
        elif 32<=c<127 or c>=128:out.append(c)
        elif c==9:out+=b'\\t'
        elif c==10:out+=b'\\n'
        elif c==13:out+=b'\\r'
        else:out.extend(f'\\{c:03o}'.encode())
    return b'"'+bytes(out)+b'"'
def exact_pattern(rel): # 转义通配魔法符,只精确匹配该完整相对路径;无特殊字符不加引号,有空格/控制符/引号才 C 引号
    esc=b''.join(b'\\'+bytes([c]) if c in b'\\*?[' else bytes([c]) for c in rel)
    return esc if all(33<=c<127 and c!=34 for c in esc) else c_quote(esc)
def pointer_info(data): # 识别标准 LFS 指针,拒绝带扩展或无效的指针再次当普通文件
    if data.startswith(POINTER_PREFIX.rstrip(b"\n")+b"\r\n"):data=data.replace(b"\r\n",b"\n")
    if not data.startswith(POINTER_PREFIX):return None
    m=re.fullmatch(rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize (0|[1-9][0-9]*)\n?",data)
    if not m:raise StopPush("发现无效或带扩展的 LFS 指针;拒绝将指针再次作为普通文件上传")
    return m[1].decode(),int(m[2])
def pointer_bytes(oid,size):return POINTER_PREFIX+f"oid sha256:{oid}\nsize {size}\n".encode()
class LFSCache:
    def __init__(self,repo,config=None):
        storage=cfg(config or repo.get_config_stack(),b"lfs",b"storage");common=Path(repo.commondir()).resolve();base=Path(text(storage)).expanduser() if storage else common/"lfs"
        if not base.is_absolute():base=common/base
        self.base=base;self.verified={};self.sources={} # sources:(oid,size)->(工作区路径, file_id),零拷贝流式源,避免整文件复制撑爆磁盘
        self.repo=repo#为了 lfs-cache
    def path(self,oid):
        if not re.fullmatch(r"[a-f0-9]{64}",oid):raise StopPush("无效的 LFS SHA256")
        return self.base/"objects"/oid[:2]/oid[2:4]/oid
            
    def _load_hash_cache(self):
        """只读 .git/config 里的缓存注释行;文件缺失或解析失败都返回空 dict,绝不抛异常。
        返回 {(relpath, size, mtime_ns): sha256}。"""
        CACHE_PREFIX = "#lfs-cache\t"
        cache = {}
        cache_file="qgb-lfs-cache.txt"
        cfg_path = Path(self.repo.controldir()) / cache_file
        try:
            data = cfg_path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            return cache
        for line in data.splitlines():
            if not line.startswith(CACHE_PREFIX):
                continue
            parts = line[len(CACHE_PREFIX):].split("\t", 3)
            if len(parts) != 4 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                continue
            try:
                size = int(parts[1]); mtime_ns = int(parts[2])
            except ValueError:
                continue
            cache[(parts[3], size, mtime_ns)] = parts[0]
        return cache

    def hash_source(self, path):
        """只流式计算 oid/size,不落盘;读取期间文件变化由 regular_reader 直接报错。
        若 cache_file 里存在同仓库相对路径 + 同 size + 同 mtime_ns 的缓存注释,直接返回其 sha256,跳过磁盘读取。
        缓存只读不写;任何一步不满足(文件不存在、不是普通文件、路径不在仓库内、元数据对不上)都回退到原逻辑。"""
        p = Path(path)
        # 尝试命中缓存:失败一律静默回退,不影响原逻辑
        try:
            probe = p.lstat()
        except OSError:
            probe = None
        if probe is not None and stat.S_ISREG(probe.st_mode):
            cached = getattr(self, "_hash_cache", None)
            if cached is None:
                cached = self._load_hash_cache()
                self._hash_cache = cached
            repo_root = Path(self.repo.path).resolve()
            try:
                rel = p.resolve().relative_to(repo_root).as_posix()
            except (ValueError, OSError):
                rel = None
            if rel is not None:
                hit = cached.get((rel, probe.st_size, probe.st_mtime_ns))
                if hit is not None:
                    LOG.info("lfs-cache 指纹命中 缓存,跳过磁盘读取: %s | %s", path, human(probe.st_size))
                    return hit, probe.st_size
        # 原逻辑:流式读取,边读边算
        h = hashlib.sha256(); size = 0; last = time.monotonic()
        with regular_reader(path) as (f, st):
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block); size += len(block)
                if time.monotonic() - last >= 1:
                    LOG.info("LFS 指纹计算: %s | %s/%s", path, human(size), human(st.st_size))
                    last = time.monotonic()
        return h.hexdigest(), size        
    # def hash_source(self,path): # 只流式计算 oid/size,不落盘;读取期间文件变化由 regular_reader 直接报错
        # h=hashlib.sha256();size=0;last=time.monotonic()
        # with regular_reader(path) as (f,st):
            # for block in iter(lambda:f.read(1024*1024),b""):
                # h.update(block);size+=len(block)
                # if time.monotonic()-last>=1:LOG.info("LFS 指纹计算: %s | %s/%s",path,human(size),human(st.st_size));last=time.monotonic()
        # return h.hexdigest(),size
    def stage_source(self,path): # 登记工作区源并返回指针;零额外磁盘占用
        oid,size=self.hash_source(path);p=Path(path);self.sources[(oid,size)]=(p,file_id(p.lstat()));return pointer_bytes(oid,size)
    def import_object(self,source): # 仅手工指针场景显式导入对象,复制进缓存并校验
        oid,size=self.hash_source(source);dest=self.path(oid);dest.parent.mkdir(parents=True,exist_ok=True)
        if not (dest.is_file() and not dest.is_symlink() and dest.stat().st_size==size):shutil.copyfile(source,dest)
        h=hashlib.sha256();total=0
        with regular_reader(dest) as (f,_):
            for block in iter(lambda:f.read(1024*1024),b""):h.update(block);total+=len(block)
        if total!=size or h.hexdigest()!=oid:raise StopPush(f"LFS 导入损坏: {oid}")
        self.verified[(oid,size)]=file_id(dest.stat());return oid,size
    def has_object(self,oid,size):
        p=self.path(oid)
        try:return p.is_file() and not p.is_symlink() and p.stat().st_size==size
        except OSError:return False
    def require(self,oid,size): # 缓存对象完整校验(oid/size/内容)
        p=self.path(oid)
        if not p.is_file() or p.is_symlink():raise StopPush(f"远端需要 LFS {oid[:16]},但缓存不存在;不能只发指针")
        st=p.stat()
        if self.verified.get((oid,size))!=file_id(st) or st.st_size!=size:
            h=hashlib.sha256();total=0
            with regular_reader(p) as (f,_):
                for block in iter(lambda:f.read(1024*1024),b""):h.update(block);total+=len(block)
            if total!=size or h.hexdigest()!=oid:raise StopPush(f"LFS 缓存损坏: {oid}")
            self.verified[(oid,size)]=file_id(p.stat())
        return p
    def resolve(self,oid,size): # 上传取真实字节:缓存优先,其次工作区零拷贝源(变化即拒)
        p=self.path(oid)
        if p.is_file() and not p.is_symlink():return self.require(oid,size)
        src=self.sources.get((oid,size))
        if src is not None:
            sp,fid=src
            if not sp.is_file() or sp.is_symlink() or file_id(sp.lstat())!=fid:raise StopPush(f"LFS 工作区源已变化或不存在: {sp};请修复后重新运行")
            return sp
        raise StopPush(f"远端需要 LFS {oid[:16]},但缓存与工作区均无内容;不能只发指针")
def walk_candidates(repo,index,manager,config): # 已跟踪路径绕过忽略;父目录被忽略也不能漏掉已跟踪后代的修改/删除
    root=Path(repo.path);tracked=dict(index.items());ignorecase=config.get_boolean((b"core",),b"ignorecase",False);lookup={os.fsdecode(k).casefold():k for k in tracked} if ignorecase else {};files={};skipped=0;count=0;last=time.monotonic();validator=get_path_element_validator(config)
    def folded(rel):return os.fsdecode(rel).casefold() if ignorecase else os.fsdecode(rel)
    def old_key(rel):return rel if rel in tracked else lookup.get(os.fsdecode(rel).casefold())
    def add(path):
        nonlocal skipped,count,last
        rel=os.fsencode(path.relative_to(root).as_posix());old=old_key(rel);count+=1
        if not validate_path(rel,validator):raise StopPush(f"无效 Git 路径: {path}")
        if old is None and manager.is_ignored(os.fsdecode(rel)) is True:skipped+=1;return
        files[rel]=(path,old)
        if time.monotonic()-last>=1:LOG.info("扫描文件: %d | 排除: %d | 当前: %s",count,skipped,os.fsdecode(rel));last=time.monotonic()
    parents={folded(b"/".join(p.split(b"/")[:i])) for p in tracked for i in range(1,len(p.split(b"/")))}
    for base,dirs,names in os.walk(root,topdown=True,followlinks=False):
        current=Path(base);keep=[]
        for name in dirs:
            path=current/name;rel=os.fsencode(path.relative_to(root).as_posix())
            if (name.casefold() if os.name=="nt" else name)==".git":continue
            if path.is_symlink():add(path);continue # 符号链接(含 Windows junction)只登记链接本身
            if folded(rel) not in parents and old_key(rel) is None and manager.is_ignored(os.fsdecode(rel)) is True:skipped+=1;continue # 只剪枝"目录名本身被忽略"(如 blocked/);select/* 不命名 select,必须下去找 !select/keep.txt
            if (path/".git").exists() or (old_key(rel) is not None and tracked[old_key(rel)].mode==0o160000):add(path);continue # 子模块登记 gitlink,不进入
            keep.append(name)
        dirs[:]=keep
        for name in names:
            if (name.casefold() if os.name=="nt" else name)!=".git":add(current/name)
    LOG.info("扫描完成: %d 项 | 未跟踪且被忽略: %d 项",count,skipped);return files
def normalize_blob(data,attrs,old,repo,config,path,renormalize=False): # 内建 text/eol/autocrlf/ident;未知 filter/encoding 明确报错
    if attrs.get(b"working-tree-encoding") not in (None,False):raise StopPush(f"暂不支持 working-tree-encoding: {os.fsdecode(path)}")
    if attrs.get(b"filter") not in (None,False,b"lfs"):raise StopPush(f"不执行外部 filter={text(attrs.get(b'filter'))}: {os.fsdecode(path)}")
    if attrs.get(b"ident") is True:data=re.sub(rb"\$Id:[^$\r\n]*\$",b"$Id$",data)
    text_attr=attrs.get(b"text");eol=attrs.get(b"eol");auto=cfg(config,b"core",b"autocrlf",b"false").lower()
    if text_attr is None and b"crlf" in attrs:text_attr=False if attrs[b"crlf"] is False else True
    if text_attr is False or (text_attr is None and eol is None and auto not in (b"true",b"input")):return data
    automatic=text_attr!=True and not (text_attr is None and eol in (b"lf",b"crlf"));nonprint=sum(data.count(bytes([c])) for c in range(32) if c not in (8,9,10,12,13,27))+data.count(b"\x7f")
    if automatic and (b"\0" in data or data.count(b"\r")!=data.count(b"\r\n") or nonprint>(len(data)-nonprint)//128):return data
    if automatic and old is not None and not renormalize and old.mode!=0o160000:
        if b"\r\n" in repo.object_store[old.sha].data:return data
    converted=data.replace(b"\r\n",b"\n");safe=cfg(config,b"core",b"safecrlf",b"false").lower();core_eol=cfg(config,b"core",b"eol",b"native")
    checkout_crlf=eol==b"crlf" or (eol is None and (auto==b"true" or (auto!=b"input" and (core_eol==b"crlf" or (core_eol==b"native" and os.name=="nt")))))
    restored=converted.replace(b"\n",b"\r\n") if checkout_crlf else converted
    if safe in (b"true",b"warn") and restored!=data:
        if safe=="true":raise StopPush(f"core.safecrlf 拒绝不可逆换行转换: {os.fsdecode(path)}")
        LOG.warning("换行转换不可逆: %s",os.fsdecode(path))
    return converted
def rebuild_attributes(repo,a,manager,old_entries,large): # LFS 规则严格按当前阈值重建:全部旧 filter=lfs 行(含 *.pdf 通配/过期行)清除,非 LFS 行原样保留
    target=Path(repo.path)/".gitattributes"
    if target.is_symlink():raise StopPush(".gitattributes 不能是符号链接")
    if large and os.fsencode(".gitattributes") not in old_entries and manager.is_ignored(".gitattributes") is True:raise StopPush("自动 LFS 需要 .gitattributes,但它被忽略;请调整忽略规则")
    original=config_bytes(target);kept=[]
    for line in original.splitlines():
        s=line.strip();first=s.split(None,1)[0] if s else b""
        if s and not (b"filter=lfs" in s and not first.startswith(b"[attr]")):kept.append(s) # 只删 LFS 追踪行;宏定义 [attr]x filter=lfs 与非 LFS 行保留
    rules=sorted({exact_pattern(rel) for rel in large})
    new=b"\n".join(kept+[r+b" filter=lfs diff=lfs merge=lfs -text" for r in rules]);new=new+b"\n" if new else b""
    changed=new!=original
    if changed:atomic_write(target,new);LOG.info(".gitattributes 按阈值重建: LFS 精确规则 %d 条(已清除通配/过期规则)",len(rules))
    return changed,target
def stage_all(repo,index,a,config,cache): # 直接写 Blob 与索引项,替代 porcelain.add,永不触发 filter.lfs 外部进程;LFS 身份只由阈值决定
    if any(not isinstance(e,IndexEntry) for _,e in index.items()):raise StopPush("索引存在未解决冲突,拒绝自动提交")
    if any(stat.S_ISDIR(e.mode) or (e.flags&0x4000) for _,e in index.items()):raise StopPush("稀疏索引/skip-worktree 不受支持,请先展开")
    manager=ignore_manager(repo,config);files=walk_candidates(repo,index,manager,config);attrs=Attributes(repo,config,index);old_entries=dict(index.items());root=Path(repo.path);large={}
    for rel,(path,oldkey) in files.items():
        st=path.lstat()
        if oldkey is not None and (old_entries[oldkey].flags&0x8000 or old_entries[oldkey].mode==0o120000):continue
        if not stat.S_ISREG(st.st_mode) or a.no_auto_lfs or path.name in (".gitattributes",".gitignore",".gitmodules",".lfsconfig"):continue
        if st.st_size<=1024 and pointer_info(read_regular(path)):continue # 已是指针文件,不重复判定
        if st.st_size>=a.size:large[rel]=path # 阈值是 LFS 的唯一依据;旧 .gitattributes 说了不算
    changed,target=rebuild_attributes(repo,a,manager,old_entries,large);key=os.fsencode(".gitattributes")
    if changed or key in files:files[key]=(target,key if key in old_entries else None)
    attrs.invalidate();seen=set();payloads=0
    for p,e in old_entries.items():
        if e.mode==0o160000 and not (root/os.fsdecode(p)).exists():seen.add(p);LOG.warning("子模块未检出或工作树缺失,保留 gitlink: %s",os.fsdecode(p))
    for rel,(path,oldkey) in sorted(files.items()):
        old=old_entries.get(oldkey);st=path.lstat();mode=0o100644
        if old is not None and old.flags&0x8000:seen.add(oldkey);continue
        if stat.S_ISDIR(st.st_mode):
            if (path/".git").exists():
                with Repo(str(path)) as sub:
                    try:oid=sub.head()
                    except KeyError:
                        if old is not None:raise StopPush(f"已跟踪子模块没有有效 HEAD: {path}")
                        LOG.warning("跳过未提交的嵌套仓库: %s",path);continue
                if old is None:LOG.warning("仅记录嵌套仓库 gitlink,不上传其内部文件: %s",path)
                entry=index_entry_from_stat(st,oid,mode=0o160000)
            elif old is not None and old.mode==0o160000:seen.add(oldkey);continue
            else:continue
        else:
            if stat.S_ISLNK(st.st_mode):data=os.fsencode(os.readlink(path));mode=0o120000
            elif stat.S_ISREG(st.st_mode):
                effective=attrs.get(rel)
                if effective.get(b"working-tree-encoding") not in (None,False):raise StopPush(f"不支持 working-tree-encoding: {os.fsdecode(rel)}")
                fmode=config.get_boolean((b"core",),b"filemode",os.name!="nt");executable=(fmode and st.st_mode&0o111) or (not fmode and old is not None and old.mode==0o100755);mode=0o100755 if executable else 0o100644
                if rel in large:
                    data=cache.stage_source(path);payloads+=1 # 零拷贝流式:登记工作区源,不复制
                else:
                    filt=effective.get(b"filter")
                    if filt not in (None,False,b"lfs"):raise StopPush(f"不调用外部 filter={text(filt)}: {os.fsdecode(rel)}") # 旧通配规则里的 filter=lfs 对小文件视为无过滤器
                    if st.st_size>=a.max_blob_size:raise StopPush(f"普通 Blob 超过允许大小: {os.fsdecode(rel)}")
                    data=normalize_blob(read_regular(path),effective,old,repo,config,rel,a.renormalize)
                    pi=pointer_info(data) # 手工指针:缓存必须证明拥有对象,否则拒绝只发指针
                    if pi is not None and not cache.has_object(*pi):raise StopPush(f"工作区是 LFS 指针但缓存无对象: {os.fsdecode(rel)};请恢复对象或删除指针")
                if file_id(path.lstat())!=file_id(st):raise StopPush(f"暂存期间文件被修改: {path}")
                blob=Blob.from_string(data);repo.object_store.add_object(blob);entry=index_entry_from_stat(st,blob.id,mode=mode)
                if rel in large and pointer_info(data) is None:raise StopPush(f"LFS 暂存验证失败: {path}")
            else:raise StopPush(f"不支持的文件类型: {path}")
        if oldkey is not None and oldkey!=rel:del index[oldkey]
        index[rel]=entry;seen.add(rel)
    for rel in list(index):
        if rel not in seen:del index[rel]
    LOG.info("索引暂存完成 | LFS 新指纹: %d | 零拷贝 | 未调用任何外部过滤器",payloads)
def atomic_write(path,data): # 同目录临时文件原子替换,失败不留半写文件,不覆盖符号链接
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise StopPush(f"拒绝覆盖符号链接: {path}")
    mode=stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644;fd,tmp=tempfile.mkstemp(prefix=".purepush-",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,mode);os.replace(tmp,path)
    finally:
        with suppress(FileNotFoundError):os.unlink(tmp)
