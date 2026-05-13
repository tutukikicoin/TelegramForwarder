from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from models.models import (
    get_session, Chat, ForwardRule, Keyword, ReplaceRule,
    MediaTypes, MediaExtensions, RuleSync, PushConfig, RSSConfig, RSSPattern
)
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError
from .auth import get_current_user
from utils.common import check_and_clean_chats
import json
import os
import logging
import urllib.request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web")
templates = Jinja2Templates(directory="rss/app/templates")

CHATS_CACHE_PATH = os.path.join('.', 'db', 'chats_cache.json')
FEISHU_ROUTES_PATH = os.path.join('.', 'db', 'feishu_routes.json')

# 飞书配置（从环境变量读取）
FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')

# 目标 TG 账号 ID（kimi），所有规则都转发到这个账号
TARGET_TG_ID = os.getenv('USER_ID', '')


def get_feishu_token():
    """获取飞书 tenant access token"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return None
    try:
        url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
        data = json.dumps({'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET}).encode()
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get('code') == 0:
                return result['tenant_access_token']
    except Exception as e:
        logger.error(f"获取飞书 token 失败: {e}")
    return None


def load_feishu_routes():
    """读取飞书路由配置"""
    if os.path.exists(FEISHU_ROUTES_PATH):
        try:
            with open(FEISHU_ROUTES_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_feishu_routes(routes):
    """保存飞书路由配置"""
    with open(FEISHU_ROUTES_PATH, 'w', encoding='utf-8') as f:
        json.dump(routes, f, ensure_ascii=False, indent=2)


@router.get("/dashboard", response_class=HTMLResponse)
async def web_dashboard(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("web_dashboard.html", {"request": request, "user": user})


@router.get("/api/chats")
async def get_chats(user=Depends(get_current_user)):
    """从缓存文件获取 Telegram 对话列表"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)
    try:
        if not os.path.exists(CHATS_CACHE_PATH):
            return JSONResponse({"success": True, "chats": [], "message": "对话缓存不存在，请重启服务"})
        with open(CHATS_CACHE_PATH, 'r', encoding='utf-8') as f:
            chats = json.load(f)
        return JSONResponse({"success": True, "chats": chats})
    except Exception as e:
        logger.error(f"读取对话缓存失败: {str(e)}")
        return JSONResponse({"success": False, "message": f"读取失败: {str(e)}"})


@router.get("/api/feishu_groups")
async def get_feishu_groups(user=Depends(get_current_user)):
    """通过飞书 API 获取机器人所在的群列表"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)
    token = get_feishu_token()
    if not token:
        return JSONResponse({"success": False, "message": "无法获取飞书 Token，请检查 FEISHU_APP_ID 和 FEISHU_APP_SECRET"})
    try:
        groups = []
        page_token = ''
        while True:
            url = f'https://open.feishu.cn/open-apis/im/v1/chats?page_size=100'
            if page_token:
                url += f'&page_token={page_token}'
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                for item in result.get('data', {}).get('items', []):
                    groups.append({
                        'chat_id': item['chat_id'],
                        'name': item.get('name', '未命名群'),
                    })
                if not result.get('data', {}).get('has_more'):
                    break
                page_token = result['data'].get('page_token', '')
        return JSONResponse({"success": True, "groups": groups})
    except Exception as e:
        logger.error(f"获取飞书群列表失败: {e}")
        return JSONResponse({"success": False, "message": f"获取失败: {str(e)}"})


@router.get("/api/rules")
async def get_rules(user=Depends(get_current_user)):
    """获取所有转发规则，附带飞书目标信息"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)

    session = get_session()
    try:
        rules = session.query(ForwardRule).options(
            joinedload(ForwardRule.source_chat),
            joinedload(ForwardRule.target_chat)
        ).all()

        # 读取飞书路由配置
        feishu_routes = load_feishu_routes()
        # 反转路由: chat_id -> [label1, label2, ...]
        feishu_reverse = {}
        for label, chat_id in feishu_routes.items():
            feishu_reverse.setdefault(chat_id, []).append(label)

        # 读取 push 配置
        push_configs = {}
        for pc in session.query(PushConfig).all():
            push_configs[pc.rule_id] = pc.push_channel

        rules_list = []
        for rule in rules:
            # 从 push_channel 提取标签名
            push_label = ''
            feishu_chat_id = ''
            if rule.id in push_configs:
                channel = push_configs[rule.id]
                # 格式: json://feishu-bridge:8001/标签名
                if '/feishu-bridge' in channel:
                    push_label = channel.rsplit('/', 1)[-1] if '/' in channel else ''
                    push_label = urllib.request.unquote(push_label) if push_label else ''
                    # 查找对应的飞书 chat_id
                    feishu_chat_id = feishu_routes.get(push_label, '')

            rules_list.append({
                "id": rule.id,
                "source_name": rule.source_chat.name if rule.source_chat else "未知",
                "source_chat_id": rule.source_chat.telegram_chat_id if rule.source_chat else "",
                "push_label": push_label,
                "feishu_chat_id": feishu_chat_id,
                "enabled": rule.enable_rule,
            })

        return JSONResponse({"success": True, "rules": rules_list})
    except Exception as e:
        logger.error(f"获取规则失败: {str(e)}")
        return JSONResponse({"success": False, "message": str(e)})
    finally:
        session.close()


