#!/usr/bin/env python3
"""数字人群会话巡检器。

设计文档：https://joyspace.jd.com/pages/LvJn2RsNAkrUrqfzwzyZ

当前进度：
- 功能 1：定时任务骨架（可配置轮询间隔 + 收到信号干净退出）
- 功能 2：发现巡检对象（有哪些交付数字人、哪些群、每个群里有哪些会话）
- 功能 3：判断会话是否在执行中（方案判定树【步骤 1】，整棵树的分岔点）
- 功能 4：进行中提醒 REMIND_LONG_RUNNING（只判定并打印，还不真的发消息）
- 功能 5：真的把消息发出去（dryRun 闸门 + 群白名单）
- 功能 6：降噪链（signature 去重 + 同类 30min 冷却 + 同群限流 + quietHours）
- 功能 7：联调友好（单例锁 / 代码改动自动重载 / 配置热加载）
- 功能 8：模型异常中断 ALERT_MODEL_ERROR（idle 分支第一条告警）
- 功能 9：发送前复查（判定与投递之间状态会变，过时的消息不发）
- 功能 10：异常留痕（完整堆栈+上下文落盘，单群失败隔离，--errors 查看）
- 功能 11：用户消息未获回复 ALERT_USER_NOT_REPLIED（铁律二-1，含「消息被吞」子形态）
- 功能 12：两条派活铁律 ALERT_SUB_NOT_REPORTED / ALERT_DA_NOT_REPLIED_AFTER_SUB
- 功能 13：等用户回答时不提醒（球在用户脚下，交付流程里 66/90 次对客都属这种）
- 功能 14：文案分清"在等用户"和"在等基础 Agent"（判定与文案共用同一守卫）
- 功能 15：CC 卡死检测 ALERT_CC_STALLED + 补上方案的信号 4（旧实现是纯空壳）
- 功能 16：唤起数字人（5 类 ALERT，默认关闭；唤起消息不算用户发言，防自问自答）
- 功能 17：唤起送达按**证据**判定（delivered / failed / unconfirmed 三态 + 后台补报）
- 功能 18：告警尾句由 wake.enabled 真实状态生成（正文不许自己承诺"正在补处理"）
- 功能 19：运行时注入的 role=user 消息不算用户发言（靠 __openclaw 结构判据）
- 功能 20：标出"仅调试阈值触发"的事件（联调期不用手工回放去分辨真假）
- 功能 21：remind 总开关（方案 §6.3 列了但一直是 0 引用的空承诺）

真实端到端跑通的（有数据铁证）：
- REMIND_LONG_RUNNING 69 次 / ALERT_MODEL_ERROR 20 次 / ALERT_SUB_NOT_REPORTED 2 次
- 唤起：5 条真实落地，其中 ALERT_MODEL_ERROR 触发 2 条、ALERT_SUB_NOT_REPORTED 触发 3 条
- 投递失败重试：真实网络故障下连续 15 轮失败 → 网好后补发成功，期间不记 notified
- 单例锁 / SIGTERM / SIGINT / execv 热重载

只有断言+回放、**一次没真触发过**的（别当成验过了）：
- ALERT_CC_STALLED、ALERT_DA_NOT_REPLIED_AFTER_SUB、ALERT_USER_NOT_REPLIED
  （含「消息被吞」子形态）
- 同群限流 —— 唯一防刷屏的闸门，调试配置 GROUP_RATE_MAX=60 使它从未被触发
- quietHours 真实拦截；唤起的 failed / unconfirmed / 后台补报三条分支
- 生产阈值：至今只在调试值下跑过（interval 5s vs 30s、USER_QUIET_MS 8s vs 300s）

尚未实现：
- WARN_WORKFLOW_SILENT（方案判定树里有，只要求打日志）
- 管理端配置下发 / 轮询服务端公共配置、模型兜底判定（方案说"默认零模型"，可选）

**已确认废弃，不要再实现**：
- ALERT_ABORTED_BY_RESTART —— 新方案动作表里已删除，2026-08-25 与需求方确认作废。
  这类故障由 ALERT_MODEL_ERROR 覆盖（session.ended.status=error 那一支）。
"""

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime

# 运行时目录。配置、状态、日志都放这里，不写 ~/.openclaw（巡检器对 openclaw 只读）。
STATE_DIR = os.path.expanduser("~/.openclaw-inspector")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")

# 被巡检的 openclaw 安装根目录。每个数字人在这下面占一个同名子目录。
OPENCLAW_HOME = os.path.expanduser("~/.openclaw")

# 配置默认值。config.json 里写了哪个键就覆盖哪个，没写的用这里的值。
DEFAULT_CONFIG = {
    "enabled": True,     # 总开关，false 时启动即退出
    "interval": 30000,   # 轮询间隔（毫秒），方案默认 30s
}


def log(msg):
    """带时间戳打一行日志到 stdout；配了 log.file 就同时落盘（带轮转）。

    时间戳带完整日期：巡检器是常驻进程、部署在服务器上长期运行，日志跨天甚至跨年，
    只打时分秒时排查跨天问题（"这条 16:06 的告警是今天还是昨天的"）对不上，
    轮转后的历史文件（inspector.log.1/.2/…）更是没法定位到哪天。
    落盘失败只提示一次性的错，不抛 —— 日志写不进去也不该让巡检停摆。
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if not _log_file:
        return
    try:
        rotate_file(_log_file, _log_max_bytes, _log_keep)
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[日志落盘失败] {_log_file}：{e!r}", flush=True)


def apply_log_settings(cfg):
    """按配置设定主日志落盘参数。返回 (文件路径, 上限字节, 保留份数)。

    单独抽出来是因为配置支持热加载 —— 改了 log 段要能立刻生效，
    不然改完得重启，就跟"热加载"这个卖点自相矛盾了。
    """
    global _log_file, _log_max_bytes, _log_keep
    section = (cfg or {}).get("log") or {}
    path = section.get("file")
    _log_file = os.path.expanduser(str(path)) if path else ""
    raw_max = section.get("maxBytes")
    _log_max_bytes = (int(raw_max) if isinstance(raw_max, (int, float)) and raw_max > 0
                      else LOG_MAX_BYTES_DEFAULT)
    raw_keep = section.get("keep")
    _log_keep = (int(raw_keep) if isinstance(raw_keep, (int, float)) and raw_keep >= 0
                 else LOG_KEEP_DEFAULT)
    return _log_file, _log_max_bytes, _log_keep


def json_object(text):
    """把一段 JSON 文本解析成 dict；不是合法 JSON、或解析出来不是对象，都返回 None。

    "解析成功但不是对象"这一种必须显式挡住，不能只 catch ValueError：
    实测（2026-08-21 群 10232848092）有 exec 的结果文本是裸数字 '239' / '11078'
    （看着像 wc -l 之类的输出），json.loads 给出的是 int，后面再对它做
    `"sent" in result` 就抛 TypeError: argument of type 'int' is not iterable，
    整轮巡检挂掉。而且倒序扫描什么时候扫到那条消息取决于文件尾部内容，所以故障
    是间歇性的（实测第 299/300 轮挂、301 轮又好了），最难查的那种。

    所有读外部 JSON 的地方都走这里，别各自 json.loads 再各自判类型。
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def json_object_from_file(path):
    """读一个文件并解析成 dict；读不了或不是对象都返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json_object(f.read())
    except OSError:
        return None


# ======================== 功能 10：异常留痕 ========================
#
# 巡检器要长期无人值守地跑，异常必须留下**能直接定位**的痕迹，否则出问题只能靠猜。
#
# 实测的反面教材（2026-08-21 第 299/300 轮）：日志里只有
#   TypeError("argument of type 'int' is not iterable")
# 没有堆栈、没有出错的群、没有出错的阶段，只能靠人肉推理找。而且它是间歇性的
# （倒序扫描什么时候读到那条坏数据取决于文件尾部内容），终端往上一滚就没了。
#
# 所以：完整堆栈 + 上下文 + 落盘到 errors.jsonl（append-only，重启不丢）。

ERRORS_PATH = os.path.join(STATE_DIR, "errors.jsonl")

# 落盘上限，防止某个必然失败的分支把磁盘写满。
ERRORS_MAX_BYTES = 4 * 1024 * 1024

# 轮转默认值。巡检器是常驻进程，5 秒一轮、每轮至少一行，一天就是一万七千行；
# 不轮转的话磁盘迟早被写满，而且真出事时想翻日志会被历史噪音埋掉。
LOG_MAX_BYTES_DEFAULT = 10 * 1024 * 1024
LOG_KEEP_DEFAULT = 5


def rotate_file(path, max_bytes, keep):
    """按大小轮转一个日志文件，并把超出保留份数的历史**删掉**。返回是否轮转了。

    命名沿用最常见的约定，方便配合外部工具：
        path → path.1 → path.2 → … → path.<keep>，再老的删除

    为什么要显式删而不是只改名：只留一份（原实现那样直接 os.replace 到 .1）会让
    上一份历史被静默覆盖，排查跨天问题时刚好差那一份；而完全不删又会无限长大。

    keep <= 0 表示不留历史，超限直接截断。
    整段兜住异常：轮转失败不能反过来把巡检搞挂，最坏情况是这次没转成，下次再试。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return False
        if keep <= 0:
            os.remove(path)
            return True
        # 从最老的往回挪，避免覆盖：先删掉将要超出保留份数的那一份
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(keep - 1, 0, -1):
            src, dst = f"{path}.{i}", f"{path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
        return True
    except OSError as e:
        # 用 print 而不是 log()：log() 自己会调轮转，出错时互相递归就麻烦了
        print(f"[轮转失败] {path}：{e!r}", flush=True)
        return False


def cleanup_rotated(path, keep):
    """删掉 path.<n> 里 n 超过 keep 的历史文件。返回删掉的份数。

    正常轮转时 rotate_file 已经会顺手删；这个函数管的是**把 keep 调小之后**留下的
    存量（原来留 20 份、改成留 5 份，那 6~20 份不会有人再碰它们）。启动时跑一次。
    """
    removed = 0
    try:
        for name in os.listdir(os.path.dirname(path) or "."):
            full = os.path.join(os.path.dirname(path) or ".", name)
            if not full.startswith(path + "."):
                continue
            suffix = full[len(path) + 1:]
            if not suffix.isdigit():
                continue          # 不是轮转产物（比如 .bak），别乱删
            if int(suffix) > max(keep, 0):
                os.remove(full)
                removed += 1
    except OSError as e:
        print(f"[清理历史日志失败] {path}：{e!r}", flush=True)
    return removed


# 主日志的落盘配置。由 main() 按配置设定，配置热加载时同步刷新。
# 默认不落盘 —— 手工在终端跑时输出已经在眼前，再写一份纯属浪费。
_log_file = ""
_log_max_bytes = LOG_MAX_BYTES_DEFAULT
_log_keep = LOG_KEEP_DEFAULT


