from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.models.entities import Book, Chapter, ChapterBrief, GenerationTask
from app.services.chapter_samples import TASK_TYPE_CHAPTER_SAMPLE
from app.services.chapter_samples import _sample_diversity_report
from app.db.session import session_scope
from app.services.chapter_samples import adopt_chapter_sample, latest_chapter_samples
from regression_db import isolated_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect latest chapter sample diversity.")
    parser.add_argument("--book-id", type=int, default=2)
    parser.add_argument("--chapter-number", type=int, default=1)
    parser.add_argument("--min-score", type=int, default=65)
    args = parser.parse_args()

    isolated_database("chapter-sample-diversity-regression")
    with session_scope() as session:
        book_id, chapter_number = _seed_sample_fixture(session)
        latest = latest_chapter_samples(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            limit=3,
        )
        adopted = adopt_chapter_sample(
            session,
            task_id=int(latest.get("task_id") or 0),
            sample_index=1,
            revision_mode="targeted",
        )
        adopted_brief = session.get(ChapterBrief, adopted.brief_id)
        adopted_text = "\n".join(
            [adopted_brief.goal or "", adopted_brief.required_beats or "", adopted_brief.constraints or ""]
        ) if adopted_brief else ""
        no_usable_report = _sample_diversity_report(_thin_distinct_samples())
    report = latest.get("diversity_report") or latest.get("fallback_diversity_report") or {}
    score = int(report.get("score") or 0)
    latest_failed = latest.get("status") == "failed"
    no_usable_guard_ok = no_usable_report.get("status") == "attention" and "no_usable_sample" in (
        no_usable_report.get("issues") or []
    )
    adoption_fingerprint_ok = (
        "写作指纹继承" in adopted_text
        and "视角距离" in adopted_text
        and "句段节奏" in adopted_text
        and "场景展开" in adopted_text
    )
    sample_retry_ok = bool(report.get("retry_directives")) and bool(report.get("usable_requirements"))
    status = (
        "pass"
        if not latest_failed
        and score >= args.min_score
        and not report.get("issues")
        and no_usable_guard_ok
        and adoption_fingerprint_ok
        and sample_retry_ok
        else "attention"
    )
    print(
        json.dumps(
            {
                "status": status,
                "book_id": book_id,
                "chapter_number": chapter_number,
                "task_id": latest.get("task_id"),
                "latest_task_status": latest.get("status"),
                "latest_error": latest.get("error", ""),
                "fallback_task_id": latest.get("fallback_task_id"),
                "score": score,
                "threshold": args.min_score,
                "diversity_report": report,
                "no_usable_guard": no_usable_report,
                "adoption_fingerprint_ok": adoption_fingerprint_ok,
                "sample_retry_ok": sample_retry_ok,
                "attention_explanation": _attention_explanation(latest=latest, report=report, threshold=args.min_score),
                "trial_impact": "blocks_trial" if latest_failed else ("safe_to_trial_with_review" if status == "attention" else "safe_to_trial"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _seed_sample_fixture(session) -> tuple[int, int]:
    book = Book(title=f"sample-diversity-regression-{datetime.utcnow().timestamp()}", genre="真实武侠", target_platform="manual")
    session.add(book)
    session.flush()
    chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
    session.add(chapter)
    samples = [
        _sample_fixture(1, "药铺血账", "现场压力", "药铺账本和门闩逼主角选择"),
        _sample_fixture(2, "渡口错认", "规则误判", "路引、船夫和差役制造误判"),
        _sample_fixture(3, "祠堂供灯", "证言压力", "供灯、绳痕和活证人推动选择"),
    ]
    task = GenerationTask(
        book_id=book.id,
        task_type=TASK_TYPE_CHAPTER_SAMPLE,
        status="completed",
        input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
        output_json=json.dumps({"samples": samples, "gate_passed": True}, ensure_ascii=False),
    )
    session.add(task)
    session.flush()
    return book.id, chapter.chapter_number


def _sample_fixture(index: int, title: str, axis: str, opening_seed: str) -> dict:
    openings = {
        1: (
            "药铺后门被雨泡得发胀，门闩却从里侧断成两截。陈默蹲下去摸断口，指腹沾到一点黑色药泥，"
            "鼻尖先闻见苦参味，随后才听见柜台后有人压着喘。掌柜不肯开灯，只隔着药柜问他带没带银子。"
            "陈默说没有，又把袖中的锈铜铃按住，因为铃舌正朝账本方向发冷。门外铁尺馆的人已经踩上台阶，"
            "他必须在交出账本和保住柜后伤者之间选一边。掌柜怕得把算盘珠拨错，柜后那人却忍着疼用鞋尖顶出一只破碗，"
            "碗底压着半枚旧印。陈默忽然明白，门闩不是被撞断，而是有人从里头故意掰断，好让搜铺的人以为伤者刚逃进来。"
            "他没有解释，只把药碗端到门口，借苦味遮住血腥。下一瞬，伤者从药柜下伸出两根手指，指尖写着一个湿漉漉的梅字。"
        ),
        2: (
            "渡口雾气贴着水面滚，船夫把路引翻来覆去看了三遍，偏说陈默的印泥颜色不对。陈默先看见船板下藏着半截湿绳，"
            "又看见差役靴边沾着不是渡口的红土，心里知道盘问不是查证，是拖时间。身后追来的药铺小伙子喘得弯腰，怀里账纸快被雨打透。"
            "陈默没有硬闯，只把三枚铜钱排在船舷上，问船夫：若这渡口今晚真归官府管，为什么官灯还灭着？"
            "船夫脸色一变，差役的手也按上刀柄。陈默背心出了一层汗，因为他猜错一半，船夫不是帮差役拖人，而是在等另一笔价钱。"
            "药铺小伙子想喊，被陈默用手肘压住。雾里传来一声轻咳，像有人隔水认出了账纸上的旧印。"
            "水雾里另一艘无灯小船悄悄靠岸，船头挂着的不是官牌，而是一截刚从药铺门闩上拆下来的木刺。"
        ),
        3: (
            "祠堂偏殿只点着一盏供灯，灯芯烧得太短，火光一跳一跳地舔着牌位底座。陈默推门进去时，先听见木梁上有细微摩擦声，"
            "像湿绳被人慢慢收紧。他抬头看了一眼，喉咙也跟着发干，因为梁下悬着的不是尸体，而是一个被吊住手腕的活证人。"
            "对方嘴里塞着布，只能用脚尖一下下碰地，碰出的节奏正好对应药铺账纸上的三笔缺口。"
            "陈默没有急着割绳，先把供桌上的香灰拨开，露出一枚被按进灰里的旧铜扣。铜扣仍带体温，说明布置这局的人刚走不久。"
            "若立刻救人，他会踩断供桌下的暗线；若先查暗线，证人撑不到半盏茶。他屏住呼吸，用刀鞘托住绳影，逼自己在发抖前做出选择。"
        ),
    }
    return {
        "index": index,
        "title": title,
        "exploration_axis": axis,
        "experiment_hypothesis": opening_seed,
        "direction": "主角在现场压力下主动选择并付出代价",
        "opening": openings[index],
        "scene_plan": ["现场异常", "人物试探", "章末新线索"],
        "difference_from_existing": f"{title}采用不同入口和压力源。",
        "anti_ai_flavor_strategy": "用具体物件和人物反应承载信息。",
        "pov_strategy": "贴住主角误判、观察和身体反应。",
        "precision_strategy": "只让推断来自可见证据。",
    }


def _thin_distinct_samples() -> list[dict]:
    return [
        {
            "index": 1,
            "title": "扫帚与落叶",
            "exploration_axis": "规则误判型",
            "experiment_hypothesis": "测试物理证据触发。",
            "direction": "观察证据。",
            "opening": "林默被扫堂腿撂倒，爬起来看见落叶绕着扫帚打旋。他盯住竹柄上的磨痕，觉得这场景有点熟。",
            "scene_plan": ["观察", "试探", "触发"],
            "difference_from_existing": "换成物理证据。",
            "anti_ai_flavor_strategy": "用物件写。",
            "pov_strategy": "贴住身体感受。",
            "precision_strategy": "判断来自可见证据。",
        },
        {
            "index": 2,
            "title": "欠条与饭钱",
            "exploration_axis": "利益交换型",
            "experiment_hypothesis": "测试欠条关系。",
            "direction": "写欠条换机会。",
            "opening": "报名桌后的人要三钱银子，林默摸了摸空口袋，只能拿出草稿纸，问能不能写欠条。",
            "scene_plan": ["报名", "写欠条", "交换"],
            "difference_from_existing": "换成利益交换。",
            "anti_ai_flavor_strategy": "用账册写。",
            "pov_strategy": "贴住窘迫。",
            "precision_strategy": "账目清楚。",
        },
        {
            "index": 3,
            "title": "后山与铜片",
            "exploration_axis": "信息悬疑型",
            "experiment_hypothesis": "测试升维线索。",
            "direction": "从铜片发现异常。",
            "opening": "柴房角落露出半截铜片，边缘发黑，背面刻着云中君三个小字。林默握在手里，掌心忽然一烫。",
            "scene_plan": ["劈柴", "拾铜片", "异常"],
            "difference_from_existing": "换成物件谜题。",
            "anti_ai_flavor_strategy": "用触感写。",
            "pov_strategy": "贴住疼痛。",
            "precision_strategy": "物件尺寸明确。",
        },
    ]


def _attention_explanation(*, latest: dict, report: dict, threshold: int) -> list[str]:
    reasons: list[str] = []
    if latest.get("status") == "failed":
        reasons.append(f"latest_sample_task_failed:{latest.get('latest_error') or latest.get('error') or ''}")
    score = int(report.get("score") or 0)
    if score < threshold:
        reasons.append(f"diversity_score_low:{score}<{threshold}")
    for issue in report.get("issues") or []:
        reasons.append(f"diversity_issue:{issue}")
    return reasons


if __name__ == "__main__":
    raise SystemExit(main())
