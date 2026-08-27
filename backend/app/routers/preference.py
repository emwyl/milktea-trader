"""投资偏好分析路由：问卷 → 评分 → LLM 建议（Key 到位生效）→ 匹配方案类型。"""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import UserProfile, User, UserSetting
from app.schemas import PreferenceIn, PreferenceOut
from app.services.preference import QUESTIONNAIRE, INDICATOR_CHOICES, score, match_scheme
from app.services.ai import get_ai_provider

router = APIRouter(prefix="/api/preference", tags=["preference"])


@router.get("/questionnaire")
def questionnaire(user: User = Depends(get_current_user)):
    return {"questions": QUESTIONNAIRE, "indicators": INDICATOR_CHOICES}


@router.get("/me")
def _user_ai_cfg(db, user_id: int) -> dict:
    row = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == "ai").first()
    return json.loads(row.value_json) if row else {}


def my_profile(db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    p = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
    if not p:
        return {"empty": True}
    return {"scores": json.loads(p.scores_json), "archetype": p.archetype,
            "summary": "", "focus_indicators": json.loads(p.focus_indicators_json),
            "ai_advice": p.ai_advice, "matched_scheme": match_scheme(p.archetype)}


@router.post("")
def submit(body: PreferenceIn, db: SessionLocal = Depends(get_db), user: User = Depends(get_current_user)):
    res = score(body.answers)
    # 取 AI Key（按用户隔离）
    ai_cfg = _user_ai_cfg(db, user.id)
    ai_advice = ""
    focus = body.focus_indicators or []
    if ai_cfg.get("api_key"):
        prompt = (
            f"用户投资画像：{res['summary']}，标签：{res['archetype']}。\n"
            f"用户关注的指标：{focus}。\n"
            "请基于专业共识（多指标共振、先控风险再谈收益、严格止损），给出：\n"
            "1) 对用户关注指标组合的专业点评与盲区提醒；2) 推荐的指标搭配与风控要点；"
            "3) 适合他的选股/择时模型方向。简洁、可操作、强调风险。"
        )
        ai_advice = get_ai_provider().analyze(prompt, ai_cfg)
    prof = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
    if not prof:
        prof = UserProfile(user_id=user.id)
        db.add(prof)
    prof.answers_json = json.dumps(body.answers)
    prof.scores_json = json.dumps(res["scores"])
    prof.archetype = res["archetype"]
    prof.focus_indicators_json = json.dumps(focus)
    prof.ai_advice = ai_advice
    prof.updated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
    db.commit()
    return PreferenceOut(scores=res["scores"], archetype=res["archetype"], summary=res["summary"],
                         focus_indicators=focus, ai_advice=ai_advice, matched_scheme=match_scheme(res["archetype"]))