def record_error(exc, **context):
    """把一次异常连堆栈和上下文追加到 errors.jsonl，并在日志里打一行摘要。

    context 里传出错时能拿到的一切定位信息（轮次、数字人、群号、阶段…）。
    自己写盘失败也不能反过来把巡检搞挂，所以整段兜住。
    """
    tb = traceback.format_exc()
    summary = f"{type(exc).__name__}: {exc}"
    where = "　".join(f"{k}={v}" for k, v in context.items() if v is not None)
    log(f"⚠️ 异常：{summary}　[{where}]　详情见 {ERRORS_PATH}")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        # 超上限就轮转。用共用的 rotate_file 留多份 —— 原来直接 os.replace 到 .1
        # 只留一份，上一份历史会被静默覆盖，排查跨天问题时刚好差那一份。
        rotate_file(ERRORS_PATH, ERRORS_MAX_BYTES, _log_keep)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "error": summary,
            "type": type(exc).__name__,
            **context,
            "traceback": tb.strip().splitlines(),
        }
        with open(ERRORS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as write_failure:      # noqa: BLE001 —— 留痕失败不能影响巡检
        log(f"⚠️ 异常留痕本身失败了：{write_failure!r}")


def print_errors(limit=20):
    """把最近的异常打出来看，供事后排查（--errors 入口）。"""
    if not os.path.exists(ERRORS_PATH):
        print(f"没有异常记录：{ERRORS_PATH} 不存在（说明一次都没出过错）")
        return
    rows = []
    try:
        with open(ERRORS_PATH, encoding="utf-8") as f:
            for line in f:
                record = json_object(line.strip())
                if record:
                    rows.append(record)
    except OSError as e:
        print(f"读取异常记录失败：{e!r}")
        return
    if not rows:
        print(f"异常记录为空（{ERRORS_PATH}）—— 没有出过错。")
        return
    print(f"共 {len(rows)} 条异常记录，显示最近 {min(limit, len(rows))} 条："
          f"（完整文件 {ERRORS_PATH}）")
    counter = {}
    for record in rows:
        counter[record.get("error", "?")] = counter.get(record.get("error", "?"), 0) + 1
    print("\n=== 按异常去重统计 ===")
    for err, count in sorted(counter.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4} 次  {err}")
    print("\n=== 最近若干条明细 ===")
    for record in rows[-limit:]:
        ctx = "　".join(f"{k}={v}" for k, v in record.items()
                        if k not in ("ts", "error", "type", "traceback"))
        print(f"\n  [{record.get('ts')}] {record.get('error')}")
        if ctx:
            print(f"    上下文 {ctx}")
        for line in (record.get("traceback") or [])[-6:]:
            print(f"    {line}")


def load_config(path):
    """读配置文件，和默认值合并。文件不存在或读坏了都退回默认值，不让巡检器起不来。"""
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(path):
        log(f"配置文件不存在，使用默认配置：{path}")
        return cfg
    user_cfg = json_object_from_file(path)
    if user_cfg is None:
        log(f"配置文件读不了或不是 JSON 对象，使用默认配置：{path}")
        return cfg
    cfg.update(user_cfg)
    log(f"已加载配置：{path}")
    return cfg


def inspect_once(cfg, round_no):
    """跑一轮巡检：发现对象 → 白名单过滤 → 逐群走判定树 → 发送（受 dryRun 约束）。

    每个群单独兜异常：一个群的数据有问题不能拖垮整轮。
    实测踩过（2026-08-21 第 299/300 轮）：某个群的 exec 结果里有一条裸数字，
    解析时抛 TypeError，当时异常兜在轮级别，结果**那一轮 4 个群全被跳过**——
    一条坏数据让整台机器的巡检停摆两轮。
    """
    humans = discover_digital_humans()
    delivery = [h for h in humans if h["isDelivery"]]
    notified = NotifiedStore()
    scanned = skipped = failed = 0
    acted, suppressed = [], []
    # 先把上几轮放后台的唤起的最终结果补报出来，别让日志停在"未确认"
    acted.extend(line for _, line in report_pending_wakes())
    for human in delivery:
        for group_id, sessions in sorted(human["groups"].items()):
            if not group_allowed(cfg, group_id):
                skipped += 1
                continue
            scanned += 1
            try:
                events, _ = decide(human, group_id, sessions, cfg)
                if events:
                    for kind, line in handle_events(human, group_id, sessions,
                                                    events, cfg, notified):
                        (acted if kind == "acted" else suppressed).append(line)
            except Exception as e:              # noqa: BLE001 —— 单群失败要隔离
                failed += 1
                record_error(e, round=round_no, daId=human["daId"], groupId=group_id,
                             phase="decide/handle")
    notified.save()

    mode = "dryRun" if (cfg or {}).get("dryRun", True) else "实发"
    log(f"第 {round_no} 轮［{mode}］：交付数字人 {len(delivery)} 个 / 巡检群 {scanned} 个"
        f"（白名单外跳过 {skipped} 个"
        f"{f'，出错 {failed} 个' if failed else ''}）"
        f"→ 动作 {len(acted)} 条"
        f"{f'，被降噪拦下 {len(suppressed)} 条' if suppressed else ''}")
    # 真发/发失败每次都打；被闸门拦下的只在**变化时**打一次，避免持续静默期间刷屏
    for line in acted:
        log(f"  {line}")
    global _last_suppressed
    if suppressed and suppressed != _last_suppressed:
        for line in suppressed:
            log(f"  {line}")
        log("  （以上被拦下的情况若持续不变，后续轮次不再重复打印）")
    _last_suppressed = suppressed


# 上一轮被降噪拦下的说明，用来判断"情况有没有变"，决定要不要再打一遍日志。
_last_suppressed = []


# ============================ 功能 2：发现巡检对象 ============================
#
# 目标：回答"这台机器上有哪些交付数字人的群，每个群里有哪些会话文件要读"。
#
# 三层关系全部从磁盘布局和 sessionKey 里解出来，不依赖任何注册表文件：
#
#   ~/.openclaw/<da>/                          ← 一个数字人实例（有 openclaw.json）
#     private-experts/<da>/AGENTS.md            ← 判断是不是"交付数字人"
#     agents/<da>/sessions/sessions.json        ← 数字人本体的会话
#     agents/<sub>/sessions/sessions.json       ← 基础 Agent 的会话
#
# sessionKey 里带着群号，所以同一个群的三方会话能直接对齐：
#   数字人本体   agent:zqjzszr:jingme:group:10232767188
#   基础 Agent   agent:cangjie:jingme:group-virtual:10232767188:zqjzszr

# 交付工作流定义文件的形态 —— 这是认定交付数字人的**唯一判据**。
# 实测（2026-08-29 三个实例）：
#   private-experts/<da>/workflow/zqjz-full-delivery.json
#   private-experts/<da>/workflow/zqjz-delivery-clarify-standalone.json
#   private-experts/<da>/workflow/zqjz-bugfix-flow.json
# 看目录而不是看名单：工作流改名、新增都不用回来改代码。
# 按前缀 + 扩展名双重限定，不能只看前缀 —— 目录里可能混进 .bak / .disabled 之类。
# 历史：曾经的判据是在 AGENTS.md 正文里搜写死的工作流名字（zqjz-full-delivery /
# zqjz-delivery-clarify-standalone），2026-08-29 与需求方确认废弃 —— 文档提及
# 不等于能力实存，且名单跟不上改名。
DELIVERY_WORKFLOW_PATTERNS = ("zqjz-*.json", "zqjz-*.yaml", "zqjz-*.yml")

# sessionKey 里表示"这是个群会话"的段。
GROUP_SEGMENTS = ("group", "group-virtual")


def parse_group_id(session_key):
    """从 sessionKey 里解出群号；不是群会话就返回 None。

    要处理三种实际存在的形态（群号都是紧跟在 group 段之后的第一个纯数字段）：
      agent:zqjzszr:jingme:group:10232767188                      数字人本体
      agent:cangjie:jingme:group-virtual:10232767188:zqjzszr       基础 Agent
      agent:shenkuo:jingme:group:dw.zqjz.ts1:group:10232767188     带账号段的变体
    """
    parts = session_key.split(":")
    for i, seg in enumerate(parts):
        if seg in GROUP_SEGMENTS:
            for later in parts[i + 1:]:
                if later.isdigit():
                    return later
    return None


def delivery_workflows(inst_dir, da_id):
    """列出这个数字人 workflow/ 目录下的交付工作流定义文件名，没有返回空列表。

    路径必须按 da_id 限定到 private-experts/<da_id>/workflow/ —— 实测同一实例下
    还挂着别的专家目录（zqjzszr 实例里有 private-experts/zhyyszr/），扫整个
    private-experts 会把别人的工作流算到这个数字人头上。
    """
    wf_dir = os.path.join(inst_dir, "private-experts", da_id, "workflow")
    names = set()
    for pattern in DELIVERY_WORKFLOW_PATTERNS:
        for path in glob.glob(os.path.join(wf_dir, pattern)):
            names.add(os.path.basename(path))
    return sorted(names)


def is_delivery_agent(inst_dir, da_id):
    """判断是不是交付数字人：private-experts/<da>/workflow/ 下有 zqjz- 开头的
    工作流定义文件（能力实存）。目录不存在、没有匹配文件都算不是。"""
    return bool(delivery_workflows(inst_dir, da_id))


def activity_ts(entry):
    """一条 session 最近一次活动的时间戳（epoch 毫秒），取不到返回 0。

    取所有候选字段里**最大的**，不是"第一个存在的"。
    实测踩过（2026-08-21 群 10232848092）：数字人 17:22:26→17:23:02 真跑了 36 秒，
    同一条记录里 startedAt / endedAt / updatedAt / lastInteractionAt 全是 17:2x，
    唯独 lastActivityAt 停在 2 小时前的 15:25:21。原先按固定顺序取第一个存在的字段，
    正好抓住那个唯一过期的值，把旁边 4 个新鲜的全扔了 —— 结果步骤 0 判成"82 分钟
    没活动"的僵尸群，整个群被跳过，那 36 秒的运行完全没被巡检到。

    步骤 0 问的是"最近有没有发生过任何事"，取最大值才符合这个语义；
    方案说 updatedAt"不代表活动"是对的，但那只意味着不能拿它当精确的活动时刻，
    不意味着可以在它明显更新时无视它。
    """
    candidates = []
    for field in ("lastActivityAt", "lastInteractionAt", "endedAt", "startedAt", "updatedAt"):
        value = entry.get(field)
        if isinstance(value, (int, float)) and value > 0:
            candidates.append(int(value))
    return max(candidates) if candidates else 0


def read_sessions(sessions_json):
    """读一个 agent 的 sessions.json，返回 {sessionKey: entry}。读不了就返回空。"""
    data = json_object_from_file(sessions_json)
    if data is None:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def collect_group_sessions(inst_dir, da_id):
    """读一个数字人实例下所有 agent 的 sessions.json，按群号归拢成 {群号: [会话]}。

    单独抽出来，是为了让"发送前复查"能重新读一次盘 —— 复查必须和判定用同一份新鲜
    数据、同一把尺子，详见 still_relevant() 的注释。
    """
    groups = {}
    pattern = os.path.join(inst_dir, "agents", "*", "sessions", "sessions.json")
    for sessions_json in sorted(glob.glob(pattern)):
        agent_id = sessions_json.split(os.sep)[-3]
        role = "数字人" if agent_id == da_id else "基础Agent"
        for session_key, entry in read_sessions(sessions_json).items():
            group_id = parse_group_id(session_key)
            if group_id is None:
                continue  # 私聊等非群会话，本方案不巡检
            groups.setdefault(group_id, []).append({
                "agentId": agent_id,
                "role": role,
                "sessionKey": session_key,
                "sessionId": entry.get("sessionId") or "",
                "status": entry.get("status"),
                "activityTs": activity_ts(entry),
                # 网关侧记的"最后一次跟用户交互"。它和消息流里的 tUser 正常只差 0~1 秒
                # （实测 4 个群都是 -1~0 秒）；差很多就说明网关收到了消息、但它没进会话，
                # 是"消息被吞"的可判定信号。
                "lastInteractionAt": entry.get("lastInteractionAt") or 0,
                "sessionFile": entry.get("sessionFile") or "",
                "startedAt": entry.get("startedAt") or 0,
                # 发消息要用的路由信息。lastTo 是最近一次实际投递的目标，
                # 没有就退回 route.target.to。
                "channel": entry.get("channel") or entry.get("lastChannel") or "",
                "target": entry.get("lastTo")
                          or ((entry.get("route") or {}).get("target") or {}).get("to")
                          or "",
                # 空壳会话：建过但从没跑过（没有 startedAt/status）。
                # 它没有任何运行痕迹，后面的判定必须跳过，否则会被当成异常。
                "isStub": entry.get("startedAt") is None and entry.get("status") is None,
            })
    # 每个群内按最近活动排序，数字人本体排在最前，方便阅读。
    for sessions in groups.values():
        sessions.sort(key=lambda s: (s["role"] != "数字人", -s["activityTs"]))
    return groups


def discover_digital_humans():
    """扫出这台机器上所有数字人，及其群会话。

    返回一个列表，每项形如：
      {
        "daId": "zqjzszr",
        "instDir": "/Users/x/.openclaw/zqjzszr",
        "isDelivery": True,          # 是否交付数字人
        "hasOwnSessions": True,      # 数字人本体是否跑过（agents/<da> 是否存在）
        "groups": {群号: [会话, ...]},
      }
    """
    humans = []
    for inst_dir in sorted(glob.glob(os.path.join(OPENCLAW_HOME, "*"))):
        da_id = os.path.basename(inst_dir)
        # 数字人实例的标志：目录里有 openclaw.json。~/.openclaw 下还有 bin/ node/ 这类
        # 公共目录，靠这个把它们排除掉。
        if not os.path.isfile(os.path.join(inst_dir, "openclaw.json")):
            continue
        humans.append({
            "daId": da_id,
            "instDir": inst_dir,
            "isDelivery": is_delivery_agent(inst_dir, da_id),
            "hasOwnSessions": os.path.isdir(os.path.join(inst_dir, "agents", da_id)),
            "agentNames": load_agent_names(inst_dir),
            "sendScript": resolve_send_script(inst_dir, da_id),
            "groups": collect_group_sessions(inst_dir, da_id),
        })
    return humans


# 对客发送脚本在数字人自己的 workspace 下。必须用"这个数字人目录下的那一份"：
# 脚本会从自身路径往上找含 openclaw.json + state/ 的最近祖先，据此自锁到对应实例、
# 自己推导 OPENCLAW_CONFIG_PATH / STATE_DIR / 网关端口。换句话说"调哪份脚本"就
# 决定了"以哪个数字人的身份发"，巡检器不需要自己维护实例注册表或探测端口。
SEND_SCRIPT_RELPATH = os.path.join(
    "skills", "zqjz-agent-runtime-specs", "scripts", "send-user-message.py")


def resolve_send_script(inst_dir, da_id):
    """定位这个数字人的 send-user-message.py，找不到返回空串。"""
    path = os.path.join(inst_dir, "private-experts", da_id, SEND_SCRIPT_RELPATH)
    return path if os.path.isfile(path) else ""


def openclaw_python():
    """openclaw 自带的 python，没有就退回跑巡检器的这个解释器。"""
    bundled = os.path.join(OPENCLAW_HOME, "python", "bin", "python3")
    return bundled if os.path.isfile(bundled) else sys.executable


def stable_cwd():
    """给子进程用的工作目录 —— 必须是**当下按路径名查得到**的目录，不能靠继承。

    实测 2026-08-24：终端 shell 待在一个后来被删掉重建的目录里（同名不同 inode，
    lsof 照样打印出路径，所以肉眼完全看不出问题），巡检器从那里启动，继承到的 cwd
    指向已删除的那个 inode。openclaw 是 Node 写的，启动先 process.cwd()，于是每一次
    投递都在最外层就挂掉：

        shell-init: error retrieving current directory: getcwd: cannot access
                    parent directories: No such file or directory
        [openclaw] Reason: ENOENT: process.cwd failed ... uv_cwd

    两个坑叠在一起才这么难查：
      · 报错在 send-user-message.py 内部调 openclaw 那一层，日志里露出来的是
        "openclaw message send 失败"，看着像投递方式不对，其实和投递毫无关系；
      · reload_self 用 execv，**cwd 会原样继承下去**，所以改代码热重载也修不掉。

    巡检器是常驻进程，跑几天几周，不能把自己的可用性押在启动时那个目录上。
    """
    for path in (STATE_DIR, os.path.expanduser("~"), "/"):
        if path and os.path.isdir(path):
            return path
    return "/"


# openclaw 可执行文件。装在 <root>/bin 下，不在就回落 PATH。
OPENCLAW_BIN = (os.path.join(OPENCLAW_HOME, "bin", "openclaw")
                if os.path.isfile(os.path.join(OPENCLAW_HOME, "bin", "openclaw"))
                else "openclaw")

# 多数字人同机运行，每个实例有独立 state 目录和**动态端口**的 gateway。
# 直接调 openclaw CLI 必须显式锁定实例，否则会回落到 ~/.openclaw 根、连到错的数字人。
# 这一点和 send-user-message.py 不同 —— 那个脚本部署在数字人目录下、能自锁；
# openclaw 是公共可执行文件，锁定得由调用方负责。
# 端口公式与 slot 来源都抄自 send-user-message.py（部署方维护的权威实现）：
#   PORT = 18789 + (slot + 1) * 20，slot 取自 ~/.openclaw/.dh-slots.json[agentId]
# 实测校验：zqjzszr slot=1 → 18829，与该脚本 --diag 报出的端口一致。
DH_SLOTS_FILE = os.path.join(OPENCLAW_HOME, ".dh-slots.json")
GATEWAY_PORT_BASE = 18789
GATEWAY_PORT_STEP = 20


def gateway_port_for(da_id):
    """按 slot 算这个数字人的 gateway 端口；算不出返回 None（不设，让 openclaw 自己找）。"""
    slots = json_object_from_file(DH_SLOTS_FILE) or {}
    slot = slots.get(da_id)
    if isinstance(slot, int) and slot > 0:
        return GATEWAY_PORT_BASE + (slot + 1) * GATEWAY_PORT_STEP
    return None


def cli_cwd_for(human):
    """跑 openclaw / 数字人脚本时该用的工作目录：**这个数字人的实例根目录**。

    不能用巡检器自己的状态目录。实测 2026-08-24 16:14:52 的教训：
    openclaw agent 会在 **cwd** 下找 private-experts/<da>/，找不到就当成一个全新
    数字人从零初始化，然后把消息投进 gateway-fallback-* 会话 —— 后果是
      · 群会话压根收不到这条唤起，那次唤起完全白做；
      · 新建会话里 6 次 assistant turn 全部 "failed before producing content"；
      · 巡检器状态目录里凭空多出一个空白工作区（IDENTITY.md 是未填写的模板、
        git 仓库没有任何提交），看着像是谁误操作留下的垃圾。
    OPENCLAW_STATE_DIR 指对了也救不回来 —— 这一步它认的是 cwd。

    实例目录不存在时才回落 stable_cwd()：宁可少一层锁定，也不能把继承来的、
    可能已经失效的 cwd 传下去（那会让 openclaw 直接在 getcwd() 上挂掉）。
    """
    inst_dir = human.get("instDir") or ""
    if inst_dir and os.path.isdir(inst_dir):
        return inst_dir
    return stable_cwd()


def cli_env_for(human):
    """构造调 openclaw CLI 的环境，锁定到这个数字人的实例和 gateway。"""
    env = os.environ.copy()
    inst_dir = human.get("instDir") or ""
    if inst_dir:
        env["OPENCLAW_STATE_DIR"] = inst_dir
        env["OPENCLAW_CONFIG_PATH"] = os.path.join(inst_dir, "openclaw.json")
    port = gateway_port_for(human.get("daId"))
    if port:
        env["OPENCLAW_GATEWAY_PORT"] = str(port)
    bindir = os.path.join(OPENCLAW_HOME, "bin")
    if os.path.isdir(bindir):
        parts = (env.get("PATH") or "").split(os.pathsep)
        if bindir not in parts:
            env["PATH"] = os.pathsep.join([bindir, *parts])
    return env


def load_agent_names(inst_dir):
    """读 templates/agents/<id>.json 的 name 字段，拿到基础 Agent 的中文名。

    告警文案里要说"正在等沈括处理"而不是"正在等 shenkuo 处理"。读不到就退回英文 id。
    """
    names = {}
    for path in glob.glob(os.path.join(inst_dir, "templates", "agents", "*.json")):
        agent_id = os.path.basename(path)[: -len(".json")]
        data = json_object_from_file(path) or {}
        name = data.get("name")
        if isinstance(name, str) and name:
            names[agent_id] = name
    return names


def fmt_ts(ms):
    """epoch 毫秒 → "08-20 20:15:24（3.2h 前）"，0 表示没有活动记录。"""
    if not ms:
        return "无活动记录"
    when = datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")
    ago_s = time.time() - ms / 1000
    if ago_s < 60:
        ago = f"{ago_s:.0f}s"
    elif ago_s < 3600:
        ago = f"{ago_s / 60:.0f}min"
    elif ago_s < 86400:
        ago = f"{ago_s / 3600:.1f}h"
    else:
        ago = f"{ago_s / 86400:.1f}d"
    return f"{when}（{ago} 前）"


def pad(s, width):
    """按终端显示宽度左对齐补空格（中文算 2 列），让含中文的表格能对齐。"""
    s = str(s)
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)
    return s + " " * max(0, width - shown)


# ========================= 功能 3：判断会话是否在执行中 =========================
#
# 这是方案判定树的【步骤 1】，整棵树的分岔点：
#   在跑   → 只可能出"进行中提醒"
#   没在跑 → 才去查那一串中断告警
# 判错的后果是方向性的：把"在跑"判成"没在跑"，会对正常运行的会话发一堆中断告警。
#
# 方案给了 4 个信号，任一命中即视为在跑。这里实现前 3 个，CC 那条留到后面的功能。

RUN_MARKERS = ("session.started", "session.ended")

# 倒着扫 trajectory 的字节上限。正常情况下读 1~5 行就能出结论，这个上限只是兜底，
# 防止遇到异常文件把内存和时间吃光。
TRAJECTORY_SCAN_LIMIT = 2 * 1024 * 1024


