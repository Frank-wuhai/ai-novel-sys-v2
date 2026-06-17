from __future__ import annotations

import json

from app.services.quality import evaluate_chapter
from app.services.writer_craft import evaluate_writer_craft


def main() -> int:
    terse = "\n".join(
        [
            "铁剑门很冷。",
            "压迫感扑面而来。",
            "林默沉默。",
            "尴尬。",
            "所有人都看着他。",
            "气氛肃杀。",
            "他知道自己必须现在就做决定。",
            "真实，冰冷，像某种命运。",
        ]
        * 12
    )
    expanded = "\n".join(
        [
            "铁剑门的门槛比林默想的高，青石边缘被无数双鞋底磨得发亮。他抬脚时鞋尖蹭了一下，泥灰落进门缝，旁边负责登记的灰衣弟子抬起眼，笔杆在桌面轻轻一敲。",
            "院子东侧飘来蒸馒头的酸气，混着演武场上的汗味。林默的肚子不争气地叫了一声，他下意识按住腹部，掌心隔着粗布摸到自己跳得发紧的胃。",
            "灰衣弟子没笑，只把名册往回拖了半寸：“会什么？”这半寸比嘲笑还难受，像是先替他判了不合格。林默喉咙发干，眼角扫见墙边那把秃了半截的扫帚，竹柄上有新磨出来的凹痕。",
            "他本来想说自己会写程序，话到嘴边又咽了回去。这里没人认那个。他低头看了看自己的手，指腹还留着现实里敲键盘磨出的薄茧，只好硬着头皮改口：“扫地，会一点。”",
            "灰衣弟子终于笑了一下，不是善意，是觉得这个人还有点用。他把扫帚踢过来，竹柄撞在林默脚边，发出空心的一声响：“三进院子，晚饭前扫完。扫不干净，饭也别吃。”",
            "林默弯腰去捡，后背却先绷紧了。几个排队的新杂役都在看他，有人憋笑，有人松了口气，像少了一个抢名额的人。他攥住扫帚时，掌心被毛刺扎了一下，那点疼反而让他清醒过来。",
            "他不是来拜师的，至少现在不是。他只是饿，想混过今晚，再找到退出键。可扫帚握进手里的瞬间，演武场那边一声剑鸣擦着风过去，墙角落叶被卷起半圈，又贴着青砖散开。",
            "林默停住了。那不是普通风声，叶片转向太齐，像有人先在地上画了一道看不见的弧线。他抬头，看见练剑的师兄收脚时右肩慢了半拍，鞋底在青砖上蹭出一条短短的灰痕。",
        ]
        * 4
    )
    terse_craft = evaluate_writer_craft(terse)
    expanded_craft = evaluate_writer_craft(expanded)
    terse_quality = evaluate_chapter(terse, min_chars=300, goal="铁剑门入局", required_beats="写出场景压力", constraints="")
    expanded_quality = evaluate_chapter(expanded, min_chars=300, goal="铁剑门入局", required_beats="写出场景压力", constraints="")
    failures: list[str] = []
    if int(terse_craft["checks"].get("scene_expansion") or 0) >= 55:
        failures.append("terse_scene_not_penalized")
    if int(expanded_craft["checks"].get("scene_expansion") or 0) < 70:
        failures.append("expanded_scene_not_rewarded")
    if "scene_expansion_underdeveloped" not in terse_quality.report:
        failures.append("quality_issue_missing")
    payload = {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "terse_scene_expansion": terse_craft["checks"].get("scene_expansion"),
        "expanded_scene_expansion": expanded_craft["checks"].get("scene_expansion"),
        "terse_issues": terse_craft["issues"],
        "expanded_issues": expanded_craft["issues"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
