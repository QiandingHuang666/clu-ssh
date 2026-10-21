# clu 人工功能测试清单（v1）

> 目的：在真实集群/服务器上逐项验证 clu 的全部用户可见功能。
> 用法：按顺序执行，把结果勾在 `[ ]` 里；失败项记录实际输出与时间。
> 约定：`$CL` = 你的 Slurm 集群连接 label（如 `cluster-r`）；`$BOX` = 普通服务器连接 label；
> `$JOB` = 测试作业号；`$PURPOSE` = 测试会话用途名（如 `mt-check`）。

## 0. 准备

- [ ] `clu --version` 输出版本号
- [ ] `clu server ensure` 拉起 daemon；再执行一次显示"已在运行"
- [ ] `clu server status` 显示版本、pid、uptime 与连接池
- [ ] `clu server stop` 后 `clu server status` 提示未运行
- [ ] `ps aux | grep clu.daemon` 确认停止后无残留 daemon 进程（应只有 0~1 个）
- [ ] `clu connection import` 从 `~/.ssh/config` 导入；通配条目被跳过并提示
- [ ] `clu connection add $CL --host ... --user ... --port ... --kind cluster`
- [ ] `clu connection list` 能看到全部连接；daemon 在线时 state 列有值
- [ ] `clu connection check $CL` 返回"在线"与耗时
- [ ] `clu connection check 不存在的label` 报"未知连接"
- [ ] `clu connection remove 测试label` 后列表消失

## 1. 连接池与主机密钥

- [ ] 连续执行两次 `clu exec $CL hostname`，第二次明显更快（连接复用）
- [ ] 断开网络 10 秒后恢复，再执行 `clu exec $CL hostname` 能自动重连成功
- [ ] 首次连接后 `~/.config/clu/known_hosts` 出现 `[host]:port` 条目（非 22 端口带方括号）
- [ ] 手工篡改该条目中的密钥后连接被拒绝（提示 host key not trusted）；恢复条目后正常
- [ ] 带 ProxyJump 的连接（若有）可正常 `clu exec`

## 2. 远程执行与安全门

- [ ] `clu exec $CL "echo hello"` 输出 hello，exit=0，显示耗时
- [ ] `clu exec $CL "sleep 2" --timeout 1` 超时失败并报错
- [ ] `clu exec $CL "exit 3"` 命令退出码为 3（CLI 退出码同步）
- [ ] `clu exec $CL "rm -rf /"` 被拒绝（dangerous）
- [ ] `clu exec $CL "mkfs.ext4 /dev/sda1"` 被拒绝
- [ ] `clu exec $CL "scancel --all"` 被拒绝
- [ ] `clu exec $CL "rm -rf /tmp/clu-test-dir"` 允许执行
- [ ] `clu exec $CL "torchrun train.py"` 在 cluster 连接被拒绝，提示用 submit/alloc
- [ ] `clu exec $CL "pip install requests"` 被拒绝
- [ ] `clu exec $BOX "torchrun --help"` 在普通服务器上允许（无登录节点概念）
- [ ] 给 profile 配置 `heavy_patterns = ["mybench"]` 后 `clu exec $CL mybench` 被拒
- [ ] profile 设 `heavy_guard = false` 后 `clu exec $CL "torchrun --help"` 放行；危险命令仍被拒

## 3. 资源查看

- [ ] `clu resources $CL` 输出分区表（节点数、CPU 空闲/总、GPU 空闲/总）
- [ ] 与 `clu exec $CL "scontrol show nodes -o"` 抽查 1~2 个节点，CPU/GPU 数字一致
- [ ] `clu resources $CL --partition <分区>` 只显示该分区
- [ ] `clu resources $CL --cpu-free` 只列出有空闲 CPU 的节点（`--nodes` 查看明细）
- [ ] `clu resources $CL --cpu-gpu-free --gpu-only` 只列出 CPU 与 GPU 同时空闲的 GPU 节点
- [ ] `clu resources $CL --fully-idle` 只列出 CPU 与 GPU 全空闲节点
- [ ] MCP：在 Claude Code 中让 agent 调 `resources`（同参数），结果与 CLI 一致
- [ ] 大集群下默认输出为紧凑视图（无节点明细）；带过滤自动附节点明细

