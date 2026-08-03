
#todo ？ 2026年8月3日 05点57分

✅ stderr 分级日志：正常返回码 stderr 输出 DEBUG，仅非 0 返回码 stderr 标记 ERROR
附带优化：realtime=True 模式增加进程组捕获，中断时完整清理子进程 


默认 git push 不显示连接详情   【除非一开始 verbose debug】
但是如果 push第一次 失败，retry时候 自动调高输出级别 显示详情

commit_msg auto模式下 加入  最大size 修改的文件名。