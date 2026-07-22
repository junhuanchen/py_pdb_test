#!/usr/bin/env python3
"""
网络鼠标服务端 - 通过 uinput 创建虚拟鼠标设备
生成 /dev/input/event* 节点，接收网络事件并注入系统
"""

import socket
import struct
import threading
import sys
import os
from evdev import UInput, ecodes as e

# 鼠标事件协议格式: 
#   type(1B) + code(1B) + value(4B, signed int)
# type: 0=REL_X, 1=REL_Y, 2=BTN_LEFT, 3=BTN_RIGHT, 4=BTN_MIDDLE, 5=REL_WHEEL
EVENT_FORMAT = '!BBi'
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

# 事件类型映射
EVENT_TYPES = {
    0: (e.EV_REL, e.REL_X),
    1: (e.EV_REL, e.REL_Y),
    2: (e.EV_KEY, e.BTN_LEFT),
    3: (e.EV_KEY, e.BTN_RIGHT),
    4: (e.EV_KEY, e.BTN_MIDDLE),
    5: (e.EV_REL, e.REL_WHEEL),
}


class NetworkMouseServer:
    def __init__(self, host='0.0.0.0', port=5555):
        self.host = host
        self.port = port
        self.running = False
        
        # 创建虚拟鼠标设备（生成 /dev/input/event* 节点）
        self.ui = UInput(
            name='Network Virtual Mouse',
            vendor=0x1234,
            product=0x5678,
            version=0x0100,
            bustype=e.BUS_USB,
            events={
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
                e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
            }
        )
        
        # 获取生成的设备节点路径
        self.device_path = self.ui.device.path
        print(f"[+] 虚拟鼠标已创建: {self.device_path}")
        print(f"[+] 设备名称: {self.ui.name}")
        
        # 查找对应的 event 节点号
        self._find_event_node()

    def _find_event_node(self):
        """查找生成的 /dev/input/eventX 节点"""
        try:
            import glob
            # 通过 sysfs 找到对应的 event 节点
            sysfs_path = self.ui.device.path
            # 通常 path 已经是 /dev/input/eventX
            if 'event' in sysfs_path:
                print(f"[+] evdev 节点: {sysfs_path}")
        except Exception as ex:
            print(f"[!] 无法确定 event 节点: {ex}")

    def handle_client(self, conn, addr):
        """处理单个客户端连接"""
        print(f"[+] 客户端连接: {addr}")
        try:
            while self.running:
                data = conn.recv(EVENT_SIZE)
                if not data:
                    break
                if len(data) != EVENT_SIZE:
                    continue
                
                etype, code, value = struct.unpack(EVENT_FORMAT, data)
                
                if etype in EVENT_TYPES:
                    ev_type, ev_code = EVENT_TYPES[etype]
                    self.ui.write(ev_type, ev_code, value)
                    # 同步事件，确保立即生效
                    if ev_type == e.EV_KEY:
                        self.ui.syn()
                else:
                    print(f"[!] 未知事件类型: {etype}")
                    
        except ConnectionResetError:
            pass
        except Exception as ex:
            print(f"[!] 客户端错误: {ex}")
        finally:
            conn.close()
            print(f"[-] 客户端断开: {addr}")

    def start(self):
        """启动服务端"""
        self.running = True
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(5)
        
        print(f"\n[*] 网络鼠标服务端已启动")
        print(f"[*] 监听地址: {self.host}:{self.port}")
        print(f"[*] 按 Ctrl+C 停止服务\n")
        
        try:
            while self.running:
                conn, addr = sock.accept()
                client_thread = threading.Thread(
                    target=self.handle_client, 
                    args=(conn, addr),
                    daemon=True
                )
                client_thread.start()
        except KeyboardInterrupt:
            print("\n[*] 正在关闭服务...")
        finally:
            self.running = False
            sock.close()
            self.ui.close()
            print("[+] 服务已停止，虚拟鼠标已移除")

    def inject_test(self):
        """测试：本地注入一些鼠标事件"""
        print("\n[*] 运行测试模式...")
        import time
        
        # 向右移动
        for i in range(50):
            self.ui.write(e.EV_REL, e.REL_X, 10)
            self.ui.syn()
            time.sleep(0.01)
        
        # 点击左键
        self.ui.write(e.EV_KEY, e.BTN_LEFT, 1)
        self.ui.syn()
        time.sleep(0.1)
        self.ui.write(e.EV_KEY, e.BTN_LEFT, 0)
        self.ui.syn()
        
        print("[+] 测试完成")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='网络鼠标服务端')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址')
    parser.add_argument('--port', type=int, default=5555, help='监听端口')
    parser.add_argument('--test', action='store_true', help='本地测试模式')
    args = parser.parse_args()
    
    server = NetworkMouseServer(args.host, args.port)
    
    if args.test:
        server.inject_test()
    else:
        server.start()