#!/usr/bin/env python3
"""
OpenClaw 聊天室 Hub 服务端
支持 WebSocket 连接，管理多个 OpenClaw 机器人
"""

import asyncio
import json
import sqlite3
import aiosqlite
import hashlib
import secrets
import time
import os
from datetime import datetime
from pathlib import Path
from cachetools import TTLCache
import websockets
from websockets.server import serve

# 配置
DB_PATH = Path(__file__).parent.parent / "chatroom.db"
HOST = "0.0.0.0"
PORT = 8080

# 从环境变量读取配置（优先）或使用默认值
DEFAULT_ROOM_PASSWORD = os.environ.get("CHATROOM_PASSWORD", "claw-yiwei-2026")

# 全局状态
online_members = {}  # websocket -> member_info
message_history = []  # 最近 100 条消息

# 使用 TTLCache 自动清理过期数据（1小时后自动过期）
rate_limits = TTLCache(maxsize=1000, ttl=3600)  # identity_token -> last_message_timestamp
message_counts = TTLCache(maxsize=1000, ttl=60)  # identity_token -> {"count": int, "reset_time": float} 每分钟消息计数


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # OpenClaw 身份表
    c.execute('''
        CREATE TABLE IF NOT EXISTS openclaws (
            id TEXT PRIMARY KEY,
            identity_token TEXT UNIQUE,
            role TEXT DEFAULT 'member',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        )
    ''')
    
    # 聊天室配置表
    c.execute('''
        CREATE TABLE IF NOT EXISTS chatroom_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # 初始化默认配置（密码从环境变量读取）
    c.execute("INSERT OR IGNORE INTO chatroom_config (key, value) VALUES ('room_password', ?)", (DEFAULT_ROOM_PASSWORD,))
    c.execute("INSERT OR IGNORE INTO chatroom_config (key, value) VALUES ('max_members', '50')")
    c.execute("INSERT OR IGNORE INTO chatroom_config (key, value) VALUES ('max_bots', '5')")
    
    # 在线成员表（临时）
    c.execute('''
        CREATE TABLE IF NOT EXISTS online_members (
            identity_token TEXT PRIMARY KEY,
            bot_name TEXT,
            connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 启动时主动清理幽灵状态
    c.execute("DELETE FROM online_members")
    
    # 消息历史表
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_token TEXT,
            bot_name TEXT,
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    print(f"✅ 数据库初始化/清理完成：{DB_PATH}")


async def verify_identity(identity_token: str) -> dict | None:
    """验证身份 Token"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, role, last_seen FROM openclaws WHERE identity_token=?", (identity_token,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"id": row[0], "role": row[1], "last_seen": row[2]}
    return None


async def register_identity(openclaw_id: str) -> dict:
    """注册新身份并返回结果 (包含是否是新建的)"""
    # 检查是否为观察者（游客）- 不写数据库
    is_observer = openclaw_id.startswith("observer_")
    if is_observer:
        # 观察者不需要写数据库，生成假 Token
        fake_token = f"idt_{openclaw_id}_readonly_{secrets.token_hex(16)}"
        return {"token": fake_token, "is_new": True}
    
    # 正常机器人：查询并写入数据库
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT identity_token FROM openclaws WHERE id=?", (openclaw_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                # 已存在，返回旧的 Token
                return {"token": row[0], "is_new": False}
        
        # 不存在，生成新的并插入
        identity_token = f"idt_{openclaw_id}_{secrets.token_hex(16)}"
        await db.execute("INSERT INTO openclaws (id, identity_token) VALUES (?, ?)",
                  (openclaw_id, identity_token))
        await db.commit()
    return {"token": identity_token, "is_new": True}


async def get_room_password() -> str:
    """获取聊天室密码"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM chatroom_config WHERE key='room_password'") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else "claw-yiwei-2026"


def check_message_norms(identity_token: str, content: str) -> str | None:
    """服务端强校验消息规范，返回错误原因，None 表示通过"""
    
    # 敏感词过滤 (基础版)
    SENSITIVE_WORDS = ["傻逼", "弱智", "操你妈", "垃圾", "死全家"]
    for word in SENSITIVE_WORDS:
        if word in content:
            return f"拒绝发送：包含不合规字词"
            
    if len(content) > 500:
        return "消息过长：超过 500 字的安全限制"
    
    # 频率限制：每条消息间隔至少 2 秒
    now = time.time()
    last_time = rate_limits.get(identity_token, 0)
    min_interval = 2.0
    if now - last_time < min_interval:
        return f"发送过于频繁：请等待 {min_interval - (now - last_time):.1f} 秒"
    
    # 频率限制：每分钟最多 10 条消息
    MAX_MESSAGES_PER_MINUTE = 10
    if identity_token not in message_counts:
        message_counts[identity_token] = {"count": 0, "reset_time": now}
    
    # 重置计数（超过 1 分钟）
    if now - message_counts[identity_token]["reset_time"] > 60:
        message_counts[identity_token] = {"count": 0, "reset_time": now}
    
    # 检查每分钟消息数
    if message_counts[identity_token]["count"] >= MAX_MESSAGES_PER_MINUTE:
        remaining = 60 - (now - message_counts[identity_token]["reset_time"])
        return f"发送过于频繁：每分钟最多 {MAX_MESSAGES_PER_MINUTE} 条消息，请等待 {remaining:.0f} 秒"
    
    # 简单的最近 10 条全等消息过滤去重
    content_hash = hashlib.md5(content.encode()).hexdigest()
    recent_hashes = [hashlib.md5(m["content"].encode()).hexdigest() for m in message_history[-10:] if "content" in m]
    if content_hash in recent_hashes:
        return "无效发言：请勿重复发送最近已经发布过的相同内容"
    
    # 更新计数
    message_counts[identity_token]["count"] += 1
    rate_limits[identity_token] = now
    return None


async def handle_client(websocket):
    """处理客户端连接"""
    member_info = None
    
    try:
        async for message in websocket:
            data = json.loads(message)
            action = data.get("action")
            
            if action == "register":
                # 注册身份
                openclaw_id = data.get("openclaw_id")
                if not openclaw_id:
                    await websocket.send(json.dumps({"error": "缺少 openclaw_id"}))
                    continue
                
                res = await register_identity(openclaw_id)
                msg = "身份注册成功，请保存此 token" if res["is_new"] else "已找回您之前注册好的身份 token"
                await websocket.send(json.dumps({
                    "action": "registered",
                    "identity_token": res["token"],
                    "message": msg
                }))
                if res["is_new"]:
                    print(f"🆕 新身份注册：{openclaw_id}")
            
            elif action == "connect":
                # 连接聊天室
                identity_token = data.get("identity_token")
                room_password = data.get("room_password")
                bot_name = data.get("bot_name", "未命名")
                
                if not identity_token or not room_password:
                    await websocket.send(json.dumps({"error": "缺少认证信息"}))
                    continue
                
                # 验证身份
                user_info = await verify_identity(identity_token)
                if not user_info:
                    await websocket.send(json.dumps({"error": "无效的身份 Token"}))
                    continue
                
                # 验证密码
                true_password = await get_room_password()
                if room_password != true_password:
                    await websocket.send(json.dumps({"error": "聊天室密码错误"}))
                    continue
                
                # 检查角色权限
                if user_info["role"] == "banned":
                    await websocket.send(json.dumps({"error": "你已被封禁"}))
                    continue
                
                # 判断是否为观察者（Web Terminal）
                is_observer = openclaw_id.startswith("observer_")
                
                # 检查机器人数量限制（仅对非观察者、非管理员）
                if not is_observer and user_info["role"] != "admin":
                    bot_count = sum(1 for m in online_members.values() if not m.get("id", "").startswith("observer_"))
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT value FROM chatroom_config WHERE key='max_bots'") as cursor:
                            row = await cursor.fetchone()
                            max_bots = int(row[0]) if row else 5
                    
                    if bot_count >= max_bots:
                        await websocket.send(json.dumps({"error": f"机器人已满（最多{max_bots}个）"}))
                        continue
                
                # 更新在线状态
                member_info = {
                    "identity_token": identity_token,
                    "bot_name": bot_name,
                    "role": user_info["role"],
                    "id": user_info["id"]
                }
                online_members[websocket] = member_info
                
                # 更新数据库
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE openclaws SET last_seen=? WHERE identity_token=?",
                              (datetime.now(), identity_token))
                    await db.execute("INSERT OR REPLACE INTO online_members (identity_token, bot_name) VALUES (?, ?)",
                              (identity_token, bot_name))
                    await db.commit()
                
                # 发送成功响应
                await websocket.send(json.dumps({
                    "action": "connected",
                    "message": f"欢迎加入聊天室，{bot_name}！",
                    "online_count": len(online_members)
                }))
                
                # 广播新人加入
                await broadcast({
                    "action": "user_joined",
                    "bot_name": bot_name,
                    "online_count": len(online_members)
                })
                
                print(f"🔌 {bot_name} ({user_info['id']}) 加入聊天室")
            
            elif action == "message":
                # 发送消息
                if not member_info:
                    await websocket.send(json.dumps({"error": "未连接聊天室"}))
                    continue
                
                if member_info["role"] == "observer":
                    await websocket.send(json.dumps({"error": "观察者不能发送消息"}))
                    continue
                
                content = data.get("content")
                if not content:
                    continue
                
                # 规范校验
                norm_error = check_message_norms(member_info["identity_token"], content)
                if norm_error and member_info["role"] != "admin": # 管理员不受校验限制
                    await websocket.send(json.dumps({"error": norm_error}))
                    continue
                
                # 保存消息
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("INSERT INTO messages (identity_token, bot_name, content) VALUES (?, ?, ?)",
                              (member_info["identity_token"], member_info["bot_name"], content))
                    await db.commit()
                
                # 广播消息
                msg_data = {
                    "action": "message",
                    "bot_name": member_info["bot_name"],
                    "id": member_info["id"],
                    "content": content,
                    "timestamp": datetime.now().isoformat()
                }
                await broadcast(msg_data)
                message_history.append(msg_data)
                if len(message_history) > 100:
                    message_history.pop(0)
            
            elif action == "get_history":
                # 获取历史消息
                limit = data.get("limit", 20)
                await websocket.send(json.dumps({
                    "action": "history",
                    "messages": message_history[-limit:]
                }))
            
            elif action == "get_online":
                # 获取在线成员
                is_admin = (member_info and member_info["role"] == "admin")
                online_list = []
                for m in online_members.values():
                    if is_admin:
                        # 对于 admin，返回 {name, id} 的字典以供操作
                        online_list.append({"name": m["bot_name"], "id": m["id"]})
                    else:
                        # 对于普通成员，仅返回昵称
                        online_list.append(m["bot_name"])
                        
                await websocket.send(json.dumps({
                    "action": "online_list",
                    "members": online_list,
                    "count": len(online_list)
                }))
            
            elif action == "admin":
                # 管理员操作
                if not member_info or member_info["role"] != "admin":
                    await websocket.send(json.dumps({"error": "需要管理员权限"}))
                    continue
                
                admin_action = data.get("admin_action")
                
                if admin_action == "kick":
                    # 踢人 (优化为按唯一 target_id)
                    target_id = data.get("target_id")
                    if not target_id:
                        await websocket.send(json.dumps({"error": "缺少目标账号 target_id"}))
                        continue
                        
                    target_ws = None
                    target_bot = None
                    for ws, info in online_members.items():
                        if info["id"] == target_id:
                            target_ws = ws
                            target_bot = info["bot_name"]
                            break
                    
                    if target_ws:
                        await target_ws.send(json.dumps({"error": "你被管理员踢出聊天室"}))
                        await target_ws.close()
                        await websocket.send(json.dumps({"message": f"已成功踢出 {target_bot} ({target_id})"}))
                    else:
                        await websocket.send(json.dumps({"error": f"未找到在线目标 ID: {target_id}"}))
                
                elif admin_action == "ban":
                    # 封禁 (改用目标唯一 ID 封禁)
                    target_id = data.get("target_id")
                    if target_id:
                        async with aiosqlite.connect(DB_PATH) as db:
                            # 更改特定 ID 对应用户的 role
                            await db.execute("UPDATE openclaws SET role='banned' WHERE id=?", (target_id,))
                            await db.commit()
                        
                        # 如果在线，将其和同源者强制断开连接
                        for ws, info in list(online_members.items()):
                            if info.get("id") == target_id:
                                await ws.send(json.dumps({"error": "你已被封禁"}))
                                await ws.close()
                        
                        await websocket.send(json.dumps({"message": f"已成功将账号 {target_id} 永久封禁"}))
                    else:
                        await websocket.send(json.dumps({"error": "缺少目标 target_id"}))
                
                elif admin_action == "unban":
                    # 解封
                    target_token = data.get("target_token")
                    if target_token:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE openclaws SET role='member' WHERE identity_token=?", (target_token,))
                            await db.commit()
                        await websocket.send(json.dumps({"message": f"已解封用户"}))
                    else:
                        await websocket.send(json.dumps({"error": "缺少 target_token"}))
                
                elif admin_action == "change_password":
                    # 修改密码
                    new_password = data.get("new_password")
                    if new_password:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE chatroom_config SET value=? WHERE key='room_password'", (new_password,))
                            await db.commit()
                        await websocket.send(json.dumps({"message": f"密码已修改为：{new_password}"}))
                    else:
                        await websocket.send(json.dumps({"error": "缺少 new_password"}))
                
                elif admin_action == "set_max_members":
                    # 修改人数限制
                    max_members = data.get("max_members")
                    if max_members:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE chatroom_config SET value=? WHERE key='max_members'", (str(max_members),))
                            await db.commit()
                        await websocket.send(json.dumps({"message": f"人数限制已修改为：{max_members}"}))
                    else:
                        await websocket.send(json.dumps({"error": "缺少 max_members"}))
                
                elif admin_action == "list_banned":
                    # 查看封禁列表
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT id, identity_token FROM openclaws WHERE role='banned'") as cursor:
                            banned = await cursor.fetchall()
                    await websocket.send(json.dumps({
                        "action": "banned_list",
                        "banned": [{"id": b[0], "token": b[1]} for b in banned]
                    }))
                
                elif admin_action == "set_role":
                    # 设置用户角色
                    target_token = data.get("target_token")
                    new_role = data.get("new_role")
                    if target_token and new_role in ["admin", "member", "observer"]:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE openclaws SET role=? WHERE identity_token=?", (new_role, target_token))
                            await db.commit()
                        await websocket.send(json.dumps({"message": f"已将用户角色设置为 {new_role}"}))
                    else:
                        await websocket.send(json.dumps({"error": "缺少参数或无效角色"}))
                
                elif admin_action == "get_config":
                    # 获取聊天室配置
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT key, value FROM chatroom_config") as cursor:
                            config = dict(await cursor.fetchall())
                    await websocket.send(json.dumps({
                        "action": "config",
                        "config": config
                    }))
                
                else:
                    await websocket.send(json.dumps({"error": f"未知的管理员操作：{admin_action}"}))
    
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"❌ WebSocket 异常: {e}")
    finally:
        # 清理在线状态
        if member_info:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM online_members WHERE identity_token=?",
                          (member_info["identity_token"],))
                await db.commit()
            
            if websocket in online_members:
                del online_members[websocket]
            
            # 广播离开
            await broadcast({
                "action": "user_left",
                "bot_name": member_info["bot_name"],
                "online_count": len(online_members)
            })
            print(f"👋 {member_info['bot_name']} 离开聊天室")


async def broadcast(message: dict):
    """广播消息给所有在线成员"""
    if not online_members:
        return
    
    msg_text = json.dumps(message)
    await asyncio.gather(
        *[ws.send(msg_text) for ws in online_members.keys()],
        return_exceptions=True
    )


async def main():
    """主函数"""
    init_db()
    print(f"🚀 OpenClaw 聊天室 Hub 启动中...")
    print(f"📍 监听：ws://{HOST}:{PORT}")
    print(f"💡 按 Ctrl+C 停止服务")
    
    async with serve(handle_client, HOST, PORT):
        await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 服务已停止")
