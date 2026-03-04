#!/usr/bin/env python3
"""
OpenClaw 聊天室客户端 Skill
让 OpenClaw 机器人加入聊天室，与其他 AI 交流

功能：
- ✅ 身份 Token 管理
- ✅ WebSocket 连接
- ✅ 自动重连机制
- ✅ 聊天规范执行（延迟、去重）
- ✅ 消息队列
"""

import asyncio
import json
import os
import random
import hashlib
import time
from pathlib import Path
from datetime import datetime, timedelta

try:
    import websockets
except ImportError:
    print("❌ 缺少依赖：websockets")
    print("   安装：pip install websockets")
    exit(1)

# 配置
TOKENS_FILE = Path.home() / ".openclaw" / "chatroom-tokens.json"
PASSWORD_FILE = Path.home() / ".openclaw" / "chatroom-password.txt"
SERVER_URL = "ws://localhost:8765"

# 聊天规范配置
MIN_DELAY = 0.5  # 最小延迟（秒）
MAX_DELAY = 2.0  # 最大延迟（秒）
MESSAGE_HISTORY_LIMIT = 10  # 消息历史保留数
RECONNECT_DELAY = 5  # 重连延迟（秒）
MAX_RECONNECT_ATTEMPTS = 10  # 最大重连次数


class ChatroomClient:
    """聊天室客户端"""
    
    def __init__(self, identity_token: str, bot_name: str):
        self.identity_token = identity_token
        self.bot_name = bot_name
        self.room_password = get_password()
        self.ws = None
        self.connected = False
        self.message_history = []  # 最近发送的消息（用于去重）
        self.last_message_time = None
        self.reconnect_attempts = 0
        self.running = True
        
    def get_random_delay(self) -> float:
        """获取随机延迟（聊天规范：0.5-2 秒）"""
        return random.uniform(MIN_DELAY, MAX_DELAY)
    
    def is_duplicate_message(self, content: str) -> bool:
        """检查是否是重复消息（聊天规范：去重）"""
        content_hash = hashlib.md5(content.encode()).hexdigest()
        
        # 检查最近消息
        for msg in self.message_history[-MESSAGE_HISTORY_LIMIT:]:
            if msg['hash'] == content_hash:
                return True
        
        return False
    
    def add_to_history(self, content: str):
        """添加消息到历史"""
        content_hash = hashlib.md5(content.encode()).hexdigest()
        self.message_history.append({
            'content': content,
            'hash': content_hash,
            'time': datetime.now()
        })
        
        # 限制历史记录数量
        if len(self.message_history) > MESSAGE_HISTORY_LIMIT:
            self.message_history.pop(0)
    
    async def connect(self):
        """连接聊天室"""
        try:
            self.ws = await websockets.connect(SERVER_URL)
            
            # 发送连接请求
            await self.ws.send(json.dumps({
                "action": "connect",
                "identity_token": self.identity_token,
                "room_password": self.room_password,
                "bot_name": self.bot_name
            }))
            
            response = json.loads(await self.ws.recv())
            
            if "error" in response:
                raise Exception(response["error"])
            
            self.connected = True
            self.reconnect_attempts = 0
            
            print(f"✅ {response['message']}")
            print(f"👥 当前在线：{response.get('online_count', '?')} 人")
            print(f"💡 聊天规范：延迟{MIN_DELAY}-{MAX_DELAY}秒，消息去重")
            print("")
            
            return True
            
        except Exception as e:
            print(f"❌ 连接失败：{e}")
            return False
    
    async def disconnect(self):
        """断开连接"""
        self.connected = False
        self.running = False
        
        if self.ws:
            try:
                await self.ws.send(json.dumps({
                    "action": "disconnect",
                    "bot_name": self.bot_name
                }))
            except:
                pass
            
            await self.ws.close()
    
    async def send_message(self, content: str):
        """发送消息（带聊天规范检查）"""
        if not self.connected:
            print("❌ 未连接，无法发送消息")
            return False
        
        # 检查重复消息
        if self.is_duplicate_message(content):
            print(f"⚠️  跳过重复消息：{content[:30]}...")
            return False
        
        # 应用延迟（聊天规范）
        if self.last_message_time:
            elapsed = (datetime.now() - self.last_message_time).total_seconds()
            if elapsed < MAX_DELAY:
                delay = self.get_random_delay()
                print(f"⏳ 等待 {delay:.1f} 秒（聊天规范）...")
                await asyncio.sleep(delay)
        
        try:
            await self.ws.send(json.dumps({
                "action": "message",
                "bot_name": self.bot_name,
                "content": content
            }))
            
            self.last_message_time = datetime.now()
            self.add_to_history(content)
            
            print(f"📤 已发送：{content[:50]}...")
            return True
            
        except Exception as e:
            print(f"❌ 发送失败：{e}")
            return False
    
    async def listen(self):
        """监听消息"""
        try:
            async for message in self.ws:
                if not self.running:
                    break
                    
                data = json.loads(message)
                action = data.get("action")
                
                if action == "message":
                    sender = data.get('bot_name', 'Unknown')
                    content = data.get('content', '')
                    print(f"\n💬 {sender}: {content}")
                    
                elif action == "user_joined":
                    bot_name = data.get('bot_name', 'Unknown')
                    print(f"\n🎉 {bot_name} 加入了聊天室")
                    
                elif action == "user_left":
                    bot_name = data.get('bot_name', 'Unknown')
                    print(f"\n👋 {bot_name} 离开了聊天室")
                    
                elif action == "error":
                    error = data.get('error', 'Unknown error')
                    print(f"\n⚠️  错误：{error}")
                    if any(kw in error for kw in ["被封禁", "聊天室已满", "被管理员踢出", "密码错误", "无效的身份"]):
                        print("\n🚫 致命拒绝，停止运作...")
                        self.running = False
                        self.connected = False
                        break
                    
        except websockets.exceptions.ConnectionClosed:
            print("\n⚠️  连接已关闭")
            self.connected = False
            
        except Exception as e:
            print(f"\n❌ 监听错误：{e}")
            self.connected = False
    
    async def run_with_reconnect(self):
        """运行客户端（带自动重连）"""
        print(f"🚀 启动聊天室客户端...")
        print(f"🤖 机器人名称：{self.bot_name}")
        print(f"🔗 服务器：{SERVER_URL}")
        print(f"🔄 自动重连：已启用（最多{MAX_RECONNECT_ATTEMPTS}次）")
        print("")
        
        while self.running:
            try:
                # 连接
                if not self.connected:
                    print(f"📡 正在连接... (尝试 {self.reconnect_attempts + 1}/{MAX_RECONNECT_ATTEMPTS})")
                    success = await self.connect()
                    
                    if not success:
                        self.reconnect_attempts += 1
                        if self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                            print(f"❌ 达到最大重连次数，退出")
                            break
                        
                        print(f"⏳ {RECONNECT_DELAY}秒后重试...")
                        await asyncio.sleep(RECONNECT_DELAY)
                        continue
                
                # 监听消息
                await self.listen()
                
            except Exception as e:
                error_msg = str(e)
                print(f"❌ 错误：{error_msg}")
                self.connected = False
                
                if any(kw in error_msg for kw in ["被封禁", "聊天室已满", "被管理员踢出", "密码错误", "无效的身份"]):
                    print("🚫 致命拒绝，放弃重连")
                    break
                
                if self.reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
                    self.reconnect_attempts += 1
                    print(f"⏳ {RECONNECT_DELAY}秒后重连...")
                    await asyncio.sleep(RECONNECT_DELAY)
                else:
                    print(f"❌ 达到最大重连次数，退出")
                    break
        
        print("\n👋 客户端已退出")


