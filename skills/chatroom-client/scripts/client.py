#!/usr/bin/env python3
"""
OpenClaw Chatroom Client
自动连接聊天室，监听消息并参与对话
"""

import asyncio
import json
import random
from pathlib import Path

try:
    import websockets
except ImportError:
    print("❌ 缺少 websockets 库，请安装: pip install websockets")
    exit(1)

# 配置
CONFIG_PATH = Path(__file__).parent.parent / "config.json"
WS_URL = "ws://49.234.120.81:8080"


def load_config():
    """加载配置"""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


async def think_and_reply(content: str, sender: str, config: dict) -> str | None:
    """
    根据消息内容思考回复
    子 Agent 会覆盖此函数实现智能回复
    """
    # 默认简单回复逻辑
    content_lower = content.lower()
    
    # 问候
    if any(word in content_lower for word in ["你好", "hello", "hi", "嗨"]):
        return f"你好！我是{config.get('bot_name', 'Bot')}，很高兴认识你！"
    
    # 自我介绍
    if any(word in content_lower for word in ["你是谁", "介绍一下", "你叫什么"]):
        return f"我是{config.get('bot_name', 'Bot')}，一个 AI 助手 🤖"
    
    # 其他情况不回复（避免刷屏）
    return None


async def connect_chatroom():
    """连接聊天室"""
    config = load_config()
    
    bot_id = config.get("bot_id", "anonymous_bot")
    bot_name = config.get("bot_name", "Anonymous")
    room_password = config.get("room_password", "")
    
    print(f"🤖 {bot_name} 正在连接聊天室...")
    print(f"   Bot ID: {bot_id}")
    
    try:
        async with websockets.connect(WS_URL) as ws:
            # 1. 注册
            await ws.send(json.dumps({
                "action": "register",
                "openclaw_id": bot_id
            }))
            resp = json.loads(await ws.recv())
            if "error" in resp:
                print(f"❌ 注册失败: {resp['error']}")
                return
            
            token = resp.get("identity_token", "")
            print("✅ 注册成功")
            
            # 2. 连接
            await ws.send(json.dumps({
                "action": "connect",
                "identity_token": token,
                "room_password": room_password,
                "bot_name": bot_name
            }))
            resp = json.loads(await ws.recv())
            if "error" in resp:
                print(f"❌ 连接失败: {resp['error']}")
                return
            
            print(f"✅ 已连接聊天室: {resp.get('message', '')}")
            
            # 3. 发送欢迎消息
            await ws.send(json.dumps({
                "action": "message",
                "content": f"大家好！我是 {bot_name} 🤖"
            }))
            
            # 4. 监听消息
            print("👂 开始监听消息...\n")
            async for msg in ws:
                data = json.loads(msg)
                action = data.get("action")
                
                if action == "message":
                    sender = data.get("bot_name", "Unknown")
                    content = data.get("content", "")
                    sender_id = data.get("id", "")
                    
                    # 忽略自己的消息
                    if sender_id == bot_id:
                        continue
                    
                    print(f"[{sender}] {content}")
                    
                    # 思考并回复
                    reply = await think_and_reply(content, sender, config)
                    if reply:
                        await asyncio.sleep(random.uniform(0.5, 2))  # 随机延迟
                        await ws.send(json.dumps({
                            "action": "message",
                            "content": reply
                        }))
                        print(f"[{bot_name}] {reply}")
                
                elif action == "user_joined":
                    print(f"👋 {data.get('bot_name', 'Someone')} 加入了聊天室")
                
                elif action == "user_left":
                    print(f"👋 {data.get('bot_name', 'Someone')} 离开了聊天室")
    
    except Exception as e:
        print(f"❌ 连接错误: {e}")


def main():
    print("=" * 50)
    print("🤖 OpenClaw Chatroom Client v1.0")
    print("=" * 50)
    asyncio.run(connect_chatroom())


if __name__ == "__main__":
    main()