def iter_lines_reverse(path, max_bytes=TRAJECTORY_SCAN_LIMIT, chunk_size=64 * 1024):
    """从文件尾部往前逐行读，yield (整行 bytes, 已扫描字节数)。

    为什么不用"读尾部固定 N 字节"：trajectory 单行能到 150KB，固定窗口很可能一行都
    截不全，或者截到的几行里既没有 session.started 也没有 session.ended，判不出运行态。
    倒着读、找到答案就停，正常只读 1~5 行。
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        pending = b""   # 上一轮切剩的、可能不完整的第一行
        scanned = 0
        while pos > 0 and scanned < max_bytes:
            step = min(chunk_size, pos)
            pos -= step
            f.seek(pos)
            scanned += step
            parts = (f.read(step) + pending).split(b"\n")
            pending = parts[0]        # 它的开头还在更前面，留到下一轮
            for raw in reversed(parts[1:]):
                if raw.strip():
                    yield raw, scanned
        if pos == 0 and pending.strip():
            yield pending, scanned


def iso_to_ms(s):
    """ISO8601 时间（形如 2026-08-19T03:15:24.291Z）→ epoch 毫秒；解析不了返回 0。

    trajectory 里的 ts 是字符串，而 sessions.json 里是 epoch 毫秒，得统一到毫秒才能比。
    """
    if not isinstance(s, str) or not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def resolve_trajectory_file(session_file):
    """由 <sid>.jsonl 找到对应的 trajectory 文件，找不到返回空串。"""
    if not session_file.endswith(".jsonl"):
        return ""
    base = session_file[: -len(".jsonl")]
    # 优先看指针文件：trajectory 有可能被放到别的位置，指针里的 runtimeFile 才是准的。
    pointer = json_object_from_file(base + ".trajectory-path.json") or {}
    runtime_file = pointer.get("runtimeFile")
    if isinstance(runtime_file, str) and runtime_file and os.path.exists(runtime_file):
        return runtime_file
    direct = base + ".trajectory.jsonl"
    return direct if os.path.exists(direct) else ""


def latest_run_marker(traj_file, as_of_ms=None, scan_limit=TRAJECTORY_SCAN_LIMIT):
    """倒着扫 trajectory，找最近一次的 run 开始/结束标记。

    as_of_ms 不为 None 时忽略该时刻之后的事件，用来回溯"历史某一刻会怎么判"——
    没有会话正在运行时，这是唯一能验证"在跑"这条分支的办法。回溯要跨过之后所有
    事件才能到目标时刻，比生产用法费得多，所以 scan_limit 可以调大。

    返回 {"marker": "session.started"/"session.ended"/None, "ts", "runId", "scannedBytes"}
    marker 为 None 表示扫完（或扫到上限）都没找到标记 —— 运行态未知，不是"没在跑"。
    """
    out = {"marker": None, "ts": 0, "runId": "", "scannedBytes": 0}
    if not traj_file:
        return out
    try:
        for raw, scanned in iter_lines_reverse(traj_file, max_bytes=scan_limit):
            out["scannedBytes"] = scanned
            event = json_object(raw)
            if event is None:
                continue          # 半行/坏行/不是对象，跳过继续往前找
            if event.get("type") not in RUN_MARKERS:
                continue
            ts = iso_to_ms(event.get("ts"))
            if as_of_ms is not None and ts > as_of_ms:
                continue
            out.update(marker=event["type"], ts=ts, runId=event.get("runId") or "")
            return out
    except OSError:
        pass
    return out


def latest_trajectory_event(traj_file, as_of_ms=None, scan_limit=TRAJECTORY_SCAN_LIMIT):
    """倒着扫 trajectory，返回最新一条事件的 type（任意类型），找不到返回空串。

    跟 latest_run_marker 的区别：那个只认 session.started/ended，用来判运行态；
    这个要的是"最新发生的是什么"，用来生成"正在干啥"的文案（比如最新是
    prompt.submitted 就说"正在思考中"）。
    """
    if not traj_file:
        return ""
    try:
        for raw, _ in iter_lines_reverse(traj_file, max_bytes=scan_limit):
            event = json_object(raw)
            if event is None:
                continue
            ts = iso_to_ms(event.get("ts"))
            if as_of_ms is not None and ts > as_of_ms:
                continue
            return event.get("type") or ""
    except OSError:
        pass
    return ""


# 运行态三档。"未知"必须单独一档：把未知当成"没在跑"，会让正在跑的会话掉进中断
# 告警分支，对活着的会话发一串"中断"告警 —— 这是最不能犯的方向性错误。
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_UNKNOWN = "unknown"

STATE_LABEL = {
    STATE_RUNNING: "🟢 在执行中",
    STATE_IDLE: "⚪️ 没在跑",
    STATE_UNKNOWN: "❔ 未知（本轮跳过，不判异常）",
}


def judge_running(sessions, as_of_ms=None, scan_limit=TRAJECTORY_SCAN_LIMIT, cc=None):
    """判断一个群的运行态，并说明是哪个信号判的。

    满足任意一条即视为在跑（方案的第 4 条 CC 信号留到后面的功能）：
      信号1  数字人 sessions.json 的 status == "running"
      信号2  数字人 trajectory 最新是 session.started 且之后没有 session.ended
      信号3  任一基础 Agent 的 status == "running"

    三条都没说"在跑"时还要再分一次：
      信号2 给出了明确的 session.ended  → idle，可以往下走中断告警判定
      信号2 没结论（文件缺失 / 扫到上限）→ unknown，本轮什么都不做

    返回 {"state": running/idle/unknown, "hits": [命中的信号], "evidence": [每条信号的依据],
          "trajMarker": trajectory 最新标记的事件类型,
          "trajTs": 该标记的时刻 —— state=running 时它就是"当前这次 run 的开始时间"}
    """
    hits, evidence = [], []
    traj_conclusive = False      # 信号 2 是否给出了明确结论
    traj_marker = None
    traj_ts = 0

    da = next((s for s in sessions if s["role"] == "数字人"), None)
    subs = [s for s in sessions if s["role"] != "数字人"]

    # 信号 1：数字人主状态
    if da is None:
        evidence.append("信号1 数字人主状态：这个群里没有数字人本体会话")
    else:
        if da["status"] == "running":
            hits.append("信号1 数字人 status=running")
        evidence.append(f"信号1 数字人主状态：status={da['status']}")

    # 信号 2：数字人 trajectory 的 run 标记
    if da is None:
        evidence.append("信号2 trajectory：无数字人会话，跳过")
    else:
        traj = resolve_trajectory_file(da["sessionFile"])
        if not traj:
            evidence.append("信号2 trajectory：找不到 trajectory 文件 → 无结论")
        else:
            mark = latest_run_marker(traj, as_of_ms, scan_limit)
            if mark["marker"] is None:
                evidence.append(
                    f"信号2 trajectory：扫了 {mark['scannedBytes'] / 1024:.0f}KB"
                    f" 没找到 run 标记 → 无结论"
                )
            else:
                traj_conclusive = True
                traj_marker = mark["marker"]
                traj_ts = mark["ts"]
                if mark["marker"] == "session.started":
                    hits.append("信号2 trajectory 最新是 session.started（之后无 ended）")
                evidence.append(
                    f"信号2 trajectory：最新标记 {mark['marker']} @ {fmt_ts(mark['ts'])}"
                    f" run={mark['runId'][:8]}（读了 {mark['scannedBytes'] / 1024:.0f}KB）"
                )

    # 信号 3：任一基础 Agent 在跑
    running_subs = [s["agentId"] for s in subs if s["status"] == "running"]
    if running_subs:
        hits.append(f"信号3 基础 Agent 在跑：{'/'.join(running_subs)}")
    evidence.append(
        f"信号3 基础 Agent：{len(subs)} 个，其中 running "
        f"{len(running_subs)} 个（{'/'.join(running_subs) or '无'}）"
    )

    # 信号 4：CC 子会话活跃（方案的第 4 条，旧实现从未接上）
    cc = cc or {}
    if not cc.get("found"):
        evidence.append(f"信号4 CC：不采 —— {cc.get('why') or '定位不到 CC 项目'}")
    elif cc.get("running"):
        hits.append(f"信号4 CC 活跃：{cc['activeCount']} 个子会话在跑")
        evidence.append(f"信号4 CC：{cc['activeCount']} 个子会话在跑"
                        f"，最后触碰 {fmt_ts(cc.get('lastTouch'))}")
    else:
        evidence.append(f"信号4 CC：空闲，最后触碰 {fmt_ts(cc.get('lastTouch'))}")

    if hits:
        state = STATE_RUNNING
    elif traj_conclusive:
        state = STATE_IDLE
    else:
        state = STATE_UNKNOWN
    return {"state": state, "hits": hits, "evidence": evidence,
            "trajMarker": traj_marker, "trajTs": traj_ts,
            # 只有最新标记确实是 session.started 时，它才是"当前这次 run 的开始时间"。
            # 实测踩过（2026-08-21 群 10232848092）：运行态是靠信号 1 判出来的，而
            # trajectory 最新标记还是上一轮的 session.ended，结果拿"上次结束时刻"
            # 当本次开始时刻，文案报出"已运行 23 分钟"，而这次 run 其实只跑了 15 秒。
            "runStartedTs": traj_ts if traj_marker == "session.started" else 0}


def print_discovery(cfg=None):
    """把发现结果 + 判定过程打成一张人能直接核对的表。"""
    humans = discover_digital_humans()
    if not humans:
        print(f"没有在 {OPENCLAW_HOME} 下找到任何数字人实例（判据：目录里有 openclaw.json）。")
        return
    for h in humans:
        flags = []
        flags.append("交付数字人 ✅" if h["isDelivery"] else "非交付数字人，不巡检 ⏭️")
        if not h["hasOwnSessions"]:
            flags.append("本体未运行过")
        print(f"\n数字人 {h['daId']}　[{' | '.join(flags)}]")
        print(f"  目录 {h['instDir']}")
        print(f"  发送脚本 {h['sendScript'] or '❌ 找不到（这个数字人发不出消息）'}")
        if not h["groups"]:
            print("  （没有群会话）")
            continue
        for group_id, sessions in sorted(h["groups"].items()):
            allowed = group_allowed(cfg, group_id)
            gate = "" if allowed else "　⏭️ 白名单外，不巡检"
            print(f"  群 {group_id}　{len(sessions)} 个会话{gate}")
            if not allowed:
                continue
            events, trace = decide(h, group_id, sessions, cfg)
            for line in trace:
                print(f"      · {line}")
            if events:
                for event in events:
                    print(f"      ▶ 判定出事件【{event['severity']}】{event['type']}")
                    print(f"        投递目标 {event['target'] or '(取不到)'}")
                    for line in event["text"].splitlines():
                        print(f"        | {line}")
            else:
                print("      ▶ 无事件（保持静默）")
            for s in sessions:
                mark = "空壳未跑" if s["isStub"] else f"status={s['status']}"
                print(
                    f"    [{pad(s['role'], 9)}] {pad(s['agentId'], 9)} {pad(mark, 14)}"
                    f" 最后活动 {fmt_ts(s['activityTs'])}"
                )


# ===================== 功能 4：进行中提醒（REMIND_LONG_RUNNING）=====================
#
# 判定树 running 分支的终点：在跑 + 用户/数字人静默超过阈值 → 群里说一句"正在处理中"。
# 这是唯一的 INFO 级事件，不唤起数字人，方案灰度里第一个放开的就是它。
#
# 需要从 <sid>.jsonl 消息流里取两个时间点，两个都是旧实现读错的地方：
#
#   T_user       最后一条"真实用户消息"
#                真实用户消息的标志是 message.provenance 不存在（带 __openclaw.senderId）；
#                基础 Agent 的回报也是 role=user，但带 provenance.kind=inter_session。
#
#   T_da_send_ok 最后一次对客发送"真正成功"
#                旧实现找 '"sent": true' —— 该字符串在真实数据里出现 0 次。
#                真实形态是：assistant 的 exec toolCall（command 含 send-user-message.py）
#                → 按 toolCallId 配到 role=toolResult / toolName=exec 的结果
#                → 结果的 content[].text 是个字符串化 JSON，里面 ok=true 才算发出去了。

TRANSCRIPT_SCAN_LIMIT = 8 * 1024 * 1024

# assistant 最终文本是这些标记时，表示"明示不对客"，不算给用户回了话（方案 §4.2）。
NO_REPLY_TOKENS = ("NO_REPLY", "ANNOUNCE_SKIP", "REPLY_SKIP")


def _is_no_reply_text(text):
    """assistant 文本是不是"明示不对客"的标记。

    按**末行**匹配，不是整段 grep 也不是全等：实测存在"正文 + 标记"的形态
    （如 "...进度播报已发送。\\n\\nNO_REPLY"），纯全等会漏、纯 grep 会把正文里
    提到这几个词的普通回复也误判成不对客。

    还要处理标记粘连：实测出现过 "REPLY_SKIPANNOUNCE_SKIP"（模型输出时没换行，
    两个标记连在一起，2026-08-22 15:55 群 10232962603）。末行如果整体就是若干个
    标记拼起来的，同样算"明示不对客"。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1]
    if last in NO_REPLY_TOKENS:
        return True
    # 反复剥掉开头的标记；能剥干净说明整行只由标记拼成
    rest = last
    stripped_any = False
    while rest:
        for token in NO_REPLY_TOKENS:
            if rest.startswith(token):
                rest = rest[len(token):].strip()
                stripped_any = True
                break
        else:
            return False
    return stripped_any

# 判定用到的阈值默认值，config.json 的 thresholds 段可覆盖。
THRESHOLD_DEFAULTS = {
    "ACTIVE_WINDOW_MS": 30 * 60 * 1000,        # 步骤 0：这么久没活动就当僵尸群，不巡检
    "USER_QUIET_MS": 5 * 60 * 1000,            # 步骤 F：对客静默这么久才提醒"进行中"
    "COOLDOWN_MS": 30 * 60 * 1000,             # 同一会话同一类事件的冷却
    "GROUP_RATE_WINDOW_MS": 60 * 60 * 1000,    # 同群限流的统计窗口
    "GROUP_RATE_MAX": 3,                       # 同群在窗口内最多发几条
    "MODEL_ERROR_GRACE_MS": 30 * 1000,         # 模型异常后留给自动重试/failover 的宽限
    "USER_REPLY_ACK_MS": 60 * 1000,            # 用户说完话后，数字人多久没回就算异常
    # 网关记的 lastInteractionAt 比消息流的 tUser 晚这么多，就认定消息没进会话。
    # 正常只差 0~1 秒（实测 4 个群分别 -1/-0/-0/-1 秒），留 2 分钟余量给落盘和排队。
    "MSG_SWALLOWED_MS": 2 * 60 * 1000,
    # 派活后多久没回报算异常。阈值来自实测分布（两个群共 64 次派活）：
    # 中位 15 秒、P90 80 秒、最大 333 秒 —— 方案给的 90 秒会对 6/55（11%）正常派活
    # 误报。所以分两档：
    "SUB_REPORT_LAG_MS": 10 * 60 * 1000,   # sub 还在 running 时的兜底宽限
    "SUB_DONE_LAG_MS": 90 * 1000,          # sub 已不在 running（结束了却没回报）时
    # 收到回报后多久没对客也没明示不对客算异常。实测 87 次回报的处理滞后中位 19 秒、
    # 最大 364 秒 —— 方案给的 60 秒会对 3/76（4%）误报，所以放到 10 分钟。
    "INTER_SESSION_ACK_MS": 10 * 60 * 1000,
    # CC 显示在跑但这么久没被碰过就算卡死。方案给的是 3 分钟，沿用。
    "CC_STALE_MS": 3 * 60 * 1000,
}


def threshold(cfg, name):
    """取阈值，配置里没写就用默认值。"""
    value = ((cfg or {}).get("thresholds") or {}).get(name)
    return int(value) if isinstance(value, (int, float)) and value > 0 else THRESHOLD_DEFAULTS[name]


def debug_only_note(event, cfg):
    """这条事件是不是**只因为阈值被调小了**才成立的？是就返回一句说明，否则空串。

    联调时会把阈值压到几秒好快速触发（比如 USER_QUIET_MS 从 300 秒压到 8 秒），
    代价是每个正常会话都在刷提醒 —— 数字人两次工具调用之间隔 10 秒是常态。
    实测 2026-08-25 群 10232962603 就这样连发 5 条"正在执行命令"，而生产阈值下
    一条都不会发。这时候最费时间的不是误报本身，是**分不清哪条是真 bug、哪条只是
    阈值调小的必然结果** —— 每条都得手工回放一遍才知道。

    所以让判定自己把这件事说出来：拿实测值和**生产默认值**比一次。
    生产阈值下它照样成立 → 这是真事，得查；生产阈值下不成立 → 调试假象，可以放过。

    阈值调回生产值后这个标记自动消失，不用记着去摘。
    """
    gate = ((event or {}).get("detail") or {}).get("gate") or {}
    key, measured = gate.get("key"), gate.get("measured")
    if not key or not isinstance(measured, (int, float)):
        return ""                       # 这类事件没有时长闸门，无从比较
    production = THRESHOLD_DEFAULTS.get(key)
    if production is None or measured > production:
        return ""                       # 生产阈值下同样成立，是真事
    effective = threshold(cfg, key)
    if effective >= production:
        return ""                       # 本来就在用生产值，没有"调小"这回事
    return (f"仅调试阈值触发：{key} 实测 {fmt_duration(measured)}，"
            f"当前阈值 {fmt_duration(effective)}，生产阈值 {fmt_duration(production)}")


def _send_result_ok(msg):
    """一条 exec 的 toolResult 是否表示"消息真的发出去了"。

    结果文本是字符串化 JSON。判据优先看 sent —— 它是脚本给出的"已投递"直接信号，
    实测 65 次成功发送全部带 sent=true；--dry-run / --assemble-only 时是 false。
    万一将来没有这个字段，退回 ok=true 且非 assembleOnly。
    解析失败一律当失败（保守，宁可漏报也不误判成功）。
    """
    result = json_object(_tool_result_text(msg))
    if result is None:
        return False      # 不是 JSON 对象（实测有裸数字 '239'）一律当没发成功
    if "sent" in result:
        return result["sent"] is True
    return result.get("ok") is True and result.get("assembleOnly") is not True


def _tool_result_text(msg):
    """把一条 toolResult 的 content 拼成文本。"""
    return "".join(
        c.get("text", "") for c in (msg.get("content") or []) if isinstance(c, dict)
    )


def _send_message_type(msg):
    """从 exec 的 toolResult 里取 send-user-message.py 的 messageType。

    脚本把消息分两类（见其源码 AT_MESSAGE_TYPES / NO_AT_MESSAGE_TYPES）：
      clarification / confirmation / result / error  需要 @ 用户
      progress                                       纯播报
    我们只关心"是不是在等用户回答"，见 AWAITING_USER_SEND_TYPES。
    """
    result = json_object(_tool_result_text(msg)) or {}
    value = result.get("messageType")
    return value if isinstance(value, str) else ""


# 这几种对客类型意味着"问题已经抛给用户了，正在等他回答" —— 球在用户脚下，
# 这时候再提醒"我正在处理中"是噪音，甚至会让用户以为不用回答。
#
# 实测（2026-08-22 群 10232962603 一次真实交付流程）这不是边缘情况而是主流：
# 90 次成功对客里 clarification 59 次、confirmation 7 次，合计 66 次都在等用户；
# progress 只有 23 次。少了这条豁免，交付流程里巡检器会一直催。
AWAITING_USER_SEND_TYPES = ("clarification", "confirmation")

# 真人消息的判据：openclaw 给每条真人消息都挂了这个元数据块（里面有 senderIsOwner /
# senderId / senderName）。运行时自己注入的 role=user 消息一律没有。
#
# 实测 2026-08-25 全库统计，分离得干干净净：
#   真人消息            161 / 161 条都有
#   CC 回调 [CC-CALLBACK]  13 条、System 注入 5 条、
#   "Continue the OpenClaw runtime event." 2 条、巡检器唤起 5 条 —— 25 / 25 条都没有
#
# **不能改用"以 [ 开头"这类前缀规则**：实测 [图片] 是真人发图（还带 MediaPath），
# [System] 才是运行时注入，两者形状一模一样；真人也会发"查 OpenClaw config 配置"
# 这种带关键词的话。按前缀猜必然两头都错。
OPENCLAW_MSG_META = "__openclaw"