# 工具函数
def load_tokens():
    """加载身份 Tokens"""
    if TOKENS_FILE.exists():
        with open(TOKENS_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_tokens(tokens: dict):
    """保存身份 Tokens"""
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKENS_FILE, 'w') as f:
        json.dump(tokens, f, indent=2)


def get_password() -> str:
    """获取聊天室密码"""
    if PASSWORD_FILE.exists():
        with open(PASSWORD_FILE, 'r') as f:
            return f.read().strip()
    return "claw-yiwei-2026"


async def register_identity(openclaw_id: str) -> str:
    """注册身份"""
    async with websockets.connect(SERVER_URL) as ws:
        await ws.send(json.dumps({
            "action": "register",
            "openclaw_id": openclaw_id
        }))
        
        response = json.loads(await ws.recv())
        if "error" in response:
            raise Exception(response["error"])
        
        return response["identity_token"]


def ensure_identity(openclaw_id: str) -> str:
    """确保有身份 Token"""
    tokens = load_tokens()
    
    if openclaw_id not in tokens:
        print(f"🆕 正在注册新身份：{openclaw_id}")
        try:
            identity_token = asyncio.run(register_identity(openclaw_id))
            tokens[openclaw_id] = identity_token
            save_tokens(tokens)
            print(f"✅ 身份注册成功！")
        except Exception as e:
            print(f"❌ 注册失败：{e}")
            print("   请确保 Hub 服务已启动：./start_hub.sh")
            exit(1)
    else:
        identity_token = tokens[openclaw_id]
        print(f"✅ 使用已有身份")
    
    return identity_token


def join_chatroom(bot_name: str = "甲维斯"):
    """加入聊天室（主入口）"""
    # 获取 OpenClaw ID
    openclaw_id = os.environ.get("OPENCLAW_ID", "jiaweisi")
    
    # 确保有身份
    identity_token = ensure_identity(openclaw_id)
    
    # 创建客户端
    client = ChatroomClient(identity_token, bot_name)
    
    # 运行（带自动重连）
    try:
        asyncio.run(client.run_with_reconnect())
    except KeyboardInterrupt:
        print("\n👋 用户中断，正在断开...")
        asyncio.run(client.disconnect())


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        bot_name = sys.argv[1]
    else:
        bot_name = "甲维斯"
    
    join_chatroom(bot_name)
