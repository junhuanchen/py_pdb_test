# Remote PDB WebSocket Demo

这个目录里是一组远程调试 Python 的测试脚本。核心目标是：通过浏览器 HTML 页面，经由 WebSocket 控制一个正在运行的 Python 目标函数，实现暂停、继续、单步、重启、查看局部变量、查看调用栈、特殊断点跳转、高亮、程序输出捕获。

所有核心代码只使用 Python 标准库。

## 文件说明

- `remote_pdb_lib.py`：调试库和 WebSocket 服务。支持 `module:function` 方式加载目标函数。
- `remote_pdb_control.html`：浏览器控制端。
- `remote_pdb_target.py`：简单 while 循环示例。
- `remote_pdb_tcp_server.py`：TCP 服务端 restart/socket 回收测试目标。
- `tcp_time_client.py`：独立 TCP 客户端，循环发送时间 JSON。
- `remote_pdb_exception_target.py`：stdout/stderr/traceback/未捕获异常捕获测试目标。

## HTML 控制端

直接用浏览器打开：

```text
/home/dls/py_test/remote_pdb_control.html
```

默认 WebSocket 地址是：

```text
ws://127.0.0.1:8765
```

如果 `8765` 被占用，可以启动调试服务时改用 `8766`，然后在 HTML 输入框里填：

```text
ws://127.0.0.1:8766
```

HTML 页面支持：

- `Connect` / `Disconnect`
- `Pause`
- `Step`
- `Next`
- `Continue`
- `Restart`
- `Stop`
- 查看当前源码片段
- 查看 locals
- 查看 stack
- 在当前 frame 中 `Eval`
- 特殊断点 `Enable` / `Disable` / `Jump`
- `Program Output` 面板同步显示目标程序 `print()`、`stderr`、`traceback.print_exc()`
- `JSON Debug Events` 面板显示完整结构化事件

## 基础示例

启动简单 while 循环示例：

```bash
python3 remote_pdb_lib.py remote_pdb_target:changing_values 127.0.0.1 8765
```

或如果端口被占用：

```bash
python3 remote_pdb_lib.py remote_pdb_target:changing_values 127.0.0.1 8766
```

打开 HTML 后连接对应 WebSocket 地址，然后可以点：

- `Continue`：循环继续运行。
- `Step`：逐行执行。
- `Restart`：重启目标函数。
- `Jump before-print`：跳到 `checkpoint("before-print", ...)`。

目标脚本中的特殊断点写法：

```python
from remote_pdb_lib import checkpoint

checkpoint("before-print", "message is ready; inspect total/history/message here")
```

不需要装饰器。目标函数可以由库通过 `module:function` 加载。

## TCP Restart Socket 回收测试

这个测试用于确认：点击 `Restart` 时，旧 TCP 服务端创建的 `server_sock` 和 `conn` 是否会进入 `finally` 并关闭，避免旧 socket 占用端口。

启动被调试 TCP 服务端：

```bash
python3 remote_pdb_lib.py remote_pdb_tcp_server:tcp_echo_server 127.0.0.1 8766
```

HTML 连接：

```text
ws://127.0.0.1:8766
```

启动独立 TCP 客户端：

```bash
python3 tcp_time_client.py
```

客户端会连接业务 TCP 服务端：

```text
127.0.0.1:9099
```

测试步骤：

1. HTML 点 `Continue`，让 TCP 服务端开始监听。
2. 客户端会自动连接并每秒发送时间 JSON。
3. HTML 点 `Restart`。
4. 观察服务端终端输出和客户端输出。

期望服务端输出类似：

```text
[tcp-server] closing conn: {'exists': True, 'fd': 9, ...}
[tcp-server] closed conn: fd=-1
[tcp-server] closing server_sock: {'exists': True, 'fd': 8, ...}
[tcp-server] closed server_sock: fd=-1
[tcp-server] cleanup complete run_id=...
[tcp-server] run_id=... listening on 127.0.0.1:9099 ...
```

期望客户端输出表现：

```text
[tcp-client] server closed connection
[tcp-client] connected local=(...) remote=('127.0.0.1', 9099)
[tcp-client] recv {"run_id": 新的 run_id, ...}
```

这说明旧 socket 已关闭，新服务端重新 bind 同一个端口成功。

## Print 和异常输出捕获测试

启动异常捕获测试目标：

```bash
python3 remote_pdb_lib.py remote_pdb_exception_target:exception_demo 127.0.0.1 8766
```

HTML 连接：

```text
ws://127.0.0.1:8766
```

点击 `Continue`。

这个脚本会测试：

- `print(..., flush=True)` 的 stdout 捕获。
- `print(..., file=sys.stderr)` 的 stderr 捕获。
- `traceback.print_exc()` 的多行 traceback 捕获。
- 未捕获 `RuntimeError` 的 `type=exception` 事件。

HTML 的 `Program Output` 面板会看到类似：

```text
[stdout] [exception-demo] stdout before handled exception
[stderr] [exception-demo] stderr before handled exception
[stderr] Traceback (most recent call last):
[stderr] ZeroDivisionError: division by zero
```

`JSON Debug Events` 面板会看到最终异常事件：

```json
{
  "type": "exception",
  "error": "RuntimeError('uncaught test exception for websocket stderr/output capture')"
}
```

## 调试事件结构

常见事件：

```json
{
  "type": "status",
  "target": "tcp_echo_server",
  "reason": "paused",
  "state": "paused",
  "file": "...",
  "line": 58,
  "function": "tcp_echo_server",
  "locals": {},
  "stack": [],
  "source": [],
  "special_breakpoints": []
}
```

程序输出事件：

```json
{
  "type": "output",
  "stream": "stdout",
  "text": "[tcp-server] listening ...",
  "at": 1784736311.0
}
```

异常事件：

```json
{
  "type": "exception",
  "error": "RuntimeError('...')"
}
```

## 注意事项

- WebSocket 调试端口默认是 `8765`，被占用时建议用 `8766`。
- TCP 测试业务端口是 `9099`。
- HTML 断开连接时，调试器会自动进入 pause 状态，重连后会推送当前状态。
- `Restart` 是安全重启：先通知旧 debuggee 退出，等待其执行 `finally`，再启动新线程。
- 如果旧 debuggee 5 秒内无法退出，会发 `restart-blocked`，避免新实例抢占旧资源。
- `print()` 捕获会保留终端原始输出，同时同步给 HTML。
