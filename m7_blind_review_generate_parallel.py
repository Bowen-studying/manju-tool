#!/usr/bin/env python3
"""M7 blind review material generator (parallel, private-mapping mode).

Runs BOTH engines (agent via gpt-5.6-sol, workflow via agnes-2.0-flash) on the
same story inputs, anonymises storyboards (no engine/path/timestamp), emits
reviewer-facing A/B documents, and prints the seed->engine mapping to STDOUT
only (never written to a file; mapping is held privately).

Env:
  LLM_API_BASE / LLM_API_KEY / LLM_MODEL        (agent engine LLM)
  WORKFLOW_LLM_BASE / WORKFLOW_LLM_KEY / WORKFLOW_LLM_MODEL  (workflow LLM)
  HTTPS_PROXY / NO_PROXY
  MANJU_BLIND_PARALLEL (default 3 concurrent pairs)

Usage:
  python m7_blind_review_generate_parallel.py
  python m7_blind_review_generate_parallel.py --only s01
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
REVIEW_DIR = os.environ.get("MANJU_BLIND_REVIEW_DIR", os.path.join(ROOT_DIR, "m7_blind_review_materials"))
RAW_DIR = os.path.join(REVIEW_DIR, "raw")
ANON_DIR = os.path.join(REVIEW_DIR, "匿名评审版")
SAMPLES_DIR = os.environ.get("MANJU_M7_SAMPLES_DIR", os.path.join(ROOT_DIR, "m7_samples"))

INPUTS = [
    ("s01", "s01_short_dialogue.txt"), ("s02", "s02_monologue.txt"),
    ("s03", "s03_silent_action.txt"), ("s04", "s04_special_chars.txt"),
    ("s05", "s05_single_shot.txt"), ("n01", "n01_short_novel.txt"),
    ("n02", "n02_long_novel.txt"), ("n03", "n03_dialogue_novel.txt"),
    ("n04", "n04_scenery_novel.txt"), ("n05", "n05_formatted_novel.txt"),
    ("x01", "x01_duplicate_name.txt"), ("x02", "x02_multi_scene.txt"),
    ("x03", "x03_mixed_cues.txt"), ("x04", "x04_punctuation_storm.txt"),
    ("x05", "x05_eight_roles.txt"),
    ("b01", "b01_railway_station.txt"), ("b02", "b02_midnight_cafe.txt"),
    ("b03", "b03_old_photo.txt"), ("b04", "b04_storm_at_sea.txt"),
    ("b05", "b05_orphanage_letter.txt"),
]

RUNNER = os.environ.get("MANJU_BLIND_REVIEW_RUNNER", os.path.join(ROOT_DIR, "_sb_pair_worker.py"))


def _write_extra_inputs() -> None:
    os.makedirs(os.path.join(REVIEW_DIR, "extra"), exist_ok=True)
    extras = {
        "b01_railway_station.txt": (
            "凌晨四点的火车站，林静拖着行李箱站在空荡的候车大厅。广播里反复播放晚点通知。"
            "一个穿军大衣的老人坐在长椅上，从口袋里掏出一个铝饭盒，打开后递给她一半馒头。"
            "她摇摇头，眼泪却先掉了下来。老人把馒头放在她手边，起身走向检票口。"
            "列车进站时，她把馒头掰成两半，一半放进嘴里，另一半攥在手里。"
        ),
        "b02_midnight_cafe.txt": (
            "午夜十二点的咖啡馆，只剩一个客人。老板娘擦着杯子，问他是不是又加班。"
            "他没回答，只盯着窗外的雨。墙上挂钟指向十二点零五分。"
            "老板娘端来一杯热牛奶，说今晚这杯算她的。他喝了一口，突然笑了，说原来你还记得。"
            "雨停的时候，他把零钱压在杯底，推门离开，没有回头。"
        ),
        "b03_old_photo.txt": (
            "搬家那天，陈默在旧书箱底翻出一张泛黄的照片。照片里一家三口站在老屋前，"
            "母亲扎着辫子，父亲抱着他，他手里举着一串糖葫芦。照片背面写着'1998年春天'。"
            "他摩挲着照片，想起母亲去年去世前一直念叨着老屋前的石榴树。"
            "傍晚他开车回到老屋，树还在，房子已经拆了一半。他把照片夹进石榴树的树缝里，"
            "对着空荡荡的院子站了很久。"
        ),
        "b04_storm_at_sea.txt": (
            "风暴来临前的海面异常平静。老船长站在甲板上，望着远处压过来的黑云。"
            "水手们忙着收帆，只有见习生阿远愣在原地。船长把望远镜塞给他，说看仔细了，"
            "这片海明天就不一样了。黑云吞没最后一丝光时，浪头第一次拍上船舷。"
            "阿远抓紧桅杆，看见船长在驾驶舱里稳稳地握着舵轮，像一尊雕像。"
            "天亮时海面重归平静，阿远在航海日志上写下：风暴过去了，船还在。"
        ),
        "b05_orphanage_letter.txt": (
            "孤儿院院长把一封信交给小雨，说是十年前有人留下的。信封上没有署名，"
            "只写着'给小雨'。信里夹着一张褪色的合影，和一个地址。"
            "小雨按地址找到一座小城，敲开那扇门，开门的老太太愣了一下，"
            "颤抖着问她是不是叫小雨。小雨点点头，老太太把她拉进屋里，"
            "墙上的相框里，正是那张合影里的年轻女人。"
        ),
    }
    for name, content in extras.items():
        path = os.path.join(REVIEW_DIR, "extra", name)
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)


def _anonymise_storyboard(storyboard: dict) -> str:
    lines: list[str] = []
    for scene in storyboard.get("scenes", []):
        heading = scene.get("heading") or scene.get("scene_heading") or "场景"
        lines.append(f"【场景】{heading}")
        for shot in scene.get("shots", []):
            visual = shot.get("visual", {})
            desc = visual.get("description", "") if isinstance(visual, dict) else shot.get("visual_description", "")
            lines.append(f"  镜头: {desc}")
            audio = shot.get("audio", {}) if isinstance(shot.get("audio"), dict) else {}
            if audio.get("dialogue"):
                lines.append(f"    对白({audio.get('speaker','')}): {audio['dialogue']}")
            if audio.get("narration"):
                lines.append(f"    旁白: {audio['narration']}")
    return "\n".join(lines)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", help="兼容旧用法：直接给出样本 ID")
    parser.add_argument("--only", action="append", default=[], help="只运行一个样本 ID；可重复")
    parser.add_argument("--review-dir", default=None)
    parser.add_argument("--samples-dir", default=None)
    parser.add_argument("--runner", default=None)
    parser.add_argument("--mapping-output", default=None, help="可选：保存私有机器可读映射")
    parser.add_argument("--seed", type=int, default=None, help="可复现的 A/B 随机种子")
    parser.add_argument("--llm-api-base", default=None)
    parser.add_argument("--https-proxy", default=None, help="可选代理；默认不设置代理")
    parser.add_argument("--no-proxy", action="store_true")
    parsed = parser.parse_args()

    global REVIEW_DIR, RAW_DIR, ANON_DIR, SAMPLES_DIR, RUNNER
    if parsed.review_dir:
        REVIEW_DIR = os.path.abspath(parsed.review_dir)
        RAW_DIR = os.path.join(REVIEW_DIR, "raw")
        ANON_DIR = os.path.join(REVIEW_DIR, "匿名评审版")
    if parsed.samples_dir:
        SAMPLES_DIR = os.path.abspath(parsed.samples_dir)
    if parsed.runner:
        RUNNER = os.path.abspath(parsed.runner)
    only = set(parsed.only or parsed.ids)

    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(ANON_DIR, exist_ok=True)
    _write_extra_inputs()

    targets = [(iid, name) for iid, name in INPUTS if not only or iid in only]

    # Base env for every worker: agent engine LLM + workflow LLM + proxy/no-proxy.
    base_env = dict(os.environ)
    llm_base = parsed.llm_api_base or os.environ.get("LLM_API_BASE", "")
    if not llm_base:
        print("LLM_API_BASE is required for the agent worker; no local proxy default is used", file=sys.stderr)
        return 2
    llm_key = os.environ.get("LLM_API_KEY", "")
    llm_model = os.environ.get("LLM_MODEL", "gpt-5.6-sol")
    wf_base = os.environ.get("WORKFLOW_LLM_BASE", "https://apihub.agnes-ai.com/v1")
    wf_key = os.environ.get("WORKFLOW_LLM_KEY", "")
    wf_model = os.environ.get("WORKFLOW_LLM_MODEL", "agnes-2.0-flash")
    base_env.update({
        "LLM_API_BASE": llm_base, "LLM_API_KEY": llm_key, "LLM_MODEL": llm_model,
        "WORKFLOW_LLM_BASE": wf_base, "WORKFLOW_LLM_KEY": wf_key,
        "WORKFLOW_LLM_MODEL": wf_model,
    })
    base_env.pop("HTTPS_PROXY", None)
    base_env.pop("https_proxy", None)
    proxy = "" if parsed.no_proxy else (parsed.https_proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy", ""))
    if proxy:
        base_env["HTTPS_PROXY"] = proxy
        base_env["https_proxy"] = proxy

    parallel = int(os.environ.get("MANJU_BLIND_PARALLEL", "2"))
    # Per-engine wall-clock timeout; a hung LLM call must not stall the queue.
    engine_timeout = {"agent": 1500, "workflow": 420}

    # Queue of (input_id, engine) pairs; a worker per pair so each process
    # has a clean LLM configuration (no get_ai_config cache cross-talk).
    todo_queue = [(iid, eng) for iid, _ in targets for eng in ("agent", "workflow")]
    # Skip pairs that already produced a storyboard (crash-resume safe).
    queue = []
    for iid, eng in todo_queue:
        sb = os.path.join(RAW_DIR, iid, eng, "storyboard.json")
        if os.path.isfile(sb):
            print(f"[skip] {iid}/{eng} already done", flush=True)
        else:
            queue.append((iid, eng))
    attempts: dict[tuple[str, str], int] = {}
    procs: list[tuple[str, str, subprocess.Popen, float]] = []
    raw_results: dict[str, dict[str, dict]] = {}
    while queue or procs:
        while len(procs) < parallel * 2 and queue:
            iid, eng = queue.pop(0)
            name = dict(INPUTS)[iid]
            src = os.path.join(SAMPLES_DIR if not iid.startswith("b") else os.path.join(REVIEW_DIR, "extra"), name)
            outdir = os.path.join(RAW_DIR, iid)
            env = dict(base_env)
            env["SB_INPUT"] = src
            env["SB_OUTPUT"] = outdir
            env["SB_ENGINE"] = eng
            p = subprocess.Popen([sys.executable, RUNNER], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace")
            procs.append((iid, eng, p, time.monotonic()))
            print(f"[start] {iid}/{eng}", flush=True)
        done: list[tuple[str, str, subprocess.Popen]] = []
        now = time.monotonic()
        for iid, eng, p, started in procs:
            code = p.poll()
            if code is not None:
                out, _ = p.communicate(timeout=5)
                print(f"[done] {iid}/{eng} code={code}", flush=True)
                try:
                    md = json.loads(out.strip().splitlines()[-1])
                except Exception:
                    md = {"engine": eng, "error": "parse-failed"}
                raw_results.setdefault(iid, {})[eng] = md
                done.append((iid, eng, p))
            elif now - started > engine_timeout[eng]:
                p.kill()
                out, _ = p.communicate(timeout=5)
                print(f"[timeout] {iid}/{eng} killed after {int(now - started)}s", flush=True)
                raw_results.setdefault(iid, {})[eng] = {"engine": eng, "error": "timeout"}
                done.append((iid, eng, p))
        for item in done:
            try:
                procs.remove(item)
            except ValueError:
                # A reader-thread decode failure may have already removed it.
                procs = [p for p in procs if p[0] != item[0] or p[1] != item[1]]
            iid, eng, _ = item
            md = raw_results.get(iid, {}).get(eng, {})
            ok = isinstance(md, dict) and not md.get("error") and os.path.isfile(os.path.join(RAW_DIR, iid, eng, "storyboard.json"))
            attempts[(iid, eng)] = attempts.get((iid, eng), 0) + 1
            if not ok and attempts[(iid, eng)] < 2:
                queue.append((iid, eng))
                print(f"[requeue] {iid}/{eng} attempt {attempts[(iid, eng)]}", flush=True)
        if procs:
            time.sleep(5)

    results: dict[str, dict[str, dict]] = {}
    for iid, _ in targets:
        got = raw_results.get(iid, {})
        if isinstance(got.get("agent"), dict) and isinstance(got.get("workflow"), dict):
            results[iid] = got

    # A/B randomisation with recorded seed; mapping printed to stdout ONLY.
    seed = parsed.seed if parsed.seed is not None else int.from_bytes(os.urandom(4), "big")
    rng = random.Random(seed)
    mapping: dict[str, dict] = {"seed": seed, "pairs": {}}
    order: list[tuple[str, dict[str, str]]] = []
    for idx, (iid, _) in enumerate(targets, start=1):
        if iid not in results or not results[iid].get("agent") or not results[iid].get("workflow"):
            continue
        label = f"组{idx:02d}"
        left_engine = "agent" if rng.random() < 0.5 else "workflow"
        right_engine = "workflow" if left_engine == "agent" else "agent"
        mapping["pairs"][label] = {"input_id": iid, "A": left_engine, "B": right_engine}
        order.append((label, results[iid], left_engine, right_engine))

    for label, pair, left_engine, right_engine in order:
        anon = os.path.join(ANON_DIR, f"{label}.md")
        left = _anonymise_storyboard(pair[left_engine])
        right = _anonymise_storyboard(pair[right_engine])
        with open(anon, "w", encoding="utf-8") as handle:
            handle.write(f"# {label}\n\n")
            handle.write("## A\n\n" + left + "\n\n")
            handle.write("## B\n\n" + right + "\n")

    print("=== MAPPING (private, do not share) ===")
    print(json.dumps(mapping, ensure_ascii=False))
    mapping_output = parsed.mapping_output or os.environ.get("M7_BLIND_MAPPING_OUTPUT", "")
    if mapping_output:
        mapping_output = os.path.abspath(mapping_output)
        os.makedirs(os.path.dirname(mapping_output), exist_ok=True)
        with open(mapping_output, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": "m7-blind-review-mapping-v1", **mapping}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(f"generated {len(order)} pairs -> {ANON_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
