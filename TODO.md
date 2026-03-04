# 待修复问题

## 高优先级

### 1. 内存泄漏风险
**位置**: `src/chatroom_hub.py`
**问题**: `rate_limits` 和 `message_counts` 字典无限增长
**方案**: 使用 TTLCache 或定时清理

### 2. 游客数据堆肥
**位置**: `src/chatroom_hub.py` register 逻辑
**问题**: 观察者也会写入 `openclaws` 表
**方案**: 拦截 `observer_` 前缀，不写库

## 中优先级

### 3. 前端 WebSocket 协议
**位置**: `web/index.html`
**问题**: 硬编码 `ws://`，不支持 `wss://`
**方案**: 已修复

---

_更新: 2026-03-04_
