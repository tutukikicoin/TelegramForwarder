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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web")
templates = Jinja2Templates(directory="rss/app/templates")

CHATS_CACHE_PATH = os.path.join('.', 'db', 'chats_cache.json')


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


@router.get("/api/rules")
async def get_rules(user=Depends(get_current_user)):
    """获取所有转发规则"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)

    session = get_session()
    try:
        rules = session.query(ForwardRule).options(
            joinedload(ForwardRule.source_chat),
            joinedload(ForwardRule.target_chat)
        ).all()

        rules_list = []
        for rule in rules:
            rules_list.append({
                "id": rule.id,
                "source_name": rule.source_chat.name if rule.source_chat else "未知",
                "source_chat_id": rule.source_chat.telegram_chat_id if rule.source_chat else "",
                "target_name": rule.target_chat.name if rule.target_chat else "未知",
                "target_chat_id": rule.target_chat.telegram_chat_id if rule.target_chat else "",
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
    """创建转发规则"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)

    data = await request.json()
    source_id = data.get("source_id", "").strip()
    source_name = data.get("source_name", "").strip()
    target_id = data.get("target_id", "").strip()
    target_name = data.get("target_name", "").strip()

    if not source_id or not target_id:
        return JSONResponse({"success": False, "message": "请选择源群和目标群"})

    session = get_session()
    try:
        # 查找或创建源聊天
        source_chat = session.query(Chat).filter(Chat.telegram_chat_id == source_id).first()
        if not source_chat:
            source_chat = Chat(telegram_chat_id=source_id, name=source_name or "未命名")
            session.add(source_chat)
            session.flush()

        # 查找或创建目标聊天
        target_chat = session.query(Chat).filter(Chat.telegram_chat_id == target_id).first()
        if not target_chat:
            target_chat = Chat(telegram_chat_id=target_id, name=target_name or "未命名")
            session.add(target_chat)
            session.flush()

        # 设置 current_add_id
        if not target_chat.current_add_id:
            target_chat.current_add_id = source_id

        # 创建转发规则
        from enums.enums import ForwardMode, AddMode
        rule = ForwardRule(
            source_chat_id=source_chat.id,
            target_chat_id=target_chat.id
        )

        # 如果绑定自己，使用白名单模式
        if source_id == target_id:
            rule.forward_mode = ForwardMode.WHITELIST
            rule.add_mode = AddMode.WHITELIST

        session.add(rule)
        session.commit()

        return JSONResponse({
            "success": True,
            "message": f"已创建规则: {source_chat.name} -> {target_chat.name}",
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
        logger.error(f"切换规则状态失败: {str(e)}")
        return JSONResponse({"success": False, "message": str(e)})
    finally:
        session.close()


@router.delete("/api/rules/{rule_id}")
async def delete_rule(rule_id: int, user=Depends(get_current_user)):
    """删除规则（完整清理关联数据）"""
    if not user:
        return JSONResponse({"success": False, "message": "未登录"}, status_code=401)

    session = get_session()
    try:
        rule = session.query(ForwardRule).get(rule_id)
        if not rule:
            return JSONResponse({"success": False, "message": "规则不存在"})

        # 删除所有关联数据
        session.query(ReplaceRule).filter(ReplaceRule.rule_id == rule.id).delete()
        session.query(Keyword).filter(Keyword.rule_id == rule.id).delete()
        session.query(MediaExtensions).filter(MediaExtensions.rule_id == rule.id).delete()
        session.query(MediaTypes).filter(MediaTypes.rule_id == rule.id).delete()
        session.query(RuleSync).filter(RuleSync.rule_id == rule.id).delete()
        session.query(RuleSync).filter(RuleSync.sync_rule_id == rule.id).delete()
        session.query(PushConfig).filter(PushConfig.rule_id == rule.id).delete()

        # 删除 RSS 配置及其 patterns
        rss_config = session.query(RSSConfig).filter(RSSConfig.rule_id == rule.id).first()
        if rss_config:
            session.query(RSSPattern).filter(RSSPattern.rss_config_id == rss_config.id).delete()
            session.delete(rss_config)

        # 保留 rule 对象引用用于清理聊天
        rule_obj = rule

        # 删除规则本身
        session.delete(rule)
        session.commit()

        # 清理不再使用的聊天记录
        await check_and_clean_chats(session, rule_obj)

        return JSONResponse({"success": True, "message": "规则已删除"})
    except Exception as e:
        session.rollback()
        logger.error(f"删除规则失败: {str(e)}")
        return JSONResponse({"success": False, "message": f"删除失败: {str(e)}"})
    finally:
        session.close()