def scan_transcript(session_file, as_of_ms=None, scan_limit=TRANSCRIPT_SCAN_LIMIT):
    """倒着扫 <sid>.jsonl，采集判定要用的时间点。

    倒着扫的好处：要找的都是"最后一次"，倒序遇到的第一个就是答案，凑齐就能停。
    顺带的顺序特性：toolResult 排在它的 toolCall 后面，倒序反而是先看到结果、
    再看到调用，正好能在遇到 send 调用时立刻知道它成没成。

    ⚠️ 数字人对客有**两种**方式，都得认，否则静默判定会持续误报：
      1. 走 send-user-message.py（交付场景 AGENTS.md 规约要求）→ tDaSendOk
      2. 直接用 assistant 文本回复，由 openclaw 网关投递给用户 → tDaText
    实测（2026-08-22）两个群的形态完全相反：
      群 10232767188（交付群）send-user-message.py 252 次、assistant 文本 200 条
      群 10232848092（调试群）send-user-message.py   0 次、assistant 文本  22 条
    原实现只认第 1 种，于是在调试群里 tDaSendOk 恒为 0，静默基准只能落在 T_user 上
    —— 只要用户发过消息、8 秒后就提醒，完全不管数字人已经回过话了。用户因此连着
    收到"数字人自己的进度播报"+"巡检器的正在处理中"两条，正是降噪要避免的噪音。

    返回 {"tUser","tDaSendOk","tDaText","tDaReply","lastToolCall","scannedBytes","capped"}
      tDaSendOk  严格证据：send-user-message.py 且脚本回报已投递（三条铁律要用）
      tDaText    最后一条非 NO_REPLY 家族的 assistant 文本
      tDaReply   两者取晚 —— 静默判定用这个
    """
    out = {"tUser": 0, "tDaSendOk": 0, "tDaText": 0, "tDaReply": 0,
           "tModelError": 0, "modelErrorMessage": "", "modelChain": [],
           "tDispatch": 0, "lastNoReply": False, "tNoReplyMark": 0, "tWake": 0,
           "tRuntime": 0,
           "lastSendType": "",
           "dispatchTo": {}, "recvFrom": {},
           "lastToolCall": None, "scannedBytes": 0, "capped": False}
    if not session_file or not os.path.exists(session_file):
        return out

    exec_results = {}     # toolCallId -> (是否成功, 结果落盘时刻)
    try:
        for raw, scanned in iter_lines_reverse(session_file, max_bytes=scan_limit):
            out["scannedBytes"] = scanned
            row = json_object(raw)
            if row is None:
                continue
            msg = row.get("message")
            if not isinstance(msg, dict):
                # custom / model-snapshot 记录了模型切换，倒序收集就是 failover 链。
                # 实测（2026-08-22 13:16:56~13:17:02）连换 5 个模型：
                # GLM-5.2 → GLM-5.1 → GPT-5.5 → DeepSeek-V4-Pro → Qwen3-Embedding-8B
                # 最后落在一个 embedding 模型上，数字人从此哑掉。这条链对判断
                # "openclaw 到底自愈了没有"很关键，所以要采。
                if row.get("customType") == "model-snapshot" and len(out["modelChain"]) < 8:
                    ts = iso_to_ms(row.get("timestamp"))
                    if as_of_ms is None or ts <= as_of_ms:
                        model_id = ((row.get("data") or {}).get("modelId")) or ""
                        if model_id:
                            out["modelChain"].append({"modelId": model_id, "ts": ts})
                continue
            ts = iso_to_ms(row.get("timestamp"))
            if as_of_ms is not None and ts > as_of_ms:
                continue

            role = msg.get("role")
            if role == "user":
                # 巡检器自己的唤起消息也是 role=user，必须排除，否则会自问自答：
                # 铁律二-1 把它当成"用户消息没人回"、REMIND 的静默基准被它重置。
                body = msg.get("content")
                if isinstance(body, str) and body.lstrip().startswith(WAKE_MESSAGE_PREFIX):
                    if not out["tWake"]:
                        out["tWake"] = ts
                    continue
                # 基础 Agent 的回报也是 user，要靠 provenance 区分开。
                provenance = msg.get("provenance") or {}
                kind = provenance.get("kind")
                if kind == "inter_session":
                    # 回报来自哪个基础 Agent，从 sourceSessionKey 里解：
                    # agent:<sub>:jingme:group-virtual:<gid>:<da>
                    parts = str(provenance.get("sourceSessionKey") or "").split(":")
                    sub = parts[1] if len(parts) > 1 else ""
                    if sub and sub not in out["recvFrom"]:
                        out["recvFrom"][sub] = ts     # 倒序，第一次遇到就是最近一次
                elif OPENCLAW_MSG_META not in msg:
                    # 运行时注入的消息，不是人发的，绝不能重置"用户在等"的基准。
                    if not out["tRuntime"]:
                        out["tRuntime"] = ts
                elif not out["tUser"]:
                    out["tUser"] = ts
            elif role == "toolResult":
                if msg.get("toolName") == "exec":
                    exec_results[msg.get("toolCallId")] = (
                        _send_result_ok(msg), ts, _send_message_type(msg))
            elif role == "assistant":
                # 模型调用失败：消息流里是实时可见的，且带具体原因，比 trajectory 强
                if msg.get("stopReason") == "error" and not out["tModelError"]:
                    out["tModelError"] = ts
                    out["modelErrorMessage"] = str(msg.get("errorMessage") or "").strip()
                for c in (msg.get("content") or []):
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        # 记下最后一条 assistant 文本是不是"明示不对客"的标记，
                        # 铁律判定要用它来豁免（数字人已经声明这轮不用回话）
                        if _is_no_reply_text(c.get("text")):
                            if not out["tDaText"] and not out["lastNoReply"]:
                                out["lastNoReply"] = True
                            if not out["tNoReplyMark"]:
                                out["tNoReplyMark"] = ts
                        # assistant 文本 = 直接对客，但两种要排除：
                        #  1. 明示不对客的标记（NO_REPLY 家族）
                        #  2. 模型调用失败的占位符 —— stopReason=error 时正文是
                        #     "[assistant turn failed before producing content]"，
                        #     那根本不是回复。实测（2026-08-22 13:16:56）如果把它算成
                        #     对客，静默基准会被推到失败时刻，巡检器就以为数字人回过
                        #     话了，该发的提醒反而被压掉。
                        if (not out["tDaText"]
                                and msg.get("stopReason") != "error"
                                and not _is_no_reply_text(c.get("text"))):
                            out["tDaText"] = ts
                        continue
                    if c.get("type") != "toolCall":
                        continue
                    command = str(((c.get("arguments") or {}).get("command")) or "")
                    if out["lastToolCall"] is None:
                        out["lastToolCall"] = {
                            "name": c.get("name") or "",
                            "isWorkflow": "workflow.py" in command,
                            "ts": ts,
                        }
                    # 派活给基础 Agent：派了就是在等回报，"用户没收到回复"要豁免
                    if c.get("name") == "sessions_send":
                        if not out["tDispatch"]:
                            out["tDispatch"] = ts
                        target_key = str(((c.get("arguments") or {}).get("sessionKey")) or "")
                        parts = target_key.split(":")
                        sub = parts[1] if len(parts) > 1 else ""
                        if sub and sub not in out["dispatchTo"]:
                            out["dispatchTo"][sub] = ts
                    if (not out["tDaSendOk"]
                            and c.get("name") == "exec"
                            and "send-user-message.py" in command):
                        ok, result_ts, send_type = exec_results.get(
                            c.get("id"), (False, 0, ""))
                        if ok:
                            # 用结果落盘的时刻，那才是"真的发出去了"的时间
                            out["tDaSendOk"] = result_ts or ts
                            out["lastSendType"] = send_type

            if out["tUser"] and out["tDaSendOk"] and out["tDaText"] and out["lastToolCall"]:
                break            # 要的都齐了，不用再往前扫
        else:
            out["capped"] = out["scannedBytes"] >= scan_limit
    except OSError:
        pass
    out["tDaReply"] = max(out["tDaSendOk"], out["tDaText"])
    return out


# 工具名 → 人话，用于"正在干啥"文案。
#
# 这些信息来自消息流 <sid>.jsonl，它是**实时写入**的 —— 实测（2026-08-22 11:49 那次
# 97 秒 run）提醒在 11:49:43 触发时，已经能读到 11:49:30 的用户消息和 11:49:37 的
# toolCall。这一点和 trajectory 正相反：trajectory 要到 run 结束才落盘。
#
# 所以方案 §8.3 里"数字人最新是 prompt.submitted → 正在思考中"那条基本用不上（它读
# trajectory，run 进行中读到的是上一轮的事件）；真正可用的是消息流里的最新 toolCall。
# 实测那次 lastToolCall 是 process（在等 sleep 90），但原实现只认 sessions_send 和
# workflow.py，于是掉进兜底，群里只说了句"正在处理中"。
TOOL_ACTIVITY_LABELS = {
    "exec": "正在执行命令",
    "process": "正在等后台任务完成",
    "read": "正在读取文件",
    "write": "正在写文件",
    "edit": "正在修改文件",
    "sessions_send": "正在派发任务",
    "sessions_history": "正在回顾历史会话",
    "message": "正在发送消息",
}


def is_awaiting_user(transcript):
    """问题是不是已经抛给用户、正在等他回答（球在用户脚下）。

    两个条件都要满足，缺一不可：
      · 最后一次成功对客的类型是澄清 / 确认
      · 且它发生在用户最后一次发言**之后**
    第二个守卫是必须的：lastSendType 会一直保留着上一轮那次澄清的类型，用户答完之后
    它并不会被清掉。实测踩过（2026-08-22 17:05）用户已经回答并又提了新要求，判定这边
    因为带了守卫、正确地继续提醒，而文案那边漏了守卫，说成"正在等您回答"——
    用户明确指出"魏征还没让我澄清，我在等魏征"。

    抽成函数就是为了让判定和文案共用同一份逻辑，不再各写一遍然后漂移。
    """
    return (transcript.get("lastSendType") in AWAITING_USER_SEND_TYPES
            and transcript.get("tDaSendOk", 0) >= transcript.get("tUser", 0))


def pending_subs(transcript):
    """派了活还没等到回报的基础 Agent，按派活时间从早到晚。

    比看 sub 的 sessions.json status 更可靠：status 有滞后（实测派活 17:05:40、
    回报 17:05:53，而 sessions.json 里 weizheng 一直是 done），而"派了活没回报"
    完全由数字人自己的消息流决定，实时可见。
    """
    dispatched = transcript.get("dispatchTo") or {}
    received = transcript.get("recvFrom") or {}
    pending = [(ts, sub) for sub, ts in dispatched.items()
               if ts > received.get(sub, 0)]
    return [sub for _, sub in sorted(pending)]


def describe_activity(sessions, transcript, traj_marker, agent_names):
    """生成"正在干啥"的文案（方案 §8.3），零模型，按优先级取第一个命中。

    CC 那条（正在跑代码执行子任务）留到 CC 卡死检测那个功能一起做。
    """
    # 1) 球在用户脚下 —— 最具体，且用的是和判定完全相同的 is_awaiting_user()
    if is_awaiting_user(transcript):
        return "正在等您回答"

    # 2) CC 在跑代码子任务（方案 §8.3 把它排在最前，但"在等用户"比它更具体）
    if (transcript.get("cc") or {}).get("running"):
        return "正在跑代码执行子任务"

    # 3) 派了活还没回报 —— 用消息流判，不看 sub 的 status（后者有滞后）
    waiting = pending_subs(transcript)
    if waiting:
        names = "、".join(agent_names.get(a, a) for a in waiting)
        return f"正在等 {names} 处理"

    # 3) sub 的 status 说它在跑（消息流看不出时的补充信号）
    running_subs = [s["agentId"] for s in sessions
                    if s["role"] != "数字人" and s["status"] == "running"]
    if running_subs:
        names = "、".join(agent_names.get(a, a) for a in running_subs)
        return f"正在等 {names} 处理"

    last = transcript.get("lastToolCall") or {}
    if last.get("isWorkflow"):
        return "正在推进工作流"
    label = TOOL_ACTIVITY_LABELS.get(last.get("name"))
    if label:
        return label
    if traj_marker == "prompt.submitted":
        # trajectory 落盘滞后，这条实际只在 run 已结束时才可能命中，保留兜个底
        return "正在思考中"
    return "正在处理中"


def fmt_duration(ms):
    """把时长说成人话，按量级换单位；下限是"1 秒"。

    几个下限/上限都是实测踩出来的：
    · 一律按分钟取整 → 13 秒会显示成"已运行 0 分钟"，比不说还糟
    · 秒也四舍五入   → 400 毫秒会显示成"0 秒"，同样是废话
    · 一律按分钟     → 52 小时会显示成"已等待 3150 分钟"，没人能读
    """
    if ms < 60_000:
        return f"{max(1, round(ms / 1000))} 秒"
    if ms < 60 * 60_000:
        return f"{ms / 60_000:.0f} 分钟"
    if ms < 24 * 60 * 60_000:
        return f"{ms / (60 * 60_000):.1f} 小时"
    return f"{ms / (24 * 60 * 60_000):.1f} 天"


def check_long_running(human, group_id, sessions, cfg, now_ms, latest_event, transcript,
                       run_started_ts):
    """步骤 F：在跑但对客静默太久 → REMIND_LONG_RUNNING。

    静默从"最后一次跟用户有来往"算起，取 T_user 和 T_da_send_ok 里更晚的那个：
    数字人中途播报过进度，就不该再提醒。

    run_started_ts 由调用方给出"当前这次 run 的开始时间"，优先 trajectory 的
    session.started，拿不到时回落 sessions.json 的 startedAt（见 decide 里的注释）。
    实在算不出可信值就不提这句话，宁可少说，也不要报一个错的数字。
    """
    da = next((s for s in sessions if s["role"] == "数字人"), None)
    if da is None:
        return None
    quiet_since = max(transcript["tUser"], transcript["tDaReply"])
    if not quiet_since:
        return None      # 连一次来往都没有，没有基准，不提醒

    # 球在用户脚下：最后一次对客是澄清/确认类，且发生在用户最后一次发言之后，
    # 说明问题已经抛给用户、正在等他回答。这时候提醒"我正在处理中"是噪音。
    # 实测（2026-08-22 16:01）数字人 16:00:58 刚把澄清问题发给用户，21 秒后巡检器
    # 就发了"正在执行命令，已等待 1 分钟"——用户被问了问题反被催。
    if is_awaiting_user(transcript):
        return None

    quiet_ms = now_ms - quiet_since
    if quiet_ms <= threshold(cfg, "USER_QUIET_MS"):
        return None

    doing = describe_activity(sessions, transcript, latest_event,
                              human.get("agentNames") or {})
    da_name = (human.get("agentNames") or {}).get(human["daId"], human["daId"])

    # 文案里的时长说的是"你这个请求等了多久"，而不是"当前这个 run 跑了多久"。
    # 实测（2026-08-22 15:43 群 10232962603）派活场景下数字人在"派活→等回报→被唤醒
    # →再派活"的循环里起一连串短 run，sessions.json.startedAt 每次都被重置，按 run
    # 算出来是"已运行 0 秒"，对用户毫无意义。用户关心的是自己等了多久，所以从最后
    # 一次用户发言算起。run 时长留在 detail 里供排查。
    waited_ms = now_ms - transcript["tUser"] if transcript["tUser"] else 0
    ran_ms = now_ms - run_started_ts if run_started_ts and now_ms > run_started_ts else 0
    waited_text = f"，已等待 {fmt_duration(waited_ms)}" if waited_ms > 0 else ""
    return {
        "type": "REMIND_LONG_RUNNING",
        "severity": "INFO",
        "daId": human["daId"],
        "groupId": group_id,
        "sessionKey": da["sessionKey"],
        "channel": da["channel"],
        "target": da["target"],
        "text": (f"🕐 {da_name} {doing}{waited_text}。\n"
                 f"　　如需查看进度，回复「状态」；如需取消，回复「取消」。"),
        "detail": {
            "gate": {"key": "USER_QUIET_MS", "measured": quiet_ms},
            "quietMs": quiet_ms,
            "quietSince": quiet_since,
            "tUser": transcript["tUser"],
            "tDaSendOk": transcript["tDaSendOk"],
            "tDaText": transcript["tDaText"],
            "tDaReply": transcript["tDaReply"],
            "runStartedTs": run_started_ts,
            "ranMs": ran_ms,
            "waitedMs": waited_ms,
            "doing": doing,
        },
    }


