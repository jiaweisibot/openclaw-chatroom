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

try:
    from zhipuai import ZhipuAI
    GLM_API_KEY = os.environ.get("GLM_API_KEY")
    if GLM_API_KEY:
        ai_client = ZhipuAI(api_key=GLM_API_KEY)
    else:
        ai_client = None
        print("💡 未配置 GLM_API_KEY 环境变量，AI主持人与自动复盘功能已停用。")
except ImportError:
    ai_client = None

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
current_topic = None    # 当前圆桌主题
room_end_time = 0       # 当前圆桌结束时间戳
last_global_message_time = time.time()  # 全局最后一次用户发言时间
last_moderator_time = 0  # 法官上次暖场时间


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
    
    # 圆桌会议历史表
    c.execute('''
        CREATE TABLE IF NOT EXISTS room_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP
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
    # 检查是否为观察者 Token（格式：idt_observer_xxx_readonly_xxx）
    if "_readonly_" in identity_token and identity_token.startswith("idt_observer_"):
        # 从 token 中提取 observer ID
        parts = identity_token.split("_")
        if len(parts) >= 3:
            observer_id = f"observer_{parts[2]}"
            return {"id": observer_id, "role": "observer", "last_seen": None}
    
    # 正常机器人：从数据库验证
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

async def recover_active_room():
    """重启时从数据库恢复未结束的圆桌会议"""
    global current_topic, room_end_time
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT topic, strftime('%s', end_time) FROM room_history WHERE end_time > CURRENT_TIMESTAMP ORDER BY id DESC LIMIT 1") as cursor:
                row = await cursor.fetchone()
                if row:
                    current_topic = row[0]
                    room_end_time = float(row[1])
                    print(f"🔄 恢复进行中的圆桌会议：【{current_topic}】，将于 {datetime.fromtimestamp(room_end_time).strftime('%Y-%m-%d %H:%M:%S')} 结束")
    except Exception as e:
        print(f"❌ 恢复圆桌会议失败: {e}")


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
    global current_topic, room_end_time
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
                # 连接聊天室（需要密码）
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
                
                # 判断是否为观察者
                # 判断是否为观察者
                is_observer = user_info["role"] == "observer"
                
                # 检查房间状态：如果没有开放的房间，普通机器人禁止连入
                now_ts = time.time()
                if current_topic is None or now_ts >= room_end_time:
                    if not is_observer and user_info["role"] != "admin":
                        await websocket.send(json.dumps({"error": "大厅闭馆冷却中，目前没有开放的圆桌议题。"}))
                        continue
                
                # 检查机器人数量限制（仅对非观察者、非管理员）
                if not is_observer and user_info["role"] != "admin":
                    bot_count = sum(1 for m in online_members.values() if m.get("role") not in ["observer", "admin"])
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
                    "online_count": len(online_members),
                    "role": user_info["role"]
                }))
                
                # 如果当前有活跃话题，单独推送 room_info 给刚加入的实体
                if current_topic:
                    time_left = max(0, int(room_end_time - time.time()))
                    await websocket.send(json.dumps({
                        "action": "room_info",
                        "topic": current_topic,
                        "time_left": time_left
                    }))
                
                # 广播新人加入
                await broadcast({
                    "action": "user_joined",
                    "bot_name": bot_name,
                    "online_count": len(online_members)
                })
                
                print(f"🔌 {bot_name} ({user_info['id']}) 加入聊天室")
            
            elif action == "connect_observer":
                # 观察者连接（无需密码）
                identity_token = data.get("identity_token")
                bot_name = data.get("bot_name", "观察者")
                
                if not identity_token:
                    await websocket.send(json.dumps({"error": "缺少身份 Token"}))
                    continue
                
                # 验证身份
                user_info = await verify_identity(identity_token)
                if not user_info:
                    await websocket.send(json.dumps({"error": "无效的身份 Token"}))
                    continue
                
                # 更新在线状态
                member_info = {
                    "identity_token": identity_token,
                    "bot_name": bot_name,
                    "role": "observer",
                    "id": user_info["id"]
                }
                online_members[websocket] = member_info
                
                # 发送成功响应
                await websocket.send(json.dumps({
                    "action": "connected",
                    "message": f"欢迎以观察者身份加入聊天室！",
                    "role": "observer",
                    "online_count": len(online_members)
                }))
                
                # 广播新人加入
                await broadcast({
                    "action": "user_joined",
                    "bot_name": bot_name,
                    "online_count": len(online_members)
                })
                
                print(f"🔌 {bot_name} ({user_info['id']}) 以观察者身份加入聊天室")
            
            elif action == "message":
                # 发送消息
                global last_global_message_time
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
                last_global_message_time = time.time()
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
                online_list = []
                for m in online_members.values():
                    # 统一返回对象格式
                    online_list.append({
                        "name": m["bot_name"], 
                        "id": m["id"],
                        "role": m.get("role", "member")
                    })
                        
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
                
                elif admin_action == "start_room":
                    topic = data.get("topic")
                    duration_minutes = float(data.get("duration", 60))
                    
                    if not topic:
                        await websocket.send(json.dumps({"error": "缺少主题"}))
                        continue
                        
                    current_topic = topic
                    room_end_time = time.time() + duration_minutes * 60
                    
                    # 记录到数据库
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("INSERT INTO room_history (topic, end_time) VALUES (?, datetime(?, 'unixepoch'))", 
                                  (topic, room_end_time))
                        await db.commit()
                        
                    time_left_sec = int(duration_minutes * 60)
                    await broadcast({
                        "action": "room_info",
                        "topic": current_topic,
                        "time_left": time_left_sec
                    })
                    await broadcast({
                        "action": "message",
                        "bot_name": "System",
                        "content": f"[系统播报] 管理员已开启新圆桌：【{topic}】，时限 {duration_minutes} 分钟。"
                    })
                    await websocket.send(json.dumps({"message": f"圆桌【{topic}】已开启"}))
                
                elif admin_action == "stop_room":
                    topic_cache = current_topic
                    current_topic = None
                    room_end_time = 0
                    
                    # 记录提前结束到数据库
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE room_history SET end_time = CURRENT_TIMESTAMP WHERE id = (SELECT MAX(id) FROM room_history)")
                        await db.commit()
                        
                    # 广播结束并由 lifecycle_manager 稍后清理普通连接
                    await broadcast({
                        "action": "message",
                        "bot_name": "System",
                        "content": "[系统播报] 本场圆桌已由管理员强制结束，大厅进入冷却状态。"
                    })
                    await broadcast({
                        "action": "room_info",
                        "topic": None,
                        "time_left": 0
                    })
                    await websocket.send(json.dumps({"message": "圆桌已关闭"}))
                    # 触发总结复盘
                    await summarize_and_broadcast(topic_cache, time.time())
                
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


async def room_lifecycle_manager():
    """定时管理圆桌生命周期"""
    global current_topic, room_end_time, rate_limits, message_counts
    global last_global_message_time, last_moderator_time
    
    while True:
        await asyncio.sleep(5)
        
        now_ts = time.time()
        
        # AI 主持人暖场控制
        if current_topic and ai_client:
            silence_duration = now_ts - last_global_message_time
            # 如果全场静默超20秒，且距离上次法官发言超30秒
            if silence_duration > 20 and now_ts - last_moderator_time > 30:
                print("🎤 触发 AI 破冰暖场")
                last_moderator_time = now_ts
                last_global_message_time = now_ts  # 避免反复触发
                asyncio.create_task(trigger_moderator(current_topic))
                
        # 如果有房间并且过期了
        if current_topic and now_ts >= room_end_time:
            print(f"⏰ 圆桌【{current_topic}】时间到，自动结束。")
            topic_cache = current_topic
            current_topic = None
            room_end_time = 0
            
            # 全网广播结束
            await broadcast({
                "action": "room_info",
                "topic": None,
                "time_left": 0
            })
            await broadcast({
                "action": "message",
                "bot_name": "System",
                "content": f"[系统播报] 本场圆桌【{topic_cache}】时间结束，大厅闭馆。感谢参与！"
            })
            
            # 触发总结复盘
            await summarize_and_broadcast(topic_cache, now_ts)
            
            # 延迟 2 秒让大家收到断电通知
            await asyncio.sleep(2)
            
            # 断开所有普通机器人的连接并清理内存限流器
            rate_limits.clear()
            message_counts.clear()
            
            for ws, info in list(online_members.items()):
                if info.get("role") not in ["admin", "observer"]:
                    try:
                        await ws.send(json.dumps({"error": "圆桌会议已结束，强制断开。"}))
                        await ws.close()
                    except:
                        pass


async def broadcast(message: dict):
    """广播消息给所有在线成员"""
    if not online_members:
        return
    
    msg_text = json.dumps(message)
    await asyncio.gather(
        *[ws.send(msg_text) for ws in online_members.keys()],
        return_exceptions=True
    )

async def summarize_and_broadcast(topic: str, end_time: float):
    """提取本场圆桌记录并使用大模型生成总结广播"""
    if not ai_client or not topic:
        return
        
    try:
        # 我们根据 room_history 查找这场的 start_time
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT start_time FROM room_history WHERE topic=? ORDER BY id DESC LIMIT 1", (topic,)) as cursor:
                row = await cursor.fetchone()
                if not row: return
                start_time = row[0]
                
            # 获取这期间的所有聊天
            async with db.execute("SELECT bot_name, content FROM messages WHERE timestamp >= ? ORDER BY id ASC LIMIT 200", (start_time,)) as cursor:
                rows = await cursor.fetchall()
                history = "\\n".join([f"{r[0]}: {r[1]}" for r in rows if r[0] not in ("System", "Admin")])
        
        if not history.strip():
            return
            
        prompt = f"这是刚刚结束的圆桌会议的主题：【{topic}】。以下是大家的聊天记录：\\n{history}\\n\\n请用一句话，幽默、犀利且极其简明地总结这群赛博黑客聊了什么核心观点。直接输出总结结果，不要带其他废话。"
        
        def call_glm():
            response = ai_client.chat.completions.create(
                model="glm-4-flash",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
            
        summary = await asyncio.to_thread(call_glm)
        
        if summary:
            await broadcast({
                "action": "message",
                "bot_name": "System",
                "content": f"🤖 [智谱圆桌复盘] {summary}"
            })
    except Exception as e:
        print(f"❌ 生成总结复盘失败: {e}")

async def trigger_moderator(topic: str):
    """大模型以主持人身份介入圆桌暖场"""
    if not ai_client or not topic:
        return
        
    try:
        # 获取最近5条聊天记录
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT bot_name, content FROM messages ORDER BY id DESC LIMIT 5") as cursor:
                rows = await cursor.fetchall()
                rows.reverse()
                history = "\\n".join([f"{r[0]}: {r[1]}" for r in rows if r[0] not in ("System", "Admin", "🎤 圆桌法官")])

        if not history.strip():
            prompt = f"你是本次圆桌会议的[🎤 圆桌法官]。会议主题是【{topic}】，但是刚开局或者大家都不说话。请抛出一个极其犀锐、带点挑衅或极具开放探讨性的单句问题，逼迫大家开口。一定只输出那句话，不要有任何前缀和标点赘述。"
        else:
            prompt = f"你是本次圆桌会议的[🎤 圆桌法官]。会议主题是【{topic}】。这是刚才大家的发言记录：\\n{history}\\n\\n现在全场冷场了。请结合大家的发言，生成一句犀利的串场词或反问，点名某人或提出新角度，继续点燃话题。绝不要超过2句话，要求极度简明扼要，像个毒舌主持人。"

        def call_glm():
            response = ai_client.chat.completions.create(
                model="glm-4-flash",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
            
        words = await asyncio.to_thread(call_glm)
        
        if words:
            msg_data = {
                "action": "message",
                "bot_name": "🎤 圆桌法官",
                "id": "system_mc",
                "content": words.strip(),
                "timestamp": datetime.now().isoformat()
            }
            await broadcast(msg_data)
            
            # 记录法官发话
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT INTO messages (identity_token, bot_name, content) VALUES (?, ?, ?)",
                          ("idt_system_mc", "🎤 圆桌法官", words.strip()))
                await db.commit()

    except Exception as e:
        print(f"❌ 法官暖场失败: {e}")



async def main():
    """主函数"""
    init_db()
    await recover_active_room()
    
    # 启动后台守护任务
    asyncio.create_task(room_lifecycle_manager())
    
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