## 4. 抢占与交互式资源（alloc / attach / session）

- [ ] `clu alloc $CL --purpose $PURPOSE --wait 60`：默认 GPU 优先；无 GPU 空闲时回退 CPU
- [ ] 指定资源：`clu alloc $CL --gpu --gpus 1 --cpus 8 --purpose $PURPOSE`
- [ ] 指定 CPU：`clu alloc $CL --cpu --cpus 4 --purpose $PURPOSE`
- [ ] 显式分区：`clu alloc $CL -p <分区> --purpose $PURPOSE`
- [ ] 默认核数：不传 `--cpus` 时，观察 salloc 命令含 `-c min(总核/卡数, 空闲核)`
- [ ] 同名活会话再次 alloc：返回 `exists`，提示 attach 或先 release（不静默复用）
- [ ] `clu session list` 能看到刚建会话（登记表驱动，秒级返回）
- [ ] `clu session list --deep` 全量扫描也能列出
- [ ] `clu attach $CL $PURPOSE`：进入计算节点 shell，**有提示符**、`hostname` 为计算节点、个人配置（alias/PATH/提示符）生效
- [ ] 在会话中执行 `nvidia-smi`（GPU 作业）能看到分配的卡
- [ ] 会话内 `exit` 后 `clu session list` 不再显示该会话（资源已释放）
- [ ] 重新 alloc 同名会话 → 正常重建（拿到新 job 号）
- [ ] `clu session release $CL $PURPOSE` 立即释放；`--all` 释放该连接全部会话
- [ ] **daemon 重启恢复**：alloc 后 `clu server stop && clu server ensure`，`clu session list` 仍能看到会话与 job/节点
- [ ] 关掉一台不相关连接（或填错 host）后 `clu session list`：该连接标注"不可达"，其余会话照常显示
- [ ] 手工 `tmux kill-session -t clu/$CL/$PURPOSE` 后等待巡检（≤5 分钟）或 MCP `session_info` 刷新，登记表自动销账

## 5. 批处理作业

- [ ] 从文件提交：`clu submit $CL -f script.sh --name mt --partition <分区> -c 1 -t 0:05:00`
- [ ] 从 stdin 提交：`cat script.sh | clu submit $CL --name mt-stdin ...`
- [ ] 从字面量提交：`clu submit $CL --script "#!/bin/bash
hostname" --name mt-lit`
- [ ] 未传 `-t` 时最终脚本中**没有** `#SBATCH -t`
- [ ] 传入的分区/核数/GPU/时限均出现在远端脚本中
- [ ] 用户脚本自带 `#SBATCH -p` / `#SBATCH -o` 时不被覆盖
- [ ] 远端 `~/.clu/jobs/$CL/` 出现脚本与 `-%j.out/-%j.err` 日志文件
- [ ] `clu job list $CL` 显示 job/名称/状态/节点/分区/提交时间
- [ ] `clu job list $CL --active` 只显示非终态
- [ ] `clu jobs $CL` 实时队列与 `squeue -u $USER` 一致；`--states RUNNING` 过滤生效
- [ ] `clu job log $CL $JOB` 读到 stdout/stderr 内容
- [ ] 作业经历 PENDING → RUNNING → COMPLETED，台账状态随之更新（`job list` 观察）
- [ ] 故意提交失败作业（`exit 1`）→ 台账状态 FAILED，exit_code 非 0
- [ ] `scancel` 取消作业 → 台账状态 CANCELLED
- [ ] 不手动刷新，等待 ≥60s 后 `job list` 状态已由 daemon 巡检更新
- [ ] MCP：`submit` / `job_list` / `log` 三个工具与 CLI 行为一致
- [ ] 提交含危险命令的脚本（`rm -rf /`）被拒