def decide(human, group_id, sessions, cfg, now_ms=None, as_of_ms=None, scan_limit=None):
    """对一个群走一遍判定树，返回 (事件列表, 过程说明)。

    目前实现到判定树的：
      步骤 0  最近没活动 → 僵尸群，跳过
      步骤 1  运行态三态
      步骤 F  running + 对客静默超时 → REMIND_LONG_RUNNING
    idle 分支的那一串中断告警是后面的功能。

    now_ms 与 as_of_ms 分开传：回放历史时刻时，"现在"必须是那个历史时刻，
    否则"多久没活动"会拿真实当前时间去减，全都算成超时。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    trace = []

    # 步骤 0：僵尸群过滤
    newest = max((s["activityTs"] for s in sessions), default=0)
    idle_ms = now_ms - newest if newest else None
    if idle_ms is None or idle_ms > threshold(cfg, "ACTIVE_WINDOW_MS"):
        shown = f"{idle_ms / 60000:.0f}min" if idle_ms is not None else "无活动记录"
        trace.append(f"步骤0 活跃检查：最近活动 {shown} 前 → 僵尸群，跳过")
        return [], trace
    trace.append(f"步骤0 活跃检查：最近活动 {idle_ms / 60000:.1f}min 前 → 继续")

    # 步骤 1：运行态
    da = next((s for s in sessions if s["role"] == "数字人"), None)
    # CC 状态：判运行态（信号 4）、生成文案、以及 CC 卡死告警都要用，读一次就好。
    # 注意它只有"当下"的值，回放历史时刻时不可信，所以 as_of_ms 不为空时不采。
    cc = {"found": False, "why": "回放历史时刻，CC 只有当下状态、不可信，不采"}
    if as_of_ms is None:
        project = find_cc_project(group_id, human.get("daId"))
        if not project:
            cc = {"found": False, "why": "META.md 里没有这个群聊ID对应的项目"}
        else:
            cc = read_cc_status(project["ccDir"])
            cc["projectKey"] = project["projectKey"]
            if not cc["found"]:
                # 项目找到了但 cc-config 下还没有目录：说明还没进编码阶段，
                # 这是正常的（方案 §8.2 E：定位失败降级为不巡检 CC），不是异常
                cc["why"] = f"项目 {project['projectKey']} 还没进编码阶段（无 CC 目录）"
    kwargs = {"as_of_ms": as_of_ms, "cc": cc}
    if scan_limit:
        kwargs["scan_limit"] = scan_limit
    verdict = judge_running(sessions, **kwargs)
    trace.extend(verdict["evidence"])
    trace.append(f"步骤1 运行态：{verdict['state']}")

    if verdict["state"] == STATE_UNKNOWN:
        trace.append("→ 运行态未知，本轮不做任何判定")
        return [], trace

    # 消息流两个分支都要用，在这里扫一次就好。它是实时写入的（trajectory 不是），
    # 所以中断告警也得看它，不能只看 trajectory。
    limit = scan_limit or TRANSCRIPT_SCAN_LIMIT
    transcript = scan_transcript(da["sessionFile"] if da else "", as_of_ms, limit)
    transcript["cc"] = cc
    trace.append(
        f"消息流：T_user={fmt_ts(transcript['tUser'])}"
        f"　T_da_send_ok={fmt_ts(transcript['tDaSendOk'])}"
        f"　T_da_text={fmt_ts(transcript['tDaText'])}"
        f"　T_model_error={fmt_ts(transcript['tModelError'])}"
        f"　最后对客类型={transcript['lastSendType'] or '(无)'}"
        f"（读了 {transcript['scannedBytes'] / 1024:.0f}KB"
        f"{'，已触顶' if transcript['capped'] else ''}）"
    )

    if verdict["state"] == STATE_IDLE:
        trace.append("→ 空闲，进入中断告警判定")
        traj = resolve_trajectory_file(da["sessionFile"]) if da else ""
        outcome = latest_run_outcome(traj, as_of_ms, scan_limit or TRAJECTORY_SCAN_LIMIT)
        trace.append(
            f"　A 模型异常：消息流 errorMessage={transcript['modelErrorMessage'] or '(无)'}"
            f"　trajectory run={(outcome['runId'] or '?')[:8]}"
            f" status={outcome['status']} terminalError={outcome['terminalError']}"
        )
        if transcript["tModelError"] and transcript["tDaReply"] > transcript["tModelError"]:
            trace.append(
                f"　　出错后数字人于 {fmt_ts(transcript['tDaReply'])} 又对客了 → 已自愈，不告警"
            )
        event = check_model_error(human, group_id, sessions, cfg, now_ms, outcome, transcript)
        if event:
            trace.append(f"→ 命中 {event['type']}：{event['detail']['reason']}")
            return [event], trace

        if cc.get("found"):
            trace.append(
                f"　E CC 卡死：{cc['activeCount']} 个子会话活跃"
                f"　最后触碰 {fmt_ts(cc.get('lastTouch'))}"
                f"　项目 {cc.get('projectKey', '?')}"
            )
        event = check_cc_stalled(human, group_id, sessions, cfg, now_ms, cc)
        if event:
            trace.append(f"→ 命中 {event['type']}：{event['detail']['projectKey']}")
            return [event], trace

        gap = ((da.get("lastInteractionAt") or 0) - transcript["tUser"]) if da else 0
        trace.append(
            f"　二-1 用户消息未获回复：T_user={fmt_ts(transcript['tUser'])}"
            f"　T_da_reply={fmt_ts(transcript['tDaReply'])}"
            f"　网关 lastInteractionAt 比消息流晚 {gap / 1000:+.0f} 秒"
            f"　派活={fmt_ts(transcript['tDispatch'])}"
            f"　明示不对客={transcript['lastNoReply']}"
        )
        trace.append(
            f"　铁律一 派活必有回报：派活={ {k: fmt_ts(v)[:14] for k, v in (transcript['dispatchTo'] or {}).items()} }"
            f"　回报={ {k: fmt_ts(v)[:14] for k, v in (transcript['recvFrom'] or {}).items()} }"
        )
        event = check_sub_not_reported(human, group_id, sessions, cfg, now_ms, transcript)
        if event:
            trace.append(f"→ 命中 {event['type']}：{event['detail']['subs']}")
            return [event], trace

        event = check_user_not_replied(human, group_id, sessions, cfg, now_ms, transcript)
        if event:
            trace.append(f"→ 命中 {event['type']}（形态 {event['detail']['form']}）")
            return [event], trace

        trace.append(
            f"　铁律二-2 回报后必转达：T_no_reply_mark={fmt_ts(transcript['tNoReplyMark'])}"
            f"（明示不对客也算处理过）"
        )
        event = check_da_not_replied_after_sub(human, group_id, sessions, cfg,
                                              now_ms, transcript)
        if event:
            trace.append(f"→ 命中 {event['type']}：sub={event['detail']['sub']}")
            return [event], trace

        trace.append("　全部中断告警判定完毕，无命中")
        return [], trace

    # running：走步骤 F
    latest_event = latest_trajectory_event(
        resolve_trajectory_file(da["sessionFile"]) if da else "", as_of_ms,
        scan_limit or TRAJECTORY_SCAN_LIMIT)
    trace.append(f"trajectory 最新事件：{latest_event or '(取不到)'}")

    # "当前这次 run 的开始时间"，用于文案里的"已运行多久"：
    #   首选 trajectory 的 session.started —— 最精确，回放历史时刻时也正确
    #   回落 sessions.json 的 startedAt   —— trajectory 要到 run 结束才落盘，
    #                                        run 进行中时往往只有它可用
    # 回落必须带 <= now 的校验：回放历史时刻时 sessions.json 只有最终快照，
    # startedAt 可能晚于 as_of，那时算出来是负数，宁可不提这句话。
    run_started_ts = verdict.get("runStartedTs") or 0
    if not run_started_ts:
        candidate = da["startedAt"] if da else 0
        if candidate and candidate <= now_ms:
            run_started_ts = candidate
            trace.append(f"运行时长基准：trajectory 无 session.started，"
                         f"回落 sessions.json.startedAt={fmt_ts(candidate)}")
        else:
            trace.append("运行时长基准：取不到可信值，文案不提运行时长")
    else:
        trace.append(f"运行时长基准：trajectory session.started={fmt_ts(run_started_ts)}")

    event = check_long_running(human, group_id, sessions, cfg, now_ms, latest_event,
                               transcript, run_started_ts)
    if event is None:
        quiet_since = max(transcript["tUser"], transcript["tDaReply"])
        if quiet_since:
            trace.append(
                f"步骤F 静默检查：静默 {(now_ms - quiet_since) / 60000:.1f}min"
                f" ≤ 阈值 {threshold(cfg, 'USER_QUIET_MS') / 60000:.0f}min → 正常运行，不提醒"
            )
        else:
            trace.append("步骤F 静默检查：没有任何对客来往，无基准 → 不提醒")
        return [], trace
    trace.append(f"步骤F 静默检查：静默 {event['detail']['quietMs'] / 60000:.1f}min → 触发提醒")
    return [event], trace


# =================== 功能 5：真的把消息发出去（含去重与灰度闸门）===================
#
# 这是巡检器第一次产生副作用，所以前面串了一串闸门，任何一道不过就不发：
#
#   dryRun     默认 true。true 时仍然调真实脚本，但带 --dry-run，走完整链路只是
#              不投递 —— 这样能验证参数、目标、实例定位都对，而不打扰任何人。
#   白名单     groups.include 为空 = 全部群；非空只对名单内的群巡检。
#
# 后面四道是方案 §5.2 的降噪链，缺一道都会真的打扰到人（下面每道都写了实测依据）：
#
#   同 signature 已通知   →  丢弃
#   同类 30min 冷却内     →  丢弃
#   同群 1 小时超 3 条     →  丢弃
#   quietHours 且非 WARN  →  只写日志

NOTIFIED_PATH = os.path.join(STATE_DIR, "notified.json")


# 每类事件的 signature 由哪些字段拼成 —— 决定"事件算不算变化了、能不能再发一次"。
# REMIND 用最后一次对客来往的时刻：只要用户又说话了、或数字人又回过话了，
# 就是新一轮静默，可以再提醒。
#
# ⚠️ 光靠 signature 不够。实测（USER_QUIET_MS=10s、6 分钟一个任务）：数字人每播报
# 一次进度 T_da_reply 就变、signature 就变，结果 73 轮里实发了 4 条。所以下面
# 的冷却和限流不是可选项。
#
# 📌 已确认的设计取舍（2026-08-22 与用户确认，别当 bug 改掉）：
# **一段连续静默只提醒一次，不管它持续多久。**
# 闸门是串行的：signature 去重在冷却之前。一个长任务里，只要用户不说话、数字人也不
# 回话，signature 逐字不变，每一轮都被第一道拦下，压根走不到冷却那一步 —— 所以
# COOLDOWN_MS 配 1 分钟还是 30 分钟，对"同一段静默"毫无影响。
# 实测日志（群 10232848092，run 11:49:30→11:51:07 共 97 秒）：11:49:46 发出一条，
# 之后 11:49:48~11:51:03 每轮都是"signature 未变"，全程只有 1 条。
# 曾讨论过两种续报方案（固定周期续报 / 按更大时间门槛升级续报），结论是不做：
# 反复说"还在处理中"不提供新信息，第一条已经达到"打消用户疑虑"的目的；固定周期在
# 30 分钟任务上会发 30 条，本身就变成噪音源。
def event_signature(event):
    detail = event.get("detail") or {}
    if event["type"] == "REMIND_LONG_RUNNING":
        return f"{detail.get('tUser', 0)}:{detail.get('tDaReply', detail.get('tDaSendOk', 0))}"
    if event["type"] == "ALERT_SUB_NOT_REPORTED":
        # 按"哪些 sub + 哪次派活"去重：重新派活就是新一件事
        return f"{','.join(detail.get('subs') or [])}:{detail.get('tDispatch', 0)}"
    if event["type"] == "ALERT_DA_NOT_REPLIED_AFTER_SUB":
        # 按"哪次回报"去重：来了新回报就是新一件事
        return f"{detail.get('sub', '')}:{detail.get('tRecv', 0)}"
    if event["type"] == "ALERT_USER_NOT_REPLIED":
        # 按"哪条用户消息 + 哪种形态"去重：用户再发新消息就是新一件事，可以再报
        return f"{detail.get('form', '')}:{detail.get('tUser', 0)}"
    if event["type"] == "ALERT_MODEL_ERROR":
        # 按 runId 去重：一个失败的 run 只告一次。方案建议用 model.completed.ts，
        # 但 runId 更直接，而且同一 run 里 model.completed 和 session.ended 都可能
        # 带异常标志，用时间戳会把同一件事算成两件。
        return f"{detail.get('runId', '')}:{detail.get('reason', '')}"
    return str(detail.get("quietSince", ""))


def group_allowed(cfg, group_id):
    """白名单过滤。groups.include 为空表示不限制。"""
    include = ((cfg or {}).get("groups") or {}).get("include") or []
    return not include or str(group_id) in [str(g) for g in include]


def parse_quiet_hours(spec):
    """解析 "HH:MM-HH:MM" 成 (起始分钟, 结束分钟)；解析不了返回 None。"""
    if not isinstance(spec, str) or "-" not in spec:
        return None
    start, _, end = spec.partition("-")

    def to_minutes(text):
        parts = text.strip().split(":")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            return None
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 24 and 0 <= minute < 60):
            return None
        return hour * 60 + minute

    start_m, end_m = to_minutes(start), to_minutes(end)
    if start_m is None or end_m is None:
        return None
    return start_m, end_m


def in_quiet_hours(cfg, now_ms):
    """现在是否处于静默时段。

    必须支持跨零点的区间（如 22:00-08:00）—— 直接写 start <= now < end 在跨零点时
    恒为假，静默时段等于形同虚设。
    """
    window = parse_quiet_hours((cfg or {}).get("quietHours"))
    if not window:
        return False
    start_m, end_m = window
    if start_m == end_m:
        return False        # 零长度区间，视为没配
    local = datetime.fromtimestamp(now_ms / 1000)
    now_m = local.hour * 60 + local.minute
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m      # 跨零点


class NotifiedStore:
    """记住"发过什么、什么时候发的"，供去重 / 冷却 / 限流三道闸门判断。

    存成：
      {"events": {"<sessionKey>|<type>": {"sig": ..., "ts": ...}},
       "groupSends": {"<群号>": [发送时刻, ...]}}
    """

    def __init__(self, path=None):
        # 默认值必须在**调用时**取，不能写成 path=NOTIFIED_PATH ——
        # 那样会在类定义时就把当时的值烧死，之后改模块变量对它无效。
        # 实测 2026-08-25 因此踩坑：验证脚本改了 NOTIFIED_PATH 想隔离，
        # 而 inspect_once 里的 NotifiedStore() 照旧读写**真实**状态文件，
        # 只因为那几轮恰好 0 条动作才没把真实 notified.json 写脏。
        path = path or NOTIFIED_PATH
        self.path = path
        self.events = {}
        self.group_sends = {}
        loaded = json_object_from_file(path)
        if loaded is None:
            return       # 首次运行或文件坏了，当空的重新攒
        if "events" in loaded:
            self.events = loaded.get("events") or {}
            self.group_sends = loaded.get("groupSends") or {}
        else:
            # 老格式是扁平的 {key: signature}。迁移过来并把时刻记为 0，
            # 等于"冷却已过"，这样升级后不会漏发，也不会丢去重。
            self.events = {k: {"sig": v, "ts": 0} for k, v in loaded.items()
                           if isinstance(v, str)}

    @staticmethod
    def _key(event):
        return f"{event['sessionKey']}|{event['type']}"

    def _recent_group_sends(self, group_id, now_ms, window_ms):
        return [t for t in self.group_sends.get(str(group_id), [])
                if isinstance(t, (int, float)) and now_ms - t < window_ms]

    def gate(self, event, cfg, now_ms):
        """走完降噪链。返回 (是否放行, 拦住的原因)。放行时原因为空串。"""
        record = self.events.get(self._key(event)) or {}

        if record.get("sig") == event_signature(event):
            return False, "同一情况已通知过（signature 未变）"

        cooldown = threshold(cfg, "COOLDOWN_MS")
        last_ts = record.get("ts") or 0
        if last_ts and now_ms - last_ts < cooldown:
            left = (cooldown - (now_ms - last_ts)) / 60000
            return False, f"同类事件冷却中，还剩 {left:.1f}min"

        window = threshold(cfg, "GROUP_RATE_WINDOW_MS")
        limit = threshold(cfg, "GROUP_RATE_MAX")
        recent = self._recent_group_sends(event["groupId"], now_ms, window)
        if len(recent) >= limit:
            return False, (f"本群限流：{window / 3600000:.0f} 小时内已发 {len(recent)} 条"
                           f"（上限 {limit}）")

        return True, ""

    def record(self, event, now_ms, cfg=None):
        """记下这次发送，供后续三道闸门判断。"""
        self.events[self._key(event)] = {"sig": event_signature(event), "ts": now_ms}
        group_id = str(event["groupId"])
        # 落盘只留一段时间内的，避免文件无限长大。但**不能写死 24 小时** ——
        # 裁剪窗口比限流窗口短的话，会把仍该计数的记录砍掉，限流直接失效。
        # 实测反例（GROUP_RATE_WINDOW_MS 配 3 天、上限 3）：在 70/40/10 小时前各发
        # 一条，每次 record 都按"当下往前 24 小时"裁剪，前两条被砍，groupSends 只剩
        # 1 条，第 4 条于是被放行 —— 而 3 天窗口内其实已经发满 3 条。
        # 所以裁剪窗口取"配置的限流窗口"和 24 小时里更大的那个。
        keep_window = max(24 * 3600 * 1000, threshold(cfg, "GROUP_RATE_WINDOW_MS"))
        kept = [t for t in self.group_sends.get(group_id, [])
                if isinstance(t, (int, float)) and now_ms - t < keep_window]
        kept.append(now_ms)
        self.group_sends[group_id] = kept

    def save(self):
        """原子写：先写临时文件再改名，避免被中途打断留下半个坏文件。"""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"events": self.events, "groupSends": self.group_sends},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"notified.json 写入失败：{e!r}")


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text):
    """去掉终端颜色转义。

    openclaw 的插件日志是带色的，原样记进 errors.jsonl / 终端日志会变成
        [35m[plugins][39m [31m[京Me插件] 获取应用令牌失败[39m
    实测 2026-08-24 就是这样，读起来很费劲，而且污染落盘的记录。
    """
    return ANSI_ESCAPE_RE.sub("", text or "")


def send_failure_reason(proc):
    """从 send-user-message.py 的失败输出里抠出**真正的原因**。

    不能直接截原始输出的前 N 个字符。该脚本失败时走 emit(result, 5)，把整个 result
    当 JSON 打到 stderr，而有用的三个字段是**追加在末尾**的：

        {"ok": false, "messageType": ..., "target": ..., "userErp": null,
         "userName": null, ...,
         "error": "openclaw message send 失败",   ← 真正的原因从这里才开始
         "sendStderr": "...", "sendCode": N}

    实测（2026-08-24 断网验证）：截前 200 字正好停在 "userName"，连着十几轮日志全是
    一模一样的无用前缀，排查时等于什么都没记。
    """
    raw = strip_ansi((proc.stderr or proc.stdout or "")).strip()
    detail = json_object(raw)
    if detail is None:
        return raw[:300] or "（没有任何输出）"
    parts = [detail.get("error") or "脚本未说明原因"]
    if detail.get("sendCode") is not None:
        parts.append(f"openclaw 退出码={detail['sendCode']}")
    stderr = strip_ansi(detail.get("sendStderr") or "").strip()
    if stderr:
        parts.append(stderr[:500])
    return " ｜ ".join(parts)


def recovery_hint(cfg):
    """告警末尾那句"接下来会怎样"。**必须和 wake.enabled 的真实状态一致。**

    2026-08-24 发现这批文案和实际行为全是错位的，而且是两个相反方向：
      · ALERT_SUB_NOT_REPORTED / ALERT_DA_NOT_REPLIED_AFTER_SUB / ALERT_CC_STALLED /
        ALERT_USER_NOT_REPLIED 写着"正在补处理…""正在重发…""正在重试…"，
        可唤起默认是关的 —— wake.enabled=false 时根本没人补处理，等于骗用户干等；
      · ALERT_MODEL_ERROR 反过来写"请回复任意消息以继续"，开了唤起之后这句又多余，
        用户以为得自己动手，其实巡检器已经替他唤起了。

    所以尾句只能由这一个地方按开关的真实状态生成，各告警正文只描述"发生了什么"，
    不许自己承诺"接下来会怎样"。

    唤起成功与否在发这条消息时还不知道（顺序是先告诉用户、再唤起），所以措辞要把
    两种结果都盖住：唤起成功用户不用管，唤起失败用户还知道自己能救。
    """
    if wake_enabled(cfg):
        return "已自动唤起数字人继续处理；若仍无进展，请回复任意消息。"
    return "请回复任意消息以继续。"


def send_group_message(human, event, dry_run):
    """调数字人自己的 send-user-message.py 往群里发一条消息。

    返回 (是否成功, 说明)。dry_run=True 时带 --dry-run，脚本走完整流程但不投递。
    """
    script = human.get("sendScript")
    if not script:
        return False, f"找不到 send-user-message.py（数字人 {human['daId']}）"
    if not event.get("target"):
        return False, "事件里没有投递目标（target 为空）"

    cmd = [
        openclaw_python(), script,
        "--message-type", "progress",   # progress 是唯一不需要 @ 人的类型，巡检提醒正合适
        "--target", event["target"],
        "--content", event["text"],
    ]
    if dry_run:
        cmd.append("--dry-run")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                              cwd=cli_cwd_for(human))
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"调用失败：{e!r}"
    if proc.returncode != 0:
        return False, f"退出码 {proc.returncode}：{send_failure_reason(proc)}"

    # 脚本输出 JSON，据此确认到底投递了没有
    result = json_object(proc.stdout)
    if result is None:
        return False, f"输出不是 JSON 对象：{proc.stdout.strip()[:200]}"
    if dry_run:
        return True, f"dryRun 通过（未投递），最终文本 {len(result.get('finalMessage') or '')} 字"
    if result.get("sent") is True:
        return True, "已投递"
    return False, f"脚本未投递：sent={result.get('sent')} warning={result.get('warning')}"


def still_relevant(human, group_id, sessions, event, cfg, now_ms):
    """发送前复查：这条提醒/告警现在还成立吗？返回 (是否仍成立, 过时原因)。

    为什么需要这一道：判定和真正投递之间有可观的间隔（调发送脚本实测约 5 秒，
    加上轮询间隔），这期间数字人可能已经把结果回给用户了。实测踩过
    （2026-08-21 群 10232848092）：数字人 15 秒就答完了，而"正在处理中"的提醒在
    它回答之后才落地，用户看到的是一条明显过时的消息。

    ⚠️ 复查必须和判定用**同一把尺子**。第一版只看 trajectory 的最新标记，比判定窄得多
    （判定看信号 1/2/3），后果是：凡是靠信号 1（sessions.json status=running）判出来的
    running，复查一律丢弃 —— 不是偶发，是必然。
    实测（2026-08-22 11:29 群 10232848092）：run 11:29:10→11:29:21 真在跑，
    sessions.json 已经是 running，但 trajectory 要到 run 结束才落盘，复查读到的还是
    上一个 run 的 session.ended @ 11:11:00，于是报"数字人已于 18min 前结束"把提醒丢了。
    现在改为重新读盘 + 重跑 judge_running，两边口径一致。
    """
    da = next((s for s in sessions if s["role"] == "数字人"), None)
    if da is None or not da["sessionFile"]:
        return True, ""
    detail = event.get("detail") or {}

    # 重新读盘：传进来的 sessions 是本轮开始时的快照，这期间可能已经变了
    fresh_groups = collect_group_sessions(human["instDir"], human["daId"])
    fresh_sessions = fresh_groups.get(group_id) or sessions
    fresh_da = next((s for s in fresh_sessions if s["role"] == "数字人"), da)
    traj = resolve_trajectory_file(fresh_da["sessionFile"])

    if event["type"] == "REMIND_LONG_RUNNING":
        # 1) 用和判定完全相同的口径重新判一次运行态
        verdict = judge_running(fresh_sessions)
        if verdict["state"] != STATE_RUNNING:
            return False, (f"数字人已不在运行中（复查判定 {verdict['state']}："
                           f"{verdict['evidence'][0]}），提醒已过时")
        # 2) 数字人已经回过话了 → 静默被打破，用户不需要这条提醒
        fresh = scan_transcript(fresh_da["sessionFile"])
        newer = max(fresh["tUser"], fresh["tDaReply"])
        if newer > (detail.get("quietSince") or 0):
            return False, (f"期间已有新的对客来往（{fmt_ts(newer)}），"
                           f"静默已被打破，提醒已过时")
        return True, ""

    if event["type"] == "ALERT_MODEL_ERROR":
        # 又跑起来了说明已经自行恢复，不用再告诉用户"本轮已停止"
        verdict = judge_running(fresh_sessions)
        if verdict["state"] == STATE_RUNNING:
            return False, "数字人已重新开始运行，告警已过时"
        outcome = latest_run_outcome(traj)
        if outcome["found"] and outcome["runId"] != detail.get("runId"):
            return False, f"最近 run 已换成 {outcome['runId'][:8]}，告警已过时"
        return True, ""

    return True, ""


# ==================== 功能 16：唤起数字人 ====================
#
# 方案 §5.1 动作矩阵要求 5 类 ALERT 都"唤起数字人"（REMIND 不唤起）。旧实现一条都没做。
#
# CLI 用法和方案、旧实现说的都不一样，是实测出来的（openclaw 2026.7.1）：
#   · 方案说走 `openclaw sessions send` —— **不存在**。sessions 的子命令只有
#     cleanup / compact / export-trajectory / list / tail。sessions_send 是数字人用的
#     **工具名**（agent 间发消息），方案把工具名当成 CLI 命令写了。
#   · 旧实现用 `openclaw agent --system-event --no-wait` —— **两个 flag 都不存在**，
#     在这个版本上必然失败。
#   · 真正可用的是：openclaw agent --session-key <key> --message <text>
#
# 不加 --deliver：数字人对客有自己的规约（走 send-user-message.py），让 CLI 把 agent
# 回复直接投递到群里会绕过规约，还可能和数字人自己的对客重复。
#
# 不阻塞：openclaw agent 会跑完一整轮才返回（默认超时 600 秒）。巡检器不能为一次唤起
# 卡十分钟，所以只等一小段时间捕捉"立刻失败"（session key 写错、gateway 没起），
# 之后放它在后台跑完。

# 唤起消息的前缀。它必须能被巡检器自己认出来并从 tUser 里排除 —— 唤起消息是以
# role=user 进入数字人会话的，不排除的话：
#   · 铁律二-1 会把自己发的唤起当成"用户消息没人回"来告警
#   · REMIND 的静默基准会被自己的唤起重置
# 也就是巡检器跟自己对话、越告警越有话说。
WAKE_MESSAGE_PREFIX = "[巡检器]"

# 哪些事件要唤起（方案 §5.1：5 类 ALERT 全部唤起，REMIND 不唤起）。
WAKE_EVENT_TYPES = (
    "ALERT_MODEL_ERROR",
    "ALERT_CC_STALLED",
    "ALERT_SUB_NOT_REPORTED",
    "ALERT_USER_NOT_REPLIED",
    "ALERT_DA_NOT_REPLIED_AFTER_SUB",
)

# 唤起后最多等多久拿"真送到了"的证据。到点还没证据就如实说"未确认"，不谎报成功。
WAKE_PROBE_SECONDS = 8
# 等证据时的轮询间隔
WAKE_POLL_SECONDS = 0.5

# 还没拿到结论的唤起。放后台的那些必须在后续轮次里把真实结果补报出来，
# 否则日志永远停在"未确认"，等于没记。
_pending_wakes = []


def wake_landed_ts(session_file):
    """消息流里最后一条巡检器唤起消息的时间戳；0 表示还没有。

    为什么用消息流而不是 trajectory：<sid>.jsonl 是**实时写**的，trajectory 要到 run
    结束才 flush。唤起当下唯一拿得到的证据就是它。
    """
    if not session_file:
        return 0
    return scan_transcript(session_file).get("tWake") or 0


def da_session_file(sessions):
    """从会话列表里取数字人自己那条的消息流路径。"""
    for s in sessions or []:
        if s.get("role") == "数字人" and s.get("sessionFile"):
            return s["sessionFile"]
    return ""


def report_pending_wakes():
    """把放到后台的唤起的最终结果补报出来。返回 [(kind, 说明)]。

    实测 2026-08-24 16:04:27：日志打了"唤起已发起：数字人正在处理"，其实那次**根本
    没送到** —— 消息流里压根没有那条唤起消息。原因是当时只凭"8 秒内没退出"就断定成功，
    而当时网络正在抽（京Me 令牌 fetch failed），它在探测窗口之后才失败。
    慢速失败和成功长得一模一样，所以必须回头看真实结果。
    """
    lines = []
    for item in list(_pending_wakes):
        proc = item["proc"]
        landed = wake_landed_ts(item["sessionFile"]) > item["before"]
        if landed:
            _pending_wakes.remove(item)
            lines.append(("acted", f"{item['tag']} → 唤起已确认送达（后台补报）"))
            continue
        code = proc.poll()
        if code is None:
            continue                      # 还在跑，下一轮再看
        _pending_wakes.remove(item)
        output = strip_ansi((proc.stdout.read() if proc.stdout else "") or "").strip()
        if code == 0:
            lines.append(("acted", f"{item['tag']} → 唤起进程正常退出，但消息流里没有"
                                   f"这条唤起的痕迹，实际未送达：{output[:300] or '无输出'}"))
        else:
            lines.append(("acted", f"{item['tag']} → 唤起失败（后台补报）退出码 {code}："
                                   f"{output[:300] or '无输出'}"))
    return lines


def wake_enabled(cfg):
    """唤起总开关。默认**关闭** —— 方案 §6.3 明确 wake.enabled 在 M4 前默认 false。

    发消息只是打扰用户，唤起是让数字人真的开始干活，副作用重一个量级，必须显式打开。
    """
    return bool(((cfg or {}).get("wake") or {}).get("enabled", False))


# 进行中提醒类事件。单独列出来而不是靠 "REMIND_" 前缀判断：新增事件类型时必须
# 显式决定它归不归 remind 开关管，靠前缀会让人忘了这件事。
REMIND_EVENT_TYPES = ("REMIND_LONG_RUNNING",)


def remind_enabled(cfg):
    """进行中提醒总开关（方案 §6.3 列了这个开关，之前一直没实现）。默认**开启**。

    和 wake.enabled 的默认值刚好相反，理由不同：
      · 唤起会让数字人真的跑一轮，副作用重，所以默认关、必须显式打开；
      · 进行中提醒是这个巡检器的主职（实测触发 69 次，占全部事件的 75%），
        默认关等于装了个不干活的巡检器。

    关掉后 REMIND 只写日志、不投递、**不记 notified**。
    不记的理由和 quietHours 一致：这条压根没发出去，不该占用去重和冷却的额度，
    开关再打开时还能补发。
    保留日志的理由：否则排查时会误以为"判定漏了"，而实际是被开关压住了。
    """
    return bool(((cfg or {}).get("remind") or {}).get("enabled", True))


def wake_message_for(event):
    """给数字人看的唤起文案。带前缀，好让巡检器认出这是自己发的。"""
    detail = event.get("detail") or {}
    reason = {
        "ALERT_MODEL_ERROR":
            f"上一轮模型调用异常（{detail.get('reason', '未知')}），请继续处理未完成的事情",
        "ALERT_CC_STALLED":
            "代码执行子任务长时间无响应，请检查并补处理",
        "ALERT_SUB_NOT_REPORTED":
            f"派给 {'、'.join(detail.get('subs') or ['基础 Agent'])} 的任务迟迟没有回报，"
            f"请主动追问或补处理",
        "ALERT_USER_NOT_REPLIED":
            "用户的消息似乎没有得到回复，请检查并回复用户",
        "ALERT_DA_NOT_REPLIED_AFTER_SUB":
            f"已收到 {detail.get('sub', '基础 Agent')} 的结果但没有转达给用户，请转达",
    }.get(event["type"], "检测到异常，请检查当前会话状态")
    return f"{WAKE_MESSAGE_PREFIX} {reason}。"


def wake_digital_human(human, event, dry_run, sessions=None, tag=""):
    """唤起数字人继续处理。返回 (状态, 说明)，状态是三态：

        "delivered"    有证据：唤起消息已经进了数字人会话
        "failed"       明确失败
        "unconfirmed"  还没结论，已挂到 _pending_wakes，后续轮次补报

    **不能拿"进程没退出"当成功。** 实测 2026-08-24 16:04:27 就是这么谎报的：日志写
    "已发起，数字人正在处理"，而消息流里压根没有那条唤起消息 —— 当时网络在抽，
    openclaw 在 8 秒探测窗口之后才失败。慢速失败和成功长得一模一样，所以要么拿到
    证据，要么如实说不知道。

    dry_run 时**完全不执行** —— openclaw agent 没有 dry-run 开关，任何调用都是真的让
    数字人跑一轮，所以干跑阶段只打印将要执行的命令。
    """
    session_key = event.get("sessionKey")
    if not session_key:
        return "failed", "事件里没有 sessionKey，无法定位要唤起哪个会话"

    message = wake_message_for(event)
    cmd = [OPENCLAW_BIN, "agent", "--session-key", session_key, "--message", message]
    if dry_run:
        port = gateway_port_for(human.get("daId"))
        return "delivered", (f"dryRun 未执行。将要跑：openclaw agent --session-key "
                             f"{session_key} --message '{message[:40]}…'"
                             f"（gateway 端口 {port or '未指定'}）")

    # 先记下基线，之后靠"它有没有变大"来判断到底送到没有
    session_file = da_session_file(sessions)
    before = wake_landed_ts(session_file)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=cli_env_for(human),
                                cwd=cli_cwd_for(human))
    except (OSError, subprocess.SubprocessError) as e:
        return "failed", f"启动失败：{e!r}"

    deadline = time.time() + WAKE_PROBE_SECONDS
    while time.time() < deadline:
        if session_file and wake_landed_ts(session_file) > before:
            return "delivered", "已确认送达（唤起消息已进入数字人会话），数字人开始处理"
        if proc.poll() is not None:
            break
        _stop.wait(WAKE_POLL_SECONDS)          # 可被 Ctrl-C / SIGTERM 打断

    code = proc.poll()
    if code is None:
        if not session_file:
            return "unconfirmed", (f"已启动但无法确认 —— 定位不到数字人消息流，"
                                   f"没有证据可查（{WAKE_PROBE_SECONDS}s 内未结束）")
        _pending_wakes.append({"proc": proc, "sessionFile": session_file,
                               "before": before, "tag": tag})
        return "unconfirmed", (f"已启动，但 {WAKE_PROBE_SECONDS}s 内消息流里还没出现这条"
                               f"唤起，尚不能确认送达；结果由后续轮次补报")

    output = strip_ansi((proc.stdout.read() if proc.stdout else "") or "").strip()
    if session_file and wake_landed_ts(session_file) > before:
        return "delivered", "已确认送达并跑完一轮"
    if code == 0:
        return "failed", (f"进程正常退出，但消息流里没有这条唤起的痕迹，实际未送达："
                          f"{output[:400] or '无输出'}")
    return "failed", f"退出码 {code}：{output[:400] or '（没有任何输出）'}"


def handle_events(human, group_id, sessions, events, cfg, notified, now_ms=None):
    """把判定出的事件走完降噪链并发送。

    返回 [(kind, 说明)]，kind 为：
      "acted"      真发了 / 发失败 —— 这些必须每次都打日志
      "suppressed" 被闸门拦下 —— 只汇总条数，不每轮刷一行
    为什么要分开：一段持续静默里 signature 不变，每一轮都会被第一道闸门拦下。
    实测（2026-08-22 13:57~13:59）5 秒一轮，"拦下：signature 未变"连着刷了几十屏，
    真正需要注意的信息反而被埋掉。

    顺序按方案 §5.2，前面加了类别总开关、末尾加了一道发送前复查：
      remind 总开关 → signature 去重 → 同类冷却 → 同群限流 → quietHours
      → 发送前复查 → 真发

    总开关放最前面：被它关掉的事件根本不会发，不该白占去重和冷却的额度。

    只有真发成功才写入 notified.json：被 quietHours 压住的事件不记，等静默时段过了
    还能补发；发失败的也不记，下一轮会重试。
    复查判定已过时的也不记 —— 那件事本身没发生过，不该占用去重和冷却的额度。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    dry_run = (cfg or {}).get("dryRun", True)
    lines = []
    for event in events:
        tag = f"【{event['severity']}】{event['type']} 群 {event['groupId']}"

        # 方案 §6.3 的 remind 总开关。只压 REMIND，ALERT 照发 —— 关掉"进行中提醒"
        # 不该连"出故障了"一起关掉。
        if event["type"] in REMIND_EVENT_TYPES and not remind_enabled(cfg):
            lines.append(("suppressed",
                          f"{tag} → 进行中提醒已关闭（remind.enabled=false），只写日志"))
            continue
        # 联调期阈值被压小时，标出"这条只因为阈值小才成立"，省得每条都手工回放去分辨。
        # 阈值调回生产值后自动消失。
        debug_note = debug_only_note(event, cfg)
        if debug_note:
            tag += "［调试］"

        allow, reason = notified.gate(event, cfg, now_ms)
        if not allow:
            lines.append(("suppressed", f"{tag} → 拦下：{reason}"))
            continue

        # quietHours 内非 WARN 只写日志。不记 signature，出了静默时段还能补发。
        if event["severity"] != "WARN" and in_quiet_hours(cfg, now_ms):
            lines.append(("suppressed",
                          f"{tag} → 静默时段（{cfg.get('quietHours')}）只写日志，不发送"))
            continue

        fresh, stale_reason = still_relevant(human, group_id, sessions, event, cfg, now_ms)
        if not fresh:
            lines.append(("suppressed", f"{tag} → 发送前复查不通过，丢弃：{stale_reason}"))
            continue

        # 群里那条也标上，否则你只看到群消息时无从判断这条该不该出现。
        # 用副本改，不动原事件 —— signature / 冷却记的都是原文。
        sent_event = event
        if debug_note:
            sent_event = dict(event,
                              text=f"{event['text']}\n　　［{debug_note}］")

        send_started = time.time()
        ok, note = send_group_message(human, sent_event, dry_run)
        send_ms = int((time.time() - send_started) * 1000)
        prefix = "dryRun" if dry_run else "实发"
        if debug_note:
            lines.append(("acted", f"{tag} → {debug_note}"))
        if ok:
            notified.record(event, now_ms, cfg)
            lines.append(("acted", f"{tag} → {prefix}成功：{note}（耗时 {send_ms / 1000:.1f}s）"))
            # 投递本身要花时间，这段时间里情况可能已经变了。发送前复查在投递**之前**
            # 就做完了，管不到路上这几秒。消息撤不回来，但必须把真相记下来 ——
            # 否则你只看到群里一条过时消息，无从判断是判定错了还是竞态。
            #
            # 实测 2026-08-25 16:06 的例子：
            #   16:06:25 用户发"状态"
            #   16:06:35 判定：静默 10 秒 > 阈值 8 秒 → 触发（此刻数字人确实还没回）
            #   16:06:44 数字人投出状态汇总
            #   16:06:49 巡检消息才落到群里 —— 投递耗了 14 秒，比整个阈值还长
            # 判定没错，是消息在路上过时了。阈值远大于投递耗时才不会有这个问题。
            if not dry_run:
                after_ok, after_why = still_relevant(human, group_id, sessions, event, cfg,
                                                     int(time.time() * 1000))
                if not after_ok:
                    lines.append(("acted",
                                  f"{tag} → ⚠️ 这条到群里时已过时：投递耗时 {send_ms / 1000:.1f}s，"
                                  f"期间{after_why}。判定当时没错，是竞态；"
                                  f"阈值远大于投递耗时才能避免"))
        else:
            lines.append(("acted", f"{tag} → {prefix}失败：{note}"))
            continue         # 消息都没发出去，就先别唤起数字人

        # 方案 §5.1：5 类 ALERT 都要唤起数字人继续处理，REMIND 只提醒不唤起。
        # 顺序是先告诉用户、再唤起数字人：万一唤起失败，用户至少已经知道出事了。
        if event["type"] not in WAKE_EVENT_TYPES:
            continue
        if not wake_enabled(cfg):
            lines.append(("acted", f"{tag} → 唤起已关闭（wake.enabled=false），跳过"))
            continue
        status, wake_note = wake_digital_human(human, event, dry_run,
                                               sessions=sessions, tag=tag)
        label = {"delivered": "唤起已送达", "failed": "唤起失败",
                 "unconfirmed": "唤起未确认"}[status]
        lines.append(("acted", f"{tag} → {label}：{wake_note}"))
    return lines


# ==================== 功能 7：联调友好（单例锁 / 自动重载 / 热加载）====================
#
# 场景：一边在终端里手动开着巡检器看日志，一边还在改代码。
# 不处理的话有两个坑：
#   1. Python 启动时就把代码读进内存了，改完文件那个进程还在跑旧代码 ——
#      看到的日志和改的代码不一致，比 bug 本身更费时间。
#   2. 手动开着一个、验证时又起一个 → 两个实例各发一遍消息。

LOCK_PATH = os.path.join(STATE_DIR, "inspector.lock")

SELF_PATH = os.path.abspath(__file__)


def acquire_singleton():
    """拿单例锁。已被占用就返回 None（连同占用者 pid），让调用方决定怎么退。

    返回 (锁文件对象, 占用者pid)。拿到锁时 pid 为 None。
    锁文件对象必须一直被引用着，被 GC 掉就等于解锁了。
    """
    try:
        import fcntl
    except ImportError:
        return None, None          # 非 POSIX 平台，不加锁
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip()
        handle.close()
        return None, holder or "未知"
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle, None


def code_version():
    """给日志用的代码版本标识：文件修改时间 + git short hash（拿不到就省略）。"""
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(SELF_PATH)).strftime("%m-%d %H:%M:%S")
    except OSError:
        mtime = "?"
    try:
        proc = subprocess.run(["git", "-C", os.path.dirname(SELF_PATH),
                               "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5,
                              cwd=stable_cwd())
        git = proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        git = ""
    return f"mtime={mtime}" + (f" git={git}" if git else "")


def code_compiles(path=None):
    """一份代码能不能编译过。返回 (是否OK, 报错说明)。默认检查自己。

    重载前必须过这一关：改到一半存盘（语法不完整）时直接重启会让进程崩掉，
    而这个进程正是用户开着看日志的那个。编译不过就继续用内存里的旧代码跑。

    用内置 compile() 直接编源码文本，不写任何文件。别用
    py_compile.compile(cfile=os.devnull) —— 它会因为"不能往非普通文件写字节码"
    永远抛 FileExistsError，等于校验恒不通过、重载永远不发生（实测踩过，而且因为
    失败方向是安全的，日志看起来很正常，极易蒙过去）。
    """
    path = path or SELF_PATH
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
        compile(source, path, "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"第 {e.lineno} 行：{e.msg}"
    except (OSError, ValueError) as e:
        return False, repr(e)


def reload_self(lock_handle):
    """用 execv 把自己换成新代码。成功的话这个函数不会返回。"""
    if lock_handle is not None:
        # Python 的文件描述符默认带 CLOEXEC，exec 后锁会自动释放；这里显式关掉，
        # 让新进程能干净地重新拿锁。
        try:
            lock_handle.close()
        except OSError:
            pass
    try:
        os.execv(sys.executable, [sys.executable, SELF_PATH] + sys.argv[1:])
    except OSError as e:
        log(f"重载失败，继续用当前代码跑：{e!r}")


# ==================== 功能 8：模型异常中断 ALERT_MODEL_ERROR ====================
#
# idle 分支的第一条告警。判据只看 trajectory 里的字段，不依赖那三条"必然要有回复"
# 的铁律，所以是 idle 分支里最独立、最不容易误报的一条。
#
# ⚠️ 方案 §8.2 的 A 条件不能照抄。它写的是：
#     if lm 存在 AND (T_da_ended 不存在 OR lm.ts >= T_da_ended) AND ...
# 但一个 run 内的事件顺序固定是
#     context.compiled(3) → prompt.submitted(4) → model.completed(5)
#     → trace.artifacts(6) → session.ended(7)
# model.completed 必然早于 session.ended（实测 83/83 个 run 无一例外），所以在 idle
# 分支里 lm.ts >= T_da_ended 恒假，照抄就是一条永不触发的死代码。
# 这里改成按 runId 圈定范围：取最近结束的那个 run，只看它自己的事件。
#
# 另外方案只点了 4 个异常字段，实际 schema 有 7 个，漏掉的恰是"静默中断"的典型形态
# （timedOutByRunBudget / timedOutDuringToolExecution）。下面按实际字段全查。

# 表示"这一 run 异常结束"的布尔标志，及其人话说明。
MODEL_ERROR_FLAGS = {
    "timedOut": "模型调用超时",
    "idleTimedOut": "空闲超时",
    "timedOutDuringCompaction": "上下文压缩时超时",
    "timedOutDuringToolExecution": "工具执行时超时",
    "timedOutByRunBudget": "达到单轮预算上限",
    "aborted": "被中断",
}

# externalAbort 不算故障：它表示外部主动取消（用户点了停止），告警只会打扰人。
# 方案原文也强调了"aborted 且非 externalAbort"，这一点是对的，保留。
EXTERNAL_ABORT_FLAG = "externalAbort"

# session.ended.data.terminalError 里的错误码 → 人话。方案完全没提这个字段，但
# 实测断网触发的真实异常里，它是唯一带具体原因的字段（见下面的实测记录）。
# 认不出的码原样带出来，不要吞掉。
TERMINAL_ERROR_LABELS = {
    "non_deliverable_terminal_turn": "本轮结果无法送达（网络或网关不可用）",
}

RUN_OUTCOME_TYPES = ("model.completed", "session.ended")


def latest_run_outcome(traj_file, as_of_ms=None, scan_limit=TRAJECTORY_SCAN_LIMIT):
    """倒扫 trajectory，取"最近一个已结束的 run"的收尾情况。

    为什么要按 runId 圈：同一 run 的 model.completed 和 session.ended 都带异常标志，
    但它们的时间戳一定是前者更早。只有把两者归到同一个 runId 下一起看，才能判断
    "这一轮是不是异常收场"，而不是拿时间戳互相比较（那样恒假）。

    返回 {"runId","endedTs","status","flags":[(字段, 说明)],"promptErrorSource",
          "externalAbort","found":bool}
    """
    out = {"runId": "", "endedTs": 0, "status": None, "flags": [],
           "promptErrorSource": None, "externalAbort": False,
           "terminalError": None, "found": False}
    if not traj_file:
        return out
    target_run = None
    try:
        for raw, _ in iter_lines_reverse(traj_file, max_bytes=scan_limit):
            event = json_object(raw)
            if event is None:
                continue
            if event.get("type") not in RUN_OUTCOME_TYPES:
                continue
            ts = iso_to_ms(event.get("ts"))
            if as_of_ms is not None and ts > as_of_ms:
                continue
            run_id = event.get("runId") or ""
            if target_run is None:
                # 倒着扫，第一个碰到的 session.ended 就是最近结束的那个 run。
                # 如果先碰到 model.completed（说明这一 run 还没写 ended），也用它定 run。
                target_run = run_id
                out["runId"] = run_id
                out["found"] = True
            elif run_id != target_run:
                break        # 已经翻到上一个 run 了，收工

            data = event.get("data") or {}
            if event["type"] == "session.ended":
                out["endedTs"] = ts
                out["status"] = data.get("status")
                if data.get("terminalError"):
                    out["terminalError"] = data["terminalError"]
            if data.get("promptErrorSource"):
                out["promptErrorSource"] = data["promptErrorSource"]
            if data.get(EXTERNAL_ABORT_FLAG) is True:
                out["externalAbort"] = True
            for field, label in MODEL_ERROR_FLAGS.items():
                if data.get(field) is True and (field, label) not in out["flags"]:
                    out["flags"].append((field, label))
    except OSError:
        pass
    return out


# ============ 功能 12：两条派活相关的铁律（旧实现的重灾区，判据全部重新推导）============
#
# 旧实现在这两条上是 93 次告警、误报率 100%（铁律一）和 48%（铁律二-2）。
# 根因见 README 的 P0-c：它用 `T_sub_ended > T_da_recv_from_sub` 表达"回报没来"，
# 但基础 Agent 的 endedAt 必然晚于它发出回报的时刻（实测 5 个 sub 里 4 个差 4~23 秒），
# 所以那个条件恒真、恒告警。
#
# 这里换锚点：**只看数字人自己消息流里的 派活时刻 vs 回报时刻**，完全不碰基础 Agent
# 的 endedAt。消息流是实时写入的，而且两个时刻在同一个文件里、同一套时钟下，可比。
#
# 判据依据的实测数据（2026-08-22 15:34~15:55 群 10232962603，一次真实交付流程）：
#   · 7 次派活/转发**全部**收到回报，滞后 4/8/11/13/32/32/60 秒
#     → 方案的 SUB_REPORT_LAG_MS=90s 宽限合理，留了 30 秒余量
#   · sessions_send 有两种用途：[任务交接卡]（派新活）3 次、[用户回复转发] 4 次，
#     两种都收到了回报，所以不区分对待
#   · 回报之后数字人的第一条 assistant 文本：5 次是 NO_REPLY 家族、3 次是真实文本
#     → 铁律二-2 必须严格豁免标记，否则至少 5/8 会误报


def check_sub_not_reported(human, group_id, sessions, cfg, now_ms, transcript):
    """铁律一 · 派发基础 Agent 必有回报 → ALERT_SUB_NOT_REPORTED。

    判据 = 派活时刻 vs 回报时刻（都取自数字人自己的消息流），再叠加该 sub 的运行态。
    绝不碰基础 Agent 的 endedAt —— 它必然晚于回报发出时刻，拿它比恒为真，这是旧实现
    100% 误报的根因（README P0-c）。

    为什么必须叠加 sub 的 status：单靠时间阈值分不清"基础 Agent 在干活"和"卡死了"。
    实测滞后分布（两个群共 64 次派活）：中位 15 秒，P90 80 秒，**最大 333 秒**。
    基础 Agent 真在建项目 / 解读需求就是要几分钟。所以：
      · sub 还在 running  → 它在干活，用长宽限（SUB_REPORT_LAG_MS）只做兜底
      · sub 已经不 running → 它结束了却没回报，是真异常，用短宽限（SUB_DONE_LAG_MS）
    这也正是方案"基础 Agent 已完但未回报"的原意，旧实现只是把"已完"表达错了。
    """
    names = human.get("agentNames") or {}
    status_of = {s["agentId"]: s["status"] for s in sessions if s["role"] != "数字人"}
    long_lag = threshold(cfg, "SUB_REPORT_LAG_MS")
    done_lag = threshold(cfg, "SUB_DONE_LAG_MS")
    stale = []
    for sub, dispatched_at in (transcript["dispatchTo"] or {}).items():
        received_at = (transcript["recvFrom"] or {}).get(sub, 0)
        if received_at > dispatched_at:
            continue                       # 派活之后收到过回报，正常
        still_working = status_of.get(sub) == "running"
        lag = long_lag if still_working else done_lag
        if now_ms - dispatched_at <= lag:
            continue                       # 还在宽限期内
        stale.append((sub, dispatched_at, still_working))
    if not stale:
        return None

    stale.sort(key=lambda x: x[1])
    da = next((s for s in sessions if s["role"] == "数字人"), None)
    if da is None:
        return None
    who = "、".join(names.get(sub, sub) for sub, _, _ in stale)
    oldest = stale[0][1]
    da_name = names.get(human["daId"], human["daId"])
    return {
        "type": "ALERT_SUB_NOT_REPORTED",
        "severity": "ALERT",
        "daId": human["daId"],
        "groupId": group_id,
        "sessionKey": da["sessionKey"],
        "channel": da["channel"],
        "target": da["target"],
        "text": (f"⚠️ {da_name} 已把任务派给 {who}，但 {fmt_duration(now_ms - oldest)}"
                 f"没有收到回报。\n　　{recovery_hint(cfg)}"),
        "detail": {
            "subs": [sub for sub, _, _ in stale],
            "tDispatch": oldest,
            # 两级阈值：基础 Agent 还在跑用 SUB_REPORT_LAG_MS，已经停了用 SUB_DONE_LAG_MS。
            # 报最老那条用的是哪一级，才对得上它实际过的那道闸门。
            "gate": {"key": "SUB_REPORT_LAG_MS" if stale[0][2] else "SUB_DONE_LAG_MS",
                     "measured": now_ms - oldest},
            "recvFrom": dict(transcript["recvFrom"] or {}),
            "subStatus": {sub: status_of.get(sub) for sub, _, _ in stale},
            "lagMs": now_ms - oldest,
        },
    }


def check_da_not_replied_after_sub(human, group_id, sessions, cfg, now_ms, transcript):
    """铁律二-2 · 基础 Agent 回报后必转达 → ALERT_DA_NOT_REPLIED_AFTER_SUB。

    收到回报之后，数字人要么对客、要么明示不对客（NO_REPLY 家族），两者都没有且
    超过宽限期才算异常。

    "明示不对客"这条豁免是承重的：实测 8 次回报里有 5 次数字人紧接着就输出
    NO_REPLY / REPLY_SKIP / ANNOUNCE_SKIP。少了它误报率至少 5/8。
    """
    recv = transcript["recvFrom"] or {}
    if not recv:
        return None
    latest_sub, latest_recv = max(recv.items(), key=lambda kv: kv[1])
    if not latest_recv:
        return None

    # 回报之后数字人处理过了吗？对客了算、明示不对客也算
    handled_at = max(transcript["tDaReply"], transcript["tNoReplyMark"])
    if handled_at >= latest_recv:
        return None
    # 回报之后又派活了 → 还在流程里推进，不是"吞掉回报"
    if transcript["tDispatch"] > latest_recv:
        return None
    if now_ms - latest_recv <= threshold(cfg, "INTER_SESSION_ACK_MS"):
        return None

    da = next((s for s in sessions if s["role"] == "数字人"), None)
    if da is None:
        return None
    names = human.get("agentNames") or {}
    da_name = names.get(human["daId"], human["daId"])
    sub_name = names.get(latest_sub, latest_sub)
    return {
        "type": "ALERT_DA_NOT_REPLIED_AFTER_SUB",
        "severity": "ALERT",
        "daId": human["daId"],
        "groupId": group_id,
        "sessionKey": da["sessionKey"],
        "channel": da["channel"],
        "target": da["target"],
        "text": (f"⚠️ {da_name} 已收到 {sub_name} 的结果，但 "
                 f"{fmt_duration(now_ms - latest_recv)}没有转达给您。"
                 f"\n　　{recovery_hint(cfg)}"),
        "detail": {
            "gate": {"key": "INTER_SESSION_ACK_MS", "measured": now_ms - latest_recv},
            "sub": latest_sub,
            "tRecv": latest_recv,
            "tDaReply": transcript["tDaReply"],
            "tNoReplyMark": transcript["tNoReplyMark"],
            "lagMs": now_ms - latest_recv,
        },
    }


# ==================== 功能 15：CC 卡死检测 ALERT_CC_STALLED ====================
#
# 判定树 idle 分支的最后一条，也是方案里"是否在执行中"的第 4 个信号。
# 旧实现是纯空壳：枚举和文案都在，但 Context.cc_is_running 永远是默认 False，
# 全文没有一行读 cc-config，decide() 里也没有 check_E。
#
# 关联链路（实测验证过，2026-08-22）：
#   ~/.openclaw/projects/<projectKey>/META.md          里有「群聊ID」和「agentId」
#   → 项目源码目录 <项目>/src
#   → 按 / 和 . 都替换成 - 编码成 cc-config 的目录名
#   → ~/.openclaw/cc-config/projects/<enc>/session.json
# 例：/Users/x/.openclaw/projects/P-2026.../src
#     → -Users-x--openclaw-projects-P-2026---src
#
# 实测两个项目：老项目 P-20260818（群 10232767188）目录存在；今天的 P-20260822
# （群 10232962603）还在澄清阶段、没进编码，cc-config 下没有目录 —— 这正是方案
# §8.2 E 说的"定位失败降级为不巡检 CC"，不是异常。

CC_PROJECTS_DIR = os.path.join(OPENCLAW_HOME, "projects")
CC_CONFIG_DIR = os.path.join(OPENCLAW_HOME, "cc-config", "projects")


def encode_cc_dir(path):
    """把项目源码路径编码成 cc-config 下的目录名：/ 和 . 都变成 -。"""
    return path.replace("/", "-").replace(".", "-")


def read_meta_field(text, name):
    """从 META.md 里取 `- **字段名**: 值` 形式的值，取不到返回空串。"""
    for line in text.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if not stripped.startswith("**"):
            continue
        head, _, tail = stripped.partition("**:")
        if not tail:
            head, _, tail = stripped.partition("**：")
        if head.strip("* ") == name:
            return tail.strip()
    return ""


def find_cc_project(group_id, da_id=None):
    """按群号找它对应的 CC 项目，返回 {"projectKey","ccDir"}；找不到返回 None。

    群号写在 META.md 的「群聊ID」里。同一个群可能先后有多个项目，取 META.md 最新
    修改的那个（正在推进的那个项目文件才会被持续更新）。
    """
    best = None
    for meta_path in glob.glob(os.path.join(CC_PROJECTS_DIR, "*", "META.md")):
        try:
            with open(meta_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            mtime = os.path.getmtime(meta_path)
        except OSError:
            continue
        if read_meta_field(text, "群聊ID") != str(group_id):
            continue
        if da_id and read_meta_field(text, "agentId") not in ("", str(da_id)):
            continue
        if best is None or mtime > best[0]:
            project_dir = os.path.dirname(meta_path)
            best = (mtime, {
                "projectKey": os.path.basename(project_dir),
                "ccDir": os.path.join(CC_CONFIG_DIR,
                                      encode_cc_dir(os.path.join(project_dir, "src"))),
            })
    return best[1] if best else None


def read_cc_status(cc_dir):
    """读 CC 的 session.json，判断它在不在跑、最后一次触碰是什么时候。

    session.json 形如（方案 §7.4，实测一致）：
      {"sessions": {"<sid>": "0", ...}, "total_count": 2, "max_last_ts": 178...}
    sessions 的 value 是**字符串**形式的活跃计数，"0" 空闲、大于 0 表示在跑。

    返回 {"found","running","activeCount","lastTouch","lockMtime"}
    """
    out = {"found": False, "running": False, "activeCount": 0,
           "lastTouch": 0, "lockMtime": 0}
    if not cc_dir:
        return out
    data = json_object_from_file(os.path.join(cc_dir, "session.json"))
    if data is None:
        return out
    out["found"] = True
    active = 0
    for value in (data.get("sessions") or {}).values():
        try:
            active += max(0, int(str(value)))
        except (TypeError, ValueError):
            continue
    out["activeCount"] = active
    out["running"] = active > 0
    last = data.get("max_last_ts")
    out["lastTouch"] = int(last) if isinstance(last, (int, float)) and last > 0 else 0
    try:
        out["lockMtime"] = int(os.path.getmtime(os.path.join(cc_dir, "session.lock")) * 1000)
    except OSError:
        pass
    # 方案说 session.lock 的 mtime 也能辅助判活；取两者更晚的作为"最后被碰过"
    out["lastTouch"] = max(out["lastTouch"], out["lockMtime"])
    return out


def check_cc_stalled(human, group_id, sessions, cfg, now_ms, cc):
    """E · CC 卡死 → ALERT_CC_STALLED。

    判据（方案 §8.2 E）：CC 显示在跑，但已经很久没被碰过。
    "很久"取 CC_STALE_MS；定位不到 CC 项目时直接跳过，不猜。
    """
    if not cc.get("found") or not cc.get("running"):
        return None
    last_touch = cc.get("lastTouch") or 0
    if not last_touch or now_ms - last_touch <= threshold(cfg, "CC_STALE_MS"):
        return None
    da = next((s for s in sessions if s["role"] == "数字人"), None)
    if da is None:
        return None
    da_name = (human.get("agentNames") or {}).get(human["daId"], human["daId"])
    return {
        "type": "ALERT_CC_STALLED",
        "severity": "ALERT",
        "daId": human["daId"],
        "groupId": group_id,
        "sessionKey": da["sessionKey"],
        "channel": da["channel"],
        "target": da["target"],
        "text": (f"⚠️ {da_name} 的代码执行子任务无响应"
                 f"（已 {fmt_duration(now_ms - last_touch)}没有进展）。"
                 f"\n　　{recovery_hint(cfg)}"),
        "detail": {
            "gate": {"key": "CC_STALE_MS", "measured": now_ms - last_touch},
            "projectKey": cc.get("projectKey", ""),
            "activeCount": cc.get("activeCount", 0),
            "lastTouch": last_touch,
            "staleMs": now_ms - last_touch,
        },
    }


def check_user_not_replied(human, group_id, sessions, cfg, now_ms, transcript):
    """铁律二-1 · 用户消息必有回复 → ALERT_USER_NOT_REPLIED。

    两个子形态，严重程度不同，文案也不同：

    A 消息被吞（最严重）
      网关侧 lastInteractionAt 明显晚于消息流里的 tUser，说明网关收到了用户消息，
      但它**没进会话、没起 run** —— 数字人连接收消息的能力都没了。
      实测（2026-08-22 13:19/13:22 与 14:49/14:54 两次）：数字人 failover 降级到
      Qwen3-Embedding-8B（向量模型，不能做对话补全）后彻底哑掉，用户连发两条都没
      任何反应，那两条消息在消息流里根本不存在。当时 lastInteractionAt=13:22:10 而
      tUser=13:16:29，差 6 分钟。
      正常情况这两个值只差 0~1 秒（实测 4 个群分别是 -1/-0/-0/-1 秒），所以差很多
      是可信信号。

    B 收到没回（方案原本设想的形态）
      消息进了会话，但数字人一直没给用户回话。

    豁免（这几种"没回"是正常的）：
      · 该 turn 内派活给了基础 Agent → 在等回报，等它回来再判
      · 最后一条 assistant 文本是 NO_REPLY 家族 → 数字人明示这轮不用回
      · 运行态还在跑 → 那是 REMIND 管的事（本函数只在 idle 分支调用）
    """
    da = next((s for s in sessions if s["role"] == "数字人"), None)
    if da is None:
        return None
    t_user = transcript["tUser"]
    if not t_user:
        return None                      # 用户一句话都没说过，无从判断

    # 豁免：派活了在等回报 / 明示不对客
    if transcript["tDispatch"] > t_user:
        return None
    if transcript["lastNoReply"]:
        return None

    da_name = (human.get("agentNames") or {}).get(human["daId"], human["daId"])
    swallowed_gap = (da.get("lastInteractionAt") or 0) - t_user

    # ---- 子形态 A：消息被吞 ----
    if swallowed_gap > threshold(cfg, "MSG_SWALLOWED_MS"):
        return {
            "type": "ALERT_USER_NOT_REPLIED",
            "severity": "ALERT",
            "daId": human["daId"],
            "groupId": group_id,
            "sessionKey": da["sessionKey"],
            "channel": da["channel"],
            "target": da["target"],
            "text": (f"⚠️ {da_name} 收到了您的消息，但没能开始处理"
                     f"（已积压 {fmt_duration(now_ms - t_user)}），需要人工介入。\n"
                     f"　　继续发消息可能同样没有反应。"),
            "detail": {
                "gate": {"key": "MSG_SWALLOWED_MS", "measured": swallowed_gap},
                "form": "swallowed",
                "tUser": t_user,
                "lastInteractionAt": da.get("lastInteractionAt") or 0,
                "gapMs": swallowed_gap,
                "tDaReply": transcript["tDaReply"],
            },
        }

    # ---- 子形态 B：收到没回 ----
    if transcript["tDaReply"] >= t_user:
        return None                      # 数字人回过了
    quiet_ms = now_ms - t_user
    if quiet_ms <= threshold(cfg, "USER_REPLY_ACK_MS"):
        return None                      # 还在宽限期内，再等等
    return {
        "type": "ALERT_USER_NOT_REPLIED",
        "severity": "ALERT",
        "daId": human["daId"],
        "groupId": group_id,
        "sessionKey": da["sessionKey"],
        "channel": da["channel"],
        "target": da["target"],
        "text": (f"⚠️ {da_name} 可能没能回复您 {fmt_duration(quiet_ms)}前的消息。"
                 f"\n　　{recovery_hint(cfg)}"),
        "detail": {
            "gate": {"key": "USER_REPLY_ACK_MS", "measured": quiet_ms},
            "form": "no_reply",
            "tUser": t_user,
            "tDaReply": transcript["tDaReply"],
            "quietMs": quiet_ms,
            "lastInteractionAt": da.get("lastInteractionAt") or 0,
        },
    }


def check_model_error(human, group_id, sessions, cfg, now_ms, outcome, transcript=None):
    """A · 模型异常中断 → ALERT_MODEL_ERROR。

    宽限期的意义：openclaw 自己有 failover / 自动重试，异常后几十秒内可能自愈。
    只在它连重试都没救回来时才打扰用户（方案 §10.1：只在 failover 也失败后才告警）。

    两个错误来源，各有长短，都要看：
      消息流 <sid>.jsonl        实时可见、带具体原因（stopReason=error + errorMessage）
      trajectory session.ended  要到 run 结束才落盘，但带 terminalError
    实测（2026-08-22 13:16:56）消息流里是 errorMessage='Connection error.'，
    比 trajectory 的 terminalError='non_deliverable_terminal_turn' 好懂得多。

    自愈判定：出错之后数字人又给用户回过话（tDaReply > tModelError）就说明已经
    恢复，不该再报。少了这条，一次一小时前的失败会被无限期反复判定为"当前异常"。
    """
    transcript = transcript or {}
    reason = None
    anchor = 0
    run_id = outcome.get("runId") or ""

    # 优先用消息流里的错误：实时且原因具体
    t_model_error = transcript.get("tModelError") or 0
    if t_model_error:
        message = transcript.get("modelErrorMessage") or ""
        reason = f"模型调用失败：{message}" if message else "模型调用失败"
        anchor = t_model_error
        chain = [m["modelId"] for m in (transcript.get("modelChain") or [])
                 if m.get("ts", 0) >= t_model_error]
        if len(chain) > 1:
            # failover 换过好几个模型，把落点说出来 —— 实测出现过降级到 embedding
            # 模型（Qwen3-Embedding-8B）导致数字人彻底哑掉的情况
            reason += f"（已尝试切换 {len(chain)} 个模型，当前 {chain[0]}）"

    if reason is None and outcome.get("found"):
        if outcome["terminalError"]:
            code = str(outcome["terminalError"])
            reason = TERMINAL_ERROR_LABELS.get(code, code)
        elif outcome["promptErrorSource"]:
            reason = str(outcome["promptErrorSource"])
        elif outcome["flags"]:
            # aborted 但是外部主动取消 → 不是故障，不告警
            real = [(f, label) for f, label in outcome["flags"]
                    if not (f == "aborted" and outcome["externalAbort"])]
            if not real:
                return None
            reason = "、".join(label for _, label in real)
        elif outcome["status"] not in (None, "success"):
            reason = f"异常结束 status={outcome['status']}"
        anchor = outcome["endedTs"]

    if reason is None:
        return None

    # 出错之后数字人又对客了 → 已自愈，不报
    replied_after = transcript.get("tDaReply") or 0
    if anchor and replied_after > anchor:
        return None

    # 算不出锚点时退回群里最近一次活动时间，免得因为拿不到时刻就永远不告警
    if not anchor:
        anchor = max((s["activityTs"] for s in sessions), default=0)
    if not anchor:
        return None
    if now_ms - anchor <= threshold(cfg, "MODEL_ERROR_GRACE_MS"):
        return None

    da = next((s for s in sessions if s["role"] == "数字人"), None)
    if da is None:
        return None
    da_name = (human.get("agentNames") or {}).get(human["daId"], human["daId"])
    return {
        "type": "ALERT_MODEL_ERROR",
        "severity": "ALERT",
        "daId": human["daId"],
        "groupId": group_id,
        "sessionKey": da["sessionKey"],
        "channel": da["channel"],
        "target": da["target"],
        "text": (f"⚠️ {da_name} 上次模型调用异常（{reason}），本轮已停止。"
                 f"\n　　{recovery_hint(cfg)}"),
        "detail": {
            "gate": {"key": "MODEL_ERROR_GRACE_MS", "measured": now_ms - anchor},
            "runId": run_id,
            "reason": reason,
            "status": outcome.get("status"),
            "flags": [f for f, _ in outcome.get("flags") or []],
            "promptErrorSource": outcome.get("promptErrorSource"),
            "terminalError": outcome.get("terminalError"),
            "externalAbort": outcome.get("externalAbort"),
            "endedTs": outcome.get("endedTs"),
            "tModelError": t_model_error,
            "modelErrorMessage": transcript.get("modelErrorMessage") or "",
            "anchorTs": anchor,
        },
    }


# ============================== 停止信号处理 ==============================

# 收到停止信号后置位。用 Event 而不是 bool，是为了让 sleep 能被立刻叫醒。
_stop = threading.Event()


def install_signal_handlers():
    """接管 SIGINT 和 SIGTERM，收到就置位停止标志，让主循环跑完当前一轮后干净退出。

    两个信号都要管：
    - SIGINT  —— 前台运行时用户按 Ctrl+C；
    - SIGTERM —— launchd / `kill` 停进程时发的，不接管就会在半轮中间硬死。
    显式安装还有个副作用是好的：后台启动时 shell 会把 SIGINT 设成"忽略"并被
    Python 继承，这里主动安装就把它覆盖回来了。
    """
    def on_signal(signum, _frame):
        name = signal.Signals(signum).name
        log(f"收到 {name}，将在本轮结束后退出…")
        _stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)


def main():
    parser = argparse.ArgumentParser(description="数字人群会话巡检器")
    parser.add_argument("--config", default=CONFIG_PATH, help=f"配置文件路径（默认 {CONFIG_PATH}）")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出，方便手工验证")
    parser.add_argument("--discover", action="store_true",
                        help="只做巡检对象发现，打一张详细表后退出（功能 2 的验证入口）")
    parser.add_argument("--interval", type=int, help="轮询间隔（毫秒），覆盖配置文件")
    parser.add_argument("--errors", action="store_true",
                        help=f"打印历史异常记录（{ERRORS_PATH}）后退出")
    args = parser.parse_args()

    # 立刻离开继承来的工作目录。它可能已经被删掉重建（同名不同 inode），
    # 那样任何子进程都会在 getcwd() 上挂掉 —— 详见 stable_cwd() 的注释。
    # 必须在起任何子进程之前做，而且 execv 重载后会重新走到这里，所以能自愈。
    try:
        inherited = os.getcwd()
    except OSError:
        inherited = ""            # 继承来的 cwd 已经失效（目录被删掉重建）
    workdir = stable_cwd()
    try:
        os.chdir(workdir)
    except OSError as e:
        log(f"切换工作目录到 {workdir} 失败：{e!r}")
    else:
        # 措辞要让人看出这是"已经处理好了"，不是报错。实测用户第一眼读成了报错。
        if not inherited:
            log(f"继承来的工作目录已失效（目录被删掉重建过），已自动切到 {workdir}，"
                f"不影响巡检。你那个终端里 cd 一次即可恢复正常。")
        elif inherited != workdir:
            log(f"工作目录 {inherited} → {workdir}")

    cfg = load_config(args.config)
    log_file, log_max, log_keep = apply_log_settings(cfg)
    if log_file:
        dropped = cleanup_rotated(log_file, log_keep) + cleanup_rotated(ERRORS_PATH, log_keep)
        log(f"日志落盘 {log_file}，单文件上限 {log_max / 1024 / 1024:.0f}MB，"
            f"保留 {log_keep} 份历史"
            + (f"（启动时清掉 {dropped} 份超额历史）" if dropped else ""))

    if args.errors:
        print_errors()
        return 0

    if args.discover:
        print_discovery(cfg)
        return 0

    # 命令行参数优先级高于配置文件，方便调试时临时改小间隔。
    if args.interval is not None:
        cfg["interval"] = args.interval
        log(f"命令行覆盖轮询间隔：{args.interval} ms")

    if not cfg.get("enabled", True):
        log("配置 enabled=false，巡检器不启动。")
        return 0

    interval_ms = int(cfg.get("interval") or 0)
    if interval_ms <= 0:
        log(f"轮询间隔非法（interval={interval_ms}），必须是正整数毫秒。")
        return 1

    if args.once:
        inspect_once(cfg, 1)
        return 0

    # 只有持续模式才加单例锁：--once / --discover 是手工一次性动作，不该被挡住。
    lock, holder = acquire_singleton()
    if lock is None and holder is not None:
        log(f"已经有一个巡检器在跑了（pid={holder}），本次不启动。")
        log(f"要换成这个实例，先 kill {holder}。锁文件：{LOCK_PATH}")
        return 1

    log(f"巡检器启动，每 {interval_ms / 1000:g} 秒一轮，Ctrl+C 停止。")
    log(f"代码版本 {code_version()}　配置 {args.config}")
    if cfg.get("autoReload", True):
        log("已开启代码自动重载：改动 inspector.py 会在下一轮自动生效（语法不过则不重载）")
    install_signal_handlers()

    # 记下代码和配置的当前状态，之后每轮比对，变了就重载 / 热加载。
    def mtime_of(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    code_mtime = mtime_of(SELF_PATH)
    cfg_mtime = mtime_of(args.config)
    broken_code_mtime = 0      # 已经报过错的那个坏版本，别每轮重复刷

    round_no = 0
    while not _stop.is_set():
        round_no += 1
        started = time.monotonic()

        # 配置热加载：改阈值 / 白名单 / dryRun 不用重启。
        new_cfg_mtime = mtime_of(args.config)
        if new_cfg_mtime and new_cfg_mtime != cfg_mtime:
            cfg_mtime = new_cfg_mtime
            cfg = load_config(args.config)
            if args.interval is not None:
                cfg["interval"] = args.interval
            new_interval = int(cfg.get("interval") or 0)
            if new_interval > 0:
                interval_ms = new_interval
            apply_log_settings(cfg)     # log 段也支持热改，否则跟"热加载"自相矛盾
            log(f"配置已重新加载：间隔 {interval_ms / 1000:g}s、"
                f"{'dryRun' if cfg.get('dryRun', True) else '实发'}")

        try:
            inspect_once(cfg, round_no)
        except Exception as e:              # noqa: BLE001 —— 兜底，单轮失败不能让巡检器死掉
            # 正常情况下异常已经在群级别被兜住了，走到这里说明是发现阶段或
            # 更外层的问题，同样要留痕。
            record_error(e, round=round_no, phase="round")

        # 代码自动重载：放在巡检之后、睡觉之前，保证换代码不会打断一轮的中途。
        new_code_mtime = mtime_of(SELF_PATH)
        if cfg.get("autoReload", True) and new_code_mtime and new_code_mtime != code_mtime:
            ok, err = code_compiles()
            if ok:
                log(f"检测到代码更新（{code_version()}），重启加载…")
                reload_self(lock)          # 成功的话不会返回
                code_mtime = new_code_mtime
            elif new_code_mtime != broken_code_mtime:
                broken_code_mtime = new_code_mtime
                log(f"代码有语法错误，继续用当前版本跑（改好后自动重载）：{err}")

        # 减掉本轮自己的耗时，保证是"每 N 秒一轮"而不是"每轮之间隔 N 秒"。
        # wait() 收到停止信号会立刻返回，不用等睡满。
        elapsed = time.monotonic() - started
        _stop.wait(max(0.0, interval_ms / 1000 - elapsed))
    log(f"已跑 {round_no} 轮，退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