@router.post("/api/rules")
async def create_rule(request: Request, user=Depends(get_current_user)):
    """创建转发规则：TG 源群 → kimi → 飞书目标群"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)

    data = await request.json()
    source_id = data.get("source_id", "").strip()
    source_name = data.get("source_name", "").strip()
    feishu_chat_id = data.get("feishu_chat_id", "").strip()
    push_label = data.get("push_label", "").strip()

    if not source_id:
        return JSONResponse({"success": False, "message": "请选择 TG 源群"})
    if not feishu_chat_id:
        return JSONResponse({"success": False, "message": "请选择飞书目标群"})
    if not push_label:
        # 默认用源群名称作为标签
        push_label = source_name or source_id

    target_id = TARGET_TG_ID
    if not target_id:
        return JSONResponse({"success": False, "message": "未配置 USER_ID（目标 TG 账号）"})

    session = get_session()
    try:
        # 查找或创建源聊天
        source_chat = session.query(Chat).filter(Chat.telegram_chat_id == source_id).first()
        if not source_chat:
            source_chat = Chat(telegram_chat_id=source_id, name=source_name or "未命名")
            session.add(source_chat)
            session.flush()

        # 查找或创建目标聊天 (kimi)
        target_chat = session.query(Chat).filter(Chat.telegram_chat_id == target_id).first()
        if not target_chat:
            target_chat = Chat(telegram_chat_id=target_id, name="kimi")
            session.add(target_chat)
            session.flush()

        if not target_chat.current_add_id:
            target_chat.current_add_id = source_id

        # 创建转发规则
        rule = ForwardRule(
            source_chat_id=source_chat.id,
            target_chat_id=target_chat.id,
            enable_push=True,
            enable_only_push=True,
        )
        session.add(rule)
        session.flush()

        # 创建 PushConfig
        push_channel = f"json://feishu-bridge:8001/{urllib.request.quote(push_label, safe='')}"
        push_config = PushConfig(
            rule_id=rule.id,
            enable_push_channel=True,
            push_channel=push_channel,
            media_send_mode='Single',
        )
        session.add(push_config)

        session.commit()

        # 更新飞书路由配置
        routes = load_feishu_routes()
        routes[push_label] = feishu_chat_id
        save_feishu_routes(routes)

        return JSONResponse({
            "success": True,
            "message": f"已创建规则: {source_chat.name} -> 飞书",
            "rule_id": rule.id
        })
    except IntegrityError:
        session.rollback()
        return JSONResponse({"success": False, "message": "该转发规则已存在"})
    except Exception as e:
        session.rollback()
        logger.error(f"创建规则失败: {str(e)}")
        return JSONResponse({"success": False, "message": f"创建失败: {str(e)}"})
    finally:
        session.close()


@router.put("/api/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: int, user=Depends(get_current_user)):
    """启用/禁用规则"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)
    session = get_session()
    try:
        rule = session.query(ForwardRule).get(rule_id)
        if not rule:
            return JSONResponse({"success": False, "message": "规则不存在"})
        rule.enable_rule = not rule.enable_rule
        session.commit()
        status = "已启用" if rule.enable_rule else "已禁用"
        return JSONResponse({"success": True, "message": status, "enabled": rule.enable_rule})
    except Exception as e:
        session.rollback()
        return JSONResponse({"success": False, "message": str(e)})
    finally:
        session.close()


@router.delete("/api/rules/{rule_id}")
async def delete_rule(rule_id: int, user=Depends(get_current_user)):
    """删除规则（完整清理关联数据 + 飞书路由）"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)

    session = get_session()
    try:
        rule = session.query(ForwardRule).get(rule_id)
        if not rule:
            return JSONResponse({"success": False, "message": "规则不存在"})

        # 获取 push label 用于清理飞书路由
        push_label = ''
        push_config = session.query(PushConfig).filter(PushConfig.rule_id == rule.id).first()
        if push_config and '/feishu-bridge' in (push_config.push_channel or ''):
            push_label = push_config.push_channel.rsplit('/', 1)[-1]
            push_label = urllib.request.unquote(push_label) if push_label else ''

        # 删除所有关联数据
        session.query(ReplaceRule).filter(ReplaceRule.rule_id == rule.id).delete()
        session.query(Keyword).filter(Keyword.rule_id == rule.id).delete()
        session.query(MediaExtensions).filter(MediaExtensions.rule_id == rule.id).delete()
        session.query(MediaTypes).filter(MediaTypes.rule_id == rule.id).delete()
        session.query(RuleSync).filter(RuleSync.rule_id == rule.id).delete()
        session.query(RuleSync).filter(RuleSync.sync_rule_id == rule.id).delete()
        session.query(PushConfig).filter(PushConfig.rule_id == rule.id).delete()

        rss_config = session.query(RSSConfig).filter(RSSConfig.rule_id == rule.id).first()
        if rss_config:
            session.query(RSSPattern).filter(RSSPattern.rss_config_id == rss_config.id).delete()
            session.delete(rss_config)

        rule_obj = rule
        session.delete(rule)
        session.commit()

        # 清理飞书路由
        if push_label:
            routes = load_feishu_routes()
            routes.pop(push_label, None)
            save_feishu_routes(routes)

        # 清理不再使用的聊天记录
        await check_and_clean_chats(session, rule_obj)

        return JSONResponse({"success": True, "message": "规则已删除"})
    except Exception as e:
        session.rollback()
        logger.error(f"删除规则失败: {str(e)}")
        return JSONResponse({"success": False, "message": f"删除失败: {str(e)}"})
    finally:
        session.close()
