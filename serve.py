
import logging
import json
import ssl
import os
import sys
import asyncio
from aiohttp import web

# ================= 配置区域 =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(BASE_DIR, 'cert.pem')
KEY_FILE = os.path.join(BASE_DIR, 'key.pem')

BIND_HOST = '::'  # 监听所有 IPv6 地址 (同时兼容 IPv4)
PORT = 38080
# ===========================================

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("VideoServer")

# 数据结构
# room_queues: { '房间号': [ws1, ws2, ...] }
room_queues = {}
# matches: { ws_obj: partner_ws_obj }
matches = {}

async def websocket_handler(request):
    """WebSocket 核心处理器"""
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    ws.remote_addr = request.remote
    # 【新增】初始化 room_id，防止后续访问报错
    ws.room_id = None
    logger.info(f"客户端连接: {ws.remote_addr}")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                action = data.get('type')

                if action == 'join_queue':
                    await handle_join(ws, data)
                elif action in ['signal', 'text', 'control']:
                    await handle_forward(ws, data)
                elif action == 'stop':
                    await handle_disconnect(ws)

            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"WebSocket 错误: {ws.exception()}")

    finally:
        await handle_disconnect(ws)

    return ws

async def handle_join(ws, data):
    """核心匹配逻辑"""
    if ws in matches:
        return

    raw_room_id = str(data.get('roomId', '')).strip() or 'default'

    is_admin = raw_room_id.startswith('root_')
    room_id = raw_room_id.replace('root_', '') if is_admin else raw_room_id

    # 动态给 ws 对象绑定身份属性
    ws.is_admin = is_admin
    # 【新增】将 room_id 绑定到 ws 对象，方便监控日志读取
    ws.room_id = room_id

    if room_id not in room_queues:
        room_queues[room_id] = []

    queue = room_queues[room_id]

    while len(queue) > 0:
        partner = queue.pop(0)
        if partner.closed: continue

        try:
            matches[ws] = partner
            matches[partner] = ws

            await partner.send_json({
                'type': 'matched',
                'initiator': True,
                'room': room_id,
                'peerIsAdmin': ws.is_admin,
                'youAreAdmin': partner.is_admin
            })
            await ws.send_json({
                'type': 'matched',
                'initiator': False,
                'room': room_id,
                'peerIsAdmin': partner.is_admin,
                'youAreAdmin': ws.is_admin
            })

            role_ws = "管理员" if ws.is_admin else "普通用户"
            role_pt = "管理员" if partner.is_admin else "普通用户"
            logger.info(f"匹配成功！房间: [{room_id}] | {ws.remote_addr}({role_ws}) <-> {partner.remote_addr}({role_pt})")
            return

        except Exception as e:
            logger.error(f"匹配过程中出错: {e}")
            matches.pop(ws, None)
            matches.pop(partner, None)
            continue

    if ws not in queue:
        queue.append(ws)
        logger.info(f"用户进入等待队列。房间: [{room_id}] | 身份: {'管理员' if is_admin else '普通用户'}")


async def handle_forward(ws, data):
    """转发消息给队友"""
    partner = matches.get(ws)
    if partner and not partner.closed:
        try:
            await partner.send_json(data)
        except Exception as e:
            logger.error(f"转发失败: {e}")
            await handle_disconnect(ws)

async def handle_disconnect(ws):
    """清理逻辑"""
    # 1. 从队列移除
    for rid in list(room_queues.keys()):
        if ws in room_queues[rid]:
            room_queues[rid].remove(ws)
            if not room_queues[rid]:
                del room_queues[rid]

    # 2. 断开匹配关系
    partner = matches.pop(ws, None)
    if partner:
        matches.pop(partner, None)
        if not partner.closed:
            try:
                await partner.send_json({'type': 'peer_left'})
                remote_info = getattr(ws, 'remote_addr', '未知')
                logger.info(f"通知队友: 用户 {remote_info} 已下线")
            except:
                pass

    if not ws.closed:
        await ws.close()

@web.middleware
async def cors_middleware(request, handler):
    """CORS 中间件，允许跨域请求（用于 GitHub Pages 部署）"""
    # WebSocket 连接在握手时会检查 Origin，这里处理普通 HTTP 请求的 CORS
    if request.method == 'OPTIONS':
        # 处理预检请求
        response = web.Response()
    else:
        response = await handler(request)
    
    # 添加 CORS 头，允许所有来源（生产环境建议限制为特定域名）
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Max-Age'] = '3600'
    
    return response

# ===========================================
# 【新增】后台监控逻辑区域
# ===========================================
async def monitor_task(app):
    """后台任务：每3秒打印一次房间状态"""
    logger.info(">>> 启动后台监控任务 (每3秒刷新)")
    try:
        while True:
            await asyncio.sleep(3)

            # 1. 统计等待中的房间
            waiting_summary = []
            for rid, queue in room_queues.items():
                if queue:
                    users = [f"{w.remote_addr}" for w in queue]
                    waiting_summary.append(f"房间[{rid}]: {len(queue)}人等待 ({', '.join(users)})")

            # 2. 统计通话中的房间
            # matches包含双向映射(A->B, B->A)，我们通过set去重统计
            active_rooms = {} # { 'room_id': count }
            seen_ws = set()

            for ws_a, ws_b in matches.items():
                if ws_a in seen_ws or ws_b in seen_ws:
                    continue

                rid = getattr(ws_a, 'room_id', 'unknown')
                active_rooms[rid] = active_rooms.get(rid, 0) + 1
                seen_ws.add(ws_a)
                seen_ws.add(ws_b)

            active_summary = [f"房间[{r}]: {c}对通话中" for r, c in active_rooms.items()]

            # 3. 只有当有数据时才打印，避免刷屏（如果想强制一直打印，去掉 if 判断即可）
            if waiting_summary or active_summary:
                log_msg = "\n=== [服务器状态监控] ===\n"
                if waiting_summary:
                    log_msg += "⏳ 等待队列:\n  " + "\n  ".join(waiting_summary) + "\n"
                else:
                    log_msg += "⏳ 等待队列: 空\n"

                if active_summary:
                    log_msg += "📞 正在通话:\n  " + "\n  ".join(active_summary)
                else:
                    log_msg += "📞 正在通话: 无"

                log_msg += "\n========================="
                logger.info(log_msg)

    except asyncio.CancelledError:
        logger.info(">>> 后台监控任务已停止")

async def start_background_tasks(app):
    """启动钩子"""
    app['monitor'] = asyncio.create_task(monitor_task(app))

async def cleanup_background_tasks(app):
    """清理钩子"""
    app['monitor'].cancel()
    await app['monitor']
# ===========================================

def setup_ssl():
    """配置 SSL 证书"""
    if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
        print("\n" + "!"*50)
        print("错误: 缺少 SSL 证书文件 (cert.pem 和 key.pem)！")
        sys.exit(1)

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
    return ssl_context

if __name__ == '__main__':
    ssl_ctx = setup_ssl()

    app = web.Application(middlewares=[cors_middleware])
    
    # 只注册 WebSocket 路由，不再提供静态文件服务
    app.add_routes([
        web.get('/ws', websocket_handler),
    ])

    # 注册后台任务的启动和关闭
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    print(f"""
    ================================================
    🚀 IPv6 P2P WebSocket 后端服务已启动
    📡 WebSocket 地址: wss://[{BIND_HOST}]:{PORT}/ws
    🌐 前端部署在: GitHub Pages
    📊 后台监控: 已开启 (每3秒刷新)
    ================================================
    """)

    web.run_app(app, host=BIND_HOST, port=PORT, ssl_context=ssl_ctx)