## 6. 文件传输（MCP）

- [ ] 让 agent 调 `upload` 上传本地文件到 `~/.clu-test/upload.txt`，远端 `cat` 内容一致
- [ ] 远端父目录不存在时自动创建
- [ ] `download` 下载远端文件到本地，内容一致（含中文/二进制文件各一）
- [ ] 上传不存在的本地文件 → 明确报错
- [ ] 下载不存在的远端文件 → 明确报错

## 7. 端口转发

- [ ] `clu forward add $CL <远端host> 22 --local-port 0` 返回实际端口
- [ ] `nc -w2 127.0.0.1 <端口>` 能读到 SSH banner
- [ ] `clu forward list` 显示该转发
- [ ] `clu forward remove <id>` 后端口不可再连
- [ ] 连接断开后转发失效（已知限制；重新 add 可恢复）

## 8. 留痕与台账

- [ ] MCP `ledger_query` 按连接过滤，返回最近 exec/submit/alloc/release 记录
- [ ] 按 kind 过滤（exec/submit/alloc）结果正确
- [ ] 按关键词过滤能命中命令文本
- [ ] 按 job_id 过滤能命中对应提交记录
- [ ] `sqlite3 ~/.local/share/clu/ledger.sqlite3 "select * from sessions;"` 字段完整
- [ ] `sqlite3 ... "select job_id,name,state,script_path from jobs;"` 脚本内容非空
- [ ] 释放会话/作业完成后有 release 记录；巡检销账有 watchdog 记录

## 9. Claude Code（MCP）集成

- [ ] `claude mcp add --scope user clu -- clu-mcp` 后新会话可见 clu 工具
- [ ] 工具清单共 16 个：exec/jobs/status/resources/submit/log/cancel/ledger_query/connection_list/alloc/session_list/session_info/session_release/upload/download/job_list
- [ ] 只读工具带 read-only hint（可在权限设置中免确认）
- [ ] 自然语言任务："看看 $CL 有什么卡空着" → 一次 `resources` 调用给出正确结果
- [ ] "抢一张卡" → `alloc` 返回 granted，随后可给出 attach 指引
- [ ] "提交这个脚本" → `submit` 完成并返回 job_id 与日志路径
- [ ] "作业 $JOB 什么状态/日志呢" → `job_list` / `log` 正确回答
- [ ] "释放测试会话" → `session_release` 成功
- [ ] 危险命令请求（"删掉根目录"）被安全门拒绝

## 10. 鲁棒性

- [ ] 执行输出超过 64KB 的命令：返回被截断并注明长度
- [ ] 中文/emoji 输出正常显示
- [ ] `clu server stop` 后立即执行任意 CLI 命令：自动拉起 daemon 并继续
- [ ] 并发两个终端同时 `clu exec`：互不干扰
- [ ] daemon 日志 `daemon.log` 中每个 RPC 有 method/label/耗时记录

## 11. 已知限制（预期"不通过"，不作为缺陷）

| 项 | 现状 |
|---|---|
| 密码认证 | 不支持，仅公钥 |
| 多级跳板 | 仅单级 |
| rsync 目录同步 | v1.1 |
| SOCKS/远程转发 | 仅本地 `-L` |
| Windows | 不支持 |
| 指定节点名 | 仅节点数 `-N`，无 `-w` |
| 数组作业 | 不支持 |
| 实时流式日志 | 按需 tail |
| 日志本地副本 | 不保存，按需读取远端 |
| 转发自动重建 | 连接断开后需重新 add |
| 会话日志跨重建 | 日志追加累积（已按最后一次授予解析） |

## 验收记录

| 日期 | 版本 | 通过/总数 | 失败项编号 | 备注 |
|---|---|---|---|---|
| | | | | |
